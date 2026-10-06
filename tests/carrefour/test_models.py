from __future__ import annotations

import dataclasses
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import (
    ConfigurationError,
    InvalidResponseError,
    Product,
    ProductSummary,
    Unit,
    UnitPrice,
)
from supermercapy._core.html import extract_json_assignment
from supermercapy.carrefour import (
    CarrefourCategory,
    CarrefourPhoto,
    CarrefourProduct,
    CarrefourStore,
    image_url,
    normalise_category_id,
)
from supermercapy.carrefour.models import (
    parse_category_page,
    parse_drives,
    parse_home,
    parse_listing,
    parse_listing_card,
    parse_menu,
    parse_product,
    parse_search_document,
    parse_search_result,
    parse_stores_location,
    parse_suggestions,
)
from tests.conftest import read_fixture

STORE = "carrefour"
STATE_MARKER = "window.__INITIAL_STATE__"


def state_of(name: str) -> Any:
    return extract_json_assignment(read_fixture(STORE, name), STATE_MARKER)


def documents(name: str = "search.json") -> list[Any]:
    return read_fixture(STORE, name)["catalog"]["content"]


# ------------------------------------------------------------------- the model


def test_the_product_model_extends_the_core_one_and_stays_frozen() -> None:
    product = parse_search_document(documents()[0])
    assert isinstance(product, Product)
    assert isinstance(product, ProductSummary)
    assert dataclasses.is_dataclass(product)
    assert not hasattr(product, "__dict__")
    with pytest.raises(FrozenInstanceError):
        product.name = "changed"  # type: ignore[misc]


def test_an_app_price_below_the_shelf_price_is_a_discount() -> None:
    product = parse_product(state_of("product.html"))
    assert product.app_price == Decimal("0.79")
    assert product.price.amount == Decimal("0.84")
    assert product.has_app_discount
    assert not dataclasses.replace(product, app_price=None).has_app_discount
    assert not dataclasses.replace(product, app_price=Decimal("0.84")).has_app_discount


# -------------------------------------------------------------- search shape


def test_a_search_document_carries_numbers_iso_dates_and_a_leaf_category() -> None:
    product = parse_search_document(documents()[0])
    assert product.id == "714713105"
    assert product.ean == "8431876234121"
    assert product.brand == "CARREFOUR"
    assert product.pack_size_text == "1 l."
    assert product.price.amount == Decimal("0.94")
    assert product.price.previous == Decimal("0.99")
    assert product.price.is_discounted
    assert product.price.valid_from == datetime.fromisoformat(
        "2021-09-02T11:55:59.534+02:00"
    )
    # a search document knows only the leaf; ancestors match nothing upstream
    assert product.category_ids == ("cat20093",)
    assert product.availability.available is True
    assert product.sku_id == "4704010000"
    assert product.image_stem == "470401"
    assert product.info_tags[0].message == "Sin lactosa"
    assert product.info_tags[0].colour == "#efdef9"


def test_the_unset_date_sentinel_is_dropped() -> None:
    product = parse_search_document(documents()[1])
    assert product.price.valid_from is None
    assert product.price.valid_until is None
    assert product.price.previous is None
    assert not product.price.is_discounted


def test_a_search_document_never_carries_a_beacon_url() -> None:
    raw = documents()[0]
    assert "tagging" in raw
    product = parse_search_document(raw)
    assert "tagging" not in repr(product)
    assert "api.empathy.co" not in repr(product)


def test_the_catalog_keyed_fields_fall_back_to_any_catalog() -> None:
    raw = dict(documents()[0])
    raw["urls"] = {"cellar": "/supermercado/vino/R-1/p"}
    raw["image_path"] = {"cellar": "https://static.carrefour.es/hd_350x_/a/b_00_1.jpg"}
    product = parse_search_document(raw)
    assert product.slug == "vino"
    assert product.thumbnail is not None


def test_a_document_without_an_id_or_a_name_is_invalid() -> None:
    for broken in ({"display_name": "x"}, {"product_id": "1"}):
        with pytest.raises(InvalidResponseError):
            parse_search_document(broken)


