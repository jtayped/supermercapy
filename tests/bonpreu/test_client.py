from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from supermercapy import (
    AuthenticationError,
    BlockedError,
    Bonpreu,
    Capability,
    ChallengedError,
    ConfigurationError,
    InvalidResponseError,
    Language,
    NotFoundError,
    RetryPolicy,
    TransportError,
    UnsupportedOperationError,
)
from supermercapy.bonpreu import BonpreuProduct, SortOption
from tests.conftest import read_fixture
from tests.harness import _bonpreu_handler

STORE = "bonpreu"
BASE = "https://www.compraonline.bonpreuesclat.cat"
PAGES = f"{BASE}/api/webproductpagews"
SEARCH = f"{PAGES}/v6/product-pages/search"
LISTING = f"{PAGES}/v6/product-pages"
REGION = "d57f6327-ce67-44d5-bdd5-dc63e98460a6"
FRUITS = "130716f2-795a-4f0b-ad39-b449817921b3"
NOVETATS = "2068f9ba-b1af-4cf1-ad2b-3301c8d1a977"


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


def canned(requests: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return _bonpreu_handler(request)

    return httpx.MockTransport(handler)


def client(requests: list[httpx.Request] | None = None, **options: Any) -> Bonpreu:
    options.setdefault("min_request_interval", 0.0)
    return Bonpreu(transport=canned(requests), **options)


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_binds_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Bonpreu(transport=httpx.MockTransport(handler)) as bonpreu:
        assert bonpreu.store_id is None
        assert bonpreu.region_id == REGION
        assert bonpreu.language is Language.CATALAN
        assert bonpreu.min_request_interval == 2.0


def test_pacing_defaults_to_two_seconds() -> None:
    # pacing does not prevent the waf challenge, it only delays it; the
    # default is the slowest rate reconnaissance still found workable
    assert Bonpreu.default_min_request_interval == 2.0


def test_the_language_travels_as_a_cookie_and_not_as_a_header() -> None:
    # accept-language is ignored by the backend; the `language` cookie is the
    # only locale lever there is
    requests: list[httpx.Request] = []

    with client(requests, language="es") as bonpreu:
        assert bonpreu.language is Language.SPANISH
        bonpreu.get_categories()

    assert requests[0].headers["Cookie"] == "language=es-ES"
    assert requests[0].headers["Accept-Language"] == "es"


def test_catalan_is_the_default_language() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.get_categories()

    assert requests[0].headers["Cookie"] == "language=ca-ES"


@pytest.mark.parametrize("language", ["en", "vl", "gl", ""])
def test_unsupported_languages_are_rejected(language: str) -> None:
    with pytest.raises(ConfigurationError, match="language"):
        Bonpreu(language=language)


def test_no_request_carries_a_key_or_a_csrf_header() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.get_product("29189")

    headers = requests[0].headers
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"].startswith("Mozilla/5.0 ")
    assert "X-CSRF-TOKEN" not in headers
    assert "Authorization" not in headers


def test_the_default_user_agent_is_a_browser_and_can_be_overridden() -> None:
    # the waf refuses /api/ to a user agent it does not take for a browser, so
    # the library's own `supermercapy/<version>` default would be blocked outright
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        assert "Chrome/" in bonpreu.user_agent
        bonpreu.get_categories()
    with client(requests, user_agent="custom/1") as bonpreu:
        bonpreu.get_categories()

    assert requests[0].headers["User-Agent"] == Bonpreu.default_user_agent
    assert requests[1].headers["User-Agent"] == "custom/1"


# ---------------------------------------------------------------- capabilities


def test_the_declared_capabilities_match_the_reconnaissance() -> None:
    assert Bonpreu.supports(Capability.NUTRITION)
    assert Bonpreu.supports(Capability.PROMOTIONS)
    assert Bonpreu.supports(Capability.NEW_ARRIVALS)
    assert Bonpreu.supports(Capability.OFFERS)
    # no barcode of any kind exists in the api, and one region serves everyone
    assert not Bonpreu.supports(Capability.EAN)
    assert not Bonpreu.supports(Capability.EAN_LOOKUP)
    assert not Bonpreu.supports(Capability.POSTAL_CODE)
    assert not Bonpreu.supports(Capability.STORES)
    assert not Bonpreu.supports(Capability.HOME)
    assert not Bonpreu.supports(Capability.FUTURE_PRICES)


def test_the_catalog_is_not_offered_and_costs_no_request() -> None:
    # twenty-one thousand products against a budget of twenty-odd requests:
    # an iter_catalog here would always die part way through
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        assert not Bonpreu.supports(Capability.CATALOG)
        with pytest.raises(UnsupportedOperationError) as catalog:
            bonpreu.get_catalog()
        with pytest.raises(UnsupportedOperationError):
            next(iter(bonpreu.iter_catalog()))

    assert catalog.value.capability is Capability.CATALOG
    assert catalog.value.store == "bonpreu"
    assert requests == []


def test_there_is_no_postcode_or_store_selection() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        with pytest.raises(UnsupportedOperationError):
            Bonpreu.from_postal_code("08013")
        with pytest.raises(UnsupportedOperationError):
            bonpreu.list_stores()
        with pytest.raises(UnsupportedOperationError):
            bonpreu.get_home()

    assert requests == []


# ---------------------------------------------------------------------- search


def test_search_sends_the_documented_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        page = bonpreu.search_products("llet")

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{SEARCH}?q=llet&tag=web&includeAdditionalPageInfo=true"
        "&maxPageSize=30&maxProductsToDecorate=30"
    )
    assert page.query == "llet"
    assert page.page_size == 30
    # the storefront publishes no hit count anywhere
    assert page.total_hits is None
    assert page.next_cursor == "56f27f77-f00f-4f4b-85ae-b5c65627c546"
    assert page.has_more is True


