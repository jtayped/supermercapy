from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from supermercapy import (
    Bonarea,
    Capability,
    ConfigurationError,
    InvalidResponseError,
    Language,
    NotFoundError,
    RetryPolicy,
    TransportError,
)
from supermercapy.bonarea import BonareaCategory, BonareaPhoto, BonareaProduct
from tests.conftest import read_fixture
from tests.harness import HARNESSES

STORE = "bonarea"
BASE = "https://www.bonarea-online.com"
# the endpoints a read-only client is allowed to touch
READ_ONLY_ACTIONS = {
    "ShoppingBody",
    "Article",
    "GetProductSheet",
    "search",
    "GetPostalCodes",
}


def canned(handler: Any = None, **options: Any) -> Bonarea:
    """build a client against the shared canned storefront."""

    client = HARNESSES[STORE].client(handler, **options)
    assert isinstance(client, Bonarea)
    return client


def recording(requests: list[httpx.Request], **options: Any) -> Bonarea:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    return canned(handler, **options)


def replying(data: object, requests: list[httpx.Request] | None = None) -> Bonarea:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, request=request, json=data)

    client = Bonarea(min_request_interval=0.0, transport=httpx.MockTransport(handler))
    return client


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode(), keep_blank_values=True)


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_binds_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Bonarea(transport=httpx.MockTransport(handler)) as client:
        assert client.store_id is None
        assert client.language is Language.SPANISH


def test_requests_are_paced_half_a_second_apart_unless_told_otherwise() -> None:
    assert Bonarea.default_min_request_interval == 0.5
    with Bonarea() as client:
        assert client.min_request_interval == 0.5
    with Bonarea(min_request_interval=0.0) as client:
        assert client.min_request_interval == 0.0
    with Bonarea(min_request_interval=2.0) as client:
        assert client.min_request_interval == 2.0


def test_only_the_two_storefront_languages_are_offered() -> None:
    with Bonarea(language="ca", min_request_interval=0.0) as client:
        assert client.language is Language.CATALAN
    for language in ("en", "vl", "fr", ""):
        with pytest.raises(ConfigurationError, match="language"):
            Bonarea(language=language)


def test_the_declared_capabilities_are_the_two_bonarea_can_honour() -> None:
    assert Bonarea.capabilities == frozenset({Capability.CATALOG, Capability.NUTRITION})
    assert not Bonarea.supports(Capability.EAN)
    assert not Bonarea.supports(Capability.PROMOTIONS)
    assert not Bonarea.supports(Capability.POSTAL_CODE)
    assert not Bonarea.supports(Capability.STORES)


# ------------------------------------------------------------------- transport


def test_every_read_is_a_form_post_to_the_language_path() -> None:
    # the ajax endpoints are [HttpPost]-only: a GET on one answers 404
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.get_categories()
        client.get_product("13*5361")
        client.search_products("leche")
        client.delivery_zones(25)
        client.get_product_sheet("13*5361")

    posts = [request for request in requests if request.method == "POST"]
    assert len(posts) == 5
    for request in posts:
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert str(request.url).startswith(f"{BASE}/es/shop/")
        assert request.url.query == b""
        assert str(request.url).rsplit("/", 1)[-1] in READ_ONLY_ACTIONS


def test_reads_need_no_cookie_token_or_bespoke_header() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.get_category("13*300*010*010")

    request = requests[0]
    assert "cookie" not in request.headers
    assert "__RequestVerificationToken" not in request.content.decode()
    assert request.headers["user-agent"].startswith("supermercapy/")


def test_the_client_never_touches_a_cart_or_session_endpoint() -> None:
    # setting a fulfilment mode changes what later reads report, so a
    # read-only client keeps its session pristine
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.get_categories()
        client.get_category("13*300*010*010")
        client.get_product("13*5361")
        list(client.iter_search("leche"))
        list(client.iter_catalog())
        client.delivery_zones("25", "Guissona")

    actions = {
        str(request.url).rsplit("/", 1)[-1]
        for request in requests
        if request.method == "POST"
    }
    assert actions <= READ_ONLY_ACTIONS
    assert "ModifyPurchaseType" not in actions
    # the only GET is the language warm-up
    assert [str(request.url) for request in requests if request.method == "GET"] == [
        f"{BASE}/es"
    ]


