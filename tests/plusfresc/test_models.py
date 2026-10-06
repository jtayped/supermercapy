from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import InvalidResponseError, Language, Unit, UnitPrice
from supermercapy.plusfresc import (
    NUTRITION_UNITS,
    ImageSize,
    PlusfrescPhoto,
    PlusfrescPromotion,
    TextType,
    resolve_center_id,
    text_in,
    texts_of,
)
from supermercapy.plusfresc.models import (
    parse_category,
    parse_center,
    parse_characteristic,
    parse_format,
    parse_nutrition,
    parse_pickup_point,
    parse_product,
    parse_search_result,
    parse_unit,
)
from tests.conftest import read_fixture

STORE = "plusfresc"
IMAGES = "https://compra.plusfresc.cat/ImatgesProductes"
SITE = "https://compra.plusfresc.cat"


@pytest.fixture
def listing() -> list[Any]:
    rows: list[Any] = read_fixture(STORE, "listing.json")
    return rows


@pytest.fixture
def offers() -> list[Any]:
    rows: list[Any] = read_fixture(STORE, "offers.json")
    return rows


@pytest.fixture
def detail() -> dict[str, Any]:
    data: dict[str, Any] = read_fixture(STORE, "product.json")
    return data


# --------------------------------------------------------------- text helpers


def test_texts_are_collected_per_language_and_type() -> None:
    texts = [
        {"lang": "ca", "order": 1, "text": "Olis", "type": 1},
        {"lang": "es", "order": 1, "text": "Aceites", "type": 1},
        {"lang": "ca", "order": 1, "text": "", "type": 7},
        {"lang": "ca", "order": 1, "text": "later", "type": 1},
        "not an object",
    ]

    assert texts_of(texts, TextType.CATEGORY_NAME) == {"ca": "Olis", "es": "Aceites"}
    assert texts_of(texts, TextType.SLUG) == {}
    assert texts_of("not an array", TextType.CATEGORY_NAME) == {}


def test_text_in_falls_back_to_whichever_language_exists() -> None:
    assert text_in({"ca": "Olis", "es": "Aceites"}, Language.SPANISH) == "Aceites"
    assert text_in({"ca": "Olis", "es": "Aceites"}, "ca") == "Olis"
    assert text_in({"es": "Aceites"}, Language.CATALAN) == "Aceites"
    assert text_in({}, Language.CATALAN) is None


# ------------------------------------------------------------------- products


def test_a_listing_row_parses_into_a_full_summary(listing: list[Any]) -> None:
    product = parse_product(listing[0])

    assert product.id == "000105"
    assert product.item_id == "000105"
    assert product.placement_id == "01010101000105"
    assert product.name == "Oli d'oliva verge extra GERMANOR, 750 ml"
    assert product.name_ca == product.name
    assert product.name_es == "Aceite de oliva virgen extra GERMANOR, 750 ml"
    assert product.brand == "GERMANOR"
    assert product.unit_measure == "L"
    assert product.order == 114
    assert product.availability.available is True
    assert product.has_format is False
    assert product.formats == ()
    assert product.promotions == ()
    assert product.nutrition is None


def test_the_barcode_is_always_none_because_the_storefront_has_none(
    listing: list[Any], detail: dict[str, Any]
) -> None:
    assert parse_product(listing[0]).ean is None
    assert parse_product(detail).ean is None


def test_the_two_fixed_renditions_become_the_thumbnail_and_the_photo(
    listing: list[Any],
) -> None:
    product = parse_product(listing[0])
    stem = "000105_Oli_d_oliva_verge_extra_GERMANOR_ampolla_750_ml"

    assert product.thumbnail is not None
    assert product.thumbnail.url == f"{IMAGES}/{stem}.jpg"
    assert product.thumbnail.kind == "icon"
    assert [photo.url for photo in product.photos] == [
        f"{IMAGES}/{stem}_gran.jpg",
        f"{IMAGES}/{stem}.jpg",
    ]


def test_prices_come_from_integer_cents_without_touching_a_float(
    listing: list[Any],
) -> None:
    plain = parse_product(listing[0]).price
    reduced = parse_product(listing[1]).price

    assert plain.amount == Decimal("8.49")
    assert plain.previous is None
    assert plain.is_discounted is False
    assert plain.discount_percentage is None
    assert reduced.amount == Decimal("4.49")
    assert reduced.previous == Decimal("5.99")
    assert reduced.is_discounted is True
    assert reduced.discount_percentage == Decimal("25.04")
    assert reduced.valid_until == datetime(2026, 9, 14, tzinfo=UTC)


