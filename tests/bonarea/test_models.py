from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import (
    ConfigurationError,
    InvalidResponseError,
    Photo,
    Unit,
    UnitPrice,
)
from supermercapy.bonarea import (
    BonareaCategory,
    BonareaPhoto,
    BonareaPrice,
    BonareaProduct,
    Characteristic,
    listing_categories,
    to_api_id,
    to_url_id,
)
from supermercapy.bonarea.models import (
    parse_articles,
    parse_category_tree,
    parse_nutrition,
    parse_postal_codes,
    parse_product,
    parse_search_result,
)
from tests.conftest import read_fixture

STORE = "bonarea"


def article(name: str = "product.json") -> Any:
    return read_fixture(STORE, name)["article"]


def listing(name: str = "category.json") -> Any:
    return read_fixture(STORE, name)


# ------------------------------------------------------------------------- ids


@pytest.mark.parametrize(
    ("value", "api", "url"),
    [
        ("13*5361", "13*5361", "13_5361"),
        ("13_5361", "13*5361", "13_5361"),
        ("  13_300_010  ", "13*300*010", "13_300_010"),
        ("03_78501", "03*78501", "03_78501"),
        (5361, "5361", "5361"),
    ],
)
def test_both_id_spellings_translate_in_either_direction(
    value: Any, api: str, url: str
) -> None:
    # the json api rejects the underscore form that urls and images use, so
    # every id is normalised on the way in and translated on the way out
    assert to_api_id(value) == api
    assert to_url_id(value) == url
    assert to_url_id(to_api_id(value)) == url
    assert to_api_id(to_url_id(value)) == api


def test_a_product_exposes_the_url_spelling_of_its_id() -> None:
    product = parse_product(article())

    assert product.id == "13*5361"
    assert product.url_id == "13_5361"
    assert product.url.endswith("/13_5361")


def test_a_category_exposes_the_url_spelling_of_its_id() -> None:
    category = parse_category_tree(listing("tree.json")["nivells"])[0]

    assert category.id == "13*300"
    assert category.url_id == "13_300"


# -------------------------------------------------------------------- products


def test_a_product_maps_every_documented_field() -> None:
    product = parse_product(article())

    assert isinstance(product, BonareaProduct)
    assert product.id == "13*5361"
    assert product.name == "Alas de pollo amarillas partidas"
    assert product.slug == "alas-de-pollo-amarillas-partidas"
    assert product.url == (
        "https://www.bonarea-online.com/online/producto/"
        "alas-de-pollo-amarillas-partidas/13_5361"
    )
    assert product.pack_size_text == "aprox. 561 g"
    assert product.weight_grams == 561
    assert product.family_name == "Carnes y huevos"
    assert product.is_new is False
    assert product.is_sponsored is False
    assert product.is_variable_weight is False
    assert product.requires_age_check is False
    assert product.variants == ()


def test_a_listing_row_and_a_product_detail_parse_into_the_same_type() -> None:
    row = next(
        item for item in listing()["articles"] if item["identifier"] == "13*5361"
    )
    detail = parse_product(article())
    summary = parse_product(row)

    assert isinstance(summary, BonareaProduct)
    assert summary.id == detail.id
    assert summary.price == detail.price
    assert summary.characteristics == detail.characteristics
    # only the detail carries a breadcrumb and the two html blobs
    assert summary.category_path == ()
    assert summary.nutrition is None
    assert detail.category_path != ()
    assert detail.nutrition is not None


def test_no_article_carries_an_ean() -> None:
    # bonàrea publishes no barcode in the json, the page markup, json-ld or
    # any meta tag, so the field can only ever be None
    products = [parse_product(item) for item in listing()["articles"]]

    assert [product.ean for product in [*products, parse_product(article())]] == [
        None
    ] * (len(products) + 1)


def test_no_article_carries_a_brand() -> None:
    # there is no brand field either: the name is the only place a brand
    # appears, and own-label goods are marked with a badge instead
    product = parse_product(article())

    assert product.brand is None
    assert product.has(Characteristic.OWN_BRAND)


def test_the_breadcrumb_becomes_the_category_path() -> None:
    product = parse_product(article())

    assert product.category_ids == (
        "13*300",
        "13*300*010",
        "13*300*010*010",
        "13*300*010*010*010",
    )
    root, second = product.category_path[:2]
    assert isinstance(root, BonareaCategory)
    assert (root.name, root.level, root.parent_id) == ("Alimentación", 1, None)
    assert (second.level, second.parent_id, second.slug) == (
        2,
        "13*300",
        "carnes-y-huevos",
    )
    # the breadcrumb's application-relative ~/ link is anchored on the site
    assert second.url == (
        "https://www.bonarea-online.com/categorias/carnes-y-huevos/13_300_010"
    )


