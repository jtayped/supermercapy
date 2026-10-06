"""the tapestry parsers, driven through eroski's models and fixtures."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import (
    InvalidResponseError,
    Product,
    ProductSummary,
    Promotion,
    SearchResult,
    Unit,
    UnitPrice,
)
from supermercapy._platforms.tapestry import (
    TapestryCategory,
    TapestryProduct,
    absolute_url,
    category_path,
    index_categories,
    page_inits,
    parse_listing,
    parse_menu,
    parse_product,
    tile_markup,
)
from supermercapy.eroski import EroskiCategory, EroskiProduct, EroskiSearchResult
from tests.conftest import read_fixture

STORE = "eroski"
SITE = "https://supermercado.eroski.es"


def listing(data: Any, *, page: int = 0, query: str = "") -> EroskiSearchResult:
    return parse_listing(
        data,
        product_type=EroskiProduct,
        result_type=EroskiSearchResult,
        site_url=SITE,
        query=query,
        page=page,
    )


def product(html: str, product_id: str = "3430469") -> EroskiProduct:
    return parse_product(
        html,
        product_type=EroskiProduct,
        category_type=EroskiCategory,
        site_url=SITE,
        product_id=product_id,
        url=f"{SITE}/es/productdetail/{product_id}-producto/",
    )


def menu(html: str) -> tuple[EroskiCategory, ...]:
    return parse_menu(html, category_type=EroskiCategory, site_url=SITE)


def metrics(item_id: str, name: str = "Producto", **extra: Any) -> str:
    """return one tile's ga4 attribute, html-escaped as the storefront writes it."""

    item = {"item_id": item_id, "item_name": name, **extra}
    event = {"event": "select_item", "ecommerce": {"items": [item]}}
    return json.dumps(event).replace('"', "&quot;")


def tile(item_id: str = "1", body: str = "", **extra: Any) -> str:
    return (
        '<div class="col border-0 product-item-lineal item-type-1">'
        f'<a class="product-title-link" data-metrics="{metrics(item_id, **extra)}"'
        f' href="{SITE}:443/es/productdetail/{item_id}-producto-de-prueba/">x</a>'
        f"{body}</div>"
    )


def fragment(content: str, inits: list[Any] | None = None) -> dict[str, Any]:
    return {"content": content, "_tapestry": {"inits": inits or []}}


def page(body: str, inits: list[Any] | None = None) -> str:
    script = (
        '<script>require(["t5/core/pageinit"], function(pi) { pi([], '
        f"{json.dumps(inits or [])}); }});</script>"
    )
    return f"<html><body>{body}{script}</body></html>"


# ------------------------------------------------------------------- the models


def test_the_store_models_extend_the_core_ones_and_stay_frozen() -> None:
    result = listing(read_fixture(STORE, "search.json"))
    row = result.products[0]

    assert isinstance(result, SearchResult)
    assert isinstance(row, EroskiProduct)
    assert isinstance(row, TapestryProduct)
    assert isinstance(row, Product)
    assert isinstance(row, ProductSummary)
    assert not hasattr(row, "__dict__")
    with pytest.raises(FrozenInstanceError):
        row.name = "changed"  # type: ignore[misc]
    category = menu(read_fixture(STORE, "menu.html"))[0]
    assert isinstance(category, EroskiCategory)
    assert isinstance(category, TapestryCategory)


# --------------------------------------------------------------------- listings


def test_a_search_page_reads_every_tile_and_skips_the_ad_slots() -> None:
    result = listing(read_fixture(STORE, "search.json"), query="aceite oliva")

    assert [row.id for row in result.products] == ["3854569", "15923279", "3430469"]
    assert result.query == "aceite oliva"
    assert result.page == 0
    assert result.page_size == 20
    assert result.total_hits is None
    assert result.next_cursor == "1"


