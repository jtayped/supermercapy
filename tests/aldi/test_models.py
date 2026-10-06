from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import InvalidResponseError, Product, Unit, UnitPrice
from supermercapy.aldi import AldiPrice, AldiProduct, Region
from supermercapy.aldi.models import (
    api_data,
    next_data,
    page_props,
    parse_category_node,
    parse_category_page,
    parse_children,
    parse_hits,
    parse_offer_groups,
    parse_price,
    parse_product,
)
from tests.conftest import read_fixture

STORE = "aldi"


def record(name: str = "product.json") -> Any:
    return read_fixture(STORE, name)


def page(name: str) -> Any:
    return next_data(read_fixture(STORE, name))


def html(document: object) -> str:
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(document)}</script>"
    )


# ------------------------------------------------------------------ records


def test_a_record_maps_every_documented_field() -> None:
    product = parse_product(record(), region=Region.PENINSULA)

    assert isinstance(product, AldiProduct)
    assert isinstance(product, Product)
    assert product.id == "995700"
    assert product.name == "Uva de mesa blanca sin semillas"
    assert product.brand is None
    assert product.slug == "uva-de-mesa-blanca-sin-semillas-995700"
    assert product.url == (
        "https://www.aldi.es/producto/uva-de-mesa-blanca-sin-semillas-995700.html"
    )
    assert product.pack_size_text == "500 g unidad"
    assert product.article_number == "9957"
    assert product.category_ids == ("frutas",)
    assert product.main_category_id == "frutas"
    assert product.description is not None
    assert product.description.startswith("Origen: España")
    assert product.long_description is not None
    assert "Islas Baleares" in product.long_description
    assert product.availability.available is True
    assert product.is_coming_soon is False
    assert product.is_recall is False
    assert product.region == "pen"
    assert product.ean is None
    assert product.nutrition is None


def test_photos_and_certificates_are_kept_apart() -> None:
    product = parse_product(record())

    assert [photo.kind for photo in product.photos] == ["primary", "gallery"]
    assert product.thumbnail == product.photos[0]
    assert product.photos[0].alt == (
        "Uva de mesa blanca sin semillas | ALDI Supermercados"
    )
    assert [photo.kind for photo in product.certificates] == ["certificate"]
    assert product.certificates[0].url.endswith("/01_A_Origen_nacional")


def test_the_current_price_keeps_its_window_and_shelf_label() -> None:
    price = parse_product(record()).price

    assert isinstance(price, AldiPrice)
    assert price.amount == Decimal("1.49")
    assert price.previous == Decimal("1.89")
    assert price.is_discounted is True
    assert price.discount_percentage == Decimal("21.16")
    assert price.promo_text == "-21%"
    assert price.unit_price == Decimal("2.98")
    assert price.unit_price_unit == "kg"
    assert price.reference == UnitPrice(amount=Decimal("2.98"), unit=Unit.KILOGRAM)
    # monday 5 october 2026 at midnight in madrid: the index already carries
    # the price a day before it applies
    assert price.valid_from == datetime(2026, 10, 4, 22, 0, tzinfo=UTC)
    assert price.valid_until == datetime(2026, 10, 11, 21, 59, 59, tzinfo=UTC)


def test_promotion_prices_become_dated_promotions() -> None:
    product = parse_product(record())

    (promotion,) = product.promotions
    assert promotion.kind == "promotion_price"
    assert promotion.description == "-21%"
    assert promotion.price == Decimal("1.49")
    assert promotion.starts_at == product.price.valid_from
    assert promotion.ends_at == product.price.valid_until
    assert product.promotion_prices[0] == product.price


def test_a_later_window_is_published_ahead() -> None:
    product = parse_product(record("product_two_windows.json"))

    first, later = product.promotion_prices
    assert first.valid_until is not None and later.valid_from is not None
    assert later.valid_from > first.valid_until
    assert later.valid_from == datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
    assert product.price.is_discounted is False
    assert product.price.previous is None
    assert [promotion.description for promotion in product.promotions] == [None, None]
    assert product.brand == "CUCINA®"


def test_loose_produce_without_a_price_has_an_empty_price() -> None:
    product = parse_product(record("product_loose.json"))

    assert product.price == AldiPrice()
    assert product.pack_size_text is None
    assert product.promotions == ()


def test_the_url_follows_the_region() -> None:
    product = parse_product(record(), region="bal")

    assert product.url == (
        "https://www.aldi.es/bal/producto/uva-de-mesa-blanca-sin-semillas-995700.html"
    )
    assert product.region == "bal"
    assert parse_product(record()).region is None