def test_photos_address_the_image_cdn_by_file_name() -> None:
    product = parse_product(article())

    assert product.photos == (
        BonareaPhoto(
            url="https://images.bonarea.com/13_5361_1.png", file_name="13_5361_1.png"
        ),
    )
    assert product.thumbnail is product.photos[0]
    assert isinstance(product.thumbnail, Photo)


def test_a_photo_resizes_through_the_cdn_query() -> None:
    photo = parse_product(article()).photos[0]

    assert photo.sized(500, 500) == (
        "https://images.bonarea.com/13_5361_1.png?width=500&height=500"
    )


@pytest.mark.parametrize("size", [0, -1, True, 1.5, "500", None])
def test_a_photo_rejects_a_size_that_is_not_a_positive_integer(size: Any) -> None:
    photo = parse_product(article()).photos[0]

    with pytest.raises(ConfigurationError, match=r"width|height"):
        photo.sized(size, 500)
    with pytest.raises(ConfigurationError, match=r"width|height"):
        photo.sized(500, size)


def test_a_search_hit_keeps_the_prefixed_image_name_verbatim() -> None:
    # some images are published under a "P"-prefixed name, which cannot be
    # derived from the id, so the name always comes from the payload
    hits = parse_articles(read_fixture(STORE, "search.json"), label="search")

    assert hits[0].photos[0].file_name == "P13_0066_1.png"


def test_a_product_is_frozen_and_slotted() -> None:
    product = parse_product(article())

    assert not hasattr(product, "__dict__")
    with pytest.raises(FrozenInstanceError):
        product.name = "changed"  # type: ignore[misc]


def test_an_article_without_an_identifier_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="identifier"):
        parse_product({"description": "Alas"})


def test_an_article_without_a_description_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="description"):
        parse_product({"identifier": "13*5361"})


def test_unknown_and_missing_fields_are_ignored_rather_than_fatal() -> None:
    product = parse_product(
        {"identifier": "13_5361", "description": "Alas", "novetatDelFutur": 1}
    )

    assert product.id == "13*5361"
    assert product.price == BonareaPrice()
    assert product.photos == ()
    assert product.characteristics == ()
    assert product.availability.available is None
    assert product.availability.status is None
    assert product.nutrition is None
    assert product.raw_general_info is None


# ---------------------------------------------------------------------- prices


def test_the_unit_price_is_parsed_out_of_its_display_string() -> None:
    # everything price-related except priceToPay is display text with a
    # european decimal comma
    product = parse_product(article())

    assert product.price.amount == Decimal("2.83")
    assert product.price.unit_price == Decimal("5.05")
    assert product.price.unit_price_unit == "kg"
    assert product.price.unit_price_text == "5,05 €/kg"
    assert product.price.selling_unit == "€/u."
    assert product.price.currency == "EUR"


def test_a_litre_unit_price_parses_the_same_way() -> None:
    product = parse_product(article("product_packaged.json"))

    assert product.price.amount == Decimal("6.72")
    assert product.price.unit_price == Decimal("1.12")
    assert product.price.unit_price_unit == "l"
    assert product.pack_size_text == "6 l"


def test_the_reference_price_is_read_from_the_display_string() -> None:
    assert parse_product(article()).price.reference == UnitPrice(
        amount=Decimal("5.05"), unit=Unit.KILOGRAM
    )
    assert parse_product(article("product_packaged.json")).price.reference == (
        UnitPrice(amount=Decimal("1.12"), unit=Unit.LITRE)
    )


