from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

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
    UnsupportedOperationError,
)
from supermercapy.alcampo import Alcampo, AlcampoProduct, AlcampoStore, SortOption
from tests.conftest import read_fixture
from tests.harness import _alcampo_handler

STORE = "alcampo"
BASE = "https://www.compraonline.alcampo.es"
PAGES = f"{BASE}/api/webproductpagews"
SEARCH = f"{PAGES}/v6/product-pages/search"
LISTING = f"{PAGES}/v6/product-pages"
VAGUADA = "ac90d761-9d58-4918-a37d-dd14e1ce384a"
FRUITS = "25fd70b8-959e-4633-b14a-0cee7cd3669a"
LAGUNA = "9c767992-4437-4cad-a914-0ff6ce7eebfa"
CANARIAS = "5c2a56e0-dc3b-4d29-8b67-24f8b3c78f38"
STORES = f"{BASE}/api/ecomdeliverydestinations/v4/delivery-addresses"
SESSION = f"{BASE}/api/customersessions/v2/sessions/active"


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


def client(
    requests: list[httpx.Request] | None = None,
    store: str | AlcampoStore | None = None,
    **options: Any,
) -> Alcampo:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return _alcampo_handler(request)

    options.setdefault("min_request_interval", 0.0)
    return Alcampo(store, transport=httpx.MockTransport(handler), **options)


def laguna() -> AlcampoStore:
    with client() as alcampo:
        return next(s for s in alcampo.list_stores() if s.id == LAGUNA)


def _canned(request: httpx.Request, name: str) -> httpx.Response:
    canned_response = read_fixture(STORE, name)
    return httpx.Response(
        canned_response["status"],
        request=request,
        headers=canned_response["headers"],
        content=canned_response.get("body", "").encode(),
    )


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_reads_the_default_region() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Alcampo(transport=httpx.MockTransport(handler)) as alcampo:
        assert alcampo.store_id is None
        assert alcampo.region_id == VAGUADA
        assert alcampo.language is Language.SPANISH
        assert alcampo.min_request_interval == 2.0
        assert "Chrome/" in alcampo.user_agent


def test_an_unbound_client_spends_no_request_on_its_region() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        alcampo.get_product("54180")

    assert [str(request.url) for request in requests] == [
        f"{PAGES}/v5/products/bop?retailerProductId=54180"
    ]
    assert "X-CSRF-TOKEN" not in requests[0].headers


@pytest.mark.parametrize("store", ["", "  ", True, 3.5])
def test_an_unusable_store_is_refused(store: Any) -> None:
    with pytest.raises(ConfigurationError, match="store"):
        Alcampo(store)


@pytest.mark.parametrize("language", ["ca", "en", "vl"])
def test_only_spanish_is_served(language: str) -> None:
    with pytest.raises(ConfigurationError, match="language"):
        Alcampo(language=language)


# ---------------------------------------------------------------- capabilities


def test_the_declared_capabilities() -> None:
    for capability in (
        Capability.STORES,
        Capability.NUTRITION,
        Capability.PROMOTIONS,
        Capability.NEW_ARRIVALS,
        Capability.OFFERS,
    ):
        assert Alcampo.supports(capability)
    for capability in (
        Capability.POSTAL_CODE,
        Capability.CATALOG,
        Capability.EAN,
        Capability.EAN_LOOKUP,
        Capability.HOME,
        Capability.FUTURE_PRICES,
    ):
        assert not Alcampo.supports(capability)


def test_the_catalog_and_postcodes_are_not_offered_and_cost_no_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        with pytest.raises(UnsupportedOperationError):
            alcampo.get_catalog()
        with pytest.raises(UnsupportedOperationError):
            next(iter(alcampo.iter_catalog()))
        with pytest.raises(UnsupportedOperationError):
            Alcampo.from_postal_code("28029")

    assert requests == []


# ---------------------------------------------------------------------- search


def test_search_sends_the_documented_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        page = alcampo.search_products("leche")

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{SEARCH}?q=leche&tag=web&includeAdditionalPageInfo=true"
        "&maxPageSize=30&maxProductsToDecorate=30"
    )
    assert page.query == "leche"
    assert page.total_hits is None
    assert page.next_cursor == "bc6d1b6c-cf6c-48db-a1a5-947ef0f4ed8e"


