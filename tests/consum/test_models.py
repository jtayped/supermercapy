from __future__ import annotations

import copy
import dataclasses
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import (
    ConfigurationError,
    InvalidResponseError,
    Photo,
    Promotion,
    Unit,
    UnitPrice,
)
from supermercapy.consum import (
    ActionType,
    ConsumOffer,
    ConsumPhoto,
    ConsumProduct,
    ImageSize,
    ProductFilters,
    ProductKind,
    PromotionType,
    TargetType,
)
from supermercapy.consum.models import (
    parse_group,
    parse_product,
    parse_search_result,
    parse_sort_option,
    parse_stores,
)
from tests.conftest import read_fixture

STORE = "consum"


def product_fixture(name: str = "product.json") -> Any:
    return read_fixture(STORE, name)


def listing_products(name: str) -> list[Any]:
    return read_fixture(STORE, name)["products"]


# -------------------------------------------------------------------- products


def test_a_product_maps_every_documented_field() -> None:
    product = parse_product(product_fixture())

    assert isinstance(product, ConsumProduct)
    assert product.id == "7080604"
    assert product.internal_id == "5932"
    assert product.name == "Leche Semidesnatada Brik"
    assert product.brand == "CONSUM"
    assert product.ean == "8414807514219"
    assert product.slug == "leche-semidesnatada-brik"
    assert product.url.endswith("/p/leche-semidesnatada-brik/7080604")
    assert product.description == "Leche Semidesnatada Brik 1 L"
    assert product.product_type == ProductKind.STANDARD
    assert product.is_new is False
    assert product.is_featured is False
    assert product.is_sponsored is False
    assert product.is_variable_weight is False
    assert product.contains_allergens is False
    assert product.allows_comments is False
    assert product.category_ids == ("2075",)
    assert product.categories[0].name == "Semidesnatada"
    assert product.categories[0].kind == 0
    assert product.category_path == product.categories
    assert product.offers == ()
    assert product.coupons == ()
    assert product.promotions == ()


def test_consum_publishes_no_nutrition_data() -> None:
    # the storefront gates the nutrition tab on an attribute no product has,
    # and the cdn bucket behind it is empty, so the field is always None.
    assert parse_product(product_fixture()).nutrition is None


def test_the_stable_id_is_the_code_not_the_internal_integer() -> None:
    product = parse_product(product_fixture())

    assert product.id == "7080604"
    assert product.internal_id == "5932"
    assert product.id != product.internal_id


def test_prices_are_euros_despite_the_cent_amount_key() -> None:
    product = parse_product(product_fixture())

    assert product.price.amount == Decimal("0.84")
    assert product.price.previous is None
    assert product.price.unit_price == Decimal("0.84")
    assert product.price.unit_price_unit == "1 L"
    assert product.price.tax_percentage == Decimal("4.0")
    assert product.price.currency == "EUR"
    assert product.price.is_discounted is False
    assert product.price.discount_percentage is None


def test_an_offer_price_becomes_the_current_amount() -> None:
    product = parse_product(listing_products("offers_immediate.json")[0])

    assert product.price.amount == Decimal("1.69")
    assert product.price.previous == Decimal("1.99")
    assert product.price.unit_price == Decimal("5.18")
    assert product.price.is_discounted is True
    assert product.price.discount_percentage == Decimal("15.08")


def test_a_deferred_offer_leaves_the_shelf_price_alone() -> None:
    product = parse_product(listing_products("offers_deferred.json")[0])

    assert product.price.amount == Decimal("4.3")
    assert product.price.previous is None
    assert product.price.is_discounted is False


def test_purchase_quantities_come_from_the_price_block() -> None:
    product = parse_product(product_fixture())

    assert product.availability.available is True
    assert product.availability.status == "1"
    assert product.availability.min_quantity == Decimal("1.0")
    assert product.availability.max_quantity == Decimal("60.0")
    assert product.availability.increment == Decimal("1.0")
    assert product.availability.temporarily_out_of_stock is False