def test_search_pages_by_passing_the_token_back_verbatim() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        first = bonpreu.search_products("llet")
        last = bonpreu.search_products("llet", cursor=first.next_cursor)

    assert params_of(requests[1])["pageToken"] == [first.next_cursor]
    # the extra page metadata is only asked for on the first page
    assert "includeAdditionalPageInfo" not in params_of(requests[1])
    assert last.products == ()
    # the token outlives the products, so an empty page ends the walk
    assert last.next_cursor is None


def test_a_page_token_walk_replays_the_cookies_that_scope_it() -> None:
    # a token is session scoped: without the VISITORID and global_sid of the
    # request that produced it, the follow-up is refused (http 400 in september
    # 2026; a token the session does not hold answers 401 since october)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = _bonpreu_handler(request)
        response.headers["Set-Cookie"] = "VISITORID=canned; Path=/"
        return response

    with Bonpreu(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as bonpreu:
        first = bonpreu.search_products("llet")
        bonpreu.search_products("llet", cursor=first.next_cursor)

    assert "VISITORID" not in requests[0].headers.get("Cookie", "")
    cookies = requests[1].headers["Cookie"]
    assert "VISITORID=canned" in cookies
    assert "language=ca-ES" in cookies


def test_iter_search_terminates_at_the_first_empty_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        products = list(bonpreu.iter_search("llet"))

    assert len(requests) == 2
    assert [product.id for product in products] == [
        "49637",
        "49644",
        "23597",
        "23882",
    ]


def test_a_long_query_is_truncated_the_way_the_storefront_truncates_it() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.search_products("llet " * 20)

    assert params_of(requests[0])["q"] == ["llet " * 10]
    assert len(params_of(requests[0])["q"][0]) == 50


@pytest.mark.parametrize("query", ["", "   ", None, 7])
def test_an_unusable_query_is_refused_before_any_request(query: Any) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu, pytest.raises(ConfigurationError, match="query"):
        bonpreu.search_products(query)

    assert requests == []


def test_page_size_is_capped_at_the_storefront_maximum() -> None:
    assert Bonpreu.default_page_size == 30
    assert Bonpreu.max_page_size == 300
    with client() as bonpreu:
        with pytest.raises(ConfigurationError, match="page_size"):
            bonpreu.search_products("llet", page_size=301)
        with pytest.raises(ConfigurationError, match="page_size"):
            bonpreu.search_products("llet", page_size=0)


def test_search_accepts_a_category_a_sort_and_filters() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.search_products(
            "poma",
            page_size=10,
            category_id=FRUITS,
            sort=SortOption.PRICE_ASCENDING,
            filters={"boolean": "onOffer", "dietaryAndLifestyle": ["eco", "vegan"]},
        )

    assert str(requests[0].url) == (
        f"{SEARCH}?q=poma&categoryId={FRUITS}&tag=web&includeAdditionalPageInfo=true"
        "&maxPageSize=10&maxProductsToDecorate=10&sortOptionId=priceAscending"
        "&filters=boolean%3DonOffer%26dietaryAndLifestyle%3Deco%2Cvegan"
    )


def test_a_filter_value_holding_a_space_is_encoded_twice() -> None:
    # the value is percent-encoded once inside the filters string and once more
    # as a query parameter, so a brand id lands as LA%2520COLLITA
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.search_products("poma", filters={"brands": "LA COLLITA"})

    assert requests[0].url.query.decode().endswith("filters=brands%3DLA%2520COLLITA")


@pytest.mark.parametrize("sort", ["cheapest", "", 3])
def test_an_unknown_sort_is_refused(sort: Any) -> None:
    with client() as bonpreu, pytest.raises(ConfigurationError, match="sort"):
        bonpreu.search_products("llet", sort=sort)


@pytest.mark.parametrize("filters", ["boolean=onOffer", 3, ["eco"]])
def test_filters_must_be_a_mapping(filters: Any) -> None:
    with client() as bonpreu, pytest.raises(ConfigurationError, match="filters"):
        bonpreu.search_products("llet", filters=filters)


@pytest.mark.parametrize("cursor", ["", "  ", 5])
def test_an_unusable_cursor_is_refused(cursor: Any) -> None:
    with client() as bonpreu, pytest.raises(ConfigurationError, match="cursor"):
        bonpreu.search_products("llet", cursor=cursor)


# --------------------------------------------------------------------- product


def test_get_product_sends_the_numeric_retailer_id() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        product = bonpreu.get_product(29189)

    assert len(requests) == 1
    assert str(requests[0].url) == f"{PAGES}/v5/products/bop?retailerProductId=29189"
    assert isinstance(product, BonpreuProduct)
    assert product.id == "29189"
    assert product.product_uuid == "f3ea1055-906f-430a-b024-8bd03feadb9b"
    assert product.ean is None


def test_a_retired_product_raises_not_found() -> None:
    with client() as bonpreu, pytest.raises(NotFoundError):
        bonpreu.get_product("00000")


# ------------------------------------------------------------------ categories


def test_get_categories_reads_the_whole_tree_in_one_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        categories = bonpreu.get_categories()

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{PAGES}/v1/categories?decoration=false&categoryDepth=4"
    )
    assert [category.retailer_category_id for category in categories] == [
        "03",
        "01",
        "90",
    ]
    assert categories[0].children[0].parent_id == categories[0].id