@pytest.mark.parametrize(
    ("unit_price", "expected"),
    [
        # every unit seen on october 2026 searches
        ("0,89 €/l", UnitPrice(amount=Decimal("0.89"), unit=Unit.LITRE)),
        ("18,13 €/l.", UnitPrice(amount=Decimal("18.13"), unit=Unit.LITRE)),
        ("4,46 €/kg", UnitPrice(amount=Decimal("4.46"), unit=Unit.KILOGRAM)),
        ("9,5 €/kg", UnitPrice(amount=Decimal("9.50"), unit=Unit.KILOGRAM)),
        ("25,67 €/kg.", UnitPrice(amount=Decimal("25.67"), unit=Unit.KILOGRAM)),
        ("10,35 €/kg..", UnitPrice(amount=Decimal("10.35"), unit=Unit.KILOGRAM)),
        ("0,56 €/k.", UnitPrice(amount=Decimal("0.56"), unit=Unit.KILOGRAM)),
        ("0,22 €/u.", UnitPrice(amount=Decimal("0.22"), unit=Unit.PIECE)),
        ("0,18 €/d.", UnitPrice(amount=Decimal("0.18"), unit=Unit.DOSE)),
        ("0,08 €/m", UnitPrice(amount=Decimal("0.08"), unit=Unit.METRE)),
        ("0,03 €/m.", UnitPrice(amount=Decimal("0.03"), unit=Unit.METRE)),
        # 12,17 € for 200 ml of sun cream: labelled per millilitre, priced per litre
        ("60,85 €/ml.", UnitPrice(amount=Decimal("60.85"), unit=Unit.LITRE)),
        ("", None),
        ("1,00 €/xx", None),
    ],
)
def test_every_unit_price_display_string_is_normalised(
    unit_price: str, expected: UnitPrice | None
) -> None:
    product = parse_product({**article(), "unitPrice": unit_price})

    assert product.price.reference == expected


def test_prices_carry_no_previous_value_tax_or_offer() -> None:
    price = parse_product(article()).price

    assert price.previous is None
    assert price.tax_percentage is None
    assert price.discount_percentage is None
    assert price.valid_from is None
    assert price.valid_until is None
    assert price.is_discounted is False


def test_the_discount_field_is_kept_raw_because_its_meaning_is_unknown() -> None:
    # every article sampled reports discount 0, and nothing in the storefront
    # says whether it is a percentage or an amount, so it is not interpreted
    product = parse_product(article())

    assert product.price.raw_discount == Decimal(0)
    assert product.price.discount_percentage is None


def test_a_price_drop_badge_is_the_only_discount_signal() -> None:
    product = parse_product(
        {
            "identifier": "13*5361",
            "description": "Alas",
            "priceToPay": 2.83,
            "caracteristiques": [{"descripcio": "BAIXADA_PREU"}],
        }
    )

    assert product.price.is_discounted is True
    assert product.price.previous is None
    assert product.has(Characteristic.PRICE_DROP)


# ---------------------------------------------------------------- availability


def test_availability_maps_the_stock_and_order_limits() -> None:
    product = parse_product(article())

    assert product.availability.available is True
    assert product.availability.status == "in_stock"
    assert product.availability.max_stock == 618
    assert product.availability.max_quantity == Decimal(618)
    assert product.availability.min_quantity == Decimal(1)
    assert product.availability.is_published is True
    assert product.availability.grid_buy_allowed is True


def test_an_article_out_of_stock_says_so() -> None:
    row = next(
        item for item in listing()["articles"] if item["identifier"] == "13*5324"
    )
    product = parse_product(row)

    assert product.availability.available is False
    assert product.availability.status == "out_of_stock"
    assert product.availability.max_stock == 0


def test_the_weekday_codes_are_kept_as_the_storefront_writes_them() -> None:
    # the encoding is undocumented, and the field only becomes meaningful
    # once a fulfilment mode is chosen, which a read-only client never does
    product = parse_product(article())

    assert product.availability.days_without_service == ("1",)


# ---------------------------------------------------------------- html blobs


def test_the_general_info_blob_fills_the_prose_fields() -> None:
    product = parse_product(article())

    assert product.description.startswith("Alas de pollo amarillo cortadas en dos")
    assert product.storage == "Mantener entre 0 y 4 °C."
    assert (
        product.usage
        == "Cocinar antes de su consumo. Una vez abierto consumir en 24 horas."
    )
    assert product.origin == "España"


def test_the_extended_info_blob_fills_the_label_fields() -> None:
    product = parse_product(article())

    assert product.legal_name == "Pollo - Alas partidas amarillas"
    assert product.additional_info == "Envasado en atmósfera protectora"
    assert product.operator == (
        "CORPORACIÓN ALIMENTARIA GUISSONA, S.A. TRASPALAU, 8 - 25210 GUISSONA (LLEIDA)"
    )


def test_the_same_sections_are_read_in_catalan() -> None:
    # the headings are localised, so both spellings are recognised
    product = parse_product(article("product_ca.json"))

    assert product.name == "Ales de pollastre grogues partides"
    assert product.storage == "Mantenir entre 0 i 4 ºC"
    assert (
        product.usage
        == "Cuinar abans del seu consum. Un cop obert consumir en 24 hores."
    )
    assert product.origin == "Espanya"
    assert product.legal_name == "Pollastre - Ales partides grogues"
    assert product.additional_info == "Envasat en atmosfera protectora"