# --------------------------------------------------------------- the warm-up


def test_search_primes_a_language_session_before_asking() -> None:
    # search ignores the language in the path and reads it from the asp.net
    # session, so a cookieless spanish query comes back in catalan
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.search_products("leche")

    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", f"{BASE}/es"),
        ("POST", f"{BASE}/es/shop/search"),
    ]
    assert requests[1].headers["cookie"] == "ASP.NET_SessionId=canned"


def test_the_warm_up_follows_the_selected_language() -> None:
    requests: list[httpx.Request] = []
    with recording(requests, language="ca") as client:
        client.search_products("llet")

    assert str(requests[0].url) == f"{BASE}/ca"
    assert str(requests[1].url) == f"{BASE}/ca/shop/search"


def test_the_session_is_primed_once_per_client() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.search_products("leche")
        client.search_products("leche")
        list(client.iter_search("leche"))

    assert [request.method for request in requests] == ["GET", "POST", "POST", "POST"]


def test_browsing_never_costs_a_warm_up_request() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.get_categories()
        client.get_category("13*300*010*010")
        client.get_product("13*5361")

    assert [request.method for request in requests] == ["POST", "POST", "POST"]


def test_a_search_that_fails_leaves_the_session_unprimed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, request=request, html="<html></html>")
        return httpx.Response(500, request=request)

    with canned(handler, retry_policy=RetryPolicy(max_attempts=1)) as client:
        with pytest.raises(TransportError, match="HTTP 500"):
            client.search_products("leche")
        with pytest.raises(TransportError, match="HTTP 500"):
            client.get_categories()

    # the warm-up ran for the search and not for the browse that followed
    assert [request.method for request in requests].count("GET") == 1


# ---------------------------------------------------------------------- search


def test_search_sends_only_the_query_and_pages_client_side() -> None:
    # the storefront takes no page, size or sort parameter and answers with
    # every hit at once, so a cursor walk re-requests the same payload
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        first = client.search_products("leche", page_size=2)
        second = client.search_products("leche", page_size=2, cursor=first.next_cursor)

    posts = [request for request in requests if request.method == "POST"]
    assert [form_of(request) for request in posts] == [
        {"strQuery": ["leche"]},
        {"strQuery": ["leche"]},
    ]
    assert first.query == "leche"
    assert first.total_hits == 5
    assert first.next_cursor == "2"
    assert [product.id for product in first.products] == ["13*0066", "13*0036"]
    assert [product.id for product in second.products] == ["13*8493", "13*7668"]
    assert second.next_cursor == "4"


def test_iter_search_streams_every_hit_in_one_request() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        products = list(client.iter_search("leche", page_size=2))

    assert len([request for request in requests if request.method == "POST"]) == 1
    assert len(products) == 5
    assert all(isinstance(product, BonareaProduct) for product in products)


def test_the_canned_search_shows_why_the_warm_up_exists() -> None:
    # this capture was taken through a catalan-primed session: the query is
    # spanish and every description comes back in catalan
    payload = read_fixture(STORE, "search.json")

    assert payload["strQuery"] == "leche"
    assert payload["url"].startswith("/ca/")
    assert payload["articles"][0]["description"].startswith("Llet")


def test_search_validates_its_arguments() -> None:
    with canned() as client:
        with pytest.raises(ConfigurationError, match="query"):
            client.search_products(None)  # type: ignore[arg-type]
        # "²" and "٣" pass str.isdigit; neither is an offset search returned
        for cursor in ("", "-1", "two", 2, "²", "٣"):
            with pytest.raises(ConfigurationError, match="cursor"):
                client.search_products("leche", cursor=cursor)  # type: ignore[arg-type]
        for page_size in (0, -1, True, "10"):
            with pytest.raises(ConfigurationError, match="page_size"):
                client.search_products("leche", page_size=page_size)  # type: ignore[arg-type]
            with pytest.raises(ConfigurationError, match="page_size"):
                next(client.iter_search("leche", page_size=page_size))  # type: ignore[arg-type]


# --------------------------------------------------------------------- product