@pytest.mark.parametrize(
    ("scale", "expected"),
    [
        ("kg", UnitPrice(amount=Decimal("2"), unit=Unit.KILOGRAM)),
        ("l", UnitPrice(amount=Decimal("2"), unit=Unit.LITRE)),
        ("100-ml", UnitPrice(amount=Decimal("20"), unit=Unit.LITRE)),
        ("unidad", UnitPrice(amount=Decimal("2"), unit=Unit.PIECE)),
        ("lavado", UnitPrice(amount=Decimal("2"), unit=Unit.DOSE)),
        ("capsula", UnitPrice(amount=Decimal("2"), unit=Unit.PIECE)),
        ("bolsita", UnitPrice(amount=Decimal("2"), unit=Unit.PIECE)),
        ("toallita", UnitPrice(amount=Decimal("2"), unit=Unit.PIECE)),
        ("panuelo", UnitPrice(amount=Decimal("2"), unit=Unit.PIECE)),
        ("m-(metro)", UnitPrice(amount=Decimal("2"), unit=Unit.METRE)),
        # sent on prices that are per kilogram
        ("g", UnitPrice(amount=Decimal("2"), unit=Unit.KILOGRAM)),
        ("100-g", UnitPrice(amount=Decimal("2"), unit=Unit.KILOGRAM)),
        ("par", None),
        ("caja", None),
    ],
)
def test_every_base_price_scale_seen_is_read(
    scale: str, expected: UnitPrice | None
) -> None:
    price = parse_price(
        {
            "priceValue": 4,
            "basePrice": [{"basePriceValue": 2, "basePriceScale": scale}],
        }
    )

    assert price.unit_price_unit == scale
    assert price.reference == expected


def test_a_base_price_without_a_scale_reads_as_unknown() -> None:
    price = parse_price({"priceValue": 4, "basePrice": [{"basePriceValue": 2}]})

    assert price.unit_price == Decimal("2")
    assert price.reference is None


def test_a_strike_price_that_is_not_higher_is_no_discount() -> None:
    price = parse_price({"priceValue": 2, "strikePrice": {"strikePriceValue": 2}})

    assert price.previous is None
    assert price.is_discounted is False
    assert price.discount_percentage is None


@pytest.mark.parametrize("field", ["objectID", "name"])
def test_a_record_without_an_identity_is_rejected(field: str) -> None:
    data = {key: value for key, value in record().items() if key != field}

    with pytest.raises(InvalidResponseError):
        parse_product(data)


def test_missing_and_unknown_fields_are_tolerated() -> None:
    product = parse_product(
        {
            "objectID": 1,
            "name": "x",
            "assets": [{"type": "primary"}, "?"],
            "productReferences": [{"type": "EAN", "value": "1"}],
            "currentPrice": {"validFrom": "soon"},
            "surprise": [1],
        }
    )

    assert product.id == "1"
    assert product.photos == ()
    assert product.thumbnail is None
    assert product.article_number is None
    assert product.price.valid_from is None
    assert product.url is None
    assert product.availability.available is None


# ------------------------------------------------------------------ queries


def test_hits_become_a_page_with_an_offset_cursor() -> None:
    page_data = read_fixture(STORE, "search.json")

    result = parse_hits(
        page_data, query="leche", offset=0, page_size=3, region=Region.PENINSULA
    )

    assert result.total_hits == 111
    assert result.next_cursor == "3"
    assert result.offset == 0
    assert result.region == "pen"
    assert [product.id for product in result.products] == ["872200", "828100", "82000"]


def test_the_last_page_and_an_unknown_total() -> None:
    page_data = read_fixture(STORE, "search.json")
    last = parse_hits(
        {**page_data, "nbHits": 3},
        query="x",
        offset=0,
        page_size=24,
        region=Region.CANARY_ISLANDS,
    )
    unknown = parse_hits(
        {"hits": page_data["hits"]},
        query="x",
        offset=6,
        page_size=3,
        region=Region.PENINSULA,
    )
    empty = parse_hits(
        {"hits": []}, query="x", offset=6, page_size=3, region=Region.PENINSULA
    )

    assert last.next_cursor is None
    assert unknown.total_hits is None
    assert unknown.next_cursor == "9"
    assert empty.next_cursor is None


def test_an_answer_without_hits_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="no hits array"):
        parse_hits({}, query="x", offset=0, page_size=1, region=Region.PENINSULA)


# ------------------------------------------------------------- site pages


