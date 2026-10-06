from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from supermercapy import (
    AuthenticationError,
    ConfigurationError,
    InvalidResponseError,
    Language,
    NotFoundError,
    OutOfCoverageError,
    Plusfresc,
    TransportError,
    Unit,
    UnitPrice,
)
from supermercapy.plusfresc import PlusfrescPhoto
from tests.conftest import read_fixture

STORE = "plusfresc"
BASE = "https://wscompra.plusfresc.cat/api"
IMAGES = "https://compra.plusfresc.cat/ImatgesProductes"
TOKEN = read_fixture(STORE, "guest_token.json")
EXPIRED_TOKEN = read_fixture(STORE, "guest_token_expired.json")
# obviously fake payload segments, to exercise the expiry reader's giving up.
NO_EXPIRY_PAYLOAD = "eyJyb2xlIjogIkd1ZXN0In0"
NON_OBJECT_PAYLOAD = "Imp1c3QgYSBzdHJpbmci"
BOOLEAN_EXPIRY_PAYLOAD = "eyJleHAiOiB0cnVlfQ"

Handler = Callable[[httpx.Request], httpx.Response]


def json_response(
    request: httpx.Request, data: object, status: int = 200
) -> httpx.Response:
    return httpx.Response(status, request=request, json=data)


def routed(
    requests: list[httpx.Request],
    routes: dict[str, object],
    *,
    minted: object = TOKEN,
) -> Handler:
    """serve one canned payload per path and record every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.startswith("/api/loginGuest/"):
            return json_response(request, minted)
        if path in routes:
            payload = routes[path]
            if isinstance(payload, httpx.Response):
                return payload
            return json_response(request, payload)
        return httpx.Response(404, request=request)

    return handler


def make_client(
    handler: Handler, *, center: int | str = 12, **options: Any
) -> Plusfresc:
    options.setdefault("min_request_interval", 0)
    return Plusfresc(center, transport=httpx.MockTransport(handler), **options)


def fixture_client(
    requests: list[httpx.Request], routes: dict[str, object], **options: Any
) -> Plusfresc:
    return make_client(routed(requests, routes), **options)


SEARCH = "/api/search/languages/ca/llet/products/12"
DETAIL = "/api/productdetails/files/12/002530/ca"


def search_routes(name: str = "search.json") -> dict[str, object]:
    return {SEARCH: read_fixture(STORE, name)}


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_exposes_the_center() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with make_client(handler) as client:
        assert client.center == 12
        assert client.store_id == "12"
        assert client.language is Language.CATALAN


def test_the_default_center_is_the_storefronts_own() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Plusfresc(transport=httpx.MockTransport(handler)) as client:
        assert client.center == 12


def test_locker_ids_resolve_to_their_parent_center() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with make_client(handler, center="127899871") as client:
        assert client.center == 12
        assert client.store_id == "12"


@pytest.mark.parametrize("center", ["", "  ", "abc", 0, -1, True, 1.5, "12a", None])
def test_invalid_centers_are_rejected(center: Any) -> None:
    with pytest.raises(ConfigurationError, match="center"):
        Plusfresc(center)


@pytest.mark.parametrize("language", ["en", "vl", "fr", ""])
def test_unsupported_languages_are_rejected(language: str) -> None:
    with pytest.raises(ConfigurationError, match="language"):
        Plusfresc(12, language=language)


def test_the_shipped_defaults_pace_requests_and_identify_supermercapy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    assert Plusfresc.default_min_request_interval == 0.5
    with Plusfresc(12, transport=httpx.MockTransport(handler)) as client:
        assert client.min_request_interval == 0.5
        assert client.user_agent.startswith("supermercapy/")


# ------------------------------------------------------------------------ auth


def test_the_guest_token_is_minted_for_the_bound_center_and_then_sent() -> None:
    requests: list[httpx.Request] = []

    routes = {
        "/api/search/languages/ca/llet/products/3": read_fixture(STORE, "search.json")
    }
    with fixture_client(requests, routes, center=3) as client:
        client.search_products("llet")

    mint, search = requests
    assert mint.method == "POST"
    assert str(mint.url) == f"{BASE}/loginGuest/3"
    assert mint.content == b'""'
    assert mint.headers["Content-Type"] == "application/json"
    # the mint itself must never carry a bearer, stale or otherwise.
    assert "Authorization" not in mint.headers
    assert search.headers["Authorization"] == f"Bearer {TOKEN}"


def test_public_endpoints_cost_no_token_but_carry_one_once_it_exists() -> None:
    requests: list[httpx.Request] = []
    routes: dict[str, object] = {
        "/api/categories/tree/12/Root": read_fixture(STORE, "tree.json"),
        **search_routes(),
    }

    with fixture_client(requests, routes) as client:
        client.get_categories()
        client.search_products("llet")
        client.get_categories()

    assert [request.url.path for request in requests] == [
        "/api/categories/tree/12/Root",
        "/api/loginGuest/12",
        "/api/search/languages/ca/llet/products/12",
        "/api/categories/tree/12/Root",
    ]
    assert "Authorization" not in requests[0].headers
    assert requests[3].headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_live_token_is_reused_across_authenticated_calls() -> None:
    requests: list[httpx.Request] = []
    routes: dict[str, object] = {
        **search_routes(),
        DETAIL: read_fixture(STORE, "product.json"),
    }

    with fixture_client(requests, routes) as client:
        client.search_products("llet")
        client.get_product("002530")

    assert [request.url.path for request in requests].count("/api/loginGuest/12") == 1


def test_a_token_inside_its_refresh_margin_is_minted_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    # the canned token expires at 4102444800; stand just inside the margin.
    monkeypatch.setattr(
        "supermercapy.plusfresc.client.time.time", lambda: 4102444800 - 30.0
    )

    with fixture_client(requests, search_routes()) as client:
        client.search_products("llet")
        client.search_products("llet")

    assert [request.url.path for request in requests].count("/api/loginGuest/12") == 2


def test_a_token_without_a_readable_expiry_falls_back_to_the_thirty_minute_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    clock = [1000.0]
    monkeypatch.setattr("supermercapy.plusfresc.client.time.time", lambda: clock[0])
    handler = routed(requests, search_routes(), minted="not-a-jwt")

    with make_client(handler) as client:
        client.search_products("llet")
        clock[0] += 1700.0
        client.search_products("llet")
        clock[0] += 100.0
        client.search_products("llet")

    assert [request.url.path for request in requests].count("/api/loginGuest/12") == 2


@pytest.mark.parametrize(
    "minted",
    [
        "not-a-jwt",
        "header.???not-base64???.signature",
        f"header.{NO_EXPIRY_PAYLOAD}.signature",
        f"header.{NON_OBJECT_PAYLOAD}.signature",
        f"header.{BOOLEAN_EXPIRY_PAYLOAD}.signature",
    ],
    ids=["no-segments", "unreadable", "no-exp", "non-object", "boolean-exp"],
)
def test_a_token_with_no_readable_expiry_is_still_usable(
    minted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr("supermercapy.plusfresc.client.time.time", lambda: 1000.0)
    handler = routed(requests, search_routes(), minted=minted)

    with make_client(handler) as client:
        client.search_products("llet")
        client.search_products("llet")

    # the thirty-minute fallback keeps the token alive within the same minute.
    assert [request.url.path for request in requests].count("/api/loginGuest/12") == 1


def test_an_expired_token_is_reminted_once_after_a_401() -> None:
    requests: list[httpx.Request] = []
    rows = read_fixture(STORE, "search.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return json_response(request, TOKEN)
        if len([item for item in requests if item.method == "POST"]) < 2:
            return json_response(
                request,
                {"Message": "Se ha denegado la autorización para esta solicitud."},
                status=401,
            )
        return json_response(request, rows)

    with make_client(handler) as client:
        result = client.search_products("llet")

    assert [request.method for request in requests] == ["POST", "GET", "POST", "GET"]
    assert result.products


def test_a_second_401_raises_an_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return json_response(request, TOKEN)
        return json_response(request, {"Message": "denegada"}, status=401)

    with make_client(handler) as client, pytest.raises(AuthenticationError):
        client.search_products("llet")


def test_a_failed_remint_surfaces_as_an_authentication_error() -> None:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls[0] += 1
            if calls[0] == 1:
                return json_response(request, EXPIRED_TOKEN)
            return httpx.Response(500, request=request, json={})
        return json_response(request, {"Message": "denegada"}, status=401)

    policy_client = make_client(handler)
    with policy_client as client, pytest.raises(AuthenticationError):
        client.search_products("llet")


def test_a_blank_token_response_raises_an_authentication_error() -> None:
    requests: list[httpx.Request] = []
    handler = routed(requests, search_routes(), minted="   ")

    with (
        make_client(handler) as client,
        pytest.raises(AuthenticationError, match="tok"),
    ):
        client.search_products("llet")


def test_a_401_on_the_mint_itself_raises_without_minting_again() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(request, {"Message": "denegada"}, status=401)

    with make_client(handler) as client, pytest.raises(AuthenticationError):
        client.search_products("llet")

    # it used to re-mint from inside the mint until python's recursion limit.
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/api/loginGuest/12")
    ]


def test_a_remint_answered_with_a_401_stops_after_one_attempt() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        mints = [item for item in requests if item.method == "POST"]
        if request.method == "POST" and len(mints) == 1:
            return json_response(request, TOKEN)
        return json_response(request, {"Message": "denegada"}, status=401)

    with make_client(handler) as client, pytest.raises(AuthenticationError):
        client.search_products("llet")

    assert [request.method for request in requests] == ["POST", "GET", "POST"]


# ---------------------------------------------------------------------- search


def test_search_puts_the_language_and_query_in_the_path() -> None:
    requests: list[httpx.Request] = []
    routes = {
        "/api/search/languages/es/oli oliva/products/12": read_fixture(
            STORE, "search.json"
        )
    }

    with fixture_client(requests, routes, language="es") as client:
        client.search_products("oli oliva")

    assert str(requests[1].url) == f"{BASE}/search/languages/es/oli%20oliva/products/12"
    assert requests[1].url.query == b""


def test_search_slices_one_unpaged_response_client_side() -> None:
    requests: list[httpx.Request] = []

    with fixture_client(requests, search_routes()) as client:
        first = client.search_products("llet", page_size=2)
        second = client.search_products("llet", page_size=2, cursor=first.next_cursor)
        last = client.search_products("llet", page_size=2, cursor="4")

    assert [request.url.path for request in requests].count(SEARCH) == 3
    assert first.query == "llet"
    assert first.page_size == 2
    assert first.offset == 0
    assert first.row_count == 6
    assert first.total_hits == 6
    assert first.next_cursor == "2"
    assert second.offset == 2
    assert second.next_cursor == "4"
    assert last.next_cursor is None
    assert last.has_more is False
    assert [item.id for item in first.products] != [item.id for item in second.products]


def test_a_page_of_exactly_one_hundred_rows_is_flagged_as_truncated() -> None:
    requests: list[httpx.Request] = []

    with fixture_client(requests, search_routes("search_capped.json")) as client:
        capped = client.search_products("llet", page_size=100)
        short = client.search_products("llet", page_size=100)

    assert capped.row_count == 100
    assert capped.truncated is True
    assert capped.total_hits == 100
    assert capped.next_cursor is None
    assert short.truncated is True


def test_a_short_page_is_not_truncated() -> None:
    requests: list[httpx.Request] = []

    with fixture_client(requests, search_routes()) as client:
        assert client.search_products("llet").truncated is False


def test_iter_search_walks_every_page_of_one_response() -> None:
    requests: list[httpx.Request] = []

    with fixture_client(requests, search_routes()) as client:
        streamed = list(client.iter_search("llet", page_size=2))

    assert len(streamed) == 6
    assert len({item.id for item in streamed}) == 6


@pytest.mark.parametrize("query", ["", " ", "a", " l "])
def test_queries_below_two_characters_are_rejected_before_any_request(
    query: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a rejected query performed I/O")

    with (
        make_client(handler) as client,
        pytest.raises(ConfigurationError, match="quer"),
    ):
        client.search_products(query)


@pytest.mark.parametrize(
    ("query", "segment"),
    [
        ("llet 1/2", "llet 1 2"),
        ("oli 0,4%", "oli 0,4"),
        ("oli+verge", "oli verge"),
        ("a&b:c*d?e<f>g\\h", "a b c d e f g h"),
        ("llet.", "llet"),
        ("oli verge. ", "oli verge"),
        ("  llet   sencera ", "llet sencera"),
        ("d'oliva", "d'oliva"),
    ],
)
def test_query_characters_the_path_cannot_carry_are_sent_as_spaces(
    query: str, segment: str
) -> None:
    requests: list[httpx.Request] = []
    routes = {
        f"/api/search/languages/ca/{segment}/products/12": read_fixture(
            STORE, "search.json"
        )
    }

    with fixture_client(requests, routes) as client:
        result = client.search_products(query)

    # iis answered 404 for a slash, plus or trailing dot and 400 for the rest.
    assert requests[1].url.path == f"/api/search/languages/ca/{segment}/products/12"
    assert result.query == query
    assert result.products


@pytest.mark.parametrize("query", ["%%", "./", " . ", "a/"])
def test_a_query_with_nothing_routable_left_is_rejected_before_any_request(
    query: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a rejected query performed I/O")

    with (
        make_client(handler) as client,
        pytest.raises(ConfigurationError, match="quer"),
    ):
        client.search_products(query)


@pytest.mark.parametrize("query", [None, 7, b"llet"])
def test_non_string_queries_are_rejected(query: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a rejected query performed I/O")

    with (
        make_client(handler) as client,
        pytest.raises(ConfigurationError, match="quer"),
    ):
        client.search_products(query)


@pytest.mark.parametrize("cursor", ["", "abc", "-1", 4, True])
def test_invalid_cursors_are_rejected(cursor: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a rejected cursor performed I/O")

    with (
        make_client(handler) as client,
        pytest.raises(ConfigurationError, match="cursor"),
    ):
        client.search_products("llet", cursor=cursor)


@pytest.mark.parametrize("page_size", [0, -1, 101, 1.5, True])
def test_page_sizes_outside_the_search_cap_are_rejected(page_size: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a rejected page size performed I/O")

    with (
        make_client(handler) as client,
        pytest.raises(ConfigurationError, match="page_size"),
    ):
        client.search_products("llet", page_size=page_size)


def test_a_non_array_search_response_is_rejected() -> None:
    requests: list[httpx.Request] = []

    with (
        fixture_client(requests, {SEARCH: {"products": []}}) as client,
        pytest.raises(InvalidResponseError, match="search"),
    ):
        client.search_products("llet")


# --------------------------------------------------------------------- product


def test_get_product_sends_the_documented_request() -> None:
    requests: list[httpx.Request] = []
    routes = {DETAIL: read_fixture(STORE, "product.json")}

    with fixture_client(requests, routes) as client:
        product = client.get_product("002530")

    assert str(requests[1].url) == f"{BASE}/productdetails/files/12/002530/ca"
    assert product.id == "002530"
    assert product.item_id == "002530"


def test_the_product_key_is_the_item_id_not_the_placement_id() -> None:
    requests: list[httpx.Request] = []
    routes = {DETAIL: read_fixture(STORE, "product.json")}

    with fixture_client(requests, routes) as client:
        product = client.get_product("002530")

    assert product.id == "002530"
    assert product.placement_id == "01050203002530"
    assert product.leaf_category_id == "01050203"


def test_a_composite_placement_id_is_reported_as_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return json_response(request, TOKEN)
        return json_response(request, "ERROR ENTRADA", status=416)

    with make_client(handler) as client, pytest.raises(NotFoundError):
        client.get_product("01050203002530")


def test_an_unknown_item_id_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return json_response(request, TOKEN)
        return json_response(request, "ItemID not visible from perspective 0", 404)

    with make_client(handler) as client, pytest.raises(NotFoundError):
        client.get_product("999999")


@pytest.mark.parametrize("product_id", ["", "   ", True, [], None])
def test_invalid_product_ids_are_rejected_before_any_request(product_id: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a rejected product id performed I/O")

    with make_client(handler) as client, pytest.raises(ConfigurationError):
        client.get_product(product_id)


def test_the_product_sheet_free_text_stays_spanish_in_catalan() -> None:
    requests: list[httpx.Request] = []
    routes = {DETAIL: read_fixture(STORE, "product.json")}

    with fixture_client(requests, routes) as client:
        product = client.get_product("002530")

    assert product.name == "Assortiment de galetes BIRBA, capsa 560 grams"
    assert product.name_es == "Surtido de galletas BIRBA, caja 560 gramos"
    # filedetails is spanish-only upstream, even for a catalan sheet.
    assert product.description == "Surtido Camprodon 560g"
    assert product.storage is not None
    assert product.storage.startswith("Conservar en lugar fresco")
    assert product.nutrition is not None
    assert product.nutrition.ingredients is not None
    assert product.nutrition.ingredients.startswith("Harina de trigo")
    # the nutrition table and the allergens are localised, though.
    assert product.nutrition.values[0].name == "Valor energètic"


# ------------------------------------------------------------------ categories


def test_get_categories_costs_one_request_for_the_whole_tree() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/categories/tree/12/Root": read_fixture(STORE, "tree.json")}

    with fixture_client(requests, routes) as client:
        categories = client.get_categories()

    assert len(requests) == 1
    assert str(requests[0].url) == f"{BASE}/categories/tree/12/Root"
    assert [category.id for category in categories] == [
        "PromoHighlight",
        "Oferta2",
        "01",
        "40",
    ]
    assert categories[2].children[0].parent_id == "01"


def test_get_category_fetches_the_subtree_and_its_listing() -> None:
    requests: list[httpx.Request] = []
    routes = {
        "/api/categories/tree/12/010101": read_fixture(STORE, "tree_010101.json"),
        "/api/products/category/010101/12": read_fixture(STORE, "listing.json"),
    }

    with fixture_client(requests, routes) as client:
        category = client.get_category("010101")

    assert [str(request.url) for request in requests] == [
        f"{BASE}/categories/tree/12/010101",
        f"{BASE}/products/category/010101/12",
    ]
    assert category.id == "010101"
    assert category.name == "Olis"
    assert category.leaf_count == 46
    assert [child.id for child in category.children] == ["01010101", "01010102"]
    assert [product.id for product in category.products] == [
        "000105",
        "000106",
        "000130",
    ]


def test_get_category_honours_a_page_size() -> None:
    requests: list[httpx.Request] = []
    routes = {
        "/api/categories/tree/12/010101": read_fixture(STORE, "tree_010101.json"),
        "/api/products/category/010101/12": read_fixture(STORE, "listing.json"),
    }

    with fixture_client(requests, routes) as client:
        category = client.get_category("010101", page_size=1)

    assert len(category.products) == 1


def test_an_empty_category_node_raises_not_found() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/categories/tree/12/9999": {"url_base_img": "/x/", "category": None}}

    with (
        fixture_client(requests, routes) as client,
        pytest.raises(NotFoundError, match="9999"),
    ):
        client.get_category("9999")


def test_get_category_products_pages_one_listing_client_side() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/products/category/010101/12": read_fixture(STORE, "listing.json")}

    with fixture_client(requests, routes) as client:
        page = client.get_category_products("010101", page_size=2)
        tail = client.get_category_products("010101", page_size=2, cursor="2")

    assert str(requests[0].url) == f"{BASE}/products/category/010101/12"
    assert page.query == ""
    assert page.row_count == 3
    assert page.next_cursor == "2"
    assert page.truncated is False
    assert len(tail.products) == 1
    assert tail.next_cursor is None


def test_a_category_listing_past_the_search_cap_is_not_truncated() -> None:
    requests: list[httpx.Request] = []
    rows = read_fixture(STORE, "search_capped.json")
    routes = {"/api/products/category/0901/12": [*rows, rows[0]]}

    with fixture_client(requests, routes) as client:
        page = client.get_category_products("0901", page_size=100)
        tail = client.get_category_products("0901", cursor=page.next_cursor)

    # a listing is the whole category; only search stops at a hundred rows.
    assert page.row_count == 101
    assert page.total_hits == 101
    assert page.truncated is False
    assert page.next_cursor == "100"
    assert tail.truncated is False
    assert len(tail.products) == 1


def test_a_non_array_listing_response_is_rejected() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/products/category/010101/12": {"products": []}}

    with (
        fixture_client(requests, routes) as client,
        pytest.raises(InvalidResponseError, match="listing"),
    ):
        client.get_category_products("010101")


def test_the_catalog_is_one_listing_of_the_root_node_with_no_token() -> None:
    requests: list[httpx.Request] = []
    rows = read_fixture(STORE, "listing.json")
    # the same product placed under two categories comes back twice upstream.
    routes = {"/api/products/category/Root/12": [*rows, rows[0]]}

    with fixture_client(requests, routes) as client:
        walked = list(client.iter_catalog())
        catalog = client.get_catalog()

    assert [str(request.url) for request in requests] == [
        f"{BASE}/products/category/Root/12"
    ] * 2
    assert all("Authorization" not in request.headers for request in requests)
    assert [product.id for product in walked] == [
        "000105",
        "000106",
        "000130",
        "000105",
    ]
    assert [product.id for product in catalog] == ["000105", "000106", "000130"]


# ----------------------------------------------------------- listings by theme


def test_new_arrivals_come_from_the_novetats_category_and_are_marked_new() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/products/category/40/12": read_fixture(STORE, "listing.json")}

    with fixture_client(requests, routes) as client:
        arrivals = client.get_new_arrivals()

    assert str(requests[0].url) == f"{BASE}/products/category/40/12"
    assert arrivals
    assert all(product.is_new for product in arrivals)


def test_offers_come_from_the_web_offers_category() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/products/category/Oferta2/12": read_fixture(STORE, "offers.json")}

    with fixture_client(requests, routes) as client:
        offers = client.get_offers()

    assert str(requests[0].url) == f"{BASE}/products/category/Oferta2/12"
    assert [product.id for product in offers] == ["007404", "002500", "003230"]
    assert all(product.promotions for product in offers)


def test_highlighted_offers_come_from_the_promo_carousel_category() -> None:
    requests: list[httpx.Request] = []
    routes = {
        "/api/products/category/PromoHighlight/12": read_fixture(STORE, "offers.json")
    }

    with fixture_client(requests, routes) as client:
        client.get_offers(highlighted=True)

    assert str(requests[0].url) == f"{BASE}/products/category/PromoHighlight/12"


# ------------------------------------------------------------ stores and zones


def test_from_postal_code_binds_the_center_in_one_request() -> None:
    requests: list[httpx.Request] = []
    handler = routed(requests, {"/api/utils/25001/centre": "12"})

    with Plusfresc.from_postal_code(
        "25001", transport=httpx.MockTransport(handler), min_request_interval=0
    ) as client:
        assert client.center == 12

    assert [str(request.url) for request in requests] == [f"{BASE}/utils/25001/centre"]


def test_a_postcode_outside_the_delivery_area_raises_out_of_coverage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # a 409 carrying "0" is a normal answer here, not a transport failure.
        return json_response(request, "0", status=409)

    with pytest.raises(OutOfCoverageError, match="28001"):
        Plusfresc.from_postal_code(
            "28001", transport=httpx.MockTransport(handler), min_request_interval=0
        )


def test_a_zero_center_with_a_200_also_raises_out_of_coverage() -> None:
    requests: list[httpx.Request] = []
    handler = routed(requests, {"/api/utils/28001/centre": "0"})

    with pytest.raises(OutOfCoverageError):
        Plusfresc.from_postal_code(
            "28001", transport=httpx.MockTransport(handler), min_request_interval=0
        )


def test_other_transport_failures_are_not_mistaken_for_coverage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, request=request, json={})

    with pytest.raises(TransportError) as raised:
        Plusfresc.from_postal_code(
            "25001", transport=httpx.MockTransport(handler), min_request_interval=0
        )
    assert not isinstance(raised.value, OutOfCoverageError)


@pytest.mark.parametrize("postal_code", ["", "2500", "abcde", "99001", 25001])
def test_from_postal_code_validates_the_postcode_before_any_request(
    postal_code: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a rejected postcode performed I/O")

    with pytest.raises(ConfigurationError, match="postal_code"):
        Plusfresc.from_postal_code(postal_code, transport=httpx.MockTransport(handler))


def test_list_stores_returns_every_center_and_locker() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/utils/centres": read_fixture(STORE, "pickup_points.json")}

    with fixture_client(requests, routes) as client:
        stores = client.list_stores()

    assert str(requests[0].url) == f"{BASE}/utils/centres"
    assert len(stores) == 9
    locker = stores[-1]
    assert locker.is_locker is True
    assert locker.pickup_code == "127899871"
    # the locker's own id is not a usable center; its parent store's is.
    assert locker.id == "12"
    assert locker.postal_code == "25007"
    assert locker.city == "LLEIDA"
    assert locker.province == "Lleida"


def test_list_stores_filters_by_the_center_a_postcode_resolves_to() -> None:
    requests: list[httpx.Request] = []
    routes = {
        "/api/utils/25001/centre": "12",
        "/api/utils/centres": read_fixture(STORE, "pickup_points.json"),
    }

    with fixture_client(requests, routes) as client:
        stores = client.list_stores("25001")

    assert [str(request.url) for request in requests] == [
        f"{BASE}/utils/25001/centre",
        f"{BASE}/utils/centres",
    ]
    assert {store.id for store in stores} == {"12"}
    assert [store.name for store in stores] == ["CIUTAT ELISIS", "LOCKER GARDENY"]


def test_centers_lists_the_preparation_centers() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/zones/preparationcenters": read_fixture(STORE, "centers.json")}

    with fixture_client(requests, routes) as client:
        centers = client.centers()

    assert str(requests[0].url) == f"{BASE}/zones/preparationcenters"
    assert [center.id for center in centers] == [
        "3",
        "12",
        "17",
        "33",
        "36",
        "58",
        "61",
        "110",
    ]
    assert centers[4].name == "Tàrrega"
    assert centers[4].kind == "center"
    assert centers[4].address == "Avinguda Catalunya, 67-69"


def test_a_non_array_center_response_is_rejected() -> None:
    requests: list[httpx.Request] = []

    with (
        fixture_client(requests, {"/api/zones/preparationcenters": {}}) as client,
        pytest.raises(InvalidResponseError, match="preparation centers"),
    ):
        client.centers()


# ------------------------------------------------------------------ extensions


def test_get_catalog_version_reads_the_day_month_year_date() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/categories/newest": read_fixture(STORE, "catalog_version.json")}

    with fixture_client(requests, routes) as client:
        version = client.get_catalog_version()

    assert str(requests[0].url) == f"{BASE}/categories/newest"
    assert version.isoformat() == "2026-08-10"


def test_an_unreadable_catalog_version_is_rejected() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/categories/newest": {"unchanged_since": "2026-08-10"}}

    with (
        fixture_client(requests, routes) as client,
        pytest.raises(InvalidResponseError, match="catalog version"),
    ):
        client.get_catalog_version()


def test_get_units_labels_the_unit_measure_codes() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/utils/units/ca": read_fixture(STORE, "units.json")}

    with fixture_client(requests, routes) as client:
        units = client.get_units()

    assert str(requests[0].url) == f"{BASE}/utils/units/ca"
    assert {unit.code: unit.name for unit in units}["Kg"] == "Quilo"


def test_get_units_follows_the_selected_language() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/utils/units/es": read_fixture(STORE, "units.json")}

    with fixture_client(requests, routes, language="es") as client:
        client.get_units()

    assert str(requests[0].url) == f"{BASE}/utils/units/es"


def test_a_non_array_unit_response_is_rejected() -> None:
    requests: list[httpx.Request] = []

    with (
        fixture_client(requests, {"/api/utils/units/ca": {}}) as client,
        pytest.raises(InvalidResponseError, match="units"),
    ):
        client.get_units()


def test_download_photo_streams_the_requested_rendition(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, content=b"jpeg-bytes")

    photo = PlusfrescPhoto(url=f"{IMAGES}/000105_Oli_gran.jpg", kind="image")
    with make_client(handler) as client:
        small = client.download_photo(photo, tmp_path / "small.jpg", size="small")
        large = client.download_photo(photo, tmp_path / "large.jpg")

    assert str(requests[0].url) == f"{IMAGES}/000105_Oli.jpg"
    assert str(requests[1].url) == f"{IMAGES}/000105_Oli_gran.jpg"
    assert small.read_bytes() == b"jpeg-bytes"
    assert large.read_bytes() == b"jpeg-bytes"


def test_download_photo_rejects_a_plain_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a rejected photo performed I/O")

    with (
        make_client(handler) as client,
        pytest.raises(ConfigurationError, match="Photo"),
    ):
        client.download_photo("https://example.invalid/x.jpg", "x.jpg")  # type: ignore[arg-type]


def test_the_guest_token_never_leaves_the_api_host(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    search = routed(requests, search_routes())

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(BASE):
            return search(request)
        requests.append(request)
        return httpx.Response(200, request=request, content=b"jpeg-bytes")

    photo = PlusfrescPhoto(url=f"{IMAGES}/000105_Oli_gran.jpg", kind="image")
    with make_client(handler) as client:
        client.search_products("llet")
        client.download(photo, tmp_path / "storefront.jpg")
        client.download("https://bucket.s3.amazonaws.com/x.jpg", tmp_path / "s3.jpg")
        client.download("http://wscompra.plusfresc.cat/api/x.jpg", tmp_path / "x")

    mint, api, storefront, bucket, plaintext = requests
    assert "Authorization" not in mint.headers
    assert api.headers["Authorization"] == f"Bearer {TOKEN}"
    # the image host and a caller's own url are third parties to the token,
    # and an s3-style host refuses a request carrying a foreign bearer.
    assert storefront.url.host == "compra.plusfresc.cat"
    assert "Authorization" not in storefront.headers
    assert "Authorization" not in bucket.headers
    assert "Authorization" not in plaintext.headers


def test_a_401_off_the_api_host_is_not_taken_for_an_expired_token(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    search = routed(requests, search_routes())

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(BASE):
            return search(request)
        requests.append(request)
        return httpx.Response(401, request=request)

    with make_client(handler) as client:
        client.search_products("llet")
        with pytest.raises(TransportError) as raised:
            client.download(f"{IMAGES}/x.jpg", tmp_path / "x.jpg")

    assert not isinstance(raised.value, AuthenticationError)
    assert raised.value.status_code == 401
    assert [request.method for request in requests] == ["POST", "GET", "GET"]


# --------------------------------------------------------------------- prices


def test_prices_are_exact_decimals_carved_from_integer_cents() -> None:
    requests: list[httpx.Request] = []
    routes = {"/api/products/category/010101/12": read_fixture(STORE, "listing.json")}

    with fixture_client(requests, routes) as client:
        products = client.get_category_products("010101").products

    discounted = next(product for product in products if product.price.is_discounted)
    assert discounted.price.amount == Decimal("4.49")
    assert discounted.price.previous == Decimal("5.99")
    assert discounted.price.unit_price == Decimal("4.48")
    assert discounted.price.unit_price_unit == "L"
    assert discounted.price.reference == UnitPrice(
        amount=Decimal("4.48"), unit=Unit.LITRE
    )
    assert discounted.price.discount_percentage == Decimal("25.04")
    assert discounted.price.currency == "EUR"


def test_language_changes_the_resolved_text_but_not_a_listing_request() -> None:
    catalan: list[httpx.Request] = []
    spanish: list[httpx.Request] = []
    listing = read_fixture(STORE, "listing.json")

    with fixture_client(catalan, {"/api/products/category/010101/12": listing}) as ca:
        first = ca.get_category_products("010101").products[0]
    with fixture_client(
        spanish, {"/api/products/category/010101/12": listing}, language="es"
    ) as es:
        second = es.get_category_products("010101").products[0]

    assert str(catalan[0].url) == str(spanish[0].url)
    assert first.name == "Oli d'oliva verge extra GERMANOR, 750 ml"
    assert second.name == "Aceite de oliva virgen extra GERMANOR, 750 ml"
    assert first.name_ca == second.name_ca