def test_the_tree_depth_can_be_trimmed() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.get_categories(depth=1)

    assert params_of(requests[0])["categoryDepth"] == ["1"]


@pytest.mark.parametrize("depth", [0, 5, -1, True, "2", 1.5])
def test_an_out_of_range_tree_depth_is_refused(depth: Any) -> None:
    with client() as bonpreu, pytest.raises(ConfigurationError, match="depth"):
        bonpreu.get_categories(depth=depth)


def test_get_category_sends_category_id_and_never_category() -> None:
    # `category=` is silently accepted and answers with the root pseudo-category
    # and generic products, which is the easiest way to read the wrong data here
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        category = bonpreu.get_category(FRUITS)

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{LISTING}?categoryId={FRUITS}&tag=web&includeAdditionalPageInfo=true"
        "&maxPageSize=30&maxProductsToDecorate=30"
    )
    assert "category" not in params_of(requests[0])
    assert category.id == FRUITS
    assert category.retailer_category_id == "0301"
    assert category.product_count == 358
    assert [child.retailer_category_id for child in category.children] == [
        "030108",
        "030101",
        "030102",
    ]
    assert [product.id for product in category.products] == ["03643", "07464", "33958"]


def test_an_unknown_category_is_a_404_and_raises_not_found() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu, pytest.raises(NotFoundError):
        bonpreu.get_category("00000000-0000-4000-8000-000000000000")

    assert len(requests) == 1


