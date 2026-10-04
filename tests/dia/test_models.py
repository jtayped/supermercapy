from __future__ import annotations

import copy
import dataclasses
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import InvalidResponseError, Product, ProductSummary, Unit, UnitPrice
from supermercapy.dia import (
    DiaCategory,
    DiaNutrition,
    DiaPrice,
    DiaProduct,
    DiaPromotion,
)
from supermercapy.dia.models import (
    absolute_url,
    category_id_of,
    parse_categories,
    parse_nutrition,
    parse_price,
    parse_product,
    parse_promotion,
    parse_row,
)
from tests.conftest import read_fixture

STORE = "dia"
SITE = "https://www.dia.es"


def search_rows() -> list[Any]:
    rows: list[Any] = read_fixture(STORE, "search.json")["search_items"]
    return rows


def sheet(name: str = "product.json") -> Any:
    return read_fixture(STORE, name)


# ------------------------------------------------------------------------ rows


def test_a_row_maps_every_documented_field() -> None:
    product = parse_row(search_rows()[0])

    assert isinstance(product, DiaProduct)
    assert isinstance(product, ProductSummary)
    assert product.id == "130063P6"
    assert product.name == "Leche semidesnatada sin lactosa Dia Láctea pack 6 x 1 L"
    assert product.brand == "Dia Láctea"
    assert product.url == (
        f"{SITE}/huevos-leche-y-mantequilla/leche-sin-lactosa-y-enriquecidas/p/130063P6"
    )
    assert product.thumbnail is not None
    assert product.thumbnail.url == (
        f"{SITE}/product_images/130063P6/130063P6_ISO_0_ES.jpg"
    )
    assert product.thumbnail.kind == "ISO"
    assert product.availability.available is True
    assert product.availability.units_in_stock == 259
    assert product.is_dia_brand is True
    assert product.brand_type == "D"
    assert product.stamp == "Mejor valorado"
    assert product.stamp_code == "4"
    assert product.is_new is False
    assert product.is_variable_weight is False
    assert product.category_ids == ()
    assert product.ean is None
    assert product.nutrition is None


def test_a_club_offer_keeps_the_struck_price_and_the_member_flag() -> None:
    price = parse_row(search_rows()[0]).price

    assert isinstance(price, DiaPrice)
    assert price.amount == Decimal("5.34")
    assert price.previous == Decimal("5.64")
    assert price.is_discounted is True
    assert price.discount_percentage == Decimal("5")
    assert price.is_club_price is True
    assert price.currency == "EUR"


def test_the_unit_price_is_the_one_the_shopper_pays() -> None:
    price = parse_row(search_rows()[0]).price

    assert price.unit_price == Decimal("0.89")
    assert price.unit_price_unit == "LITRO"
    assert price.reference == UnitPrice(amount=Decimal("0.89"), unit=Unit.LITRE)


def test_a_row_promotion_carries_its_label_and_member_flag() -> None:
    (promotion,) = parse_row(search_rows()[0]).promotions

    assert isinstance(promotion, DiaPromotion)
    assert promotion.description == "SOLO A 0,89 EUR LECHE S.L. SEM DIA LÁCTEA 1 L"
    assert promotion.short_description == "LECHE S/LACTOSA SEMI"
    assert promotion.member_only is True
    assert promotion.is_online_only is False
    assert promotion.price is None
    assert promotion.starts_at is None


def test_a_plain_row_is_not_discounted() -> None:
    product = parse_row(search_rows()[1])

    assert product.brand == "Asturiana"
    assert product.is_dia_brand is False
    assert product.price.amount == Decimal("8.22")
    assert product.price.previous is None
    assert product.price.is_discounted is False
    assert product.price.discount_percentage is None
    assert product.price.is_club_price is False
    assert product.promotions == ()
    assert product.stamp is None