def test_get_product_posts_the_identifier_percent_encoded() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        product = client.get_product("13*5361")

    assert str(requests[0].url) == f"{BASE}/es/shop/Article"
    # '*' is not a safe character in a form body, so it travels escaped
    assert requests[0].content == b"identifier=13%2A5361"
    assert isinstance(product, BonareaProduct)
    assert product.id == "13*5361"
    assert product.nutrition is not None


@pytest.mark.parametrize("product_id", ["13_5361", "13*5361", " 13_5361 "])
def test_an_underscore_id_is_translated_before_the_request(product_id: str) -> None:
    # the api answers the underscore spelling with {"article": null}, so the
    # client normalises whatever a url or an image name gave the caller
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        assert client.get_product(product_id).id == "13*5361"

    assert form_of(requests[0]) == {"identifier": ["13*5361"]}


def test_a_null_article_with_http_200_raises_not_found() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client, pytest.raises(NotFoundError) as raised:
        client.get_product("13*9999999")

    assert read_fixture(STORE, "product_missing.json") == {"article": None}
    assert "13*9999999" in str(raised.value)
    assert raised.value.status_code == 404


def test_get_product_validates_its_argument() -> None:
    with canned() as client:
        for product_id in ("", "   ", True, [], None):
            with pytest.raises(ConfigurationError, match="product_id"):
                client.get_product(product_id)  # type: ignore[arg-type]


def test_get_product_sheet_returns_the_rendered_modal() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, request=request, json=read_fixture(STORE, "product_sheet.json")
        )

    with canned(handler) as client:
        sheet = client.get_product_sheet("13_5361")

    assert str(requests[0].url) == f"{BASE}/es/shop/GetProductSheet"
    assert form_of(requests[0]) == {"idArticle": ["13*5361"]}
    assert 'data-ref="13*300"' in sheet


def test_an_empty_product_sheet_raises_not_found() -> None:
    with (
        replying({"success": True, "htmlProductSheet": "  "}) as client,
        pytest.raises(NotFoundError, match="13\\*1"),
    ):
        client.get_product_sheet("13*1")


def test_an_unknown_product_sheet_is_not_found_despite_its_html() -> None:
    # the storefront renders a "this product does not exist" sheet with http
    # 200 and flags it only with success: false
    payload = read_fixture(STORE, "product_sheet_missing.json")
    assert "not-existing-product" in payload["htmlProductSheet"]
    requests: list[httpx.Request] = []
    with (
        recording(requests) as client,
        pytest.raises(NotFoundError, match="13\\*9999999"),
    ):
        client.get_product_sheet("13*9999999")
    assert len(requests) == 1


# ------------------------------------------------------------------ categories


def test_get_categories_asks_for_a_blank_listing_and_costs_one_request() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        categories = client.get_categories()

    assert len(requests) == 1
    assert str(requests[0].url) == f"{BASE}/es/shop/ShoppingBody"
    assert requests[0].content == b"reference="
    assert [category.id for category in categories] == ["13*300", "13*320"]
    assert isinstance(categories[0], BonareaCategory)
    assert read_fixture(STORE, "tree.json")["articles"] == []


def test_get_category_returns_children_and_articles_in_one_request() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        category = client.get_category("13_300_010_010")

    assert len(requests) == 1
    assert form_of(requests[0]) == {"reference": ["13*300*010*010"]}
    assert category.id == "13*300*010*010"
    assert category.name == "Aves"
    assert category.level == 3
    assert category.parent_id == "13*300*010"
    assert [child.name for child in category.children] == [
        "Codornices y otras aves",
        "Pavo",
        "Pollo",
    ]
    assert [product.id for product in category.products] == [
        "13*5361",
        "13*6361",
        "13*5379",
        "13*5324",
    ]


def test_the_product_count_comes_from_the_rows_not_total_articles() -> None:
    # every node reports totalArticles 0, so the number is never usable
    with recording([]) as client:
        category = client.get_category("13*300*010*010")

    assert read_fixture(STORE, "category.json")["nivellActual"]["totalArticles"] == 0
    assert category.product_count == 4
    assert category.product_count == len(category.products)


def test_a_menu_level_category_lists_nothing() -> None:
    with recording([]) as client:
        category = client.get_category("13*300*010")

    assert category.level == 2
    assert category.products == ()
    assert category.product_count == 0
    assert [child.id for child in category.children] == ["13*300*010*010"]