def test_the_page_cursor_is_the_offset_plus_the_rows_returned() -> None:
    data = read_fixture(STORE, "search.json")
    first = parse_search_result(data, query="leche", offset=0, page_size=24)
    assert first.next_cursor == "2"
    assert first.total_hits == 3
    assert not first.truncated
    last = parse_search_result(
        read_fixture(STORE, "search_page2.json"), query="leche", offset=2, page_size=24
    )
    assert last.next_cursor is None
    empty = parse_search_result(
        read_fixture(STORE, "search_empty.json"), query="x", offset=0, page_size=24
    )
    assert empty.products == ()
    assert empty.next_cursor is None


def test_a_page_against_the_offset_ceiling_is_truncated() -> None:
    data = {
        "catalog": {
            "content": [
                {"product_id": str(n), "display_name": "row"} for n in range(24)
            ],
            "numFound": 5000,
        }
    }
    result = parse_search_result(data, query="leche", offset=2490, page_size=24)
    assert result.truncated
    assert result.next_cursor is None
    assert result.offset == 2490


# ------------------------------------------------------------- rendered shapes


def test_a_listing_card_prices_in_localised_text() -> None:
    state = state_of("listing.html")
    card = parse_listing(state).products[0]
    assert card.id == "VC4AECOMM-481229"
    assert card.price.amount == Decimal("7.29")
    assert card.price.previous == Decimal("7.65")
    assert card.price.is_discounted
    assert card.price.unit_price == Decimal("0.81")
    assert card.price.unit_price_text == "0,81 €"
    assert card.price.reference == UnitPrice(amount=Decimal("0.81"), unit=Unit.LITRE)
    assert card.app_price == Decimal("6.99")
    assert card.stock == 33
    assert card.availability.available is True
    assert card.sell_pack_unit == 1
    assert card.slug == "leche-semidesnatada-carrefour-pack-de-9-briks-de-1-l"


def test_promotion_badges_use_day_first_dates() -> None:
    promotion = parse_listing(state_of("listing.html")).products[0].promotions[0]
    assert promotion.description == "XXL Ahorro"
    assert promotion.kind == "promotions"
    assert promotion.starts_at == datetime(2026, 9, 10, tzinfo=UTC)
    assert promotion.ends_at == datetime(2026, 9, 23, tzinfo=UTC)
    assert promotion.id == "/supermercado/xxl-ahorro/7964363137/s"


def test_purchase_limits_survive_as_restrictions() -> None:
    restriction = parse_listing(state_of("listing.html")).products[0].restrictions[0]
    assert restriction.id == "100000185"
    assert restriction.name == "MÁXIMO EN LECHE XXL PACK 9X1L."
    assert restriction.quantity == 54000
    assert restriction.kind == "product-quantity"


def test_an_out_of_stock_card_reports_zero_rather_than_unknown() -> None:
    card = parse_listing(state_of("listing.html")).products[1]
    assert card.stock == 0
    assert card.availability.available is False
    assert card.promotions == ()
    assert card.restrictions == ()


def test_a_card_without_an_id_is_skipped_rather_than_raising() -> None:
    listing = parse_listing(state_of("listing.html"))
    assert len(listing.products) == 3
    with pytest.raises(InvalidResponseError):
        parse_listing_card({"name": "no id"})
    assert parse_listing({}).products == ()


def test_a_product_page_is_the_only_source_of_nutrition() -> None:
    product = parse_product(state_of("product.html"))
    assert product.nutri_score == "B"
    assert product.review_rating is not None
    assert product.review_rating.average == Decimal("4.49")
    assert product.review_rating.count == 91
    nutrition = product.nutrition
    assert nutrition is not None
    # the allergen markup is unescaped rather than handed over as html
    assert nutrition.ingredients == "Leche semidesnatada de vaca"
    assert nutrition.raw_html == "<b>Leche</b> semidesnatada de vaca"
    assert nutrition.allergens == "Leche"
    assert nutrition.per == "100 ml"
    assert nutrition.nutri_score == "B"
    assert [value.name for value in nutrition.values] == [
        "Valor energético (kcal)",
        "Valor energético (kJ)",
        "Grasas (g)",
        "Saturadas (g)",
        "Hidratos de carbono (g)",
        "Azúcares (g)",
        "Proteínas (g)",
        "Sal (g)",
    ]
    assert nutrition.values[0].per_100 == "46"
    assert nutrition.values[0].unit == "Kcal"


def test_a_product_page_reads_its_reference_price_from_the_offer() -> None:
    product = parse_product(state_of("product.html"))
    assert product.price.unit_price_text == "0,84 €"
    assert product.price.reference == UnitPrice(amount=Decimal("0.84"), unit=Unit.LITRE)