def test_loose_produce_is_variable_weight_and_approximate() -> None:
    product = parse_row(search_rows()[3])

    assert product.name == "Banana granel 900 g aprox."
    assert product.brand is None
    assert product.is_variable_weight is True
    assert product.price.is_approximate is True
    assert product.price.reference == UnitPrice(
        amount=Decimal("1.55"), unit=Unit.KILOGRAM
    )


def test_a_listing_row_records_the_category_it_was_listed_under() -> None:
    row = read_fixture(STORE, "listing.json")["plp_items"][0]

    assert parse_row(row, category_id="L2051").category_ids == ("L2051",)


def test_the_new_stamp_marks_a_row_new() -> None:
    row = {**search_rows()[2], "stamp_code": "2", "stamp_description": "Novedad"}

    product = parse_row(row)

    assert product.is_new is True
    assert product.stamp == "Novedad"


def test_an_online_only_promotion_is_flagged() -> None:
    row = read_fixture(STORE, "category_offers.json")["plp_items"][1]

    product = parse_row(row)

    assert product.price.is_discounted is False
    assert product.promotions[0].description == "2ª UD AL 50% DTO. SELECCIÓN ALPRO"
    assert product.promotions[0].is_online_only is True


def test_a_row_falls_back_to_the_object_id() -> None:
    row = {key: value for key, value in search_rows()[1].items() if key != "sku_id"}

    assert parse_row(row).id == "148777P6"


@pytest.mark.parametrize("field", ["sku_id", "display_name"])
def test_a_row_without_an_identity_is_rejected(field: str) -> None:
    row = {
        key: value
        for key, value in search_rows()[1].items()
        if key not in {field, "object_id"}
    }

    with pytest.raises(InvalidResponseError):
        parse_row(row)


def test_missing_and_unknown_row_fields_are_tolerated() -> None:
    product = parse_row(
        {"sku_id": 1, "display_name": "x", "prices": "?", "surprise": {"a": 1}}
    )

    assert product.id == "1"
    assert product.price == DiaPrice()
    assert product.availability.available is None
    assert product.thumbnail is None
    assert product.url is None


def test_promotions_without_text_are_dropped() -> None:
    assert parse_promotion({"only_club_dia": True}) is None
    short = parse_promotion({"short_description": "PIZZA"})
    assert short is not None
    assert short.description == "PIZZA"


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("LITRO", UnitPrice(amount=Decimal("2"), unit=Unit.LITRE)),
        ("KILO", UnitPrice(amount=Decimal("2"), unit=Unit.KILOGRAM)),
        ("UNIDAD", UnitPrice(amount=Decimal("2"), unit=Unit.PIECE)),
        ("100 ML.", UnitPrice(amount=Decimal("20"), unit=Unit.LITRE)),
        ("100 GR.", UnitPrice(amount=Decimal("20"), unit=Unit.KILOGRAM)),
        ("LAVADO", UnitPrice(amount=Decimal("2"), unit=Unit.DOSE)),
        ("DOCENA", UnitPrice(amount=Decimal("0.1667"), unit=Unit.PIECE)),
        ("METRO", UnitPrice(amount=Decimal("2"), unit=Unit.METRE)),
        ("CAJA", None),
    ],
)
def test_every_measure_unit_seen_is_normalised(
    unit: str, expected: UnitPrice | None
) -> None:
    price = parse_price({"price": 4, "price_per_unit": 2, "measure_unit": unit})

    assert price.unit_price_unit == unit
    assert price.reference == expected


def test_a_promo_flag_without_a_lower_price_keeps_no_previous_price() -> None:
    price = parse_price({"price": 2, "strikethrough_price": 2, "is_promo_price": True})

    assert price.is_discounted is True
    assert price.previous is None
    assert price.discount_percentage is None


# ----------------------------------------------------------------- the sheet


