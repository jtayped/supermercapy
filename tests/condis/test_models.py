from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from supermercapy import Product, Unit, UnitPrice
from supermercapy.condis import CondisCategory, CondisProduct
from supermercapy.condis.models import (
    flight_payload,
    flight_value,
    page_url,
    parse_categories,
    parse_product,
    parse_row,
    parse_search_result,
    parse_suggestions,
)
from tests.conftest import read_fixture

STORE = "condis"
PHOTOS = "https://cdn.condis.es/fit-in/1920x1080/es/products"


def rows(name: str) -> list[Any]:
    return list(read_fixture(STORE, name)["catalog"]["content"])


def row(product_id: str) -> Any:
    return next(item for item in rows("search.json") if item["id"] == product_id)


def sheet(name: str) -> Any:
    return flight_value(flight_payload(read_fixture(STORE, name)), "productInformation")


def page(name: str) -> CondisProduct:
    html = read_fixture(STORE, name)
    payload = flight_payload(html)
    return parse_product(
        flight_value(payload, "productInformation"),
        branch=flight_value(payload, "matchedChild"),
        section=flight_value(payload, "parentCategoryName"),
        url=page_url(html),
    )


BRANCH = {
    "id": "c07__cat00210003",
    "name": "Leche",
    "externalId": "cat00210003",
    "children": [
        "junk",
        {
            "id": "c07__cat00210003__cat002100030003",
            "name": "Leche entera",
            "externalId": "cat002100030003",
        },
    ],
}


# --------------------------------------------------------------------- payload


def test_the_flight_payload_joins_every_chunk_the_page_streamed() -> None:
    html = (
        '<script>self.__next_f.push([1,"a:{\\"x\\":"])</script>'
        '<script>self.__next_f.push([1,"1}\\n"])</script>'
        '<script>self.__next_f.push([1,"\\u00"])</script>'
    )

    payload = flight_payload(html)

    # the last chunk is no valid string literal and is skipped
    assert payload == 'a:{"x":1}\n'
    assert flight_value(payload, "x") == 1
    assert flight_value(payload, "y") is None


def test_a_key_followed_by_no_json_is_read_at_its_next_occurrence() -> None:
    payload = '"k":$undefined,"k":{"v":2}'

    assert flight_value(payload, "k") == {"v": 2}
    assert flight_value('"k":$undefined', "k") is None


def test_a_reference_to_a_value_streamed_elsewhere_is_passed_over() -> None:
    payload = '"k":"$undefined","k":null,"k":"$8:1:props","k":[1],"s":"text"'

    assert flight_value(payload, "k") == [1]
    assert flight_value(payload, "s") == "text"
    assert flight_value('"k":"$undefined"', "k") is None


def test_the_canonical_address_is_read_from_the_page_head() -> None:
    assert page_url(read_fixture(STORE, "product_704049.html")) == (
        "https://compraonline.condis.es/leche-condis-entera-1-l/p/704049/es_ES"
    )
    assert page_url(read_fixture(STORE, "product_missing.html")) is None


# ------------------------------------------------------------------ index rows


def test_a_row_reads_names_prices_and_the_unit_price_text() -> None:
    product = parse_row(row("704049"))

    assert isinstance(product, CondisProduct)
    assert product.id == "704049"
    assert product.name == "LECHE CONDIS ENTERA 1 L"
    assert product.ean is None
    assert product.slug == "leche-condis-entera-1-l"
    assert product.url == (
        "https://compraonline.condis.es/leche-condis-entera-1-l/p/704049/es_ES"
    )
    assert product.thumbnail is not None
    assert product.thumbnail.url == f"{PHOTOS}/704049.jpg"
    assert product.price.amount == Decimal("0.99")
    assert product.price.previous is None
    assert product.price.is_discounted is False
    assert product.price.unit_price == Decimal("0.99")
    assert product.price.unit_price_unit == "Litro"
    assert product.price.unit_price_text == "0,99€/Litro"
    assert product.price.reference == UnitPrice(amount=Decimal("0.99"), unit=Unit.LITRE)
    assert product.category_ids == ("c07__cat00210003",)
    assert product.category_names == ("Leche entera", "Bebidas", "Leche")
    assert product.section == "Bebidas"
    assert product.is_own_brand is True
    assert product.is_prime is True
    assert "Marca propia" in product.badges
    assert product.kcal == Decimal("63")
    assert product.availability.available is True