def test_the_overview_lists_the_visible_top_level_categories() -> None:
    roots = parse_children(
        api_data(page_props(page("productos.html"))), parent_id=None, level=1
    )

    # "verano" is out of season and hidden by the site
    assert [root.id for root in roots] == [
        "fruta-y-verdura",
        "lacteos-y-huevos",
        "marcas",
    ]
    dairy = roots[1]
    assert dairy.name == "Lácteos y huevos"
    assert dairy.key == "lacteos-y-huevos"
    assert dairy.level == 1
    assert dairy.parent_id is None
    assert dairy.url == "https://www.aldi.es/productos/lacteos-y-huevos.html"
    assert dairy.image_url is not None


def test_a_top_level_page_carries_its_children_and_no_filter() -> None:
    node, filters = parse_category_page(page("category_root.html"))

    assert filters is None
    assert (node.id, node.name, node.level, node.parent_id) == (
        "lacteos-y-huevos",
        "Lácteos y huevos",
        1,
        None,
    )
    milk = node.children[1]
    assert milk.id == "lacteos-y-huevos/leche-y-bebidas-vegetales"
    assert milk.key == "leche-y-bebidas-vegetales"
    assert milk.slug == "leche-y-bebidas-vegetales"
    assert milk.parent_id == "lacteos-y-huevos"
    assert milk.level == 2


def test_a_child_page_carries_its_parent_and_filter() -> None:
    node, filters = parse_category_page(page("category_leaf.html"))

    assert filters == "categoryIDs:leche-y-bebidas-vegetales"
    assert node.id == "lacteos-y-huevos/leche-y-bebidas-vegetales"
    assert node.parent_id == "lacteos-y-huevos"
    assert node.level == 2
    assert node.children == ()


def test_a_page_that_names_no_category_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="names no category"):
        parse_category_page(page("productos.html"))


def test_children_without_a_path_or_a_name_are_skipped() -> None:
    assert (
        parse_category_node(
            {"reference": {"path": "/elsewhere/x"}, "title": "x"},
            parent_id=None,
            level=1,
        )
        is None
    )
    assert (
        parse_category_node(
            {"reference": {"path": "/productos/x"}}, parent_id=None, level=1
        )
        is None
    )
    node = parse_category_node(
        {
            "reference": {"path": "/productos/x"},
            "title": "X",
            "marketingCategory": True,
        },
        parent_id=None,
        level=1,
    )
    assert node is not None
    assert (node.name, node.key, node.is_marketing) == ("X", None, True)


def test_the_offers_page_becomes_dated_sections() -> None:
    groups = parse_offer_groups(page("offers.html"), region=Region.PENINSULA)

    first, second = groups
    assert first.title == "Ofertas en fruta y verdura"
    assert first.period == "Desde el 28 de septiembre de 2026"
    assert first.starts_on == date(2026, 9, 28)
    assert first.ends_on == date(2026, 10, 4)
    assert first.published_on == date(2026, 9, 25)
    # the page names one product it does not embed; it is left out
    assert [product.id for product in first.products] == ["994700", "600237700"]
    assert all(product.region == "pen" for product in first.products)
    assert second.title == "Flores y plantas"


def test_offer_sections_without_a_title_or_a_date_are_tolerated() -> None:
    document = copy.deepcopy(page("offers.html"))
    props = document["props"]["pageProps"]
    answers = json.loads(props["apiData"])
    offers = answers[0][1]["res"]
    offers["categories"][0]["startDate"] = "lunes"
    del offers["categories"][0]["endDate"]
    offers["categories"][0]["content"].append({"productIds": ["994700"]})
    props["apiData"] = answers

    groups = parse_offer_groups(document)

    assert len(groups) == 2
    assert groups[0].starts_on is None
    assert groups[0].ends_on is None


def test_embedded_data_must_be_present_and_readable() -> None:
    with pytest.raises(InvalidResponseError, match="no __NEXT_DATA__"):
        next_data("<html></html>")
    with pytest.raises(InvalidResponseError, match="unreadable __NEXT_DATA__"):
        next_data('<script id="__NEXT_DATA__" type="application/json">{</script>')
    with pytest.raises(InvalidResponseError, match="not an object"):
        next_data(html([1]))
    with pytest.raises(InvalidResponseError, match="apiData is unreadable"):
        api_data({"apiData": "{"})


def test_api_data_skips_malformed_pairs() -> None:
    answers = api_data({"apiData": [["A", {"res": 1}], ["B"], [3, {}], "x"]})

    assert answers == {"A": 1}
    assert api_data({}) == {}
    assert page_props({}) == {}
