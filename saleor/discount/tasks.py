from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

import graphene
import pytz
from celery.utils.log import get_task_logger
from django.db.models import Exists, F, OuterRef, Q

from ..celeryconf import app
from ..graphql.discount.mutations.utils import CATALOGUE_FIELD_TO_TYPE_NAME
from ..plugins.manager import get_plugins_manager
from ..product.tasks import update_products_discounted_prices_of_catalogues_task
from .models import (
    CheckoutLineDiscount,
    OrderLineDiscount,
    Promotion,
    PromotionRule,
    PromotionTranslation,
    Sale,
    SaleChannelListing,
    SaleTranslation,
)
from .utils import CATALOGUE_FIELDS, CatalogueInfo

task_logger = get_task_logger(__name__)


@app.task
def handle_sale_toggle():
    """Send the notification about sales toggle and recalculate discounted prcies.

    Send the notifications about starting or ending sales and call recalculation
    of product discounted prices.
    """
    manager = get_plugins_manager()

    sales = get_sales_to_notify_about()

    sale_id_to_catalogue_infos, catalogue_infos = fetch_catalogue_infos(sales)

    if not sales:
        return

    for sale in sales:
        catalogues = sale_id_to_catalogue_infos.get(sale.id)
        manager.sale_toggle(sale, catalogues)

    if catalogue_infos:
        # Recalculate discounts of affected products
        update_products_discounted_prices_of_catalogues_task.delay(
            product_ids=list(catalogue_infos["products"]),
            category_ids=list(catalogue_infos["categories"]),
            collection_ids=list(catalogue_infos["collections"]),
            variant_ids=list(catalogue_infos["variants"]),
        )

    sale_ids = ", ".join([str(sale.id) for sale in sales])
    sales.update(notification_sent_datetime=datetime.now(pytz.UTC))

    task_logger.info("The sale_toggle webhook sent for sales with ids: %s", sale_ids)


def fetch_catalogue_infos(sales):
    catalogue_info = defaultdict(set)
    sale_id_to_catalogue_info: Dict[int, CatalogueInfo] = defaultdict(
        lambda: defaultdict(set)
    )
    for sale_data in sales.values("id", *CATALOGUE_FIELDS):
        sale_id = sale_data["id"]
        for field in CATALOGUE_FIELDS:
            if id := sale_data.get(field):
                type_name = CATALOGUE_FIELD_TO_TYPE_NAME[field]
                global_id = graphene.Node.to_global_id(type_name, id)
                sale_id_to_catalogue_info[sale_id][field].add(global_id)
                catalogue_info[field].add(id)

    return sale_id_to_catalogue_info, catalogue_info


def get_sales_to_notify_about():
    """Return sales for which the notify should be sent.

    The notification should be sent for sales for which the start date or end date
    has passed and the notification date is null or the last notification was sent
    before the start or end date.
    """
    now = datetime.now(pytz.UTC)
    sales = Sale.objects.filter(
        (
            (
                Q(notification_sent_datetime__isnull=True)
                | Q(notification_sent_datetime__lt=F("start_date"))
            )
            & Q(start_date__lte=now)
        )
        | (
            (
                Q(notification_sent_datetime__isnull=True)
                | Q(notification_sent_datetime__lt=F("end_date"))
            )
            & Q(end_date__lte=now)
        )
    ).distinct()
    return sales


