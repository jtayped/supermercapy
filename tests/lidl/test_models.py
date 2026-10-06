from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import InvalidResponseError, Photo, Unit, UnitPrice
from supermercapy.lidl import (
    ImageSize,
    LidlPhoto,
    LidlPrice,
    LidlProduct,
    ProductFamily,
    family_of,
    parent_id_of,
)
from supermercapy.lidl.models import (
    LidlLeaflet,
    parse_categories,
    parse_category,
    parse_leaflet,
    parse_leaflets,
    parse_price,
    parse_product,
    parse_search_result,
    parse_sitemap_ids,
    parse_stores,
    store_total,
)
from tests.conftest import read_fixture

STORE = "lidl"


def grocery() -> Any:
    return read_fixture(STORE, "product_grocery.json")


def shop() -> Any:
    return read_fixture(STORE, "product_shop.json")


# ------------------------------------------------------------------------- ids


@pytest.mark.parametrize(
    ("given", "parent"),
    [
        ("11002343", "11002343"),
        ("11002343005", "11002343"),
        ("100408615", "100408615"),
        ("100408615001", "100408615"),
        ("not-a-number", "not-a-number"),
    ],
)
def test_a_variant_id_is_its_parent_plus_three_digits(given: str, parent: str) -> None:
    assert parent_id_of(given) == parent


@pytest.mark.parametrize(
    ("product_id", "category", "family"),
    [
        ("11002343", None, ProductFamily.GROCERY),
        ("100408615", None, ProductFamily.SHOP),
        ("85000001", "F+V", ProductFamily.GROCERY),
        ("85000001", "Categorías/Moda", ProductFamily.SHOP),
    ],
)
def test_the_family_follows_the_id_prefix_or_the_flat_category(
    product_id: str, category: str | None, family: ProductFamily
) -> None:
    assert family_of(product_id, category=category) is family


def test_sitemap_ids_keep_product_urls_only_and_deduplicate() -> None:
    xml = read_fixture(STORE, "product_sitemap.xml")

    assert parse_sitemap_ids(xml + xml) == (
        "100214229",
        "100399898",
        "11150856",
        "11137251",
    )
    assert parse_sitemap_ids("<loc>https://www.lidl.es/c/a/s1</loc>") == ()
    assert parse_sitemap_ids("") == ()


# ----------------------------------------------------------------------- photo


def test_a_photo_returns_the_size_the_payload_holds() -> None:
    product = parse_product(grocery(), region="26")
    photo = product.photos[0]

    assert isinstance(photo, Photo)
    assert photo.sized(ImageSize.THUMB) == photo.thumb_url
    assert photo.sized("large") == photo.large_url
    assert photo.alt


def test_a_listing_photo_has_one_size_and_falls_back_to_it() -> None:
    photo = LidlPhoto(url="https://www.lidl.es/assets/one.jpg")

    assert photo.sized(ImageSize.THUMB) == "https://www.lidl.es/assets/one.jpg"
    with pytest.raises(ValueError, match="size must be one of"):
        photo.sized("huge")


def test_a_listing_row_uses_its_single_image_when_it_lists_none() -> None:
    product = parse_product(
        {
            "erpNumber": "11000001",
            "title": "one",
            "image": "https://www.lidl.es/assets/one.jpg",
        }
    )
    assert [photo.url for photo in product.photos] == [
        "https://www.lidl.es/assets/one.jpg"
    ]

    without = parse_product({"erpNumber": "11000002", "title": "two"})
    assert without.photos == ()
    assert without.thumbnail is None


def test_an_image_entry_without_a_url_is_skipped() -> None:
    product = parse_product(
        {
            "erpNumber": "11000003",
            "title": "three",
            "media": {
                "gallery": {"images": [1, 2]},
                "imageMap": {
                    "1": {"accessibility": "no url"},
                    "2": {"mediumUrl": "https://www.lidl.es/assets/medium.jpg"},
                },
            },
        }
    )
    assert [photo.url for photo in product.photos] == [
        "https://www.lidl.es/assets/medium.jpg"
    ]


# ----------------------------------------------------------------------- price


def test_a_price_object_without_an_amount_is_not_a_price() -> None:
    assert parse_price({"currencyCode": "EUR"}) is None
    assert parse_price(None) is None
    assert parse_price({"price": 1.25}) is not None


