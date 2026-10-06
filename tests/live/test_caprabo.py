"""live contract tests for caprabo, against www.capraboacasa.com.

caprabo runs eroski's storefront build, so the request flow is the one
``test_eroski`` covers in full; this module checks what caprabo answers on its
own host: search and its page cursor, product detail, a missing product, the
category menu, one category, a root's second page, the first rows of a catalog
walk, and the catalan
storefront with its translated names. ``get_catalog`` is left out because it
walks every root listing.

one module-scoped client makes ten requests, about 3.5 mb of html and 280 kb
on the wire: the menu twice and one product page at 630 to 770 kb each, and
fragments of about 220 kb, at the client's default pace of one request a
second.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from supermercapy import Language, NotFoundError
from supermercapy._platforms.tapestry import page_inits, tile_markup
from supermercapy.caprabo import (
    Caprabo,
    CapraboCategory,
    CapraboProduct,
    CapraboSearchResult,
)
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "caprabo"
HOST = "www.capraboacasa.com"
QUERY = "leche"
SEARCH_PATH = "/search/results:loadpage"
LISTING_PATH = "/supermarket:loadpage"
ADD_COMPONENT = "common/button/productListItemAddComponent:init"
CATEGORY_KEYS = tuple(
    f"$[].item_category{suffix}" for suffix in ("", "2", "3", "4", "5")
)


def tile_items(content: str) -> list[Any]:
    return [
        json.loads(markup.metrics[0])["ecommerce"]["items"][0]
        for markup in tile_markup(content)
        if markup.metrics
    ]


def add_options(inits: list[Any]) -> list[Any]:
    return [
        item[1] for item in inits if isinstance(item, list) and item[0] == ADD_COMPONENT
    ]


def assert_fragment_matches(live: Any, name: str, label: str) -> None:
    fixture = read_fixture(STORE, name)
    assert_no_drift(
        tile_items(live["content"]),
        tile_items(fixture["content"]),
        label=f"{label} tiles",
        ignore=CATEGORY_KEYS,
    )
    assert_no_drift(
        add_options(live["_tapestry"]["inits"]),
        add_options(fixture["_tapestry"]["inits"]),
        label=f"{label} purchase options",
    )


def assert_row(row: CapraboProduct) -> None:
    assert isinstance(row, CapraboProduct)
    assert row.id and row.name
    assert isinstance(row.price.amount, Decimal)
    assert row.price.amount > 0
    assert row.url is not None and row.url.startswith(f"https://{HOST}/")


@pytest.fixture(scope="module")
def caprabo(module_recorder: RecordingTransport) -> Iterator[Caprabo]:
    with Caprabo(**live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(caprabo: Caprabo) -> CapraboSearchResult:
    return caprabo.search_products(QUERY)


@pytest.fixture(scope="module")
def tree(caprabo: Caprabo) -> tuple[CapraboCategory, ...]:
    return caprabo.get_categories()


def test_search_returns_caprabos_own_rows(
    first_page: CapraboSearchResult, module_recorder: RecordingTransport
) -> None:
    assert len(first_page.products) >= 15
    for row in first_page.products:
        assert_row(row)
        assert row.shop_id
    assert first_page.next_cursor == "1"
    assert_fragment_matches(
        module_recorder.last_json(SEARCH_PATH, host=HOST), "search.json", "search"
    )


def test_the_cursor_reaches_the_next_page(
    caprabo: Caprabo, first_page: CapraboSearchResult
) -> None:
    second = caprabo.search_products(QUERY, cursor=first_page.next_cursor)

    assert second.products
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }


def test_get_product_reads_a_caprabo_page(
    caprabo: Caprabo,
    first_page: CapraboSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    summary = first_page.products[0]

    product = caprabo.get_product(summary.id)

    assert product.id == summary.id
    assert product.price.amount == summary.price.amount
    assert product.photos
    assert product.features
    page = module_recorder.last(f"/productdetail/{summary.id}-", host=HOST)
    assert_no_drift(
        add_options(page_inits(page.response.text)),
        add_options(page_inits(read_fixture(STORE, "product.html"))),
        label="product purchase options",
    )


def test_a_missing_product_is_not_found(caprabo: Caprabo) -> None:
    with pytest.raises(NotFoundError):
        caprabo.get_product("99999999")


def test_the_menu_shares_eroskis_ids(tree: tuple[CapraboCategory, ...]) -> None:
    assert len(tree) >= 8
    assert "2059806" in {root.id for root in tree}
    for root in tree:
        assert root.name
        assert root.children


def test_get_category_lists_one_page(
    caprabo: Caprabo,
    tree: tuple[CapraboCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    leaf = tree[0].children[0].children[0]

    category = caprabo.get_category(leaf.id)

    assert category.id == leaf.id
    assert category.products
    for row in category.products:
        assert_row(row)
    assert_fragment_matches(
        module_recorder.last_json(LISTING_PATH, host=HOST), "listing.json", "category"
    )


def test_a_root_listing_pages_on(
    caprabo: Caprabo, tree: tuple[CapraboCategory, ...]
) -> None:
    second = caprabo.get_category_products(tree[0].id, cursor="1")

    assert second.page == 1
    assert second.products
    assert second.next_cursor == "2"


def test_a_catalog_walk_starts_with_the_first_root(caprabo: Caprabo) -> None:
    rows = list(itertools.islice(caprabo.iter_catalog(), 3))

    assert len(rows) == 3
    for row in rows:
        assert_row(row)


def test_the_catalan_storefront_translates_names(
    module_recorder: RecordingTransport, first_page: CapraboSearchResult
) -> None:
    with Caprabo(language="ca", **live_options(module_recorder)) as client:
        page = client.search_products("llet")

    assert client.language is Language.CATALAN
    assert page.products
    assert any(row.name.lower().startswith("llet") for row in page.products)
    exchange = module_recorder.last(SEARCH_PATH, host=HOST)
    assert exchange.request.url.path == f"/ca{SEARCH_PATH}"