def test_a_reduced_price_equal_to_the_listed_one_is_not_a_discount(
    listing: list[Any],
) -> None:
    row = {**listing[0], "new_value_cents": listing[0]["value_cents"]}
    price = parse_product(row).price

    assert price.is_discounted is False
    assert price.previous is None
    assert price.amount == Decimal("8.49")


def test_a_markdown_becomes_a_promotion_without_a_campaign_id(
    offers: list[Any],
) -> None:
    promotion = parse_product(offers[0]).promotions[0]

    assert promotion.id is None
    assert promotion.kind == "markdown"
    assert promotion.strapline_ca == "Oferta a preu reduït!"
    assert promotion.strapline_es == "¡Oferta a precio reducido!"
    assert promotion.description == "Oferta a preu reduït!"
    assert promotion.requires_quantity is None
    assert promotion.combinable_with == ()
    assert promotion.band_url == f"{IMAGES}/OfferType_5637145854.png"
    assert promotion.chip_color == "250;193;21;255"


def test_a_multibuy_keeps_its_campaign_id_and_combinable_items(
    offers: list[Any],
) -> None:
    promotion = parse_product(offers[2]).promotions[0]

    assert promotion.id == "CategoMxN_34041"
    assert promotion.kind == "multibuy"
    assert promotion.requires_quantity == 2
    assert promotion.combinable_with == ("003231", "003232")
    assert promotion.price == Decimal("3.74")


def test_a_bundle_quantity_is_not_read(offers: list[Any]) -> None:
    # a "CategoXXX_" bundle sends 1000, 2000 or 0 for the same item, which
    # matches nothing the storefront shows
    row = dict(offers[2])
    row["promo_id"] = "CategoXXX_00312"
    row["qty_required"] = 2000

    promotion = parse_product(row).promotions[0]

    assert promotion.id == "CategoXXX_00312"
    assert promotion.kind == "multibuy"
    assert promotion.requires_quantity is None


def test_dates_are_read_as_day_month_year_not_iso(offers: list[Any]) -> None:
    promotion = parse_product(offers[0]).promotions[0]

    assert promotion.end_date == date(2026, 9, 14)
    assert promotion.ends_at == datetime(2026, 9, 14, tzinfo=UTC)


def test_the_unused_parent_category_placeholder_is_dropped(
    offers: list[Any], listing: list[Any]
) -> None:
    # promo pseudo-categories send the literal "Field not used" as the parent.
    assert offers[0]["category_id"] == "Field not used"
    assert parse_product(offers[0]).category_ids == ("Oferta2",)
    assert parse_product(listing[0]).category_ids == ("010101", "01010101")


def test_characteristic_ids_are_upper_cased_but_the_raw_spelling_is_kept(
    listing: list[Any],
) -> None:
    row = listing[0]
    dirty = {
        **row,
        "characteristics": [
            {**row["characteristics"][0], "id": "LOL_si", "embedded_icon_url": "LOL"}
        ],
    }
    product = parse_product(dirty)

    badge = product.characteristics[0]
    assert badge.id == "LOL_SI"
    assert badge.raw_id == "LOL_si"
    assert badge.icon_key == "LOL"
    assert badge.label_ca == "PRODUCTE LOCAL"
    assert badge.label_es == "PRODUCTO LOCAL"
    assert product.characteristic("lol_si") is badge
    assert product.characteristic("ECO_SI") is None


def test_service_formats_and_their_options_are_kept(detail: dict[str, Any]) -> None:
    rows: list[Any] = read_fixture(STORE, "search.json")
    product = parse_product(rows[0])

    assert product.has_format is True
    fmt = product.formats[0]
    assert fmt.format_id == "Pa6-1L"
    assert fmt.order == 1
    assert fmt.title_ca == "Format de Servei"
    assert fmt.title_es == "Formato de Servicio"
    assert [option.option_id for option in fmt.options] == ["Unitat", "Zpack"]
    pack = fmt.options[1]
    assert pack.description_ca == "Pack (6 unitats x 1l.)"
    assert pack.min_value_mili == 1000
    assert pack.max_value_mili == 0
    assert pack.interval_mili == 0
    assert pack.percentage_mili == 6000
    assert parse_product(detail).formats == ()


def test_a_detail_envelope_adds_the_sheet_to_the_same_row(
    detail: dict[str, Any],
) -> None:
    product = parse_product(detail)

    assert product.id == "002530"
    assert product.description == "Surtido Camprodon 560g"
    assert product.pack_size_text == "560"
    assert product.label is None
    assert product.additional_data is None
    assert product.nutrition is not None
    assert product.nutrition.net_weight == "560"
    assert product.nutrition.ration_value == "100"
    assert product.nutrition.per == "100"
    assert product.nutrition.has_ingredients is True
    assert product.nutrition.has_nutritionals is True
    assert product.nutrition.has_allergens is True
    assert product.nutrition.extended is None