def test_a_listing_that_falls_back_to_the_root_category_raises_not_found() -> None:
    # what an unknown category answered until september 2026, and what
    # `category=` instead of `categoryId=` still answers: the root listing
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            request, read_fixture(STORE, "category_root.json"), status=200
        )

    with (
        Bonpreu(transport=httpx.MockTransport(handler), min_request_interval=0.0) as bp,
        pytest.raises(NotFoundError, match="category"),
    ):
        bp.get_category("not-a-category")


# ------------------------------------------------------- new arrivals, offers


def test_new_arrivals_reads_the_novetats_category_and_marks_the_rows() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        products = bonpreu.get_new_arrivals()

    assert len(requests) == 1
    # the whole category in one page: two hundred-odd products, where the
    # default thirty would silently drop most of them
    assert str(requests[0].url) == (
        f"{LISTING}?categoryId={NOVETATS}&tag=web&includeAdditionalPageInfo=true"
        "&maxPageSize=300&maxProductsToDecorate=300"
    )
    assert products
    assert all(product.is_new for product in products)


def _arrivals_page(
    ids: list[str], *, token: str | None, count: int | None
) -> dict[str, Any]:
    page = read_fixture(STORE, "new_arrivals.json")
    template = page["productGroups"][0]["decoratedProducts"][0]
    page["productGroups"] = [
        {
            "type": "ungrouped",
            "decoratedProducts": [
                {**template, "retailerProductId": item} for item in ids
            ],
        }
    ]
    page["metadata"] = {} if token is None else {"nextPageToken": token}
    if count is None:
        del page["additionalPageInfo"]["currentCategory"]
    else:
        page["additionalPageInfo"]["currentCategory"]["productCount"] = count
    return page


@pytest.mark.parametrize(
    ("pages", "expected_ids", "expected_requests"),
    [
        # the reported count is reached on the second page: no third request
        (
            [(["1", "2"], "t1", 4), (["3", "4"], "t2", None)],
            ["1", "2", "3", "4"],
            2,
        ),
        # no count reported: walk until a page adds nothing
        (
            [(["1", "2"], "t1", None), (["3"], "t2", None), ([], "t3", None)],
            ["1", "2", "3"],
            3,
        ),
        # a page that only repeats rows ends the walk too
        (
            [(["1", "2"], "t1", 9), (["2", "1"], "t2", None)],
            ["1", "2"],
            2,
        ),
    ],
)
def test_new_arrivals_walk_the_category_by_page_token(
    pages: list[tuple[list[str], str | None, int | None]],
    expected_ids: list[str],
    expected_requests: int,
) -> None:
    # novetats held 510 products in october 2026, more than one page of 300
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        ids, token, count = pages[len(requests) - 1]
        return _json_response(
            request, _arrivals_page(ids, token=token, count=count), status=200
        )

    with Bonpreu(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as bonpreu:
        products = bonpreu.get_new_arrivals()

    assert [product.id for product in products] == expected_ids
    assert all(product.is_new for product in products)
    assert len(requests) == expected_requests
    assert "pageToken" not in params_of(requests[0])
    for index, request in enumerate(requests[1:]):
        assert params_of(request)["pageToken"] == [pages[index][1]]
        assert params_of(request)["categoryId"] == [NOVETATS]
        assert params_of(request)["maxPageSize"] == ["300"]


def test_offers_read_the_promotions_listing_for_the_one_region() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        products = bonpreu.get_offers()

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{BASE}/api/product-listing-pages/v1/pages/promotions?regionId={REGION}"
        "&tag=web&includeAdditionalPageInfo=true"
        "&maxPageSize=30&maxProductsToDecorate=30"
    )
    assert [product.id for product in products] == ["23201", "36338", "42519"]


