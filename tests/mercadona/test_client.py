from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from supermercapy import (
    ConfigurationError,
    InvalidResponseError,
    Language,
    Mercadona,
    NotFoundError,
    Product,
    ProductSummary,
    RetryPolicy,
    Store,
)
from supermercapy.mercadona import (
    HomeNotification,
    MercadonaProduct,
    MercadonaProductSummary,
    SeasonSummary,
)

STORE = "mercadona"


def json_response(request: httpx.Request, data: object) -> httpx.Response:
    return httpx.Response(200, request=request, json=data)


def test_constructor_performs_no_io_and_context_manager_closes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("constructor or attribute access performed I/O")

    client = Mercadona(
        "MAD3", language=Language.ENGLISH, transport=httpx.MockTransport(handler)
    )
    assert client.warehouse == "mad3"
    assert client.store_id == "mad3"
    assert client.language is Language.ENGLISH
    assert client.min_request_interval == 0
    assert not client.is_closed
    assert requests == []

    with client as entered:
        assert entered is client
    assert client.is_closed
    client.close()
    with pytest.raises(ConfigurationError, match="closed"), client:
        pass


@pytest.mark.parametrize("warehouse", ["", "a", "bad/warehouse", "mád3", 123])
def test_invalid_warehouse_is_rejected(warehouse: Any) -> None:
    with pytest.raises(ConfigurationError):
        Mercadona(warehouse)


@pytest.mark.parametrize("language", ["fr", "ES", "", "vl"])
def test_invalid_language_is_rejected(language: str) -> None:
    with pytest.raises(ConfigurationError, match="language"):
        Mercadona("mad3", language=language)


def test_catalan_language_is_supported() -> None:
    with Mercadona("mad3", language="ca") as client:
        assert client.language is Language.CATALAN


@pytest.mark.parametrize("timeout", ["soon", 0, -1, float("nan"), float("inf"), True])
def test_invalid_timeout_is_rejected(timeout: Any) -> None:
    with pytest.raises(ConfigurationError, match="timeout"):
        Mercadona("mad3", timeout=timeout)


def test_invalid_structured_timeout_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="timeout values"):
        Mercadona("mad3", timeout=httpx.Timeout(connect=-1, read=1, write=1, pool=1))


@pytest.mark.parametrize("interval", ["later", -1, float("nan"), float("inf"), True])
def test_invalid_request_interval_is_rejected(interval: Any) -> None:
    with pytest.raises(ConfigurationError, match="min_request_interval"):
        Mercadona("mad3", min_request_interval=interval)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": True}, "max_attempts"),
        ({"max_attempts": 1.5}, "max_attempts"),
        ({"backoff_factor": -1}, "backoff_factor"),
        ({"backoff_factor": float("nan")}, "backoff_factor"),
        ({"backoff_factor": True}, "backoff_factor"),
        ({"max_delay": -1}, "max_delay"),
        ({"max_delay": float("inf")}, "max_delay"),
        ({"jitter_ratio": -0.1}, "jitter_ratio"),
        ({"jitter_ratio": 1.1}, "jitter_ratio"),
        ({"jitter_ratio": float("nan")}, "jitter_ratio"),
    ],
)
def test_invalid_retry_policy_is_rejected(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        RetryPolicy(**kwargs)


def test_from_postal_code_resolves_warehouse_and_sends_exact_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, request=request, headers={"X-Customer-Wh": "mad3"}, json={}
        )

    with Mercadona.from_postal_code(
        "28001",
        min_request_interval=0.25,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.warehouse == "mad3"
        assert client.min_request_interval == 0.25

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PUT"
    assert request.url == httpx.URL(
        "https://tienda.mercadona.es/api/postal-codes/actions/change-pc/"
    )
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-language"] == "es"
    assert request.headers["user-agent"].startswith("supermercapy/")
    assert json.loads(request.read()) == {"new_postal_code": "28001"}


def test_from_postal_code_closes_client_when_header_is_missing() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json={})
    )
    with pytest.raises(InvalidResponseError, match="X-Customer-Wh"):
        Mercadona.from_postal_code("28001", transport=transport)


def test_list_stores_resolves_the_warehouse_for_a_postcode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, request=request, headers={"X-Customer-Wh": "BCN1"}, json={}
        )

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        assert client.list_stores("08001") == (
            Store(id="bcn1", name="bcn1", kind="warehouse"),
        )
        with pytest.raises(ConfigurationError, match="postal_code"):
            client.list_stores()


