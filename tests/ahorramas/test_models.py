"""ahorramás's parsers: grid tiles, product records and the menu."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import pytest

from supermercapy import (
    InvalidResponseError,
    Product,
    ProductSummary,
    Promotion,
    Unit,
    UnitPrice,
)
from supermercapy.ahorramas import (
    AhorramasCategory,
    AhorramasProduct,
    AhorramasSearchResult,
)
from supermercapy.ahorramas.models import (
    absolute_url,
    grid_category,
    index_categories,
    parse_grid,
    parse_menu,
    parse_product,
)
from tests.conftest import read_fixture

STORE = "ahorramas"
SITE = "https://www.ahorramas.com"


def grid(name: str, *, size: int = 24, offset: int = 0) -> AhorramasSearchResult:
    return parse_grid(
        read_fixture(STORE, name), query="q", offset=offset, page_size=size
    )


def gtm(**fields: Any) -> str:
    return quote(json.dumps(fields))


def tile(pid: str = "1", body: str = "", *, tile_class: str = "product-tile") -> str:
    return (
        f'<div class="product" data-pid="{pid}"><div class="{tile_class}" '
        f'data-brand="MARCA" data-category1="Uno" data-category2="">{body}</div></div>'
    )


def footer(**params: str) -> str:
    query = "&amp;".join(f"{key}={value}" for key, value in params.items())
    options = json.dumps(
        {"options": [{"id": "x", "url": f"{SITE}/Search-ShowAjax?{query}"}]}
    ).replace('"', "&quot;")
    return f'<div class="grid-footer" data-sort-options="{options}"></div>'


def record(**product: Any) -> dict[str, Any]:
    return {
        "action": "Product-Variation",
        "product": {"id": "1", "productName": "Uno", **product},
    }


# ------------------------------------------------------------------- the models


def test_the_store_models_extend_the_core_ones_and_stay_frozen() -> None:
    result = grid("search.html")
    row = result.products[0]

    assert isinstance(row, AhorramasProduct)
    assert isinstance(row, Product)
    assert isinstance(row, ProductSummary)
    assert not hasattr(row, "__dict__")
    with pytest.raises(FrozenInstanceError):
        row.name = "changed"  # type: ignore[misc]
    assert isinstance(
        parse_menu(read_fixture(STORE, "menu.html"))[0], AhorramasCategory
    )


# ------------------------------------------------------------------------ grids


def test_a_search_grid_reads_every_tile() -> None:
    result = grid("search.html")

    assert [row.id for row in result.products] == ["98878", "52028", "60203"]
    assert result.query == "q"
    assert result.offset == 0
    assert result.page_size == 24
    assert result.total_hits is None
    # three rows against twenty-four asked for: this was the last page
    assert result.next_cursor is None


def test_a_plain_tile_carries_its_price_unit_price_and_limits() -> None:
    row = grid("search.html").products[0]

    assert row.name == "Agua en botella de 5 litros Bezoya"
    assert row.brand == "BEZOYA"
    assert row.slug == "agua-en-botella-de-5-litros-bezoya"
    assert row.url == f"{SITE}/agua-en-botella-de-5-litros-bezoya-98878.html"
    assert row.thumbnail is not None
    assert row.thumbnail.url.startswith(f"{SITE}/dw/image/v2/")
    assert row.price.amount == Decimal("2.25")
    assert row.price.previous is None
    assert row.price.unit_price == Decimal("0.45")
    assert row.price.unit_price_unit == "LITRO"
    assert row.price.unit_price_text == "0,45€/LITRO"
    assert row.price.reference == UnitPrice(amount=Decimal("0.45"), unit=Unit.LITRE)
    assert not row.price.is_discounted
    assert row.availability.available is True
    assert row.availability.min_quantity == Decimal("1.0")
    assert row.availability.increment == Decimal("1.0")
    assert row.category_names == ("Bebidas", "Agua", "Agua sin gas")
    assert row.category_ids == ()
    assert not row.is_variable_weight
    assert row.average_weight is None
    assert not row.is_sponsored
    assert row.ean is None
    assert row.nutrition is None


def test_a_price_drop_shows_the_old_price_and_a_dated_promotion() -> None:
    row = grid("search.html").products[1]

    assert row.id == "52028"
    # the analytics attribute names the old price, the tile the one to pay
    assert row.price.amount == Decimal("0.48")
    assert row.price.previous == Decimal("0.64")
    assert row.price.is_discounted
    assert row.price.unit_price == Decimal("0.32")
    assert row.promotions == (
        Promotion(
            description="Bajada de precio a 0.48€",
            price=Decimal("0.48"),
            starts_at=datetime(2026, 10, 1, tzinfo=UTC),
            ends_at=datetime(2026, 10, 28, tzinfo=UTC),
        ),
    )


def test_a_multibuy_callout_names_the_quantity_it_needs() -> None:
    (promotion,) = grid("search.html").products[2].promotions

    assert promotion.description == "Comprando 2, la unidad te sale a 0.50€"
    assert promotion.price == Decimal("0.50")
    assert promotion.requires_quantity == 2
    assert promotion.starts_at == datetime(2026, 9, 24, tzinfo=UTC)


def test_produce_sold_by_weight_is_priced_per_kilogram() -> None:
    lamb, chops = grid("listing.html").products

    assert lamb.is_variable_weight
    assert lamb.badges == ("PESO VARIABLE",)
    assert lamb.average_weight == Decimal("2.8")
    assert lamb.price.reference == UnitPrice(
        amount=Decimal("19.99"), unit=Unit.KILOGRAM
    )
    assert lamb.availability.min_quantity == Decimal("2.8")
    assert chops.price.unit_price_unit == "Kg"


def test_a_full_page_moves_the_cursor_by_the_rows_sent() -> None:
    result = grid("search.html", size=3, offset=6)

    assert result.next_cursor == "9"
    assert result.offset == 6


def test_the_grid_reports_the_category_it_served() -> None:
    assert grid_category(read_fixture(STORE, "listing.html")) == "cordero_y_cabrito"
    assert grid_category(read_fixture(STORE, "listing_unknown.html")) is None
    assert grid_category(read_fixture(STORE, "search_empty.html")) is None
    assert grid_category("<div></div>") is None
    assert grid_category('<div data-sort-options="not json"></div>') is None
    assert grid_category(None) is None  # type: ignore[arg-type]


def test_an_empty_grid_has_no_rows_and_no_cursor() -> None:
    result = grid("search_empty.html")

    assert result.products == ()
    assert result.next_cursor is None


def test_a_tile_falls_back_to_its_analytics_and_skips_without_identity() -> None:
    html = (
        tile("", f'<a class="product-pdp-link" data-gtm-layer="{gtm(name="x")}"></a>')
        + tile(
            "",
            '<a class="product-pdp-link" '
            f'data-gtm-layer="{gtm(id="9", name="Nueve", brand="B")}" '
            'href="/otra-pagina/"></a>'
            '<a class="product-pdp-link" href="/x-1.html"></a>',
            tile_class="product-tile citrusAdPrint",
        )
        + '<div class="product" data-pid=\'5\'><div class="product-tile">'
        '<a class="product-pdp-link" data-gtm-layer="not json"></a></div></div>'
        + footer(q="x")
    )

    result = parse_grid(html, query="x", offset=0, page_size=3)

    (row,) = result.products
    assert row.id == "9"
    assert row.name == "Nueve"
    assert row.brand == "MARCA"
    assert row.url == f"{SITE}/otra-pagina/"
    assert row.slug is None
    assert row.is_sponsored
    assert row.category_names == ("Uno",)
    assert row.availability.available is None
    assert row.price.amount is None
    # three tiles were sent, so the page was full
    assert result.next_cursor == "3"


def test_tile_markup_variations_are_tolerated() -> None:
    body = (
        '<div class="product-tile second"></div>'
        '<img class="badge-icon" title="SIN GLUTEN"/><img class="badge-icon" '
        'title="SIN GLUTEN"><img class="badge-icon"><img class="weight-icon '
        'badge-icon" title=" "/><img class="tile-image" src="//cdn/x.jpg"/>'
        '<img class="tile-image" src="/second.jpg"><br/>'
        '<h2 class="product-name-gtm">Uno <h2>y</h2> dos</h2>'
        '<h2 class="product-name-gtm">otro</h2>'
        '<span class="value">sin precio</span>'
        '<span class="sales"><span class="value" content="1.10"></span></span>'
        '<span class="sales"><span class="value" content="9.99"></span></span>'
        '<span class="unit-price-per-unit grey"> </span>'
        '<span class="unit-price-per-unit grey">1,10€/Kg</span>'
        '<span class="promo-price">fuera</span>'
        '<div class="add-to-cart" data-available="maybe" data-hasunitweight="no"'
        ' data-mediumweight="0.5"></div>'
        '<div class="add-to-cart" data-available="false"></div>'
        '<div class="promo-info-label"><div>Oferta <span class="promo-price">'
        '1€</span></div><div class="promo-info-label"></div></div>'
        '<div class="promo-info-label"> </div>'
        '<div class="promo-info-label">Sin cerrar'
    )

    (row,) = parse_grid(tile("7", body), query="", offset=0, page_size=24).products

    assert row.name == "Uno y dos"
    assert row.badges == ("SIN GLUTEN",)
    assert row.is_variable_weight
    assert row.average_weight == Decimal("0.5")
    assert row.thumbnail is not None
    assert row.thumbnail.url == "https://cdn/x.jpg"
    assert row.price.amount == Decimal("1.10")
    assert row.price.unit_price_text == "1,10€/Kg"
    assert row.availability.available is None
    assert [promotion.description for promotion in row.promotions] == [
        "Oferta 1€",
        "Sin cerrar",
    ]
    assert row.promotions[0].price == Decimal("1")
    assert row.promotions[0].starts_at is None


def test_a_callout_left_open_or_without_a_price_still_reads() -> None:
    html = (
        '<div class="product" data-pid="8"><div class="product-tile">'
        '<h2 class="product-name-gtm">Ocho</h2><div class="promo-info-label">'
        'Abierta <span class="promo-price"> </span>'
    )

    (row,) = parse_grid(html, query="", offset=0, page_size=24).products

    assert row.promotions == (Promotion(description="Abierta"),)


def test_drained_weight_is_priced_per_kilogram() -> None:
    body = (
        '<h2 class="product-name-gtm">Garbanzo cocido Alipende 400g</h2>'
        '<span class="unit-price-per-unit grey">2,00€/KG.PESO ESC</span>'
    )

    (row,) = parse_grid(tile("7", body), query="", offset=0, page_size=24).products

    assert row.price.unit_price == Decimal("2.00")
    assert row.price.unit_price_unit == "KG.PESO ESC"
    assert row.price.reference == UnitPrice(amount=Decimal("2.00"), unit=Unit.KILOGRAM)


def test_unit_text_that_is_not_a_unit_price_is_kept_as_text() -> None:
    body = (
        '<h2 class="product-name-gtm">Uno</h2>'
        '<span class="unit-price-per-unit grey">consultar</span>'
    )

    (row,) = parse_grid(tile("7", body), query="", offset=0, page_size=24).products

    assert row.price.unit_price is None
    assert row.price.unit_price_unit is None
    assert row.price.unit_price_text == "consultar"


def test_a_grid_without_tiles_or_markup_is_empty() -> None:
    assert parse_grid("", query="", offset=0, page_size=24).products == ()
    assert parse_grid(None, query="", offset=0, page_size=24).products == ()  # type: ignore[arg-type]


# ---------------------------------------------------------------------- records


def test_a_record_reads_prices_promotions_and_attributes() -> None:
    product = parse_product(read_fixture(STORE, "product.json"))

    assert product.id == "52028"
    assert product.name == "Agua mineral natural Nestlé Aquarel botella 1,5 l"
    assert product.brand == "AQUAREL"
    assert (
        product.url
        == f"{SITE}/agua-mineral-natural-nestle-aquarel-botella-15-l-52028.html"
    )
    assert product.slug == "agua-mineral-natural-nestle-aquarel-botella-15-l"
    assert product.pack_size_text == "1,50 LITRO"
    assert product.price.amount == Decimal("0.48")
    assert product.price.previous == Decimal("0.64")
    assert product.price.discount_percentage == Decimal("25")
    assert product.price.unit_price == Decimal("0.32")
    assert product.price.unit_price_unit == "LITRO"
    assert product.price.unit_price_text is None
    assert product.price.reference == UnitPrice(amount=Decimal("0.32"), unit=Unit.LITRE)
    assert product.promotions == (
        Promotion(
            id="576486",
            kind="Bajada de precio",
            description="Bajada de precio a 0.48€",
            price=Decimal("0.48"),
            starts_at=datetime(2026, 10, 1, tzinfo=UTC),
            ends_at=datetime(2026, 10, 28, tzinfo=UTC),
        ),
    )
    assert product.availability.available is True
    assert product.availability.status == "En stock"
    assert product.availability.max_quantity == Decimal("100")
    assert product.category_ids == ("agua",)
    assert product.category_names == ("Bebidas", "Agua")
    assert len(product.photos) == 3
    assert product.photos[0].url.startswith(f"{SITE}/on/demandware.static/")
    assert product.photos[0].kind == "large"
    assert product.thumbnail is not None
    assert "sw=140" in product.thumbnail.url
    assert product.description is not None
    assert product.description.startswith("Compra Agua Aquarel Nestlé 1,5l online")
    assert ("Proveedor comercial", "AQUAREL") in product.general_info
    assert product.nutrition is None
    assert product.ean is None


def test_a_record_sold_by_weight_is_priced_per_kilogram() -> None:
    product = parse_product(read_fixture(STORE, "product_weighed.json"))

    assert product.is_variable_weight
    assert product.average_weight == Decimal("0.19")
    assert product.price.amount == Decimal("2.85")
    # the record's own measure disagrees with the tile, so the price stands
    assert product.price.reference == UnitPrice(
        amount=Decimal("2.85"), unit=Unit.KILOGRAM
    )
    assert product.availability.increment == Decimal("0.19")
    assert product.pack_size_text is None
    assert product.general_info == (("Código", "75248"),)


def test_a_record_without_identity_is_invalid() -> None:
    with pytest.raises(InvalidResponseError, match="without an id"):
        parse_product(read_fixture(STORE, "product_missing.json"))


def test_a_sparse_record_still_parses() -> None:
    data = record(
        akeneo_codigoMedida="KILO",
        akeneo_unidadMedida={},
        discountPercent="0",
        price={"sales": {"value": 1}, "list": {"value": 1}},
        images={
            "large": [{"url": ""}, {"url": "/a.jpg"}, {"url": "/a.jpg"}],
            "small": [],
        },
        availability={"messages": [None, " "]},
        promotions=[
            {"enabled": False, "details": "apagada"},
            {
                "calloutMsg": "Solo texto",
                "calloutDate": "(Válido del 30/02/26 al 3/3/2026)",
            },
            {"details": " "},
        ],
        attrGeneralInfo=[
            {"name": "netWeight", "label": "Peso Neto", "value": "<p>1 kg</p>"},
            {
                "name": "addedSugar",
                "type": "boolean",
                "label": "Sin azúcar",
                "value": True,
            },
            {
                "name": "veganProduct",
                "type": "boolean",
                "label": "Vegano",
                "value": False,
            },
            {"name": "sinNombre", "value": "x"},
            {"value": "huérfano"},
            {"name": "pigBreed", "label": "Raza", "value": {}},
        ],
        attrProductDecription=[
            {"name": "otro", "value": "no"},
            {"name": "productDescription", "value": "<p> </p>"},
        ],
        longDescription="DESCRIPCIÓN",
        attrAkeneoBadges=[{"value": True, "title": " "}, {"value": None, "title": "X"}],
    )

    product = parse_product(data)

    assert product.price.unit_price is None
    assert product.price.unit_price_unit is None
    assert not product.price.is_discounted
    assert product.price.discount_percentage is None
    assert [photo.url for photo in product.photos] == [f"{SITE}/a.jpg"]
    assert product.thumbnail is None
    assert product.availability.status is None
    assert product.url is None
    assert product.slug is None
    assert product.category_ids == ()
    (promotion,) = product.promotions
    assert promotion.description == "Solo texto"
    assert promotion.starts_at is None
    assert promotion.ends_at == datetime(2026, 3, 3, tzinfo=UTC)
    assert product.pack_size_text == "1 kg"
    assert product.general_info == (
        ("Peso Neto", "1 kg"),
        ("Sin azúcar", "true"),
        ("sinNombre", "x"),
    )
    assert product.description == "DESCRIPCIÓN"
    assert product.badges == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (" ", None),
        ("//cdn/x", "https://cdn/x"),
        ("/x", f"{SITE}/x"),
        ("https://otra/x", "https://otra/x"),
    ],
)
def test_absolute_urls(value: str | None, expected: str | None) -> None:
    assert absolute_url(value) == expected


# ------------------------------------------------------------------------- menu


def test_the_menu_is_rebuilt_from_its_paths_with_the_menus_own_ids() -> None:
    roots = parse_menu(read_fixture(STORE, "menu.html"))

    assert [root.id for root in roots] == ["ofertas", "frescos", "limpieza", "hogar"]
    offers, fresh, cleaning, home = roots
    assert offers.name == "OFERTAS"
    assert offers.children[0].id == "ofertas_lacteos"
    (butcher,) = fresh.children
    assert [leaf.id for leaf in butcher.children] == ["pollo", "cordero_y_cabrito"]
    lamb = butcher.children[1]
    assert lamb.name == "Cordero y lechal"
    assert lamb.path == "/frescos/carniceria/cordero-y-lechal/"
    assert lamb.slug == "cordero-y-lechal"
    assert lamb.url == f"{SITE}/frescos/carniceria/cordero-y-lechal/"
    assert lamb.parent_id == "carniceria"
    assert lamb.level == 3
    # a "ver todo" link of the cleaning branch points into the home branch,
    # and the home branch keeps its own id
    assert cleaning.children[0].id == "limpieza_ambientadores"
    assert home.children[0].id == "ambientadores"
    assert home.children[0].children[0].id == "ambientadores_spray"


def test_the_index_holds_every_node() -> None:
    index = index_categories(parse_menu(read_fixture(STORE, "menu.html")))

    assert len(index) == 11
    assert index["cordero_y_cabrito"].parent_id == "carniceria"


def test_menu_links_without_text_or_parent_are_named_or_dropped() -> None:
    html = (
        '<a class="nav-link" id="raiz" href="/raiz/"></a>'
        '<a class="dropdown-link" id="huerfana" href="/sola/huerfana/">Huérfana</a>'
        '<a class="dropdown-link" href="/raiz/sin-id/">Sin id</a>'
        '<a class="dropdown-link" id="hoja" href="/raiz/hoja/">Hoja</a>'
        '<a class="dropdown-link" id="hoja_dos" href="/raiz/hoja/">Repetida</a>'
        '<a class="link" id="otra" href="/raiz/otra/">Otra</a><p>suelto</p>'
    )

    (root,) = parse_menu(html)

    assert root.name == "raiz"
    (leaf,) = root.children
    assert (leaf.id, leaf.name) == ("hoja", "Hoja")
    assert parse_menu(None) == ()  # type: ignore[arg-type]