def test_search_pages_by_passing_the_token_back_verbatim() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        products = list(alcampo.iter_search("leche"))

    assert len(requests) == 2
    assert params_of(requests[1])["pageToken"] == [
        "bc6d1b6c-cf6c-48db-a1a5-947ef0f4ed8e"
    ]
    assert "includeAdditionalPageInfo" not in params_of(requests[1])
    assert len(products) == 6


def test_search_accepts_a_category_a_sort_and_filters() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        alcampo.search_products(
            "leche",
            page_size=10,
            category_id=FRUITS,
            sort=SortOption.PRICE_ASCENDING,
            filters={"dummyValue": "new"},
        )

    assert str(requests[0].url) == (
        f"{SEARCH}?q=leche&categoryId={FRUITS}&tag=web"
        "&includeAdditionalPageInfo=true&maxPageSize=10&maxProductsToDecorate=10"
        "&sortOptionId=priceAscending&filters=dummyValue%3Dnew"
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"query": ""}, "query"),
        ({"query": "leche", "page_size": 301}, "page_size"),
        ({"query": "leche", "sort": "cheapest"}, "sort"),
        ({"query": "leche", "filters": ["new"]}, "filters"),
        ({"query": "leche", "cursor": ""}, "cursor"),
    ],
)
def test_unusable_search_arguments_are_refused(
    arguments: dict[str, Any], message: str
) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo, pytest.raises(ConfigurationError, match=message):
        alcampo.search_products(**arguments)

    assert requests == []


# --------------------------------------------------------------------- product


def test_get_product_reads_the_sheet() -> None:
    with client() as alcampo:
        product = alcampo.get_product(54180)

    assert isinstance(product, AlcampoProduct)
    assert product.id == "54180"
    assert product.nutrition is not None


def test_a_missing_product_raises_not_found() -> None:
    with client() as alcampo, pytest.raises(NotFoundError):
        alcampo.get_product("1")


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_tree_in_one_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        categories = alcampo.get_categories()
        alcampo.get_categories(depth=2)

    assert str(requests[0].url) == (
        f"{PAGES}/v1/categories?decoration=false&categoryDepth=4"
    )
    assert params_of(requests[1])["categoryDepth"] == ["2"]
    assert categories[0].retailer_category_id == "OCFYP"


@pytest.mark.parametrize("depth", [0, 5, True, "2"])
def test_an_out_of_range_tree_depth_is_refused(depth: Any) -> None:
    with client() as alcampo, pytest.raises(ConfigurationError, match="depth"):
        alcampo.get_categories(depth=depth)


def test_get_category_reads_one_listing() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        category = alcampo.get_category(FRUITS, page_size=300)

    assert str(requests[0].url) == (
        f"{LISTING}?categoryId={FRUITS}&tag=web&includeAdditionalPageInfo=true"
        "&maxPageSize=300&maxProductsToDecorate=300"
    )
    assert category.id == FRUITS
    assert category.product_count == 235
    assert len(category.products) == 3
    assert category.children


def test_an_unknown_category_raises_not_found() -> None:
    with client() as alcampo, pytest.raises(NotFoundError):
        alcampo.get_category("00000000-0000-4000-8000-000000000000")


# -------------------------------------------------------------- offers, extras


def test_offers_read_the_promotions_listing_of_the_default_region() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        offers = alcampo.get_offers()

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{BASE}/api/product-listing-pages/v1/pages/promotions?regionId={VAGUADA}"
        "&tag=web&includeAdditionalPageInfo=true"
        "&maxPageSize=30&maxProductsToDecorate=30"
    )
    assert offers
    assert all(product.promotions for product in offers)


def test_new_arrivals_filter_the_whole_shop_listing_on_new_products() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        products = alcampo.get_new_arrivals()

    # the storefront's own "nuevo producto" filter on the root listing, in
    # pages of three hundred; the one page it held needs no second request
    assert [str(request.url) for request in requests] == [
        f"{LISTING}?tag=web&includeAdditionalPageInfo=true&maxPageSize=300"
        "&maxProductsToDecorate=300&filters=dummyValue%3Dnew"
    ]
    assert [product.id for product in products] == ["25140", "24858", "747889"]
    assert all(product.is_new for product in products)


