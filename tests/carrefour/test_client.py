from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from supermercapy import (
    BlockedError,
    Capability,
    Carrefour,
    ChallengedError,
    ConfigurationError,
    InvalidResponseError,
    Language,
    NotFoundError,
    OutOfCoverageError,
    RetryPolicy,
    TransportError,
)
from supermercapy.carrefour import CarrefourPhoto, CarrefourProduct
from tests.conftest import read_fixture
from tests.harness import HARNESSES

STORE = "carrefour"
SITE = "https://www.carrefour.es"
STATIC = "https://static.carrefour.es"
INDEX = "https://api.empathy.co/search/v1/query/carrefour"
SALE_POINT = "005290"
# never retry and never sleep between attempts; the tests assert counts
NO_BACKOFF = RetryPolicy(max_attempts=3, backoff_factor=0.0, jitter_ratio=0.0)


def canned(handler: Any = None, **options: Any) -> Carrefour:
    """build a client against the shared canned storefront."""

    client = HARNESSES[STORE].client(handler, **options)
    assert isinstance(client, Carrefour)
    return client


def recording(requests: list[httpx.Request], **options: Any) -> Carrefour:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    return canned(handler, **options)


def replying(response: httpx.Response | object, **options: Any) -> Carrefour:
    """build a client whose every request gets the same canned answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, httpx.Response):
            return httpx.Response(
                response.status_code,
                request=request,
                content=response.content,
                headers=response.headers,
            )
        return httpx.Response(200, request=request, json=response)

    return Carrefour(
        SALE_POINT,
        transport=httpx.MockTransport(handler),
        **options,
    )


def urls(requests: list[httpx.Request]) -> list[str]:
    return [str(request.url) for request in requests]


def freeze(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> list[float]:
    """drive both the core and the carrefour clocks from one fake value."""

    slept: list[float] = []

    def sleep(delay: float) -> None:
        slept.append(delay)
        clock[0] += delay

    for module in ("supermercapy._core.client", "supermercapy.carrefour.client"):
        monkeypatch.setattr(f"{module}.time.monotonic", lambda: clock[0])
        monkeypatch.setattr(f"{module}.time.sleep", sleep)
    return slept


def page(body: str) -> str:
    return f"<html><body><script>window.__INITIAL_STATE__ = {body};</script></body>"


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_binds_a_sale_point() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Carrefour(SALE_POINT, transport=httpx.MockTransport(handler)) as client:
        assert client.sale_point == SALE_POINT
        assert client.store_id == SALE_POINT
        assert client.catalog == "food"
        assert client.language is Language.SPANISH


def test_the_sale_point_is_optional_and_opaque() -> None:
    with Carrefour() as client:
        assert client.sale_point is None
        assert client.store_id is None
    # store codes are alphanumeric and sparse, never integers
    with Carrefour("0000GP") as client:
        assert client.sale_point == "0000GP"
    with Carrefour(5290) as client:
        assert client.sale_point == "5290"
    for value in ("", "   ", True, []):
        with pytest.raises(ConfigurationError, match="sale_point"):
            Carrefour(value)  # type: ignore[arg-type]


def test_only_the_three_indexed_catalogs_are_accepted() -> None:
    for value in ("food", "FOOD", " cellar ", "nonfood"):
        with Carrefour(catalog=value) as client:
            assert client.catalog == value.strip().lower()
    for value in ("drinks", "", 4):
        with pytest.raises(ConfigurationError, match="catalog"):
            Carrefour(catalog=value)  # type: ignore[arg-type]


def test_the_declared_capabilities_are_the_seven_carrefour_can_honour() -> None:
    assert Carrefour.capabilities == frozenset(
        {
            Capability.POSTAL_CODE,
            Capability.STORES,
            Capability.EAN,
            Capability.EAN_LOOKUP,
            Capability.NUTRITION,
            Capability.PROMOTIONS,
            Capability.HOME,
        }
    )
    # the sitemaps are challenged, so the catalog cannot be enumerated
    assert not Carrefour.supports(Capability.CATALOG)
    # carrefour publishes no novelties or offers feed of its own
    assert not Carrefour.supports(Capability.NEW_ARRIVALS)
    assert not Carrefour.supports(Capability.OFFERS)
    assert not Carrefour.supports(Capability.FUTURE_PRICES)


def test_the_three_search_languages_are_separate_indexes() -> None:
    requests: list[httpx.Request] = []
    for language in ("es", "ca", "en"):
        with recording(requests, language=language) as client:
            client.search_products("leche")
        assert requests[-1].url.params["lang"] == language
        assert requests[-1].headers["Accept-Language"].startswith(language)
    for language in ("vl", "fr", ""):
        with pytest.raises(ConfigurationError, match="language"):
            Carrefour(language=language)


# -------------------------------------------------------------------- transport


def test_the_default_user_agent_is_a_browser_string() -> None:
    # the cloudflare gate is a user-agent check: a library string is a 403
    assert Carrefour.default_user_agent.startswith("Mozilla/5.0")
    assert "Chrome/" in Carrefour.default_user_agent
    assert "supermercapy" not in Carrefour.default_user_agent
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.get_product("521007071")
    assert {request.headers["User-Agent"] for request in requests} == {
        Carrefour.default_user_agent
    }


def test_the_first_storefront_request_warms_a_session_once() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.get_product("521007071")
        client.get_stock("521007071")
    warmups = [url for url in urls(requests) if url == f"{SITE}/supermercado"]
    assert len(warmups) == 1
    assert urls(requests)[0] == f"{SITE}/supermercado"
    assert requests[0].headers["Sec-Fetch-Dest"] == "document"


def test_searches_never_pay_for_a_warm_up() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.search_products("leche")
        client.suggest("lech")
    assert urls(requests) == [
        f"{INDEX}/search?query=leche&lang=es&catalog=food&rows=24&start=0"
        f"&store={SALE_POINT}",
        f"{INDEX}/empathize?query=lech&lang=es&rows=5",
    ]


def test_the_warm_session_is_renewed_after_twenty_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    freeze(monkeypatch, clock)
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.get_stock("521007071")
        clock[0] += 1499.0
        client.get_stock("521007071")
        warm_within_ttl = urls(requests).count(f"{SITE}/supermercado")
        clock[0] += 2.0
        client.get_stock("521007071")
    assert warm_within_ttl == 1
    assert urls(requests).count(f"{SITE}/supermercado") == 2


def test_a_waf_block_is_never_retried() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            403, request=request, html=read_fixture(STORE, "blocked.html")
        )

    with (
        canned(handler, retry_policy=NO_BACKOFF) as client,
        pytest.raises(BlockedError) as raised,
    ):
        client.get_home()
    assert raised.value.status_code == 403
    # one request, no warm-up retry: the rule will not clear
    assert len(requests) == 1


def test_a_challenge_warms_a_new_session_and_retries_once() -> None:
    requests: list[httpx.Request] = []
    challenged: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.startswith("/supermercado/p/R-") and len(challenged) < 1:
            challenged.append(path)
            return httpx.Response(
                403, request=request, html=read_fixture(STORE, "challenge.html")
            )
        return HARNESSES[STORE].handler(request)

    with canned(handler, retry_policy=NO_BACKOFF) as client:
        product = client.get_product("521007071")
    assert product.id == "521007071"
    assert urls(requests).count(f"{SITE}/supermercado") == 2


def test_a_second_challenge_raises_rather_than_warming_again() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/supermercado":
            return httpx.Response(
                200, request=request, html=read_fixture(STORE, "warmup.html")
            )
        return httpx.Response(
            403, request=request, html=read_fixture(STORE, "challenge.html")
        )

    with (
        canned(handler, retry_policy=NO_BACKOFF) as client,
        pytest.raises(ChallengedError) as raised,
    ):
        client.get_stock("521007071")
    assert raised.value.suggested_backoff == 1800.0
    assert urls(requests).count(f"{SITE}/supermercado") == 2


def test_an_unrecognised_403_stays_a_plain_transport_error() -> None:
    # only the two known bodies get a bot-protection exception of their own
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, text="nope")

    with (
        canned(handler, retry_policy=NO_BACKOFF) as client,
        pytest.raises(TransportError) as raised,
    ):
        client.get_home()
    assert not isinstance(raised.value, BlockedError)
    assert issubclass(ChallengedError, BlockedError)


# ----------------------------------------------------------------------- search


def test_search_lowercases_the_query_and_sends_the_sale_point() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        result = client.search_products("Leche SIN Lactosa", page_size=48)
    # the storefront's own client lowercases too, which keeps the cdn key.
    # compare parsed params: httpx 0.27 spells a space %20 and 0.28 spells it +
    url = requests[0].url
    assert str(url.copy_with(query=None)) == f"{INDEX}/search"
    assert url.params.multi_items() == [
        ("query", "leche sin lactosa"),
        ("lang", "es"),
        ("catalog", "food"),
        ("rows", "48"),
        ("start", "0"),
        ("store", SALE_POINT),
    ]
    assert result.query == "Leche SIN Lactosa"
    assert result.page_size == 48


def test_an_unbound_client_searches_the_national_superset() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    with Carrefour(transport=httpx.MockTransport(handler)) as client:
        client.search_products("leche")
    assert "store" not in requests[0].url.params


def test_search_paging_walks_the_offset_and_stops_at_the_total() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        first = client.search_products("leche")
        assert first.total_hits == 3
        assert first.offset == 0
        assert first.next_cursor == "2"
        second = client.search_products("leche", cursor=first.next_cursor)
    assert requests[1].url.params["start"] == "2"
    assert second.next_cursor is None
    assert second.offset == 2
    assert not second.truncated


def test_sort_is_passed_through_untouched() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.search_products("leche", sort="best_sellers_food desc")
    assert requests[0].url.params["sort"] == "best_sellers_food desc"
    with canned() as client:
        for value in ("", "   ", 4):
            with pytest.raises(ConfigurationError, match="sort"):
                client.search_products("leche", sort=value)  # type: ignore[arg-type]


def test_a_blank_query_is_refused_before_any_request() -> None:
    # every carrefour endpoint makes query mandatory; there is no match-all
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a blank query reached the network")

    with canned(handler) as client:
        for value in ("", "   ", None, 7):
            with pytest.raises(ConfigurationError, match="query"):
                client.search_products(value)  # type: ignore[arg-type]
            with pytest.raises(ConfigurationError, match="query"):
                client.suggest(value)  # type: ignore[arg-type]


def test_paging_stops_at_the_offset_ceiling_and_says_so() -> None:
    documents = [
        {"product_id": f"id{index}", "display_name": f"row {index}"}
        for index in range(24)
    ]
    payload = {"catalog": {"content": documents, "numFound": 5000}}
    with replying(payload) as client:
        result = client.search_products("leche", cursor="2490")
        assert result.truncated
        assert result.next_cursor is None
        with pytest.raises(ConfigurationError, match="2498"):
            client.search_products("leche", cursor="2499")
        # "²" and "٣" pass str.isdigit; neither is an offset search returned
        for value in ("x", "-1", 12, "²", "٣"):
            with pytest.raises(ConfigurationError, match="cursor"):
                client.search_products("leche", cursor=value)  # type: ignore[arg-type]


def test_page_size_is_capped_at_the_index_limit() -> None:
    with canned() as client:
        assert Carrefour.max_page_size == 500
        with pytest.raises(ConfigurationError, match="page_size"):
            client.search_products("leche", page_size=501)


def test_a_response_without_an_envelope_is_invalid() -> None:
    with (
        replying({"error": "Query parameter is mandatory."}) as client,
        pytest.raises(InvalidResponseError, match="catalog envelope"),
    ):
        client.search_products("leche")


def test_suggestions_come_back_deduplicated() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        assert client.suggest("Lech", limit=3) == ("leche", "leche sin lactosa")
    assert str(requests[0].url) == f"{INDEX}/empathize?query=lech&lang=es&rows=3"
    with canned() as client:
        for value in (0, -1, True, "5"):
            with pytest.raises(ConfigurationError, match="limit"):
                client.suggest("lech", limit=value)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- product


def test_a_product_is_fetched_through_its_canonical_redirect() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        product = client.get_product("521007071")
    assert urls(requests)[1:] == [
        f"{SITE}/supermercado/p/R-521007071/p",
        f"{SITE}/supermercado/leche-semidesnatada-carrefour-brik-1-l/R-521007071/p",
    ]
    assert product.slug == "leche-semidesnatada-carrefour-brik-1-l"
    assert product.nutrition is not None


def test_an_unknown_id_redirects_to_the_home_page() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client, pytest.raises(NotFoundError):
        client.get_product("999999999")
    assert urls(requests)[-1] == f"{SITE}/supermercado/p/R-999999999/p"


@pytest.mark.parametrize(
    "location", ["/", "/moda", "https://www.carrefour.es/supermercado", ""]
)
def test_any_redirect_out_of_the_food_catalog_is_not_found(location: str) -> None:
    # the same unknown id was seen redirecting home and to another business
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/supermercado":
            return httpx.Response(200, request=request, html="<html></html>")
        return httpx.Response(301, request=request, headers={"Location": location})

    with canned(handler) as client, pytest.raises(NotFoundError, match="away"):
        client.get_product("999999999")
    # the redirect is never followed out of the catalog
    assert urls(requests) == [
        f"{SITE}/supermercado",
        f"{SITE}/supermercado/p/R-999999999/p",
    ]


def test_a_redirect_that_drops_the_query_keeps_the_listing_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/supermercado/c/cat20093/c":
            location = "/supermercado/la-despensa/lacteos/leche/cat20093/c"
            return httpx.Response(301, request=request, headers={"Location": location})
        return HARNESSES[STORE].handler(request)

    with canned(handler) as client:
        client.get_category("cat20093", offset=24)
    assert requests[-1].url.path == "/supermercado/la-despensa/lacteos/leche/cat20093/c"
    assert requests[-1].url.params["offset"] == "24"


def test_every_id_spelling_is_kept_as_an_opaque_string() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        for identifier in ("VC4AECOMM-481229", "prod190222", "fprod1280750"):
            with pytest.raises(NotFoundError):
                client.get_product(identifier)
    assert urls(requests)[1:] == [
        f"{SITE}/supermercado/p/R-VC4AECOMM-481229/p",
        f"{SITE}/supermercado/p/R-prod190222/p",
        f"{SITE}/supermercado/p/R-fprod1280750/p",
    ]


def test_a_page_without_rendered_state_is_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, html="<html>nothing</html>")

    with canned(handler) as client, pytest.raises(NotFoundError):
        client.get_product("521007071")


def test_a_redirect_loop_is_reported_rather_than_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/supermercado":
            return httpx.Response(200, request=request, html="<html></html>")
        return httpx.Response(
            301, request=request, headers={"Location": "/supermercado/loop/R-1/p"}
        )

    with canned(handler) as client, pytest.raises(NotFoundError):
        client.get_product("1")


def test_a_barcode_lookup_checks_the_barcode_it_got_back() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        product = client.get_product_by_ean("8431876011937")
    assert product.ean == "8431876011937"
    assert urls(requests)[0] == (
        f"{INDEX}/search?query=8431876011937&lang=es&catalog=food"
        f"&rows=1&start=0&store={SALE_POINT}"
    )
    with canned() as client:
        # a free-text match on an unrelated product must not answer the lookup
        with pytest.raises(NotFoundError):
            client.get_product_by_ean("521007071")
        with pytest.raises(NotFoundError):
            client.get_product_by_ean("0000000000000")


def test_a_barcode_whose_page_disagrees_is_not_found() -> None:
    index = read_fixture(STORE, "lookup_brik.json")
    index["catalog"]["content"][0]["ean13"] = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.empathy.co":
            return httpx.Response(200, request=request, json=index)
        return HARNESSES[STORE].handler(request)

    with canned(handler) as client, pytest.raises(NotFoundError):
        client.get_product_by_ean("8431876234121")


# --------------------------------------------------------------------- listings


MENU = f"{SITE}/cloud-api/categories-api/v1/categories/menu"


def test_the_departments_cost_one_menu_request() -> None:
    # the navigation sitemaps sit behind a challenge since october 2026
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        tree = client.get_categories()
    # the menu answers cold, so there is no warm-up page
    assert len(requests) == 1
    menu = requests[0]
    assert str(menu.url.copy_with(query=None)) == MENU
    assert dict(menu.url.params) == {
        "current_category": "foodRootCategory",
        "depth": "1",
        "lang": "es",
        "sale_point": SALE_POINT,
    }
    assert menu.headers["Sec-Fetch-Mode"] == "cors"
    assert [node.id for node in tree] == ["cat20968591", "cat20002", "cat20001"]
    assert tree[2].name == "La Despensa"
    assert tree[2].url == f"{SITE}/supermercado/la-despensa/cat20001/c"


def test_the_deep_tree_costs_one_more_menu_request_per_department() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        tree = client.get_categories(deep=True)
    asked = [request.url.params["current_category"] for request in requests]
    assert asked == ["foodRootCategory", "cat20968591", "cat20002", "cat20001"]
    assert [child.id for child in tree[2].children] == [
        "cat21078001",
        "cat20009",
        "cat33207691",
    ]
    assert {child.level for child in tree[2].children} == {2}
    # the canned menu knows nothing below the other two departments
    assert tree[0].children == tree[1].children == ()


def test_a_challenged_menu_warms_a_session_and_asks_again() -> None:
    requests: list[httpx.Request] = []
    challenge = read_fixture(STORE, "challenge.html")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/menu") and len(requests) == 1:
            return httpx.Response(403, request=request, html=challenge)
        return HARNESSES[STORE].handler(request)

    with canned(handler) as client:
        tree = client.get_categories()
    assert [request.url.path for request in requests] == [
        "/cloud-api/categories-api/v1/categories/menu",
        "/supermercado",
        "/cloud-api/categories-api/v1/categories/menu",
    ]
    assert len(tree) == 3


def test_an_unbound_client_asks_the_menu_in_its_language_alone() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    transport = httpx.MockTransport(handler)
    with Carrefour(language="ca", transport=transport) as client:
        client.get_categories()
    assert "sale_point" not in requests[-1].url.params
    assert requests[-1].url.params["lang"] == "ca"


def test_a_category_is_one_page_fetch_with_a_placeholder_slug() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        category = client.get_category("cat20093")
    # the warm-up, then the page; no sitemap and no menu
    assert urls(requests) == [
        f"{SITE}/supermercado",
        f"{SITE}/supermercado/c/cat20093/c",
    ]
    assert requests[1].headers["Sec-Fetch-Dest"] == "document"
    assert category.id == "cat20093"
    assert category.name == "Leche"
    assert category.parent_id == "cat20011"
    assert category.url == f"{SITE}/supermercado/la-despensa/lacteos/leche/cat20093/c"
    # a leaf: the navigation strip lists its siblings, not children
    assert category.children == ()
    # the two totals on the page disagree; the inner one is the one paging uses
    assert category.product_count == 121
    assert [product.id for product in category.products][:2] == [
        "VC4AECOMM-481229",
        "521007071",
    ]


def test_an_aisle_returns_its_leaves_as_children() -> None:
    with canned() as client:
        aisle = client.get_category("cat20011")
    assert aisle.name == "Lácteos"
    assert [child.id for child in aisle.children] == [
        "cat20093",
        "cat20090",
        "cat20089",
    ]
    assert aisle.product_count == 394
    assert len(aisle.products) == 1


def test_a_category_offset_reaches_the_page() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        category = client.get_category("cat20093", offset=24)
    assert requests[-1].url.path == "/supermercado/c/cat20093/c"
    assert requests[-1].url.params["offset"] == "24"
    assert category.products[0].id == "521007071"


def test_a_listing_offset_is_passed_through_and_floored_upstream() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        listing = client.get_category_products(
            "/supermercado/la-despensa/lacteos/leche/cat20093/c", offset=276
        )
    assert requests[-1].url.params["offset"] == "276"
    # page_size cannot be set from the url and the offset comes back floored
    assert listing.offset == 264
    assert listing.page_size == 24
    assert listing.sort_options[0] == "best_sellers_food desc"


def test_sponsored_rows_are_flagged_and_the_ad_carousel_is_ignored() -> None:
    with canned() as client:
        listing = client.get_category_products(
            f"{SITE}/supermercado/la-despensa/lacteos/leche/cat20093/c"
        )
    assert [product.id for product in listing.products] == [
        "VC4AECOMM-481229",
        "521007071",
        "736814096",
    ]
    assert [product.is_sponsored for product in listing.products] == [
        False,
        False,
        True,
    ]
    assert "999000111" not in {product.id for product in listing.products}


def test_category_ids_are_normalised_and_unknown_ones_are_not_found() -> None:
    with canned() as client:
        assert client.get_category(20093).id == "cat20093"
        with pytest.raises(NotFoundError):
            client.get_category("cat99999999")
        for value in ("leche", "cat", "20093x"):
            with pytest.raises(ConfigurationError, match="cat20093"):
                client.get_category(value)
        with pytest.raises(ConfigurationError, match="offset"):
            client.get_category("cat20093", offset=-1)
        for value in ("", "  ", 4):
            with pytest.raises(ConfigurationError, match="category_url"):
                client.get_category_products(value)  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError, match="category_url"):
            client.get_category_products("https://example.com/x")


def test_the_home_page_carousels_are_the_cms_featured_products() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        sections = client.get_home()
    assert [section.title for section in sections] == [
        "Destacados Supermercado",
        "La Despensa",
        None,
    ]
    assert [len(section.products) for section in sections] == [2, 1, 1]
    # the warm-up and the home page are the same url
    assert urls(requests) == [f"{SITE}/supermercado", f"{SITE}/supermercado"]


# ----------------------------------------------------------------- stores


def test_the_drive_directory_is_the_source_of_bindable_ids() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        stores = client.list_stores()
    assert urls(requests)[-1] == f"{SITE}/cloud-api/salepoints/v1/drives"
    assert [store.id for store in stores] == ["005851", "005290", "0000GP"]
    assert {store.kind for store in stores} == {"drive"}
    assert stores[1].address == "Carretera Nacional VI km. 22"


def test_a_postcode_lists_physical_shops_in_another_namespace() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        stores = client.list_stores("28232")
    assert urls(requests)[-1] == (
        f"{SITE}/cloud-api/salepoints/v1/stores-location/28232"
    )
    assert [store.id for store in stores] == ["005D", "005Y"]
    assert {store.kind for store in stores} == {"store"}
    assert stores[0].sap_code == "0070"
    assert stores[0].distance_km == 1.2
    with canned() as client, pytest.raises(ConfigurationError, match="postal_code"):
        client.list_stores("2823")


def test_from_postal_code_prefers_a_drive_in_the_same_postcode() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    with Carrefour.from_postal_code(
        "28230", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.sale_point == "005290"
    assert urls(requests)[1:] == [
        f"{SITE}/cloud-api/salepoints/v1/stores-location/28230",
        f"{SITE}/cloud-api/salepoints/v1/drives",
    ]


def test_from_postal_code_falls_back_to_the_nearest_drive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return HARNESSES[STORE].handler(request)

    with Carrefour.from_postal_code(
        "28232", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.sale_point == "005290"
        assert client.catalog == "food"


def test_an_unserved_postcode_is_out_of_coverage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return HARNESSES[STORE].handler(request)

    transport = httpx.MockTransport(handler)
    # an empty answer and a 404 mean the same thing
    with pytest.raises(OutOfCoverageError):
        Carrefour.from_postal_code("08019", transport=transport)
    with pytest.raises(OutOfCoverageError):
        Carrefour.from_postal_code("08001", transport=transport)
    with pytest.raises(ConfigurationError, match="postal_code"):
        Carrefour.from_postal_code("abcde", transport=transport)


def test_a_shop_without_coordinates_still_binds_a_drive() -> None:
    shops = {"stores": [{"id": "005D", "name": "Carrefour El Pinar"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drives"):
            return httpx.Response(
                200, request=request, json=read_fixture(STORE, "drives.json")
            )
        if request.url.path == "/supermercado":
            return httpx.Response(200, request=request, html="<html></html>")
        return httpx.Response(200, request=request, json=shops)

    with Carrefour.from_postal_code(
        "28232", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.sale_point == "005851"


def test_an_empty_drive_directory_is_out_of_coverage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drives"):
            return httpx.Response(200, request=request, json={"groups": []})
        if request.url.path == "/supermercado":
            return httpx.Response(200, request=request, html="<html></html>")
        return httpx.Response(
            200, request=request, json=read_fixture(STORE, "stores_location.json")
        )

    with pytest.raises(OutOfCoverageError):
        Carrefour.from_postal_code("28232", transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------- extensions


def test_stock_always_carries_a_session_and_never_a_store() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        assert client.get_stock("521007071") == 2977
    params = requests[-1].url.params
    # omitting session is a firewall block, not an api error; the value is ignored
    assert params["session"] == "empathy"
    # the proxy overrides store from the session, so sending one would mislead
    assert "store" not in params
    # rows above 96 silently misbehave on the proxy
    assert int(params["rows"]) <= 48
    assert str(requests[-1].url).startswith(f"{SITE}/search-api/query/v1/search?")


def test_stock_for_an_unlisted_product_is_not_found() -> None:
    with canned() as client, pytest.raises(NotFoundError):
        client.get_stock("714713105")


def test_photos_are_downloaded_at_any_width(tmp_path: Any) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, request=request, content=b"jpeg")

    photo = CarrefourPhoto(
        url=f"{STATIC}/hd_350x_/img_pim_food/231394_00_1.jpg", kind="thumbnail"
    )
    with canned(handler) as client:
        client.download_photo(photo, tmp_path / "a.jpg")
        client.download_photo(photo, tmp_path / "b.jpg", width=1500)
        with pytest.raises(ConfigurationError, match="CarrefourPhoto"):
            client.download_photo(photo.url, tmp_path / "c.jpg")  # type: ignore[arg-type]
    assert requested == [
        f"{STATIC}/hd_350x_/img_pim_food/231394_00_1.jpg",
        f"{STATIC}/hd_1500x_/img_pim_food/231394_00_1.jpg",
    ]


def test_rendered_state_is_read_with_raw_decode_not_a_regex() -> None:
    # a greedy regex would stop at the first "};" inside this string
    state = {"pdp": {"product": {"product_id": "1", "name": "a};</script> b"}}}
    html = page(json.dumps(state)) + "<script>var other = {};</script></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/supermercado/p/R-"):
            return httpx.Response(
                301,
                request=request,
                headers={"Location": "/supermercado/a/R-1/p"},
            )
        return httpx.Response(200, request=request, html=html)

    with canned(handler) as client:
        product = client.get_product("1")
    assert product.name == "a};</script> b"


def test_analytics_beacons_are_never_exposed_or_followed() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        products = client.search_products("leche").products
    assert all("tagging" not in url for url in urls(requests))
    for product in products:
        assert isinstance(product, CarrefourProduct)
        for value in vars(type(product)).get("__slots__", ()):
            assert "empathy.co/tagging" not in repr(getattr(product, value, None))
    assert "tagging" not in repr(products)