def test_a_tile_carries_its_unit_price_and_the_lowered_price_badge() -> None:
    row = listing(read_fixture(STORE, "search.json")).products[0]

    assert row.name == "Aceite de oliva virgen extra EROSKI, garrafa 3 litros"
    assert row.brand == "EROSKI"
    assert row.slug == "aceite-de-oliva-virgen-extra-eroski-garrafa-3-litros"
    assert row.url == (
        f"{SITE}/es/productdetail/3854569-"
        "aceite-de-oliva-virgen-extra-eroski-garrafa-3-litros/"
    )
    assert row.thumbnail is not None
    assert row.thumbnail.url == f"{SITE}/images/3854569.jpg"
    assert row.thumbnail.kind == "thumbnail"
    assert row.price.amount == Decimal("12.80")
    assert row.price.previous is None
    assert row.price.unit_price == Decimal("4.27")
    assert row.price.unit_price_unit == "1 LITRO"
    assert row.price.unit_price_text == "1 LITRO A 4,27 €"
    assert row.price.reference == UnitPrice(amount=Decimal("4.27"), unit=Unit.LITRE)
    assert not row.price.is_discounted
    assert row.is_lowered_price
    assert row.promotions == ()
    assert row.shop_id == "157"
    assert not row.is_marketplace
    assert row.availability.max_quantity == Decimal("100")
    assert row.availability.min_quantity == Decimal("1")
    assert row.availability.increment == Decimal("1")
    assert row.ean is None


def test_a_multibuy_badge_becomes_a_promotion() -> None:
    row = listing(read_fixture(STORE, "search.json")).products[1]

    assert row.promotions == (
        Promotion(kind="2x1x70", description="2ª unidad -70%", requires_quantity=2),
    )
    assert not row.price.is_discounted
    assert row.category_ids == ("2059806", "2059988", "2059989")


def test_a_struck_through_price_and_its_percentage_mark_a_discount() -> None:
    row = listing(read_fixture(STORE, "search.json")).products[2]

    assert row.price.amount == Decimal("13.95")
    assert row.price.previous == Decimal("19.75")
    assert row.price.discount_percentage == Decimal("29")
    assert row.price.is_discounted
    assert row.promotions == ()


def test_a_fresh_listing_marks_produce_sold_by_weight() -> None:
    rows = listing(read_fixture(STORE, "listing.json")).products

    assert [row.id for row in rows] == ["12069175", "18702746"]
    assert all(row.is_variable_weight for row in rows)
    banana, kiwi = rows
    assert banana.price.previous == Decimal("2.30")
    assert banana.price.discount_percentage == Decimal("13")
    assert kiwi.price.reference == UnitPrice(amount=Decimal("7.70"), unit=Unit.KILOGRAM)
    assert kiwi.availability.max_quantity == Decimal("25")


def test_marketplace_tiles_keep_their_letters_their_seller_and_their_shop() -> None:
    rows = listing(read_fixture(STORE, "listing_marketplace.json")).products

    console, phone = rows
    assert console.id == "MP96290"
    assert console.is_marketplace
    assert console.shop_id == "1983"
    assert console.thumbnail is not None
    assert console.thumbnail.url == f"{SITE}/imagesMarket/MP96290_1.webp"
    assert console.category_ids == ("6000420", "6000567", "6000568", "6000573")
    assert phone.id == "26746354"
    assert not phone.is_marketplace
    assert phone.shop_id == "2126"


def test_the_empty_page_ends_the_listing() -> None:
    result = listing(read_fixture(STORE, "search_empty.json"), page=3)

    assert result.products == ()
    assert result.next_cursor is None
    assert result.page == 3


def test_a_page_the_storefront_calls_finished_ends_the_listing() -> None:
    data = fragment(tile("1"), ["pages/supermarket:finishPagination"])

    result = listing(data, page=4)

    assert [row.id for row in result.products] == ["1"]
    assert result.next_cursor is None


def test_tiles_without_identity_are_skipped_and_options_are_optional() -> None:
    content = (
        '<div class="col product-item-lineal"><a class="product-title-link">x</a></div>'
        '<div class="col product-item-lineal">'
        '<a class="product-title-link" data-metrics="not json">x</a></div>'
        '<div class="col product-item-lineal"><a class="product-title-link" '
        'data-metrics="{&quot;ecommerce&quot;:{&quot;items&quot;:[]}}">x</a></div>'
        + tile("7", item_brand="", item_category=12, price=1.5)
    )
    inits: list[Any] = [
        "t5/core/zone",
        [],
        ["common/button/productListItemAddComponent:init"],
        ["common/button/productListItemAddComponent:init", {"itemId": "x"}],
    ]

    (row,) = listing(fragment(content, inits)).products

    assert row.id == "7"
    assert row.brand is None
    assert row.category_ids == ("12",)
    # no visible price, so the analytics figure stands in
    assert row.price.amount == Decimal("1.5")
    assert row.availability.max_quantity is None
    assert row.shop_id is None
    assert row.thumbnail is None


