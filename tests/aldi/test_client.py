from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from supermercapy import (
    Aldi,
    AuthenticationError,
    Capability,
    ConfigurationError,
    InvalidResponseError,
    Language,
    NotFoundError,
)
from supermercapy.aldi import AldiSearchResult, Region
from supermercapy.aldi.client import region_for_postal_code
from tests.conftest import read_fixture
from tests.harness import _aldi_handler

STORE = "aldi"
ALGOLIA = "https://l9knu74io7-dsn.algolia.net/1/indexes"
SITE = "https://www.aldi.es"
KEY = "83df5acd172c42ab174afa4583232b5d"
LEAF = "lacteos-y-huevos/leche-y-bebidas-vegetales"

Handler = Callable[[httpx.Request], httpx.Response]


def client(
    requests: list[httpx.Request] | None = None,
    handler: Handler = _aldi_handler,
    **options: Any,
) -> Aldi:
    def record(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return handler(request)

    return Aldi(transport=httpx.MockTransport(record), **options)


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(json.loads(request.content)["params"], keep_blank_values=True)


def synthetic_index(total: int) -> Handler:
    """an index of ``total`` numbered records serving any offset window."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = params_of(request)
        offset = int(params["offset"][0])
        length = int(params["length"][0])
        hits = [
            {"objectID": str(index), "name": f"product {index}"}
            for index in range(offset, min(offset + length, total))
        ]
        return httpx.Response(
            200, request=request, json={"hits": hits, "nbHits": total}
        )

    return handler


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_binds_the_peninsula() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as aldi:
        assert aldi.region is Region.PENINSULA
        assert aldi.store_id == "pen"
        assert aldi.index_name == "an_prd_es_es_pen_products2"
        assert aldi.language is Language.SPANISH
        assert aldi.min_request_interval == 0

    assert requests == []


def test_the_declared_capabilities_match_the_reconnaissance() -> None:
    assert Aldi.capabilities == {
        Capability.POSTAL_CODE,
        Capability.CATALOG,
        Capability.OFFERS,
        Capability.PROMOTIONS,
        Capability.FUTURE_PRICES,
    }
    assert Aldi.supported_languages == {Language.SPANISH}
    assert Aldi.max_page_size == 1000


@pytest.mark.parametrize("region", ["bal", Region.CANARY_ISLANDS])
def test_a_region_selects_its_index(region: str | Region) -> None:
    with client(region=region) as aldi:
        assert aldi.index_name == f"an_prd_es_es_{Region(region).value}_products2"


@pytest.mark.parametrize("region", ["", "PEN", "madrid", 3])
def test_an_unknown_region_is_rejected(region: Any) -> None:
    with pytest.raises(ConfigurationError, match="region"):
        Aldi(region)


@pytest.mark.parametrize(
    ("postal_code", "region"),
    [
        ("07001", Region.BALEARIC_ISLANDS),
        ("35001", Region.CANARY_ISLANDS),
        ("38001", Region.CANARY_ISLANDS),
        ("28001", Region.PENINSULA),
        ("08013", Region.PENINSULA),
        ("51001", Region.PENINSULA),
        ("52006", Region.PENINSULA),
    ],
)
def test_a_postcode_maps_to_its_region_without_a_request(
    postal_code: str, region: Region
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("from_postal_code performed I/O")

    with Aldi.from_postal_code(
        postal_code, transport=httpx.MockTransport(handler)
    ) as aldi:
        assert aldi.region is region

    assert region_for_postal_code(postal_code) is region
    assert requests == []


def test_an_invalid_postcode_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="postal_code"):
        Aldi.from_postal_code("1234")


def test_stores_and_barcodes_are_not_offered() -> None:
    for capability in (
        Capability.STORES,
        Capability.EAN,
        Capability.EAN_LOOKUP,
        Capability.NUTRITION,
        Capability.NEW_ARRIVALS,
    ):
        assert not Aldi.supports(capability)


# ---------------------------------------------------------------------- search


def test_search_sends_the_documented_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region="bal") as aldi:
        result = aldi.search_products("leche", page_size=2)

    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == f"{ALGOLIA}/an_prd_es_es_bal_products2/query"
    assert request.headers["X-Algolia-Application-Id"] == "L9KNU74IO7"
    assert request.headers["X-Algolia-API-Key"] == KEY
    assert params_of(request) == {
        "query": ["leche"],
        "offset": ["0"],
        "length": ["2"],
        "attributesToHighlight": ["[]"],
    }
    assert isinstance(result, AldiSearchResult)
    assert result.region == "bal"
    assert result.page_size == 2
    assert result.total_hits == 3
    assert result.next_cursor == "2"
    assert [product.id for product in result.products] == ["872200", "828100"]
    assert result.products[0].url is not None
    assert result.products[0].url.startswith(f"{SITE}/bal/producto/")


def test_the_cursor_is_an_offset() -> None:
    requests: list[httpx.Request] = []

    with client(requests, synthetic_index(30)) as aldi:
        rows = list(aldi.iter_search("x", page_size=12))

    assert [params_of(item)["offset"] for item in requests] == [["0"], ["12"], ["24"]]
    assert [row.id for row in rows] == [str(index) for index in range(30)]


@pytest.mark.parametrize("query", ["", "  ", None, 3])
def test_an_unusable_query_is_refused_before_any_request(query: Any) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as aldi, pytest.raises(ConfigurationError, match="query"):
        aldi.search_products(query)

    assert requests == []


@pytest.mark.parametrize("cursor", ["", "x", "-3", "\u0661", 4])
def test_an_unusable_cursor_is_refused(cursor: Any) -> None:
    with client() as aldi, pytest.raises(ConfigurationError, match="cursor"):
        aldi.search_products("leche", cursor=cursor)


def test_page_size_is_capped_at_a_thousand() -> None:
    with client() as aldi, pytest.raises(ConfigurationError, match="page_size"):
        aldi.search_products("leche", page_size=1001)


def test_a_rotated_key_fails_loudly_after_one_request() -> None:
    requests: list[httpx.Request] = []
    invalid = read_fixture(STORE, "invalid_key.json")

    def rejecting(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, json=invalid)

    with (
        client(requests, rejecting) as aldi,
        pytest.raises(AuthenticationError, match="ALGOLIA_API_KEY") as raised,
    ):
        aldi.search_products("leche")

    assert raised.value.status_code == 403
    assert len(requests) == 1


def test_a_site_403_is_an_ordinary_error() -> None:
    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, html="no")

    with client(handler=refusing) as aldi, pytest.raises(Exception) as raised:
        aldi.get_categories()

    assert not isinstance(raised.value, AuthenticationError)


# --------------------------------------------------------------------- product


def test_get_product_reads_one_record() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as aldi:
        product = aldi.get_product(995700)

    (request,) = requests
    assert request.method == "GET"
    assert str(request.url) == f"{ALGOLIA}/an_prd_es_es_pen_products2/995700"
    assert request.headers["X-Algolia-API-Key"] == KEY
    assert product.id == "995700"
    assert product.region == "pen"


def test_a_missing_record_is_not_found() -> None:
    with (
        client(region="can") as aldi,
        pytest.raises(NotFoundError, match="no product 999999999 in the can index"),
    ):
        aldi.get_product("999999999")


@pytest.mark.parametrize("product_id", ["abc", "12/3", "1 2"])
def test_a_non_numeric_id_is_refused_before_any_request(product_id: str) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as aldi, pytest.raises(ConfigurationError, match="numeric"):
        aldi.get_product(product_id)

    assert requests == []


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_overview_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as aldi:
        roots = aldi.get_categories()

    (request,) = requests
    assert str(request.url) == f"{SITE}/productos.html"
    assert request.headers["Accept"].startswith("text/html")
    assert "X-Algolia-API-Key" not in request.headers
    assert [root.id for root in roots] == [
        "fruta-y-verdura",
        "lacteos-y-huevos",
        "marcas",
    ]
    assert all(root.children == () for root in roots)


def test_a_deep_tree_reads_one_page_per_top_level_category() -> None:
    requests: list[httpx.Request] = []
    root_page = read_fixture(STORE, "category_root.html")

    def every_root(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 2 and request.url.path != "/productos.html":
            return httpx.Response(200, request=request, html=root_page)
        return _aldi_handler(request)

    with client(requests, every_root) as aldi:
        roots = aldi.get_categories(deep=True)

    assert [item.url.path for item in requests] == [
        "/productos.html",
        "/productos/fruta-y-verdura.html",
        "/productos/lacteos-y-huevos.html",
        "/productos/marcas.html",
    ]
    assert all(len(root.children) == 2 for root in roots)


def test_a_child_category_is_listed_with_the_sites_own_filter() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as aldi:
        category = aldi.get_category(LEAF)

    page, query = requests
    assert str(page.url) == f"{SITE}/productos/{LEAF}.html"
    assert params_of(query) == {
        "query": [""],
        "offset": ["0"],
        "length": ["1000"],
        "attributesToHighlight": ["[]"],
        "filters": ["categoryIDs:leche-y-bebidas-vegetales"],
    }
    assert category.id == LEAF
    assert category.name == "Leche y bebidas vegetales"
    assert category.parent_id == "lacteos-y-huevos"
    assert category.product_count == 2
    assert [product.id for product in category.products] == ["970000", "872200"]


def test_a_top_level_category_lists_its_children_together() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as aldi:
        category = aldi.get_category("lacteos-y-huevos")

    assert params_of(requests[1])["filters"] == [
        'categoryIDs:"huevos" OR categoryIDs:"leche-y-bebidas-vegetales"'
    ]
    assert [child.id for child in category.children] == [
        "lacteos-y-huevos/huevos",
        f"{LEAF}",
    ]
    assert category.products


def test_a_category_with_nothing_to_list_costs_one_request() -> None:
    requests: list[httpx.Request] = []
    document = read_fixture(STORE, "category_root.html").replace(
        "PRODUCT_MGNL_CATEGORY_CHILDREN_GET", "SOMETHING_ELSE"
    )

    def bare(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, html=document)

    with client(requests, bare) as aldi:
        category = aldi.get_category("lacteos-y-huevos")

    assert len(requests) == 1
    assert category.children == ()
    assert category.products == ()
    assert category.product_count is None


def test_an_unknown_category_page_is_not_found() -> None:
    with client() as aldi, pytest.raises(NotFoundError):
        aldi.get_category("no-existe")


@pytest.mark.parametrize(
    "category_id", ["Lacteos", "a/b/c", "../x", "lacteos-y-huevos.html", "a b"]
)
def test_a_malformed_category_id_is_refused(category_id: str) -> None:
    with client() as aldi, pytest.raises(ConfigurationError, match="category_id"):
        aldi.get_category(category_id)


def test_a_page_without_embedded_data_is_invalid() -> None:
    def bare(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, html="<html></html>")

    with client(handler=bare) as aldi, pytest.raises(InvalidResponseError):
        aldi.get_categories()


def test_products_by_category_key() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as aldi:
        page = aldi.get_category_products("golden_seafood", page_size=1)

    assert params_of(requests[0])["filters"] == ['categoryIDs:"golden_seafood"']
    assert page.next_cursor == "1"


@pytest.mark.parametrize("key", ["", "a/b", "Frutas", 3, 'x" OR y'])
def test_a_malformed_category_key_is_refused(key: Any) -> None:
    with client() as aldi, pytest.raises(ConfigurationError, match="key"):
        aldi.get_category_products(key)


# --------------------------------------------------------- catalog and offers


def test_iter_catalog_reads_a_thousand_records_a_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests, synthetic_index(2342)) as aldi:
        catalog = aldi.get_catalog()

    assert [params_of(item)["offset"][0] for item in requests] == ["0", "1000", "2000"]
    assert all(params_of(item)["length"] == ["1000"] for item in requests)
    assert len(catalog) == 2342


def test_get_offers_reads_the_regions_offers_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region="can") as aldi:
        offers = aldi.get_offers()
        groups = aldi.get_offer_groups()

    assert [item.url.path for item in requests] == ["/can/ofertas.html"] * 2
    assert [product.id for product in offers] == [
        "994700",
        "600237700",
        "601593700",
        "601972400",
    ]
    assert all(product.region == "can" for product in offers)
    assert [group.title for group in groups] == [
        "Ofertas en fruta y verdura",
        "Flores y plantas",
    ]


def test_a_product_in_two_offer_sections_is_returned_once() -> None:
    document = read_fixture(STORE, "offers.html").replace(
        '\\"601972400\\"]', '\\"601972400\\", \\"994700\\"]'
    )

    def doubled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, html=document)

    with client(handler=doubled) as aldi:
        offers = aldi.get_offers()
        groups = aldi.get_offer_groups()

    assert len(groups[1].products) == 3
    assert [product.id for product in offers] == [
        "994700",
        "600237700",
        "601593700",
        "601972400",
    ]
