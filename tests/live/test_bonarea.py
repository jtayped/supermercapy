"""live contract tests for bonàrea, against www.bonarea-online.com.

covers every public method once: search with its client-side cursor and the
single-request stream, product detail in both id spellings, a missing
product, the product sheet and a missing one, the category tree, a listing
node, a menu node and an unknown category, a bounded catalog walk, the badge
filters for new products and price drops, the delivery zones of a province
and of one town, both image downloads at a cdn size, and a catalan search and
product. ``get_catalog`` is left out because it walks about 490 listings.

one module-scoped client makes about twenty-five requests: two language
warm-ups, four searches, the tree three times, six listings and a handful of
small posts and images, about three megabytes in all. bonàrea has shown no
rate limit, so the client keeps its default pacing.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from supermercapy import Bonarea, Language, NotFoundError
from supermercapy.bonarea import (
    BonareaCategory,
    BonareaPhoto,
    BonareaProduct,
    BonareaSearchResult,
    Characteristic,
    listing_categories,
    to_url_id,
)
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "bonarea"
QUERY = "leche entera"
CATALAN_QUERY = "llet sencera"
PROVINCE = "25"
TOWN = "Guissona"

# what only some articles carry: the id of the variant group they belong to
ARTICLE_OPTIONAL = ("$.articles[].idAgrupacio",)


def assert_row(product: BonareaProduct) -> None:
    assert isinstance(product.id, str) and "*" in product.id
    assert product.name
    assert product.price.currency == "EUR"
    assert isinstance(product.price.amount, Decimal)
    assert product.price.amount > 0
    assert product.ean is None


@pytest.fixture(scope="module")
def bonarea(module_recorder: RecordingTransport) -> Iterator[Bonarea]:
    with Bonarea(**live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(bonarea: Bonarea) -> BonareaSearchResult:
    return bonarea.search_products(QUERY, page_size=5)


@pytest.fixture(scope="module")
def product(bonarea: Bonarea, first_page: BonareaSearchResult) -> BonareaProduct:
    return bonarea.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def tree(bonarea: Bonarea) -> tuple[BonareaCategory, ...]:
    return bonarea.get_categories()


@pytest.fixture(scope="module")
def listing_node(tree: tuple[BonareaCategory, ...]) -> BonareaCategory:
    return listing_categories(tree)[0]


# ---------------------------------------------------------------------- search


def test_search_warms_the_language_and_slices_one_response(
    first_page: BonareaSearchResult, module_recorder: RecordingTransport
) -> None:
    assert len(first_page.products) == 5
    for row in first_page.products:
        assert_row(row)
    assert first_page.total_hits is not None and first_page.total_hits > 5
    assert first_page.next_cursor == "5"
    # the warm-up sets the session cookie search reads its language from
    warm_up = module_recorder.last("/es", method="GET")
    assert warm_up.response.status_code == 200
    assert (
        "ASP.NET_SessionId"
        in module_recorder.last("/shop/search").request.headers["Cookie"]
    )
    # a spanish session answers in spanish; a cold one answers in catalan
    assert any("leche" in row.name.lower() for row in first_page.products)
    assert_no_drift(
        module_recorder.last_json("/shop/search"),
        read_fixture(STORE, "search.json"),
        label="search",
    )


def test_the_cursor_slices_the_same_result_set(
    bonarea: Bonarea, first_page: BonareaSearchResult
) -> None:
    second = bonarea.search_products(QUERY, page_size=5, cursor=first_page.next_cursor)

    assert second.offset == 5
    assert second.total_hits == first_page.total_hits
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }


def test_iter_search_streams_every_hit_in_one_request(
    bonarea: Bonarea,
    first_page: BonareaSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    before = len(module_recorder.exchanges)

    hits = list(bonarea.iter_search(QUERY))

    assert len(module_recorder.exchanges) - before == 1
    assert len(hits) == first_page.total_hits


# --------------------------------------------------------------------- product


def test_get_product_reads_label_data(
    product: BonareaProduct,
    first_page: BonareaSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    assert product.id == first_page.products[0].id
    assert_row(product)
    assert product.photos
    assert product.category_path
    assert all(
        node.url and node.url.startswith("https://") for node in product.category_path
    )
    assert product.nutrition is not None
    assert product.nutrition.values
    assert product.raw_extended_info
    assert_no_drift(
        module_recorder.last_json("/shop/Article"),
        read_fixture(STORE, "product.json"),
        label="product",
    )


def test_the_url_spelling_of_an_id_reads_the_same_article(
    bonarea: Bonarea, product: BonareaProduct
) -> None:
    assert bonarea.get_product(to_url_id(product.id)).id == product.id


def test_a_missing_product_is_not_found(
    bonarea: Bonarea, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        bonarea.get_product("13*9999999")

    assert_no_drift(
        module_recorder.last_json("/shop/Article"),
        read_fixture(STORE, "product_missing.json"),
        label="missing product",
    )


def test_get_product_sheet_returns_the_modal_html(
    bonarea: Bonarea,
    product: BonareaProduct,
    module_recorder: RecordingTransport,
) -> None:
    sheet = bonarea.get_product_sheet(product.id)

    assert sheet.strip().startswith("<")
    assert_no_drift(
        module_recorder.last_json("/shop/GetProductSheet"),
        read_fixture(STORE, "product_sheet.json"),
        label="product sheet",
    )


def test_a_missing_product_sheet_is_not_found(
    bonarea: Bonarea, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        bonarea.get_product_sheet("13*9999999")

    assert_no_drift(
        module_recorder.last_json("/shop/GetProductSheet"),
        read_fixture(STORE, "product_sheet_missing.json"),
        label="missing product sheet",
    )


# ------------------------------------------------------------------ categories


def test_get_categories_returns_the_nested_tree(
    tree: tuple[BonareaCategory, ...], module_recorder: RecordingTransport
) -> None:
    assert len(tree) > 5
    for root in tree:
        assert root.level == 1
        assert root.parent_id is None
        assert root.url and root.url.startswith("https://www.bonarea-online.com/")
    assert len(listing_categories(tree)) > 100
    assert_no_drift(
        module_recorder.last_json("/shop/ShoppingBody"),
        read_fixture(STORE, "tree.json"),
        label="tree",
    )


def test_get_category_lists_the_articles_of_a_listing_node(
    bonarea: Bonarea,
    listing_node: BonareaCategory,
    module_recorder: RecordingTransport,
) -> None:
    category = bonarea.get_category(listing_node.id)

    assert category.id == listing_node.id
    assert category.name == listing_node.name
    assert category.products
    for row in category.products:
        assert_row(row)
    assert category.product_count == len(category.products)
    assert_no_drift(
        module_recorder.last_json("/shop/ShoppingBody"),
        read_fixture(STORE, "category.json"),
        label="listing",
        ignore=ARTICLE_OPTIONAL,
    )


def test_a_menu_node_has_children_and_no_articles(
    bonarea: Bonarea,
    tree: tuple[BonareaCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    menu = next(child for root in tree for child in root.children if child.children)

    category = bonarea.get_category(menu.id)

    assert category.children
    assert category.products == ()
    assert category.product_count == 0
    assert_no_drift(
        module_recorder.last_json("/shop/ShoppingBody"),
        read_fixture(STORE, "category_menu.json"),
        label="menu listing",
    )


def test_an_unknown_category_is_not_found(
    bonarea: Bonarea, module_recorder: RecordingTransport
) -> None:
    before = len(module_recorder.exchanges)

    with pytest.raises(NotFoundError):
        bonarea.get_category("13*300*999")

    # the http 500 the reference throws is not retried; the tree confirms it
    walk = module_recorder.exchanges[before:]
    assert [item.response.status_code for item in walk] == [500, 200]
    assert "customErrors" in walk[0].response.text


def test_iter_catalog_walks_listings_lazily(
    bonarea: Bonarea, module_recorder: RecordingTransport
) -> None:
    before = len(module_recorder.exchanges)

    products = list(itertools.islice(bonarea.iter_catalog(), 5))

    assert len(products) == 5
    for row in products:
        assert_row(row)
    # the tree, then the first listing node, and nothing more
    assert len(module_recorder.exchanges) - before == 2


# ------------------------------------------------------------------ extensions


def test_the_badge_filters_read_one_listing_each(
    bonarea: Bonarea,
    listing_node: BonareaCategory,
    module_recorder: RecordingTransport,
) -> None:
    before = len(module_recorder.exchanges)

    new = bonarea.new_products(listing_node.id)
    reduced = bonarea.price_drops(listing_node.id)

    assert len(module_recorder.exchanges) - before == 2
    assert all(row.has(Characteristic.NEW) and row.is_new for row in new)
    assert all(row.has(Characteristic.PRICE_DROP) for row in reduced)
    assert all(row.price.is_discounted for row in reduced)


def test_delivery_zones_of_a_province_and_a_town(
    bonarea: Bonarea, module_recorder: RecordingTransport
) -> None:
    province = bonarea.delivery_zones(PROVINCE)

    assert province
    assert all(len(code) == 5 and code.startswith(PROVINCE) for code in province)
    assert len(set(province)) == len(province)
    assert_no_drift(
        module_recorder.last_json("/shop/GetPostalCodes"),
        read_fixture(STORE, "postal_codes.json"),
        label="postal codes",
    )

    town = bonarea.delivery_zones(PROVINCE, TOWN)

    assert town
    assert set(town) <= set(province)


def test_download_and_download_photo_at_a_cdn_size(
    bonarea: Bonarea, product: BonareaProduct, tmp_path: Path
) -> None:
    photo = product.photos[0]
    assert isinstance(photo, BonareaPhoto)

    # the original upload is several megabytes, so both ask for a rendition
    plain = bonarea.download(photo.sized(200, 200), tmp_path / "plain.png")
    sized = bonarea.download_photo(photo, tmp_path / "sized.png", width=200, height=200)

    assert plain.read_bytes()
    assert sized.read_bytes()


# ------------------------------------------------------------------- languages


def test_catalan_search_and_product(
    module_recorder: RecordingTransport, product: BonareaProduct
) -> None:
    with Bonarea(language="ca", **live_options(module_recorder)) as client:
        page = client.search_products(CATALAN_QUERY, page_size=5)
        catalan = client.get_product(product.id)

    assert client.language == Language.CATALAN
    assert module_recorder.last("/ca", method="GET").response.status_code == 200
    assert page.products
    for row in page.products:
        assert_row(row)
    assert any("llet" in row.name.lower() for row in page.products)
    assert catalan.id == product.id
    assert catalan.name != product.name
    assert catalan.nutrition is not None
    assert_no_drift(
        module_recorder.last_json("/ca/shop/Article"),
        read_fixture(STORE, "product_ca.json"),
        label="catalan product",
    )