def test_the_spanish_labels_fill_the_core_free_text_fields() -> None:
    product = parse_product(state_of("product.html"))
    assert product.legal_name == "Leche UHT semidesnatada"
    assert product.origin == "España"
    assert product.storage == "Lugar fresco y seco"
    assert product.usage == "Conservar en lugar seco y fresco"
    assert [detail.name for detail in product.details][:2] == [
        "Condiciones y/o fecha de consumo una vez abierto el envase",
        "Denominación legal",
    ]


def test_a_product_page_keeps_every_image_rendition_and_the_breadcrumb() -> None:
    product = parse_product(state_of("product.html"))
    assert [photo.kind for photo in product.photos] == [
        "large",
        "medium",
        "thumbnail",
    ]
    assert product.thumbnail is not None
    assert product.thumbnail.kind == "thumbnail"
    # the two synthetic root crumbs carry no category id and are dropped
    assert product.category_ids == ("cat20001", "cat20011", "cat20093")
    assert [node.name for node in product.category_path] == [
        "La Despensa",
        "Lácteos",
        "Leche",
    ]
    assert product.category_path[-1].parent_id == "cat20011"
    assert product.sell_pack_unit == 6
    assert product.image_stem == "231394"
    assert product.brand == "CARREFOUR"


def test_a_page_without_a_product_block_is_invalid() -> None:
    with pytest.raises(InvalidResponseError, match="product block"):
        parse_product({"pdp": {}})


def test_a_product_page_without_extras_stays_readable() -> None:
    product = parse_product(
        {"pdp": {"product": {"product_id": "1", "name": "bare", "brand": "ACME"}}}
    )
    assert product.nutrition is None
    assert product.review_rating is None
    assert product.details == ()
    assert product.photos == ()
    assert product.brand == "ACME"
    assert product.price.amount is None


def test_the_breadcrumb_is_read_from_either_place() -> None:
    crumbs = {
        "items": [
            {"category_id": "cat1", "text": "uno", "url": "/supermercado/uno/cat1/c"}
        ]
    }
    product = parse_product(
        {"pdp": {"product": {"product_id": "1", "name": "x"}}, "breadcrumb": crumbs}
    )
    assert product.category_ids == ("cat1",)


def test_the_home_carousels_come_from_the_cms_in_page_order() -> None:
    sections = parse_home(state_of("home.html"))
    # the empty fourth carousel is skipped
    assert [section.title for section in sections] == [
        "Destacados Supermercado",
        "La Despensa",
        None,
    ]
    first, offers, orphan = sections
    assert {section.layout for section in sections} == {"featuredProducts"}
    assert first.id == "97226c14-df3d-44c2-9dd8-c08449047d80"
    assert first.name == "1_novedades_supermercado"
    assert first.category_ids == ()
    assert [product.id for product in first.products] == [
        "VC4AECOMM-539120",
        "prod820096",
    ]
    assert first.products[0].price.amount == Decimal("4.89")
    assert first.products[1].promotions[0].description == "3x2"
    # the automatic offer carousels name the department they draw from
    assert offers.name == "auto-ofers-despensa"
    assert offers.category_ids == ("cat20001",)
    assert offers.description == "No te pierdas las mejores ofertas"
    # a carousel whose cms document is missing keeps its cards and its id
    assert orphan.layout == "featuredProducts"
    assert orphan.name is None
    assert len(orphan.products) == 1


def test_the_home_page_tolerates_missing_and_malformed_blocks() -> None:
    assert parse_home({}) == ()
    assert parse_home({"cms": {"featured_products": [4, {"products": []}]}}) == ()
    # the shape the home parser read before october 2026 is not a carousel
    assert (
        parse_home({"novedades": {"items": [{"product_id": "1", "name": "x"}]}}) == ()
    )
    section = parse_home(
        {
            "cms": {
                "pageModel": {"content": {"ua": {"title": "  ", "name": 4}}},
                "featured_products": [
                    {
                        "cms_content_id": "a",
                        "products": [{"product_id": "1", "name": "x"}],
                    }
                ],
            }
        }
    )[0]
    assert (section.title, section.name, section.layout) == (
        None,
        None,
        "featuredProducts",
    )


