"""live contract tests for carrefour, against www.carrefour.es and its index.

covers every public method once: the postal-code binding, the drive directory
and the shops near a postcode, search with its offset cursor and a sort, the
autocomplete, product detail through its canonical redirect, a missing
product, the barcode lookup, the stock figure from the proxied search, the
department menu with every department's aisles, a leaf, an aisle, an offset
page and an unknown category, one listing page by url, the home carousels,
both image downloads, and a catalan and an english search.

one module-scoped client makes about forty requests: seven rendered pages of
250 to 500 kb counting the warm-up, two redirects, eleven small menu calls,
five storefront json calls, eleven index calls and two images, about three
megabytes in all. the catalog is not covered: carrefour's sitemaps answer
every client with cloudflare's interactive challenge since october 2026.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from supermercapy import Carrefour, Language, NotFoundError
from supermercapy._core.html import extract_json_assignment
from supermercapy.carrefour import (
    CarrefourCategory,
    CarrefourPhoto,
    CarrefourProduct,
    CarrefourSearchResult,
)
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "carrefour"
POSTAL_CODE = "28232"
QUERY = "leche"
INDEX = "api.empathy.co"
SITE = "www.carrefour.es"
MENU_PATH = "/cloud-api/categories-api/v1/categories/menu"
STATE_MARKER = "window.__INITIAL_STATE__"

# what only some search documents carry: dietary badges
DOCUMENT_OPTIONAL = ("$.catalog.content[].info_tags",)
# what only some listing cards carry: an ad id inside the grid, purchase
# limits, a struck-through price, the promotion badges, a pack unit and
# dietary badges
CARD_OPTIONAL = (
    "citrus_ad_id",
    "restrictions",
    "strikethrough_price",
    "badge",
    "badge_map",
    "sell_pack_unit",
    "info_tags",
)
LISTING_OPTIONAL = tuple(
    f"$.productCardList.results.items[].{key}" for key in CARD_OPTIONAL
)


def page_state(recorder: RecordingTransport, path: str) -> Any:
    """return the rendered state of the newest page whose path holds ``path``."""

    response = recorder.last(path, host=SITE).response
    return extract_json_assignment(response.text, STATE_MARKER)


def fixture_state(name: str) -> Any:
    return extract_json_assignment(read_fixture(STORE, name), STATE_MARKER)


def carousel_documents(state: Any) -> list[Any]:
    content = state["cms"]["pageModel"]["content"]
    return [
        document
        for document in content.values()
        if document.get("documentType") == "featuredProducts"
    ]


def menu_json(recorder: RecordingTransport, current: str) -> Any:
    for exchange in recorder.find(MENU_PATH, host=SITE):
        if exchange.request.url.params.get("current_category") == current:
            return exchange.json()
    raise AssertionError(f"no menu request for {current} was recorded")


def is_image(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8")
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        or data[4:12] in {b"ftypavif", b"ftypavis"}
    )


def assert_row(product: CarrefourProduct) -> None:
    assert isinstance(product.id, str) and product.id
    assert product.name
    assert product.price.currency == "EUR"
    assert isinstance(product.price.amount, Decimal)
    assert product.price.amount > 0


@pytest.fixture(scope="module")
def carrefour(module_recorder: RecordingTransport) -> Iterator[Carrefour]:
    with Carrefour.from_postal_code(
        POSTAL_CODE, **live_options(module_recorder)
    ) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(carrefour: Carrefour) -> CarrefourSearchResult:
    return carrefour.search_products(QUERY, page_size=5)


@pytest.fixture(scope="module")
def product(carrefour: Carrefour, first_page: CarrefourSearchResult) -> Any:
    return carrefour.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def tree(carrefour: Carrefour) -> tuple[CarrefourCategory, ...]:
    return carrefour.get_categories(deep=True)


@pytest.fixture(scope="module")
def aisle(tree: tuple[CarrefourCategory, ...]) -> CarrefourCategory:
    # the first child of a department is often an offers link to a filtered
    # listing; the first one with a slug path is a real aisle
    return next(
        child for department in tree for child in department.children if child.slug_path
    )


@pytest.fixture(scope="module")
def aisle_page(carrefour: Carrefour, aisle: CarrefourCategory) -> CarrefourCategory:
    return carrefour.get_category(aisle.id)


# --------------------------------------------------------------------- binding


def test_from_postal_code_binds_a_drive(
    carrefour: Carrefour, module_recorder: RecordingTransport
) -> None:
    assert isinstance(carrefour.sale_point, str) and carrefour.sale_point
    assert carrefour.store_id == carrefour.sale_point
    assert_no_drift(
        module_recorder.last_json("/stores-location/", host=SITE),
        read_fixture(STORE, "stores_location.json"),
        label="stores location",
    )
    assert_no_drift(
        module_recorder.last_json("/drives", host=SITE),
        read_fixture(STORE, "drives.json"),
        label="drives",
    )


def test_list_stores_returns_the_drives_and_the_bound_one(
    carrefour: Carrefour,
) -> None:
    drives = carrefour.list_stores()

    assert drives
    assert carrefour.sale_point in {drive.id for drive in drives}
    assert {drive.kind for drive in drives} == {"drive"}
    assert all(drive.postal_code for drive in drives)


def test_list_stores_near_a_postcode_returns_shops(carrefour: Carrefour) -> None:
    shops = carrefour.list_stores(POSTAL_CODE)

    assert shops
    assert {shop.kind for shop in shops} == {"store"}
    distances = [shop.distance_km for shop in shops if shop.distance_km is not None]
    assert distances == sorted(distances)


# ---------------------------------------------------------------------- search


def test_search_returns_priced_rows_and_an_offset_cursor(
    first_page: CarrefourSearchResult, module_recorder: RecordingTransport
) -> None:
    assert len(first_page.products) == 5
    for row in first_page.products:
        assert_row(row)
        assert row.ean
        assert row.url and row.url.startswith(f"https://{SITE}/supermercado/")
    assert first_page.total_hits is not None and first_page.total_hits > 5
    assert first_page.next_cursor == "5"
    assert not first_page.truncated
    assert_no_drift(
        module_recorder.last_json("/search", host=INDEX),
        read_fixture(STORE, "search.json"),
        label="search",
        ignore=DOCUMENT_OPTIONAL,
    )


def test_the_cursor_continues_at_the_next_offset(
    carrefour: Carrefour, first_page: CarrefourSearchResult
) -> None:
    second = carrefour.search_products(
        QUERY, page_size=5, cursor=first_page.next_cursor
    )

    assert second.offset == 5
    assert second.products
    assert second.next_cursor == "10"
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }


def test_sort_is_applied_upstream(carrefour: Carrefour) -> None:
    page = carrefour.search_products(QUERY, page_size=10, sort="price asc")

    prices = [row.price.amount for row in page.products]
    assert all(isinstance(price, Decimal) for price in prices)
    assert prices == sorted(prices)  # type: ignore[type-var]


def test_suggest_returns_completions(
    carrefour: Carrefour, module_recorder: RecordingTransport
) -> None:
    completions = carrefour.suggest("lech")

    assert completions
    assert all(isinstance(item, str) and item for item in completions)
    assert_no_drift(
        module_recorder.last_json("/empathize", host=INDEX),
        read_fixture(STORE, "empathize.json"),
        label="empathize",
    )


# --------------------------------------------------------------------- product


def test_get_product_follows_the_canonical_redirect(
    product: CarrefourProduct,
    first_page: CarrefourSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    assert product.id == first_page.products[0].id
    assert_row(product)
    assert product.ean == first_page.products[0].ean
    assert product.slug
    assert product.photos
    assert product.category_path
    assert all(node.id.startswith("cat") for node in product.category_path)
    assert product.nutrition is not None
    redirect = module_recorder.last(f"/p/R-{product.id}/p", host=SITE)
    assert redirect.response.status_code in {301, 302}
    assert_no_drift(
        page_state(module_recorder, f"/R-{product.id}/p"),
        fixture_state("product.html"),
        label="product page",
        # only some products print a nutri-score label or declare allergens
        ignore=(
            "$.pdp.product.nutri_score",
            "$.pdp.product.nutrition_info.alergenos",
        ),
    )


def test_a_missing_product_is_not_found(
    carrefour: Carrefour, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError, match="away from the catalog"):
        carrefour.get_product("999999999")

    redirect = module_recorder.last("/p/R-999999999/p", host=SITE)
    assert redirect.response.status_code in {301, 302}


def test_get_product_by_ean_finds_the_same_product(
    carrefour: Carrefour, product: CarrefourProduct
) -> None:
    assert product.ean is not None

    found = carrefour.get_product_by_ean(product.ean)

    assert found.id == product.id
    assert found.ean == product.ean


def test_get_stock_reads_the_proxied_search(
    carrefour: Carrefour,
    first_page: CarrefourSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    stock = carrefour.get_stock(first_page.products[0].id)

    assert stock is None or (isinstance(stock, int) and stock >= 0)
    exchange = module_recorder.last("/search-api/query/v1/search", host=SITE)
    assert exchange.request.url.params["session"]
    assert_no_drift(
        exchange.json(),
        read_fixture(STORE, "proxy_search.json"),
        label="proxied search",
    )


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_departments_and_their_aisles(
    tree: tuple[CarrefourCategory, ...], module_recorder: RecordingTransport
) -> None:
    assert len(tree) >= 5
    for department in tree:
        assert department.id.startswith("cat")
        assert department.name
        assert department.level == 1
        assert department.parent_id is None
        for child in department.children:
            assert child.parent_id == department.id
            assert child.level == 2
    assert sum(len(department.children) for department in tree) > 20
    assert_no_drift(
        menu_json(module_recorder, "foodRootCategory"),
        read_fixture(STORE, "menu.json"),
        label="department menu",
    )
    with_aisles = next(department for department in tree if department.children)
    assert_no_drift(
        menu_json(module_recorder, with_aisles.id),
        read_fixture(STORE, "menu_despensa.json"),
        label="aisle menu",
    )


def test_get_category_returns_an_aisle_with_its_leaves(
    aisle: CarrefourCategory,
    aisle_page: CarrefourCategory,
    module_recorder: RecordingTransport,
) -> None:
    category = aisle_page

    assert category.id == aisle.id
    assert category.name == aisle.name
    assert category.level == 2
    assert category.children
    assert all(child.parent_id == aisle.id for child in category.children)
    assert category.products
    for row in category.products:
        assert_row(row)
    assert category.product_count is not None
    assert category.product_count >= len(category.products)
    assert_no_drift(
        page_state(module_recorder, f"/c/{aisle.id}/c"),
        fixture_state("category_aisle.html"),
        label="aisle page",
        ignore=LISTING_OPTIONAL,
    )


def test_get_category_returns_a_leaf_without_children(
    carrefour: Carrefour,
    aisle: CarrefourCategory,
    aisle_page: CarrefourCategory,
    module_recorder: RecordingTransport,
) -> None:
    leaf_id = aisle_page.children[0].id

    leaf = carrefour.get_category(leaf_id)

    assert leaf.id == leaf_id
    assert leaf.parent_id == aisle.id
    assert leaf.level == 3
    assert leaf.children == ()
    assert leaf.url is not None and leaf.url.endswith(f"/{leaf_id}/c")
    assert leaf.products
    assert_no_drift(
        page_state(module_recorder, f"/c/{leaf_id}/c"),
        fixture_state("listing.html"),
        label="leaf page",
        ignore=LISTING_OPTIONAL,
    )


def test_a_category_offset_reaches_the_next_page(
    carrefour: Carrefour, aisle_page: CarrefourCategory
) -> None:
    second = carrefour.get_category(aisle_page.id, offset=24)

    assert second.products
    assert second.product_count == aisle_page.product_count
    assert not {row.id for row in second.products} & {
        row.id for row in aisle_page.products
    }


def test_an_unknown_category_is_not_found(carrefour: Carrefour) -> None:
    with pytest.raises(NotFoundError):
        carrefour.get_category("cat99999999")


def test_get_category_products_reads_one_listing_page_by_url(
    carrefour: Carrefour, aisle: CarrefourCategory
) -> None:
    assert aisle.url is not None

    listing = carrefour.get_category_products(aisle.url, offset=24)

    assert listing.offset == 24
    assert listing.page_size == 24
    assert listing.products
    assert listing.sort_options


# ------------------------------------------------------------------------ home


def test_get_home_reads_the_cms_carousels(
    carrefour: Carrefour, module_recorder: RecordingTransport
) -> None:
    sections = carrefour.get_home()

    assert sections
    for section in sections:
        assert section.layout == "featuredProducts"
        assert section.title
        assert section.id
        assert section.products
        for row in section.products:
            assert row.id and row.name
    live = page_state(module_recorder, "/supermercado")
    fixture = fixture_state("home.html")
    assert_no_drift(
        live["cms"]["featured_products"],
        fixture["cms"]["featured_products"],
        label="home carousels",
        ignore=(
            *(f"$[].products[].{key}" for key in CARD_OPTIONAL),
            # a carousel shows a banner only when the cms gives it one
            "$[].banner",
        ),
    )
    # the cms documents are keyed by id, which changes whenever the page is
    # edited, so their shape is compared as a list
    assert_no_drift(
        carousel_documents(live),
        carousel_documents(fixture),
        label="home carousel documents",
        # only an automatic offers carousel names its departments
        ignore=("$[].categoryId",),
    )


# ---------------------------------------------------------------------- images


def test_download_and_download_photo(
    carrefour: Carrefour, first_page: CarrefourSearchResult, tmp_path: Path
) -> None:
    thumbnail = first_page.products[0].thumbnail
    assert isinstance(thumbnail, CarrefourPhoto)

    original = carrefour.download(thumbnail, tmp_path / "thumb.jpg")
    resized = carrefour.download_photo(thumbnail, tmp_path / "200.jpg", width=200)

    # the resizer answers an image accept header with webp or avif behind
    # the .jpg name, and a bare request with a jpeg
    assert is_image(original.read_bytes())
    assert is_image(resized.read_bytes())


# ------------------------------------------------------------------- languages


@pytest.mark.parametrize(("language", "query"), [("ca", "llet"), ("en", "milk")])
def test_the_other_languages_search_their_own_index(
    carrefour: Carrefour,
    module_recorder: RecordingTransport,
    language: str,
    query: str,
) -> None:
    with Carrefour(
        carrefour.sale_point,
        language=language,
        **live_options(module_recorder),
    ) as client:
        page = client.search_products(query, page_size=5)

    assert client.language == Language(language)
    assert page.products
    for row in page.products:
        assert_row(row)
    assert (
        module_recorder.last("/search", host=INDEX).request.url.params["lang"]
        == language
    )


def test_the_catalan_menu_names_departments_in_catalan(
    carrefour: Carrefour,
    tree: tuple[CarrefourCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    with Carrefour(
        carrefour.sale_point, language="ca", **live_options(module_recorder)
    ) as client:
        departments = client.get_categories()

    assert {node.id for node in departments} == {node.id for node in tree}
    assert {node.name for node in departments} != {node.name for node in tree}