def test_promotions_accept_either_category_identifier() -> None:
    # unlike the plain category listing, this one takes the hierarchical
    # retailer code as well as the uuid
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        page = bonpreu.get_promotions(retailer_category_id="0301", page_size=3)
        bonpreu.get_promotions(category_id=FRUITS, cursor=page.next_cursor)

    assert params_of(requests[0])["retailerCategoryId"] == ["0301"]
    assert params_of(requests[0])["maxPageSize"] == ["3"]
    assert params_of(requests[1])["categoryId"] == [FRUITS]
    assert params_of(requests[1])["pageToken"] == [page.next_cursor]
    assert page.next_cursor == "00000000-0000-4000-8000-000000000001"
    # the promotions listing offers no "on offer" boolean group of its own
    assert [group.id for group in page.filters] == ["brands", "dietaryAndLifestyle"]
    # and names its default order `relevance` where the others say `favorite`
    assert page.sort_options[0] == "relevance"


# ------------------------------------------------------------------ extensions


def test_suggest_sends_the_term_the_limit_and_the_region() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        suggestions = bonpreu.suggest("llet", limit=4)

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{BASE}/api/search/v1/suggestions/primary?searchTerm=llet&limit=4"
        f"&regionId={REGION}"
    )
    assert suggestions[0] == "llet"


@pytest.mark.parametrize("limit", [0, -1, True, "5"])
def test_an_unusable_suggestion_limit_is_refused(limit: Any) -> None:
    with client() as bonpreu, pytest.raises(ConfigurationError, match="limit"):
        bonpreu.suggest("llet", limit=limit)


def test_similar_products_are_resolved_through_the_batch_endpoint() -> None:
    # the endpoint answers with internal uuids alone, and one batch request
    # resolves them all; the batch is the only non-get in the client, so it is
    # also the only request that mints and carries a csrf token
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        products = bonpreu.get_similar("29189")

    assert [request.method for request in requests] == ["GET", "GET", "PUT"]
    assert str(requests[0].url) == (
        f"{PAGES}/v5/products/similar?retailerProductId=29189"
    )
    assert str(requests[1].url) == f"{BASE}/"
    assert str(requests[2].url) == f"{PAGES}/v6/products"
    assert json.loads(requests[2].read()) == [
        "371e6587-1b14-4272-8e9e-488e2d3b879d",
        "53465c7a-a818-4176-8903-a87e93a8c7c8",
    ]
    assert requests[2].headers["X-CSRF-TOKEN"] == "00000000-0000-4000-8000-000000000000"
    assert "X-CSRF-TOKEN" not in requests[0].headers
    assert [product.id for product in products] == ["03643", "07464"]


def test_the_csrf_token_is_minted_once_and_reused() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.get_similar("29189")
        bonpreu.get_similar("29189")

    assert [str(request.url) for request in requests].count(f"{BASE}/") == 1


def test_related_products_take_the_documented_narrowing_parameters() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.get_related("29189", limit=5, high_relevance_only=True)

    assert str(requests[0].url) == (
        f"{PAGES}/v5/products/related?retailerProductId=29189"
        "&limit=5&highRelevanceOnly=true"
    )


def test_an_empty_uuid_list_costs_no_batch_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json=[])

    with Bonpreu(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as bonpreu:
        assert bonpreu.get_similar("29189") == ()

    assert len(requests) == 1


def test_a_home_page_without_a_token_raises_rather_than_sending_a_blank_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, request=request, html="<html></html>")
        return _bonpreu_handler(request)

    with (
        Bonpreu(transport=httpx.MockTransport(handler), min_request_interval=0.0) as bp,
        pytest.raises(InvalidResponseError, match="csrf"),
    ):
        bp.get_similar("29189")


