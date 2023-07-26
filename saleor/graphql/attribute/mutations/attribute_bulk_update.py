from collections import defaultdict
from typing import List, Tuple, Union

import graphene
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.text import slugify
from graphene.utils.str_converters import to_camel_case
from graphql.error import GraphQLError
from text_unidecode import unidecode

from ....attribute import models
from ....attribute.error_codes import AttributeBulkUpdateErrorCode
from ....core.tracing import traced_atomic_transaction
from ....permission.enums import PageTypePermissions, ProductTypePermissions
from ...core import ResolveInfo
from ...core.descriptions import ADDED_IN_315, PREVIEW_FEATURE
from ...core.doc_category import DOC_CATEGORY_ATTRIBUTES
from ...core.enums import ErrorPolicyEnum
from ...core.mutations import BaseMutation, ModelMutation
from ...core.types import (
    AttributeBulkUpdateError,
    BaseInputObjectType,
    BaseObjectType,
    NonNullList,
)
from ...core.utils import (  # get_duplicated_values,
    WebhookEventAsyncType,
    WebhookEventInfo,
    from_global_id_or_error,
)
from ...core.validators import validate_one_of_args_is_in_mutation
from ...plugins.dataloaders import get_plugin_manager_promise
from ..enums import AttributeTypeEnum
from ..types import Attribute
from .attribute_update import AttributeUpdateInput

ONLY_SWATCH_FIELDS = ["file_url", "content_type", "value"]


class AttributeBulkUpdateResult(BaseObjectType):
    attribute = graphene.Field(Attribute, description="Attribute data.")
    errors = NonNullList(
        AttributeBulkUpdateError,
        required=False,
        description="List of errors occurred on update attempt.",
    )

    class Meta:
        doc_category = DOC_CATEGORY_ATTRIBUTES


def get_results(
    instances_data_with_errors_list: list[dict], reject_everything: bool = False
) -> list[AttributeBulkUpdateResult]:
    return [
        AttributeBulkUpdateResult(
            attribute=None if reject_everything else data.get("instance"),
            errors=data.get("errors"),
        )
        for data in instances_data_with_errors_list
    ]


class AttributeBulkUpdateInput(BaseInputObjectType):
    id = graphene.ID(description="ID of an attribute to update.", required=False)
    external_reference = graphene.String(
        description="External ID of this attribute.", required=False
    )
    fields = AttributeUpdateInput(description="Fields to update.", required=True)

    class Meta:
        doc_category = DOC_CATEGORY_ATTRIBUTES