def test_search_encodes_query_and_parses_pagination(load_fixture: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(request, load_fixture(STORE, "search.json"))

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        result = client.search_products(
            "café con leche & miel", cursor="2", page_size=2
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == httpx.URL(
        "https://7uzjkl1dj0-dsn.algolia.net/1/indexes/products_prod_mad3_es/query"
    )
    assert request.headers["x-algolia-application-id"] == "7UZJKL1DJ0"
    assert request.headers["x-algolia-api-key"]
    assert json.loads(request.read()) == {
        "params": "query=caf%C3%A9+con+leche+%26+miel&page=2&hitsPerPage=2"
    }
    assert result.query == "café con leche & miel"
    assert result.page == 2
    assert result.page_size == 2
    assert result.total_hits == 7
    assert result.total_pages == 4
    assert result.next_cursor == "3"
    assert result.has_more
    assert result.processing_time_ms == 3
    assert tuple(product.id for product in result.products) == ("1001", "2002")
    assert result.products[1].price.amount == Decimal("1.345")


def test_search_last_page_has_no_cursor(load_fixture: Any) -> None:
    search = load_fixture(STORE, "search.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(request, {**search, "page": 3})

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        result = client.search_products("x", cursor="3")
    assert result.next_cursor is None
    assert not result.truncated


def test_the_last_page_under_the_index_cap_is_truncated(load_fixture: Any) -> None:
    # the index pages through a thousand hits at most, so the last page it
    # serves for a broad query leaves thousands of matches unreached
    search = load_fixture(STORE, "search.json")
    capped = {**search, "nbHits": 4282, "nbPages": 500, "page": 499}

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(request, capped)

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        last = client.search_products("", cursor="499", page_size=2)

    assert last.next_cursor is None
    assert last.truncated
    assert last.total_hits == 4282


def test_search_can_filter_by_top_level_category(load_fixture: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(request, load_fixture(STORE, "search.json"))

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        client.search_products("", top_level_category_id=20)

    assert json.loads(requests[0].read()) == {
        "params": "query=&page=0&hitsPerPage=20&filters=categories.id%3A20"
    }


def test_indexed_catalog_partitions_paginates_and_reconciles(
    load_fixture: Any,
) -> None:
    requests: list[httpx.Request] = []
    search = load_fixture(STORE, "search.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = json.loads(request.read())["params"]
        if "facets=" in params:
            return json_response(
                request,
                {
                    "hits": [],
                    "nbHits": 3,
                    "nbPages": 0,
                    "facets": {"categories.id": {"6": 2, "7": 2}},
                },
            )
        if "categories.id%3A6" in params:
            return json_response(
                request,
                {
                    **search,
                    "hits": search["hits"],
                    "nbHits": 2,
                    "nbPages": 1,
                    "page": 0,
                    "hitsPerPage": 1000,
                },
            )
        return json_response(
            request,
            {
                **search,
                "hits": [search["hits"][1], {**search["hits"][0], "id": "3003"}],
                "nbHits": 2,
                "nbPages": 1,
                "page": 0,
                "hitsPerPage": 1000,
            },
        )

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        result = client.get_indexed_catalog()

    assert result.reported_total_hits == 3
    assert result.queried_category_ids == ("6", "7")
    assert result.reconciled
    assert tuple(product.id for product in result.products) == ("1001", "2002", "3003")
    assert len(requests) == 3


def test_indexed_catalog_follows_partition_pages(load_fixture: Any) -> None:
    requests: list[httpx.Request] = []
    search = load_fixture(STORE, "search.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = parse_qs(json.loads(request.read())["params"], keep_blank_values=True)
        if "facets" in params:
            return json_response(
                request,
                {
                    "hits": [],
                    "nbHits": 3,
                    "nbPages": 0,
                    "facets": {"categories.id": {"6": 3}},
                },
            )
        page = int(params["page"][0])
        hits = search["hits"] if page == 0 else [{**search["hits"][0], "id": "3003"}]
        return json_response(
            request, {"hits": hits, "nbHits": 3, "nbPages": 2, "page": page}
        )

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        result = client.get_indexed_catalog()

    assert result.reconciled
    assert len(requests) == 3
    assert tuple(product.id for product in result.products) == ("1001", "2002", "3003")


def _score_index(
    scores: list[tuple[str, float]], summary: dict[str, object], requests: list[Any]
) -> Any:
    """a search index without facets whose only filterable attribute is score."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = parse_qs(json.loads(request.read())["params"], keep_blank_values=True)
        if "facets" in params:
            return json_response(
                request,
                {"hits": [], "nbHits": len(scores), "nbPages": 0, "facets": {}},
            )
        lower, upper = 0.0, math.inf
        for clause in params["numericFilters"][0].split(","):
            if clause.startswith("score>="):
                lower = float(clause[len("score>=") :])
            elif clause.startswith("score<"):
                upper = float(clause[len("score<") :])
        matched = [(pid, score) for pid, score in scores if lower <= score < upper]
        size = int(params["hitsPerPage"][0])
        hits = [{**summary, "id": pid, "score": score} for pid, score in matched[:size]]
        return json_response(
            request,
            {
                "hits": hits,
                "nbHits": len(matched),
                "nbPages": math.ceil(len(matched) / size) if size else 0,
                "page": 0,
                "hitsPerPage": size,
            },
        )

    return handler


def test_indexed_catalog_without_facets_partitions_by_score(
    load_fixture: Any,
) -> None:
    requests: list[httpx.Request] = []
    summary = load_fixture(STORE, "search.json")["hits"][0]
    scores = [(str(1000 + index), float(index)) for index in range(1, 1501)]

    with Mercadona(
        "4558", transport=httpx.MockTransport(_score_index(scores, summary, requests))
    ) as client:
        result = client.get_indexed_catalog()

    assert result.reported_total_hits == 1500
    assert result.reconciled
    assert len(result.products) == 1500
    assert result.queried_category_ids == ()
    assert result.queried_score_ranges == (
        (0.0, 512.0),
        (512.0, 1024.0),
        (1024.0, 2048.0),
    )
    assert result.partition_count == 3
    # overview, two bound probes, and one query per visited range.
    assert len(requests) == 10


def test_indexed_catalog_score_beyond_every_probe_stays_unreconciled(
    load_fixture: Any,
) -> None:
    requests: list[httpx.Request] = []
    summary = load_fixture(STORE, "search.json")["hits"][0]
    scores = [("1", 1.0), ("2", 1e13)]

    with Mercadona(
        "4558", transport=httpx.MockTransport(_score_index(scores, summary, requests))
    ) as client:
        result = client.get_indexed_catalog()

    # sixteen probes never find an empty bound, so the product above the last
    # one is never queried and the catalog says it is incomplete
    assert result.reported_total_hits == 2
    assert not result.reconciled
    assert [product.id for product in result.products] == ["1"]
    assert result.queried_score_ranges == ((0.0, 1024.0 * 4**16),)
    # overview, sixteen bound probes, and one query
    assert len(requests) == 18


def test_indexed_catalog_score_ties_beyond_the_cap_stay_unreconciled(
    load_fixture: Any,
) -> None:
    requests: list[httpx.Request] = []
    summary = load_fixture(STORE, "search.json")["hits"][0]
    scores = [(str(index), 5.0) for index in range(1200)]

    with Mercadona(
        "4558", transport=httpx.MockTransport(_score_index(scores, summary, requests))
    ) as client:
        result = client.get_indexed_catalog()

    assert not result.reconciled
    assert len(result.products) == 1000
    assert len(result.queried_score_ranges) == 1
    lower, upper = result.queried_score_ranges[0]
    assert lower <= 5.0 < upper
    assert upper - lower <= 1e-6


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": None}, "query"),
        ({"query": "x", "cursor": "-1"}, "cursor"),
        ({"query": "x", "cursor": "next"}, "cursor"),
        ({"query": "x", "cursor": 2}, "cursor"),
        ({"query": "x", "page_size": 0}, "page_size"),
        ({"query": "x", "page_size": 1001}, "page_size"),
    ],
)
def test_search_validates_arguments(kwargs: dict[str, Any], message: str) -> None:
    with (
        Mercadona("mad3") as client,
        pytest.raises(ConfigurationError, match=message),
    ):
        client.search_products(**kwargs)


def test_get_product_returns_complete_immutable_model(load_fixture: Any) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url == httpx.URL(
            "https://tienda.mercadona.es/api/products/1001/?lang=en&wh=mad3"
        )
        return json_response(request, load_fixture(STORE, "product_full.json"))

    with Mercadona(
        "mad3", language="en", transport=httpx.MockTransport(handler)
    ) as client:
        product = client.get_product(1001)
        count_after_call = requests
        assert product.name == "Leche entera Hacendado"
        assert product.details.suppliers == ("Proveedor Uno", "Proveedor Dos")
        assert product.details.origin == "España"
        assert product.origin == "España"
        assert product.details.alcohol_by_volume == Decimal("5.4")
        assert product.nutrition.ingredients == "Leche de vaca"
        assert product.price.bulk == Decimal("1.2300")
        assert product.price.previous == Decimal("1.30")
        assert product.photos[0].file_name == "milk.jpg"
        assert product.photos[0].url.endswith("/images/milk.jpg")
        assert product.photos[0].perspective == 2
        assert len(product.photos) == 2
        assert product.thumbnail is not None
        assert product.categories[0].children[0].id == "72"
        assert product.category_ids == ("6", "72")
        assert product.category_path == product.categories
        assert requests == count_after_call

    assert isinstance(product, MercadonaProduct)
    assert isinstance(product, Product)
    assert isinstance(product, ProductSummary)
    assert not isinstance(product, MercadonaProductSummary)
    assert not hasattr(product, "__dict__")
    with pytest.raises(FrozenInstanceError):
        product.name = "Changed"  # type: ignore[misc]


def test_get_product_accepts_missing_optional_and_unknown_fields(
    load_fixture: Any,
) -> None:
    transport = httpx.MockTransport(
        lambda request: json_response(
            request, load_fixture(STORE, "product_missing_optional.json")
        )
    )
    with Mercadona("mad3", transport=transport) as client:
        product = client.get_product("id with / characters")

    assert product.id == "2002"
    assert product.photos == ()
    assert product.thumbnail is None
    assert product.details.description is None
    assert product.nutrition.allergens is None
    assert product.price.unit_size is None


def test_categories_category_and_catalog_request_counts(load_fixture: Any) -> None:
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        data_by_path = {
            "/api/categories/": load_fixture(STORE, "categories.json"),
            "/api/categories/72/": load_fixture(STORE, "category_72.json"),
            "/api/categories/73/": load_fixture(STORE, "category_73.json"),
        }
        return json_response(request, data_by_path[request.url.path])

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        categories = client.get_categories()
        category = client.get_category(72)
        before_catalog = len(requests)
        catalog = client.get_catalog()

    assert categories[0].id == "6"
    assert tuple(child.id for child in categories[0].children) == ("72", "73")
    assert category.children[0].products[0].id == "1001"
    assert requests[:2] == [
        httpx.URL("https://tienda.mercadona.es/api/categories/?lang=es&wh=mad3"),
        httpx.URL("https://tienda.mercadona.es/api/categories/72/?lang=es&wh=mad3"),
    ]
    assert len(requests) - before_catalog == 3
    assert requests[before_catalog:] == [
        httpx.URL("https://tienda.mercadona.es/api/categories/?lang=es&wh=mad3"),
        httpx.URL("https://tienda.mercadona.es/api/categories/72/?lang=es&wh=mad3"),
        httpx.URL("https://tienda.mercadona.es/api/categories/73/?lang=es&wh=mad3"),
    ]
    assert tuple(product.id for product in catalog) == ("1001", "3003")
    assert all(isinstance(product, MercadonaProductSummary) for product in catalog)


def category_tree_client(requests: list[httpx.URL], load_fixture: Any) -> Mercadona:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        if request.url.path == "/api/categories/":
            return json_response(request, load_fixture(STORE, "categories.json"))
        if request.url.path == "/api/categories/72/":
            return json_response(request, load_fixture(STORE, "category_72.json"))
        return httpx.Response(404, request=request, json={"detail": "not found"})

    return Mercadona("mad3", transport=httpx.MockTransport(handler))


def test_a_root_category_id_falls_back_to_the_tree(load_fixture: Any) -> None:
    requests: list[httpx.URL] = []
    with category_tree_client(requests, load_fixture) as client:
        category = client.get_category(6)

    assert requests == [
        httpx.URL("https://tienda.mercadona.es/api/categories/6/?lang=es&wh=mad3"),
        httpx.URL("https://tienda.mercadona.es/api/categories/?lang=es&wh=mad3"),
    ]
    assert category.id == "6"
    assert category.level == 0
    assert category.products == ()
    assert tuple(child.id for child in category.children) == ("72", "73")
    assert [child.level for child in category.children] == [1, 1]


def test_an_id_the_tree_does_not_hold_as_a_root_is_not_found(
    load_fixture: Any,
) -> None:
    requests: list[httpx.URL] = []
    with category_tree_client(requests, load_fixture) as client:
        with pytest.raises(NotFoundError):
            client.get_category(9999)
        # a third-level group has no page and is no root either
        with pytest.raises(NotFoundError):
            client.get_category(343)

    assert [url.path for url in requests] == [
        "/api/categories/9999/",
        "/api/categories/",
        "/api/categories/343/",
        "/api/categories/",
    ]


def test_a_second_level_category_costs_one_request(load_fixture: Any) -> None:
    requests: list[httpx.URL] = []
    with category_tree_client(requests, load_fixture) as client:
        category = client.get_category(72)

    assert requests == [
        httpx.URL("https://tienda.mercadona.es/api/categories/72/?lang=es&wh=mad3")
    ]
    assert category.level == 1


def test_category_levels_follow_the_tree_when_upstream_omits_them(
    load_fixture: Any,
) -> None:
    # the category endpoints stopped sending a level; only the second level
    # has a category page, so a fetched category sits at level one
    raw = load_fixture(STORE, "category_72.json")
    assert "level" not in raw["categories"][0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/categories/":
            return json_response(request, load_fixture(STORE, "categories.json"))
        return json_response(request, raw)

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        roots = client.get_categories()
        category = client.get_category(72)

    assert roots[0].level == 0
    assert [child.level for child in roots[0].children] == [1, 1]
    assert category.level == 1
    assert category.children[0].level == 2


def test_home_preserves_duplicate_layouts_and_parses_items(load_fixture: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(request, load_fixture(STORE, "home.json"))

    transport = httpx.MockTransport(handler)
    with Mercadona("mad3", transport=transport) as client:
        sections = client.get_home()

    assert tuple(section.layout for section in sections) == (
        "notification",
        "carousel",
        "carousel",
        "banner",
        "future-layout",
    )
    assert [request.url for request in requests] == [
        httpx.URL("https://tienda.mercadona.es/api/home/?lang=es&wh=mad3")
    ]
    notification = sections[0].items[0]
    assert isinstance(notification, HomeNotification)
    assert notification.kind == "info"
    assert notification.event_key == "availability_no_address"
    # the action is an object now, and the url it leads to is what is kept
    assert notification.action == "https://tienda.mercadona.es/authenticate-user/"
    assert notification.action_title == "Identifícate"
    assert sections[0].products == ()
    assert sections[1].items[0].id == "1001"
    assert sections[1].products[0].id == "1001"
    assert sections[2].items[0].id == "3003"
    season = sections[3].items[0]
    assert isinstance(season, SeasonSummary)
    assert season.id == "season-uuid"
    assert season.banner_id == "186"
    assert season.background_colors == ("black", "transparent")
    assert sections[3].products == ()
    assert sections[4].items == ()


def test_new_arrivals_and_season_are_summaries(load_fixture: Any) -> None:
    urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(request.url)
        if request.url.path == "/api/home/new-arrivals/":
            return json_response(request, load_fixture(STORE, "new_arrivals.json"))
        return json_response(request, load_fixture(STORE, "season.json"))

    with Mercadona("mad3", transport=httpx.MockTransport(handler)) as client:
        arrivals = client.get_new_arrivals()
        season = client.get_season("season uuid/with slash")

    assert urls == [
        httpx.URL("https://tienda.mercadona.es/api/home/new-arrivals/?lang=es&wh=mad3"),
        httpx.URL(
            "https://tienda.mercadona.es/api/home/sections/"
            "season%20uuid%2Fwith%20slash/?lang=es&wh=mad3"
        ),
    ]
    assert isinstance(arrivals[0], MercadonaProductSummary)
    # the feed's items carry no is_new_arrival flag; the feed itself is one
    assert "is_new_arrival" not in load_fixture(STORE, "new_arrivals.json")["items"][0]
    assert all(arrival.is_new for arrival in arrivals)
    assert season.id == "season-uuid"
    assert season.products[0].id == "3003"


@pytest.mark.parametrize(
    ("method", "argument"),
    [
        ("get_product", ""),
        ("get_product", True),
        ("get_category", []),
        ("get_season", "   "),
    ],
)
def test_resource_ids_are_validated(method: str, argument: Any) -> None:
    with Mercadona("mad3") as client, pytest.raises(ConfigurationError):
        getattr(client, method)(argument)