@pytest.mark.parametrize(
    ("status", "out_of_stock", "expected"),
    [("1", False, True), ("1", True, False), ("0", False, False), (None, False, None)],
)
def test_availability_combines_the_status_and_the_stock_flag(
    status: str | None, out_of_stock: bool, expected: bool | None
) -> None:
    data = product_fixture()
    data["productData"]["availability"] = status
    data["productData"]["temporaryOutOfStock"] = out_of_stock
    if status is None:
        del data["productData"]["availability"]

    assert parse_product(data).availability.available is expected


def test_weight_products_are_flagged_variable_and_approximate() -> None:
    weight = next(
        item
        for item in listing_products("category_listing.json")
        if item["code"] == "1826"
    )
    product = parse_product(weight)

    assert product.product_type == ProductKind.WEIGHT
    assert product.is_variable_weight is True
    assert product.price.is_approximate is True
    assert product.availability.increment == Decimal("0.15")
    assert product.attribute("filter.id.weight") == ("true",)


@pytest.mark.parametrize(
    ("product_type", "variable"),
    [(1, False), (2, True), (3, True), (4, False), (None, False)],
)
def test_only_weight_kinds_are_variable(
    product_type: int | None, variable: bool
) -> None:
    data = {**product_fixture(), "productType": product_type}

    assert parse_product(data).is_variable_weight is variable


def test_the_pack_format_is_exposed_only_when_the_storefront_sets_it() -> None:
    assert parse_product(product_fixture()).pack_format is None

    data = product_fixture()
    data["productData"]["format"] = "Bandeja 500 g aprox."
    product = parse_product(data)

    assert product.pack_format == "Bandeja 500 g aprox."
    assert product.pack_size_text == "Bandeja 500 g aprox."


def test_photos_come_from_media_and_never_from_the_broken_image_url() -> None:
    product = parse_product(product_fixture())
    image_url = product_fixture()["productData"]["imageURL"]

    # imageURL points at {CODE}.jpg, which 404s for every product
    assert image_url.endswith("/7080604.jpg?t=20260812080202")
    assert len(product.photos) == 3
    assert all(isinstance(photo, ConsumPhoto) for photo in product.photos)
    assert all("_00" in photo.url for photo in product.photos)
    assert product.thumbnail is product.photos[0]
    assert product.photos[0].order == 1
    assert product.photos[0].kind == "P"


def test_a_product_without_media_has_no_thumbnail() -> None:
    data = product_fixture()
    data["media"] = [{"order": 1}, "not an object"]

    product = parse_product(data)

    assert product.photos == ()
    assert product.thumbnail is None


def test_an_empty_brand_object_becomes_none() -> None:
    weight = next(
        item
        for item in listing_products("category_listing.json")
        if item["code"] == "1826"
    )

    assert parse_product(weight).brand is None


def test_a_plain_string_brand_is_accepted() -> None:
    data = product_fixture()
    data["productData"]["brand"] = "  CONSUM  "

    assert parse_product(data).brand == "CONSUM"


def test_attributes_flatten_every_language() -> None:
    data = product_fixture()
    data["productData"]["attributes"] = [
        {"code": "a", "languages": [{"values": ["1"]}, {"values": ["2", 3]}]},
        {"languages": []},
        "not an object",
    ]

    product = parse_product(data)

    assert tuple(item.code for item in product.attributes) == ("a",)
    assert product.attribute("a") == ("1", "2")
    assert product.attribute("missing") == ()


@pytest.mark.parametrize("field", ["code", "productData"])
def test_a_product_without_an_identity_is_rejected(field: str) -> None:
    data = product_fixture()
    data[field] = None

    with pytest.raises(InvalidResponseError):
        parse_product(data)


def test_unknown_fields_are_ignored() -> None:
    data = product_fixture()
    data["somethingNew"] = {"nested": True}
    data["productData"]["alsoNew"] = 7

    assert parse_product(data).id == "7080604"


def test_products_are_frozen_and_slotted() -> None:
    product = parse_product(product_fixture())

    assert dataclasses.is_dataclass(product)
    assert not hasattr(product, "__dict__")
    with pytest.raises(FrozenInstanceError):
        product.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------- offers