def test_a_marketing_link_stays_inside_the_description() -> None:
    # generalInfo carries recipe links; the text is kept and the markup is
    # left on raw_general_info for a caller who wants the href
    product = parse_product(article())

    assert product.description.endswith(
        "+ Receta: Alitas de pollo marinadas con quinoa"
    )
    assert '<a href="https://www.bonarea-online.com/es/shop/recipe/12"' in (
        product.raw_general_info
    )


def test_the_raw_blobs_are_kept_as_a_fallback() -> None:
    product = parse_product(article())

    assert product.raw_general_info.startswith("<strong>DESCRIPCIÓN</strong>")
    assert product.raw_extended_info.startswith("<strong>INFORMACIÓN NUTRICIONAL:")
    assert product.nutrition.raw_html == product.raw_extended_info


def test_the_closing_footnote_is_kept_out_of_the_last_section() -> None:
    # a disclaimer paragraph trails the last heading without one of its own
    product = parse_product(article("product_packaged.json"))

    assert "La información sobre la composición" in product.raw_extended_info
    assert product.legal_name == (
        "Leche desnatada uht sin lactosa, enriquecida en vitaminas A,D,E y ácido fólico"
    )


# ------------------------------------------------------------------- nutrition


def test_nutrition_reads_ingredients_allergens_and_the_table() -> None:
    nutrition = parse_product(article("product_packaged.json")).nutrition

    assert nutrition.ingredients == (
        "LECHE fresca desnatada de vaca, vitaminas A,D,E y ácido fólico."
    )
    assert nutrition.allergens == "Leche y sus derivados"
    assert nutrition.per == "Valor medio por 100 ml de producto"
    assert nutrition.nutri_score is None
    assert len(nutrition.values) == 11
    assert nutrition.values[0].name == "Valor energético"
    assert nutrition.values[0].per_100 == "156 kJ, 37 kcal"
    assert nutrition.values[0].unit == "kJ"
    assert nutrition.values[0].per_serving is None


def test_a_bounded_measurement_survives_verbatim() -> None:
    values = {
        value.name: value
        for value in parse_product(article("product_packaged.json")).nutrition.values
    }

    assert values["Grasas"].per_100 == "< 0,5 g, de las cuales saturadas 0 g"
    assert values["Grasas"].unit == "g"
    assert values["Vitamina A"].per_100 == "120 µg 15 % VRN"
    assert values["Vitamina A"].unit == "µg"


def test_the_catalan_allergen_heading_arrives_as_an_entity() -> None:
    nutrition = parse_product(article("product_packaged_ca.json")).nutrition

    assert "<strong>AL&middot;LÈRGENS:</strong>" in nutrition.raw_html
    assert nutrition.allergens == "Llet i els seus derivats"
    assert nutrition.ingredients == (
        "LLET fresca desnatada de vaca, vitamines A,D,E i àcid fòlic."
    )
    assert nutrition.per == "Valor mitjà per 100 ml de producte"


def test_an_article_without_a_label_has_no_nutrition() -> None:
    # every listing row leaves extendedInfo empty
    assert parse_nutrition("") is None
    assert parse_nutrition(None) is None
    assert parse_nutrition("<strong>DENOMINACIÓN:</strong><p>Pollo</p>") is None


def test_a_nutrition_table_alone_is_enough() -> None:
    nutrition = parse_nutrition(
        "<strong>INFORMACIÓN NUTRICIONAL:</strong><p>Por 100 g:<br>Envase de 1 l<br>"
        "-Sal      0,2  g<br>- <br>-Proteínas 18 g</p>"
    )

    assert nutrition.ingredients is None
    assert nutrition.allergens is None
    # the first line that is not a row introduces the table; later ones are
    # not part of it and are left in the raw html
    assert nutrition.per == "Por 100 g"
    # a blank row is dropped, and a row without column alignment keeps its
    # whole text as the name
    assert [(value.name, value.per_100) for value in nutrition.values] == [
        ("Sal", "0,2 g"),
        ("Proteínas 18 g", None),
    ]