def test_the_menu_answers_the_departments_under_the_food_root() -> None:
    departments = parse_menu(read_fixture(STORE, "menu.json"), "foodRootCategory")
    assert [node.id for node in departments] == ["cat20968591", "cat20002", "cat20001"]
    despensa = departments[2]
    assert despensa.name == "La Despensa"
    assert despensa.level == 1
    # the food root is not a category, so a department has no parent
    assert despensa.parent_id is None
    assert despensa.slug == "la-despensa"
    assert despensa.slug_path == ("la-despensa",)
    assert (
        despensa.url == "https://www.carrefour.es/supermercado/la-despensa/cat20001/c"
    )
    assert despensa.image_url is not None
    assert despensa.image_url.endswith("/cat20001_icono.png")
    assert despensa.children == ()


def test_the_menu_answers_the_aisles_of_one_department() -> None:
    aisles = parse_menu(read_fixture(STORE, "menu_despensa.json"), "cat20001", level=2)
    assert [node.id for node in aisles] == ["cat21078001", "cat20009", "cat33207691"]
    assert {node.parent_id for node in aisles} == {"cat20001"}
    assert {node.level for node in aisles} == {2}
    # the offers node links to a filtered listing rather than a category page
    assert aisles[0].slug_path == ()
    assert aisles[0].url == (
        "https://www.carrefour.es/supermercado/la-despensa-promocion/F-13ji8Z13rjo/c"
    )
    assert aisles[1].slug_path == ("la-despensa", "alimentacion")


def test_the_menu_node_is_found_however_deep_it_is_nested() -> None:
    # the catalan menu starts at the food root, the spanish one above it
    data = {
        "menu": [
            {"id": "cat10060017", "name": "Moda", "childs": [{"id": "cat1"}]},
            {
                "id": "foodRootCategory",
                "name": "Supermercat",
                "childs": [
                    {"id": "cat20001", "name": "El rebost", "url_rel": "/x/cat20001/c"},
                    {"id": "cat9", "name": "  "},
                    {"name": "sense id"},
                    4,
                ],
            },
        ]
    }
    departments = parse_menu(data, "foodRootCategory")
    assert [node.name for node in departments] == ["El rebost"]
    assert parse_menu(read_fixture(STORE, "menu_empty.json"), "cat20093") == ()
    assert parse_menu({}, "foodRootCategory") == ()


def test_a_category_id_is_normalised_to_the_url_spelling() -> None:
    assert normalise_category_id("cat20093") == "cat20093"
    assert normalise_category_id(" 20093 ") == "cat20093"
    for value in ("", "leche", "cat", "cat20093x"):
        with pytest.raises(ConfigurationError, match="cat20093"):
            normalise_category_id(value)


# --------------------------------------------------------------------- stores


def test_the_drive_directory_keeps_group_and_street_parts() -> None:
    stores = parse_drives(read_fixture(STORE, "drives.json"))
    assert [store.id for store in stores] == ["005851", "005290", "0000GP"]
    assert isinstance(stores[0], CarrefourStore)
    assert stores[0].group == "Madrid"
    assert stores[0].address == "Avda. Juan Antonio Samaranch 64"
    assert stores[0].latitude == pytest.approx(40.49302)
    assert stores[2].id == "0000GP"
    assert parse_drives({}) == ()


def test_a_postcode_lookup_splits_its_position_string() -> None:
    stores = parse_stores_location(read_fixture(STORE, "stores_location.json"))
    assert [store.id for store in stores] == ["005D", "005Y"]
    assert stores[0].latitude == pytest.approx(40.5238314)
    assert stores[0].longitude == pytest.approx(-3.8836743)
    assert stores[0].click_and_collect is True
    assert stores[0].hours == "09:00-22:00"
    assert (
        parse_stores_location({"stores": [{"id": "1", "name": "x"}]})[0].latitude
        is None
    )
    assert (
        parse_stores_location(
            {"stores": [{"id": "1", "name": "x", "position": "a , b"}]}
        )[0].latitude
        is None
    )
    assert parse_stores_location({}) == ()


# --------------------------------------------------------------------- images


def test_an_image_url_is_re_rendered_by_swapping_its_width() -> None:
    photo = CarrefourPhoto(
        url="https://static.carrefour.es/hd_350x_/img_pim_food/470401_00_1.jpg"
    )
    assert photo.sized(1500) == (
        "https://static.carrefour.es/hd_1500x_/img_pim_food/470401_00_1.jpg"
    )
    assert photo.sized(777).endswith("hd_777x_/img_pim_food/470401_00_1.jpg")
    for width in (0, -1, True, "350"):
        with pytest.raises(ConfigurationError, match="width"):
            photo.sized(width)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="resizable"):
        CarrefourPhoto(url="https://example.com/a.jpg").sized(350)


