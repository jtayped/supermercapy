"""caprabo's fixtures through the shared tapestry parsers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from supermercapy import Unit, UnitPrice
from supermercapy._platforms.tapestry import (
    TapestryProduct,
    index_categories,
    parse_listing,
    parse_menu,
    parse_product,
)
from supermercapy.caprabo import CapraboCategory, CapraboProduct, CapraboSearchResult
from tests.conftest import read_fixture

STORE = "caprabo"
SITE = "https://www.capraboacasa.com"


def listing(data: Any) -> CapraboSearchResult:
    return parse_listing(
        data,
        product_type=CapraboProduct,
        result_type=CapraboSearchResult,
        site_url=SITE,
        query="leche",
        page=0,
    )


def test_a_caprabo_search_carries_its_own_regional_milk() -> None:
    result = listing(read_fixture(STORE, "search.json"))

    assert isinstance(result, CapraboSearchResult)
    catalan, asturian, glass = result.products
    assert isinstance(catalan, CapraboProduct)
    assert isinstance(catalan, TapestryProduct)
    assert catalan.id == "18581678"
    assert catalan.name == "Leche semidesnatada de Cataluña EROSKI, brik 1 litro"
    assert catalan.price.amount == Decimal("0.99")
    assert catalan.is_lowered_price
    assert catalan.shop_id == "8284"
    assert catalan.url is not None
    assert catalan.url.startswith(f"{SITE}/es/productdetail/18581678-")
    # caprabo sells this brik in sixes
    assert catalan.availability.min_quantity == Decimal("6")
    assert catalan.availability.increment == Decimal("6")
    assert asturian.price.previous == Decimal("1.17")
    assert asturian.price.is_discounted
    assert glass.price.reference == UnitPrice(amount=Decimal("1.23"), unit=Unit.LITRE)
    assert result.next_cursor == "1"


def test_a_caprabo_category_listing_keeps_paging() -> None:
    result = listing(read_fixture(STORE, "listing.json"))

    assert [row.id for row in result.products] == ["152710", "469213"]
    assert result.products[1].availability.min_quantity == Decimal("4")
    assert result.next_cursor == "1"


def test_the_caprabo_menu_shares_eroski_category_ids() -> None:
    roots = parse_menu(
        read_fixture(STORE, "menu.html"), category_type=CapraboCategory, site_url=SITE
    )

    assert [root.id for root in roots] == ["2059806", "2059698", "2060401"]
    assert isinstance(roots[0], CapraboCategory)
    assert roots[0].url == f"{SITE}/es/supermercado/2059806-alimentacion/"
    assert set(index_categories(roots)) >= {"2059808", "2059702", "2060403"}


def test_a_caprabo_product_page_reads_like_an_eroski_one() -> None:
    product = parse_product(
        read_fixture(STORE, "product.html"),
        product_type=CapraboProduct,
        category_type=CapraboCategory,
        site_url=SITE,
        product_id="18581678",
        url=f"{SITE}/es/productdetail/18581678-producto/",
    )

    assert product.id == "18581678"
    assert product.brand == "EROSKI"
    assert product.price.amount == Decimal("0.99")
    assert product.availability.max_quantity == Decimal("48")
    assert product.shop_id == "8284"
    assert [node.id for node in product.category_path] == [
        "2059806",
        "2059807",
        "2059808",
    ]
    assert isinstance(product.category_path[0], CapraboCategory)
    assert product.nutrition is not None
    assert product.nutrition.ingredients is None
    assert product.nutrition.per == "100 mililitros"
    assert product.manufacturer == "LACTALIS PULEVA S.L."
    assert product.photos[0].url == f"{SITE}/images/18581678_x.jpg"
