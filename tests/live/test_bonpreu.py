"""live contract tests for bonpreu, against www.compraonline.bonpreuesclat.cat.

covers every public method once: the category tree, search, product detail,
the similar and related products with the csrf-gated batch resolve behind
them, the promotions listing and the offers, the "novetats" new arrivals walked
by page token, one category, the suggestions, a missing product, an unknown
category, one image download, a spanish product sheet, a second search page
from the page token, and a token the session never issued. bonpreu has no
catalog, postcode, store list, ean lookup or home page, so there is nothing
else to call.

the shop's aws waf budget is the constraint. in the october 2026 audit it
challenged every catalogue path after eight to thirteen requests from one
address, and for at least half an hour after that. so one module-scoped client
at the default two-second pacing makes about twenty requests, ordered by what
a broken contract would cost: the tree, search, the sheet and the batch resolve
first, the error shapes and page tokens last. expect the tail of the module to
skip as inconclusive on a cold run, and run it at most once per half hour from
one address.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from supermercapy import BlockedError, Bonpreu, Language, NotFoundError, TransportError
from supermercapy.bonpreu import (
    BonpreuCategory,
    BonpreuProduct,
    BonpreuSearchResult,
)
from supermercapy.bonpreu._constants import NEW_ARRIVALS_CATEGORY_ID
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "bonpreu"
QUERY = "llet"
PAGES = "/api/webproductpagews"
SEARCH_PATH = f"{PAGES}/v6/product-pages/search"
LISTING_PATH = f"{PAGES}/v6/product-pages"
ROW = "$.productGroups[].decoratedProducts[]"

# what only some rows carry: an advert and its campaign on a sponsored row,
# promotions and a promotional price, a pack size, and a brand
ROW_OPTIONAL = (
    "externalAdvertId",
    "featuredProductCampaign",
    "promotions",
    "promoPrice",
    "promoUnitPrice",
    "packSizeDescription",
    "brand",
)
# and on a listing, the campaign a featured group was bought by
LISTING_OPTIONAL = (
    *(f"{ROW}.{key}" for key in ROW_OPTIONAL),
    "$.productGroups[].promotedProductsCampaignId",
    "$.productGroups[].promotedProductsCampaignName",
)


@pytest.fixture(scope="module")
def bonpreu(module_recorder: RecordingTransport) -> Iterator[Bonpreu]:
    with Bonpreu(**live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def tree(bonpreu: Bonpreu) -> tuple[BonpreuCategory, ...]:
    return bonpreu.get_categories()


@pytest.fixture(scope="module")
def first_page(bonpreu: Bonpreu) -> BonpreuSearchResult:
    return bonpreu.search_products(QUERY, page_size=5)


@pytest.fixture(scope="module")
def organic(first_page: BonpreuSearchResult) -> BonpreuProduct:
    return next(row for row in first_page.products if not row.is_sponsored)


@pytest.fixture(scope="module")
def product(bonpreu: Bonpreu, organic: BonpreuProduct) -> BonpreuProduct:
    return bonpreu.get_product(organic.id)


def assert_row(row: BonpreuProduct) -> None:
    assert isinstance(row.id, str) and row.id.isdigit()
    assert row.name
    assert row.ean is None
    assert row.product_uuid
    assert row.price.currency == "EUR"
    assert isinstance(row.price.amount, Decimal)
    assert row.price.amount > 0


def reraise_refusal(error: BaseException) -> None:
    """let a waf refusal reach the live conftest, which skips the test."""

    if isinstance(error, BlockedError):
        raise error


# ------------------------------------------------------------------ the tree


def test_get_categories_returns_the_whole_tree(
    tree: tuple[BonpreuCategory, ...], module_recorder: RecordingTransport
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
    first_page: BonpreuSearchResult, module_recorder: RecordingTransport
) -> None:
    assert first_page.products
    for row in first_page.products:
        assert_row(row)
    assert first_page.total_hits is None
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
    product: BonpreuProduct,
    organic: BonpreuProduct,
    module_recorder: RecordingTransport,
) -> None:
    assert product.id == organic.id
    assert product.product_uuid == organic.product_uuid
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
        ),
    )


def test_similar_products_are_resolved_to_rows(
    bonpreu: Bonpreu, organic: BonpreuProduct, module_recorder: RecordingTransport
) -> None:
    similar = bonpreu.get_similar(organic.id)

    raw = module_recorder.last_json(f"{PAGES}/v5/products/similar")
    assert isinstance(raw, list)
    assert len(similar) <= len(raw)
    for row in similar:
        assert_row(row)


def test_related_products_are_resolved_through_the_batch_put(
    bonpreu: Bonpreu, organic: BonpreuProduct, module_recorder: RecordingTransport
) -> None:
    related = bonpreu.get_related(organic.id)

    raw = module_recorder.last_json(f"{PAGES}/v5/products/related")
    assert isinstance(raw, list)
    for row in related:
        assert_row(row)
    if not raw:
        pytest.skip("the storefront related nothing to this product")
    put = module_recorder.last(f"{PAGES}/v6/products", method="PUT")
    assert put.request.headers["X-CSRF-TOKEN"]
    assert {row.product_uuid for row in related} <= set(raw)
    assert_no_drift(
        put.json(),
        read_fixture(STORE, "batch.json"),
        label="batch resolve",
        ignore=tuple(f"$.products[].{key}" for key in ROW_OPTIONAL),
    )
    # the csrf page is fetched once per client, however many puts follow
    homes = [item for item in module_recorder.exchanges if item.request.url.path == "/"]
    assert len(homes) == 1


# --------------------------------------------------------------------- offers


def test_promotions_return_a_page_and_a_token(
    bonpreu: Bonpreu, module_recorder: RecordingTransport
) -> None:
    page = bonpreu.get_promotions(page_size=5)

    assert page.products
    for row in page.products:
        assert_row(row)
        assert row.price.unit_name
    assert any(row.promotions for row in page.products)
    assert page.next_cursor
    assert_no_drift(
        module_recorder.last_json("/v1/pages/promotions"),
        read_fixture(STORE, "promotions.json"),
        label="promotions",
        ignore=LISTING_OPTIONAL,
    )


# ---------------------------------------------------------------- new arrivals


def test_new_arrivals_are_the_whole_novetats_category(
    bonpreu: Bonpreu, module_recorder: RecordingTransport
) -> None:
    start = len(module_recorder.exchanges)

    arrivals = bonpreu.get_new_arrivals()

    walk = module_recorder.exchanges[start:]
    first = walk[0].json()
    current = first["additionalPageInfo"]["currentCategory"]
    assert current["categoryId"] == NEW_ARRIVALS_CATEGORY_ID
    assert arrivals
    assert all(row.is_new for row in arrivals)
    assert len(arrivals) == current["productCount"]
    # three hundred rows a page
    assert len(walk) == -(-current["productCount"] // 300)
    assert_no_drift(
        first,
        read_fixture(STORE, "new_arrivals.json"),
        label="new arrivals",
        ignore=LISTING_OPTIONAL,
    )


# ------------------------------------------------------------------ categories


def test_get_category_returns_the_node_its_children_and_rows(
    bonpreu: Bonpreu,
    tree: tuple[BonpreuCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    parent = next(root for root in tree if root.children).children[0]

    category = bonpreu.get_category(parent.id)

    assert category.id == parent.id
    assert category.products
    for row in category.products:
        assert_row(row)
    assert_no_drift(
        module_recorder.last_json(LISTING_PATH),
        read_fixture(STORE, "category.json"),
        label="category listing",
        ignore=LISTING_OPTIONAL,
    )


# ----------------------------------------------------------------- extensions


def test_suggest_returns_strings(
    bonpreu: Bonpreu, module_recorder: RecordingTransport
) -> None:
    suggestions = bonpreu.suggest("lle")

    assert suggestions
    assert all(isinstance(item, str) and item for item in suggestions)
    assert_no_drift(
        module_recorder.last_json("/suggestions/primary"),
        read_fixture(STORE, "suggestions.json"),
        label="suggestions",
    )


def test_get_offers_reads_rows_on_promotion(bonpreu: Bonpreu) -> None:
    offers = bonpreu.get_offers()

    assert offers
    for row in offers:
        assert_row(row)
    assert any(row.promotions for row in offers)


# --------------------------------------------------------------------- errors


def test_a_missing_product_is_not_found(
    bonpreu: Bonpreu, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        bonpreu.get_product("1")

    assert_no_drift(
        module_recorder.last_json(f"{PAGES}/v5/products/bop"),
        read_fixture(STORE, "product_not_found.json"),
        label="missing product",
    )


def test_an_unknown_category_is_not_found(
    bonpreu: Bonpreu, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        bonpreu.get_category("00000000-0000-4000-8000-000000000000")

    exchange = module_recorder.last(LISTING_PATH)
    assert exchange.response.status_code == 404
    assert_no_drift(
        exchange.json(),
        read_fixture(STORE, "category_not_found.json"),
        label="unknown category",
    )


# ---------------------------------------------------------------------- image


def test_download_fetches_one_photo(
    bonpreu: Bonpreu, product: BonpreuProduct, tmp_path: Path
) -> None:
    path = bonpreu.download(product.photos[0], tmp_path / "photo.jpg")

    assert path.read_bytes()[:2] == b"\xff\xd8"


# ------------------------------------------------------------------- language


def test_a_spanish_client_reads_the_sheet_in_spanish(
    product: BonpreuProduct, module_recorder: RecordingTransport
) -> None:
    with Bonpreu(language="es", **live_options(module_recorder)) as spanish:
        assert spanish.language is Language.SPANISH
        sheet = spanish.get_product(product.id)

    request = module_recorder.last(f"{PAGES}/v5/products/bop").request
    assert "language=es-ES" in request.headers["Cookie"]
    assert sheet.id == product.id
    assert sheet.price.amount == product.price.amount
    assert sheet.fields


# ---------------------------------------------------------------- page tokens


def test_the_page_token_continues_the_search(
    bonpreu: Bonpreu, first_page: BonpreuSearchResult
) -> None:
    second = bonpreu.search_products(QUERY, page_size=5, cursor=first_page.next_cursor)

    assert second.products
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }


def test_a_token_the_session_never_issued_is_refused(
    bonpreu: Bonpreu, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(TransportError) as raised:
        bonpreu.search_products(QUERY, cursor="00000000-0000-4000-8000-000000000000")

    reraise_refusal(raised.value)
    assert raised.value.status_code == 401
    assert_no_drift(
        module_recorder.last_json(SEARCH_PATH),
        read_fixture(STORE, "page_token_expired.json"),
        label="unknown page token",
    )
