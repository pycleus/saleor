from copy import deepcopy
from enum import Enum
from typing import Dict, List, Optional, Union, cast

import graphene
from django.db.models import Exists, OuterRef
from graphene.utils.str_converters import to_camel_case

from ...discount.models import Promotion, PromotionRule
from ...product.models import (
    Category,
    Collection,
    CollectionProduct,
    Product,
    ProductsQueryset,
    ProductVariant,
    ProductVariantQueryset,
)
from ..core.connection import where_filter_qs
from ..product.filters import (
    CategoryWhere,
    CollectionWhere,
    ProductVariantWhere,
    ProductWhere,
)


class Operators(Enum):
    AND = "and"
    OR = "or"


def clean_predicate(predicate: Union[Dict[str, Union[dict, list]], list]):
    """Convert camel cases keys into snake case."""
    if isinstance(predicate, list):
        return [
            clean_predicate(item) if isinstance(item, (dict, list)) else item
            for item in predicate
        ]
    return {
        to_camel_case(key): clean_predicate(value)
        if isinstance(value, (dict, list))
        else value
        for key, value in predicate.items()
    }


def get_products_for_promotion(promotion: Promotion) -> ProductsQueryset:
    """Get products that are included in the promotion based on catalogue predicate."""
    variants = get_variants_for_promotion(promotion)
    return Product.objects.filter(Exists(variants.filter(product_id=OuterRef("id"))))


def get_products_for_rule(rule: PromotionRule) -> ProductsQueryset:
    """Get products that are included in the rule based on catalogue predicate."""
    variants = get_variants_for_predicate(deepcopy(rule.catalogue_predicate))
    return Product.objects.filter(Exists(variants.filter(product_id=OuterRef("id"))))


def get_variants_for_promotion(promotion: Promotion) -> ProductVariantQueryset:
    """Get variants that are included in the promotion based on catalogue predicate."""
    queryset = ProductVariant.objects.none()
    for rule in promotion.rules.iterator():
        queryset |= get_variants_for_predicate(rule.catalogue_predicate)
    return queryset


def _handle_product_predicate(
    predicate_data: Dict[str, Union[dict, list]]
) -> ProductVariantQueryset:
    product_qs = where_filter_qs(
        Product.objects.all(), {}, ProductWhere, predicate_data, None
    )
    return ProductVariant.objects.filter(
        Exists(product_qs.filter(id=OuterRef("product_id")))
    )


def _handle_variant_predicate(
    predicate_data: Dict[str, Union[dict, list]]
) -> ProductVariantQueryset:
    return where_filter_qs(
        ProductVariant.objects.all(), {}, ProductVariantWhere, predicate_data, None
    )


def _handle_collection_predicate(
    predicate_data: Dict[str, Union[dict, list]]
) -> ProductVariantQueryset:
    collection_qs = where_filter_qs(
        Collection.objects.all(), {}, CollectionWhere, predicate_data, None
    )
    collection_products = CollectionProduct.objects.filter(
        Exists(collection_qs.filter(id=OuterRef("collection_id")))
    )
    products = Product.objects.filter(
        Exists(collection_products.filter(product_id=OuterRef("id")))
    )
    return ProductVariant.objects.filter(
        Exists(products.filter(id=OuterRef("product_id")))
    )


def _handle_category_predicate(
    predicate_data: Dict[str, Union[dict, list]]
) -> ProductVariantQueryset:
    category_qs = where_filter_qs(
        Category.objects.all(), {}, CategoryWhere, predicate_data, None
    )
    products = Product.objects.filter(
        Exists(category_qs.filter(id=OuterRef("category_id")))
    )
    return ProductVariant.objects.filter(
        Exists(products.filter(id=OuterRef("product_id")))
    )


PREDICATE_TO_HANDLE_METHOD = {
    "productPredicate": _handle_product_predicate,
    "variantPredicate": _handle_variant_predicate,
    "collectionPredicate": _handle_collection_predicate,
    "categoryPredicate": _handle_category_predicate,
}


def get_variants_for_predicate(
    predicate: dict, queryset: Optional[ProductVariantQueryset] = None
) -> ProductVariantQueryset:
    """Get variants that met the predicate conditions."""
    if not predicate:
        return ProductVariant.objects.none()

    if queryset is None:
        queryset = ProductVariant.objects.all()
    and_data: Optional[List[dict]] = predicate.pop("AND", None)
    or_data: Optional[List[dict]] = predicate.pop("OR", None)

    if and_data:
        queryset = _handle_and_data(queryset, and_data)

    if or_data:
        queryset = _handle_or_data(queryset, or_data)

    if predicate:
        queryset = _handle_catalogue_predicate(queryset, predicate, Operators.AND)

    return queryset


def _handle_and_data(
    queryset: ProductVariantQueryset, data: List[Dict[str, Union[list, dict, str]]]
) -> ProductVariantQueryset:
    for predicate_data in data:
        if contains_filter_operator(predicate_data):
            queryset &= get_variants_for_predicate(predicate_data, queryset)
        else:
            queryset = _handle_catalogue_predicate(
                queryset, predicate_data, Operators.AND
            )
    return queryset


def _handle_or_data(
    queryset: ProductVariantQueryset, data: List[Dict[str, Union[dict, str, list]]]
) -> ProductVariantQueryset:
    qs = queryset.model.objects.none()
    for predicate_data in data:
        if contains_filter_operator(predicate_data):
            qs |= get_variants_for_predicate(predicate_data, queryset)
        else:
            qs = _handle_catalogue_predicate(qs, predicate_data, Operators.OR)
    queryset &= qs
    return queryset


def contains_filter_operator(input: Dict[str, Union[dict, str, list]]) -> bool:
    return any([operator in input for operator in ["AND", "OR", "NOT"]])


def _handle_catalogue_predicate(
    queryset: ProductVariantQueryset,
    predicate_data: Dict[str, Union[dict, str, list]],
    operator,
) -> ProductVariantQueryset:
    for field, handle_method in PREDICATE_TO_HANDLE_METHOD.items():
        if field_data := predicate_data.get(field):
            field_data = cast(Dict[str, Union[dict, list]], field_data)
            if operator == Operators.AND:
                queryset &= handle_method(field_data)
            else:
                queryset |= handle_method(field_data)
    return queryset


def convert_migrated_sale_catalogue_predicate(
    promotion: Promotion,
) -> Optional[Dict[str, List[int]]]:
    """Convert catalogue predicate of Promotion created from old sale.

    All migrated sales have related PromotionRule with "OR" catalogue predicate. This
    function converts:
        {
            "OR": [
                {"collectionPredicate": {"ids": ["UHJvZHV3","UHJvZHV2","UHJvZHV1]}},
                {"productPredicate": {"ids": ["UHJvZHV9","UHJvZHV8","UHJvZHV7]}},
            ]
        }
    into:
        {
            "collectionPredicate": [1,2,3],
            "productPredicate": [9,8,7],
        }
    """
    if rule := promotion.rules.first():
        if catalogue_predicate := rule.catalogue_predicate:
            predicates = {
                list(item.keys())[0]: list(item.values())[0]["ids"]
                for item in catalogue_predicate["OR"]
            }
            for key, ids in predicates.items():
                predicates[key] = [graphene.Node.from_global_id(id)[1] for id in ids]
            return predicates
    return None
