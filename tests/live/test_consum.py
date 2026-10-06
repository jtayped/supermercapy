"""live contract tests for consum, against tienda.consum.es.

covers every public method once: the postal-code binding, the home-delivery and
pickup area lists, a postcode outside the delivery area, search with its offset
cursor, a sorted and filtered search, product detail, a missing product, the
batch read, the barcode lookup, the category tree, one category and a second
page of it, the campaign groups and one campaign's products, the advertised
sort orders, both suggestion endpoints, the immediate offers (walked to the
end) and one page of deferred ones, the new arrivals, a bounded catalog walk,
both image downloads, and a valencian product and search. ``get_catalog`` is
left out because it walks every one of about nine thousand products.

one module-scoped client makes about forty requests, eleven of them the offer
walk. consum has shown no rate limit, so the client keeps its default pacing.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from supermercapy import Consum, Language, NotFoundError, OutOfCoverageError
from supermercapy.consum import (
    ConsumCategory,
    ConsumProduct,
    ConsumSearchResult,
    DeliveryMethod,
    ImageSize,
    ProductFilters,
    SortOrder,
)
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "consum"
POSTAL_CODE = "46001"
OUTSIDE_POSTAL_CODE = "28001"
QUERY = "arroz"
LISTING_PATH = "/catalog/product"

# attribute widgets only some products carry: the badge drawn over the photo
# and the one under the tile
ROW_OPTIONAL = tuple(
    f"$.products[].productData.{parent}.{widget}"
    for parent in ("attributes[]", "attributeGroups[].attributes[]")
    for widget in ("detailProductImageTopRight", "widgetProductBottomRight")
)


@pytest.fixture(scope="module")
def consum(module_recorder: RecordingTransport) -> Iterator[Consum]:
    with Consum.from_postal_code(
        POSTAL_CODE, **live_options(module_recorder)
    ) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(consum: Consum) -> ConsumSearchResult:
    return consum.search_products(QUERY, page_size=5, include_filters=True)


@pytest.fixture(scope="module")
def product(consum: Consum, first_page: ConsumSearchResult) -> ConsumProduct:
    return consum.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def tree(consum: Consum) -> tuple[ConsumCategory, ...]:
    return consum.get_categories()


def assert_row(product: ConsumProduct) -> None:
    assert isinstance(product.id, str) and product.id
    assert product.name
    assert product.price.currency == "EUR"
    assert isinstance(product.price.amount, Decimal)
    assert product.price.amount > 0
    assert product.nutrition is None


# --------------------------------------------------------------------- binding


def test_from_postal_code_binds_a_zone(
    consum: Consum, module_recorder: RecordingTransport
) -> None:
    assert isinstance(consum.zone, int)
    assert consum.store_id == str(consum.zone)
    assert_no_drift(
        module_recorder.last_json("/shipping/area"),
        read_fixture(STORE, "shipping_area.json"),
        label="shipping area",
    )


def test_list_stores_returns_the_home_delivery_areas(consum: Consum) -> None:
    stores = consum.list_stores(POSTAL_CODE)

    assert stores
    assert consum.store_id in {store.id for store in stores}
    for store in stores:
        assert store.zone_id is not None
        assert store.delivery_method == DeliveryMethod.HOME
        assert store.kind == "delivery"


def test_list_stores_for_pickup_answers_a_tuple(consum: Consum) -> None:
    # empty for every postcode tried in october 2026; the contract is the type
    stores = consum.list_stores(POSTAL_CODE, method=DeliveryMethod.SHOP)

    assert isinstance(stores, tuple)
    assert all(store.kind == "pickup" for store in stores)


def test_a_postcode_outside_the_area_is_out_of_coverage(
    module_recorder: RecordingTransport,
) -> None:
    with pytest.raises(OutOfCoverageError):
        Consum.from_postal_code(OUTSIDE_POSTAL_CODE, **live_options(module_recorder))


# ---------------------------------------------------------------------- search


def test_search_returns_rows_facets_and_an_offset_cursor(
    first_page: ConsumSearchResult, module_recorder: RecordingTransport
) -> None:
    assert len(first_page.products) + first_page.dropped_sponsored == 5
    for row in first_page.products:
        assert_row(row)
        assert row.ean
    assert first_page.total_hits is not None and first_page.total_hits > 5
    assert first_page.next_cursor == "5"
    assert first_page.filter_groups
    assert_no_drift(
        module_recorder.last_json(LISTING_PATH),
        read_fixture(STORE, "listing.json"),
        label="search listing",
        ignore=ROW_OPTIONAL,
    )


def test_the_cursor_continues_at_the_next_offset(
    consum: Consum, first_page: ConsumSearchResult
) -> None:
    second = consum.search_products(QUERY, page_size=5, cursor=first_page.next_cursor)

    assert second.offset == 5
    assert second.products
    assert second.next_cursor == "10"
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }


def test_sorting_and_filtering_are_applied_upstream(consum: Consum) -> None:
    page = consum.search_products(
        QUERY,
        page_size=10,
        order_by=SortOrder.PRICE_ASC,
        filters=ProductFilters(offer_immediate=True),
    )

    assert page.products
    assert all(row.price.is_discounted for row in page.products)
    # the storefront sorts on the shelf price, which a discounted row keeps in
    # `previous`, not on the offer price in `amount`
    shelf = [row.price.previous for row in page.products]
    assert all(isinstance(amount, Decimal) for amount in shelf)
    assert shelf == sorted(shelf)  # type: ignore[type-var]


# --------------------------------------------------------------------- product


def test_get_product_reads_one_code(
    product: ConsumProduct,
    first_page: ConsumSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    assert product.id == first_page.products[0].id
    assert_row(product)
    assert product.ean and product.ean.isdigit()
    assert product.photos
    assert product.categories
    assert_no_drift(
        module_recorder.last_json(f"/catalog/product/code/{product.id}"),
        read_fixture(STORE, "product.json"),
        label="product",
        ignore=tuple(item.replace("$.products[]", "$") for item in ROW_OPTIONAL),
    )


def test_a_missing_product_is_not_found(
    consum: Consum, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError, match=f"zone {consum.zone}"):
        consum.get_product("1")

    exchange = module_recorder.last("/catalog/product/code/1")
    assert exchange.response.status_code == 404
    assert_no_drift(
        exchange.json(), read_fixture(STORE, "error_404.json"), label="404 body"
    )


def test_get_products_reads_a_batch_and_omits_unknown_codes(
    consum: Consum,
    first_page: ConsumSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    codes = [row.id for row in first_page.products]

    products = consum.get_products([*codes, "1"])

    assert [item.id for item in products] == codes
    assert_no_drift(
        module_recorder.last_json("/catalog/product/codes/"),
        read_fixture(STORE, "batch.json"),
        label="batch",
        ignore=tuple(item.replace("$.products[]", "$[]") for item in ROW_OPTIONAL),
    )


def test_get_product_by_ean_finds_the_same_product(
    consum: Consum, product: ConsumProduct
) -> None:
    assert product.ean is not None

    found = consum.get_product_by_ean(product.ean)

    assert found.id == product.id
    assert found.ean == product.ean


# ------------------------------------------------------------------ categories


def test_get_categories_returns_the_nested_tree(
    tree: tuple[ConsumCategory, ...], module_recorder: RecordingTransport
) -> None:
    assert tree
    assert all(isinstance(root.id, str) and root.id.isdigit() for root in tree)
    assert any(root.children for root in tree)
    assert_no_drift(
        module_recorder.last_json("/shopping/category/menu"),
        read_fixture(STORE, "menu.json"),
        label="category menu",
    )


def test_get_category_joins_the_node_with_its_listing(
    consum: Consum,
    tree: tuple[ConsumCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    node = tree[0].children[0]

    category = consum.get_category(node.id)

    assert category.id == node.id
    assert category.name == node.name
    assert category.products
    for row in category.products:
        assert_row(row)
    assert category.product_count is not None
    assert category.product_count >= len(category.products)
    assert_no_drift(
        module_recorder.last_json(LISTING_PATH),
        read_fixture(STORE, "category_listing.json"),
        label="category listing",
        ignore=ROW_OPTIONAL,
    )


def test_get_category_products_pages_by_offset(
    consum: Consum, tree: tuple[ConsumCategory, ...]
) -> None:
    node = tree[0].children[0]

    page = consum.get_category_products(node.id, page_size=5, cursor="5")

    assert page.offset == 5
    assert page.products
    assert page.next_cursor is None or int(page.next_cursor) > 5


# ------------------------------------------------------------------ extensions


def test_groups_and_one_campaign(
    consum: Consum, module_recorder: RecordingTransport
) -> None:
    groups = consum.get_groups()

    assert groups
    assert all(group.code and group.name for group in groups)
    assert_no_drift(
        module_recorder.last_json("/catalog/group"),
        read_fixture(STORE, "groups.json"),
        label="groups",
    )

    page = consum.get_group_products(groups[0].code, page_size=5)

    assert page.total_hits is not None
    for row in page.products:
        assert_row(row)


def test_get_sort_orders_lists_the_named_orders(
    consum: Consum, module_recorder: RecordingTransport
) -> None:
    orders = consum.get_sort_orders()

    assert {order.id for order in orders} >= {
        SortOrder.PRICE_ASC,
        SortOrder.OFFERS_FIRST,
        SortOrder.NEWEST,
    }
    assert_no_drift(
        module_recorder.last_json("/catalog/orders"),
        read_fixture(STORE, "orders.json"),
        label="sort orders",
    )


def test_both_suggestion_endpoints(
    consum: Consum, module_recorder: RecordingTransport
) -> None:
    completions = consum.suggest("lech")
    tags = consum.suggest_tags("leche")

    assert completions and all(item.query for item in completions)
    assert tags and all(item.query and item.tag for item in tags)
    assert_no_drift(
        module_recorder.last_json("/catalog/searcher/semantics"),
        read_fixture(STORE, "semantics.json"),
        label="semantics",
    )
    assert_no_drift(
        module_recorder.last_json("/catalog/product/tag/"),
        read_fixture(STORE, "tags.json"),
        label="tags",
    )


# ------------------------------------------------------- offers, new arrivals


def test_get_offers_walks_every_immediate_offer(
    consum: Consum, module_recorder: RecordingTransport
) -> None:
    start = len(module_recorder.exchanges)

    offers = consum.get_offers()

    walk = module_recorder.exchanges[start:]
    total = walk[0].json()["totalCount"]
    assert [item.request.url.params["offset"] for item in walk] == [
        str(offset) for offset in range(0, 100 * len(walk), 100)
    ]
    pages = -(-total // 100)
    # injected rows count towards the offset, so allow one page more
    assert pages <= len(walk) <= pages + 1
    assert offers
    # sponsored rows are dropped, so the walk keeps at most the reported total
    assert total * 0.8 < len(offers) <= total
    # a multi-buy offer can leave the shelf price alone, so most, not all
    assert sum(row.price.is_discounted for row in offers) > len(offers) * 0.8
    assert len({row.id for row in offers}) == len(offers)
    assert_no_drift(
        walk[0].json(),
        read_fixture(STORE, "offers_immediate.json"),
        label="immediate offers",
        ignore=ROW_OPTIONAL,
    )


def test_deferred_offers_credit_money_back(
    consum: Consum, module_recorder: RecordingTransport
) -> None:
    page = consum.search_products(
        "", page_size=5, filters=ProductFilters(offer_deferred=True)
    )

    assert page.products
    assert any(not offer.is_immediate for row in page.products for offer in row.offers)
    assert_no_drift(
        module_recorder.last_json(LISTING_PATH),
        read_fixture(STORE, "offers_deferred.json"),
        label="deferred offers",
        ignore=ROW_OPTIONAL,
    )


def test_get_new_arrivals_are_flagged_new(consum: Consum) -> None:
    arrivals = consum.get_new_arrivals()

    assert arrivals
    assert all(row.is_new for row in arrivals)


def test_iter_catalog_walks_offsets(consum: Consum) -> None:
    rows = list(itertools.islice(consum.iter_catalog(), 150))

    assert len(rows) == 150
    for row in rows[:5]:
        assert_row(row)


# ---------------------------------------------------------------------- images


def test_both_downloads(consum: Consum, product: ConsumProduct, tmp_path: Path) -> None:
    photo = product.photos[0]

    plain = consum.download(photo, tmp_path / "plain.jpg")
    large = consum.download_photo(photo, tmp_path / "large.jpg", size=ImageSize.HIGH)

    assert plain.read_bytes()[:2] == b"\xff\xd8"
    assert large.read_bytes()[:2] == b"\xff\xd8"
    assert large.stat().st_size > plain.stat().st_size


# -------------------------------------------------------------------- language


def test_valencian_names_come_back_in_valencian(
    consum: Consum, product: ConsumProduct, module_recorder: RecordingTransport
) -> None:
    with Consum(
        consum.zone, language="vl", **live_options(module_recorder)
    ) as valencian:
        assert valencian.language is Language.VALENCIAN
        translated = valencian.get_product(product.id)
        page = valencian.search_products("arròs", page_size=3)

    assert translated.id == product.id
    assert translated.name
    assert translated.price.amount == product.price.amount
    assert page.products
