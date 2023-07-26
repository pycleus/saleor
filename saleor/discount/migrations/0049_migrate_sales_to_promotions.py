# Generated by Django 3.2.18 on 2023-07-06 09:43
from django.db import migrations
from dataclasses import dataclass
from typing import Dict, List

import graphene
from django.db.models import Exists, OuterRef


# The batch of size 100 takes ~1.2 second and consumes ~25MB memory at peak
BATCH_SIZE = 100


def convert_sale_into_promotion(Promotion, sale):
    return Promotion(
        name=sale.name,
        old_sale_id=sale.id,
        start_date=sale.start_date,
        end_date=sale.end_date,
        created_at=sale.created_at,
        updated_at=sale.updated_at,
    )


def create_promotion_rule(PromotionRule, sale, promotion, discount_value=None):
    return PromotionRule(
        name="",
        promotion=promotion,
        catalogue_predicate=create_catalogue_predicate_from_sale(sale),
        reward_value_type=sale.type,
        reward_value=discount_value,
    )


def create_catalogue_predicate_from_sale(sale):
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
    return create_catalogue_predicate(
        collection_ids, category_ids, product_ids, variant_ids
    )


def create_catalogue_predicate(collection_ids, category_ids, product_ids, variant_ids):
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


def migrate_sales_to_promotions(Sale, Promotion, sales_pks, saleid_promotion_map):
    if sales := Sale.objects.filter(pk__in=sales_pks).order_by("pk"):
        for sale in sales:
            saleid_promotion_map[sale.id] = convert_sale_into_promotion(Promotion, sale)
        Promotion.objects.bulk_create(saleid_promotion_map.values())


def migrate_sale_listing_to_promotion_rules(
    RuleInfo,
    PromotionRule,
    sale_listings,
    saleid_promotion_map,
    rules_info,
):
    if sale_listings:
        for sale_listing in sale_listings:
            promotion = saleid_promotion_map[sale_listing.sale_id]
            rules_info.append(
                RuleInfo(
                    rule=create_promotion_rule(
                        PromotionRule,
                        sale_listing.sale,
                        promotion,
                        sale_listing.discount_value,
                    ),
                    sale_id=sale_listing.sale_id,
                    channel_id=sale_listing.channel_id,
                )
            )

        promotion_rules = [rules_info.rule for rules_info in rules_info]
        PromotionRule.objects.bulk_create(promotion_rules)
        for rule_info in rules_info:
            rule_info.add_rule_to_channel()


def migrate_sales_to_promotion_rules(
    Sale, PromotionRule, sales_pks, saleid_promotion_map
):
    if sales := Sale.objects.filter(pk__in=sales_pks).order_by("pk"):
        rules: List[PromotionRule] = []
        for sale in sales:
            promotion = saleid_promotion_map[sale.id]
            rules.append(create_promotion_rule(PromotionRule, sale, promotion))
        PromotionRule.objects.bulk_create(rules)


def migrate_translations(
    SaleTranslation, PromotionTranslation, sales_pks, saleid_promotion_map
):
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


def migrate_checkout_line_discounts(
    CheckoutLineDiscount, sales_pks, rule_by_channel_and_sale
):
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


def migrate_order_line_discounts(
    OrderLineDiscount, sales_pks, rule_by_channel_and_sale
):
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


def get_rule_by_channel_sale(rules_info):
    return {
        f"{rule_info.channel_id}_{rule_info.sale_id}": rule_info.rule
        for rule_info in rules_info
    }


def channel_listing_in_batches(qs):
    first_sale_id = 0
    while True:
        batch_1 = qs.filter(sale_id__gt=first_sale_id)[:BATCH_SIZE]
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


def queryset_in_batches(queryset):
    start_pk = 0
    while True:
        qs = queryset.filter(pk__gt=start_pk)[:BATCH_SIZE]
        pks = list(qs.values_list("pk", flat=True))
        if not pks:
            break
        yield pks
        start_pk = pks[-1]


def run_migration(apps, _schema_editor):
    Promotion = apps.get_model("discount", "Promotion")
    PromotionRule = apps.get_model("discount", "PromotionRule")
    SaleChannelListing = apps.get_model("discount", "SaleChannelListing")
    SaleTranslation = apps.get_model("discount", "SaleTranslation")
    Sale = apps.get_model("discount", "Sale")
    PromotionTranslation = apps.get_model("discount", "PromotionTranslation")
    CheckoutLineDiscount = apps.get_model("discount", "CheckoutLineDiscount")
    OrderLineDiscount = apps.get_model("discount", "OrderLineDiscount")

    @dataclass
    class RuleInfo:
        rule: PromotionRule
        sale_id: int
        channel_id: int

        def add_rule_to_channel(self):
            self.rule.channels.add(self.channel_id)

    sales_listing = SaleChannelListing.objects.order_by("sale_id")
    for sale_listing_batch_pks in channel_listing_in_batches(sales_listing):
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
        rules_info: List[RuleInfo] = []

        migrate_sales_to_promotions(
            Sale, Promotion, sales_batch_pks, saleid_promotion_map
        )
        migrate_sale_listing_to_promotion_rules(
            RuleInfo,
            PromotionRule,
            sales_listing_batch,
            saleid_promotion_map,
            rules_info,
        )
        migrate_translations(
            SaleTranslation, PromotionTranslation, sales_batch_pks, saleid_promotion_map
        )

        rule_by_channel_and_sale = get_rule_by_channel_sale(rules_info)
        migrate_checkout_line_discounts(
            CheckoutLineDiscount, sales_batch_pks, rule_by_channel_and_sale
        )
        migrate_order_line_discounts(
            OrderLineDiscount, sales_batch_pks, rule_by_channel_and_sale
        )

    # migrate sales not listed in any channel
    sales_not_listed = Sale.objects.filter(
        ~Exists(sales_listing.filter(sale_id=OuterRef("pk")))
    ).order_by("pk")
    for sales_batch_pks in queryset_in_batches(sales_not_listed):
        saleid_promotion_map = {}
        migrate_sales_to_promotions(
            Sale, Promotion, sales_batch_pks, saleid_promotion_map
        )
        migrate_sales_to_promotion_rules(
            Sale, PromotionRule, sales_batch_pks, saleid_promotion_map
        )
        migrate_translations(
            SaleTranslation, PromotionTranslation, sales_batch_pks, saleid_promotion_map
        )


class Migration(migrations.Migration):
    dependencies = [
        ("discount", "0048_promotiontranslation"),
    ]

    operations = [migrations.RunPython(run_migration, migrations.RunPython.noop)]