def test_nutrition_rows_map_the_coded_units_they_are_known_for(
    detail: dict[str, Any],
) -> None:
    nutrition = parse_product(detail).nutrition
    assert nutrition is not None

    energy, kilojoules, fat = nutrition.values[:3]
    assert (energy.name, energy.unit_code, energy.unit) == (
        "Valor energètic",
        "E14",
        "kcal",
    )
    assert kilojoules.unit == "kJ"
    assert fat.unit == "g"
    assert energy.per_100 == "472"
    assert energy.per_serving == "472"
    assert energy.per_product is None
    assert energy.per_ingest is None
    assert set(NUTRITION_UNITS) == {"E14", "KJO", "GR"}


def test_an_unknown_nutrition_unit_keeps_its_code_and_no_label(
    detail: dict[str, Any],
) -> None:
    payload = {
        **detail,
        "nutritionals": [
            {"nutritionalname": "Fibra", "per_100": "3", "nutritionalunit": "ZZZ"},
            {"nutritionalname": "  ", "per_100": "1"},
        ],
    }
    nutrition = parse_nutrition(payload)
    assert nutrition is not None

    assert len(nutrition.values) == 1
    assert nutrition.values[0].unit_code == "ZZZ"
    assert nutrition.values[0].unit is None


def test_allergens_are_kept_as_rows_and_flattened_into_one_line(
    detail: dict[str, Any],
) -> None:
    nutrition = parse_product(detail).nutrition
    assert nutrition is not None

    assert nutrition.allergen_list[0].name == "Ous"
    assert nutrition.allergen_list[0].description == "Conté"
    assert nutrition.allergens is not None
    assert nutrition.allergens.startswith("Conté: Ous, Llet")


def test_allergens_without_a_description_are_listed_plainly() -> None:
    payload = {
        "filedetails": {},
        "allergens": [
            {"alergensname": "Llet", "alergendescription": ""},
            {"alergensname": "  "},
            {"alergensname": "Soja", "alergendescription": "Pot contenir"},
        ],
    }
    nutrition = parse_nutrition(payload)
    assert nutrition is not None

    assert nutrition.allergens == "Llet; Pot contenir: Soja"


def test_a_listing_row_carries_no_sheet_at_all(listing: list[Any]) -> None:
    assert parse_nutrition(listing[0]) is None
    assert parse_nutrition({"filedetails": {"desc": "x"}}) is None


def test_a_product_without_an_item_id_is_unusable(listing: list[Any]) -> None:
    row = {key: value for key, value in listing[0].items() if key != "item_id"}

    with pytest.raises(InvalidResponseError, match="item id"):
        parse_product(row)


def test_a_product_without_a_name_is_unusable(listing: list[Any]) -> None:
    row = {**listing[0], "texts": []}

    with pytest.raises(InvalidResponseError, match="product name"):
        parse_product(row)


def test_missing_optional_fields_never_raise() -> None:
    product = parse_product(
        {
            "item_id": "000001",
            "texts": [{"lang": "ca", "text": "Alguna cosa", "type": 4}],
        }
    )

    assert product.id == "000001"
    assert product.brand is None
    assert product.thumbnail is None
    assert product.photos == ()
    assert product.price.amount is None
    assert product.availability.available is None
    assert product.category_ids == ()
    assert product.promotions == ()
    assert product.formats == ()
    assert product.characteristics == ()


def test_malformed_format_and_characteristic_entries_are_ignored() -> None:
    assert parse_characteristic({"texts": []}) is None
    assert parse_characteristic("nonsense") is None
    assert parse_format({"title": []}) is None
    parsed = parse_format(
        {"format_id": "F1", "options": [{"option_id": ""}, {"option_id": "Unitat"}]}
    )
    assert parsed is not None
    assert [option.option_id for option in parsed.options] == ["Unitat"]
    assert parsed.title_ca is None


# ----------------------------------------------------------------- categories


def test_the_tree_nests_every_node_and_records_its_parent() -> None:
    tree = read_fixture(STORE, "tree.json")
    root = parse_category(tree["category"])

    assert root.id == "Root"
    assert root.name == "ARREL"
    assert root.parent_id is None
    assert root.level == 0
    assert root.is_visible is False
    food = next(child for child in root.children if child.id == "01")
    assert food.name == "Alimentació"
    assert food.name_es == "Alimentación"
    assert food.parent_id == "Root"
    assert food.level == 1
    assert food.order == 2
    assert food.product_count == 2854
    assert food.leaf_count == 2854
    assert food.image_url == f"{SITE}/ImatgesProductes/01_Alimentacio.png"
    assert food.children[0].parent_id == "01"
    assert food.children[0].id == "0101"