def test_purchase_options_with_missing_numbers_stay_unknown() -> None:
    inits = [
        [
            "common/button/productListItemAddComponent:init",
            {
                "productRef": "7",
                "maximumQuantity": 12,
                "isWeightOptionsAvailable": True,
            },
        ]
    ]

    (row,) = listing(fragment(tile("7"), inits)).products

    assert row.availability.max_quantity == Decimal("12")
    assert row.availability.min_quantity is None
    assert row.availability.increment is None
    assert row.is_variable_weight


def test_a_tile_link_that_is_not_a_product_url_gives_no_slug() -> None:
    content = (
        '<div class="col product-item-lineal"><a class="product-title-link" '
        f'data-metrics="{metrics("8")}" href="/es/otra/">x</a></div>'
        '<div class="col product-item-lineal"><a class="product-title-link" '
        f'data-metrics="{metrics("9")}" href="/es/productdetail/9-/">x</a></div>'
    )

    first, second = listing(fragment(content)).products

    assert first.url == f"{SITE}/es/otra/"
    assert first.slug is None
    assert second.slug is None


def test_an_offer_badge_without_a_code_is_still_a_promotion() -> None:
    body = (
        '<div class="product-offer"><span class="partner-price">'
        "<span>Precio</span> <span>especial</span></span></div>"
        '<div class="product-offer o_9x9x9"></div>'
    )

    (row,) = listing(fragment(tile("5", body))).products

    assert row.promotions == (Promotion(description="Precio especial"),)


def test_a_percentage_badge_without_a_number_sets_no_percentage() -> None:
    body = (
        '<div class="product-offer"><span class="partner-price '
        'product-offer-only-percent"><span>oferta</span></span></div>'
    )

    (row,) = listing(fragment(tile("5", body))).products

    assert row.price.discount_percentage is None
    assert not row.price.is_discounted


def test_unit_text_that_does_not_read_as_a_price_is_kept_as_text_only() -> None:
    body = '<p class="quantity-text">precio por definir</p>'

    (row,) = listing(fragment(tile("5", body))).products

    assert row.price.unit_price is None
    assert row.price.unit_price_unit is None
    assert row.price.unit_price_text == "precio por definir"
    assert row.price.reference is None


def test_a_roll_is_one_piece() -> None:
    body = '<p class="quantity-text"> 1 ROLLO A  0,30 € </p>'

    (row,) = listing(fragment(tile("5", body))).products

    assert row.price.reference == UnitPrice(amount=Decimal("0.30"), unit=Unit.PIECE)


def test_tile_markup_tolerates_nothing_to_split() -> None:
    assert tile_markup("") == []
    assert tile_markup(None) == []  # type: ignore[arg-type]
    assert tile_markup("<p>no tiles</p>") == []


def test_a_listing_without_content_or_inits_is_empty() -> None:
    result = listing({})

    assert result.products == ()
    assert result.next_cursor is None


# -------------------------------------------------------------------- products