def test_a_row_on_sale_keeps_the_regular_price_as_the_previous_one() -> None:
    price = parse_row(row("704036")).price

    assert price.amount == Decimal("1.89")
    assert price.previous == Decimal("1.97")
    assert price.is_discounted is True


def test_a_multibuy_becomes_a_promotion_without_touching_the_price() -> None:
    product = parse_row(row("704309"))

    assert [promotion.description for promotion in product.promotions] == [
        "Llévate 6 y paga 5"
    ]
    assert product.promotions[0].kind == "promotion"
    assert product.price.previous is None


def test_a_label_on_a_row_not_on_promotion_is_no_promotion() -> None:
    novelty = rows("novelties.json")[1]
    assert novelty["promotion_text"] == "Novedad"

    product = parse_row(novelty)

    assert product.promotions == ()
    assert product.is_new is True


def test_a_promotion_keeps_the_text_with_its_spacing_collapsed() -> None:
    product = parse_row(rows("on_promotion.json")[0])

    assert [promotion.description for promotion in product.promotions] == [
        "2ªu 50%,prod.-valor"
    ]
    assert product.price.previous == Decimal("5.99")


@pytest.mark.parametrize(
    ("pum", "expected"),
    [
        ("8,11€/Kilo", UnitPrice(amount=Decimal("8.11"), unit=Unit.KILOGRAM)),
        ("28,96€/Kg", UnitPrice(amount=Decimal("28.96"), unit=Unit.KILOGRAM)),
        ("1,25€/100 ml", UnitPrice(amount=Decimal("12.50"), unit=Unit.LITRE)),
        ("2,99€/Unidad", UnitPrice(amount=Decimal("2.99"), unit=Unit.PIECE)),
        ("1,55€/Pieza", UnitPrice(amount=Decimal("1.55"), unit=Unit.PIECE)),
        # per kilogram of drained weight
        ("19,98€/Kilo PNE", UnitPrice(amount=Decimal("19.98"), unit=Unit.KILOGRAM)),
        ("", None),
        ("precio", None),
    ],
)
def test_every_unit_the_index_sends_is_normalised(
    pum: str, expected: UnitPrice | None
) -> None:
    item = row("704049")
    item["pum"] = pum

    price = parse_row(item).price

    assert price.reference == expected
    if expected is None:
        assert price.unit_price is None
        assert price.unit_price_unit is None


def test_photos_are_named_by_file_and_fetched_at_the_zoom_size() -> None:
    item = row("704049")
    item["altImages"] = ["704049.jpg", " ", 3, "704049_2.jpg", "704049.png"]

    product = parse_row(item)

    assert [photo.url for photo in product.photos] == [
        f"{PHOTOS}/704049.jpg",
        f"{PHOTOS}/704049_2.jpg",
        # the storefront appends the extension to whatever it is named
        f"{PHOTOS}/704049.png.jpg",
    ]
    assert product.photos[0].alt == "LECHE CONDIS ENTERA 1 L"


def test_a_sparse_row_still_parses() -> None:
    product = parse_row({"id": 7, "description": " x "})

    assert product.id == "7"
    assert product.name == "x"
    assert product.url is None
    assert product.slug is None
    assert product.thumbnail is None
    assert product.availability.available is None
    assert product.category_ids == ()
    assert product.price.amount is None


def test_an_url_that_is_not_a_product_path_has_no_slug() -> None:
    item = row("704049")
    item["url"] = "https://elsewhere.invalid/x"

    product = parse_row(item)

    assert product.slug is None
    assert product.url == "https://elsewhere.invalid/x"