def test_an_immediate_offer_maps_to_a_promotion() -> None:
    product = parse_product(listing_products("offers_immediate.json")[0])
    offer = product.offers[0]

    assert isinstance(offer, ConsumOffer)
    assert isinstance(offer, Promotion)
    assert product.promotions == product.offers
    assert offer.id == "12915541"
    assert offer.promotion_id == "430877092"
    assert offer.kind == "immediate"
    assert offer.is_immediate is True
    assert offer.amount == Decimal("1.69")
    assert offer.price == Decimal("1.69")
    assert offer.description == "Ahora más barato"
    assert offer.short_description == "Ahora más barato"
    assert offer.min_description == "Ahora "
    assert offer.image_url.endswith("oferta-inmediato.png")
    assert offer.picto_type == "default"
    assert offer.promotion_type == PromotionType.OFFER_PRICE
    assert offer.target_type == TargetType.PRODUCT
    assert offer.action_type == ActionType.NET_PRICE
    assert offer.member_only is False


def test_a_deferred_offer_credits_money_back_instead_of_cutting_the_price() -> None:
    offer = parse_product(listing_products("offers_deferred.json")[0]).offers[0]

    assert offer.kind == "deferred"
    assert offer.is_immediate is False
    assert offer.amount == Decimal("0.7")
    assert offer.price is None
    assert offer.promotion_type == PromotionType.DEFERRED
    assert offer.action_type == ActionType.NET_DISCOUNT


def test_the_offer_window_comes_from_the_offer_not_the_sentinel_price_date() -> None:
    row = listing_products("offers_immediate.json")[0]
    offer = parse_product(row).offers[0]

    # every price carries a 2099 sentinel, including the discounted one
    assert all(
        price["toDate"].startswith("2099") for price in row["priceData"]["prices"]
    )
    assert offer.starts_at is not None
    assert offer.ends_at is not None
    assert offer.starts_at.year == 2026
    assert offer.ends_at.year == 2026
    assert offer.starts_at < offer.ends_at


def test_the_misspelt_immediate_flag_is_read_as_sent() -> None:
    row = listing_products("offers_immediate.json")[0]

    assert "inmediate" in row["offers"][0]
    assert "immediate" not in row["offers"][0]
    assert parse_product(row).offers[0].is_immediate is True

    row["offers"][0]["inmediate"] = False
    assert parse_product(row).offers[0].is_immediate is False


def test_an_unfamiliar_campaign_keeps_its_raw_type_codes() -> None:
    row = listing_products("offers_immediate.json")[0]
    row["offers"][0].update(
        {"promotionType": 99, "applicationTargetType": 98, "applicationActionType": 97}
    )

    offer = parse_product(row).offers[0]

    assert offer.promotion_type == 99
    assert offer.target_type == 98
    assert offer.action_type == 97


def test_coupons_parse_like_offers_but_are_empty_without_a_session() -> None:
    data = product_fixture()
    data["coupons"] = listing_products("offers_immediate.json")[0]["offers"]

    product = parse_product(data)

    assert product.coupons[0].id == "12915541"
    assert parse_product(product_fixture()).coupons == ()


# ---------------------------------------------------------------------- photos


def test_a_photo_can_be_resized_between_the_three_real_renditions() -> None:
    photo = parse_product(product_fixture()).photos[0]

    assert isinstance(photo, Photo)
    assert "/img/135x135/7080604_001.jpg" in photo.sized(ImageSize.LOW)
    assert "/img/300x300/" in photo.sized("300x300")
    assert photo.sized(ImageSize.HIGH).startswith("https://cdn-consum.")
    assert "?t=" in photo.sized(ImageSize.HIGH)


@pytest.mark.parametrize("size", ["700x700", "original", "", "thumb", 300])
def test_unsupported_renditions_are_rejected(size: Any) -> None:
    photo = parse_product(product_fixture()).photos[0]

    with pytest.raises(ValueError, match="size must be one of"):
        photo.sized(size)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.test/img/photo.jpg",
        "https://cdn.test/photo.jpg",
        "photo.jpg",
    ],
)
def test_a_url_without_a_rendition_segment_is_returned_unchanged(url: str) -> None:
    photo = ConsumPhoto(url=url)

    assert photo.sized(ImageSize.HIGH) == url


def test_a_photo_needs_a_url() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ConsumPhoto(url="   ")


# ---------------------------------------------------------------- filter builder


