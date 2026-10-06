"""live contract tests for mercadona, against tienda.mercadona.es and algolia.

covers every public method once: the postal-code binding and the one-store
list, search with its page cursor and the thousand-hit truncation, product
detail, a missing product (http 404), the category tree, one category, a
top-level category through its tree fallback and a group that has no page,
the home page and one season it links to, the new arrivals, a bounded
catalog walk, the reconciled index catalog, a resized and a plain image
download, an english and a catalan client, and the standalone warehouse
discovery and its script. ``get_catalog`` is left out because it requests
every one of about a hundred and fifty categories.

one module-scoped client makes about fifty requests. twenty-seven of them are
``get_indexed_catalog``, which pulls about eight megabytes from algolia; the
storefront itself sees about twenty. neither has shown a rate limit, so the
client keeps its default pacing.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from scripts.discover_warehouses import main as discover_main

from supermercapy import Language, Mercadona, NotFoundError, Store
from supermercapy.mercadona import (
    HomeNotification,
    MercadonaCategory,
    MercadonaProduct,
    MercadonaSearchResult,
    SeasonSummary,
    discover_warehouses,
    resolve_warehouse,
)
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "mercadona"
POSTAL_CODE = "28001"
OTHER_POSTAL_CODE = "08013"
QUERY = "leche"
SEARCH_HOST = "7uzjkl1dj0-dsn.algolia.net"
WAREHOUSE = re.compile(r"^[a-z0-9]{2,16}$")

# keys the fixtures add on purpose, to prove unknown fields are tolerated;
# they were never upstream
SYNTHETIC = {
    "search.json": ("$.future_search_field",),
    "product_full.json": (
        "$.unknown_top_level",
        "$.details.future_detail",
        "$.details.suppliers[].unexpected",
    ),
    "categories.json": ("$.results[].future_category_field",),
    "home.json": (
        "$.future_home_field",
        "$.sections[].content.items[].future_banner_field",
        "$.sections[].content.items[].unknown",
    ),
    "season.json": ("$.new_field",),
}


def assert_fixture_shape(live: Any, name: str, *, ignore: tuple[str, ...] = ()) -> None:
    assert_no_drift(
        live,
        read_fixture(STORE, name),
        label=name.removesuffix(".json"),
        ignore=SYNTHETIC.get(name, ()) + ignore,
    )


@pytest.fixture(scope="module")
def mercadona(module_recorder: RecordingTransport) -> Iterator[Mercadona]:
    with Mercadona.from_postal_code(
        POSTAL_CODE, **live_options(module_recorder)
    ) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(mercadona: Mercadona) -> MercadonaSearchResult:
    return mercadona.search_products(QUERY, page_size=5)


@pytest.fixture(scope="module")
def product(
    mercadona: Mercadona, first_page: MercadonaSearchResult
) -> MercadonaProduct:
    return mercadona.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def tree(mercadona: Mercadona) -> tuple[MercadonaCategory, ...]:
    return mercadona.get_categories()


# ---------------------------------------------------------------------- binding


def test_postal_code_binds_a_warehouse(
    mercadona: Mercadona, module_recorder: RecordingTransport
) -> None:
    assert WAREHOUSE.fullmatch(mercadona.warehouse)
    assert mercadona.store_id == mercadona.warehouse
    exchange = module_recorder.last("/api/postal-codes/actions/change-pc/")
    assert exchange.request.method == "PUT"
    assert exchange.response.headers["X-Customer-Wh"]


def test_list_stores_resolves_one_warehouse(mercadona: Mercadona) -> None:
    bound = mercadona.warehouse

    stores = mercadona.list_stores(OTHER_POSTAL_CODE)

    assert len(stores) == 1
    assert isinstance(stores[0], Store)
    assert WAREHOUSE.fullmatch(stores[0].id)
    assert stores[0].kind == "warehouse"
    # resolving another postcode does not rebind the client
    assert mercadona.warehouse == bound


# ----------------------------------------------------------------------- search


def test_search_pages_and_the_cursor_round_trips(
    mercadona: Mercadona,
    module_recorder: RecordingTransport,
    first_page: MercadonaSearchResult,
) -> None:
    assert first_page.products
    assert first_page.page == 0
    assert first_page.total_hits is not None
    assert first_page.total_hits > len(first_page.products)
    assert first_page.next_cursor == "1"
    assert not first_page.truncated
    assert_fixture_shape(
        module_recorder.last_json("/query", host=SEARCH_HOST), "search.json"
    )
    for summary in first_page.products:
        assert isinstance(summary.id, str) and summary.id
        assert summary.name
        assert summary.price.currency == "EUR"
        assert isinstance(summary.price.amount, Decimal)
        assert summary.price.amount > 0

    second = mercadona.search_products(
        QUERY, page_size=5, cursor=first_page.next_cursor
    )

    assert second.page == 1
    assert {item.id for item in second.products}.isdisjoint(
        item.id for item in first_page.products
    )


def test_a_broad_query_is_truncated_at_the_index_cap(mercadona: Mercadona) -> None:
    page = mercadona.search_products("", page_size=1000)

    assert page.total_hits is not None and page.total_hits > 1000
    assert len(page.products) == 1000
    assert page.next_cursor is None
    assert page.truncated


# ---------------------------------------------------------------------- product


def test_a_product_carries_its_detail(
    module_recorder: RecordingTransport, product: MercadonaProduct
) -> None:
    assert product.name
    assert product.ean is not None and product.ean.isdigit()
    assert product.price.currency == "EUR"
    assert product.price.amount is not None
    assert product.photos
    assert product.category_path
    assert product.category_path[0].level == 0
    assert_fixture_shape(
        module_recorder.last_json("/api/products/"), "product_full.json"
    )


def test_a_missing_product_raises_not_found(mercadona: Mercadona) -> None:
    with pytest.raises(NotFoundError):
        mercadona.get_product("99999999")


def test_photos_download_resized_and_plain(
    mercadona: Mercadona, product: MercadonaProduct, tmp_path: Path
) -> None:
    resized = mercadona.download_photo(
        product.photos[0], tmp_path / "small.jpg", width=200
    )
    plain = mercadona.download(product.photos[0].sized(width=400), tmp_path / "x.jpg")

    assert resized.stat().st_size > 1000
    assert plain.stat().st_size > 1000


# ------------------------------------------------------------------- categories


def test_the_tree_and_one_category(
    mercadona: Mercadona,
    module_recorder: RecordingTransport,
    tree: tuple[MercadonaCategory, ...],
) -> None:
    assert tree
    assert all(root.level == 0 for root in tree)
    assert all(root.children for root in tree)
    assert all(child.level == 1 for root in tree for child in root.children)
    assert_fixture_shape(
        module_recorder.last_json("/api/categories/"), "categories.json"
    )

    wanted = tree[0].children[0]
    category = mercadona.get_category(wanted.id)

    assert category.id == wanted.id
    assert category.level == 1
    assert category.children
    assert all(child.level == 2 for child in category.children)
    assert any(child.products for child in category.children)
    assert_fixture_shape(
        module_recorder.last_json(f"/api/categories/{wanted.id}/"), "category_72.json"
    )


def test_a_top_level_category_falls_back_to_the_tree(
    mercadona: Mercadona, tree: tuple[MercadonaCategory, ...]
) -> None:
    root = mercadona.get_category(tree[0].id)

    assert root.id == tree[0].id
    assert root.level == 0
    assert root.products == ()
    assert [child.id for child in root.children] == [
        child.id for child in tree[0].children
    ]


def test_a_group_below_the_second_level_is_not_found(
    mercadona: Mercadona, tree: tuple[MercadonaCategory, ...]
) -> None:
    group = mercadona.get_category(tree[0].children[0].id).children[0]

    with pytest.raises(NotFoundError):
        mercadona.get_category(group.id)


def test_the_catalog_walks_the_tree(mercadona: Mercadona) -> None:
    summaries = list(itertools.islice(mercadona.iter_catalog(), 30))

    assert len(summaries) == 30
    assert all(isinstance(summary.id, str) for summary in summaries)


def test_the_index_catalog_reconciles(mercadona: Mercadona) -> None:
    catalog = mercadona.get_indexed_catalog()

    assert catalog.queried_category_ids, "the index stopped publishing facets"
    assert len(catalog.products) > 1000
    assert catalog.reconciled, (
        f"collected {len(catalog.products)} of {catalog.reported_total_hits}"
    )


# ------------------------------------------------------------------ home pages


def test_home_new_arrivals_and_a_season(
    mercadona: Mercadona, module_recorder: RecordingTransport
) -> None:
    sections = mercadona.get_home()

    assert sections
    assert any(section.products for section in sections)
    assert_fixture_shape(module_recorder.last_json("/api/home/"), "home.json")
    for section in sections:
        for item in section.items:
            if isinstance(item, HomeNotification):
                assert item.title

    arrivals = mercadona.get_new_arrivals()
    assert arrivals
    assert all(arrival.is_new for arrival in arrivals)
    assert_fixture_shape(
        module_recorder.last_json("/api/home/new-arrivals/"), "new_arrivals.json"
    )

    seasons = [
        item
        for section in sections
        for item in section.items
        if isinstance(item, SeasonSummary)
    ]
    if not seasons:
        pytest.skip("the home page links no season this week")
    season = mercadona.get_season(seasons[0].id)
    assert season.title
    assert season.products
    assert_fixture_shape(
        module_recorder.last_json(f"/api/home/sections/{seasons[0].id}/"),
        "season.json",
    )


# -------------------------------------------------------------------- languages


@pytest.mark.parametrize(
    ("language", "query"), [(Language.ENGLISH, "milk"), (Language.CATALAN, "llet")]
)
def test_each_language_has_its_own_index_and_translation(
    mercadona: Mercadona,
    module_recorder: RecordingTransport,
    tree: tuple[MercadonaCategory, ...],
    language: Language,
    query: str,
) -> None:
    with Mercadona(
        mercadona.warehouse, language=language, **live_options(module_recorder)
    ) as client:
        page = client.search_products(query, page_size=3)
        translated = client.get_categories()

    index = f"products_prod_{mercadona.warehouse}_{language.value}"
    assert index in str(module_recorder.last("/query", host=SEARCH_HOST).request.url)
    assert page.products
    assert all(summary.price.amount is not None for summary in page.products)
    assert [root.id for root in translated] == [root.id for root in tree]
    assert {root.name for root in translated} != {root.name for root in tree}


# -------------------------------------------------------------------- discovery


def test_standalone_discovery_and_its_script(
    capsys: pytest.CaptureFixture[str],
) -> None:
    warehouse = resolve_warehouse(POSTAL_CODE)
    assert WAREHOUSE.fullmatch(warehouse)

    mapping = discover_warehouses([POSTAL_CODE, OTHER_POSTAL_CODE], max_workers=2)
    assert set(mapping) == {POSTAL_CODE, OTHER_POSTAL_CODE}
    assert mapping[POSTAL_CODE] == warehouse
    assert all(WAREHOUSE.fullmatch(value) for value in mapping.values())

    assert discover_main([OTHER_POSTAL_CODE]) == 0
    assert (
        capsys.readouterr().out
        == f"{OTHER_POSTAL_CODE}\t{mapping[OTHER_POSTAL_CODE]}\n"
    )