def test_an_unknown_category_raises_without_a_second_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, request=request, json=read_fixture(STORE, "tree.json")
        )

    with canned(handler) as client, pytest.raises(NotFoundError, match="13\\*999"):
        client.get_category("13*999")

    assert len(requests) == 1


def test_an_unknown_reference_throws_upstream_and_is_not_found() -> None:
    # the listing answers an unknown reference with asp.net's http 500 page;
    # it is not retried, and the tree confirms the id is unknown
    requests: list[httpx.Request] = []
    no_backoff = RetryPolicy(max_attempts=3, backoff_factor=0.0, jitter_ratio=0.0)
    with (
        recording(requests, retry_policy=no_backoff) as client,
        pytest.raises(NotFoundError, match="13\\*300\\*999") as raised,
    ):
        client.get_category("13*300*999")
    assert [form_of(request)["reference"] for request in requests] == [
        ["13*300*999"],
        [""],
    ]
    assert isinstance(raised.value.__cause__, TransportError)
    assert raised.value.__cause__.status_code == 500


def test_a_runtime_error_on_a_known_category_is_reraised() -> None:
    requests: list[httpx.Request] = []
    page = read_fixture(STORE, "runtime_error.html")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if form_of(request)["reference"] == ["13*300*010*010"]:
            return httpx.Response(500, request=request, html=page)
        return HARNESSES[STORE].handler(request)

    with canned(handler) as client, pytest.raises(TransportError) as raised:
        client.get_category("13*300*010*010")
    assert not isinstance(raised.value, NotFoundError)
    assert raised.value.status_code == 500
    assert len(requests) == 2


def test_other_server_errors_are_still_retried() -> None:
    requests: list[httpx.Request] = []
    no_backoff = RetryPolicy(max_attempts=3, backoff_factor=0.0, jitter_ratio=0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, request=request, text="busy")

    with (
        canned(handler, retry_policy=no_backoff) as client,
        pytest.raises(TransportError) as raised,
    ):
        client.get_category("13*300*010*010")
    # three attempts at the listing, and a 503 never consults the tree
    assert len(requests) == 3
    assert raised.value.status_code == 503


def test_get_category_validates_its_argument() -> None:
    with canned() as client:
        for category_id in ("", "   ", True, [], None):
            with pytest.raises(ConfigurationError, match="category_id"):
                client.get_category(category_id)  # type: ignore[arg-type]


def test_a_listing_without_a_tree_is_rejected() -> None:
    with (
        replying({"articles": []}) as client,
        pytest.raises(InvalidResponseError, match="category tree"),
    ):
        client.get_categories()


# --------------------------------------------------------------------- catalog


def test_iter_catalog_walks_one_listing_per_branch_and_stays_lazy() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        catalog = client.iter_catalog()
        assert requests == []
        first = next(catalog)
        products = [first, *catalog]

    references = [form_of(request)["reference"][0] for request in requests]
    # the tree first, then the listing level, then the childless menu node
    assert references == ["", "13*300*010*010", "13*320*080"]
    assert len(products) == 7
    assert products[0].id == "13*5361"


def test_get_catalog_deduplicates_articles_listed_twice() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        reference = form_of(request)["reference"][0]
        if reference == "":
            return httpx.Response(
                200, request=request, json=read_fixture(STORE, "tree.json")
            )
        # the same category answers both listing nodes
        return httpx.Response(
            200, request=request, json=read_fixture(STORE, "category.json")
        )

    with canned(handler) as client:
        catalog = client.get_catalog()

    assert [product.id for product in catalog] == [
        "13*5361",
        "13*6361",
        "13*5379",
        "13*5324",
    ]