class SaleToPromotionConverter:
    # The batch of size 100 takes ~1.2 second and consumes ~25MB memory at peak
    BATCH_SIZE = 100

    @dataclass
    class RuleInfo:
        rule: PromotionRule
        sale_id: int
        channel_id: int

        def add_rule_to_channel(self):
            self.rule.channels.add(self.channel_id)

    @staticmethod
    def convert_sale_into_promotion(sale):
        return Promotion(
            name=sale.name,
            old_sale_id=sale.id,
            start_date=sale.start_date,
            end_date=sale.end_date,
            created_at=sale.created_at,
            updated_at=sale.updated_at,
        )

    @classmethod
    def create_promotion_rule(cls, sale, promotion, discount_value=None):
        return PromotionRule(
            name="",
            promotion=promotion,
            catalogue_predicate=cls.create_catalogue_predicate(sale),
            reward_value_type=sale.type,
            reward_value=discount_value,
        )

    @staticmethod
    def create_catalogue_predicate(sale):
        collection_ids = [
            graphene.Node.to_global_id("Collection", pk)
            for pk in sale.collections.values_list("pk", flat=True)
        ]
        category_ids = [
            graphene.Node.to_global_id("Category", pk)
            for pk in sale.categories.values_list("pk", flat=True)
        ]
        product_ids = [
            graphene.Node.to_global_id("Product", pk)
            for pk in sale.products.values_list("pk", flat=True)
        ]
        variant_ids = [
            graphene.Node.to_global_id("ProductVariant", pk)
            for pk in sale.variants.values_list("pk", flat=True)
        ]

        predicate: Dict[str, List] = {"OR": []}
        if collection_ids:
            predicate["OR"].append({"collectionPredicate": {"ids": collection_ids}})
        if category_ids:
            predicate["OR"].append({"categoryPredicate": {"ids": category_ids}})
        if product_ids:
            predicate["OR"].append({"productPredicate": {"ids": product_ids}})
        if variant_ids:
            predicate["OR"].append({"variantPredicate": {"ids": variant_ids}})

        return predicate

    @classmethod
    def migrate_sales_to_promotions(cls, sales_pks, saleid_promotion_map):
        if sales := Sale.objects.filter(pk__in=sales_pks).order_by("pk"):
            for sale in sales:
                saleid_promotion_map[sale.id] = cls.convert_sale_into_promotion(sale)
            Promotion.objects.bulk_create(saleid_promotion_map.values())

    @classmethod
    def migrate_sale_listing_to_promotion_rules(
        cls,
        sale_listings,
        saleid_promotion_map,
        rules_info,
    ):
        if sale_listings:
            for sale_listing in sale_listings:
                promotion = saleid_promotion_map[sale_listing.sale_id]
                rules_info.append(
                    cls.RuleInfo(
                        rule=cls.create_promotion_rule(
                            sale_listing.sale, promotion, sale_listing.discount_value
                        ),
                        sale_id=sale_listing.sale_id,
                        channel_id=sale_listing.channel_id,
                    )
                )

            promotion_rules = [rules_info.rule for rules_info in rules_info]
            PromotionRule.objects.bulk_create(promotion_rules)
            for rule_info in rules_info:
                rule_info.add_rule_to_channel()

    @classmethod
    def migrate_sales_to_promotion_rules(cls, sales_pks, saleid_promotion_map):
        if sales := Sale.objects.filter(pk__in=sales_pks).order_by("pk"):
            rules: List[PromotionRule] = []
            for sale in sales:
                promotion = saleid_promotion_map[sale.id]
                rules.append(cls.create_promotion_rule(sale, promotion))
            PromotionRule.objects.bulk_create(rules)

    @staticmethod
    def migrate_translations(sales_pks, saleid_promotion_map):
        if sale_translations := SaleTranslation.objects.filter(sale_id__in=sales_pks):
            promotion_translations = [
                PromotionTranslation(
                    name=translation.name,
                    language_code=translation.language_code,
                    promotion=saleid_promotion_map[translation.sale_id],
                )
                for translation in sale_translations
            ]
            PromotionTranslation.objects.bulk_create(promotion_translations)

    @staticmethod
    def migrate_checkout_line_discounts(sales_pks, rule_by_channel_and_sale):
        if checkout_line_discounts := CheckoutLineDiscount.objects.filter(
            sale_id__in=sales_pks
        ).select_related("line__checkout"):
            for checkout_line_discount in checkout_line_discounts:
                if checkout_line := checkout_line_discount.line:
                    channel_id = checkout_line.checkout.channel_id
                    sale_id = checkout_line_discount.sale_id
                    lookup = f"{channel_id}_{sale_id}"
                    if promotion_rule := rule_by_channel_and_sale.get(lookup):
                        checkout_line_discount.promotion_rule = promotion_rule

            CheckoutLineDiscount.objects.bulk_update(
                checkout_line_discounts, ["promotion_rule_id"]
            )

    @staticmethod
    def migrate_order_line_discounts(sales_pks, rule_by_channel_and_sale):
        if order_line_discounts := OrderLineDiscount.objects.filter(
            sale_id__in=sales_pks
        ).select_related("line__order"):
            for order_line_discount in order_line_discounts:
                if order_line := order_line_discount.line:
                    channel_id = order_line.order.channel_id
                    sale_id = order_line_discount.sale_id
                    lookup = f"{channel_id}_{sale_id}"
                    if promotion_rule := rule_by_channel_and_sale.get(lookup):
                        order_line_discount.promotion_rule = promotion_rule

            OrderLineDiscount.objects.bulk_update(
                order_line_discounts, ["promotion_rule_id"]
            )

    @staticmethod
    def get_rule_by_channel_sale(rules_info):
        return {
            f"{rule_info.channel_id}_{rule_info.sale_id}": rule_info.rule
            for rule_info in rules_info
        }

    @classmethod
    def channel_listing_in_batches(cls, qs):
        first_sale_id = 0
        while True:
            batch_1 = qs.filter(sale_id__gt=first_sale_id)[: cls.BATCH_SIZE]
            if len(batch_1) == 0:
                break
            last_sale_id = batch_1[len(batch_1) - 1].sale_id

            # `batch_2` extends initial `batch_1` to include all records from
            # `SaleChannelListing` which refer to `last_sale_id`
            batch_2 = qs.filter(sale_id__gt=first_sale_id, sale_id__lte=last_sale_id)
            pks = list(batch_2.values_list("pk", flat=True))
            if not pks:
                break
            yield pks
            first_sale_id = batch_2[len(batch_2) - 1].sale_id

    @classmethod
    def queryset_in_batches(cls, queryset):
        start_pk = 0
        while True:
            qs = queryset.filter(pk__gt=start_pk)[: cls.BATCH_SIZE]
            pks = list(qs.values_list("pk", flat=True))
            if not pks:
                break
            yield pks
            start_pk = pks[-1]

    @classmethod
    def convert_sales_to_promotions(cls):
        sales_listing = SaleChannelListing.objects.order_by("sale_id")
        for sale_listing_batch_pks in cls.channel_listing_in_batches(sales_listing):
            sales_listing_batch = (
                SaleChannelListing.objects.filter(pk__in=sale_listing_batch_pks)
                .order_by("sale_id")
                .prefetch_related(
                    "sale",
                    "sale__collections",
                    "sale__categories",
                    "sale__products",
                    "sale__variants",
                )
            )
            sales_batch_pks = {listing.sale_id for listing in sales_listing_batch}

            saleid_promotion_map: Dict[int, Promotion] = {}
            rules_info: List[cls.RuleInfo] = []

            cls.migrate_sales_to_promotions(sales_batch_pks, saleid_promotion_map)
            cls.migrate_sale_listing_to_promotion_rules(
                sales_listing_batch,
                saleid_promotion_map,
                rules_info,
            )
            cls.migrate_translations(sales_batch_pks, saleid_promotion_map)

            rule_by_channel_and_sale = cls.get_rule_by_channel_sale(rules_info)
            cls.migrate_checkout_line_discounts(
                sales_batch_pks, rule_by_channel_and_sale
            )
            cls.migrate_order_line_discounts(sales_batch_pks, rule_by_channel_and_sale)

        # migrate sales not listed in any channel
        sales_not_listed = Sale.objects.filter(
            ~Exists(sales_listing.filter(sale_id=OuterRef("pk")))
        ).order_by("pk")
        for sales_batch_pks in cls.queryset_in_batches(sales_not_listed):
            saleid_promotion_map = {}
            cls.migrate_sales_to_promotions(sales_batch_pks, saleid_promotion_map)
            cls.migrate_sales_to_promotion_rules(sales_batch_pks, saleid_promotion_map)
            cls.migrate_translations(sales_batch_pks, saleid_promotion_map)