def test_the_filter_builder_packs_groups_the_way_the_storefront_expects() -> None:
    filters = ProductFilters(
        brands=("CONSUM", "ARLA"),
        leaf_categories=("2055",),
        offer_immediate=True,
        eco=True,
    )

    assert filters.render() == (
        "filter.brand:CONSUM,ARLA;filter.categoryLeaf:2055;"
        "filter.offerImmediate:true;filter.eco:true"
    )
    assert bool(filters) is True


def test_every_boolean_facet_has_a_filter_id() -> None:
    filters = ProductFilters(
        offer_immediate=True,
        offer_deferred=True,
        eco=True,
        own_brand=True,
        best_score=True,
        novelty=True,
        categories=("2811",),
    )

    assert filters.render() == (
        "filter.category:2811;filter.offerImmediate:true;filter.offerDeferred:true;"
        "filter.eco:true;filter.ownBrand:true;filter.bestScore:true;"
        "filter.novelty:true"
    )


def test_an_empty_filter_set_renders_to_nothing() -> None:
    assert ProductFilters().render() is None
    assert bool(ProductFilters()) is False


def test_repeated_filter_values_are_collapsed() -> None:
    assert ProductFilters(brands=("CONSUM", "CONSUM", 12)).render() == (
        "filter.brand:CONSUM,12"
    )


@pytest.mark.parametrize("value", ["", "   ", "A,B", "A;B", "A:B", None, True, 1.5])
def test_filter_values_that_would_corrupt_the_packing_are_rejected(value: Any) -> None:
    with pytest.raises(ConfigurationError, match=r"filter\.brand"):
        ProductFilters(brands=(value,)).render()


# ---------------------------------------------------------------------- stores


def test_stores_are_flattened_out_of_their_region_groups() -> None:
    stores = parse_stores(read_fixture(STORE, "shipping_area.json"))

    assert len(stores) == 1
    assert stores[0].zone_id == 147
    assert stores[0].latitude is None


def test_a_pickup_area_carries_its_pickup_point() -> None:
    data = read_fixture(STORE, "shipping_area.json")
    area = data[0]["shippingAreas"][0]
    area.update(
        {
            "shippingZoneId": "582T",
            "deliveryTypeId": "T",
            "pickupPoint": {
                "name": "Consum Ruzafa",
                "latitude": "39.4611",
                "longitude": -0.3752,
            },
        }
    )

    store = parse_stores(data)[0]

    assert store.kind == "pickup"
    assert store.delivery_method == "T"
    assert store.pickup_point == "Consum Ruzafa"
    assert store.latitude == pytest.approx(39.4611)
    assert store.longitude == pytest.approx(-0.3752)


def test_areas_without_a_zone_id_are_skipped() -> None:
    data = read_fixture(STORE, "shipping_area.json")
    data[0]["shippingAreas"].append({"shippingZoneId": "999D", "zone": {}})

    assert len(parse_stores(data)) == 1


def test_a_store_falls_back_to_its_group_name_for_the_province() -> None:
    data = read_fixture(STORE, "shipping_area.json")
    data[0]["shippingAreas"][0]["zone"]["address"]["region"] = ""
    data[0]["shippingAreas"][0]["zone"]["name"] = ""
    data[0]["shippingAreas"][0]["enabled"] = False

    store = parse_stores(data)[0]

    assert store.province == "VALENCIA"
    assert store.name == "147"
    assert store.is_enabled is False


# ------------------------------------------------------------------- campaigns


def test_a_campaign_reads_its_window_and_attribute_flags() -> None:
    groups = tuple(parse_group(item) for item in read_fixture(STORE, "groups.json"))

    assert groups[0].code == "OCSEPTIEMBRE26"
    assert groups[0].is_hidden is False
    assert groups[0].is_recipe is False
    assert groups[1].subgroups[0].code == "UNEUROSEPT26"
    assert groups[1].subgroups[0].subgroups == ()


def test_campaign_flags_follow_their_attribute_values() -> None:
    data = read_fixture(STORE, "groups.json")[0]
    data["attributes"] = [
        {"groupAttributeTypeId": "hidden", "values": ["1"]},
        {"groupAttributeTypeId": "recipe", "values": ["true"]},
        {"values": ["ignored"]},
    ]

    group = parse_group(data)

    assert group.is_hidden is True
    assert group.is_recipe is True


# ----------------------------------------------------------------- sort orders