def test_the_catalog_walk_is_paced_by_the_shared_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # nothing rate limited during recon, but a full walk is 493 requests, so
    # the walk goes through the same pacing every other client uses
    clock = [10.0]
    delays: list[float] = []

    def sleep(delay: float) -> None:
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr("supermercapy._core.client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("supermercapy._core.client.time.sleep", sleep)
    with recording([], min_request_interval=0.25) as client:
        list(client.iter_catalog())

    assert delays == pytest.approx([0.25, 0.25])


# ------------------------------------------------------------------ extensions


def test_price_drops_filters_one_category_on_the_badge() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        products = client.price_drops("13*300*010*010")

    assert len(requests) == 1
    assert [product.id for product in products] == ["13*6361"]
    assert products[0].price.is_discounted is True


def test_new_products_walks_the_catalog_when_no_category_is_given() -> None:
    # there is no novelties endpoint, so the badge is read off every listing
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        assert client.new_products() == ()

    assert [form_of(request)["reference"][0] for request in requests] == [
        "",
        "13*300*010*010",
        "13*320*080",
    ]


def test_a_badged_article_listed_twice_is_reported_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if form_of(request)["reference"][0] == "":
            return httpx.Response(
                200, request=request, json=read_fixture(STORE, "tree.json")
            )
        return httpx.Response(
            200, request=request, json=read_fixture(STORE, "category.json")
        )

    with canned(handler) as client:
        products = client.price_drops()

    assert len(requests) == 3
    assert [product.id for product in products] == ["13*6361"]


def test_delivery_zones_returns_the_postal_codes_of_a_province() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        codes = client.delivery_zones(25, "Guissona")

    assert str(requests[0].url) == f"{BASE}/es/shop/GetPostalCodes"
    assert form_of(requests[0]) == {"province": ["25"], "town": ["Guissona"]}
    assert codes[0] == "25797"
    assert len(codes) == len(set(codes))


def test_delivery_zones_sends_a_blank_town_for_a_whole_province() -> None:
    requests: list[httpx.Request] = []
    with recording(requests) as client:
        client.delivery_zones("25")

    assert requests[0].content == b"province=25&town="


def test_delivery_zones_validates_its_arguments() -> None:
    with canned() as client:
        for province in ("", "   ", True, None):
            with pytest.raises(ConfigurationError, match="province"):
                client.delivery_zones(province)  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError, match="town"):
            client.delivery_zones(25, 7)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- photos


def test_download_photo_asks_the_cdn_for_a_rendition(tmp_path: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "images.bonarea.com":
            return httpx.Response(200, request=request, content=b"png-bytes")
        return HARNESSES[STORE].handler(request)

    destination = tmp_path / "wings.png"
    with canned(handler) as client:
        product = client.get_product("13*5361")
        saved = client.download_photo(
            product.photos[0], destination, width=500, height=500
        )

    assert saved == destination
    assert destination.read_bytes() == b"png-bytes"
    assert str(requests[1].url) == (
        "https://images.bonarea.com/13_5361_1.png?width=500&height=500"
    )


def test_download_photo_defaults_to_the_original_upload(tmp_path: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, content=b"png-bytes")

    with canned(handler) as client:
        client.download_photo(
            BonareaPhoto(
                url="https://images.bonarea.com/13_5361_1.png",
                file_name="13_5361_1.png",
            ),
            tmp_path / "wings.png",
        )

    assert str(requests[0].url) == "https://images.bonarea.com/13_5361_1.png"


def test_a_failed_download_is_retried_without_reading_the_stream(
    tmp_path: Any,
) -> None:
    requests: list[httpx.Request] = []
    no_backoff = RetryPolicy(max_attempts=2, backoff_factor=0.0, jitter_ratio=0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # an unread stream: the body could say anything, it is not looked at
        return httpx.Response(500, request=request, content=iter([b"customErrors"]))

    photo = BonareaPhoto(
        url="https://images.bonarea.com/13_5361_1.png", file_name="13_5361_1.png"
    )
    with (
        canned(handler, retry_policy=no_backoff) as client,
        pytest.raises(TransportError),
    ):
        client.download_photo(photo, tmp_path / "a.png")
    assert len(requests) == 2


def test_download_photo_validates_its_arguments(tmp_path: Any) -> None:
    photo = BonareaPhoto(
        url="https://images.bonarea.com/13_5361_1.png", file_name="13_5361_1.png"
    )
    with canned() as client:
        with pytest.raises(ConfigurationError, match="BonareaPhoto"):
            client.download_photo(photo.url, tmp_path / "a.png")  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError, match="together"):
            client.download_photo(photo, tmp_path / "a.png", width=500)
        with pytest.raises(ConfigurationError, match="together"):
            client.download_photo(photo, tmp_path / "a.png", height=500)
