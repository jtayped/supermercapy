"""live contract tests for dia, against www.dia.es.

covers every public method once: the postcode check and the move of the
session to it, a postcode outside the delivery area, search with its offset
cursor and a page cut from the thirty-row minimum, a product sheet, a missing
product, the category tree, one child and one top-level category with their
next pages, a bounded catalogue walk, the "novedades" new arrivals, the offers
walked category by category and one category's offers, a catalan client, and
one image download. ``get_catalog`` is left out because it walks about 450
pages.

one module-scoped client bound to a barcelona postcode makes about sixty
requests, thirty of them the offer walk, at the default half-second pacing.
dia has shown akamai bot manager cookies but no challenge on the json api; a
refusal skips the test as inconclusive.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from supermercapy import Dia, Language, NotFoundError, OutOfCoverageError
from supermercapy.dia import DiaCategory, DiaProduct, DiaSearchResult
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import RecordingTransport, live_options

STORE = "dia"
POSTAL_CODE = "08001"
OUTSIDE_POSTAL_CODE = "35001"
QUERY = "leche"
SEARCH_PATH = "/search-back/search/reduced"

# what only some rows carry: a promotion and its badge, a stamp, free-from
# claims, the own-brand flag, and the average weight of loose produce
ROW_OPTIONAL = (
    "promotions",
    "headband_promotion",
    "stamp_code",
    "stamp_color",
    "stamp_description",
    "stamp_text_color",
    "allergens",
    "product_info",
    "dia_brand",
    "average_weight",
    "weight_in_grams",
    "brand",
)
# and what only some product sheets carry
SHEET_OPTIONAL = tuple(
    f"$.product.{key}"
    for key in (
        "info_labels",
        "ingredients",
        "instructions",
        "manufacturer_contact",
        "nutritional_info",
        "product_info",
        "promotions",
        "prices.discount_percentage",
        "prices.is_club_price",
        "prices.is_promo_price",
    )
)


def rows(path: str) -> tuple[str, ...]:
    return tuple(f"{path}.{key}" for key in ROW_OPTIONAL)


@pytest.fixture(scope="module")
def dia(module_recorder: RecordingTransport) -> Iterator[Dia]:
    with Dia.from_postal_code(POSTAL_CODE, **live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(dia: Dia) -> DiaSearchResult:
    return dia.search_products(QUERY)


@pytest.fixture(scope="module")
def product(dia: Dia, first_page: DiaSearchResult) -> DiaProduct:
    return dia.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def tree(dia: Dia) -> tuple[DiaCategory, ...]:
    return dia.get_categories()


def leaf_of(tree: tuple[DiaCategory, ...]) -> DiaCategory:
    """return the first child of the first top-level category with a listing."""

    return next(root for root in tree if root.id != "L128").children[0]


def assert_row(product: DiaProduct) -> None:
    assert isinstance(product.id, str) and product.id
    assert product.name
    assert product.price.currency == "EUR"
    assert isinstance(product.price.amount, Decimal)
    assert product.price.amount > 0
    assert product.ean is None


# --------------------------------------------------------------------- binding


def test_from_postal_code_names_the_store_and_moves_the_session(
    dia: Dia, first_page: DiaSearchResult, module_recorder: RecordingTransport
) -> None:
    assert dia.postal_code == POSTAL_CODE
    assert dia.physical_store_id and dia.physical_store_id.isdigit()
    assert first_page.postal_code == POSTAL_CODE
    put = module_recorder.last("/save-shipping-address", method="PUT")
    assert put.response.status_code == 204
    assert_no_drift(
        module_recorder.last_json("/check-service"),
        read_fixture(STORE, "check_service.json"),
        label="postcode check",
    )


def test_a_postcode_outside_the_area_is_out_of_coverage(
    module_recorder: RecordingTransport,
) -> None:
    with pytest.raises(OutOfCoverageError):
        Dia.from_postal_code(OUTSIDE_POSTAL_CODE, **live_options(module_recorder))


# ---------------------------------------------------------------------- search


def test_search_returns_rows_a_total_and_an_offset_cursor(
    first_page: DiaSearchResult, module_recorder: RecordingTransport
) -> None:
    assert len(first_page.products) == 30
    for row in first_page.products:
        assert_row(row)
    assert first_page.total_hits is not None and first_page.total_hits > 30
    assert first_page.next_cursor == "30"
    assert any(row.price.reference is not None for row in first_page.products)
    assert_no_drift(
        module_recorder.last_json(SEARCH_PATH),
        read_fixture(STORE, "search.json"),
        label="search",
        ignore=(*rows("$.search_items[]"), "$.suggestions"),
    )


def test_the_cursor_continues_and_a_small_page_is_cut_exactly(
    dia: Dia, first_page: DiaSearchResult
) -> None:
    second = dia.search_products(QUERY, cursor=first_page.next_cursor)
    small = dia.search_products(QUERY, page_size=7, cursor="28")

    assert second.products
    assert second.next_cursor == "60"
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }
    assert len(small.products) == 7
    assert small.products[2].id == second.products[0].id


# --------------------------------------------------------------------- product


def test_get_product_reads_the_sheet(
    product: DiaProduct,
    first_page: DiaSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    assert product.id == first_page.products[0].id
    assert_row(product)
    assert product.photos
    assert product.category_path
    assert product.availability.units_in_stock is not None
    assert_no_drift(
        module_recorder.last_json(f"/pdp-back/{product.id}"),
        read_fixture(STORE, "product.json"),
        label="product sheet",
        ignore=SHEET_OPTIONAL,
    )


def test_a_missing_product_is_not_found(
    dia: Dia, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError, match="no product"):
        dia.get_product("999999999")

    exchange = module_recorder.last("/pdp-back/999999999")
    assert exchange.response.status_code == 500
    assert_no_drift(
        exchange.json(),
        read_fixture(STORE, "product_error.json"),
        label="unknown sku",
    )


# ------------------------------------------------------------------ categories


def test_get_categories_returns_the_two_level_tree(
    tree: tuple[DiaCategory, ...], module_recorder: RecordingTransport
) -> None:
    assert len(tree) > 20
    assert all(root.children for root in tree)
    assert all(child.id != root.id for root in tree for child in root.children)
    assert_no_drift(
        module_recorder.last_json("/menu-data"),
        read_fixture(STORE, "menu.json"),
        label="menu",
    )


def test_a_child_category_and_its_next_page(
    dia: Dia, tree: tuple[DiaCategory, ...], module_recorder: RecordingTransport
) -> None:
    node = leaf_of(tree)

    category = dia.get_category(node.id)

    assert category.name == node.name
    assert category.products
    for row in category.products:
        assert_row(row)
        assert row.category_ids == (node.id,)
    assert category.product_count is not None
    assert_no_drift(
        module_recorder.last_json(f"/plp-back/reduced{node.url}"),
        read_fixture(STORE, "listing.json"),
        label="child listing",
        ignore=(*rows("$.plp_items[]"), "$.seo"),
    )
    first = dia.get_category_products(node.id)
    if first.next_cursor is not None:
        second = dia.get_category_products(node.id, cursor=first.next_cursor)
        assert second.products


def test_a_top_level_category_walks_its_children(
    dia: Dia, tree: tuple[DiaCategory, ...], module_recorder: RecordingTransport
) -> None:
    root = next(root for root in tree if root.id != "L128")

    category = dia.get_category(root.id)

    assert category.products
    assert category.product_count is not None
    assert category.product_count >= len(category.products)
    assert_no_drift(
        module_recorder.last_json(f"/plp-back/l1/all/{root.id}/reduced"),
        read_fixture(STORE, "listing_root.json"),
        label="top-level listing",
        ignore=(*rows("$.items[]"), "$.seo"),
    )
    page = dia.get_category_products(root.id)
    assert page.next_cursor is not None
    following = dia.get_category_products(root.id, cursor=page.next_cursor)
    assert following.products


def test_the_novedades_group_has_no_listing_of_its_own(dia: Dia) -> None:
    category = dia.get_category("L128")

    assert category.products == ()
    assert category.children


def test_iter_catalog_walks_the_listings(dia: Dia) -> None:
    sample = list(itertools.islice(dia.iter_catalog(), 50))

    assert len(sample) == 50
    for row in sample[:5]:
        assert_row(row)


# ------------------------------------------------------- new arrivals, offers


def test_new_arrivals_are_the_novedades_category(
    dia: Dia, module_recorder: RecordingTransport
) -> None:
    arrivals = dia.get_new_arrivals()

    assert arrivals
    assert all(row.is_new for row in arrivals)
    assert sum(row.stamp_code == "2" for row in arrivals) > len(arrivals) * 0.8
    assert_no_drift(
        module_recorder.last_json("/c/L2302"),
        read_fixture(STORE, "new_arrivals.json"),
        label="new arrivals",
        ignore=(*rows("$.plp_items[]"), "$.seo"),
    )


def test_get_offers_walks_every_category(
    dia: Dia, module_recorder: RecordingTransport
) -> None:
    start = len(module_recorder.exchanges)

    offers = dia.get_offers()

    walk = module_recorder.exchanges[start:]
    total = walk[0].json()["total_items"]
    assert offers
    # a product offered in two categories is returned once
    assert total * 0.6 < len(offers) <= total
    assert len({row.id for row in offers}) == len(offers)
    assert sum(bool(row.promotions) for row in offers) > len(offers) * 0.8
    assert_no_drift(
        walk[0].json(),
        read_fixture(STORE, "offers.json"),
        label="offers page",
        ignore=(*rows("$.plp_items[].items[]"), "$.plp_items[].has_more_items"),
    )
    assert_no_drift(
        walk[1].json(),
        read_fixture(STORE, "category_offers.json"),
        label="category offers",
        ignore=rows("$.plp_items[]"),
    )


def test_get_category_offers_reads_one_category(dia: Dia) -> None:
    offers = dia.get_category_offers("L108")

    assert all(row.category_ids == ("L108",) for row in offers)


# ---------------------------------------------------------- language, images


def test_a_catalan_client_reads_catalan(
    product: DiaProduct, module_recorder: RecordingTransport
) -> None:
    with Dia(language="ca", **live_options(module_recorder)) as catalan:
        assert catalan.language is Language.CATALAN
        translated = catalan.get_product(product.id)
        page = catalan.search_products("llet")
        tree = catalan.get_categories()

    assert translated.id == product.id
    assert page.products
    assert all(root.url and root.url.startswith("/ca/") for root in tree)


def test_download_a_product_image(
    dia: Dia, product: DiaProduct, tmp_path: Path
) -> None:
    path = dia.download(product.photos[0], tmp_path / "product.jpg")

    assert path.read_bytes()[:2] == b"\xff\xd8"