def test_a_repeated_heading_keeps_the_first_and_an_empty_one_is_dropped() -> None:
    product = parse_product(
        {
            "identifier": "13*1",
            "description": "Alas",
            "generalInfo": (
                "<strong>ORIGEN:</strong><p>España</p>"
                "<strong>ORIGEN:</strong><p>Francia</p>"
                "<strong>CONSERVACIÓN</strong><p> </p>"
                "<strong>SECCIÓN DEL FUTURO:</strong><p>algo</p>"
            ),
        }
    )

    assert product.origin == "España"
    assert product.storage is None
    assert product.usage is None
    # an unrecognised heading is not lost: the blob is kept whole
    assert "SECCIÓN DEL FUTURO" in product.raw_general_info


def test_an_image_entry_that_is_not_a_file_name_is_dropped() -> None:
    product = parse_product(
        {
            "identifier": "13*1",
            "description": "Alas",
            "image": ["", "   ", None, 7, " 13_1_1.png "],
        }
    )

    assert [photo.file_name for photo in product.photos] == ["13_1_1.png"]


# ------------------------------------------------------------------- badges


def test_badges_are_catalan_constants_in_both_locales() -> None:
    spanish = parse_product(article())
    catalan = parse_product(article("product_ca.json"))

    assert spanish.characteristics == catalan.characteristics
    assert spanish.characteristics == (
        "GARANTIA_BONAREA",
        "BONAREA",
        "BENESTAR_ANIMAL_AENOR",
        "REFRIGERAT",
        "TRACABILITAT",
    )
    assert spanish.has(Characteristic.CHILLED)
    assert spanish.has("REFRIGERAT")
    assert not spanish.has(Characteristic.FROZEN)


def test_an_unknown_badge_is_kept_as_a_plain_string() -> None:
    product = parse_product(
        {
            "identifier": "13*1",
            "description": "Novetat",
            "caracteristiques": [
                {"descripcio": "NOU_PRODUCTE"},
                {"descripcio": "BADGE_DEL_FUTUR"},
                {"descripcio": ""},
                {},
            ],
        }
    )

    assert product.characteristics == ("NOU_PRODUCTE", "BADGE_DEL_FUTUR")
    assert product.is_new is True
    assert product.has("BADGE_DEL_FUTUR")


def test_every_badge_in_the_enumeration_is_a_catalan_constant() -> None:
    assert Characteristic.NEW == "NOU_PRODUCTE"
    assert Characteristic.PRICE_DROP == "BAIXADA_PREU"
    assert str(Characteristic.GLUTEN_FREE) == "SENSE_GLUTEN"


# -------------------------------------------------------------------- variants


def test_variant_groups_carry_the_articles_they_group() -> None:
    row = next(
        item
        for item in listing("category_root.json")["articles"]
        if item["identifier"] == "13*0089562"
    )
    group = parse_product(row).variants[0]

    assert group.id == "AA*13*0001"
    assert group.product_id == "13*0089562"
    assert group.template == "fotos"
    assert (group.first_label, group.first_kind) == ("Tipo", "FOTO")
    assert (group.second_label, group.second_kind) == (None, None)
    assert [
        (variant.product_id, variant.first_value) for variant in group.variants
    ] == [("13*0089562", "CACAUETE"), ("13*0089561", "CHOCOLATE")]


def test_a_variant_group_is_named_in_the_language_that_was_not_asked_for() -> None:
    # the label and the values follow the request, but the group's own name
    # arrives in the other language; the value is passed through as it comes
    spanish = next(
        item
        for item in listing("category_root.json")["articles"]
        if item["identifier"] == "13*0089562"
    )
    catalan = next(
        item
        for item in read_fixture(STORE, "search.json")["articles"]
        if item["identifier"] == "13*0089562"
    )

    spanish_product = parse_product(spanish)
    catalan_product = parse_product(catalan)
    assert spanish_product.name == "Chocolate grageas M&m's cacahuete"
    assert spanish_product.variants[0].name == "Xocolata dragees M&m's"
    assert catalan_product.name == "Xocolata dragees M&m's cacauet"
    assert catalan_product.variants[0].name == "Chocolate grageas M&m's"


def test_a_name_arrives_html_escaped_and_is_decoded() -> None:
    # names are escaped for the jquery `.html()` call that renders them, so
    # the wire value of one product is "M&amp;m's"
    row = next(
        item
        for item in listing("category_root.json")["articles"]
        if item["identifier"] == "13*0089562"
    )

    assert "M&amp;m's" in row["description"]
    assert parse_product(row).name == "Chocolate grageas M&m's cacahuete"


