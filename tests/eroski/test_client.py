"""eroski's client: exact requests, paging, the menu cache and failures."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from supermercapy import (
    BlockedError,
    Capability,
    ConfigurationError,
    InvalidResponseError,
    Language,
    NotFoundError,
    RetryPolicy,
    TransportError,
)
from supermercapy.eroski import (
    Eroski,
    EroskiCategory,
    EroskiProduct,
    EroskiSearchResult,
)
from tests.conftest import read_fixture
from tests.harness import HARNESSES

STORE = "eroski"
SITE = "https://supermercado.eroski.es"
NO_BACKOFF = RetryPolicy(max_attempts=3, backoff_factor=0.0, jitter_ratio=0.0)


def recording(requests: list[httpx.Request], **options: Any) -> Eroski:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    client = HARNESSES[STORE].client(handler, **options)
    assert isinstance(client, Eroski)
    return client


def replying(response: httpx.Response, requests: list[httpx.Request]) -> Eroski:
    """build a client whose every request gets the same answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            response.status_code,
            request=request,
            content=response.content,
            headers=response.headers,
        )

    return Eroski(
        transport=httpx.MockTransport(handler),
        min_request_interval=0.0,
        retry_policy=NO_BACKOFF,
    )


def paths(requests: list[httpx.Request]) -> list[str]:
    return [request.url.path for request in requests]


# ------------------------------------------------------------------- lifecycle


def test_the_client_binds_nothing_and_paces_itself() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Eroski(transport=httpx.MockTransport(handler)) as client:
        assert client.store_id is None
        assert client.language is Language.SPANISH
        assert client.min_request_interval == 1.0
        assert client.user_agent.startswith("supermercapy/")
    assert Eroski.default_page_size == Eroski.max_page_size == 20
    assert Eroski.supported_languages == {
        Language.SPANISH,
        Language.CATALAN,
        Language.ENGLISH,
    }
    assert Eroski.capabilities == {
        Capability.CATALOG,
        Capability.NUTRITION,
        Capability.PROMOTIONS,
    }


def test_the_language_is_the_first_path_segment() -> None:
    requests: list[httpx.Request] = []

    with recording(requests, language="ca") as client:
        with pytest.raises(NotFoundError):
            client.get_product("3430469")
        client.search_products("llet")

    assert paths(requests) == [
        "/ca/productdetail/3430469-producto/",
        "/ca/search/results:loadpage",
    ]


# ---------------------------------------------------------------------- search


def test_search_asks_for_the_zone_fragment_the_infinite_scroll_loads() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        result = client.search_products("  aceite oliva ")

    (request,) = requests
    assert request.method == "GET"
    assert request.url.path == "/es/search/results:loadpage"
    assert dict(request.url.params) == {
        "q": "aceite oliva",
        "t:zoneid": "productListZone",
        "pageNumber": "0",
    }
    assert request.headers["Accept"] == "application/json"
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert isinstance(result, EroskiSearchResult)
    assert result.query == "  aceite oliva "
    assert [row.id for row in result.products] == ["3854569", "15923279", "3430469"]
    assert all(isinstance(row, EroskiProduct) for row in result.products)
    assert result.page_size == 20
    assert result.next_cursor == "1"
    assert result.has_more


def test_iter_search_walks_until_the_empty_page() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        rows = list(client.iter_search("aceite"))

    assert len(rows) == 3
    assert [request.url.params["pageNumber"] for request in requests] == ["0", "1"]


def test_a_smaller_page_is_cut_out_of_one_storefront_page() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        first = client.search_products("aceite", page_size=2)
        second = client.search_products("aceite", page_size=2, cursor=first.next_cursor)
        third = client.search_products("aceite", page_size=2, cursor=second.next_cursor)

    assert [row.id for row in first.products] == ["3854569", "15923279"]
    assert first.page_size == 2
    assert first.next_cursor == "0:2"
    assert [row.id for row in second.products] == ["3430469"]
    assert second.next_cursor == "1"
    assert third.products == ()
    assert third.next_cursor is None
    # the storefront page is asked for again until its rows run out
    assert [request.url.params["pageNumber"] for request in requests] == [
        "0",
        "0",
        "1",
    ]


