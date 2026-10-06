from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

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
    OutOfCoverageError,
    RetryPolicy,
    TransportError,
    UnsupportedOperationError,
)
from supermercapy.condis import Condis, CondisCategory, CondisProduct
from tests.conftest import read_fixture
from tests.harness import _condis_handler

STORE = "condis"
SITE = "https://compraonline.condis.es"
INDEX = "https://api.empathy.co/search/v1/query/condis"
INDEX_HOST = "api.empathy.co"
ACTION = "60ceecdfa8b37d1b66f9a3b97d656b6d8e97946615"
SCRIPTS = [
    "/_next/static/chunks/21pi4x_j2ze58.js",
    "/_next/static/chunks/2vh-f7p630kha.js",
    "/_next/static/chunks/3i33gyr3gc5bp.js",
]
SESSION = "__Secure-authjs.session-token"
SIGN_IN = [
    "/api/proxy/signin",
    "/api/anonymous/oauth2/authorize",
    "/api/auth/callback/anonymous",
]
Handler = Any


def client(
    requests: list[httpx.Request] | None = None,
    handler: Handler = _condis_handler,
    *arguments: Any,
    **options: Any,
) -> Condis:
    def record(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return handler(request)  # type: ignore[no-any-return]

    options.setdefault("min_request_interval", 0.0)
    return Condis(*arguments, transport=httpx.MockTransport(record), **options)


def signing_in(inner: Handler = _condis_handler) -> Handler:
    """put the canned storefront behind the anonymous sign-in, as the live one is.

    a page request without the session cookie is redirected to the sign-in,
    whose three hops set the cookie and redirect back to the page.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if request.url.host == INDEX_HOST or path.startswith("/_next/static/"):
            return inner(request)  # type: ignore[no-any-return]
        if path == SIGN_IN[0]:
            back = quote(params["callbackUrl"], safe="")
            location = f"{SITE}{SIGN_IN[1]}?client_id=anonymous&back={back}"
            return httpx.Response(307, request=request, headers={"Location": location})
        if path == SIGN_IN[1]:
            back = quote(params["back"], safe="")
            location = f"{SIGN_IN[2]}?code=c&back={back}"
            return httpx.Response(307, request=request, headers={"Location": location})
        if path == SIGN_IN[2]:
            return httpx.Response(
                302,
                request=request,
                headers={
                    "Location": params["back"],
                    "Set-Cookie": f"{SESSION}=canned; Path=/; Secure; HttpOnly",
                },
            )
        if SESSION not in request.headers.get("Cookie", ""):
            callback = quote(str(request.url), safe="")
            location = f"{SIGN_IN[0]}?provider=anonymous&callbackUrl={callback}"
            return httpx.Response(307, request=request, headers={"Location": location})
        return inner(request)  # type: ignore[no-any-return]

    return handler


def listing(name: str, *, total: int | None = None, rows: int | None = None) -> Any:
    """return a canned index page holding every row it reports, or ``total``."""

    page = read_fixture(STORE, name)
    catalog = page["catalog"]
    if rows is not None:
        catalog["content"] = catalog["content"][:rows]
    catalog["numFound"] = len(catalog["content"]) if total is None else total
    return page


def complete(request: httpx.Request) -> httpx.Response:
    """serve the harness, with every facet listing whole in one page."""

    facets = {
        "is_novelty": "novelties.json",
        "on_sale": "on_sale.json",
        "on_promotion": "on_promotion.json",
    }
    field = request.url.params.get("browseField")
    if request.url.host == INDEX_HOST and field in facets:
        return httpx.Response(200, request=request, json=listing(facets[field]))
    return _condis_handler(request)


def ids(products: Any) -> list[str]:
    return [product.id for product in products]


def urls(requests: list[httpx.Request]) -> list[str]:
    return [str(request.url) for request in requests]


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_binds_the_default_centre() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Condis(transport=httpx.MockTransport(handler)) as condis:
        assert condis.picking_centre == "718"
        assert condis.store_id == "718"
        assert condis.language is Language.SPANISH
        assert condis.min_request_interval == 0.5
        assert condis.user_agent.startswith("supermercapy/")


def test_a_centre_is_taken_as_a_string_or_an_integer() -> None:
    with client(None, _condis_handler, 531) as condis:
        assert condis.picking_centre == "531"
    with client(None, _condis_handler, " 533 ") as condis:
        assert condis.store_id == "533"


@pytest.mark.parametrize("centre", ["", " ", "x718", "\u0667\u0661\u0668", True, 7.18])
def test_an_unusable_centre_is_refused(centre: Any) -> None:
    with pytest.raises(ConfigurationError, match="picking_centre"):
        Condis(centre)


@pytest.mark.parametrize("language", ["ca", "en", "vl"])
def test_only_spanish_is_served(language: str) -> None:
    with pytest.raises(ConfigurationError, match="language"):
        Condis(language=language)


def test_the_declared_capabilities() -> None:
    for capability in (
        Capability.POSTAL_CODE,
        Capability.CATALOG,
        Capability.NEW_ARRIVALS,
        Capability.OFFERS,
        Capability.NUTRITION,
        Capability.PROMOTIONS,
    ):
        assert Condis.supports(capability)
    for capability in (
        Capability.STORES,
        Capability.EAN,
        Capability.EAN_LOOKUP,
        Capability.HOME,
        Capability.FUTURE_PRICES,
    ):
        assert not Condis.supports(capability)


def test_stores_and_the_home_page_are_not_offered_and_cost_no_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        with pytest.raises(UnsupportedOperationError):
            condis.list_stores()
        with pytest.raises(UnsupportedOperationError):
            condis.get_home()
        with pytest.raises(UnsupportedOperationError):
            condis.get_product_by_ean("8480000000000")

    assert requests == []


# ---------------------------------------------------------------------- search


def test_search_sends_the_documented_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        page = condis.search_products(" leche ")

    assert urls(requests) == [
        f"{INDEX}/search?lang=es&start=0&rows=24&query=leche&store=718"
    ]
    assert requests[0].headers["Accept"] == "application/json"
    assert "Cookie" not in requests[0].headers
    assert page.query == " leche "
    assert page.page_size == 24
    assert page.total_hits == 416
    assert page.offset == 0
    assert page.next_cursor == "3"
    assert ids(page.products) == ["704049", "704036", "704309"]
    assert all(isinstance(product, CondisProduct) for product in page.products)


def test_search_continues_from_the_offset_and_takes_filters() -> None:
    requests: list[httpx.Request] = []

    with client(requests, _condis_handler, 531) as condis:
        page = condis.search_products(
            "leche",
            page_size=5,
            cursor="414",
            filters=["on_sale:true", "filterCategory:c07"],
        )

    assert urls(requests) == [
        f"{INDEX}/search?lang=es&start=414&rows=5&query=leche&store=531"
        "&filter=on_sale%3Atrue&filter=filterCategory%3Ac07"
    ]
    assert page.offset == 414
    assert page.next_cursor is None
    assert ids(page.products) == ["216696", "210932"]


def test_iter_search_walks_every_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        products = list(condis.iter_search("leche"))

    assert [request.url.params["start"] for request in requests] == ["0", "3"]
    assert len(products) == 5


def test_a_page_at_the_offset_ceiling_is_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, request=request, json=listing("search.json", total=5000)
        )

    with client(None, handler) as condis:
        page = condis.search_products("leche", page_size=3, cursor="2494")

    assert page.next_cursor is None
    assert page.truncated is True


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"query": ""}, "query"),
        ({"query": "  "}, "query"),
        ({"query": 3}, "query"),
        ({"query": "leche", "page_size": 501}, "page_size"),
        ({"query": "leche", "cursor": ""}, "cursor"),
        ({"query": "leche", "cursor": "-1"}, "cursor"),
        ({"query": "leche", "cursor": "\u0661"}, "cursor"),
        ({"query": "leche", "cursor": 24}, "cursor"),
        ({"query": "leche", "cursor": "2495"}, "at most 2494"),
        ({"query": "leche", "filters": "on_sale:true"}, "filters"),
        ({"query": "leche", "filters": 3}, "filters"),
        ({"query": "leche", "filters": ["on_sale"]}, "filters"),
        ({"query": "leche", "filters": [1]}, "filters"),
    ],
)
def test_unusable_search_arguments_are_refused(
    arguments: dict[str, Any], message: str
) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis, pytest.raises(ConfigurationError, match=message):
        condis.search_products(**arguments)

    assert requests == []


def test_a_centre_the_index_does_not_know_answers_an_empty_page() -> None:
    with client(None, _condis_handler, 999) as condis:
        page = condis.search_products("leche")

    assert page.products == ()
    assert page.total_hits == 0
    assert page.next_cursor is None


# --------------------------------------------------------------------- product


def test_get_product_reads_the_page_under_a_placeholder_slug() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        product = condis.get_product(704049)

    assert urls(requests) == [f"{SITE}/p/p/704049/es_ES"]
    assert requests[0].headers["Accept"] == "text/html,application/xhtml+xml"
    assert isinstance(product, CondisProduct)
    assert product.id == "704049"
    assert product.url == f"{SITE}/leche-condis-entera-1-l/p/704049/es_ES"
    assert product.category_ids == ("c07__cat00210003__cat002100030003",)
    assert product.nutrition is not None


def test_a_product_the_shop_does_not_know_is_not_found() -> None:
    requests: list[httpx.Request] = []

    with (
        client(requests) as condis,
        pytest.raises(NotFoundError, match="999999999"),
    ):
        condis.get_product("999999999")

    assert len(requests) == 1


@pytest.mark.parametrize("product_id", ["70-4049", "704049/x", "\uff17\uff10\uff14"])
def test_a_product_id_that_is_no_product_code_is_refused(product_id: str) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis, pytest.raises(ConfigurationError, match="code"):
        condis.get_product(product_id)

    assert requests == []


def test_a_page_without_its_data_is_an_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, html="<html><body></body></html>")

    with (
        client(None, handler) as condis,
        pytest.raises(InvalidResponseError, match="without its data"),
    ):
        condis.get_product("704049")


# --------------------------------------------------------------------- sign-in


def test_the_first_page_follows_the_anonymous_sign_in_once() -> None:
    requests: list[httpx.Request] = []

    with client(requests, signing_in()) as condis:
        product = condis.get_product("704049")
        tree = condis.get_categories()

    assert [request.url.path for request in requests] == [
        "/p/p/704049/es_ES",
        *SIGN_IN,
        "/p/p/704049/es_ES",
        "/",
    ]
    assert requests[1].url.params["callbackUrl"] == f"{SITE}/p/p/704049/es_ES"
    assert all(
        request.headers["Accept"] == "text/html,application/xhtml+xml"
        for request in requests
    )
    assert SESSION not in requests[0].headers.get("Cookie", "")
    assert f"{SESSION}=canned" in requests[4].headers["Cookie"]
    assert f"{SESSION}=canned" in requests[5].headers["Cookie"]
    assert product.id == "704049"
    assert tree


def test_the_index_needs_no_session() -> None:
    requests: list[httpx.Request] = []

    with client(requests, signing_in()) as condis:
        condis.search_products("leche")
        condis.suggest("lech")

    assert [request.url.host for request in requests] == [INDEX_HOST, INDEX_HOST]


def test_a_redirect_to_nowhere_is_an_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, request=request)

    with (
        client(None, handler) as condis,
        pytest.raises(InvalidResponseError, match="nowhere"),
    ):
        condis.get_categories()


def test_a_sign_in_that_never_lands_stops_after_six_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, request=request, headers={"Location": "/"})

    with (
        client(requests, handler) as condis,
        pytest.raises(InvalidResponseError, match="more than 6 times"),
    ):
        condis.get_categories()

    assert len(requests) == 6


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_navigation_of_the_home_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        tree = condis.get_categories()
        condis.get_categories()

    # the tree is read again on every call, one request each
    assert urls(requests) == [f"{SITE}/", f"{SITE}/"]
    assert [root.id for root in tree] == ["c07", "c03"]
    assert all(isinstance(root, CondisCategory) for root in tree)
    assert tree[0].children[0].children[0].level == 2


def test_a_home_page_without_the_tree_is_an_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        html = '<script>self.__next_f.push([1,"0:{\\"categoryList\\":[]}"])</script>'
        return httpx.Response(200, request=request, html=html)

    with (
        client(None, handler) as condis,
        pytest.raises(InvalidResponseError, match="no category tree"),
    ):
        condis.get_categories()


def test_get_category_finds_the_node_and_reads_its_first_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        category = condis.get_category(
            "c07__cat00280001__cat002800010002", page_size=10
        )

    assert urls(requests) == [
        f"{SITE}/",
        f"{INDEX}/browse?lang=es&start=0&rows=10&browseField=filterCategory"
        "&browseValue=c07__cat00280001__cat002800010002&store=718",
    ]
    assert category.name == "Agua con gas"
    assert category.level == 2
    assert category.product_count == 17
    assert ids(category.products) == ["704520", "704036"]


def test_an_unknown_category_is_not_found_after_the_tree() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis, pytest.raises(NotFoundError, match="c99"):
        condis.get_category("c99")

    assert urls(requests) == [f"{SITE}/"]


def test_category_products_page_any_node_without_the_tree() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        page = condis.get_category_products("c03", page_size=2, cursor="2")
        unknown = condis.get_category_products("c99")

    assert urls(requests) == [
        f"{INDEX}/browse?lang=es&start=2&rows=2&browseField=filterCategory"
        "&browseValue=c03&store=718",
        f"{INDEX}/browse?lang=es&start=0&rows=24&browseField=filterCategory"
        "&browseValue=c99&store=718",
    ]
    assert page.query == ""
    assert page.offset == 2
    assert page.products
    assert unknown.products == ()


# ---------------------------------------------------------------- the catalog


def test_the_catalog_walks_every_top_level_category() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("browseField") == "filterCategory":
            return httpx.Response(200, request=request, json=listing("browse.json"))
        return _condis_handler(request)

    with client(requests, handler) as condis:
        rows = list(condis.iter_catalog())
        catalog = condis.get_catalog()

    browsed = [
        (request.url.params["browseValue"], request.url.params["rows"])
        for request in requests
        if request.url.host == INDEX_HOST
    ]
    assert browsed == [("c07", "500"), ("c03", "500")] * 2
    assert len(rows) == 4
    assert ids(catalog) == ["704520", "704036"]


def test_the_catalog_pages_a_category_five_hundred_rows_at_a_time() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        rows = list(condis.iter_catalog())

    starts = [
        (request.url.params["browseValue"], request.url.params["start"])
        for request in requests
        if request.url.host == INDEX_HOST
    ]
    # the canned category reports more rows than its first page holds
    assert starts == [("c07", "0"), ("c07", "2"), ("c03", "0"), ("c03", "2")]
    assert len(rows) == 8


def test_a_category_too_large_to_walk_is_walked_through_its_children() -> None:
    requests: list[httpx.Request] = []
    overflowing = {"c07", "c07__cat00280001", "c07__cat00280001__cat002800010002"}

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("browseField") != "filterCategory":
            return _condis_handler(request)
        total = 5000 if params["browseValue"] in overflowing else None
        page = listing("browse.json", total=total)
        if params["start"] != "0":
            page["catalog"]["content"] = []
        return httpx.Response(200, request=request, json=page)

    with client(requests, handler) as condis:
        rows = list(condis.iter_catalog())

    browsed = [
        (request.url.params["browseValue"], request.url.params["start"])
        for request in requests
        if request.url.host == INDEX_HOST
    ]
    assert browsed == [
        ("c07", "0"),
        ("c07__cat00280001", "0"),
        # a leaf has no children to split into, so it is walked as far as it goes
        ("c07__cat00280001__cat002800010002", "0"),
        ("c07__cat00280001__cat002800010002", "2"),
        ("c07__cat00210002", "0"),
        ("c03", "0"),
    ]
    assert len(rows) == 6


# -------------------------------------------------------------- offers, extras


def test_new_arrivals_browse_the_novelty_facet() -> None:
    requests: list[httpx.Request] = []

    with client(requests, complete) as condis:
        products = condis.get_new_arrivals()

    assert urls(requests) == [
        f"{INDEX}/browse?lang=es&start=0&rows=500&browseField=is_novelty"
        "&browseValue=true&store=718"
    ]
    assert ids(products) == ["109472", "810125"]
    assert all(product.is_new for product in products)


def test_offers_read_both_facets_and_keep_each_product_once() -> None:
    requests: list[httpx.Request] = []

    with client(requests, complete) as condis:
        offers = condis.get_offers()

    assert [request.url.params["browseField"] for request in requests] == [
        "on_sale",
        "on_promotion",
    ]
    assert all(request.url.params["rows"] == "500" for request in requests)
    assert ids(offers) == ["109472", "204265", "431090"]
    assert offers[0].price.previous is not None
    assert offers[2].promotions


def test_offers_follow_the_offset_past_the_first_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        condis.get_offers()

    assert [
        (request.url.params["browseField"], request.url.params["start"])
        for request in requests
    ] == [
        ("on_sale", "0"),
        ("on_sale", "2"),
        ("on_promotion", "0"),
        ("on_promotion", "2"),
    ]


def test_suggest_sends_the_term_and_the_centre() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as condis:
        suggestions = condis.suggest(" lech ")

    assert urls(requests) == [f"{INDEX}/empathize?query=lech&lang=es&store=718"]
    assert suggestions[:2] == ("leches", "leche semidesnatada")


def test_a_blank_suggestion_term_is_refused() -> None:
    with client() as condis, pytest.raises(ConfigurationError, match="term"):
        condis.suggest(" ")


# ------------------------------------------------------------------- postcodes


def test_a_postcode_is_resolved_through_the_storefront_action() -> None:
    requests: list[httpx.Request] = []

    with Condis.from_postal_code(
        "08034",
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or _condis_handler(request)
        ),
        min_request_interval=0.0,
    ) as condis:
        assert condis.picking_centre == "531"
        condis.search_products("leche")

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/"),
        *(("GET", script) for script in SCRIPTS),
        ("POST", "/"),
        ("GET", "/search/v1/query/condis/search"),
    ]
    assert all(request.headers["Accept"] == "*/*" for request in requests[1:4])
    action = requests[4]
    assert action.headers["Next-Action"] == ACTION
    assert action.headers["Accept"] == "text/x-component"
    assert action.headers["Content-Type"] == "text/plain;charset=UTF-8"
    assert json.loads(action.content) == ["08034"]
    assert requests[5].url.params["store"] == "531"


def test_a_postcode_on_a_fresh_session_signs_in_first() -> None:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return signing_in()(request)  # type: ignore[no-any-return]

    with Condis.from_postal_code(
        "08870", transport=httpx.MockTransport(record), min_request_interval=0.0
    ) as condis:
        assert condis.picking_centre == "531"

    assert [request.url.path for request in requests] == [
        "/",
        *SIGN_IN,
        "/",
        *SCRIPTS,
        "/",
    ]
    assert f"{SESSION}=canned" in requests[-1].headers["Cookie"]


def test_a_postcode_condis_does_not_serve_is_out_of_coverage() -> None:
    closed: list[bool] = []

    class Transport(httpx.MockTransport):
        def close(self) -> None:
            closed.append(True)

    with pytest.raises(OutOfCoverageError, match="28001"):
        Condis.from_postal_code("28001", transport=Transport(_condis_handler))

    assert closed == [True]


def test_a_malformed_postcode_is_refused_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a malformed postcode performed I/O")

    with pytest.raises(ConfigurationError, match="postal_code"):
        Condis.from_postal_code("8034", transport=httpx.MockTransport(handler))


def lookup(
    *, script: str | None = None, home: str | None = None, answer: Any = None
) -> Handler:
    """serve the harness with the action script, the home page or the answer swapped."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if script is not None and path.startswith("/_next/static/"):
            return httpx.Response(200, request=request, text=script)
        if home is not None and request.method == "GET" and path == "/":
            return httpx.Response(200, request=request, html=home)
        if answer is not None and request.method == "POST":
            if isinstance(answer, httpx.Response):
                return httpx.Response(
                    answer.status_code, request=request, headers=answer.headers
                )
            return httpx.Response(200, request=request, text=answer)
        return _condis_handler(request)

    return handler


@pytest.mark.parametrize(
    "handler",
    [
        lookup(script="no actions here"),
        lookup(home="<html><head></head><body></body></html>"),
    ],
)
def test_a_build_without_the_action_is_an_invalid_response(handler: Handler) -> None:
    with pytest.raises(InvalidResponseError, match="fetchPostalCodeById"):
        Condis.from_postal_code(
            "08034", transport=httpx.MockTransport(handler), min_request_interval=0.0
        )


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (httpx.Response(307, headers={"Location": "/api/proxy/signin"}), "redirected"),
        ('0:{"a":"$@1"}\n', "without a result"),
        ("0:{}\n1:{not json\n", "without a result"),
        ('1:{"postalCode":{"picking_centre_id":"5x"}}\n', "unusable centre"),
    ],
)
def test_an_unusable_lookup_answer_is_an_invalid_response(
    answer: Any, message: str
) -> None:
    with pytest.raises(InvalidResponseError, match=message):
        Condis.from_postal_code(
            "08034",
            transport=httpx.MockTransport(lookup(answer=answer)),
            min_request_interval=0.0,
        )


