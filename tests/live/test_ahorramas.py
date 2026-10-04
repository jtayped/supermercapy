"""live contract tests for ahorramás, against www.ahorramas.com.

covers every public method once except two walks: search and its offset
cursor, product detail, a missing product, the category menu, one category, an
unknown category, one page of the offers branch, the first rows of a catalog
walk, and a thumbnail download. ``get_offers`` walks between one and one and a
half thousand offers and ``get_catalog`` about 4,600 products, a megabyte per
hundred, so both are left out; the offers branch is read one page deep through
``get_category_products`` instead.

one module-scoped client makes ten requests, about 2.7 mb decompressed and
190 kb on the wire: the home page's menu at close to 800 kb, grid pages of
about 11 kb a product, and json records of about 30 kb, at the client's
default pace of one request a second.
cloudflare fronts the shop; it neither challenged nor limited the library's own
user agent in october 2026.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Iterator
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from supermercapy import NotFoundError
from supermercapy.ahorramas import (
    Ahorramas,
    AhorramasCategory,
    AhorramasProduct,
    AhorramasSearchResult,
)
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "ahorramas"
HOST = "www.ahorramas.com"
QUERY = "leche"
GRID_PATH = "/Search-UpdateGrid"
RECORD_PATH = "/Product-Variation"

# what only some records carry: a struck-through price, a promotion, a
# description, an image badge's icon and the json describing a weighed product
RECORD_OPTIONAL = (
    "$.product.price.list",
    "$.product.unitPrice.list",
    "$.product.promotions",
    "$.product.attrProductDecription",
    "$.product.akeneo_unidadMedida",
    "$.product.attrAkeneoBadges[].icon",
)


def gtm_items(html: str) -> list[Any]:
    """return each tile's analytics json, which names the product."""

    found = re.findall(
        r'class="product-pdp-link[^"]*"\s+data-gtm-layer="([^"]*)"', html
    )
    return [json.loads(unquote(unescape(raw))) for raw in found[::2]]


def cart_attributes(html: str) -> list[dict[str, str]]:
    """return each tile's add-to-cart data attributes, which set its limits."""

    blocks = re.findall(r'<div class="add-to-cart[^"]*"(.*?)>', html, re.DOTALL)
    return [dict(re.findall(r'(data-[a-z-]+)="([^"]*)"', block)) for block in blocks]


def assert_grid_matches(live: str, name: str, label: str) -> None:
    fixture = read_fixture(STORE, name)
    assert_no_drift(gtm_items(live), gtm_items(fixture), label=f"{label} analytics")
    assert_no_drift(
        cart_attributes(live), cart_attributes(fixture), label=f"{label} cart"
    )


def assert_row(row: AhorramasProduct) -> None:
    assert isinstance(row, AhorramasProduct)
    assert row.id and row.name
    assert isinstance(row.price.amount, Decimal)
    assert row.price.amount > 0
    assert row.url is not None and row.url.startswith(f"https://{HOST}/")


@pytest.fixture(scope="module")
def ahorramas(module_recorder: RecordingTransport) -> Iterator[Ahorramas]:
    with Ahorramas(**live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(ahorramas: Ahorramas) -> AhorramasSearchResult:
    return ahorramas.search_products(QUERY, page_size=12)


@pytest.fixture(scope="module")
def tree(ahorramas: Ahorramas) -> tuple[AhorramasCategory, ...]:
    return ahorramas.get_categories()


# ---------------------------------------------------------------------- search


def test_search_returns_priced_tiles_and_an_offset_cursor(
    first_page: AhorramasSearchResult, module_recorder: RecordingTransport
) -> None:
    assert len(first_page.products) == 12
    for row in first_page.products:
        assert_row(row)
    assert first_page.next_cursor == "12"
    assert any(row.price.reference is not None for row in first_page.products)
    exchange = module_recorder.last(GRID_PATH, host=HOST)
    assert exchange.request.url.params["q"] == QUERY
    assert_grid_matches(exchange.response.text, "search.html", "search")


def test_the_cursor_continues_at_the_next_offset(
    ahorramas: Ahorramas, first_page: AhorramasSearchResult
) -> None:
    second = ahorramas.search_products(
        QUERY, page_size=12, cursor=first_page.next_cursor
    )

    assert second.offset == 12
    assert second.products
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }


# --------------------------------------------------------------------- product


def test_get_product_reads_the_record(
    ahorramas: Ahorramas,
    first_page: AhorramasSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    summary = first_page.products[0]

    product = ahorramas.get_product(summary.id)

    assert product.id == summary.id
    assert product.name == summary.name
    assert product.price.amount == summary.price.amount
    assert product.photos
    assert product.category_ids
    assert product.availability.available is not None
    assert product.nutrition is None
    assert_no_drift(
        module_recorder.last_json(RECORD_PATH, host=HOST),
        read_fixture(STORE, "product.json"),
        label="product record",
        ignore=RECORD_OPTIONAL,
    )


def test_a_missing_product_is_not_found(
    ahorramas: Ahorramas, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        ahorramas.get_product("99999999")

    exchange = module_recorder.last(RECORD_PATH, host=HOST)
    assert exchange.response.status_code == 500
    assert_no_drift(
        exchange.json(),
        read_fixture(STORE, "product_missing.json"),
        label="missing record",
    )


# ------------------------------------------------------------------ categories


def test_the_menu_holds_the_whole_tree(tree: tuple[AhorramasCategory, ...]) -> None:
    assert len(tree) >= 8
    assert "ofertas" in {root.id for root in tree}
    for root in tree:
        assert root.name
        assert root.level == 1
        assert root.children
        for child in root.children:
            assert child.parent_id == root.id
            assert child.path.startswith(root.path)


def test_get_category_lists_one_page_of_a_leaf(
    ahorramas: Ahorramas,
    tree: tuple[AhorramasCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    fresh = next(root for root in tree if root.id == "frescos")
    leaf = fresh.children[0].children[0]

    category = ahorramas.get_category(leaf.id, page_size=12)

    assert category.id == leaf.id
    assert category.products
    for row in category.products:
        assert_row(row)
    exchange = module_recorder.last(GRID_PATH, host=HOST)
    assert exchange.request.url.params["cgid"] == leaf.id
    assert_grid_matches(exchange.response.text, "listing.html", "category")


def test_an_unknown_category_is_not_found(ahorramas: Ahorramas) -> None:
    with pytest.raises(NotFoundError):
        ahorramas.get_category_products("no_existe_supermercapy", page_size=1)


def test_the_offers_branch_lists_promoted_products(ahorramas: Ahorramas) -> None:
    page = ahorramas.get_category_products("ofertas", page_size=12)

    assert len(page.products) == 12
    assert all(row.promotions or row.price.is_discounted for row in page.products)


def test_a_catalog_walk_starts_at_the_root(ahorramas: Ahorramas) -> None:
    rows = list(itertools.islice(ahorramas.iter_catalog(), 3))

    assert len(rows) == 3
    for row in rows:
        assert_row(row)


def test_download_saves_a_thumbnail(
    ahorramas: Ahorramas, first_page: AhorramasSearchResult, tmp_path: Path
) -> None:
    thumbnail = first_page.products[0].thumbnail
    assert thumbnail is not None

    saved = ahorramas.download(thumbnail, tmp_path / "thumb.jpg")

    data = saved.read_bytes()
    assert data.startswith(b"\xff\xd8") or data[8:12] == b"WEBP"
