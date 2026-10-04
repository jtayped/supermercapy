from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from supermercapy import (
    BlockedError,
    Capability,
    ConfigurationError,
    Dia,
    InvalidResponseError,
    Language,
    NotFoundError,
    OutOfCoverageError,
    RetryPolicy,
    TransportError,
)
from supermercapy.dia import DiaProduct, DiaSearchResult
from supermercapy.dia.client import search_window
from tests.conftest import read_fixture
from tests.harness import _dia_handler

STORE = "dia"
API = "https://www.dia.es/api/v1"
MENU = f"{API}/common-aggregator/menu-data"
SEARCH = f"{API}/search-back/search/reduced"
LEAF = "/huevos-leche-y-mantequilla/leche/c/L2051"

Handler = Callable[[httpx.Request], httpx.Response]


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


def calls(requests: list[httpx.Request]) -> list[str]:
    """return each request as ``METHOD path?query`` relative to the api."""

    lines = []
    for request in requests:
        url = str(request.url)
        assert url.startswith(API)
        lines.append(f"{request.method} {url[len(API) :]}")
    return lines


def client(
    requests: list[httpx.Request] | None = None,
    handler: Handler = _dia_handler,
    **options: Any,
) -> Dia:
    def record(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return handler(request)

    options.setdefault("min_request_interval", 0.0)
    return Dia(transport=httpx.MockTransport(record), **options)


def overriding(routes: dict[str, Handler]) -> Handler:
    """answer the paths in ``routes`` specially and the rest as the harness."""

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(request.url.path.removeprefix("/api/v1"))
        return route(request) if route else _dia_handler(request)

    return handler


def answer(status: int, **kwargs: Any) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, **kwargs)

    return handler