@pytest.mark.parametrize("cursor", ["", "x", "1:", ":2", "1:2:3", "\u0661", 3])
def test_a_cursor_must_be_one_this_client_made(cursor: Any) -> None:
    requests: list[httpx.Request] = []

    with (
        recording(requests) as client,
        pytest.raises(ConfigurationError, match="cursor"),
    ):
        client.search_products("aceite", cursor=cursor)

    assert requests == []


@pytest.mark.parametrize("query", ["", "   ", None, 5])
def test_a_blank_or_missing_query_is_refused_before_any_request(query: Any) -> None:
    requests: list[httpx.Request] = []

    with (
        recording(requests) as client,
        pytest.raises(ConfigurationError, match="query"),
    ):
        client.search_products(query)

    assert requests == []


def test_a_page_size_above_twenty_is_refused() -> None:
    with Eroski() as client, pytest.raises(ConfigurationError, match="page_size"):
        client.search_products("aceite", page_size=21)


# --------------------------------------------------------------------- product


def test_get_product_reads_one_page_with_a_placeholder_slug() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        product = client.get_product(3430469)

    (request,) = requests
    assert request.url.path == "/es/productdetail/3430469-producto/"
    assert request.headers["Accept"] == "text/html,application/xhtml+xml"
    assert "X-Requested-With" not in request.headers
    assert isinstance(product, EroskiProduct)
    assert product.id == "3430469"
    assert product.url == f"{SITE}/es/productdetail/3430469-producto/"
    assert product.nutrition is not None


def test_an_unknown_product_redirects_to_the_error_page() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client, pytest.raises(NotFoundError, match="MP 1"):
        client.get_product("MP 1")

    (request,) = requests
    assert request.url.raw_path == b"/es/productdetail/MP%201-producto/"


def test_a_redirect_elsewhere_is_not_read_as_a_missing_product() -> None:
    requests: list[httpx.Request] = []
    response = httpx.Response(302, headers={"Location": "/es/login/only/"})

    with (
        replying(response, requests) as client,
        pytest.raises(InvalidResponseError, match="/es/login/only/"),
    ):
        client.get_product("1")
        with pytest.raises(InvalidResponseError, match="nowhere"):
            replying(httpx.Response(301), requests).get_product("1")


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_home_menu_in_one_request() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        roots = client.get_categories()

    assert paths(requests) == ["/es/"]
    assert requests[0].headers["Accept"] == "text/html,application/xhtml+xml"
    assert [root.id for root in roots] == ["2059806", "2059698", "6000510", "2060401"]
    assert all(isinstance(root, EroskiCategory) for root in roots)


def test_a_page_without_a_menu_is_invalid() -> None:
    requests: list[httpx.Request] = []
    response = httpx.Response(200, html="<html><body>mantenimiento</body></html>")

    with (
        replying(response, requests) as client,
        pytest.raises(InvalidResponseError, match="no category menu"),
    ):
        client.get_categories()


def test_a_redirected_home_page_is_invalid() -> None:
    requests: list[httpx.Request] = []
    response = httpx.Response(302, headers={"Location": "/error/error404/"})

    with (
        replying(response, requests) as client,
        pytest.raises(InvalidResponseError, match="redirected"),
    ):
        client.get_categories()


