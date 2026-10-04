"""live contract tests for alcampo, against www.compraonline.alcampo.es.

covers every public method once: the category tree, search and a second page
by token, one category, product detail, a client bound to a click-and-collect
point in another region, the similar and related products with the csrf-gated
batch resolve behind them, the promotions listing and the offers, the new
arrivals, the suggestions, the point list, a missing product, an unknown
category, and one image download. alcampo has no catalog, postcode resolver,
ean lookup or home page, so there is nothing else to call.

the aws waf budget is the constraint, as on bonpreu: seven or eight requests to
the product pages service from one address before it answers a challenge for
tens of minutes, while the promotions listing, the suggestions, the session and
the point list keep answering. the module sends thirteen requests to that
service at the default two-second pacing, ordered by what a broken contract
would cost: the tree, search, the sheet, the region move, the new arrivals and
one category first, the batch resolve after, the error shapes last. expect the
tail of the module to skip as inconclusive, and run it at most once per half
hour from one address.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from supermercapy import Alcampo, NotFoundError
from supermercapy.alcampo import (
    AlcampoCategory,
    AlcampoProduct,
    AlcampoSearchResult,
    AlcampoStore,
)
from supermercapy.alcampo._constants import DEFAULT_REGION_ID
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "alcampo"
QUERY = "leche"
PAGES = "/api/webproductpagews"
SEARCH_PATH = f"{PAGES}/v6/product-pages/search"
LISTING_PATH = f"{PAGES}/v6/product-pages"
ROW = "$.productGroups[].decoratedProducts[]"
# a mainland region other than the default, served by the barcelona points
BARCELONA = "08019"

# what only some rows carry: promotions, a brand, a pack size, a catchweight
# range, and a promotional or advertised placement
ROW_OPTIONAL = (
    "promotions",
    "brand",
    "packSizeDescription",
    "catchweight",
    "promoPrice",
    "promoUnitPrice",
    "externalAdvertId",
    "featuredProductCampaign",
    "maxAvailableQuantity",
)
LISTING_OPTIONAL = tuple(f"{ROW}.{key}" for key in ROW_OPTIONAL)


@pytest.fixture(scope="module")
def alcampo(module_recorder: RecordingTransport) -> Iterator[Alcampo]:
    with Alcampo(**live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def tree(alcampo: Alcampo) -> tuple[AlcampoCategory, ...]:
    return alcampo.get_categories()


@pytest.fixture(scope="module")
def first_page(alcampo: Alcampo) -> AlcampoSearchResult:
    return alcampo.search_products(QUERY, page_size=5)


@pytest.fixture(scope="module")
def product(alcampo: Alcampo, first_page: AlcampoSearchResult) -> AlcampoProduct:
    return alcampo.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def stores(alcampo: Alcampo) -> tuple[AlcampoStore, ...]:
    return alcampo.list_stores()


def assert_row(row: AlcampoProduct) -> None:
    assert isinstance(row.id, str) and row.id.isdigit()
    assert row.name
    assert row.ean is None
    assert row.product_uuid
    assert row.price.currency == "EUR"
    assert isinstance(row.price.amount, Decimal)
    assert row.price.amount > 0


# ------------------------------------------------------------------ the tree


def test_get_categories_returns_the_whole_tree(
    tree: tuple[AlcampoCategory, ...], module_recorder: RecordingTransport
) -> None:
    assert tree
    assert any(root.children for root in tree)
    assert all(root.retailer_category_id for root in tree)
    assert_no_drift(
        module_recorder.last_json(f"{PAGES}/v1/categories"),
        read_fixture(STORE, "categories.json"),
        label="category tree",
    )


# --------------------------------------------------------------------- search


def test_search_returns_rows_and_a_page_token(
    first_page: AlcampoSearchResult, module_recorder: RecordingTransport
) -> None:
    assert first_page.products
    for row in first_page.products:
        assert_row(row)
    assert first_page.next_cursor
    assert first_page.sort_options
    assert_no_drift(
        module_recorder.last_json(SEARCH_PATH),
        read_fixture(STORE, "search.json"),
        label="search",
        ignore=LISTING_OPTIONAL,
    )


# -------------------------------------------------------------------- product


def test_get_product_reads_the_sheet(
    product: AlcampoProduct,
    first_page: AlcampoSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    assert product.id == first_page.products[0].id
    assert_row(product)
    assert product.photos
    assert product.fields
    assert product.category_path
    assert_no_drift(
        module_recorder.last_json(f"{PAGES}/v5/products/bop"),
        read_fixture(STORE, "product.json"),
        label="product sheet",
        ignore=(
            *(f"$.product.{key}" for key in ROW_OPTIONAL),
            "$.bopData.detailedDescription",
            "$.bopPromotions[]",
        ),
    )


def test_a_bound_client_moves_its_session_to_the_point_region(
    stores: tuple[AlcampoStore, ...],
    product: AlcampoProduct,
    module_recorder: RecordingTransport,
) -> None:
    point = next(
        store
        for store in stores
        if store.postal_code == BARCELONA and store.region_id != DEFAULT_REGION_ID
    )

    with Alcampo(point, **live_options(module_recorder)) as bound:
        sheet = bound.get_product(product.id)
        assert bound.region_id == point.region_id

    move = module_recorder.last("/sessions/active", method="PUT")
    assert move.request.headers["X-CSRF-TOKEN"]
    assert move.json()["regionId"] == point.region_id
    assert move.json()["deliveryDestinationId"] == point.id
    assert_no_drift(move.json(), read_fixture(STORE, "session.json"), label="move")
    assert sheet.id == product.id
    assert_row(sheet)


# --------------------------------------------------------------- new arrivals


def test_new_arrivals_are_the_storefront_new_product_filter(
    alcampo: Alcampo, module_recorder: RecordingTransport
) -> None:
    arrivals = alcampo.get_new_arrivals()

    assert arrivals
    assert all(row.is_new for row in arrivals)
    request = module_recorder.last(LISTING_PATH).request
    assert request.url.params["filters"] == "dummyValue=new"
    assert "categoryId" not in request.url.params
    assert_no_drift(
        module_recorder.last_json(LISTING_PATH),
        read_fixture(STORE, "new_arrivals.json"),
        label="new arrivals",
        ignore=LISTING_OPTIONAL,
    )


# ------------------------------------------------------------------ categories


def test_get_category_returns_the_node_its_children_and_rows(
    alcampo: Alcampo,
    tree: tuple[AlcampoCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    fresh = next(root for root in tree if root.retailer_category_id == "OC2112")

    category = alcampo.get_category(fresh.children[0].id)

    assert category.id == fresh.children[0].id
    assert category.products
    for row in category.products:
        assert_row(row)
    assert_no_drift(
        module_recorder.last_json(LISTING_PATH),
        read_fixture(STORE, "category.json"),
        label="category listing",
        ignore=LISTING_OPTIONAL,
    )


# --------------------------------------------------------------------- offers


def test_promotions_return_rows_on_offer(
    alcampo: Alcampo, module_recorder: RecordingTransport
) -> None:
    page = alcampo.get_promotions(page_size=5)

    assert page.products
    for row in page.products:
        assert_row(row)
    assert all(row.promotions for row in page.products)
    request = module_recorder.last("/v1/pages/promotions").request
    assert request.url.params["regionId"] == DEFAULT_REGION_ID
    assert_no_drift(
        module_recorder.last_json("/v1/pages/promotions"),
        read_fixture(STORE, "promotions.json"),
        label="promotions",
        ignore=LISTING_OPTIONAL,
    )


def test_get_offers_reads_rows_on_promotion(alcampo: Alcampo) -> None:
    offers = alcampo.get_offers()

    assert offers
    assert any(row.promotions for row in offers)


# ----------------------------------------------------------------- extensions


def test_similar_and_related_products_are_resolved_to_rows(
    alcampo: Alcampo, product: AlcampoProduct, module_recorder: RecordingTransport
) -> None:
    similar = alcampo.get_similar(product.id)
    related = alcampo.get_related(product.id)

    for row in (*similar, *related):
        assert_row(row)
    raw = module_recorder.last_json(f"{PAGES}/v5/products/similar")
    assert isinstance(raw, list)
    if not similar and not related:
        pytest.skip("the storefront related nothing to this product")
    put = module_recorder.last(f"{PAGES}/v6/products", method="PUT")
    assert put.request.headers["X-CSRF-TOKEN"]
    assert_no_drift(
        put.json(),
        read_fixture(STORE, "batch.json"),
        label="batch resolve",
        ignore=tuple(f"$.products[].{key}" for key in ROW_OPTIONAL),
    )


def test_suggest_returns_strings(
    alcampo: Alcampo, module_recorder: RecordingTransport
) -> None:
    suggestions = alcampo.suggest("lech")

    assert suggestions
    assert all(isinstance(item, str) and item for item in suggestions)
    assert_no_drift(
        module_recorder.last_json("/suggestions/primary"),
        read_fixture(STORE, "suggestions.json"),
        label="suggestions",
    )


# --------------------------------------------------------------------- stores


def test_list_stores_returns_points_and_their_regions(
    stores: tuple[AlcampoStore, ...], module_recorder: RecordingTransport
) -> None:
    assert len(stores) > 100
    assert DEFAULT_REGION_ID in {store.region_id for store in stores}
    assert all(store.region_id for store in stores)
    assert_no_drift(
        module_recorder.last_json("/v4/delivery-addresses"),
        read_fixture(STORE, "stores.json"),
        label="collection points",
    )


# ---------------------------------------------------------------- page tokens


def test_the_page_token_continues_the_search(
    alcampo: Alcampo,
    first_page: AlcampoSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    second = alcampo.search_products(QUERY, page_size=5, cursor=first_page.next_cursor)

    assert second.products
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }
    assert_no_drift(
        module_recorder.last_json(SEARCH_PATH),
        read_fixture(STORE, "listing_page2.json"),
        label="second page",
        ignore=LISTING_OPTIONAL,
    )


# --------------------------------------------------------------------- errors


def test_a_missing_product_is_not_found(
    alcampo: Alcampo, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        alcampo.get_product("1")

    assert_no_drift(
        module_recorder.last_json(f"{PAGES}/v5/products/bop"),
        read_fixture(STORE, "product_not_found.json"),
        label="missing product",
    )


def test_an_unknown_category_is_not_found(
    alcampo: Alcampo, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        alcampo.get_category("00000000-0000-4000-8000-000000000000")

    exchange = module_recorder.last(LISTING_PATH)
    assert exchange.response.status_code == 404
    assert_no_drift(
        exchange.json(),
        read_fixture(STORE, "category_not_found.json"),
        label="unknown category",
    )


# ---------------------------------------------------------------------- image


def test_download_fetches_one_photo(
    alcampo: Alcampo, product: AlcampoProduct, tmp_path: Path
) -> None:
    path = alcampo.download(product.photos[0], tmp_path / "photo.jpg")

    assert path.read_bytes()[:2] == b"\xff\xd8"