def synthetic_search(total: int) -> Handler:
    """a search answering ``total`` numbered rows for any page and page size."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["page_size"])
        first = (page - 1) * size
        rows = [
            {"sku_id": str(index), "display_name": f"product {index}"}
            for index in range(first, min(first + size, total))
        ]
        body = {"search_items": rows, "total_items": total, "locale": "es"}
        return httpx.Response(200, request=request, json=body)

    return handler


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_binds_nothing_by_default() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        assert dia.postal_code is None
        assert dia.store_id is None
        assert dia.physical_store_id is None
        assert dia.language is Language.SPANISH
        assert dia.user_agent.startswith("supermercapy/")

    assert requests == []
    assert Dia.default_min_request_interval == 0.5


def test_the_declared_capabilities_match_the_reconnaissance() -> None:
    assert Dia.capabilities == {
        Capability.POSTAL_CODE,
        Capability.CATALOG,
        Capability.NEW_ARRIVALS,
        Capability.OFFERS,
        Capability.NUTRITION,
        Capability.PROMOTIONS,
    }
    assert Dia.default_page_size == 30
    assert Dia.max_page_size == 1000


@pytest.mark.parametrize("language", ["vl", "pt", "fr"])
def test_unsupported_languages_are_rejected(language: str) -> None:
    with pytest.raises(ConfigurationError, match="language"):
        Dia(language=language)


@pytest.mark.parametrize("postal_code", ["", "1234", "00001", 8001])
def test_an_invalid_postcode_is_rejected(postal_code: Any) -> None:
    with pytest.raises(ConfigurationError, match="postal_code"):
        Dia(postal_code)


def test_an_unbound_spanish_client_restates_nothing() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        dia.get_categories()
        dia.get_categories()

    assert calls(requests) == [
        "GET /common-aggregator/menu-data",
        "GET /common-aggregator/menu-data",
    ]
    assert "Cookie" not in requests[0].headers
    assert requests[0].headers["Accept"] == "application/json"


def test_a_language_is_set_on_the_session_once_before_the_first_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests, language="ca") as dia:
        page = dia.search_products("llet")
        dia.get_categories()

    assert calls(requests) == [
        "PATCH /common-aggregator/current/locale",
        "GET /search-back/search/reduced?q=llet&page=1&page_size=30",
        "GET /common-aggregator/menu-data",
    ]
    assert json.loads(requests[0].content) == {"locale": "ca"}
    assert "canned_locale=ca" in requests[1].headers["Cookie"]
    assert page.products


def test_a_postcode_is_put_on_the_session_once_before_the_first_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests, postal_code="08001", language="en") as dia:
        assert dia.store_id == "08001"
        page = dia.search_products("milk")
        dia.search_products("milk")

    assert calls(requests) == [
        "PATCH /common-aggregator/current/locale",
        "PUT /common-aggregator/save-shipping-address?new_postal_code=08001",
        "GET /search-back/search/reduced?q=milk&page=1&page_size=30",
        "GET /search-back/search/reduced?q=milk&page=1&page_size=30",
    ]
    assert json.loads(requests[0].content) == {"locale": "en"}
    assert requests[1].content == b""
    assert page.postal_code == "08001"


def test_a_postcode_dia_does_not_serve_fails_at_the_first_request() -> None:
    requests: list[httpx.Request] = []

    with (
        client(requests, postal_code="35001") as dia,
        pytest.raises(OutOfCoverageError, match="35001"),
    ):
        dia.get_categories()

    assert calls(requests) == [
        "PUT /common-aggregator/save-shipping-address?new_postal_code=35001"
    ]


def test_the_session_is_restated_before_it_lapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("supermercapy._core.client.time.monotonic", lambda: clock[0])
    requests: list[httpx.Request] = []

    with client(requests, postal_code="08001") as dia:
        dia.get_categories()
        clock[0] += 44 * 60
        dia.get_categories()
        clock[0] += 2 * 60
        dia.get_categories()

    assert [line.split("?")[0] for line in calls(requests)] == [
        "PUT /common-aggregator/save-shipping-address",
        "GET /common-aggregator/menu-data",
        "GET /common-aggregator/menu-data",
        "PUT /common-aggregator/save-shipping-address",
        "GET /common-aggregator/menu-data",
    ]


def test_a_lost_session_is_restated_and_the_request_repeated() -> None:
    requests: list[httpx.Request] = []
    lost = [True]

    def forgetful(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/reduced") and lost[0]:
            lost[0] = False
            request.headers.pop("Cookie", None)
        return _dia_handler(request)

    with client(requests, forgetful, postal_code="08001") as dia:
        page = dia.search_products("leche")

    assert [line.split("?")[0] for line in calls(requests)] == [
        "PUT /common-aggregator/save-shipping-address",
        "GET /search-back/search/reduced",
        "PUT /common-aggregator/save-shipping-address",
        "GET /search-back/search/reduced",
    ]
    assert page.postal_code == "08001"


def test_a_session_that_will_not_stick_raises() -> None:
    def amnesiac(request: httpx.Request) -> httpx.Response:
        request.headers.pop("Cookie", None)
        return _dia_handler(request)

    with (
        client(handler=amnesiac, language="ca") as dia,
        pytest.raises(InvalidResponseError, match="locale es"),
    ):
        dia.get_product("504P6")


# --------------------------------------------------------------- postal codes


def test_from_postal_code_checks_the_postcode_and_moves_the_session() -> None:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _dia_handler(request)

    with Dia.from_postal_code(
        "08001", transport=httpx.MockTransport(record), min_request_interval=0
    ) as dia:
        assert dia.postal_code == dia.store_id == "08001"
        assert dia.physical_store_id == "959"
        page = dia.search_products("leche")

    assert calls(requests) == [
        "GET /common-aggregator/check-service?postal_code=08001",
        "PUT /common-aggregator/save-shipping-address?new_postal_code=08001",
        "GET /search-back/search/reduced?q=leche&page=1&page_size=30",
    ]
    assert page.postal_code == "08001"


@pytest.mark.parametrize(
    "check",
    [
        answer(206),
        answer(200, json={"physical_store_id": ""}),
        answer(200, json=["959"]),
    ],
)
def test_from_postal_code_refuses_a_postcode_without_a_store(check: Handler) -> None:
    requests: list[httpx.Request] = []
    handler = overriding({"/common-aggregator/check-service": check})

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    with pytest.raises(OutOfCoverageError, match="does not deliver to 35001"):
        Dia.from_postal_code("35001", transport=httpx.MockTransport(record))

    assert len(requests) == 1


def test_from_postal_code_reports_an_unreadable_check() -> None:
    handler = overriding({"/common-aggregator/check-service": answer(200, text="?")})

    with pytest.raises(InvalidResponseError, match="invalid JSON"):
        Dia.from_postal_code("08001", transport=httpx.MockTransport(handler))


def test_from_postal_code_passes_transport_failures_through() -> None:
    handler = overriding({"/common-aggregator/check-service": answer(400, json={})})

    with pytest.raises(TransportError, match="HTTP 400"):
        Dia.from_postal_code("08001", transport=httpx.MockTransport(handler))


def test_from_postal_code_validates_before_any_request() -> None:
    with pytest.raises(ConfigurationError):
        Dia.from_postal_code("abc")


def test_stores_are_not_listed() -> None:
    assert not Dia.supports(Capability.STORES)
    assert not Dia.supports(Capability.EAN_LOOKUP)


# ---------------------------------------------------------------------- search


def test_search_sends_the_documented_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        result = dia.search_products("leche sin lactosa")

    assert len(requests) == 1
    assert str(requests[0].url).startswith(f"{SEARCH}?")
    assert params_of(requests[0]) == {
        "q": ["leche sin lactosa"],
        "page": ["1"],
        "page_size": ["30"],
    }
    assert isinstance(result, DiaSearchResult)
    assert result.query == "leche sin lactosa"
    assert result.page_size == 30
    assert result.total_hits == 58
    assert result.postal_code == "28041"
    assert [product.id for product in result.products] == [
        "130063P6",
        "148777P6",
        "309574",
        "42070",
    ]
    assert result.next_cursor == "4"


def test_a_small_page_is_cut_from_the_thirty_row_minimum() -> None:
    requests: list[httpx.Request] = []

    with client(requests, synthetic_search(100)) as dia:
        first = dia.search_products("x", page_size=10)
        second = dia.search_products("x", page_size=10, cursor=first.next_cursor)
        straddling = dia.search_products("x", page_size=7, cursor="28")

    assert [params_of(item)["page_size"] for item in requests] == [
        ["30"],
        ["30"],
        ["35"],
    ]
    assert [product.id for product in first.products] == [str(i) for i in range(10)]
    assert first.next_cursor == "10"
    assert [product.id for product in second.products] == [
        str(i) for i in range(10, 20)
    ]
    assert [product.id for product in straddling.products] == [
        str(i) for i in range(28, 35)
    ]


def test_a_deep_page_is_read_wider_to_stay_within_fifty_pages() -> None:
    requests: list[httpx.Request] = []

    with client(requests, synthetic_search(2000)) as dia:
        page = dia.search_products("x", cursor="1500")

    assert params_of(requests[0])["page"] == ["45"]
    assert params_of(requests[0])["page_size"] == ["34"]
    assert [product.id for product in page.products][:2] == ["1500", "1501"]
    assert page.next_cursor == "1530"


def test_iter_search_walks_to_the_reported_total() -> None:
    requests: list[httpx.Request] = []

    with client(requests, synthetic_search(65)) as dia:
        rows = list(dia.iter_search("x"))

    assert [row.id for row in rows] == [str(i) for i in range(65)]
    assert len(requests) == 3


def test_a_search_without_a_total_goes_on_until_a_page_is_empty() -> None:
    def no_total(request: httpx.Request) -> httpx.Response:
        response = synthetic_search(40)(request)
        body = response.json()
        del body["total_items"]
        return httpx.Response(200, request=request, json=body)

    with client(handler=no_total) as dia:
        first = dia.search_products("x")
        second = dia.search_products("x", cursor=first.next_cursor)
        third = dia.search_products("x", cursor=second.next_cursor)

    assert first.total_hits is None
    assert (first.next_cursor, second.next_cursor, third.next_cursor) == (
        "30",
        "40",
        None,
    )


@pytest.mark.parametrize(
    ("offset", "size", "expected"),
    [
        (0, 30, (1, 30, 0)),
        (0, 1, (1, 30, 0)),
        (60, 30, (3, 30, 0)),
        (4, 30, (1, 34, 4)),
        (1470, 30, (50, 30, 0)),
        (0, 1000, (1, 1000, 0)),
        # no page of a thousand holds it whole, so the page comes back short
        (500, 1000, (1, 1000, 500)),
    ],
)
def test_the_search_window(
    offset: int, size: int, expected: tuple[int, int, int]
) -> None:
    page, width, start = search_window(offset, size)

    assert (page, width, start) == expected
    assert page <= 50
    assert 30 <= width <= 1000


def test_rows_past_fifty_thousand_cannot_be_reached() -> None:
    with pytest.raises(ConfigurationError, match="row 50000"):
        search_window(50_000, 1000)


@pytest.mark.parametrize("query", ["", "   ", None, 5])
def test_an_unusable_query_is_refused_before_any_request(query: Any) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia, pytest.raises(ConfigurationError, match="query"):
        dia.search_products(query)

    assert requests == []


@pytest.mark.parametrize("cursor", ["", "abc", "-1", "\u0661\u0662", 5])
def test_an_unusable_cursor_is_refused(cursor: Any) -> None:
    with client() as dia, pytest.raises(ConfigurationError, match="cursor"):
        dia.search_products("leche", cursor=cursor)


def test_page_size_is_capped_at_a_thousand() -> None:
    with client() as dia, pytest.raises(ConfigurationError, match="page_size"):
        dia.search_products("leche", page_size=1001)


def test_a_search_answer_without_rows_is_rejected() -> None:
    handler = overriding(
        {"/search-back/search/reduced": answer(200, json={"total_items": 1})}
    )

    with (
        client(handler=handler) as dia,
        pytest.raises(InvalidResponseError, match="search_items"),
    ):
        dia.search_products("leche")


# --------------------------------------------------------------------- product


def test_get_product_reads_the_sheet_in_one_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        product = dia.get_product("504P6")

    assert calls(requests) == ["GET /pdp-back/504P6"]
    assert isinstance(product, DiaProduct)
    assert product.id == "504P6"
    assert product.nutrition is not None


def test_an_unknown_sku_is_confirmed_by_search_and_not_retried() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia, pytest.raises(NotFoundError, match="no product 999"):
        dia.get_product(999)

    assert calls(requests) == [
        "GET /pdp-back/999",
        "GET /search-back/search/reduced?q=999&page=1&page_size=30",
    ]


def test_a_sheet_failing_for_a_listed_sku_is_a_transport_error() -> None:
    sheet_error = read_fixture(STORE, "product_error.json")
    handler = overriding({"/pdp-back/130063P6": answer(500, json=sheet_error)})

    with (
        client(handler=handler) as dia,
        pytest.raises(TransportError, match="search still lists") as raised,
    ):
        dia.get_product("130063P6")

    assert raised.value.status_code == 500


def test_another_server_error_on_the_sheet_is_retried_as_usual() -> None:
    requests: list[httpx.Request] = []
    handler = overriding({"/pdp-back/504P6": answer(500, text="upstream")})

    with (
        client(
            requests,
            handler,
            retry_policy=RetryPolicy(max_attempts=2, backoff_factor=0),
        ) as dia,
        pytest.raises(TransportError, match="HTTP 500"),
    ):
        dia.get_product("504P6")

    assert len(requests) == 2


@pytest.mark.parametrize("sku", ["50/4P6", "504 P6", "../menu"])
def test_a_malformed_sku_is_refused_before_any_request(sku: str) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia, pytest.raises(ConfigurationError, match="sku"):
        dia.get_product(sku)

    assert requests == []


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_tree_in_one_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        tree = dia.get_categories()

    assert calls(requests) == ["GET /common-aggregator/menu-data"]
    assert [root.id for root in tree] == ["L128", "L105", "L108"]


def test_a_child_category_is_listed_by_its_storefront_path() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        category = dia.get_category("L2051")

    assert calls(requests) == [
        "GET /common-aggregator/menu-data",
        f"GET /plp-back/reduced{LEAF}",
    ]
    assert category.name == "Leche"
    assert category.product_count == 50
    assert [product.id for product in category.products] == [
        "504P6",
        "608P6",
        "607P6",
    ]
    assert all(product.category_ids == ("L2051",) for product in category.products)


def test_a_top_level_category_is_listed_child_by_child() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        category = dia.get_category("L108")

    assert calls(requests)[1] == "GET /plp-back/l1/all/L108/reduced"
    assert category.product_count == 203
    assert [child.id for child in category.children] == ["L2055", "L2051"]
    assert category.products
    assert all(product.category_ids == ("L2055",) for product in category.products)


def test_a_group_without_a_listing_of_its_own_has_no_products() -> None:
    with client() as dia:
        category = dia.get_category("L128")

    assert category.products == ()
    assert category.product_count is None
    assert [child.id for child in category.children] == ["L2302", "L2352"]


def test_an_unknown_category_costs_one_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia, pytest.raises(NotFoundError, match="L999"):
        dia.get_category("L999")

    assert len(requests) == 1


@pytest.mark.parametrize("category_id", ["2051", "l2051", "L20 51", "L2051/x"])
def test_a_malformed_category_id_is_refused(category_id: str) -> None:
    with client() as dia, pytest.raises(ConfigurationError, match="category_id"):
        dia.get_category(category_id)


def test_a_child_listing_pages_by_its_own_next_path() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        first = dia.get_category_products("L2051")
        second = dia.get_category_products("L2051", cursor=first.next_cursor)
        last = dia.get_category_products("L2051", cursor=second.next_cursor)

    assert first.next_cursor == f"/reduced{LEAF}?page=2"
    assert calls(requests) == [
        "GET /common-aggregator/menu-data",
        f"GET /plp-back/reduced{LEAF}",
        f"GET /plp-back/reduced{LEAF}?page=2",
        f"GET /plp-back/reduced{LEAF}?page=3",
    ]
    assert first.category_id == "L2051"
    assert first.page_size == 20
    assert last.next_cursor is None


def test_a_top_level_listing_follows_the_storefront_walk() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        first = dia.get_category_products("L108")
        last = dia.get_category_products("L108", cursor=first.next_cursor)

    assert first.next_cursor == "/l1/all/L108/reduced?category_id=L2051&page=1"
    assert calls(requests)[-1] == (
        "GET /plp-back/l1/all/L108/reduced?category_id=L2051&page=1"
    )
    assert first.category_id == "L2055"
    assert last.category_id == "L2340"
    assert last.next_cursor is None
    assert last.page_size == len(last.products)


@pytest.mark.parametrize(
    "cursor",
    [
        "/reduced/huevos/c/L2055?page=2",
        "/l1/all/L105/reduced?category_id=L2051&page=1",
        "/reduced/../c/L2051?page=2",
        "https://evil.example/reduced/x/c/L2051?page=2",
        "",
        3,
    ],
)
def test_a_listing_cursor_must_belong_to_the_category(cursor: Any) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia, pytest.raises(ConfigurationError, match="cursor"):
        dia.get_category_products("L2051", cursor=cursor)

    assert requests == []


def test_a_child_listing_that_redirects_is_reported() -> None:
    handler = overriding(
        {f"/plp-back/reduced{LEAF}": answer(301, headers={"Location": "/x"})}
    )

    with client(handler=handler) as dia, pytest.raises(TransportError, match="to /x"):
        dia.get_category("L2051")


def test_a_listing_page_without_paging_ends_the_walk() -> None:
    listing = read_fixture(STORE, "listing.json")
    del listing["current_category_url"]
    del listing["pagination"]
    handler = overriding({f"/plp-back/reduced{LEAF}": answer(200, json=listing)})

    with client(handler=handler) as dia:
        page = dia.get_category_products("L2051")

    assert page.category_id == "L2051"
    assert page.next_cursor is None
    assert page.page_size == 3


def test_a_listing_page_names_its_category_from_its_path_when_needed() -> None:
    listing = read_fixture(STORE, "listing.json")
    del listing["selected_category_id"]
    handler = overriding({f"/plp-back/reduced{LEAF}": answer(200, json=listing)})

    with client(handler=handler) as dia:
        assert dia.get_category_products("L2051").category_id == "L2051"


def test_a_walk_step_pointing_elsewhere_ends_the_walk() -> None:
    root = read_fixture(STORE, "listing_root.json")
    root["pagination"]["next_url"] = "/api/v1/search-back/search/reduced?q=x"
    handler = overriding({"/plp-back/l1/all/L108/reduced": answer(200, json=root)})

    with client(handler=handler) as dia:
        assert dia.get_category_products("L108").next_cursor is None


# ----------------------------------------------------- catalog and new arrivals


def test_iter_catalog_walks_every_top_level_category() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        rows = list(dia.iter_catalog())
        catalog = dia.get_catalog()

    walk = calls(requests)[: len(requests) // 2]
    assert walk == [
        "GET /common-aggregator/menu-data",
        "GET /plp-back/l1/all/L128/reduced",
        "GET /plp-back/l1/all/L105/reduced",
        "GET /plp-back/l1/all/L108/reduced",
        "GET /plp-back/l1/all/L108/reduced?category_id=L2051&page=1",
    ]
    assert len(rows) == 6
    assert len(catalog) == 4
    assert len({product.id for product in catalog}) == len(catalog)


def test_a_walk_that_repeats_its_step_stops() -> None:
    root = read_fixture(STORE, "listing_root.json")
    root["pagination"]["next_url"] = "/api/v1/plp-back/l1/all/L108/reduced"
    handler = overriding({"/plp-back/l1/all/L108/reduced": answer(200, json=root)})
    requests: list[httpx.Request] = []

    with client(requests, handler) as dia:
        rows = list(dia._walk_listing(dia._find_category("L108")))

    assert len(rows) == 2
    assert len(requests) == 2


def test_new_arrivals_walk_the_novedades_category() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        arrivals = dia.get_new_arrivals()

    assert calls(requests) == [
        "GET /common-aggregator/menu-data",
        "GET /plp-back/reduced/novedades-y-recomendados/novedades/c/L2302",
        "GET /plp-back/reduced/novedades-y-recomendados/novedades/c/L2302?page=2",
    ]
    assert [product.id for product in arrivals] == ["310173", "310294"]
    assert all(product.is_new for product in arrivals)
    assert all(product.stamp == "Novedad" for product in arrivals)


# ---------------------------------------------------------------------- offers


def test_get_offers_chains_each_category_offer_listing() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        offers = dia.get_offers()

    assert calls(requests) == [
        "GET /plp-back/offers/reduced",
        "GET /plp-back/offers/reduced/L128",
        "GET /plp-back/offers/reduced/L128?page=2",
        "GET /plp-back/offers/reduced/L112",
    ]
    assert [product.id for product in offers] == ["17548P6", "167442", "184840"]
    assert offers[0].category_ids == ("L128",)
    assert offers[0].promotions


def test_no_offer_groups_means_no_offers() -> None:
    requests: list[httpx.Request] = []
    empty = {"plp_items": [], "total_items": 0}
    handler = overriding({"/plp-back/offers/reduced": answer(200, json=empty)})

    with client(requests, handler) as dia:
        assert dia.get_offers() == ()

    assert len(requests) == 1


def test_an_offer_chain_that_loops_back_stops() -> None:
    looping = read_fixture(STORE, "category_offers_last.json")
    looping["next_category"] = {"category_id": "L128"}
    handler = overriding({"/plp-back/offers/reduced/L112": answer(200, json=looping)})
    requests: list[httpx.Request] = []

    with client(requests, handler) as dia:
        offers = dia.get_offers()

    assert len(requests) == 4
    assert len(offers) == 3


def test_get_category_offers_reads_one_category() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as dia:
        offers = dia.get_category_offers("L123")

    assert calls(requests) == ["GET /plp-back/offers/reduced/L123"]
    assert [product.id for product in offers] == ["184840"]


def test_an_empty_or_unpaged_offer_listing_ends_the_category() -> None:
    unpaged = read_fixture(STORE, "category_offers.json")
    del unpaged["pagination"]
    empty = {**read_fixture(STORE, "category_offers.json"), "plp_items": []}
    handler = overriding(
        {
            "/plp-back/offers/reduced/L1": answer(200, json=unpaged),
            "/plp-back/offers/reduced/L2": answer(200, json=empty),
        }
    )
    requests: list[httpx.Request] = []

    with client(requests, handler) as dia:
        assert len(dia.get_category_offers("L1")) == 2
        assert dia.get_category_offers("L2") == ()

    assert len(requests) == 2


# ------------------------------------------------------------ error handling


def test_akamai_access_denied_is_a_block_and_not_retried() -> None:
    requests: list[httpx.Request] = []
    page = read_fixture(STORE, "access_denied.html")
    handler = overriding({"/common-aggregator/menu-data": answer(403, html=page)})

    with client(requests, handler) as dia, pytest.raises(BlockedError) as raised:
        dia.get_categories()

    assert raised.value.status_code == 403
    assert len(requests) == 1


def test_the_edge_bloqueado_page_is_a_block() -> None:
    page = read_fixture(STORE, "blocked.html")
    handler = overriding({"/search-back/search/reduced": answer(404, html=page)})

    with client(handler=handler) as dia, pytest.raises(BlockedError, match="404"):
        dia.search_products("leche")


def test_an_api_404_is_not_found() -> None:
    with client() as dia, pytest.raises(NotFoundError):
        dia.get_category_products("L2051", cursor="/reduced/x/c/L2051?page=2")


@pytest.mark.parametrize(
    ("body", "message"),
    [({"text": "not json"}, "invalid JSON"), ({"json": [1]}, "non-object")],
)
def test_an_unreadable_listing_is_rejected(body: dict[str, Any], message: str) -> None:
    handler = overriding({"/plp-back/offers/reduced/L1": answer(200, **body)})

    with (
        client(handler=handler) as dia,
        pytest.raises(InvalidResponseError, match=message),
    ):
        dia.get_category_offers("L1")


def test_download_streams_a_product_image(tmp_path: Path) -> None:
    def image(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/product_images/504P6/504P6_ISO_0_ES.jpg"
        return httpx.Response(200, request=request, content=b"\xff\xd8\xff\xe0jpeg")

    with client(handler=image) as dia:
        path = dia.download(
            "https://www.dia.es/product_images/504P6/504P6_ISO_0_ES.jpg",
            tmp_path / "milk.jpg",
        )

    assert path.read_bytes().startswith(b"\xff\xd8")