def test_a_variant_without_an_article_is_dropped() -> None:
    product = parse_product(
        {
            "identifier": "13*1",
            "description": "Grageas",
            "agrupacions": [
                {
                    "agrupacio": "AA*13*0001",
                    "filtres": [{"filtre1": "CACAUET"}, {"codiArticle": "13_2"}],
                }
            ],
        }
    )

    assert product.variants[0].product_id is None
    assert [variant.product_id for variant in product.variants[0].variants] == ["13*2"]


# ------------------------------------------------------------------ categories


def test_the_tree_is_parsed_with_levels_and_parents() -> None:
    tree = parse_category_tree(listing("tree.json")["nivells"])

    assert [(node.id, node.name) for node in tree] == [
        ("13*300", "Alimentación"),
        ("13*320", "Bebidas"),
    ]
    root = tree[0]
    assert (root.level, root.parent_id, root.slug, root.icon) == (
        1,
        None,
        "alimentacion",
        "icon-food",
    )
    # the tree's relative link resolves to the same page a breadcrumb names
    assert root.url == "https://www.bonarea-online.com/categorias/alimentacion/13_300"
    menu = root.children[0]
    assert (menu.id, menu.level, menu.parent_id) == ("13*300*010", 2, "13*300")
    listing_node = menu.children[0]
    assert (listing_node.id, listing_node.level) == ("13*300*010*010", 3)
    assert [child.level for child in listing_node.children] == [4, 4, 4]
    assert listing_node.children[0].parent_id == "13*300*010*010"


def test_the_walk_stops_at_the_listing_level_and_keeps_childless_menus() -> None:
    # level 3 aggregates its level-4 children, so a walk stops there; a
    # level-2 node without children is a listing in its own right
    tree = parse_category_tree(listing("tree.json")["nivells"])

    assert [(node.id, node.level) for node in listing_categories(tree)] == [
        ("13*300*010*010", 3),
        ("13*320*080", 2),
    ]


def test_a_root_without_children_is_walked_itself() -> None:
    tree = parse_category_tree(
        [{"identifier": "13*410", "descripcio": "Material ganadero"}]
    )

    assert [node.id for node in listing_categories(tree)] == ["13*410"]


def test_a_tree_that_is_not_an_array_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="category tree"):
        parse_category_tree({"nivells": []})


def test_a_category_without_an_identity_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="category id"):
        parse_category_tree([{"descripcio": "Aves"}])
    with pytest.raises(InvalidResponseError, match="category name"):
        parse_category_tree([{"identifier": "13*300"}])


# ---------------------------------------------------------------------- search


def test_a_search_page_is_sliced_out_of_the_whole_result_set() -> None:
    payload = read_fixture(STORE, "search.json")

    first = parse_search_result(payload, query="leche", offset=0, page_size=2)
    second = parse_search_result(payload, query="leche", offset=2, page_size=2)
    last = parse_search_result(payload, query="leche", offset=4, page_size=2)

    assert first.total_hits == 5
    assert first.page_size == 2
    assert first.offset == 0
    assert len(first.products) == 2
    assert first.next_cursor == "2"
    assert first.has_more is True
    assert second.next_cursor == "4"
    assert last.next_cursor is None
    assert last.has_more is False
    assert len(last.products) == 1
    assert first.results_url == "/ca/shop/find?searchWords=leche"


def test_an_offset_past_the_end_yields_an_empty_page() -> None:
    result = parse_search_result(
        read_fixture(STORE, "search.json"), query="leche", offset=99, page_size=24
    )

    assert result.products == ()
    assert result.next_cursor is None


def test_a_search_response_without_articles_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="search response"):
        parse_search_result({"success": True}, query="leche", offset=0, page_size=24)
    with pytest.raises(InvalidResponseError, match="listing response"):
        parse_articles({"nivells": []}, label="listing")


# --------------------------------------------------------------- postal codes


def test_postal_codes_are_deduplicated_in_arrival_order() -> None:
    codes = parse_postal_codes(read_fixture(STORE, "postal_codes.json"))

    assert codes[:3] == ("25797", "25691", "25310")
    assert len(codes) == len(set(codes))
    assert all(code.isdigit() for code in codes)


def test_postal_codes_ignore_entries_that_are_not_text() -> None:
    assert parse_postal_codes(["25797", "", None, 25691, " 25797 "]) == ("25797",)


def test_a_postal_code_response_that_is_not_an_array_is_rejected() -> None:
    with pytest.raises(InvalidResponseError, match="postal code"):
        parse_postal_codes({"codes": []})