def test_an_image_url_is_built_from_the_image_stem() -> None:
    assert image_url("470401", width=350) == (
        "https://static.carrefour.es/hd_350x_/img_pim_food/470401_00_1.jpg"
    )
    assert image_url("470401", width=100, variant="01", index=2).endswith(
        "470401_01_2.jpg"
    )


# ------------------------------------------------------------------- narrowing


def test_the_store_models_narrow_the_core_collections() -> None:
    category = CarrefourCategory(id="cat1", name="uno")
    assert category.children == ()
    assert category.products == ()
    product = CarrefourProduct(id="1", name="uno")
    assert product.restrictions == ()
    assert product.info_tags == ()
    assert product.app_price is None


def test_suggestions_drop_blanks_and_repeats() -> None:
    assert parse_suggestions(read_fixture(STORE, "empathize.json")) == (
        "leche",
        "leche sin lactosa",
    )
    assert parse_suggestions({}) == ()
    assert parse_suggestions({"topTrends": {"content": [{"title": "pan"}]}}) == ("pan",)


def test_rendered_state_survives_braces_inside_strings() -> None:
    state = {"pdp": {"product": {"product_id": "1", "name": "};"}}}
    html = f"<script>{STATE_MARKER} = {json.dumps(state)};</script>"
    assert extract_json_assignment(html, STATE_MARKER) == state
    assert parse_product(state).name == "};"


# ------------------------------------------------------------------ tolerance


def test_a_search_document_without_optional_fields_still_parses() -> None:
    product = parse_search_document({"product_id": "1", "display_name": "bare"})
    assert product.url is None
    assert product.slug is None
    assert product.thumbnail is None
    assert product.category_ids == ()
    assert product.info_tags == ()
    assert product.availability.available is None
    assert product.price.amount is None


def test_a_product_url_that_is_not_a_product_url_has_no_slug() -> None:
    product = parse_search_document(
        {
            "product_id": "1",
            "display_name": "bare",
            "urls": {"food": "/supermercado/la-despensa/cat20001/c"},
        }
    )
    assert product.url == "https://www.carrefour.es/supermercado/la-despensa/cat20001/c"
    assert product.slug is None


def test_an_absolute_url_is_left_alone() -> None:
    product = parse_search_document(
        {
            "product_id": "1",
            "display_name": "bare",
            "urls": {"food": "https://www.carrefour.es/supermercado/x/R-1/p"},
        }
    )
    assert product.url == "https://www.carrefour.es/supermercado/x/R-1/p"
    assert product.slug == "x"


def test_blank_badges_tags_and_limits_are_dropped() -> None:
    card = parse_listing_card(
        {
            "product_id": "1",
            "name": "bare",
            "restrictions": [{}, {"id": "9"}],
            "badge_map": {"promotions": [{"name": "   "}, {"name": "Ahorro"}]},
        }
    )
    assert [limit.id for limit in card.restrictions] == ["9"]
    assert [promotion.description for promotion in card.promotions] == ["Ahorro"]
    assert card.promotions[0].starts_at is None
    assert card.promotions[0].kind == "promotions"
    product = parse_search_document(
        {
            "product_id": "1",
            "display_name": "bare",
            "info_tags": [{"message": " "}, {"colour": "x"}, {"message": "Bio"}],
        }
    )
    assert [tag.message for tag in product.info_tags] == ["Bio"]


def test_a_badge_without_a_type_falls_back_to_its_group_name() -> None:
    card = parse_listing_card(
        {"product_id": "1", "name": "x", "badge_map": {"labels": [{"name": "Nuevo"}]}}
    )
    assert card.promotions[0].kind == "labels"


def test_nutrition_rows_without_a_readable_value_are_skipped() -> None:
    product = parse_product(
        {
            "pdp": {
                "product": {
                    "product_id": "1",
                    "name": "x",
                    "nutrition_info": {
                        "valorEnergetico": {"kilocalorias": {"valor": None}},
                        "grasas": {"valor": "trazas"},
                        "hidratos": {},
                        "sal": {"nombre": "Sal", "valor": "  "},
                        "masInfo": [{"listaInfo": [{"nombre": "x"}, {"valor": "y"}]}],
                    },
                }
            }
        }
    )
    nutrition = product.nutrition
    assert nutrition is not None
    # a value with no number keeps its text and reports no unit
    assert [(value.name, value.per_100, value.unit) for value in nutrition.values] == [
        ("grasas", "trazas", None)
    ]
    assert product.details == ()