def test_new_arrivals_follow_the_page_token_past_the_root_count() -> None:
    # the root listing reports a product count of zero, which is no count
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = read_fixture(STORE, "new_arrivals.json")
        if "pageToken" in request.url.params:
            page["productGroups"] = page["productGroups"][1:]
            return httpx.Response(200, request=request, json=page)
        page["productGroups"] = page["productGroups"][:1]
        page["metadata"] = {"nextPageToken": "t1"}
        return httpx.Response(200, request=request, json=page)

    with Alcampo(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as alcampo:
        products = alcampo.get_new_arrivals()

    assert len(requests) == 2
    assert params_of(requests[1])["pageToken"] == ["t1"]
    assert params_of(requests[1])["filters"] == ["dummyValue=new"]
    assert [product.id for product in products] == ["25140", "24858", "747889"]


def test_promotions_take_the_retailer_category_code() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        alcampo.get_promotions(retailer_category_id="OC1701", page_size=5)

    assert params_of(requests[0])["retailerCategoryId"] == ["OC1701"]
    assert params_of(requests[0])["maxPageSize"] == ["5"]


def test_suggest_sends_the_term_the_limit_and_the_region() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        suggestions = alcampo.suggest("lech", limit=4)

    assert str(requests[0].url) == (
        f"{BASE}/api/search/v1/suggestions/primary?searchTerm=lech&limit=4"
        f"&regionId={VAGUADA}"
    )
    assert suggestions[0] == "lechuga"


def test_similar_products_are_resolved_through_the_batch_put() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        products = alcampo.get_similar("54180")

    assert [request.method for request in requests] == ["GET", "GET", "PUT"]
    assert str(requests[1].url) == f"{BASE}/"
    assert str(requests[2].url) == f"{PAGES}/v6/products"
    assert requests[2].headers["X-CSRF-TOKEN"] == (
        "00000000-0000-4000-8000-000000000000"
    )
    assert json.loads(requests[2].read()) == read_fixture(STORE, "similar.json")
    assert products
    assert all(isinstance(product, AlcampoProduct) for product in products)


def test_related_products_take_the_narrowing_parameters() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        alcampo.get_related("54180", limit=5, high_relevance_only=True)
        alcampo.get_related("54180")

    assert str(requests[0].url) == (
        f"{PAGES}/v5/products/related?retailerProductId=54180"
        "&limit=5&highRelevanceOnly=true"
    )
    related = [r for r in requests if r.url.path.endswith("/related")]
    assert str(related[1].url) == (
        f"{PAGES}/v5/products/related?retailerProductId=54180"
    )


# ---------------------------------------------------------------------- stores


def test_list_stores_reads_every_collection_point_in_one_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo:
        stores = alcampo.list_stores()

    assert [str(request.url) for request in requests] == [
        f"{STORES}?deliveryMethod=CUSTOMER_COLLECTION"
    ]
    assert len(stores) == 6
    assert {store.region_id for store in stores} >= {VAGUADA, CANARIAS}


def test_a_postal_code_keeps_its_province_with_its_own_points_first() -> None:
    with client() as alcampo:
        stores = alcampo.list_stores("08042")

    assert [store.postal_code for store in stores] == ["08042", "08019", "08950"]


def test_a_postal_code_is_validated_before_any_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as alcampo, pytest.raises(ConfigurationError):
        alcampo.list_stores("8001")

    assert requests == []


# --------------------------------------------------------------------- binding


def test_binding_a_store_id_moves_the_session_before_the_first_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests, LAGUNA) as alcampo:
        assert alcampo.store_id == LAGUNA
        assert alcampo.region_id is None
        alcampo.get_offers()
        assert alcampo.region_id == CANARIAS

    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", f"{STORES}?deliveryMethod=CUSTOMER_COLLECTION"),
        ("GET", f"{BASE}/"),
        ("PUT", SESSION),
        (
            "GET",
            f"{BASE}/api/product-listing-pages/v1/pages/promotions"
            f"?regionId={CANARIAS}&tag=web&includeAdditionalPageInfo=true"
            "&maxPageSize=30&maxProductsToDecorate=30",
        ),
    ]
    move = requests[2]
    assert json.loads(move.read()) == {
        "deliveryDestinationId": LAGUNA,
        "regionId": CANARIAS,
    }
    assert move.headers["X-CSRF-TOKEN"] == "00000000-0000-4000-8000-000000000000"
    assert move.headers["visitor-id"] == "00000000-0000-4000-8000-000000000001"
    assert move.headers["customer-id"] == ""
    # only the move carries the token; every get goes without
    assert "X-CSRF-TOKEN" not in requests[3].headers


