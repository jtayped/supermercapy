"""live contract tests for eroski, against supermercado.eroski.es.

covers every public method once: search and its page cursor, a smaller page cut
out of one storefront page, product detail with nutrition, a missing product,
the category menu, one category, a category's second page, the first rows of a
catalog walk, a thumbnail download, and a catalan search. ``get_catalog`` is
left out because it walks every root listing, hundreds of pages.

the storefront is html. full pages carry a menu of about 780 kb, so the menu,
a product page and the catalog's own menu read cost close to a megabyte each,
and every listing is a fragment of about 230 kb. one module-scoped client
makes twelve requests, about 4.5 mb of html, which compression brings down to
about 330 kb on the wire, at the client's default pace of one request a
second. eroski showed no rate limit or challenge in october
2026; it refuses only a few user agents, the library's own not among them.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from supermercapy import Language, NotFoundError
from supermercapy._platforms.tapestry import page_inits, tile_markup
from supermercapy.eroski import (
    Eroski,
    EroskiCategory,
    EroskiProduct,
    EroskiSearchResult,
)
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "eroski"
HOST = "supermercado.eroski.es"
QUERY = "aceite oliva"
SEARCH_PATH = "/search/results:loadpage"
LISTING_PATH = "/supermarket:loadpage"
ADD_COMPONENT = "common/button/productListItemAddComponent:init"
TRACKING = "common/tracking:init"

# only some products are filed under categories, and the depth varies
CATEGORY_KEYS = tuple(
    f"$[].item_category{suffix}" for suffix in ("", "2", "3", "4", "5")
)


def tile_items(content: str) -> list[Any]:
    """return the ga4 item of every tile, the json the parser reads first."""

    return [
        json.loads(markup.metrics[0])["ecommerce"]["items"][0]
        for markup in tile_markup(content)
        if markup.metrics
    ]


def add_options(inits: list[Any]) -> list[Any]:
    return [
        item[1] for item in inits if isinstance(item, list) and item[0] == ADD_COMPONENT
    ]


def viewed_item(inits: list[Any]) -> Any:
    for item in inits:
        if isinstance(item, list) and item[0] == TRACKING:
            event = json.loads(item[1])
            if event.get("event") == "view_item":
                return event["ecommerce"]["items"][0]
    raise AssertionError("the page fired no view_item event")


def assert_fragment_matches(live: Any, name: str, label: str) -> None:
    fixture = read_fixture(STORE, name)
    assert set(live) == {"content", "_tapestry"}
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


def assert_row(row: EroskiProduct) -> None:
    assert isinstance(row, EroskiProduct)
    assert row.id and row.name
    assert row.price.currency == "EUR"
    assert isinstance(row.price.amount, Decimal)
    assert row.price.amount > 0
    assert row.url is not None and row.url.startswith(f"https://{HOST}/")
    assert row.shop_id


@pytest.fixture(scope="module")
def eroski(module_recorder: RecordingTransport) -> Iterator[Eroski]:
    with Eroski(**live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(eroski: Eroski) -> EroskiSearchResult:
    return eroski.search_products(QUERY)


@pytest.fixture(scope="module")
def tree(eroski: Eroski) -> tuple[EroskiCategory, ...]:
    return eroski.get_categories()


# ---------------------------------------------------------------------- search


def test_search_returns_priced_tiles_and_a_page_cursor(
    first_page: EroskiSearchResult, module_recorder: RecordingTransport
) -> None:
    assert len(first_page.products) >= 15
    for row in first_page.products:
        assert_row(row)
    assert first_page.next_cursor == "1"
    assert first_page.total_hits is None
    # olive oil is priced per litre, and something is always on offer
    assert any(row.price.reference is not None for row in first_page.products)
    assert any(
        row.promotions or row.price.is_discounted or row.is_lowered_price
        for row in first_page.products
    )
    exchange = module_recorder.last(SEARCH_PATH, host=HOST)
    assert exchange.request.url.params["pageNumber"] == "0"
    assert_fragment_matches(exchange.json(), "search.json", "search")


def test_the_cursor_reaches_the_next_storefront_page(
    eroski: Eroski, first_page: EroskiSearchResult
) -> None:
    second = eroski.search_products(QUERY, cursor=first_page.next_cursor)

    assert second.page == 1
    assert second.products
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }


def test_a_smaller_page_is_cut_from_the_same_storefront_page(
    eroski: Eroski, first_page: EroskiSearchResult
) -> None:
    page = eroski.search_products(QUERY, page_size=5)

    assert page.page_size == 5
    assert page.next_cursor == "0:5"
    assert [row.id for row in page.products] == [
        row.id for row in first_page.products[:5]
    ]


# --------------------------------------------------------------------- product


def test_get_product_reads_the_page_and_its_characteristics(
    eroski: Eroski,
    first_page: EroskiSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    summary = first_page.products[0]

    product = eroski.get_product(summary.id)

    assert product.id == summary.id
    assert product.name == summary.name
    assert product.price.amount == summary.price.amount
    assert product.photos
    assert product.features
    assert product.manufacturer
    assert product.nutrition is not None
    assert product.nutrition.values
    page = module_recorder.last(f"/productdetail/{summary.id}-", host=HOST)
    inits = page_inits(page.response.text)
    fixture = page_inits(read_fixture(STORE, "product.html"))
    assert_no_drift(
        viewed_item(inits),
        viewed_item(fixture),
        label="product event",
        ignore=tuple(key.replace("$[]", "$") for key in CATEGORY_KEYS),
    )
    assert_no_drift(
        add_options(inits), add_options(fixture), label="product purchase options"
    )


def test_a_missing_product_redirects_to_the_error_page(
    eroski: Eroski, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        eroski.get_product("99999999")

    redirect = module_recorder.last("/productdetail/99999999-", host=HOST)
    assert redirect.response.status_code in {301, 302}
    assert "/error/" in redirect.response.headers["Location"]


# ------------------------------------------------------------------ categories


def test_the_menu_holds_the_whole_tree(tree: tuple[EroskiCategory, ...]) -> None:
    assert len(tree) >= 10
    for root in tree:
        assert root.id.isdigit()
        assert root.name
        assert root.level == 1
        assert root.parent_id is None
        assert root.children
        for child in root.children:
            assert child.parent_id == root.id
            assert child.level == 2
            assert child.path.startswith(f"{root.path}/")

    def depth(node: EroskiCategory) -> int:
        return 1 + max((depth(child) for child in node.children), default=0)

    assert max(depth(root) for root in tree) >= 4
    assert not any(root.name.startswith("Ver todo") for root in tree)


def test_get_category_lists_one_page_of_a_leaf(
    eroski: Eroski,
    tree: tuple[EroskiCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    leaf = tree[0].children[0].children[0]

    category = eroski.get_category(leaf.id)

    assert category.id == leaf.id
    assert category.name == leaf.name
    assert category.products
    for row in category.products:
        assert_row(row)
    exchange = module_recorder.last(LISTING_PATH, host=HOST)
    assert exchange.request.url.params["t:ac"] == leaf.path
    assert_fragment_matches(exchange.json(), "listing.json", "category")


def test_a_root_listing_pages_on(
    eroski: Eroski, tree: tuple[EroskiCategory, ...]
) -> None:
    second = eroski.get_category_products(tree[0].id, cursor="1")

    assert second.page == 1
    assert second.products
    assert second.next_cursor == "2"


def test_a_catalog_walk_starts_with_the_first_root(
    eroski: Eroski, tree: tuple[EroskiCategory, ...]
) -> None:
    rows = list(itertools.islice(eroski.iter_catalog(), 3))

    assert len(rows) == 3
    for row in rows:
        assert_row(row)
        assert row.category_ids[:1] in ((), (tree[0].id,))


# ------------------------------------------------------------- images, language


def test_download_saves_a_thumbnail(
    eroski: Eroski, first_page: EroskiSearchResult, tmp_path: Path
) -> None:
    thumbnail = first_page.products[0].thumbnail
    assert thumbnail is not None

    saved = eroski.download(thumbnail, tmp_path / "thumb.jpg")

    assert saved.read_bytes().startswith(b"\xff\xd8")


def test_the_catalan_storefront_answers_under_ca(
    eroski: Eroski, module_recorder: RecordingTransport
) -> None:
    with Eroski(language="ca", **live_options(module_recorder)) as client:
        page = client.search_products("llet")

    assert client.language is Language.CATALAN
    assert page.products
    exchange = module_recorder.last(SEARCH_PATH, host=HOST)
    assert exchange.request.url.path == f"/ca{SEARCH_PATH}"