def test_a_promo_pseudo_category_falls_back_to_its_headline() -> None:
    tree = read_fixture(STORE, "tree.json")
    root = parse_category(tree["category"])
    promos = next(child for child in root.children if child.id == "PromoHighlight")
    bundle = promos.children[0]

    assert bundle.id == "CategoXXX_00308"
    # the display name really is empty upstream; the headline stands in.
    assert bundle.name_ca is None
    assert bundle.name == "PLUSPACK APERITIU"
    assert bundle.description_ca is not None
    assert bundle.description_ca.startswith("PLUSPACK APERITIU\nPack vi")
    assert bundle.description_es is not None
    assert bundle.description_es.startswith("PLUSPACK APERITIVO")
    assert bundle.banner_url == f"{SITE}/promos/5637170327.jpg"
    assert bundle.promo_subtype == "PromoCru01"
    assert bundle.promo_type == 0
    assert bundle.batch_price == Decimal("9.99")


def test_a_category_with_neither_name_nor_headline_falls_back_to_its_id() -> None:
    category = parse_category({"id": "99", "texts": []})

    assert category.name == "99"
    assert category.children == ()
    assert category.is_visible is True
    assert category.banner_url is None
    assert category.image_url is None


def test_a_category_without_an_id_is_unusable() -> None:
    with pytest.raises(InvalidResponseError, match="category id"):
        parse_category({"texts": []})


def test_the_category_language_follows_the_request() -> None:
    tree = read_fixture(STORE, "tree.json")
    root = parse_category(tree["category"], language=Language.SPANISH)
    food = next(child for child in root.children if child.id == "01")

    assert food.name == "Alimentación"


# --------------------------------------------------------------------- paging


def test_a_page_is_carved_out_of_the_rows_already_in_hand(listing: list[Any]) -> None:
    first = parse_search_result(list(listing), query="oli", offset=0, page_size=2)
    second = parse_search_result(list(listing), query="oli", offset=2, page_size=2)
    past_the_end = parse_search_result(
        list(listing), query="oli", offset=9, page_size=2
    )

    assert first.row_count == 3
    assert first.total_hits == 3
    assert len(first.products) == 2
    assert first.next_cursor == "2"
    assert first.has_more is True
    assert len(second.products) == 1
    assert second.next_cursor is None
    assert past_the_end.products == ()
    assert past_the_end.next_cursor is None


def test_exactly_one_hundred_rows_means_the_answer_was_cut_short() -> None:
    rows: list[Any] = read_fixture(STORE, "search_capped.json")
    assert len(rows) == 100

    capped = parse_search_result(rows, query="llet", offset=0, page_size=100)
    short = parse_search_result(rows[:99], query="llet", offset=0, page_size=100)

    assert capped.truncated is True
    assert capped.total_hits == 100
    assert short.truncated is False
    assert short.total_hits == 99


# ---------------------------------------------------------------- photo sizes


def test_a_photo_switches_between_the_two_renditions() -> None:
    small = PlusfrescPhoto(url=f"{IMAGES}/000105_Oli.jpg")
    large = PlusfrescPhoto(url=f"{IMAGES}/000105_Oli_gran.jpg")

    assert small.sized(ImageSize.LARGE) == large.url
    assert small.sized("large") == large.url
    assert large.sized("small") == small.url
    assert large.sized(ImageSize.LARGE) == large.url
    assert small.sized("small") == small.url


def test_a_photo_with_no_filename_extension_is_returned_unchanged() -> None:
    photo = PlusfrescPhoto(url="https://example.invalid/images/000105")

    assert photo.sized("large") == photo.url
    assert PlusfrescPhoto(url="no-slash-or-dot").sized("small") == "no-slash-or-dot"


def test_an_unknown_rendition_is_rejected() -> None:
    with pytest.raises(ValueError, match="size must be one of"):
        PlusfrescPhoto(url=f"{IMAGES}/000105_Oli.jpg").sized("1600x1600")


@pytest.mark.parametrize(
    ("chip_color", "expected"),
    [
        ("250;193;21;255", (250, 193, 21, 255)),
        ("250;193;21", None),
        ("250;193;21;xx", None),
        (None, None),
    ],
)
def test_a_chip_colour_is_read_as_rgba_bytes(
    chip_color: str | None, expected: tuple[int, int, int, int] | None
) -> None:
    assert PlusfrescPromotion(chip_color=chip_color).rgba == expected