def test_a_page_offers_the_next_offset_until_the_hits_run_out() -> None:
    first = parse_search_result(
        read_fixture(STORE, "search.json"),
        query="leche",
        offset=0,
        page_size=3,
        max_start=2494,
    )
    last = parse_search_result(
        read_fixture(STORE, "search_last.json"),
        query="leche",
        offset=414,
        page_size=5,
        max_start=2494,
    )

    assert first.total_hits == 416
    assert first.next_cursor == "3"
    assert last.next_cursor is None
    assert last.offset == 414


def test_a_page_against_the_offset_ceiling_is_truncated() -> None:
    data = read_fixture(STORE, "search.json")
    data["catalog"]["numFound"] = 5000

    page = parse_search_result(
        data,
        query="leche",
        offset=2493,
        page_size=3,
        max_start=2494,
    )

    assert page.next_cursor is None
    assert page.truncated is True


def test_an_empty_or_countless_answer_ends_the_walk() -> None:
    page = parse_search_result(
        {"catalog": {"content": [{"id": "1", "description": "x"}]}},
        query="",
        offset=0,
        page_size=1,
        max_start=2494,
    )

    assert page.total_hits is None
    assert page.next_cursor == "1"
    assert (
        parse_search_result({}, query="", offset=0, page_size=1, max_start=0).products
        == ()
    )


def test_suggestions_are_distinct_and_trimmed() -> None:
    assert parse_suggestions(read_fixture(STORE, "empathize.json"))[0] == "leches"
    assert parse_suggestions(
        {"topTrends": {"content": [{"title": " a "}, {"title_raw": "a"}, {}]}}
    ) == ("a",)


# --------------------------------------------------------------- product sheet


def test_the_sheet_reads_cents_ingredients_and_the_nutrition_table() -> None:
    product = page("product_704049.html")

    assert isinstance(product, Product)
    assert product.id == "704049"
    assert product.brand == "Condis"
    assert product.pack_size_text == "1.0 litros"
    assert product.price.amount == Decimal("0.99")
    assert product.price.previous is None
    assert product.price.unit_price_text == "0,99€/Litro"
    assert product.price.reference == UnitPrice(amount=Decimal("0.99"), unit=Unit.LITRE)
    assert product.description == "Leche Entera UHT."
    assert product.usage is not None
    assert product.manufacturer == "Sabores Naturales de Aranda, S.L.U."
    nutrition = product.nutrition
    assert nutrition is not None
    assert nutrition.per == "100 g"
    assert nutrition.values[0].name == "Valor energético"
    assert nutrition.values[0].per_100 == "63,00 kcal"
    assert nutrition.values[0].unit == "kcal"
    assert nutrition.ingredients == "LECHE ENTERA. ORIGEN DE LA LECHE: ESPAÑA."
    assert nutrition.allergens == "LECHE"
    assert product.photos[0].url == f"{PHOTOS}/704049.jpg"
    assert product.promotions == ()
    assert product.slug == "leche-condis-entera-1-l"
    assert product.url == (
        "https://compraonline.condis.es/leche-condis-entera-1-l/p/704049/es_ES"
    )


def test_the_sheet_is_placed_in_the_tree_by_the_branch_around_it() -> None:
    product = page("product_704049.html")

    path = product.category_path
    assert [category.id for category in path] == [
        "c07",
        "c07__cat00210003",
        "c07__cat00210003__cat002100030003",
    ]
    assert [category.name for category in path] == ["Bebidas", "Leche", "Leche entera"]
    assert [category.parent_id for category in path] == [
        None,
        "c07",
        "c07__cat00210003",
    ]
    assert [category.level for category in path] == [0, 1, 2]
    assert all(isinstance(category, CondisCategory) for category in path)
    assert path[-1].external_id == "cat002100030003"
    assert product.category_ids == ("c07__cat00210003__cat002100030003",)
    assert (product.section, product.family, product.variety) == (
        "Bebidas",
        "Leche",
        "Leche entera",
    )
    assert product.category_names == ()