def test_a_discount_is_read_from_either_spelling() -> None:
    price = parse_price(
        {"price": 1.25, "discount": {"deletedPrice": 1.49, "percentageDiscount": 16}}
    )
    assert price is not None
    assert price.previous == Decimal("1.49")
    assert price.is_discounted is True
    assert price.discount_percentage == Decimal("16")


@pytest.mark.parametrize(
    ("base_price", "expected"),
    [
        # every basePrice shape seen on october 2026 searches: structured on
        # non-food, a sentence on food, and nothing at all on most products
        (
            {"amount": 7.8, "price": 0.64, "text": "1 m = 0.64", "unit": "m"},
            UnitPrice(amount=Decimal("0.64"), unit=Unit.METRE),
        ),
        (
            {"amount": 0.6, "price": 8.32, "text": "1 m² = 8.32", "unit": "m²"},
            UnitPrice(amount=Decimal("8.32"), unit=Unit.SQUARE_METRE),
        ),
        (
            {"prefix": False, "text": "1,55 €/kg"},
            UnitPrice(amount=Decimal("1.55"), unit=Unit.KILOGRAM),
        ),
        ({"prefix": False, "text": "1 ud 6,77 €/kg / 2 uds 6,54 €/kg"}, None),
        ({"prefix": False}, None),
        ({"amount": 3.0, "text": "1 m = 3.33", "unit": "m"}, None),
        ({"price": 1.0, "unit": "hoja"}, None),
        (None, None),
    ],
)
def test_the_base_price_is_normalised_when_it_states_one_unit_price(
    base_price: object, expected: UnitPrice | None
) -> None:
    price = parse_price({"price": 4.99, "basePrice": base_price})
    assert price is not None
    assert price.reference == expected
    structured = isinstance(base_price, dict) and "unit" in base_price
    assert (price.unit_price_unit is not None) == structured
    if structured:
        assert isinstance(base_price, dict)
        published = base_price.get("price")
        assert price.unit_price == (
            None if published is None else Decimal(str(published))
        )
    else:
        assert price.unit_price is None


def test_a_highlighted_member_price_keeps_its_reference_price() -> None:
    product = parse_product(
        {
            "erpNumber": "11000006",
            "title": "six",
            "regionsV2": {"26": {"regionName": "Barcelona", "regionPriceId": "1"}},
            "regionsPrices": {
                "1": {
                    "currentLidlPlusPrice": {
                        "highlightText": "-26%",
                        "price": {
                            "price": 3.87,
                            "basePrice": {"prefix": False, "text": "1,55 €/kg"},
                        },
                    }
                }
            },
        },
        region="26",
    )
    assert product.price.is_member_price is True
    assert product.price.highlight_text == "-26%"
    assert product.price.reference == UnitPrice(
        amount=Decimal("1.55"), unit=Unit.KILOGRAM
    )


def test_a_bare_price_falls_back_only_when_no_region_is_published() -> None:
    # a grocery row outside every campaign carries no regionsPrices at all,
    # which is normal rather than an error
    product = parse_product(
        {
            "erpNumber": "11000004",
            "title": "four",
            "price": {"price": 2.5, "packaging": {"text": "500 ml"}},
        }
    )
    assert product.price.amount == Decimal("2.5")
    assert product.pack_size_text == "500 ml"

    unpriced = parse_product(
        {
            "erpNumber": "11000005",
            "title": "five",
            "price": {"price": 2.5},
            "regionsV2": {"26": {"regionName": "Barcelona", "regionPriceId": "1"}},
            "regionsPrices": {},
        },
        region="26",
    )
    assert unpriced.price.amount is None
    assert unpriced.price_band_id is None


def test_the_default_region_names_the_band_when_nothing_is_bound() -> None:
    product = parse_product(grocery())

    assert product.price_band_id == "1"
    assert product.price.amount == Decimal("1.99")


def test_a_lone_band_is_used_when_no_region_is_marked_default() -> None:
    product = parse_product(
        {
            "erpNumber": "11000006",
            "title": "six",
            "regionsV2": {"1": {"regionName": "A Coruña", "regionPriceId": "1"}},
            "regionsPrices": {"1": {"currentPrice": {"price": 0.99}}},
        }
    )
    assert product.price.amount == Decimal("0.99")