def test_the_sheet_maps_every_documented_field() -> None:
    product = parse_product(sheet())

    assert isinstance(product, Product)
    assert product.id == "504P6"
    assert product.name == "Leche semidesnatada Dia Láctea pack 6 x 1 L"
    assert product.brand is None
    assert product.url is None
    assert product.legal_name == "Leche semidesnatada UHT."
    assert product.description is None
    assert product.net_content == "Contenido neto: 6L"
    assert product.storage is not None
    assert product.storage.startswith("Conservar en un lugar fresco")
    assert product.usage is None
    assert product.info_labels == ("Tipo de leche: Semidesnatada",)
    assert product.manufacturer == "Lactalis P., S.L."
    assert product.manufacturer_address is not None
    assert product.price.amount == Decimal("5.04")
    assert product.price.reference == UnitPrice(amount=Decimal("0.84"), unit=Unit.LITRE)
    assert product.availability.units_in_stock == 764
    assert [photo.kind for photo in product.photos] == ["ISO", "FRO", "TRA"]
    assert product.thumbnail == product.photos[0]


def test_the_breadcrumb_becomes_the_category_path() -> None:
    product = parse_product(sheet())

    assert product.category_ids == ("L108", "L2051")
    root, leaf = product.category_path
    assert isinstance(leaf, DiaCategory)
    assert (root.id, root.name, root.level, root.parent_id) == (
        "L108",
        "Huevos, leche y mantequilla",
        1,
        None,
    )
    assert (leaf.id, leaf.level, leaf.parent_id) == ("L2051", 2, "L108")
    assert leaf.url == "/huevos-leche-y-mantequilla/leche/c/L2051"


def test_the_nutrition_table_is_per_the_declared_size() -> None:
    nutrition = parse_product(sheet()).nutrition

    assert isinstance(nutrition, DiaNutrition)
    assert nutrition.per == "100 ml"
    assert nutrition.energy_kcal == Decimal("46")
    assert nutrition.energy_kj == Decimal("192")
    names = [value.name for value in nutrition.values]
    assert names == [
        "Grasas",
        "de las cuales saturadas",
        "Hidratos de Carbono",
        "de los cuales azúcares",
        "Proteínas",
        "Sal",
        "Calcio",
    ]
    fat = nutrition.values[0]
    assert (fat.per_100, fat.per_serving, fat.unit) == ("1.6", None, "g")
    assert nutrition.values[-1].unit == "mg"


def test_ingredients_are_stripped_and_bold_words_become_allergens() -> None:
    nutrition = parse_product(sheet()).nutrition

    assert nutrition is not None
    assert nutrition.ingredients == "Leche semidesnatada de vaca."
    assert nutrition.allergens == "Leche"
    assert (
        nutrition.raw_html
        == "<p><strong>Leche&nbsp;</strong>semidesnatada de vaca.</p>"
    )


def test_a_table_without_a_declared_size_is_kept_per_serving() -> None:
    data = sheet()["product"]
    data = copy.deepcopy(data)
    del data["nutritional_info"]["nutri_size"]

    nutrition = parse_nutrition(data)

    assert nutrition is not None
    assert nutrition.per is None
    assert nutrition.values[0].per_100 is None
    assert nutrition.values[0].per_serving == "1.6"


def test_loose_produce_has_no_nutrition_table_but_keeps_its_labels() -> None:
    product = parse_product(sheet("product_loose.json"))

    assert product.is_variable_weight is True
    assert product.price.is_approximate is True
    assert product.description == "BANANA GRANEL"
    assert product.info_labels
    assert product.nutrition is not None
    assert product.nutrition.values == ()


def test_a_sheet_with_nothing_nutritional_has_no_nutrition() -> None:
    data = copy.deepcopy(sheet()["product"])
    for key in ("ingredients", "nutritional_info"):
        del data[key]
    data["nutritional_info"] = {"nutritional_values": {"values": [{"title": " "}]}}

    assert parse_nutrition(data) is None


def test_ingredients_without_bold_words_have_no_allergens() -> None:
    data = {"ingredients": {"text": "<p>agua</p>"}}

    nutrition = parse_nutrition(data)

    assert nutrition is not None
    assert nutrition.allergens is None
    assert parse_nutrition({"ingredients": {"title": "Ingredientes"}}) is None


