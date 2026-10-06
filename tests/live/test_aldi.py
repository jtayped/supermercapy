"""live contract tests for aldi, against www.aldi.es and its algolia index.

covers every public method once: search with its offset cursor, a product
record, a missing record, the overview of top-level categories, one top-level
and one child category, the products under a category key, a bounded catalogue
walk, the weekly offers and their sections, the balearic and canary regions
from a postcode, and one image download. it also checks that the site's own
bundle still carries the pinned algolia key, because a rotated key is the most
likely way for this client to break. ``get_catalog`` is left out because it
reads every record, and ``get_categories(deep=True)`` because it reads twenty
pages.

about twenty requests, half of them to algolia. neither host has shown bot
protection or a rate limit.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from supermercapy import Aldi, NotFoundError
from supermercapy.aldi import AldiCategory, AldiProduct, AldiSearchResult, Region
from supermercapy.aldi._constants import ALGOLIA_API_KEY, ALGOLIA_APP_ID
from supermercapy.aldi.models import api_data, next_data, page_props
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "aldi"
QUERY = "leche"
INDEX_PATH = "/1/indexes/an_prd_es_es_pen_products2"

# what only some records carry: a brand, descriptions, a pack size, the
# categories, a price (loose produce has none), the time-limited prices, and
# the image captions newer photos come with
RECORD_OPTIONAL = (
    "assets[].altText",
    "assets[].title",
    "assets[].isAiGenerated",
    "brandName",
    "shortDescription",
    "longDescription",
    "salesUnit",
    "categoryIDs",
    "mainCategoryID",
    "hierarchicalCategories",
    "currentPrice",
    "promotionPrices",
    "legalInformation",
)


def records(path: str) -> tuple[str, ...]:
    return tuple(f"{path}.{key}" for key in RECORD_OPTIONAL)


def embedded(html: str) -> dict[str, Any]:
    """return the api answers a site page embeds, by name."""

    return api_data(page_props(next_data(html)))


@pytest.fixture(scope="module")
def aldi(module_recorder: RecordingTransport) -> Iterator[Aldi]:
    with Aldi(**live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(aldi: Aldi) -> AldiSearchResult:
    return aldi.search_products(QUERY)


@pytest.fixture(scope="module")
def product(aldi: Aldi, first_page: AldiSearchResult) -> AldiProduct:
    return aldi.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def roots(aldi: Aldi) -> tuple[AldiCategory, ...]:
    return aldi.get_categories()


def assert_record(product: AldiProduct) -> None:
    assert isinstance(product.id, str) and product.id.isdigit()
    assert product.name
    assert product.ean is None
    assert product.nutrition is None
    if product.price.amount is not None:
        assert isinstance(product.price.amount, Decimal)
        assert product.price.amount > 0


# ---------------------------------------------------------------------- search


def test_search_returns_records_and_an_offset_cursor(
    first_page: AldiSearchResult, module_recorder: RecordingTransport
) -> None:
    assert len(first_page.products) == 24
    for row in first_page.products:
        assert_record(row)
    assert first_page.total_hits is not None and first_page.total_hits > 24
    assert first_page.next_cursor == "24"
    assert any(row.price.reference is not None for row in first_page.products)
    assert_no_drift(
        module_recorder.last_json(f"{INDEX_PATH}/query"),
        read_fixture(STORE, "search.json"),
        label="search",
        ignore=(*records("$.hits[]"), "$.extensions", "$.renderingContent"),
    )


def test_the_cursor_continues(aldi: Aldi, first_page: AldiSearchResult) -> None:
    second = aldi.search_products(QUERY, cursor=first_page.next_cursor)

    assert second.offset == 24
    assert second.products
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }


# --------------------------------------------------------------------- product


def test_get_product_reads_one_record(
    product: AldiProduct,
    first_page: AldiSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    assert product.id == first_page.products[0].id
    assert_record(product)
    assert product.photos
    assert product.article_number
    assert product.region == "pen"
    assert_no_drift(
        module_recorder.last_json(f"{INDEX_PATH}/{product.id}"),
        read_fixture(STORE, "product.json"),
        label="record",
        ignore=records("$"),
    )


def test_a_missing_record_is_not_found(
    aldi: Aldi, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        aldi.get_product("999999999")

    exchange = module_recorder.last(f"{INDEX_PATH}/999999999")
    assert exchange.response.status_code == 404
    assert_no_drift(
        exchange.json(), read_fixture(STORE, "not_found.json"), label="missing record"
    )


# ------------------------------------------------------------------ categories


def test_the_overview_lists_the_top_level_categories(
    roots: tuple[AldiCategory, ...], module_recorder: RecordingTransport
) -> None:
    assert len(roots) > 15
    assert all(root.key and root.level == 1 for root in roots)
    assert_no_drift(
        embedded(module_recorder.last("/productos.html").response.text),
        embedded(read_fixture(STORE, "productos.html")),
        label="overview page",
    )


def test_a_top_level_and_a_child_category(
    aldi: Aldi,
    roots: tuple[AldiCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    root = next(root for root in roots if root.id == "lacteos-y-huevos")

    top = aldi.get_category(root.id)

    assert top.children
    assert top.products
    assert top.product_count is not None
    assert_no_drift(
        embedded(module_recorder.last(f"/productos/{root.id}.html").response.text),
        embedded(read_fixture(STORE, "category_root.html")),
        label="top-level page",
    )

    child = next(
        item for item in top.children if item.key == "leche-y-bebidas-vegetales"
    )
    category = aldi.get_category(child.id)

    assert category.parent_id == root.id
    assert category.products
    for row in category.products:
        assert_record(row)
        assert child.key in row.category_ids
    assert_no_drift(
        embedded(module_recorder.last(f"/productos/{child.id}.html").response.text),
        embedded(read_fixture(STORE, "category_leaf.html")),
        label="child page",
    )
    assert_no_drift(
        module_recorder.last_json(f"{INDEX_PATH}/query"),
        read_fixture(STORE, "category.json"),
        label="category listing",
        ignore=(*records("$.hits[]"), "$.extensions", "$.renderingContent"),
    )


def test_products_by_a_records_category_key(aldi: Aldi, product: AldiProduct) -> None:
    key = product.category_ids[0]

    page = aldi.get_category_products(key, page_size=5)

    assert page.products
    assert all(key in row.category_ids for row in page.products)


def test_iter_catalog_reads_the_index(aldi: Aldi) -> None:
    sample = list(itertools.islice(aldi.iter_catalog(), 50))

    assert len(sample) == 50
    for row in sample:
        assert_record(row)


# ---------------------------------------------------------------------- offers


def test_the_weekly_offers(aldi: Aldi, module_recorder: RecordingTransport) -> None:
    groups = aldi.get_offer_groups()
    offers = aldi.get_offers()

    assert groups
    assert all(group.starts_on and group.ends_on for group in groups)
    assert offers
    assert len({row.id for row in offers}) == len(offers)
    assert any(row.promotion_prices for row in offers)
    assert_no_drift(
        embedded(module_recorder.last("/ofertas.html").response.text),
        embedded(read_fixture(STORE, "offers.html")),
        label="offers page",
        # the records are keyed by product id, which changes weekly; their
        # shape is checked against search and the record endpoint
        ignore=("$.OFFER_GET.algoliaDataMap", "$.PAGE_MGNL_GET"),
    )


# --------------------------------------------------------------------- regions


@pytest.mark.parametrize(
    ("postal_code", "region"),
    [("07001", Region.BALEARIC_ISLANDS), ("35001", Region.CANARY_ISLANDS)],
)
def test_an_island_postcode_reads_its_own_index(
    postal_code: str, region: Region, module_recorder: RecordingTransport
) -> None:
    with Aldi.from_postal_code(postal_code, **live_options(module_recorder)) as island:
        assert island.region is region
        page = island.search_products(QUERY, page_size=5)
        offers = island.get_offers()

    assert page.products
    assert all(row.region == region.value for row in page.products)
    assert offers
    assert module_recorder.last(f"/{region.value}/ofertas.html")


# ------------------------------------------------------------- key, images


def test_the_site_bundle_still_carries_the_pinned_key(
    module_recorder: RecordingTransport,
) -> None:
    page = module_recorder.last("/productos/lacteos-y-huevos/").response.text
    match = re.search(
        r'"(/_next/static/chunks/pages/product-overview/[^"]+\.js)"', page
    )
    assert match is not None, "the category page no longer loads its own bundle"
    with httpx.Client(transport=module_recorder, timeout=20.0) as raw:
        bundle = raw.get(f"https://www.aldi.es{match.group(1)}").text

    assert f'"{ALGOLIA_APP_ID}","{ALGOLIA_API_KEY}"' in bundle


def test_download_a_product_image(
    aldi: Aldi, product: AldiProduct, tmp_path: Path
) -> None:
    path = aldi.download(product.photos[0], tmp_path / "product.jpg")

    assert path.read_bytes()[:2] == b"\xff\xd8"