def test_an_unlisted_region_reports_no_price() -> None:
    product = parse_product(grocery(), region="99")

    assert product.region("99") is None
    assert product.price.amount is None


def test_bands_and_regions_are_addressable() -> None:
    product = parse_product(grocery(), region="26")

    assert product.band("1") is not None
    assert product.band("9") is None
    assert product.region("26") is not None
    assert product.zone("PEN") is None


def test_a_shop_product_is_addressable_by_zone() -> None:
    product = parse_product(shop(), zone="CAN")

    assert product.family is ProductFamily.SHOP
    assert product.zone("CAN") is not None
    assert product.zone("XXX") is None
    assert product.price.amount == product.zone("CAN").price.amount  # type: ignore[union-attr]
    assert product.ean == "4052916891476"
    assert product.rating == Decimal("4.5")
    assert product.rating_count == 112


def test_an_unknown_zone_falls_back_to_the_bare_price() -> None:
    product = parse_product(shop(), zone="XXX")

    assert product.price.amount == Decimal("49.99")


def test_a_discounted_band_becomes_a_promotion() -> None:
    product = parse_product(
        {
            "erpNumber": "11000007",
            "title": "seven",
            "regionsV2": {"1": {"regionPriceId": "1", "isDefault": True}},
            "regionsPrices": {
                "1": {
                    "currentPrice": {
                        "price": 1.25,
                        "oldPrice": 1.49,
                        "discount": {
                            "percentageDiscount": 16,
                            "bargainHintText": "Megadescuento",
                        },
                    }
                }
            },
        }
    )
    assert [promotion.kind for promotion in product.promotions] == ["discount"]
    assert product.promotions[0].description == "Megadescuento"
    assert product.promotions[0].member_only is False


def test_member_prices_are_listed_even_without_a_band() -> None:
    product = parse_product(grocery(), region="49")

    assert product.lidl_plus and product.lidl_plus[0].is_member_price is True
    assert product.member_price is None


def test_a_member_entry_without_a_price_is_dropped() -> None:
    product = parse_product(
        {"erpNumber": "11000008", "title": "eight", "lidlPlus": [{"price": {}}, {}]}
    )
    assert product.lidl_plus == ()


# --------------------------------------------------------------------- product


def test_a_product_without_a_title_is_refused() -> None:
    with pytest.raises(InvalidResponseError, match="title"):
        parse_product({"erpNumber": "11000009"})
    with pytest.raises(InvalidResponseError, match="id"):
        parse_product({"title": "no id"})


def test_the_worlds_of_need_path_becomes_the_category_path() -> None:
    product = parse_product(grocery())

    assert [category.id for category in product.category_path] == [
        "0",
        "17",
        "1710",
        "171010",
    ]
    assert product.category_ids == ("0", "17", "1710", "171010")
    assert product.category_path[1].parent_id == "0"


def test_a_mismatched_category_path_is_dropped() -> None:
    product = parse_product(
        {
            "erpNumber": "11000010",
            "title": "ten",
            "keyfacts": {"wonCategoryPrimary": "a/b", "wonCategoryPrimaryPath": "0"},
        }
    )
    assert product.category_path == ()


def test_a_product_is_frozen_and_slotted() -> None:
    product = parse_product(grocery())

    assert isinstance(product, LidlProduct)
    assert not hasattr(product, "__dict__")
    with pytest.raises(FrozenInstanceError):
        product.name = "changed"  # type: ignore[misc]


def test_an_identifier_field_may_be_absent() -> None:
    product = parse_product({"productId": 11000011, "title": "eleven"})

    assert product.id == "11000011"
    assert product.item_id is None
    assert product.variant_id is None
    assert product.slug is None
    assert product.url is None


# ---------------------------------------------------------------------- search


def test_a_search_page_counts_the_rows_it_received() -> None:
    page = parse_search_result(
        read_fixture(STORE, "search.json"),
        query="pan",
        offset=0,
        page_size=2,
        family=ProductFamily.GROCERY,
    )
    assert page.next_cursor == "2"
    assert [product.id for product in page.products] == ["11137251"]


def test_an_empty_or_unknown_envelope_ends_the_walk() -> None:
    page = parse_search_result({"items": []}, query="pan", offset=0, page_size=48)

    assert page.products == ()
    assert page.next_cursor is None
    assert page.total_hits is None