def test_the_offer_sheet_carries_its_promotion_and_stamp() -> None:
    product = parse_product(sheet("product_offer.json"))

    assert product.price.is_club_price is True
    assert product.price.previous == Decimal("1.75")
    (promotion,) = product.promotions
    assert promotion.kind == "promotion"
    assert promotion.member_only is True
    assert product.stamp == "Air Fryer"
    assert product.usage is not None


def test_a_sheet_without_a_product_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="no product object"):
        parse_product({"product": []})


def test_breadcrumb_entries_without_a_category_link_are_skipped() -> None:
    data = copy.deepcopy(sheet())
    data["product"]["breadcrumb"].insert(0, {"link": "/", "title": "Inicio"})
    data["product"]["breadcrumb"].append({"link": "/x/c/L1"})

    assert parse_product(data).category_ids == ("L108", "L2051")


def test_unknown_sheet_fields_are_ignored() -> None:
    data = copy.deepcopy(sheet())
    data["product"]["surprise"] = [1, 2]
    data["product"]["info_labels"] = ["ok", 3]

    assert parse_product(data).info_labels == ("ok",)


# ------------------------------------------------------------------ the tree


def test_the_menu_becomes_a_two_level_tree_without_self_children() -> None:
    tree = parse_categories(read_fixture(STORE, "menu.json"))

    assert [root.id for root in tree] == ["L128", "L105", "L108"]
    fruit = tree[1]
    assert fruit.name == "Frutas"
    assert fruit.level == 1
    assert fruit.url == "/frutas/c/L105"
    assert fruit.image_url == f"{SITE}/category_images/L105/L105_icon.png"
    # the "todo frutas" entry is the parent's own page, not a child
    assert [child.id for child in fruit.children] == ["L2040", "L2033"]
    assert all(child.parent_id == "L105" for child in fruit.children)
    assert all(child.level == 2 for child in fruit.children)


def test_nodes_without_an_id_or_name_are_skipped() -> None:
    tree = parse_categories(
        {"categories": [{"id": "L1", "name": "a", "children": [{"id": "L2"}]}, {}]}
    )

    assert [(root.id, root.children) for root in tree] == [("L1", ())]


def test_a_menu_without_categories_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="no categories array"):
        parse_categories({})


# ------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/product_images/1/1.jpg", f"{SITE}/product_images/1/1.jpg"),
        ("product_images/1/1.jpg", f"{SITE}/product_images/1/1.jpg"),
        ("https://cdn.example/1.jpg", "https://cdn.example/1.jpg"),
        ("  ", None),
        (None, None),
    ],
)
def test_absolute_urls(path: object, expected: str | None) -> None:
    assert absolute_url(path) == expected


def test_category_ids_come_from_the_end_of_a_link() -> None:
    assert category_id_of("/frutas/c/L105") == "L105"
    assert category_id_of("/frutas") is None
    assert category_id_of(None) is None


def test_models_are_frozen_and_slotted() -> None:
    product = parse_row(search_rows()[0])

    assert not hasattr(product, "__dict__")
    with pytest.raises(FrozenInstanceError):
        product.name = "changed"  # type: ignore[misc]
    assert dataclasses.replace(product, name="x").name == "x"


def test_bold_words_are_listed_once_and_blank_ones_skipped() -> None:
    html = "<strong>Leche</strong>, <strong> </strong>, soja, <strong>Leche</strong>"

    nutrition = parse_nutrition({"ingredients": {"text": html}})

    assert nutrition is not None
    assert nutrition.allergens == "Leche"


def test_a_table_without_ingredients_has_no_allergens() -> None:
    data = copy.deepcopy(sheet()["product"])
    del data["ingredients"]

    nutrition = parse_nutrition(data)

    assert nutrition is not None
    assert nutrition.values
    assert (nutrition.ingredients, nutrition.allergens, nutrition.raw_html) == (
        None,
        None,
        None,
    )
