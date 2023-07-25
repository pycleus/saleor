import graphene

from ....tests.utils import (
    assert_no_permission,
    get_graphql_content,
    get_graphql_content_from_response,
)

QUERY_SALE_BY_ID = """
    query Sale($id: ID!) {
        sale(id: $id) {
            id
            name
            type
            discountValue
            currency
            collections(first: 1) {
                edges {
                    node {
                        name
                    }
                }
            }
            categories(first: 1) {
                edges {
                    node {
                        name
                    }
                }
            }
            products(first: 1) {
                edges {
                    node {
                        name
                    }
                }
            }
            variants(first: 1) {
                edges {
                    node {
                        name
                    }
                }
            }
            channelListings {
                discountValue
                channel {
                    slug
                }
                currency
            }
        }
    }
"""


def test_staff_query_sale(
    staff_api_client,
    promotion_converted_from_sale,
    permission_manage_discounts,
    product,
    variant,
    collection,
    category,
):
    # given
    promotion = promotion_converted_from_sale
    rule = promotion.rules.first()
    channel = rule.channels.first()
    variables = {"id": graphene.Node.to_global_id("Sale", promotion.old_sale_id)}

    # when
    response = staff_api_client.post_graphql(
        QUERY_SALE_BY_ID, variables, permissions=[permission_manage_discounts]
    )

    # then
    content = get_graphql_content(response)
    sale_data = content["data"]["sale"]
    assert sale_data["name"] == promotion.name
    assert sale_data["type"] == rule.reward_value_type.upper()
    assert sale_data["discountValue"] == rule.reward_value
    assert sale_data["currency"] == channel.currency_code
    assert sale_data["products"]["edges"][0]["node"]["name"] == product.name
    assert sale_data["variants"]["edges"][0]["node"]["name"] == variant.name
    assert sale_data["collections"]["edges"][0]["node"]["name"] == collection.name
    assert sale_data["categories"]["edges"][0]["node"]["name"] == category.name
    assert sale_data["channelListings"][0]["discountValue"] == rule.reward_value
    assert sale_data["channelListings"][0]["channel"]["slug"] == channel.slug
    assert sale_data["channelListings"][0]["currency"] == channel.currency_code


def test_query_sale_by_app(
    app_api_client, promotion_converted_from_sale, permission_manage_discounts
):
    # given
    promotion = promotion_converted_from_sale
    variables = {"id": graphene.Node.to_global_id("Sale", promotion.old_sale_id)}

    # when
    response = app_api_client.post_graphql(
        QUERY_SALE_BY_ID, variables, permissions=[permission_manage_discounts]
    )

    # then
    content = get_graphql_content(response)
    assert content["data"]["sale"]["name"] == promotion.name
    assert (
        content["data"]["sale"]["type"]
        == promotion.rules.first().reward_value_type.upper()
    )


def test_query_sale_by_customer(api_client, promotion_converted_from_sale):
    # given
    promotion = promotion_converted_from_sale
    variables = {"id": graphene.Node.to_global_id("Sale", promotion.old_sale_id)}
    # when
    response = api_client.post_graphql(QUERY_SALE_BY_ID, variables)
    # then
    assert_no_permission(response)


def test_staff_query_sale_by_invalid_id(
    staff_api_client, sale, permission_manage_discounts
):
    # given
    id = "bh/"
    variables = {"id": id}

    # when
    response = staff_api_client.post_graphql(
        QUERY_SALE_BY_ID, variables, permissions=[permission_manage_discounts]
    )

    # then
    content = get_graphql_content_from_response(response)
    assert len(content["errors"]) == 1
    assert content["errors"][0]["message"] == f"Couldn't resolve id: {id}."
    assert content["data"]["sale"] is None


def test_staff_query_sale_with_invalid_object_type(
    staff_api_client, promotion_converted_from_sale, permission_manage_discounts
):
    # given
    promotion = promotion_converted_from_sale
    variables = {"id": graphene.Node.to_global_id("Order", promotion.old_sale_id)}

    # when
    response = staff_api_client.post_graphql(
        QUERY_SALE_BY_ID, variables, permissions=[permission_manage_discounts]
    )

    # then
    content = get_graphql_content(response)
    assert content["data"]["sale"] is None