# -------------------------------------------------------------------- centers


def test_a_locker_id_resolves_to_the_center_that_prepares_its_orders() -> None:
    assert resolve_center_id("127899871") == 12
    assert resolve_center_id("37899871") == 3
    assert resolve_center_id(12) == 12
    assert resolve_center_id(" 110 ") == 110


@pytest.mark.parametrize("value", [None, True, 1.5, "", "abc", "12a", [], "789987"])
def test_an_unusable_center_id_resolves_to_nothing(value: Any) -> None:
    assert resolve_center_id(value) is None


def test_preparation_centers_keep_both_spellings_of_their_name() -> None:
    centers = [parse_center(item) for item in read_fixture(STORE, "centers.json")]
    tarrega = next(
        center for center in centers if center is not None and center.id == "36"
    )

    assert tarrega.name == "Tàrrega"
    assert tarrega.name_ca == "Tàrrega"
    assert tarrega.name_es == "Tàrrega"
    assert tarrega.center_id == 36
    assert tarrega.kind == "center"
    assert parse_center({"street": "nowhere"}) is None


def test_a_pickup_point_splits_its_three_line_postal_block() -> None:
    points = [
        parse_pickup_point(item) for item in read_fixture(STORE, "pickup_points.json")
    ]
    store = points[0]
    locker = points[-1]
    assert store is not None
    assert locker is not None

    assert store.id == "3"
    assert store.name == "VIA AUGUSTA"
    assert store.kind == "store"
    assert store.address == "Via Augusta, 188"
    assert store.postal_code == "08021"
    assert store.city == "BARCELONA"
    assert store.province == "Barcelona"
    assert store.is_locker is False
    assert store.pickup_code == "3"
    assert locker.kind == "locker"
    assert locker.is_locker is True
    assert locker.id == "12"
    assert locker.pickup_code == "127899871"


def test_a_pickup_point_without_a_usable_id_or_address_degrades_quietly() -> None:
    assert parse_pickup_point({"centername": "X"}) is None
    bare = parse_pickup_point({"centerid": "12"})
    assert bare is not None
    assert bare.name == "12"
    assert bare.address is None
    assert bare.postal_code is None
    assert bare.city is None
    assert bare.province is None
    unnumbered = parse_pickup_point(
        {"centerid": "12", "addressing": "Somewhere\nno postcode here"}
    )
    assert unnumbered is not None
    assert unnumbered.postal_code is None


def test_units_need_both_a_code_and_a_name() -> None:
    units = [parse_unit(item) for item in read_fixture(STORE, "units.json")]

    assert [unit.code for unit in units if unit is not None][:2] == ["Do", "Dot"]
    assert units[0] is not None
    assert units[0].language == "ca"
    assert parse_unit({"codi": "Kg"}) is None
    assert parse_unit({"text": "Quilo"}) is None


def test_the_reference_price_of_the_fixture_is_per_kilogram() -> None:
    product = parse_product(read_fixture(STORE, "product.json"))

    assert product.price.unit_price == Decimal("17.76")
    assert product.price.unit_price_unit == "Kg"
    assert product.price.reference == UnitPrice(
        amount=Decimal("17.76"), unit=Unit.KILOGRAM
    )


@pytest.mark.parametrize(
    ("value_x_unit", "unit_measure", "expected"),
    [
        # every unit_measure seen on october 2026 searches, and the units
        # endpoint's metre
        (117, "L", UnitPrice(amount=Decimal("1.17"), unit=Unit.LITRE)),
        (193, "Kg", UnitPrice(amount=Decimal("1.93"), unit=Unit.KILOGRAM)),
        # six eggs at 3,45 €
        (690, "Dot", UnitPrice(amount=Decimal("0.575"), unit=Unit.PIECE)),
        (24, "Do", UnitPrice(amount=Decimal("0.24"), unit=Unit.DOSE)),
        (19, "Un", UnitPrice(amount=Decimal("0.19"), unit=Unit.PIECE)),
        (7, "M", UnitPrice(amount=Decimal("0.07"), unit=Unit.METRE)),
        (100, "Xx", None),
        (100, "", None),
        (None, "Kg", None),
    ],
)
def test_every_unit_measure_is_normalised(
    value_x_unit: int | None, unit_measure: str, expected: UnitPrice | None
) -> None:
    product = parse_product(
        {
            "item_id": "000001",
            "texts": [{"lang": "ca", "order": 1, "text": "x", "type": 4}],
            "value_cents": 100,
            "value_x_unit": value_x_unit,
            "unit_measure": unit_measure,
        }
    )

    assert product.price.reference == expected