class AttributeBulkUpdate(BaseMutation):
    count = graphene.Int(
        required=True,
        description="Returns how many objects were updated.",
    )
    results = NonNullList(
        AttributeBulkUpdateResult,
        required=True,
        default_value=[],
        description="List of the updated attributes.",
    )

    class Arguments:
        attributes = NonNullList(
            AttributeBulkUpdateInput,
            required=True,
            description="Input list of attributes to update.",
        )
        error_policy = ErrorPolicyEnum(
            required=False,
            description="Policies of error handling. DEFAULT: "
            + ErrorPolicyEnum.REJECT_EVERYTHING.name,
        )

    class Meta:
        description = "Updates attributes." + ADDED_IN_315 + PREVIEW_FEATURE
        doc_category = DOC_CATEGORY_ATTRIBUTES
        error_type_class = AttributeBulkUpdateError
        webhook_events_info = [
            WebhookEventInfo(
                type=WebhookEventAsyncType.ATTRIBUTE_UPDATED,
                description="An attribute was updated.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ATTRIBUTE_VALUE_CREATED,
                description="An attribute value was created.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ATTRIBUTE_VALUE_DELETED,
                description="An attribute value was deleted.",
            ),
        ]

    @classmethod
    def clean_attributes(
        cls,
        info: ResolveInfo,
        attributes_data: List[AttributeBulkUpdateInput],
        index_error_map: dict[int, List[AttributeBulkUpdateError]],
    ):
        cleaned_inputs_map: dict = {}

        # attr_input_external_refs = [
        #     attribute_data.external_reference
        #     for attribute_data in attributes_data
        #     if attribute_data.external_reference
        # ]
        # attrs_existing_external_refs = set(
        #     models.Attribute.objects.filter(
        #         external_reference__in=attr_input_external_refs
        #     ).values_list("external_reference", flat=True)
        # )
        # duplicated_external_ref = get_duplicated_values(attr_input_external_refs)

        existing_slugs = set(
            models.Attribute.objects.filter(
                slug__in=[
                    slugify(unidecode(attribute_data.fields.name))
                    for attribute_data in attributes_data
                    if attribute_data.fields.name
                ]
            ).values_list("slug", flat=True)
        )

        attributes = cls.get_attributes(attributes_data)

        for attribute_index, attribute_data in enumerate(attributes_data):
            external_ref = attribute_data.external_reference

            try:
                validate_one_of_args_is_in_mutation(
                    "id",
                    attribute_data.id,
                    "external_reference",
                    external_ref,
                    use_camel_case=True,
                )
            except ValidationError as exc:
                index_error_map[attribute_index].append(
                    AttributeBulkUpdateError(
                        message=exc.message,
                        code=AttributeBulkUpdateErrorCode.INVALID.value,
                    )
                )
                cleaned_inputs_map[attribute_index] = None
                continue

            if attribute_data.id:
                try:
                    obj_type, db_id = from_global_id_or_error(
                        attribute_data.id, only_type="Attribute", raise_error=True
                    )
                except GraphQLError as exc:
                    index_error_map[attribute_index].append(
                        AttributeBulkUpdateError(
                            path="id",
                            message=str(exc),
                            code=AttributeBulkUpdateErrorCode.INVALID.value,
                        )
                    )
                    cleaned_inputs_map[attribute_index] = None
                    continue

                attribute_data["db_id"] = db_id

            # if external_ref in duplicated_external_ref:
            #     index_error_map[attribute_index].append(
            #         AttributeBulkUpdateError(
            #             path="externalReference",
            #             message="Duplicated external reference.",
            #             code=AttributeBulkUpdateErrorCode.DUPLICATED_INPUT_ITEM.value,
            #         )
            #     )
            #     cleaned_inputs_map[attribute_index] = None
            #     continue
            #
            # if external_ref and external_ref in attrs_existing_external_refs:
            #     index_error_map[attribute_index].append(
            #         AttributeBulkUpdateError(
            #             path="externalReference",
            #             message="External reference already exists.",
            #             code=AttributeBulkUpdateErrorCode.UNIQUE.value,
            #         )
            #     )
            #     cleaned_inputs_map[attribute_index] = None
            #     continue

            cleaned_input = cls.clean_attribute_input(
                info,
                attribute_data,
                attribute_index,
                existing_slugs,
                attributes,
                index_error_map,
            )
            cleaned_inputs_map[attribute_index] = cleaned_input
        return cleaned_inputs_map

    @classmethod
    def clean_attribute_input(
        cls,
        info: ResolveInfo,
        attribute_data: AttributeBulkUpdateInput,
        attribute_index: int,
        existing_slugs: set,
        attributes,
        index_error_map: dict[int, List[AttributeBulkUpdateError]],
    ):
        remove_values = attribute_data.fields.pop("remove_values", [])
        attribute_data.fields.pop("add_values", [])

        attr = cls.find_attribute(
            attribute_data.get("db_id"),
            attribute_data.get("external_reference"),
            attributes,
            attribute_index,
            index_error_map,
        )

        if not attr:
            return None

        attribute_data["instance"] = attr

        # check permissions based on attribute type
        permissions: Union[Tuple[ProductTypePermissions], Tuple[PageTypePermissions]]
        if attr.type == AttributeTypeEnum.PRODUCT_TYPE.value:
            permissions = (ProductTypePermissions.MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES,)
        else:
            permissions = (PageTypePermissions.MANAGE_PAGE_TYPES_AND_ATTRIBUTES,)

        if not cls.check_permissions(info.context, permissions):
            index_error_map[attribute_index].append(
                AttributeBulkUpdateError(
                    message=(
                        "You have no permission to manage this type of attributes. "
                        f"You need one of the following permissions: {permissions}"
                    ),
                    code=AttributeBulkUpdateErrorCode.REQUIRED.value,
                )
            )
            return None

        attribute_data["fields"] = ModelMutation.clean_input(
            info, None, attribute_data.fields, input_cls=AttributeUpdateInput
        )

        if remove_values:
            cleaned_remove_values = cls.clean_remove_values(
                remove_values, attr, attribute_index, index_error_map
            )
            attribute_data["fields"]["remove_values"] = cleaned_remove_values

        return attribute_data

    @classmethod
    def clean_remove_values(
        cls, remove_values, attribute, attribute_index, index_error_map
    ):
        clean_remove_values = []

        for index, value_global_id in enumerate(remove_values):
            try:
                obj_type, value_db_id = from_global_id_or_error(
                    value_global_id, only_type="AttributeValue", raise_error=True
                )
            except GraphQLError as exc:
                index_error_map[attribute_index].append(
                    AttributeBulkUpdateError(
                        path=f"removeValues.{index}",
                        message=str(exc),
                        code=AttributeBulkUpdateErrorCode.INVALID.value,
                    )
                )
                continue

            # values are prefetched
            values = attribute.values.all()
            value = next((obj for obj in values if str(obj.pk) == value_db_id), None)

            if not value:
                msg = f"Value {value_global_id} does not belong to this attribute."
                index_error_map[attribute_index].append(
                    AttributeBulkUpdateError(
                        path=f"removeValues.{index}",
                        message=msg,
                        code=AttributeBulkUpdateErrorCode.INVALID.value,
                    )
                )
            else:
                clean_remove_values.append(value)
        return clean_remove_values

    @classmethod
    def update_attributes(
        cls,
        info: ResolveInfo,
        cleaned_inputs_map: dict[int, dict],
        error_policy: str,
        index_error_map: dict[int, List[AttributeBulkUpdateError]],
    ) -> List[dict]:
        instances_data_and_errors_list: List[dict] = []

        for index, cleaned_input in cleaned_inputs_map.items():
            if not cleaned_input:
                instances_data_and_errors_list.append(
                    {"instance": None, "errors": index_error_map[index]}
                )
                continue

            attr = cleaned_input.get("instance")
            fields = cleaned_input["fields"]
            remove_values = fields.pop("remove_values", [])
            add_values = fields.pop("add_values", [])

            try:
                if fields:
                    attr = cls.construct_instance(attr, fields)
                    cls.clean_instance(info, attr)

                instances_data_and_errors_list.append(
                    {
                        "instance": attr,
                        "errors": index_error_map[index],
                        "remove_values": remove_values,
                        "add_values": add_values,
                        "attribute_updated": True if fields else False,
                    }
                )
            except ValidationError as exc:
                for key, errors in exc.error_dict.items():
                    for e in errors:
                        index_error_map[index].append(
                            AttributeBulkUpdateError(
                                path=to_camel_case(key),
                                message=e.messages[0],
                                code=e.code,
                            )
                        )
                instances_data_and_errors_list.append(
                    {"instance": None, "errors": index_error_map[index]}
                )
                continue

        if error_policy == ErrorPolicyEnum.REJECT_FAILED_ROWS.value:
            for instance_data in instances_data_and_errors_list:
                if instance_data["errors"]:
                    instance_data["instance"] = None

        return instances_data_and_errors_list

    @classmethod
    def get_attributes(cls, attributes_data: dict) -> list[models.Attribute]:
        lookup = Q()

        for attribute_input in attributes_data:
            external_ref = attribute_input.get("external_reference")
            attribute_id = attribute_input.get("id")

            if not external_ref and not attribute_id:
                continue

            single_attr_lookup = Q()

            if attribute_id:
                single_attr_lookup |= Q(
                    pk=graphene.Node.from_global_id(attribute_id)[1]
                )
            else:
                single_attr_lookup |= Q(external_reference=external_ref)
            lookup |= single_attr_lookup

        attributes = models.Attribute.objects.filter(lookup).prefetch_related("values")
        return list(attributes)

    @classmethod
    def find_attribute(
        cls, attr_id, external_ref, attributes, attribute_index, index_error_map
    ):
        if attr_id:
            attr = next((obj for obj in attributes if str(obj.pk) == attr_id), None)
        else:
            attr = next(
                (
                    obj
                    for obj in attributes
                    if str(obj.external_reference) == external_ref
                ),
                None,
            )
        if not attr:
            index_error_map[attribute_index].append(
                AttributeBulkUpdateError(
                    message="Couldn't resolve to an object.",
                    code=AttributeBulkUpdateErrorCode.NOT_FOUND.value,
                    path="id" if attr_id else "externalReference",
                )
            )

        return attr

    @classmethod
    def save(
        cls, instances_data_with_errors_list: list[dict]
    ) -> list[models.Attribute]:
        attributes_to_update: list = []
        values_to_create: list = []
        values_to_remove: list = []
        updated_attributes: list = []

        for attribute_data in instances_data_with_errors_list:
            attribute = attribute_data["instance"]

            if attribute_data.get("attribute_updated"):
                attributes_to_update.append(attribute)
            else:
                updated_attributes.append(attribute)

            values_to_remove.extend(attribute_data["remove_values"])
            values_to_remove.extend(attribute_data["add_values"])

        models.Attribute.objects.bulk_update(
            attributes_to_update,
            [
                "name",
                "slug",
                "unit",
                "value_required",
                "visible_in_storefront",
                "is_variant_only",
                "filterable_in_dashboard",
                "external_reference",
            ],
        )

        models.AttributeValue.objects.filter(
            id__in=[values_to_remove.id for values_to_remove in values_to_remove]
        ).delete()
        models.AttributeValue.objects.bulk_create(values_to_create)

        updated_attributes.extend(attributes_to_update)
        return updated_attributes, values_to_remove, values_to_create

    @classmethod
    def post_save_actions(
        cls,
        info: ResolveInfo,
        attributes: list[models.Attribute],
        values_to_remove: list[models.AttributeValue],
        values_to_create: list[models.AttributeValue],
    ):
        manager = get_plugin_manager_promise(info.context).get()

        for attribute in attributes:
            cls.call_event(manager.attribute_updated, attribute)
        for value in values_to_create:
            cls.call_event(manager.attribute_value_created, value)
        for value in values_to_remove:
            cls.call_event(manager.attribute_value_deleted, value)

    @classmethod
    @traced_atomic_transaction()
    def perform_mutation(cls, root, info, **data):
        index_error_map: dict = defaultdict(list)
        error_policy = data.get("error_policy", ErrorPolicyEnum.REJECT_EVERYTHING.value)

        # clean and validate inputs
        cleaned_inputs_map = cls.clean_attributes(
            info, data["attributes"], index_error_map
        )
        instances_data_with_errors_list = cls.update_attributes(
            info, cleaned_inputs_map, error_policy, index_error_map
        )

        # check if errors occurred
        inputs_have_errors = next(
            (True for errors in index_error_map.values() if errors), False
        )

        if (
            inputs_have_errors
            and error_policy == ErrorPolicyEnum.REJECT_EVERYTHING.value
        ):
            results = get_results(instances_data_with_errors_list, True)
            return AttributeBulkUpdate(count=0, results=results)

        # save all objects
        attributes, values_to_remove, values_to_create = cls.save(
            instances_data_with_errors_list
        )

        # prepare and return data
        results = get_results(instances_data_with_errors_list)
        cls.post_save_actions(info, attributes, values_to_remove, values_to_create)

        return AttributeBulkUpdate(count=len(attributes), results=results)