def test_a_nutrition_block_with_nothing_readable_is_none() -> None:
    product = parse_product(
        {
            "pdp": {
                "product": {"product_id": "1", "name": "x", "nutrition_info": {"a": 1}}
            }
        }
    )
    assert product.nutrition is None


def test_a_breadcrumb_entry_without_a_category_id_is_dropped() -> None:
    product = parse_product(
        {
            "pdp": {
                "product": {"product_id": "1", "name": "x"},
                "breadcrumb": {
                    "items": [
                        {"category_id": "rootCategory", "text": "Inicio"},
                        {"category_id": "cat1"},
                        {"text": "sin id"},
                        {"category_id": "cat2", "text": "dos"},
                    ]
                },
            }
        }
    )
    assert product.category_ids == ("cat2",)
    assert product.category_path[0].parent_id is None
    assert product.category_path[0].url is None


def test_an_image_entry_without_renditions_is_skipped() -> None:
    product = parse_product(
        {
            "pdp": {
                "product": {
                    "product_id": "1",
                    "name": "x",
                    "images": [{}, {"large": "  "}, {"medium": "https://a/b.jpg"}],
                }
            }
        }
    )
    assert [photo.url for photo in product.photos] == ["https://a/b.jpg"]


def test_a_leaf_page_lists_its_siblings_rather_than_children() -> None:
    leche = parse_category_page(state_of("listing.html"), "cat20093")
    assert leche.name == "Leche"
    assert leche.level == 3
    assert leche.parent_id == "cat20011"
    assert leche.slug_path == ("la-despensa", "lacteos", "leche")
    assert leche.url == (
        "https://www.carrefour.es/supermercado/la-despensa/lacteos/leche/cat20093/c"
    )
    # the second navigation row holds leche itself, so it is the sibling row
    assert leche.children == ()


def test_an_aisle_page_lists_its_children() -> None:
    lacteos = parse_category_page(state_of("category_aisle.html"), "cat20011")
    assert (lacteos.name, lacteos.level, lacteos.parent_id) == (
        "Lácteos",
        2,
        "cat20001",
    )
    assert [child.id for child in lacteos.children] == [
        "cat20093",
        "cat20090",
        "cat20089",
    ]
    leche = lacteos.children[0]
    assert (leche.name, leche.level, leche.parent_id) == ("Leche", 3, "cat20011")
    assert leche.slug_path == ("la-despensa", "lacteos", "leche")
    assert leche.image_url is not None


def test_a_category_page_without_a_breadcrumb_falls_back_to_its_title() -> None:
    state = {
        "category": {"display_name": " Leche "},
        "horizontalNavigation": {
            "secondLevelCategories": {
                "items": [
                    {
                        "id": "cat1",
                        "display_name": "uno",
                        "url": "/supermercado/a/cat1/c",
                    },
                    {"id": "cat2"},
                    4,
                ]
            }
        },
    }
    node = parse_category_page(state, "cat20093")
    assert (node.id, node.name, node.level, node.parent_id) == (
        "cat20093",
        "Leche",
        1,
        None,
    )
    assert [child.id for child in node.children] == ["cat1"]
    assert node.children[0].level == 2
    with pytest.raises(InvalidResponseError, match="cat20093"):
        parse_category_page({"category": {"display_name": " "}}, "cat20093")


def test_a_store_row_without_an_id_or_a_name_is_dropped() -> None:
    assert parse_drives({"groups": [{"sale_points": [{"name": "x"}, {}]}]}) == ()
    assert parse_stores_location({"stores": [{"name": "x"}, {"id": "1"}]}) == ()


def test_a_listing_without_pagination_reports_no_totals() -> None:
    listing = parse_listing({"productCardList": {"results": {"items": []}}})
    assert listing.total_results is None
    assert listing.offset == 0
    assert listing.page_size is None
    assert listing.sort_options == ()


