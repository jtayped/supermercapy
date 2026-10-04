"""ahorramás's client: exact requests, paging, the menu cache and failures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from supermercapy import (
    BlockedError,
    Capability,
    ChallengedError,
    ConfigurationError,
    InvalidResponseError,
    Language,
    NotFoundError,
    RetryPolicy,
    TransportError,
)
from supermercapy.ahorramas import (
    Ahorramas,
    AhorramasCategory,
    AhorramasProduct,
    AhorramasSearchResult,
)
from tests.conftest import read_fixture
from tests.harness import HARNESSES

STORE = "ahorramas"
CONTROLLER = "/on/demandware.store/Sites-Ahorramas-Site/es"
GRID = f"{CONTROLLER}/Search-UpdateGrid"
RECORD = f"{CONTROLLER}/Product-Variation"
NO_BACKOFF = RetryPolicy(max_attempts=3, backoff_factor=0.0, jitter_ratio=0.0)


def recording(requests: list[httpx.Request], **options: Any) -> Ahorramas:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    client = HARNESSES[STORE].client(handler, **options)
    assert isinstance(client, Ahorramas)
    return client


def replying(response: httpx.Response, requests: list[httpx.Request]) -> Ahorramas:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            response.status_code,
            request=request,
            content=response.content,
            headers=response.headers,
        )

    return Ahorramas(
        transport=httpx.MockTransport(handler),
        min_request_interval=0.0,
        retry_policy=NO_BACKOFF,
    )


# ------------------------------------------------------------------- lifecycle


def test_the_client_binds_nothing_speaks_spanish_and_paces_itself() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Ahorramas(transport=httpx.MockTransport(handler)) as client:
        assert client.store_id is None
        assert client.language is Language.SPANISH
        assert client.min_request_interval == 1.0
        assert client.user_agent.startswith("supermercapy/")
    assert Ahorramas.supported_languages == {Language.SPANISH}
    assert Ahorramas.default_page_size == 24
    assert Ahorramas.max_page_size == 100
    assert Ahorramas.capabilities == {
        Capability.CATALOG,
        Capability.OFFERS,
        Capability.PROMOTIONS,
    }
    with pytest.raises(ConfigurationError, match="language"):
        Ahorramas(language="ca")


# ---------------------------------------------------------------------- search


def test_search_asks_the_grid_the_more_results_button_loads() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        result = client.search_products("  agua ", page_size=10, cursor="20")

    (request,) = requests
    assert request.method == "GET"
    assert request.url.path == GRID
    assert dict(request.url.params) == {"q": "agua", "start": "20", "sz": "10"}
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert request.headers["Accept"] == "text/html,application/xhtml+xml"
    assert isinstance(result, AhorramasSearchResult)
    assert result.query == "  agua "
    assert result.offset == 20
    assert result.page_size == 10


def test_iter_search_ends_on_a_short_page() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        rows = list(client.iter_search("agua", page_size=3))

    assert len(rows) == 3
    # the canned page was full at three, so one more request found it empty
    assert [request.url.params["start"] for request in requests] == ["0", "3"]


@pytest.mark.parametrize("cursor", ["", "x", "-1", "\u0661", 3])
def test_a_cursor_must_be_an_offset(cursor: Any) -> None:
    requests: list[httpx.Request] = []

    with (
        recording(requests) as client,
        pytest.raises(ConfigurationError, match="cursor"),
    ):
        client.search_products("agua", cursor=cursor)

    assert requests == []


@pytest.mark.parametrize("query", ["", "  ", None])
def test_a_blank_query_is_refused_before_any_request(query: Any) -> None:
    requests: list[httpx.Request] = []

    with (
        recording(requests) as client,
        pytest.raises(ConfigurationError, match="query"),
    ):
        client.search_products(query)

    assert requests == []


def test_a_page_above_a_hundred_is_refused() -> None:
    with Ahorramas() as client, pytest.raises(ConfigurationError, match="page_size"):
        client.search_products("agua", page_size=101)


# --------------------------------------------------------------------- product


def test_get_product_reads_the_record_in_one_request() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        product = client.get_product(52028)

    (request,) = requests
    assert request.url.path == RECORD
    assert dict(request.url.params) == {"pid": "52028"}
    assert request.headers["Accept"] == "application/json"
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert isinstance(product, AhorramasProduct)
    assert product.id == "52028"


def test_an_unknown_product_is_a_500_that_reads_as_not_found() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client, pytest.raises(NotFoundError, match="99999999"):
        client.get_product("99999999")

    # a missing record is not retried as a server fault
    assert len(requests) == 1


def test_a_server_fault_elsewhere_is_still_retried() -> None:
    requests: list[httpx.Request] = []
    body = read_fixture(STORE, "product_missing.json")

    with (
        replying(httpx.Response(500, json=body), requests) as client,
        pytest.raises(TransportError, match="500"),
    ):
        client.search_products("agua")

    assert len(requests) == 3


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"action": "Product-Variation"}', b'{"error": {}}'],
)
def test_a_record_500_without_its_error_document_is_a_fault(body: bytes) -> None:
    requests: list[httpx.Request] = []

    with (
        replying(httpx.Response(500, content=body), requests) as client,
        pytest.raises(TransportError, match="500"),
    ):
        client.get_product("1")

    assert len(requests) == 3


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_home_menu() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        roots = client.get_categories()

    assert [request.url.path for request in requests] == ["/"]
    assert requests[0].headers["Accept"] == "text/html,application/xhtml+xml"
    assert [root.id for root in roots] == ["ofertas", "frescos", "limpieza", "hogar"]


def test_a_page_without_a_menu_is_invalid() -> None:
    requests: list[httpx.Request] = []

    with (
        replying(httpx.Response(200, html="<html></html>"), requests) as client,
        pytest.raises(InvalidResponseError, match="no category menu"),
    ):
        client.get_categories()


def test_get_category_reads_the_menu_once_and_checks_the_grid() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        lamb = client.get_category("cordero_y_cabrito", page_size=5)
        again = client.get_category("cordero_y_cabrito")

    assert [request.url.path for request in requests] == ["/", GRID, GRID]
    assert dict(requests[1].url.params) == {
        "cgid": "cordero_y_cabrito",
        "start": "0",
        "sz": "5",
    }
    assert isinstance(lamb, AhorramasCategory)
    assert lamb.name == "Cordero y lechal"
    assert lamb.parent_id == "carniceria"
    assert [row.id for row in lamb.products] == ["68505", "11989"]
    assert again.products == lamb.products


def test_an_id_the_menu_does_not_hold_is_not_found() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client, pytest.raises(NotFoundError, match="nada"):
        client.get_category("nada")

    assert [request.url.path for request in requests] == ["/"]


def test_a_grid_that_answers_for_another_category_is_not_found() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client, pytest.raises(NotFoundError, match="pollo"):
        # the menu knows pollo; the canned grid answers it with the whole catalogue
        client.get_category("pollo")

    assert [request.url.path for request in requests] == ["/", GRID]


def test_get_category_products_needs_no_menu() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        page = client.get_category_products("cordero_y_cabrito", cursor="24")

    (request,) = requests
    assert dict(request.url.params) == {
        "cgid": "cordero_y_cabrito",
        "start": "24",
        "sz": "24",
    }
    assert page.products == ()


def test_iter_catalog_walks_the_root_a_hundred_at_a_time() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        rows = list(client.iter_catalog())

    assert [row.id for row in rows] == ["72251", "48132"]
    (request,) = requests
    assert dict(request.url.params) == {"cgid": "root", "start": "0", "sz": "100"}


def test_get_offers_walks_the_offers_branch_to_its_end() -> None:
    requests: list[httpx.Request] = []

    class TwoAPage(Ahorramas):
        max_page_size = 2

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    with TwoAPage(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as client:
        offers = client.get_offers()

    assert [row.id for row in offers] == ["82364", "29366"]
    assert all(row.promotions for row in offers)
    assert [request.url.params["start"] for request in requests] == ["0", "2"]
    assert {request.url.params["cgid"] for request in requests} == {"ofertas"}


# -------------------------------------------------------------------- failures


def test_cloudflare_challenges_and_blocks_are_told_apart() -> None:
    requests: list[httpx.Request] = []
    challenge = httpx.Response(403, html="<title>Just a moment...</title>")
    flagged = httpx.Response(503, headers={"cf-mitigated": "challenge"})
    blocked = httpx.Response(403, html="<h1>Sorry, you have been blocked</h1>")

    with replying(challenge, requests) as client, pytest.raises(ChallengedError):
        client.search_products("agua")
    with replying(flagged, requests) as client, pytest.raises(ChallengedError):
        client.search_products("agua")
    with replying(blocked, requests) as client, pytest.raises(BlockedError):
        client.search_products("agua")

    # none of them is retried
    assert len(requests) == 3


def test_a_blocked_image_download_is_classified_from_its_stream(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = httpx.ByteStream(b"<h1>Sorry, you have been blocked</h1>")
        return httpx.Response(403, request=request, stream=body)

    with (
        Ahorramas(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BlockedError),
    ):
        client.download("https://www.ahorramas.com/dw/image/x.jpg", tmp_path / "x.jpg")

    assert len(requests) == 1
    assert not (tmp_path / "x.jpg").exists()