def test_binding_a_store_from_the_list_skips_the_lookup() -> None:
    store = laguna()
    requests: list[httpx.Request] = []

    with client(requests, store) as alcampo:
        assert alcampo.store_id == LAGUNA
        assert alcampo.region_id == CANARIAS
        alcampo.search_products("leche")

    assert [request.url.path for request in requests] == [
        "/",
        "/api/customersessions/v2/sessions/active",
        "/api/webproductpagews/v6/product-pages/search",
    ]


def test_the_session_is_moved_again_after_half_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("supermercapy._core.client.time.monotonic", lambda: clock[0])
    requests: list[httpx.Request] = []

    with client(requests, laguna()) as alcampo:
        alcampo.get_product("54180")
        clock[0] += 60
        alcampo.get_product("54180")
        clock[0] += 1800
        alcampo.get_product("54180")

    moves = [r for r in requests if r.method == "PUT"]
    assert len(moves) == 2
    # the csrf token and the visitor id are minted once
    assert [r.url.path for r in requests].count("/") == 1


def test_an_unknown_store_id_is_refused_on_first_use() -> None:
    requests: list[httpx.Request] = []

    with (
        client(requests, "00000000-0000-4000-8000-00000000dead") as alcampo,
        pytest.raises(ConfigurationError, match="click-and-collect point"),
    ):
        alcampo.get_product("54180")

    assert [request.url.path for request in requests] == [
        "/api/ecomdeliverydestinations/v4/delivery-addresses"
    ]


def test_a_move_the_storefront_does_not_confirm_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            session = read_fixture(STORE, "session.json")
            session["regionId"] = VAGUADA
            return httpx.Response(200, request=request, json=session)
        return _alcampo_handler(request)

    with (
        Alcampo(
            laguna(), transport=httpx.MockTransport(handler), min_request_interval=0
        ) as alcampo,
        pytest.raises(InvalidResponseError, match="did not move"),
    ):
        alcampo.get_product("54180")


def test_a_home_page_without_a_visitor_id_still_moves_the_session() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/":
            html = read_fixture(STORE, "home.html").replace('"visitorId"', '"x"')
            return httpx.Response(200, request=request, html=html)
        return _alcampo_handler(request)

    with Alcampo(
        laguna(), transport=httpx.MockTransport(handler), min_request_interval=0
    ) as alcampo:
        alcampo.get_product("54180")

    assert requests[1].headers["visitor-id"] == ""


# ------------------------------------------------------------------- waf


def test_a_waf_challenge_costs_one_request_and_is_never_retried() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _canned(request, "challenge.json")

    with (
        Alcampo(
            transport=httpx.MockTransport(handler),
            min_request_interval=0.0,
            retry_policy=RetryPolicy(max_attempts=5, backoff_factor=0.0),
        ) as alcampo,
        pytest.raises(ChallengedError) as raised,
    ):
        alcampo.search_products("leche")

    assert len(requests) == 1
    assert raised.value.status_code == 202
    assert raised.value.suggested_backoff == 1800.0
    assert "alcampo" in str(raised.value)


def test_the_edge_403_is_a_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _canned(request, "blocked.json")

    with (
        Alcampo(
            transport=httpx.MockTransport(handler), min_request_interval=0.0
        ) as alcampo,
        pytest.raises(BlockedError),
    ):
        alcampo.get_categories()


def test_a_stale_csrf_token_is_reminted_once() -> None:
    requests: list[httpx.Request] = []
    refused = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT" and refused["count"] == 0:
            refused["count"] += 1
            return httpx.Response(
                403,
                request=request,
                headers={"requestid": "x", "ecom-csrf-failure": "true"},
            )
        return _alcampo_handler(request)

    with Alcampo(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as alcampo:
        assert alcampo.get_similar("54180")

    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "PUT",
        "GET",
        "PUT",
    ]