def test_the_store_dataclasses_stay_frozen() -> None:
    store = CarrefourStore(id="1", name="x")
    with pytest.raises(FrozenInstanceError):
        store.name = "y"  # type: ignore[misc]


def test_rows_that_are_not_objects_are_ignored_everywhere() -> None:
    listing = parse_listing(
        {"productCardList": {"results": {"items": [None, "x", {}]}}}
    )
    assert listing.products == ()
    product = parse_product(
        {
            "pdp": {
                "product": {
                    "product_id": "1",
                    "name": "x",
                    "nutrition_info": {
                        "grasas": {"valor": "1.6 g", "listaInfo": [{"valor": None}]}
                    },
                }
            }
        }
    )
    assert product.nutrition is not None
    assert len(product.nutrition.values) == 1
    assert (
        parse_stores_location(
            {"stores": [{"id": "1", "name": "x", "position": "40.5"}]}
        )[0].longitude
        is None
    )


# ----------------------------------------------------------- reference prices


def test_a_search_document_divides_its_price_by_the_conversion_factor() -> None:
    first, second = (parse_search_document(item) for item in documents())
    assert first.price.unit_price is None
    assert first.price.reference == UnitPrice(amount=Decimal("0.94"), unit=Unit.LITRE)
    # the second fixture document sends no unit_conversion_factor
    assert second.price.unit_price_unit == "l"
    assert second.price.reference is None


@pytest.mark.parametrize(
    ("active_price", "measure_unit", "factor", "expected"),
    [
        # documents seen on october 2026 searches
        (1.45, "l", 1.5, UnitPrice(amount=Decimal("0.9667"), unit=Unit.LITRE)),
        (0.29, "l", 0.33, UnitPrice(amount=Decimal("0.8788"), unit=Unit.LITRE)),
        (1.05, "kg", 0.25, UnitPrice(amount=Decimal("4.20"), unit=Unit.KILOGRAM)),
        (5.25, "docena", 2, UnitPrice(amount=Decimal("0.2188"), unit=Unit.PIECE)),
        (5.19, "lavado", 80, UnitPrice(amount=Decimal("0.0649"), unit=Unit.DOSE)),
        (20.9, "ud", 36, UnitPrice(amount=Decimal("0.5806"), unit=Unit.PIECE)),
        (3.69, "m", 50, UnitPrice(amount=Decimal("0.0738"), unit=Unit.METRE)),
        (1.0, "kg", 0, None),
        (1.0, "kg", None, None),
        (1.0, "bandeja", 1, None),
    ],
)
def test_every_measure_unit_of_the_index_is_normalised(
    active_price: float,
    measure_unit: str,
    factor: float | None,
    expected: UnitPrice | None,
) -> None:
    document = {
        "product_id": "1",
        "display_name": "x",
        "active_price": active_price,
        "measure_unit": measure_unit,
        "unit_conversion_factor": factor,
    }
    assert parse_search_document(document).price.reference == expected


def test_a_variable_weight_document_has_no_reference_price() -> None:
    # a 900 g bag of bananas at 2,21 €; its product page says 2,45 €/kg
    document = {
        "product_id": "VC4AECOMM-779025",
        "display_name": "Plátano de Canarias IGP bolsa de 900 g aprox",
        "active_price": 2.21,
        "measure_unit": "kg",
        "unit_conversion_factor": 0.001,
        "average_weight": 900,
        "variable_weight": True,
    }
    product = parse_search_document(document)
    assert product.price.unit_price_unit == "kg"
    assert product.price.reference is None


@pytest.mark.parametrize(
    ("price_per_unit", "measure_unit", "expected"),
    [
        ("2,62 €", "docena", UnitPrice(amount=Decimal("0.2183"), unit=Unit.PIECE)),
        ("2,45 €", "kg", UnitPrice(amount=Decimal("2.45"), unit=Unit.KILOGRAM)),
        ("0,06 €", "lavado", UnitPrice(amount=Decimal("0.06"), unit=Unit.DOSE)),
        (None, "kg", None),
        ("2,45 €", None, None),
    ],
)
def test_a_listing_card_normalises_its_unit_price(
    price_per_unit: str | None, measure_unit: str | None, expected: UnitPrice | None
) -> None:
    card = parse_listing_card(
        {
            "product_id": "1",
            "name": "x",
            "price": "5,25 €",
            "price_per_unit": price_per_unit,
            "measure_unit": measure_unit,
        }
    )
    assert card.price.reference == expected
