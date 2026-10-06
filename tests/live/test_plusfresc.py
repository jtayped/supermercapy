"""live contract tests for plusfresc, against wscompra.plusfresc.cat.

covers every public method once: the postal-code binding and a postcode
outside the delivery area, the store and locker list with and without a
postcode, the preparation centers, the catalogue version, the unit codes, the
self-minted guest token and its refresh after a 401, search with its offset
cursor and its hundred-row cap, a query carrying characters the path cannot,
product detail with its nutrition sheet, a missing item id and a placement id,
the category tree, one category and a page of its listing, the web offers and
the promoted carousel, the new arrivals, the whole catalogue, both image
renditions, and a spanish client.

one module-scoped client makes about thirty-two requests at the store's own
half-second pacing. one of them is the catalogue root, a single uncached
response of about six megabytes, and the category tree is about three hundred
kilobytes; both are fetched once.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from supermercapy import Language, NotFoundError, OutOfCoverageError, Plusfresc
from supermercapy.plusfresc import (
    ImageSize,
    PlusfrescCategory,
    PlusfrescProduct,
    PlusfrescSearchResult,
)
from supermercapy.plusfresc._constants import SEARCH_RESULT_LIMIT
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "plusfresc"
POSTAL_CODE = "25001"
OUTSIDE_POSTAL_CODE = "28001"
QUERY = "llet"
API_HOST = "wscompra.plusfresc.cat"
IMAGE_HOST = "compra.plusfresc.cat"
SEARCH_PATH = "/api/search/languages/"
LISTING_PATH = "/api/products/category/"

# a multi-buy campaign adds these to a row; only some products are in one
MULTIBUY = ("combinable_with", "promo_id", "qty_required")
# and a markdown adds these, so a row outside the offers category may lack all
MARKDOWN = ("end_date", "new_value_cents", "band_uri", "chip_color")


def optional(prefix: str, fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}.{field}" for field in fields)


@pytest.fixture(scope="module")
def plusfresc(module_recorder: RecordingTransport) -> Iterator[Plusfresc]:
    with Plusfresc.from_postal_code(
        POSTAL_CODE, **live_options(module_recorder)
    ) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(plusfresc: Plusfresc) -> PlusfrescSearchResult:
    return plusfresc.search_products(QUERY, page_size=5)


@pytest.fixture(scope="module")
def product(
    plusfresc: Plusfresc, first_page: PlusfrescSearchResult
) -> PlusfrescProduct:
    return plusfresc.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def tree(plusfresc: Plusfresc) -> tuple[PlusfrescCategory, ...]:
    return plusfresc.get_categories()


def assert_row(row: PlusfrescProduct) -> None:
    assert isinstance(row.id, str) and row.id.isdigit()
    assert row.item_id == row.id
    assert row.placement_id and row.placement_id.endswith(row.id)
    assert row.name
    assert row.price.currency == "EUR"
    assert isinstance(row.price.amount, Decimal) and row.price.amount > 0
    assert row.ean is None
    assert row.photos and all(photo.url.startswith("https://") for photo in row.photos)


# --------------------------------------------------------------------- binding


def test_from_postal_code_binds_a_preparation_center(
    plusfresc: Plusfresc, module_recorder: RecordingTransport
) -> None:
    assert isinstance(plusfresc.center, int) and plusfresc.center > 0
    assert plusfresc.store_id == str(plusfresc.center)
    # the answer is a bare json string holding the center id
    assert isinstance(module_recorder.last_json(f"/utils/{POSTAL_CODE}/centre"), str)


def test_a_postcode_outside_the_area_is_out_of_coverage(
    module_recorder: RecordingTransport,
) -> None:
    with pytest.raises(OutOfCoverageError):
        Plusfresc.from_postal_code(OUTSIDE_POSTAL_CODE, **live_options(module_recorder))


def test_list_stores_resolves_lockers_to_buildable_centers(
    plusfresc: Plusfresc, module_recorder: RecordingTransport
) -> None:
    stores = plusfresc.list_stores()
    assert_no_drift(
        module_recorder.last_json("/utils/centres"),
        read_fixture(STORE, "pickup_points.json"),
        label="pickup points",
    )
    centers = plusfresc.centers()
    assert_no_drift(
        module_recorder.last_json("/zones/preparationcenters"),
        read_fixture(STORE, "centers.json"),
        label="preparation centers",
    )

    assert stores and centers
    center_ids = {center.id for center in centers}
    assert all(center.kind == "center" and center.name for center in centers)
    assert plusfresc.store_id in center_ids
    for store in stores:
        assert store.id in center_ids, f"{store.pickup_code} names no center"
        assert store.kind in {"store", "locker"}
        assert store.pickup_code and store.name
    assert all(store.postal_code for store in stores if not store.is_locker)

    nearby = plusfresc.list_stores(POSTAL_CODE)

    assert nearby
    assert {store.id for store in nearby} == {plusfresc.store_id}


# ------------------------------------------------------------------ catalogue


def test_catalog_version_and_units(
    plusfresc: Plusfresc, module_recorder: RecordingTransport
) -> None:
    version = plusfresc.get_catalog_version()
    units = plusfresc.get_units()

    assert isinstance(version, date)
    assert units and all(unit.code and unit.name for unit in units)
    assert {unit.language for unit in units} == {"ca"}
    assert_no_drift(
        module_recorder.last_json("/categories/newest"),
        read_fixture(STORE, "catalog_version.json"),
        label="catalog version",
    )
    assert_no_drift(
        module_recorder.last_json("/utils/units/ca"),
        read_fixture(STORE, "units.json"),
        label="units",
    )


def test_the_whole_catalogue_is_one_request(
    plusfresc: Plusfresc, module_recorder: RecordingTransport
) -> None:
    before = len(module_recorder.exchanges)

    catalog = plusfresc.get_catalog()

    sent = module_recorder.exchanges[before:]
    assert [exchange.request.url.path for exchange in sent] == [
        f"{LISTING_PATH}Root/{plusfresc.store_id}"
    ]
    rows = sent[0].json()
    # a product placed twice arrives twice; get_catalog keeps it once
    assert len(catalog) == len({row["item_id"] for row in rows})
    assert len(rows) >= len(catalog) > 1000
    assert len({product.id for product in catalog}) == len(catalog)
    for row in catalog[:20]:
        assert_row(row)
    assert_no_drift(
        rows,
        read_fixture(STORE, "listing.json"),
        label="catalogue",
        ignore=optional("$[]", MULTIBUY + MARKDOWN),
    )


# ----------------------------------------------------------------------- auth


def test_the_guest_token_is_minted_and_sent_only_to_the_api(
    first_page: PlusfrescSearchResult, module_recorder: RecordingTransport
) -> None:
    mint = module_recorder.last("/loginGuest/", method="POST")
    token = mint.json()

    assert "authorization" not in mint.request.headers
    assert isinstance(token, str) and token.count(".") == 2
    assert_no_drift(token, read_fixture(STORE, "guest_token.json"), label="token")
    search = module_recorder.last(SEARCH_PATH, host=API_HOST)
    assert search.request.headers["authorization"] == f"Bearer {token}"


def test_a_rejected_token_is_minted_again_once(
    plusfresc: Plusfresc, module_recorder: RecordingTransport
) -> None:
    # a well-formed token with a far expiry and a forged signature, so the
    # client sends it and the api refuses it
    plusfresc._token = read_fixture(STORE, "guest_token.json")
    plusfresc._token_expires_at = 4102444800.0
    before = len(module_recorder.exchanges)

    page = plusfresc.search_products(QUERY, page_size=1)

    sent = [
        (exchange.request.method, exchange.response.status_code)
        for exchange in module_recorder.exchanges[before:]
    ]
    assert sent == [("GET", 401), ("POST", 200), ("GET", 200)]
    assert page.products


# --------------------------------------------------------------------- search


def test_search_carves_pages_out_of_one_capped_response(
    plusfresc: Plusfresc,
    first_page: PlusfrescSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    rows = module_recorder.last_json(SEARCH_PATH)

    assert len(first_page.products) == 5
    for row in first_page.products:
        assert_row(row)
    assert first_page.row_count == first_page.total_hits == len(rows)
    # the cap the truncated flag stands on; a larger answer means it moved
    assert first_page.row_count <= SEARCH_RESULT_LIMIT
    assert first_page.truncated is (first_page.row_count == SEARCH_RESULT_LIMIT)
    assert first_page.next_cursor == "5"
    assert_no_drift(
        rows,
        read_fixture(STORE, "search.json"),
        label="search",
        ignore=optional("$[]", MULTIBUY + MARKDOWN),
    )

    second = plusfresc.search_products(
        QUERY, page_size=5, cursor=first_page.next_cursor
    )

    assert second.offset == 5
    assert [row.id for row in second.products] == [row["item_id"] for row in rows[5:10]]


def test_a_query_the_path_cannot_carry_still_searches(plusfresc: Plusfresc) -> None:
    # iis refused a slash, a plus, a percent sign and a trailing dot
    page = plusfresc.search_products("llet 1/2 50%.", page_size=3)

    assert page.query == "llet 1/2 50%."
    assert page.products


# -------------------------------------------------------------------- product


def test_get_product_reads_the_sheet(
    product: PlusfrescProduct,
    first_page: PlusfrescSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    raw = module_recorder.last_json("/productdetails/files/")

    assert product.id == first_page.products[0].id
    assert_row(product)
    assert product.price.amount == first_page.products[0].price.amount
    if any(item.get("nutritionalname") for item in raw["nutritionals"]):
        assert product.nutrition is not None
        assert product.nutrition.values
        assert all(value.name for value in product.nutrition.values)
    assert_no_drift(
        raw,
        read_fixture(STORE, "product.json"),
        label="product",
        ignore=optional("$.product", MULTIBUY + MARKDOWN),
    )


def test_a_missing_item_and_a_placement_id_are_not_found(
    plusfresc: Plusfresc, first_page: PlusfrescSearchResult
) -> None:
    placement = first_page.products[0].placement_id
    assert placement is not None

    with pytest.raises(NotFoundError):
        plusfresc.get_product("999999")
    # the composite id a listing row calls ``id`` is answered with a 416
    with pytest.raises(NotFoundError):
        plusfresc.get_product(placement)


# ----------------------------------------------------------------- categories


def test_the_tree_and_one_category(
    plusfresc: Plusfresc,
    tree: tuple[PlusfrescCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    assert_no_drift(
        module_recorder.last_json(f"/categories/tree/{plusfresc.store_id}/Root"),
        read_fixture(STORE, "tree.json"),
        label="category tree",
    )
    ids = {category.id for category in tree}
    assert {"Oferta2", "40"} <= ids
    assert all(category.name and category.parent_id == "Root" for category in tree)
    top = next(category for category in tree if category.id.isdigit())
    wanted = next(child for child in top.children if child.children)
    assert wanted.parent_id == top.id

    category = plusfresc.get_category(wanted.id, page_size=5)

    assert category.id == wanted.id
    assert category.name == wanted.name
    assert [child.id for child in category.children] == [
        child.id for child in wanted.children
    ]
    assert 0 < len(category.products) <= 5
    for row in category.products:
        assert_row(row)
    assert_no_drift(
        module_recorder.last_json(f"/categories/tree/{plusfresc.store_id}/{wanted.id}"),
        read_fixture(STORE, "tree_010101.json"),
        label="category node",
    )
    listing = module_recorder.last_json(f"{LISTING_PATH}{wanted.id}/")
    assert_no_drift(
        listing,
        read_fixture(STORE, "listing.json"),
        label="category listing",
        ignore=optional("$[]", MULTIBUY + MARKDOWN),
    )

    page = plusfresc.get_category_products(wanted.id, page_size=5, cursor="5")

    assert page.offset == 5
    assert page.row_count == len(listing)
    # a listing is the whole category, whatever its length
    assert page.truncated is False
    assert [row.id for row in page.products] == [
        row["item_id"] for row in listing[5:10]
    ]


# ----------------------------------------------------- offers and new arrivals


def test_offers_and_the_promoted_carousel(
    plusfresc: Plusfresc, module_recorder: RecordingTransport
) -> None:
    offers = plusfresc.get_offers()

    assert offers
    assert all(row.promotions for row in offers)
    assert sum(row.price.is_discounted for row in offers) > len(offers) // 2
    for row in offers[:10]:
        assert_row(row)
    assert_no_drift(
        module_recorder.last_json(f"{LISTING_PATH}Oferta2/"),
        read_fixture(STORE, "offers.json"),
        label="offers",
        ignore=optional("$[]", MULTIBUY),
    )

    highlighted = plusfresc.get_offers(highlighted=True)

    assert isinstance(highlighted, tuple)
    for row in highlighted:
        assert_row(row)


def test_new_arrivals_are_flagged_new(plusfresc: Plusfresc) -> None:
    arrivals = plusfresc.get_new_arrivals()

    assert arrivals
    assert all(row.is_new for row in arrivals)
    for row in arrivals[:10]:
        assert_row(row)


# --------------------------------------------------------------------- images


def test_both_renditions_download_without_the_token(
    plusfresc: Plusfresc,
    product: PlusfrescProduct,
    module_recorder: RecordingTransport,
    tmp_path: Path,
) -> None:
    photo = product.photos[0]

    large = plusfresc.download(photo, tmp_path / "large.jpg")
    small = plusfresc.download_photo(
        photo, tmp_path / "small.jpg", size=ImageSize.SMALL
    )

    assert large.read_bytes()[:2] == b"\xff\xd8"
    assert small.read_bytes()[:2] == b"\xff\xd8"
    assert large.stat().st_size > small.stat().st_size
    images = list(module_recorder.find("/ImatgesProductes/", host=IMAGE_HOST))
    assert len(images) >= 2
    assert all("authorization" not in item.request.headers for item in images)


# ------------------------------------------------------------------- language


def test_a_spanish_client_translates_names_and_labels(
    plusfresc: Plusfresc,
    product: PlusfrescProduct,
    module_recorder: RecordingTransport,
) -> None:
    with Plusfresc(
        plusfresc.center, language="es", **live_options(module_recorder)
    ) as spanish:
        assert spanish.language is Language.SPANISH
        translated = spanish.get_product(product.id)
        page = spanish.search_products("leche", page_size=3)
        units = spanish.get_units()

    assert translated.id == product.id
    assert translated.name == product.name_es
    assert translated.price.amount == product.price.amount
    assert module_recorder.last("/productdetails/files/").request.url.path.endswith(
        f"/{product.id}/es"
    )
    assert page.products
    assert all(row.name == row.name_es for row in page.products)
    assert {unit.language for unit in units} == {"es"}