def test_a_product_page_reads_the_event_the_markup_and_the_characteristics() -> None:
    item = product(read_fixture(STORE, "product.html"))

    assert item.id == "3430469"
    assert item.name == "Aceite de oliva virgen extra HOJIBLANCA, garrafa 3 litros"
    assert item.brand == "HOJIBLANCA"
    assert item.url == f"{SITE}/es/productdetail/3430469-producto/"
    assert item.slug is None
    assert item.price.amount == Decimal("13.95")
    assert item.price.previous == Decimal("19.75")
    assert item.price.discount_percentage == Decimal("29")
    assert item.price.reference == UnitPrice(amount=Decimal("4.65"), unit=Unit.LITRE)
    assert item.category_ids == ("2059806", "2059988", "2059989")
    assert [node.id for node in item.category_path] == [
        "2059806",
        "2059988",
        "2059989",
    ]
    leaf = item.category_path[-1]
    assert isinstance(leaf, EroskiCategory)
    assert leaf.name == "Aceite de oliva virgen"
    assert leaf.parent_id == "2059988"
    assert leaf.level == 3
    assert leaf.path == (
        "2059806-alimentacion/2059988-aceite-vinagre-sal-harina-y-pan-rallado/"
        "2059989-aceite-de-oliva-virgen"
    )
    assert item.category_path[0].parent_id is None
    assert [photo.url for photo in item.photos] == [
        f"{SITE}/images/3430469_x.jpg",
        f"{SITE}/images/3430469_2_x.jpg",
    ]
    assert {photo.kind for photo in item.photos} == {"large"}
    assert item.thumbnail is not None
    assert item.thumbnail.url == f"{SITE}/images/3430469.jpg"
    assert item.shop_id == "157"
    assert item.availability.max_quantity == Decimal("100")


def test_a_product_page_carries_the_nutrition_table_and_the_maker() -> None:
    item = product(read_fixture(STORE, "product.html"))

    nutrition = item.nutrition
    assert nutrition is not None
    assert nutrition.ingredients == "Aceite de Oliva Virgen Extra"
    assert nutrition.per == "100 mililitros"
    energy = nutrition.values[0]
    assert energy.name == "Energía"
    assert energy.per_100 == "900"
    assert energy.per_serving is None
    assert energy.unit == "kilocaloría it (international table)"
    assert [value.name for value in nutrition.values][-1] == "Sal"
    assert item.storage == "Mantener en lugar fresco y seco."
    assert item.manufacturer == "DEOLEO GLOBAL, S.A.U."
    assert item.manufacturer_address == "Ctra. N.IV. Km.388-14610 Alcolea (Córdoba)"
    assert item.alcohol_percentage is None
    assert [title for title, _ in item.features] == [
        "Ingredientes",
        "Información Nutricional",
        "Fabricante",
        "Condiciones de conservación",
    ]
    assert dict(item.features)["Información Nutricional"].startswith(
        "Cantidad 100 mililitros\nEnergía 900"
    )


def test_a_wine_page_reads_its_strength_and_its_price_drop() -> None:
    item = product(read_fixture(STORE, "product_wine.html"), "7056534")

    assert item.id == "7056534"
    assert item.alcohol_percentage == Decimal("12.5")
    assert item.is_lowered_price
    assert item.price.amount == Decimal("2.90")
    assert item.price.reference == UnitPrice(amount=Decimal("3.87"), unit=Unit.LITRE)
    # this page's breadcrumb stops at the home link and its event names no
    # category, so the product is filed nowhere
    assert item.category_path == ()
    assert item.category_ids == ()


def test_the_heading_and_the_requested_id_stand_in_for_a_missing_event() -> None:
    html = page(
        '<h1 class="description-title">Producto suelto</h1><h1>otro</h1>'
        '<div class="product-thumbnails"><img src="//images/5.jpg"><img src=" ">'
        "</div>"
        '<div class="product-thumbnails"><img src="/images/6.jpg"></div>'
        '<div class="m__breadcrumb__path"><a href="https://supermercado.eroski.es">'
        'Inicio</a><a href="/es/supermercado/12-raiz/">Raíz</a>'
        '<a href="/es/supermercado/12-raiz/13-hoja/"></a></div>'
        '<div class="m__breadcrumb__path"><a href="/es/supermercado/99-x/">X</a></div>'
    )

    item = product(html, "5")

    assert item.id == "5"
    assert item.name == "Producto suelto"
    assert item.brand is None
    assert item.url == f"{SITE}/es/productdetail/5-producto/"
    assert item.price.amount is None
    assert item.thumbnail is not None
    assert item.thumbnail.url == "https://images/5.jpg"
    assert [photo.url for photo in item.photos] == ["https://images/5.jpg"]
    assert [node.name for node in item.category_path] == ["Raíz", "13-hoja"]
    assert item.category_ids == ("12", "13")
    assert item.nutrition is None
    assert item.features == ()