@pytest.mark.parametrize(
    "answer",
    ['1:{"postalCode":{},"error":null}\n', '1:{"postalCode":null}\n', "1:[]\n"],
)
def test_an_answer_without_a_centre_is_out_of_coverage(answer: str) -> None:
    with pytest.raises(OutOfCoverageError):
        Condis.from_postal_code(
            "08034",
            transport=httpx.MockTransport(lookup(answer=answer)),
            min_request_interval=0.0,
        )


# ------------------------------------------------------------------ the edges


def test_a_cloudflare_challenge_costs_one_request_and_is_never_retried() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            403, request=request, headers={"cf-mitigated": "challenge"}, text="..."
        )

    with (
        Condis(
            transport=httpx.MockTransport(handler),
            min_request_interval=0.0,
            retry_policy=RetryPolicy(max_attempts=5, backoff_factor=0.0),
        ) as condis,
        pytest.raises(ChallengedError) as raised,
    ):
        condis.search_products("leche")

    assert len(requests) == 1
    assert raised.value.status_code == 403


@pytest.mark.parametrize("host", [SITE, INDEX])
def test_a_403_from_either_edge_is_a_block(host: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403, request=request, text="request blocked")

    with (
        Condis(
            transport=httpx.MockTransport(handler), min_request_interval=0.0
        ) as condis,
        pytest.raises(BlockedError),
    ):
        if host == SITE:
            condis.get_categories()
        else:
            condis.suggest("lech")

    assert len(requests) == 1


def test_an_index_error_is_a_transport_error_with_its_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            json={"code": 400, "error": "Pagination parameter 'start' ..."},
        )

    with client(None, handler) as condis, pytest.raises(TransportError) as raised:
        condis.search_products("leche")

    assert raised.value.status_code == 400
    assert not isinstance(raised.value, (BlockedError, ChallengedError))