def test_get_category_reads_the_menu_once_and_then_the_listing() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        bananas = client.get_category("2059702")
        fruit = client.get_category(2059699)

    assert paths(requests) == [
        "/es/",
        "/es/supermarket:loadpage",
        "/es/supermarket:loadpage",
    ]
    assert dict(requests[1].url.params) == {
        "t:ac": "2059698-frescos/2059699-frutas/2059702-platanos-y-kiwis",
        "t:zoneid": "productListZone",
        "pageNumber": "0",
    }
    assert requests[2].url.params["t:ac"] == "2059698-frescos/2059699-frutas"
    assert bananas.name == "Plátanos y kiwis"
    assert bananas.parent_id == "2059699"
    assert [row.id for row in bananas.products] == ["12069175", "18702746"]
    assert bananas.product_count is None
    assert [child.id for child in fruit.children] == ["2059701", "2059702"]


def test_an_id_the_menu_does_not_hold_is_not_found() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client, pytest.raises(NotFoundError, match="999"):
        client.get_category("999")

    assert paths(requests) == ["/es/"]


def test_get_category_products_pages_a_category_like_search() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        client.get_categories()
        first = client.get_category_products("2059702")
        rest = client.get_category_products("2059702", cursor="0:1")
        beyond = client.get_category_products("2059702", cursor="1")

    assert [row.id for row in first.products] == ["12069175", "18702746"]
    # a one-page category says so itself, so no empty page is needed
    assert first.next_cursor is None
    assert [row.id for row in rest.products] == ["18702746"]
    assert rest.next_cursor is None
    assert beyond.products == ()
    assert [request.url.params.get("pageNumber") for request in requests] == [
        None,
        "0",
        "0",
        "1",
    ]


def test_iter_catalog_walks_every_root_listing_to_its_empty_page() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        rows = list(client.iter_catalog())
        catalog = client.get_catalog()

    # four roots of one canned page each, which says it is the last
    assert len(rows) == 8
    assert [row.id for row in catalog] == ["12069175", "18702746"]
    walk = requests[: len(requests) // 2]
    assert paths(walk) == ["/es/"] + ["/es/supermarket:loadpage"] * 4
    assert [request.url.params["t:ac"] for request in walk[1:]] == [
        "2059806-alimentacion",
        "2059698-frescos",
        "6000510-electrohogar",
        "2060401-higiene-y-belleza",
    ]


# -------------------------------------------------------------------- failures


def test_the_edges_bare_403_is_a_block_and_is_not_retried() -> None:
    requests: list[httpx.Request] = []
    response = httpx.Response(403, html="<title>403</title>403 Forbidden")

    with replying(response, requests) as client, pytest.raises(BlockedError):
        client.search_products("aceite")

    assert len(requests) == 1


def test_other_errors_keep_the_core_classification() -> None:
    requests: list[httpx.Request] = []

    with (
        replying(httpx.Response(500), requests) as client,
        pytest.raises(TransportError, match="500"),
    ):
        client.search_products("aceite")

    assert len(requests) == 3


def test_a_fragment_redirected_to_the_404_page_is_not_found() -> None:
    requests: list[httpx.Request] = []
    body = {"_tapestry": {"redirectURL": f"{SITE}:443/error/error404/"}}

    with (
        replying(httpx.Response(200, json=body), requests) as client,
        pytest.raises(NotFoundError, match="error404"),
    ):
        client.search_products("aceite")


def test_a_fragment_redirected_to_the_general_error_page_is_invalid() -> None:
    requests: list[httpx.Request] = []
    body = {"_tapestry": {"redirectURL": f"{SITE}:443/es/error/general/"}}

    with (
        replying(httpx.Response(200, json=body), requests) as client,
        pytest.raises(InvalidResponseError, match="general"),
    ):
        client.search_products("aceite")


def test_a_fragment_that_is_not_json_is_invalid() -> None:
    requests: list[httpx.Request] = []

    with (
        replying(httpx.Response(200, html="<html></html>"), requests) as client,
        pytest.raises(InvalidResponseError, match="invalid JSON"),
    ):
        client.search_products("aceite")


def test_reading_a_returned_model_performs_no_io() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        product = client.get_product("3430469")
        count = len(requests)
        _ = (product.nutrition, product.photos, product.category_path, product.price)

    assert len(requests) == count == 1
    assert read_fixture(STORE, "product.html")