def test_a_sort_order_without_an_id_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="sort order id"):
        parse_sort_option({"label": "order.price.asc"})


def test_a_sort_order_without_a_label_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="sort order label"):
        parse_sort_option({"id": 1})


# --------------------------------------------------------------- search result


def test_a_page_of_only_sponsored_rows_still_advances_the_cursor() -> None:
    data = read_fixture(STORE, "category_listing.json")
    for row in data["products"]:
        row["productData"]["sponsored"] = True

    result = parse_search_result(
        data, query="", offset=10, page_size=6, drop_sponsored=True
    )

    assert result.products == ()
    assert result.dropped_sponsored == 6
    assert result.next_cursor == "16"


def test_a_page_with_no_rows_at_all_ends_the_walk() -> None:
    data = {"products": [], "hasMore": True, "totalCount": 0}

    result = parse_search_result(
        data, query="", offset=10, page_size=6, drop_sponsored=True
    )

    assert result.next_cursor is None
    assert result.has_more is False


def test_price_entries_without_an_id_are_skipped() -> None:
    data = product_fixture()
    data["priceData"]["prices"] = [
        {"value": {"centAmount": 9.99}},
        {"id": "PRICE", "value": {"centAmount": 0.84}},
        {"id": "PRICE", "value": {"centAmount": 1.11}},
    ]

    price = parse_product(data).price

    assert price.amount == Decimal("0.84")


def test_facet_groups_and_filters_without_an_id_are_skipped() -> None:
    data = read_fixture(STORE, "listing.json")
    data["filters"] = [
        {"filters": [{"id": "filter.brand", "values": []}]},
        {"groupName": "group.other", "filters": [{"values": []}, "not an object"]},
    ]

    result = parse_search_result(
        data, query="", offset=0, page_size=2, drop_sponsored=True
    )

    assert tuple(group.name for group in result.filter_groups) == ("group.other",)
    assert result.filter_groups[0].filters == ()


def test_the_reference_price_follows_the_offer() -> None:
    shelf = parse_product(product_fixture())
    offer = parse_product(listing_products("offers_immediate.json")[0])

    assert shelf.price.reference == UnitPrice(amount=Decimal("0.84"), unit=Unit.LITRE)
    assert offer.price.unit_price_unit == "1 Kg"
    assert offer.price.reference == UnitPrice(
        amount=Decimal("5.18"), unit=Unit.KILOGRAM
    )


@pytest.mark.parametrize(
    ("cent_unit_amount", "unit_type", "expected"),
    [
        # every unitPriceUnitType seen on october 2026 searches
        (0.84, "1 L", UnitPrice(amount=Decimal("0.84"), unit=Unit.LITRE)),
        (2.79, "1 Kg", UnitPrice(amount=Decimal("2.79"), unit=Unit.KILOGRAM)),
        # a 25 g snack at 1,55 € and a 50 ml deodorant at 2,30 €
        (6.2, "100 Gr", UnitPrice(amount=Decimal("62.00"), unit=Unit.KILOGRAM)),
        (4.6, "100 ml", UnitPrice(amount=Decimal("46.00"), unit=Unit.LITRE)),
        # half a dozen eggs at 3,69 €
        (7.38, "1 Dc", UnitPrice(amount=Decimal("0.615"), unit=Unit.PIECE)),
        (0.18, "1 Lv", UnitPrice(amount=Decimal("0.18"), unit=Unit.DOSE)),
        (0.19, "1 Do", UnitPrice(amount=Decimal("0.19"), unit=Unit.DOSE)),
        (0.21, "1 U", UnitPrice(amount=Decimal("0.21"), unit=Unit.PIECE)),
        (0.07, "1 M", UnitPrice(amount=Decimal("0.07"), unit=Unit.METRE)),
        (1.0, "1 Hj", None),
        (1.0, "", None),
        (None, "1 Kg", None),
    ],
)
def test_every_unit_price_unit_type_is_normalised(
    cent_unit_amount: float | None, unit_type: str, expected: UnitPrice | None
) -> None:
    data = copy.deepcopy(product_fixture())
    data["priceData"]["unitPriceUnitType"] = unit_type
    data["priceData"]["prices"][0]["value"]["centUnitAmount"] = cent_unit_amount

    assert parse_product(data).price.reference == expected