# ------------------------------------------------------------- waf challenge


def _canned(request: httpx.Request, name: str, **headers: str) -> httpx.Response:
    canned_response = read_fixture(STORE, name)
    return httpx.Response(
        canned_response["status"],
        request=request,
        headers={**canned_response["headers"], **headers},
        content=canned_response.get("body", "").encode(),
    )


def _challenge(request: httpx.Request, **headers: str) -> httpx.Response:
    return _canned(request, "challenge.json", **headers)


def _csrf_failure(request: httpx.Request) -> httpx.Response:
    return _canned(request, "csrf_failure.json")


def test_a_waf_challenge_costs_exactly_one_request_and_is_never_retried() -> None:
    # every challenged request appears to spend from the same per-ip budget, so
    # polling until it clears is exactly the wrong strategy
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _challenge(request)

    with (
        Bonpreu(
            transport=httpx.MockTransport(handler),
            min_request_interval=0.0,
            retry_policy=RetryPolicy(max_attempts=5, backoff_factor=0.0),
        ) as bonpreu,
        pytest.raises(ChallengedError) as raised,
    ):
        bonpreu.search_products("llet")

    assert len(requests) == 1
    assert raised.value.status_code == 202
    assert raised.value.suggested_backoff == 1800.0
    assert raised.value.retry_after == 1800.0
    assert "waf" in str(raised.value)


def test_a_challenge_is_recognised_without_the_exposed_action_header() -> None:
    # the other tell is the missing `requestid`, which every genuine response
    # carries; a proxy that strips the exposed header must not turn a 202 with
    # an empty body into a confusing json decode error
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202, request=request, headers={"server": "CloudFront"}, content=b""
        )

    with (
        Bonpreu(transport=httpx.MockTransport(handler), min_request_interval=0.0) as bp,
        pytest.raises(ChallengedError),
    ):
        bp.get_categories()

    assert len(requests) == 1


