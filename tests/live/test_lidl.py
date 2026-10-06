"""live contract tests for lidl, against www.lidl.es and the two schwarz apis.

covers every public method once: the postal-code binding and the store list
on the schwarz stores api, search with its offset cursor in both product
families, product detail for a shop and a grocery id, a missing product, the
category facet with and without a selected category, the gzipped product
sitemap and a bounded hydration of it, this week's offers and next week's
campaign with its future prices, the leaflet overview and one food and one
bazar leaflet, and one image download. lidl ships spanish only, so there is no
language sweep, and ``get_catalog`` is left out because it hydrates every one
of about seven thousand sitemap ids.

one module-scoped client makes about twenty-five requests. two of them are
campaign pages of one to two and a half megabytes each, and the sitemap is
fetched twice at about a hundred and twenty kilobytes.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from supermercapy import Lidl, NotFoundError
from supermercapy._core.html import attribute_values
from supermercapy.lidl import (
    LidlCategory,
    LidlLeaflet,
    LidlProduct,
    OfferWeek,
    PriceZone,
    ProductFamily,
)
from supermercapy.lidl._constants import OFFER_CAMPAIGNS
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "lidl"
POSTAL_CODE = "08013"
GROCERY_QUERY = "chocolate"
SHOP_QUERY = "taladro"
SEARCH_PATH = "/q/api/search"

# fields a tile or a detail carries only for some products: a variant's parent
# and variant ids, a brand, ratings, a member price, a promotional price
# window, a discount and the price it cuts, which a tile priced for next week
# lacks, and the store-availability preview of a product not yet on sale.
PRODUCT_OPTIONAL = (
    "brand",
    "categorySecondaryPath",
    "parentId",
    "variantId",
    "ratings",
    "keyfacts.description",
    "price.discount",
    "price.hasZeroVat",
    "price.oldPrice",
    "price.showEndDate",
    "price.startDate",
    "price.tax",
    "regionsPrices[].currentLidlPlusPrice",
    "regionsPrices[].currentPrice.discount",
    "regionsPrices[].currentPrice.oldPrice",
    "storeFacts.showStoreAvailability",
    "storeFacts.showStoreAvailabilityPreview",
    "storeFacts.storeStartDate",
)

# objects keyed by image hash, price band, region id, or leaflet product uuid
KEYED = frozenset({"imageMap", "regionsPrices", "regionsV2", "products"})


def keyed_as_list(document: Any, keys: frozenset[str] = KEYED) -> Any:
    """turn every object under one of ``keys`` into a list of its values.

    these maps are keyed by hashes or ids that differ per product, so the
    drift check compares their entries rather than their keys.
    """

    if isinstance(document, list):
        return [keyed_as_list(item, keys) for item in document]
    if not isinstance(document, dict):
        return document
    return {
        key: (
            [keyed_as_list(item, keys) for item in value.values()]
            if key in keys and isinstance(value, dict)
            else keyed_as_list(value, keys)
        )
        for key, value in document.items()
    }


def optional(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}.{path}" for path in PRODUCT_OPTIONAL)


def tiles(page: str) -> list[Any]:
    return [
        json.loads(value) for value in attribute_values(page, "div", "data-grid-data")
    ]


@pytest.fixture(scope="module")
def lidl(module_recorder: RecordingTransport) -> Iterator[Lidl]:
    with Lidl.from_postal_code(POSTAL_CODE, **live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def grocery_page(lidl: Lidl) -> Any:
    return lidl.search_products(GROCERY_QUERY, page_size=5)


@pytest.fixture(scope="module")
def shop_page(lidl: Lidl) -> Any:
    return lidl.search_products(SHOP_QUERY, page_size=5)


def first_of(page: Any, family: ProductFamily) -> LidlProduct:
    for product in page.products:
        if product.family is family:
            return product
    pytest.fail(f"no {family.value} product in the live page for {page.query!r}")


# ---------------------------------------------------------------------- binding


def test_postal_code_binds_a_region_and_a_zone(
    lidl: Lidl, module_recorder: RecordingTransport
) -> None:
    assert isinstance(lidl.region, int)
    assert lidl.store_id == str(lidl.region)
    assert lidl.zone in {zone.value for zone in PriceZone}
    assert lidl.store is not None
    assert lidl.store.postal_code == POSTAL_CODE
    page = module_recorder.last_json("/stores", host="live.api.schwarz")
    assert_no_drift(page, read_fixture(STORE, "stores_page1.json"), label="stores")


def test_list_stores_pages_the_whole_country(lidl: Lidl) -> None:
    stores = lidl.list_stores()

    assert len(stores) > 250, "the store list is paged 250 at a time"
    assert len({store.id for store in stores}) == len(stores)
    assert all(store.id.startswith("ES") for store in stores)
    assert any(store.postal_code == POSTAL_CODE for store in stores)
    assert all(
        isinstance(store.offer_region, int) for store in stores if store.offer_region
    )


# ----------------------------------------------------------------------- search


def test_search_pages_by_offset_and_the_cursor_round_trips(
    lidl: Lidl, grocery_page: Any
) -> None:
    assert grocery_page.products
    assert grocery_page.total_hits is not None
    assert grocery_page.total_hits > len(grocery_page.products)
    assert grocery_page.next_cursor == str(len(grocery_page.products))

    second = lidl.search_products(
        GROCERY_QUERY, page_size=5, cursor=grocery_page.next_cursor
    )

    assert second.offset == int(grocery_page.next_cursor)
    assert second.products
    assert {product.id for product in second.products}.isdisjoint(
        product.id for product in grocery_page.products
    )


def test_search_covers_both_families_and_matches_its_fixture(
    module_recorder: RecordingTransport, grocery_page: Any, shop_page: Any
) -> None:
    grocery = first_of(grocery_page, ProductFamily.GROCERY)
    shop = first_of(shop_page, ProductFamily.SHOP)

    for product in (grocery, shop):
        assert isinstance(product.id, str) and product.id.isdigit()
        assert product.name
        assert product.price.currency == "EUR"
        assert product.thumbnail is not None
    assert shop.price.amount is not None and shop.price.amount > Decimal(0)
    assert shop.ean is not None

    pages = [
        exchange.json()
        for exchange in module_recorder.find(SEARCH_PATH)
        if exchange.request.url.params.get("q") in {GROCERY_QUERY, SHOP_QUERY}
    ]
    merged = {**pages[0], "items": [item for page in pages for item in page["items"]]}
    assert_no_drift(
        keyed_as_list(merged),
        keyed_as_list(read_fixture(STORE, "search.json")),
        label="search",
        ignore=optional("$.items[].gridbox.data"),
    )


# ---------------------------------------------------------------------- product


def test_a_grocery_product_reads_its_region_band(
    lidl: Lidl, module_recorder: RecordingTransport, grocery_page: Any
) -> None:
    wanted = first_of(grocery_page, ProductFamily.GROCERY)

    product = lidl.get_product(wanted.id)

    assert product.id == wanted.parent_id
    assert product.family is ProductFamily.GROCERY
    assert product.region(lidl.store_id or "") is not None
    assert product.photos
    assert product.nutrition is None
    if product.price.amount is not None:
        assert product.price.currency == "EUR"
        assert product.price_band_id is not None
    assert_no_drift(
        keyed_as_list(module_recorder.last_json("/p/api/detail")),
        keyed_as_list(read_fixture(STORE, "product_grocery.json")),
        label="grocery product",
        ignore=optional("$"),
    )


def test_a_shop_product_reads_its_delivery_zone(
    lidl: Lidl, module_recorder: RecordingTransport, shop_page: Any
) -> None:
    wanted = first_of(shop_page, ProductFamily.SHOP)

    product = lidl.get_product(wanted.id)

    assert product.family is ProductFamily.SHOP
    assert product.zone(lidl.zone) is not None
    assert product.price.amount is not None
    assert product.price.currency == "EUR"
    assert product.ean is not None
    # the tile and the detail spell the brand in two places
    assert product.brand == wanted.brand
    assert len(product.photos) >= 1
    assert product.photos[0].large_url is not None
    assert_no_drift(
        keyed_as_list(module_recorder.last_json("/p/api/detail")),
        keyed_as_list(read_fixture(STORE, "product_shop.json")),
        label="shop product",
        ignore=optional("$"),
    )


def test_a_missing_product_raises_not_found(lidl: Lidl) -> None:
    with pytest.raises(NotFoundError):
        lidl.get_product("11999999")


def test_a_photo_downloads(lidl: Lidl, shop_page: Any, tmp_path: Path) -> None:
    photo = first_of(shop_page, ProductFamily.SHOP).thumbnail
    assert photo is not None

    saved = lidl.download(photo, tmp_path / "photo")

    assert saved.stat().st_size > 1000


# ------------------------------------------------------------------- categories


def test_the_category_facet_walks_down_one_level_at_a_time(
    lidl: Lidl, module_recorder: RecordingTransport
) -> None:
    roots = lidl.get_categories()
    assert roots
    assert all(isinstance(root, LidlCategory) and root.id.isdigit() for root in roots)
    assert all(root.product_count for root in roots)
    assert_no_drift(
        module_recorder.last_json(SEARCH_PATH),
        read_fixture(STORE, "categories.json"),
        label="categories",
    )

    root = lidl.get_category(roots[0].id)
    assert root.id == roots[0].id
    assert root.children
    assert root.products
    assert all(child.parent_id == root.id for child in root.children)
    assert_no_drift(
        keyed_as_list(module_recorder.last_json(SEARCH_PATH)),
        keyed_as_list(read_fixture(STORE, "category.json")),
        label="category",
        ignore=optional("$.items[].gridbox.data"),
    )

    child = lidl.get_category(root.children[0].id)
    assert child.id == root.children[0].id
    assert child.parent_id == root.id
    assert child.products
    assert_no_drift(
        keyed_as_list(module_recorder.last_json(SEARCH_PATH)),
        keyed_as_list(read_fixture(STORE, "category_child.json")),
        label="subcategory",
        ignore=optional("$.items[].gridbox.data"),
    )


# ---------------------------------------------------------------------- catalog


def test_the_sitemap_lists_both_families(lidl: Lidl) -> None:
    ids = list(lidl.iter_catalog_ids())

    assert len(ids) > 1000
    assert len(set(ids)) == len(ids)
    assert any(product_id.startswith("11") for product_id in ids)
    assert any(product_id.startswith("10") for product_id in ids)


def test_the_catalog_hydrates_sitemap_ids(lidl: Lidl) -> None:
    products = list(itertools.islice(lidl.iter_catalog(family="grocery"), 2))

    assert len(products) == 2
    assert all(product.family is ProductFamily.GROCERY for product in products)
    assert all(isinstance(product, LidlProduct) for product in products)


# ----------------------------------------------------------------------- offers


def test_this_weeks_offers_are_priced_grocery_tiles(lidl: Lidl) -> None:
    offers = lidl.get_offers()

    assert offers
    assert all(product.family is ProductFamily.GROCERY for product in offers)
    priced = [product for product in offers if product.price.amount is not None]
    assert len(priced) > len(offers) // 2
    assert all(product.price.currency == "EUR" for product in priced)
    assert any(product.price.is_discounted for product in priced)


def test_next_weeks_campaign_carries_future_prices(
    lidl: Lidl, module_recorder: RecordingTransport
) -> None:
    slug, campaign_id = OFFER_CAMPAIGNS[OfferWeek.NEXT.value]

    products = lidl.get_campaign_products(slug, campaign_id)

    assert products
    upcoming = [product for product in products if product.future_prices]
    assert upcoming, "next week's page prices nothing in advance"
    future = upcoming[0].future_prices[0]
    assert future.amount is not None
    assert future.valid_from is not None
    page = module_recorder.last(f"/c/{slug}/a{campaign_id}").response.text
    assert_no_drift(
        keyed_as_list(tiles(page)),
        keyed_as_list(tiles(read_fixture(STORE, "campaign.html"))),
        label="campaign tiles",
        ignore=optional("$[]"),
    )


# --------------------------------------------------------------------- leaflets


def test_leaflets_list_and_open_a_food_and_a_bazar_one(
    lidl: Lidl, module_recorder: RecordingTransport
) -> None:
    leaflets = lidl.get_leaflets()

    assert leaflets
    assert all(isinstance(leaflet, LidlLeaflet) for leaflet in leaflets)
    assert all(leaflet.offer_starts_on is not None for leaflet in leaflets)
    assert_no_drift(
        module_recorder.last_json("/overview", host="endpoints.leaflets.schwarz"),
        read_fixture(STORE, "leaflets.json"),
        label="leaflets",
    )

    food = next(item for item in leaflets if "aliment" in item.name.lower())
    leaflet = lidl.get_leaflet(food.id)
    assert leaflet.id == food.id
    assert leaflet.pages
    assert leaflet.pages[0].image_url is not None
    assert_no_drift(
        module_recorder.last_json("/flyer", host="endpoints.leaflets.schwarz"),
        read_fixture(STORE, "leaflet.json"),
        label="leaflet",
        # the api lists unused filters here, and the fixture sent one
        ignore=("$.warnings",),
    )

    bazar = next((item for item in leaflets if "bazar" in item.name.lower()), None)
    if bazar is None:
        pytest.skip("no bazar leaflet is on offer this week")
    detail = lidl.get_leaflet(bazar.id)
    assert detail.products
    assert all(product.id for product in detail.products)
    assert_no_drift(
        keyed_as_list(module_recorder.last_json("/flyer")),
        keyed_as_list(read_fixture(STORE, "leaflet_bazar.json")),
        label="bazar leaflet",
    )
