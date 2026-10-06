"""live contract tests for condis, against api.empathy.co and compraonline.condis.es.

covers every public method once: search and its next page by offset, a
product sheet behind the anonymous sign-in, the category tree, one category,
a bounded catalogue walk, the new arrivals, the offers, the suggestions, a
client bound through the postcode action with a search at its centre, a
missing product, and one image download. ``get_catalog`` is left out because
it walks every row of the shop, five hundred at a time.

one module-scoped client and one bound client send about forty requests at
the default half-second pacing, eighteen of them the postcode binding, which
reads about 1.4 mb of scripts to find the action. the offers and the catalogue
step read five hundred rows a request, some 5 mb between them. no refusal was
seen in october 2026; one skips the test as inconclusive.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from supermercapy import NotFoundError
from supermercapy.condis import (
    Condis,
    CondisCategory,
    CondisProduct,
    CondisSearchResult,
)
from supermercapy.condis.models import flight_payload, flight_value
from tests.conftest import read_fixture
from tests.drift import assert_no_drift
from tests.live.conftest import Exchange, RecordingTransport, live_options

STORE = "condis"
QUERY = "leche"
INDEX_HOST = "api.empathy.co"
SITE_HOST = "compraonline.condis.es"
ROWS = "$.catalog.content[]"
# a barcelona postcode, which the storefront assigned a centre other than the
# default in october 2026
POSTAL_CODE = "08034"

# what only some rows carry: a promotion, its corner badges, a sale price, and
# the energy and weight figures of food
ROW_OPTIONAL = tuple(
    f"{ROWS}.{key}"
    for key in (
        "promotion_text",
        "promotions",
        "large_promotion_media",
        "mobile_promotion_media",
        "price.discounted",
        "kcal",
        "netWeight",
    )
)


def page_value(html: str, key: str) -> Any:
    return flight_value(flight_payload(html), key)


def fixture_value(name: str, key: str) -> Any:
    return page_value(read_fixture(STORE, name), key)


def action_result(text: str) -> Any:
    """return the value a server action answered with, from its flight lines."""

    line = next(line for line in text.splitlines() if line.startswith("1:"))
    return json.loads(line[2:])


def last_storefront(
    recorder: RecordingTransport, path: str, method: str = "GET"
) -> Exchange:
    """return the newest storefront exchange for exactly ``path``."""

    return next(
        exchange
        for exchange in reversed(recorder.exchanges)
        if exchange.request.url.host == SITE_HOST
        and exchange.request.url.path == path
        and exchange.request.method == method
    )


@pytest.fixture(scope="module")
def condis(module_recorder: RecordingTransport) -> Iterator[Condis]:
    with Condis(**live_options(module_recorder)) as client:
        yield client


@pytest.fixture(scope="module")
def first_page(condis: Condis) -> CondisSearchResult:
    return condis.search_products(QUERY, page_size=5)


@pytest.fixture(scope="module")
def product(condis: Condis, first_page: CondisSearchResult) -> CondisProduct:
    return condis.get_product(first_page.products[0].id)


@pytest.fixture(scope="module")
def tree(condis: Condis) -> tuple[CondisCategory, ...]:
    return condis.get_categories()


def assert_row(row: CondisProduct) -> None:
    assert isinstance(row.id, str) and row.id.isdigit()
    assert row.name
    assert row.ean is None
    assert row.price.currency == "EUR"
    assert isinstance(row.price.amount, Decimal)
    assert row.price.amount > 0


def assert_listing_drift(
    recorder: RecordingTransport, endpoint: str, name: str
) -> None:
    assert_no_drift(
        recorder.last_json(f"/condis/{endpoint}", host=INDEX_HOST),
        read_fixture(STORE, name),
        label=name,
        ignore=ROW_OPTIONAL,
    )


# --------------------------------------------------------------------- search


def test_search_returns_rows_and_the_next_offset(
    first_page: CondisSearchResult, module_recorder: RecordingTransport
) -> None:
    assert first_page.products
    for row in first_page.products:
        assert_row(row)
        assert row.url and row.url.startswith(f"https://{SITE_HOST}/")
    assert first_page.total_hits and first_page.total_hits > 5
    assert first_page.next_cursor == "5"
    request = module_recorder.last("/condis/search", host=INDEX_HOST).request
    assert request.url.params["store"] == "718"
    assert_listing_drift(module_recorder, "search", "search.json")


def test_the_offset_continues_the_search(
    condis: Condis,
    first_page: CondisSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    second = condis.search_products(QUERY, page_size=5, cursor=first_page.next_cursor)

    assert second.products
    assert second.offset == 5
    assert not {row.id for row in second.products} & {
        row.id for row in first_page.products
    }
    assert_listing_drift(module_recorder, "search", "search_last.json")


# -------------------------------------------------------------------- product


def test_get_product_reads_the_sheet_behind_the_sign_in(
    product: CondisProduct,
    first_page: CondisSearchResult,
    module_recorder: RecordingTransport,
) -> None:
    assert product.id == first_page.products[0].id
    assert_row(product)
    assert product.photos
    assert product.url and f"/p/{product.id}/" in product.url
    assert product.category_path
    assert product.category_ids[0].startswith(product.category_path[0].id)
    assert product.nutrition is not None
    paths = [exchange.request.url.path for exchange in module_recorder.exchanges]
    assert "/api/auth/callback/anonymous" in paths
    html = module_recorder.last("/p/p/", host=SITE_HOST).response.text
    for key in ("productInformation", "matchedChild"):
        assert_no_drift(
            page_value(html, key),
            fixture_value("product_704049.html", key),
            label=key,
            ignore=("$.nutritional_info.nutritional_facts[]",),
        )


def test_a_missing_product_is_not_found(
    condis: Condis, product: CondisProduct, module_recorder: RecordingTransport
) -> None:
    with pytest.raises(NotFoundError):
        condis.get_product("999999999")

    exchange = module_recorder.last("/p/p/999999999/", host=SITE_HOST)
    assert exchange.response.status_code == 200
    assert "productInformation" not in flight_payload(exchange.response.text)


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_three_level_tree(
    tree: tuple[CondisCategory, ...], module_recorder: RecordingTransport
) -> None:
    assert len(tree) > 5
    assert all(root.children for root in tree)
    assert any(child.children for root in tree for child in root.children)
    html = last_storefront(module_recorder, "/").response.text
    assert_no_drift(
        page_value(html, "categoryList"),
        fixture_value("home.html", "categoryList"),
        label="category tree",
    )


def test_get_category_returns_the_node_its_children_and_rows(
    condis: Condis,
    tree: tuple[CondisCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    family = tree[0].children[0]

    category = condis.get_category(family.id, page_size=5)

    assert category.id == family.id
    assert category.children
    assert category.products
    assert category.product_count and category.product_count >= len(category.products)
    for row in category.products:
        assert_row(row)
    assert_listing_drift(module_recorder, "browse", "browse.json")


def test_the_catalogue_walk_starts_with_the_first_top_level_category(
    condis: Condis,
    tree: tuple[CondisCategory, ...],
    module_recorder: RecordingTransport,
) -> None:
    rows = list(itertools.islice(condis.iter_catalog(), 5))

    assert len(rows) == 5
    for row in rows:
        assert_row(row)
    request = module_recorder.last("/condis/browse", host=INDEX_HOST).request
    assert request.url.params["browseValue"] == tree[0].id
    assert request.url.params["rows"] == "500"


# ------------------------------------------------------------ arrivals, offers


def test_new_arrivals_are_the_novelty_facet(
    condis: Condis, module_recorder: RecordingTransport
) -> None:
    arrivals = condis.get_new_arrivals()

    assert arrivals
    assert all(row.is_new for row in arrivals)
    request = module_recorder.last("/condis/browse", host=INDEX_HOST).request
    assert request.url.params["browseField"] == "is_novelty"
    assert_listing_drift(module_recorder, "browse", "novelties.json")


def test_offers_are_the_sale_and_promotion_facets(
    condis: Condis, module_recorder: RecordingTransport
) -> None:
    offers = condis.get_offers()

    assert offers
    assert len({row.id for row in offers}) == len(offers)
    assert any(row.price.previous is not None for row in offers)
    assert any(row.promotions for row in offers)
    browsed = [
        exchange
        for exchange in module_recorder.find("/condis/browse", host=INDEX_HOST)
        if exchange.request.url.params["browseField"] in {"on_sale", "on_promotion"}
    ]
    for field, name in (
        ("on_sale", "on_sale.json"),
        ("on_promotion", "on_promotion.json"),
    ):
        exchange = next(
            item for item in browsed if item.request.url.params["browseField"] == field
        )
        assert_no_drift(
            exchange.json(), read_fixture(STORE, name), label=name, ignore=ROW_OPTIONAL
        )


# ----------------------------------------------------------------- extensions


def test_suggest_returns_strings(
    condis: Condis, module_recorder: RecordingTransport
) -> None:
    suggestions = condis.suggest("lech")

    assert suggestions
    assert all(isinstance(item, str) and item for item in suggestions)
    assert_no_drift(
        module_recorder.last_json("/condis/empathize", host=INDEX_HOST),
        read_fixture(STORE, "empathize.json"),
        label="suggestions",
    )


# ------------------------------------------------------------------- postcode


def test_a_postcode_binds_the_centre_the_storefront_assigns(
    module_recorder: RecordingTransport,
) -> None:
    with Condis.from_postal_code(POSTAL_CODE, **live_options(module_recorder)) as bound:
        assert bound.picking_centre.isdigit()
        page = bound.search_products(QUERY, page_size=5)

    lookup = last_storefront(module_recorder, "/", method="POST")
    assert lookup.request.headers["Next-Action"]
    assert_no_drift(
        action_result(lookup.response.text),
        action_result(read_fixture(STORE, "postal_code_08034.txt")),
        label="postcode lookup",
    )
    request = module_recorder.last("/condis/search", host=INDEX_HOST).request
    assert request.url.params["store"] == bound.picking_centre
    assert page.products
    for row in page.products:
        assert_row(row)


# ---------------------------------------------------------------------- image


def test_download_fetches_one_photo(
    condis: Condis, product: CondisProduct, tmp_path: Path
) -> None:
    path = condis.download(product.photos[0], tmp_path / "photo.jpg")

    head = path.read_bytes()[:4]
    assert head[:2] == b"\xff\xd8" or head == b"RIFF" or head == b"\x89PNG"