def test_a_sheet_whose_leaf_is_not_in_the_branch_has_no_path() -> None:
    info = sheet("product_704049.html")
    info["parent_category_id"] = "cat999"

    product = parse_product(info, branch=BRANCH, section="Bebidas")

    assert product.category_path == ()
    assert product.category_ids == ()
    assert product.family is None
    assert product.variety == "Leche entera"


def test_a_branch_without_the_top_level_name_starts_the_path_below_it() -> None:
    product = parse_product(sheet("product_704049.html"), branch=BRANCH)

    assert [category.id for category in product.category_path] == [
        "c07__cat00210003",
        "c07__cat00210003__cat002100030003",
    ]
    assert product.category_path[0].parent_id == "c07"
    assert product.section is None
    assert product.family == "Leche"


def test_a_top_level_branch_has_no_parent() -> None:
    branch = {
        "id": "c07",
        "name": "Bebidas",
        "children": [
            {"id": "c07__cat1", "name": "Leche", "externalId": "cat002100030003"}
        ],
    }

    product = parse_product(sheet("product_704049.html"), branch=branch, section="x")

    assert [(c.id, c.parent_id, c.level) for c in product.category_path] == [
        ("c07", None, 0),
        ("c07__cat1", "c07", 1),
    ]
    assert product.section is None


@pytest.mark.parametrize(
    "branch",
    [
        None,
        {"name": "Leche", "children": BRANCH["children"]},
        {**BRANCH, "children": [{"name": "x", "externalId": "cat002100030003"}]},
    ],
)
def test_an_unusable_branch_leaves_the_path_empty(branch: Any) -> None:
    product = parse_product(sheet("product_704049.html"), branch=branch, section="x")

    assert product.category_path == ()


def test_a_promotion_on_the_sheet_needs_its_flag() -> None:
    info = sheet("product_704049.html")
    info["promotional_info"] = "Llévate 6 y paga 5"

    assert parse_product(info).promotions == ()
    info["on_promotion"] = True
    assert [p.description for p in parse_product(info).promotions] == [
        "Llévate 6 y paga 5"
    ]


def test_a_sale_price_on_the_sheet_replaces_the_list_price() -> None:
    price = parse_product(sheet("product_704056.html")).price

    assert price.amount == Decimal("1.29")
    assert price.previous == Decimal("1.59")
    assert price.is_discounted is True


def test_a_bare_sheet_has_no_nutrition_and_no_path() -> None:
    product = parse_product(
        {
            "ID": "1",
            "description": "x",
            "sale_price": 0,
            "nutritional_info": {"nutritional_facts": [{"amount_per_100g": "1"}]},
            "promotional_info": "  ",
            "in_bulk_info": {"origin": "España"},
        }
    )

    assert product.nutrition is None
    assert product.category_path == ()
    assert product.promotions == ()
    assert product.price.amount is None
    assert product.origin == "España"
    assert product.url is None
    assert product.slug is None


def test_a_sheet_with_only_ingredients_still_has_nutrition() -> None:
    nutrition = parse_product(
        {"ID": "1", "description": "x", "ingredients": "agua, <b></b> sal"}
    ).nutrition

    assert nutrition is not None
    assert nutrition.values == ()
    assert nutrition.per is None
    assert nutrition.allergens is None


# ------------------------------------------------------------------ categories


def test_the_navigation_tree_nests_three_levels() -> None:
    tree = parse_categories(
        flight_value(flight_payload(read_fixture(STORE, "home.html")), "categoryList")
    )

    assert [category.id for category in tree] == ["c07", "c03"]
    child = tree[0].children[0]
    assert child.parent_id == "c07"
    assert child.level == 1
    grandchild = child.children[0]
    assert grandchild.id.startswith(f"{child.id}__")
    assert grandchild.external_id is not None
    assert grandchild.level == 2


def test_a_node_without_an_id_or_a_name_is_dropped() -> None:
    assert parse_categories([{"name": "x"}, {"id": "c01"}, "junk"]) == ()