def test_a_page_with_neither_event_nor_heading_is_invalid() -> None:
    with pytest.raises(InvalidResponseError, match="carries no product"):
        product(page("<p>nada</p>"), "5")


def test_the_event_of_the_page_wins_over_other_tracking_calls() -> None:
    inits = [
        ["common/tracking:init"],
        ["common/tracking:init", 5],
        ["common/tracking:init", "not json"],
        ["common/tracking:init", json.dumps({"event": "page_view_gtm"})],
        ["common/tracking:init", json.dumps({"event": "view_item", "ecommerce": {}})],
        [
            "common/tracking:init",
            json.dumps(
                {
                    "event": "view_item",
                    "ecommerce": {
                        "items": [{"item_id": 77, "item_name": "Uno", "price": 2}]
                    },
                }
            ),
        ],
    ]

    item = product(page("", inits), "5")

    assert item.id == "77"
    assert item.name == "Uno"
    assert item.price.amount == Decimal("2")


def test_characteristic_boxes_are_read_whatever_they_hold() -> None:
    boxes = (
        '<div class="feature feature-list"><span class="title">Tabla</span>'
        '<div class="feature"><span class="title">Dentro</span></div>'
        "<ul><li>Cantidad <span>1 ración</span></li><li>Energía <span>trazas</span>"
        "</li><li>Sal <span></span></li><li> </li></ul></div>"
        '<div class="feature feature-list"><span class="title">Vacía</span></div>'
        '<div class="feature feature-company"><span class="title">Fabricante</span>'
        '<p class="text"><strong>Nombre</strong></p><p class="text">Solo nombre</p>'
        "</div>"
        '<div class="feature feature-alcoholic"><span class="title">Grado</span>'
        '<p class="text">sin alcohol</p></div>'
        '<div class="feature feature-text-preservation"><span class="title"></span>'
        '<p class="text"><span class="title">x</span>Fresco</p></div>'
        '<div class="feature feature-other"><p class="text">sin título</p></div>'
    )
    html = page(f'<h1 class="description-title">Caja</h1>{boxes}')

    item = product(html, "5")

    nutrition = item.nutrition
    assert nutrition is not None
    assert nutrition.ingredients is None
    assert nutrition.per == "1 ración"
    energy, salt = nutrition.values
    assert energy.per_serving == "trazas"
    assert energy.per_100 is None
    assert energy.unit is None
    assert salt.per_serving is None
    assert item.manufacturer == "Solo nombre"
    assert item.manufacturer_address is None
    assert item.alcohol_percentage is None
    assert item.storage == "xFresco"
    assert dict(item.features) == {
        "Tabla": "Cantidad 1 ración\nEnergía trazas\nSal",
        "Vacía": "",
        "Fabricante": "Nombre\nSolo nombre",
        "Grado": "sin alcohol",
        # a blank title takes the next one inside the box
        "x": "xFresco",
    }


def test_nested_markup_inside_a_price_and_a_heading_is_read_whole() -> None:
    html = page(
        '<h1 class="description-title">Uno <h1>y</h1> dos</h1>'
        '<span class="offer-now"><span>1</span>,25</span>'
        '<p class="quantity-text"><p>1 KILO</p> A 2,50 €</p>'
        '<span class="partner-price o_3x2x100"><span> 3x2 </span></span>'
        '<img id="main-product-image" src="/images/5.jpg"><img alt="sin fuente">'
        '<img src="/assets/weight-icon.jpg"/><br/>'
    )

    item = product(html, "5")

    assert item.name == "Uno y dos"
    assert item.price.amount == Decimal("1.25")
    assert item.price.unit_price_text == "1 KILO A 2,50 €"
    assert item.promotions == (
        Promotion(kind="3x2x100", description="3x2", requires_quantity=3),
    )
    assert item.thumbnail is not None
    assert item.thumbnail.url == f"{SITE}/images/5.jpg"
    assert item.is_variable_weight


# ----------------------------------------------------------------------- inits