# ------------------------------------------------------------------ categories


def test_a_response_without_a_category_facet_has_no_categories() -> None:
    assert parse_categories({"facets": [{"code": "brand", "values": []}]}) == ()
    assert parse_categories({}) == ()
    assert parse_category({}, category_id="1") is None


def test_the_category_facet_nests_its_children() -> None:
    categories = parse_categories(read_fixture(STORE, "category.json"))

    assert categories[0].children[0].parent_id == categories[0].id


# ---------------------------------------------------------------------- stores


def test_a_store_row_without_an_object_number_is_skipped() -> None:
    stores = parse_stores({"items": [{"storeName": "nowhere"}, {}]})

    assert stores == ()


def test_store_coordinates_and_totals_are_typed() -> None:
    page = read_fixture(STORE, "stores_page1.json")
    stores = parse_stores(page)

    assert store_total(page) == 3
    assert store_total({}) is None
    assert isinstance(stores[0].latitude, float)
    assert stores[0].address
    assert stores[0].province


# -------------------------------------------------------------------- leaflets


def test_a_leaflet_without_dates_is_never_live() -> None:
    leaflet = LidlLeaflet(id="x", name="x")

    assert leaflet.is_live_on(date(2026, 9, 15)) is False


def test_store_targeting_is_kept_apart_from_offer_regions() -> None:
    leaflet = parse_leaflet(
        {
            "flyer": {
                "id": "1",
                "name": "one",
                "regions": [
                    {"type": "offer_region", "code": "23"},
                    {"type": "store", "code": "ES00319"},
                ],
            }
        }
    )
    assert leaflet.regions == ("23",)
    assert leaflet.store_codes == ("ES00319",)


def test_leaflet_pages_and_products_skip_rows_without_an_id() -> None:
    leaflet = parse_leaflet(
        {
            "flyer": {
                "id": "1",
                "name": "one",
                "pages": [{"number": 1}, {"id": "p1", "number": 1}],
                "products": {"a": {"title": "no id"}, "b": {"productId": "100408856"}},
            }
        }
    )
    assert [page.id for page in leaflet.pages] == ["p1"]
    assert [product.id for product in leaflet.products] == ["100408856"]


def test_leaflet_products_may_arrive_as_an_array() -> None:
    leaflet = parse_leaflet(
        {"flyer": {"id": "1", "name": "one", "products": [{"productId": "1"}]}}
    )
    assert [product.id for product in leaflet.products] == ["1"]


def test_an_overview_without_flyers_is_empty() -> None:
    assert parse_leaflets({"categories": [{"name": "Folletos"}]}) == ()
    assert parse_leaflets({}) == ()


def test_the_overview_dates_are_parsed_and_bad_ones_ignored() -> None:
    leaflets = parse_leaflets(read_fixture(STORE, "leaflets.json"))

    assert leaflets[0].starts_on == date(2026, 9, 7)
    assert leaflets[0].file_size
    broken = parse_leaflet(
        {"flyer": {"id": "1", "name": "one", "startDate": "7/9/2026", "endDate": 5}}
    )
    assert broken.starts_on is None
    assert broken.ends_on is None


# ----------------------------------------------------------------------- price


def test_prices_keep_their_own_validity_window() -> None:
    product = parse_product(grocery(), region="26")
    price = product.price

    assert isinstance(price, LidlPrice)
    assert price.valid_from is not None
    assert price.valid_until is not None
    assert price.ends_at_exclusive is not None
    assert price.ends_at_exclusive > price.valid_until


def test_blank_or_broken_rows_are_ignored_field_by_field() -> None:
    product = parse_product(
        {
            "erpNumber": "11000012",
            "title": "twelve",
            "eans": ["", "20091934"],
            "imageList_V1": [{"accessibility": "no image"}, {"image": "u"}],
            "regionsV2": {"1": {"regionPriceId": "1", "isDefault": True}},
            "regionsPrices": {
                "1": {
                    "currentPrice": {"price": 1.0},
                    "futurePrices": [{"price": {}}, {"price": {"price": 0.9}}],
                }
            },
        }
    )
    assert product.ean == "20091934"
    assert [photo.url for photo in product.photos] == ["u"]
    assert [price.amount for price in product.future_prices] == [Decimal("0.9")]