def test_a_genuine_202_is_not_mistaken_for_a_challenge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202, request=request, headers={"requestid": "abc"}, json=[]
        )

    with Bonpreu(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as bonpreu:
        assert bonpreu.get_categories() == ()


def test_the_challenge_does_not_stop_the_client_from_being_reused() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return _challenge(request)
        return _bonpreu_handler(request)

    with Bonpreu(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as bonpreu:
        with pytest.raises(ChallengedError):
            bonpreu.get_categories()
        assert bonpreu.get_categories()


# ------------------------------------------------------------- csrf and 403


def test_the_origins_403_mints_a_fresh_token_and_retries_once() -> None:
    # the origin's 403 carries `requestid` and `ecom-csrf-failure`: a stale csrf
    # token, not a block
    requests: list[httpx.Request] = []
    refused = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT" and refused["count"] == 0:
            refused["count"] += 1
            return _csrf_failure(request)
        return _bonpreu_handler(request)

    with Bonpreu(
        transport=httpx.MockTransport(handler), min_request_interval=0.0
    ) as bonpreu:
        products = bonpreu.get_similar("29189")

    methods = [request.method for request in requests]
    assert methods == ["GET", "GET", "PUT", "GET", "PUT"]
    # the retry carries a token minted after the refusal, not the stale one
    assert requests[-1].headers["X-CSRF-TOKEN"]
    assert len(products) == 2


def test_a_403_on_the_token_page_itself_does_not_loop() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/":
            return _csrf_failure(request)
        return _bonpreu_handler(request)

    with (
        Bonpreu(transport=httpx.MockTransport(handler), min_request_interval=0.0) as bp,
        pytest.raises(TransportError),
    ):
        bp.get_similar("29189")

    assert [request.url.path for request in requests].count("/") == 1


def test_related_products_can_be_asked_for_without_narrowing() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as bonpreu:
        bonpreu.get_related("29189")

    assert (
        str(requests[0].url) == f"{PAGES}/v5/products/related?retailerProductId=29189"
    )


def test_a_token_that_cannot_be_reminted_gives_up_rather_than_looping() -> None:
    requests: list[httpx.Request] = []
    served = {"home": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/":
            served["home"] += 1
            if served["home"] > 1:
                return httpx.Response(500, request=request, content=b"")
        if request.method == "PUT":
            return _csrf_failure(request)
        return _bonpreu_handler(request)

    with (
        Bonpreu(
            transport=httpx.MockTransport(handler),
            min_request_interval=0.0,
            retry_policy=RetryPolicy(max_attempts=1),
        ) as bp,
        pytest.raises(AuthenticationError),
    ):
        bp.get_similar("29189")

    # one mint, one failed remint, and no third attempt
    assert served["home"] == 2


def test_the_edge_403_is_a_block_and_costs_exactly_one_request() -> None:
    # cloudfront's "request blocked" page has no `requestid`: the waf refused
    # the caller, and minting a csrf token and retrying would only spend more
    # of the same per-ip budget. this is what a non-browser user agent gets.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _canned(request, "blocked.json")

    with (
        Bonpreu(
            transport=httpx.MockTransport(handler),
            min_request_interval=0.0,
            retry_policy=RetryPolicy(max_attempts=5, backoff_factor=0.0),
        ) as bonpreu,
        pytest.raises(BlockedError) as raised,
    ):
        bonpreu.search_products("llet")

    assert type(raised.value) is BlockedError
    assert raised.value.status_code == 403
    assert [request.url.path for request in requests] == [
        "/api/webproductpagews/v6/product-pages/search"
    ]


def test_an_edge_403_on_the_batch_put_does_not_remint_the_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT":
            return _canned(request, "blocked.json")
        return _bonpreu_handler(request)

    with (
        Bonpreu(transport=httpx.MockTransport(handler), min_request_interval=0.0) as bp,
        pytest.raises(BlockedError),
    ):
        bp.get_similar("29189")

    assert [request.method for request in requests] == ["GET", "GET", "PUT"]


def test_a_page_token_the_session_does_not_hold_is_refused_and_not_retried() -> None:
    # an unknown or stale token answers http 401 `CC-090 Unauthorized` since
    # october 2026; in september it was a 400 PageDataNotFoundException
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(
            request,
            read_fixture(STORE, "page_token_expired.json"),
            status=401,
            headers={"requestid": "00000000-0000-4000-8000-000000000000"},
        )

    with (
        Bonpreu(
            transport=httpx.MockTransport(handler),
            min_request_interval=0.0,
            retry_policy=RetryPolicy(max_attempts=5, backoff_factor=0.0),
        ) as bonpreu,
        pytest.raises(TransportError) as raised,
    ):
        bonpreu.search_products("llet", cursor="9415bca5-0c34-4d71-b38d-81491c968bd0")

    assert not isinstance(
        raised.value, (BlockedError, NotFoundError, AuthenticationError)
    )
    assert raised.value.status_code == 401
    assert len(requests) == 1


def _json_response(
    request: httpx.Request,
    data: object,
    *,
    status: int,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status, request=request, json=data, headers=headers)


@pytest.mark.parametrize(
    ("home", "raised_type"),
    [("blocked.json", BlockedError), ("challenge.json", ChallengedError)],
)
def test_a_waf_refusal_while_reminting_the_token_is_not_hidden(
    home: str, raised_type: type[BlockedError]
) -> None:
    # the put fails csrf, the remint hits the waf: that block is the answer,
    # not an AuthenticationError, and a live run must be able to tell
    requests: list[httpx.Request] = []
    served = {"home": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/":
            served["home"] += 1
            if served["home"] > 1:
                return _canned(request, home)
        if request.method == "PUT":
            return _csrf_failure(request)
        return _bonpreu_handler(request)

    with (
        Bonpreu(transport=httpx.MockTransport(handler), min_request_interval=0.0) as bp,
        pytest.raises(raised_type) as raised,
    ):
        bp.get_similar("29189")

    assert type(raised.value) is raised_type
    assert [request.method for request in requests] == ["GET", "GET", "PUT", "GET"]