def test_page_inits_read_the_second_argument_of_the_page_init_call() -> None:
    assert page_inits(page("", [["a", 1], "b"])) == [["a", 1], "b"]
    assert page_inits(None) == []  # type: ignore[arg-type]
    assert page_inits("<html></html>") == []
    assert page_inits('require(["t5/core/pageinit"]);') == []
    assert page_inits('"t5/core/pageinit" pi([] [])') == []
    assert page_inits('"t5/core/pageinit" pi([], {"a": 1})') == []
    assert page_inits('"t5/core/pageinit" pi([], [broken') == []


# ------------------------------------------------------------------------ menu


def test_the_menu_is_rebuilt_from_the_category_paths() -> None:
    roots = menu(read_fixture(STORE, "menu.html"))

    assert [root.id for root in roots] == ["2059806", "2059698", "6000510", "2060401"]
    food = roots[0]
    assert food.name == "Alimentación"
    assert food.level == 1
    assert food.parent_id is None
    assert food.slug == "alimentacion"
    assert food.path == "2059806-alimentacion"
    assert food.url == f"{SITE}/es/supermercado/2059806-alimentacion/"
    (milk,) = food.children
    assert milk.name == "Leche, batidos y bebidas vegetales"
    assert [leaf.id for leaf in milk.children] == ["2059808", "2059815"]
    assert milk.children[0].parent_id == "2059807"
    assert milk.children[0].level == 3


def test_the_see_all_entries_never_rename_a_node() -> None:
    roots = menu(read_fixture(STORE, "menu.html"))

    names = [node.name for node in index_categories(roots).values()]
    assert not any(name.startswith("Ver todo") for name in names)


def test_a_node_filed_under_two_parents_appears_under_both() -> None:
    roots = menu(read_fixture(STORE, "menu.html"))
    beauty = roots[3]

    facial, makeup = beauty.children
    assert facial.children[0].id == makeup.children[0].id == "2060403"
    assert facial.children[0].parent_id == "2060402"
    assert makeup.children[0].parent_id == "4000096"
    # the index keeps the first placement
    assert index_categories(roots)["2060403"].parent_id == "2060402"


def test_the_menu_reaches_the_fourth_level() -> None:
    roots = menu(read_fixture(STORE, "menu.html"))

    heating = roots[2].children[0].children[0]
    assert [leaf.level for leaf in heating.children] == [4, 4]
    assert len(index_categories(roots)) == 17


def test_a_link_without_text_or_parent_is_named_by_its_path_or_dropped() -> None:
    html = (
        '<a href="/es/supermercado/1-raiz/"></a><a>sin destino</a>'
        '<a href="/es/supermercado/1-raiz/">Raíz</a>'
        '<a href="/es/supermercado/2-sola/3-huerfana/">Huérfana</a>'
        '<a href="/es/supermercado/1-raiz/4-hoja/"><a href="/es/otra/">Otra</a>'
        "<p>suelto</p>"
    )

    (root,) = menu(html)

    assert root.name == "Raíz"
    (leaf,) = root.children
    assert leaf.name == "4-hoja"
    assert menu(None) == ()  # type: ignore[arg-type]


# --------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("  ", None),
        ("/es/", f"{SITE}/es/"),
        ("//cdn.example/x.jpg", "https://cdn.example/x.jpg"),
        (f"{SITE}:443/es/a//b/?q=1", f"{SITE}/es/a/b/?q=1"),
        ("https://example.com", "https://example.com/"),
    ],
)
def test_absolute_urls_drop_the_port_and_doubled_slashes(
    value: str | None, expected: str | None
) -> None:
    assert absolute_url(SITE, value) == expected


def test_a_category_path_is_read_only_from_a_category_url() -> None:
    assert category_path(None) is None
    assert category_path(f"{SITE}/es/supermercado/contacto/") is None
    assert category_path(f"{SITE}/ca/supermercado/1-a/2-b/") == "1-a/2-b"


def test_a_category_can_carry_its_products_through_replace() -> None:
    root = menu(read_fixture(STORE, "menu.html"))[0]
    row = listing(read_fixture(STORE, "search.json")).products[0]

    filled = dataclasses.replace(root, products=(row,))

    assert filled.products == (row,)
    assert filled.children == root.children
