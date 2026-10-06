from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from supermercapy import (
    ConfigurationError,
    Consum,
    InvalidResponseError,
    Language,
    NotAvailableError,
    NotFoundError,
    OutOfCoverageError,
)
from supermercapy.consum import (
    ConsumProduct,
    ConsumStore,
    DeliveryMethod,
    ImageSize,
    ProductFilters,
    SortOrder,
)
from tests.conftest import read_fixture

STORE = "consum"
BASE = "https://tienda.consum.es/api/rest/V1.0"


def json_response(request: httpx.Request, data: object) -> httpx.Response:
    return httpx.Response(200, request=request, json=data)


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


def recording(data: object, requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(request, data)

    return httpx.MockTransport(handler)


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_exposes_the_zone() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Consum("147", transport=httpx.MockTransport(handler)) as client:
        assert client.zone == 147
        assert client.store_id == "147"
        assert client.drop_sponsored is True
        assert client.language is Language.SPANISH


def test_zone_is_optional_and_omits_the_zone_header() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "menu.json"), requests)

    with Consum(transport=transport) as client:
        assert client.zone is None
        assert client.store_id is None
        client.get_categories()

    assert "X-TOL-ZONE" not in requests[0].headers


@pytest.mark.parametrize("zone", ["", "  ", "abc", 0, -1, True, 1.5, "12a"])
def test_invalid_zone_is_rejected(zone: Any) -> None:
    with pytest.raises(ConfigurationError, match="zone"):
        Consum(zone)


def test_zone_and_locale_headers_are_sent() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "menu.json"), requests)

    with Consum(147, language="vl", transport=transport) as client:
        assert client.language is Language.VALENCIAN
        client.get_categories()

    assert requests[0].headers["X-TOL-ZONE"] == "147"
    assert requests[0].headers["X-TOL-LOCALE"] == "vl"


@pytest.mark.parametrize("language", ["ca", "en", "fr", ""])
def test_unsupported_languages_are_rejected(language: str) -> None:
    with pytest.raises(ConfigurationError, match="language"):
        Consum(147, language=language)


# ---------------------------------------------------------------------- search


def test_search_sends_the_documented_request_and_pages_by_offset() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "listing.json"), requests)

    with Consum(147, transport=transport) as client:
        result = client.search_products(
            "leche",
            page_size=2,
            order_by=SortOrder.PRICE_ASC,
            filters=ProductFilters(brands=("CONSUM", "ARLA"), eco=True),
            include_filters=True,
        )

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url).startswith(f"{BASE}/catalog/product?")
    assert params_of(request) == {
        "limit": ["2"],
        "offset": ["0"],
        "q": ["leche"],
        "orderById": ["1"],
        "filters": ["filter.brand:CONSUM,ARLA;filter.eco:true"],
        "includeFilters": ["true"],
    }
    assert result.query == "leche"
    assert result.offset == 0
    assert result.page_size == 2
    assert result.total_hits == 303
    assert result.total_recipe_count == 0
    assert result.has_more is True
    assert result.next_cursor == "2"
    assert tuple(product.id for product in result.products) == ("7080604", "7080596")


def test_search_honours_the_cursor() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "listing.json"), requests)

    with Consum(147, transport=transport) as client:
        result = client.search_products("leche", page_size=2, cursor="2")

    assert params_of(requests[0])["offset"] == ["2"]
    assert result.offset == 2
    assert result.next_cursor == "4"


def test_search_reports_no_next_page_when_the_storefront_says_so() -> None:
    listing = read_fixture(STORE, "listing.json")
    transport = recording({**listing, "hasMore": False}, [])

    with Consum(147, transport=transport) as client:
        result = client.search_products("leche")

    assert result.next_cursor is None
    assert result.has_more is False


def test_facets_are_parsed_and_the_blank_brand_value_is_dropped() -> None:
    transport = recording(read_fixture(STORE, "listing.json"), [])

    with Consum(147, transport=transport) as client:
        result = client.search_products("leche", include_filters=True)

    groups = {group.name: group for group in result.filter_groups}
    assert set(groups) == {
        "group.brand",
        "group.category",
        "group.categoryLeaf",
        "group.offer",
        "group.other",
    }
    brand = groups["group.brand"].filters[0]
    assert brand.id == "filter.brand"
    assert brand.kind == "list"
    assert tuple(value.id for value in brand.values) == ("ARLA", "CONSUM")
    assert brand.values[1].count == 47
    offer = groups["group.offer"].filters
    assert tuple(item.id for item in offer) == (
        "filter.offerImmediate",
        "filter.offerDeferred",
    )
    assert offer[0].values[0].name == "Ahora más barato"


def test_sponsored_rows_are_dropped_without_breaking_the_offset() -> None:
    listing = read_fixture(STORE, "category_listing.json")
    transport = recording(listing, [])

    with Consum(147, transport=transport) as client:
        result = client.get_category_products(2811, page_size=6)

    assert len(listing["products"]) == 6
    assert result.dropped_sponsored == 2
    assert tuple(product.id for product in result.products) == (
        "1826",
        "12542",
        "53553",
        "78329",
    )
    # the cursor counts the rows the storefront returned, dropped ones included
    assert result.next_cursor == "6"


def test_sponsored_rows_are_kept_when_asked() -> None:
    transport = recording(read_fixture(STORE, "category_listing.json"), [])

    with Consum(147, drop_sponsored=False, transport=transport) as client:
        result = client.get_category_products(2811)

    assert client.drop_sponsored is False
    assert result.dropped_sponsored == 0
    assert [product.id for product in result.products if product.is_sponsored] == [
        "289512",
        "303370",
    ]


def test_search_rejects_bad_arguments() -> None:
    transport = recording(read_fixture(STORE, "listing.json"), [])

    with Consum(147, transport=transport) as client:
        with pytest.raises(ConfigurationError, match="query"):
            client.search_products(None)  # type: ignore[arg-type]
        for cursor in ("", "-1", "two", 2):
            with pytest.raises(ConfigurationError, match="cursor"):
                client.search_products("leche", cursor=cursor)  # type: ignore[arg-type]
        for page_size in (0, 101, True, "10"):
            with pytest.raises(ConfigurationError, match="page_size"):
                client.search_products("leche", page_size=page_size)  # type: ignore[arg-type]
        for order_by in (True, "1", 1.0):
            with pytest.raises(ConfigurationError, match="order_by"):
                client.search_products("leche", order_by=order_by)  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError, match="filters"):
            client.search_products("leche", filters="filter.eco:true")  # type: ignore[arg-type]


def test_search_rejects_a_non_object_envelope() -> None:
    transport = recording([], [])

    with (
        Consum(147, transport=transport) as client,
        pytest.raises(InvalidResponseError, match="non-object"),
    ):
        client.search_products("leche")


def test_search_rejects_an_envelope_without_products() -> None:
    transport = recording({"totalCount": 1}, [])

    with (
        Consum(147, transport=transport) as client,
        pytest.raises(InvalidResponseError, match="products array"),
    ):
        client.search_products("leche")


# --------------------------------------------------------------------- product


def test_get_product_requests_the_code_endpoint() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "product.json"), requests)

    with Consum(147, transport=transport) as client:
        product = client.get_product(7080604)

    assert str(requests[0].url) == f"{BASE}/catalog/product/code/7080604"
    assert isinstance(product, ConsumProduct)
    assert product.id == "7080604"
    assert product.ean == "8414807514219"
    assert product.name == "Leche Semidesnatada Brik"
    assert product.nutrition is None


def test_get_product_by_ean_uses_the_exact_match_parameter() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "listing.json"), requests)

    with Consum(147, transport=transport) as client:
        product = client.get_product_by_ean("8414807514219")

    assert params_of(requests[0]) == {
        "limit": ["1"],
        "offset": ["0"],
        "ean": ["8414807514219"],
    }
    assert product.id == "7080604"


def test_get_product_by_ean_raises_when_nothing_matches() -> None:
    listing = read_fixture(STORE, "listing.json")
    transport = recording({**listing, "products": [], "totalCount": 0}, [])

    with (
        Consum(147, transport=transport) as client,
        pytest.raises(NotFoundError, match="ean"),
    ):
        client.get_product_by_ean("0000000000000")


def test_get_products_batches_codes_and_preserves_the_requested_order() -> None:
    requests: list[httpx.Request] = []
    batch = read_fixture(STORE, "batch.json")
    transport = recording(batch, requests)

    with Consum(147, transport=transport) as client:
        products = client.get_products(["53553", 7080604])

    assert str(requests[0].url) == f"{BASE}/catalog/product/codes/53553,7080604"
    # codes the zone does not carry are simply missing from the response
    assert tuple(product.id for product in products) == ("7080604",)


def test_get_products_validates_its_argument() -> None:
    transport = recording(read_fixture(STORE, "batch.json"), [])

    with Consum(147, transport=transport) as client:
        with pytest.raises(ConfigurationError, match="at least one"):
            client.get_products([])
        with pytest.raises(ConfigurationError, match="at most"):
            client.get_products(str(code) for code in range(51))
        with pytest.raises(ConfigurationError):
            client.get_products(["7080604", ""])


def test_get_products_accepts_a_single_code_string() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "batch.json"), requests)

    with Consum(147, transport=transport) as client:
        products = client.get_products("7080604")

    assert str(requests[0].url) == f"{BASE}/catalog/product/codes/7080604"
    assert tuple(product.id for product in products) == ("7080604",)


# ------------------------------------------------------------------ categories


def test_get_categories_walks_the_whole_tree_in_one_request() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "menu.json"), requests)

    with Consum(147, transport=transport) as client:
        categories = client.get_categories()

    assert len(requests) == 1
    assert str(requests[0].url) == f"{BASE}/shopping/category/menu"
    assert tuple(category.id for category in categories) == ("2811", "1690")
    despensa = categories[0]
    assert despensa.name == "Despensa"
    assert despensa.level == 1
    assert despensa.parent_id is None
    assert despensa.slug == "despensa"
    assert despensa.image_url.endswith("icon-food.svg")
    child = despensa.children[0]
    assert child.id == "1970"
    assert child.parent_id == "2811"
    assert child.level == 2
    assert tuple(node.id for node in child.children) == ("1985", "1980")
    assert child.children[0].parent_id == "1970"


def test_get_category_joins_the_tree_node_with_its_listing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/category/menu"):
            return json_response(request, read_fixture(STORE, "menu.json"))
        return json_response(request, read_fixture(STORE, "listing.json"))

    with Consum(147, transport=httpx.MockTransport(handler)) as client:
        category = client.get_category("1970")

    assert len(requests) == 2
    assert params_of(requests[1])["categories"] == ["1970"]
    assert category.id == "1970"
    assert category.name == "Aperitivos y frutos secos"
    assert category.parent_id == "2811"
    assert category.product_count == 303
    assert tuple(product.id for product in category.products) == (
        "7080604",
        "7080596",
    )
    assert tuple(node.id for node in category.children) == ("1985", "1980")


@pytest.mark.parametrize(
    "category_id", ["despensa", "19 70", "\uff11\uff19\uff17\uff10", "-1"]
)
def test_a_non_numeric_category_id_is_refused_before_any_request(
    category_id: str,
) -> None:
    # the listing answers `categories=abc` with http 500, which the retry loop
    # would spend three requests on before failing anyway
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "listing.json"), requests)

    with (
        Consum(147, transport=transport) as client,
        pytest.raises(ConfigurationError, match="numeric"),
    ):
        client.get_category_products(category_id)

    assert requests == []


def test_get_category_raises_for_an_unknown_id() -> None:
    transport = recording(read_fixture(STORE, "menu.json"), [])

    with (
        Consum(147, transport=transport) as client,
        pytest.raises(NotFoundError, match="category 9999"),
    ):
        client.get_category(9999)


def test_get_categories_rejects_a_non_array_response() -> None:
    transport = recording({"categories": []}, [])

    with (
        Consum(147, transport=transport) as client,
        pytest.raises(InvalidResponseError, match="json array"),
    ):
        client.get_categories()


# ---------------------------------------------------------------------- stores


def test_list_stores_sends_the_required_shipping_method() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "shipping_area.json"), requests)

    with Consum(transport=transport) as client:
        stores = client.list_stores("46001")

    assert params_of(requests[0]) == {
        "shippingMethod": ["D"],
        "zipCode": ["46001"],
    }
    assert len(stores) == 1
    store = stores[0]
    assert isinstance(store, ConsumStore)
    assert store.id == "147"
    assert store.zone_id == 147
    assert store.name == "Centro 147"
    assert store.kind == "delivery"
    assert store.shipping_zone_id == "147D"
    assert store.store_code == "147"
    assert store.delivery_method == "D"
    assert store.address == "Calle Jesús 38"
    assert store.postal_code == "46007"
    assert store.city == "Valencia"
    assert store.province == "VALENCIA"
    assert store.is_enabled is True
    assert store.pickup_point is None


def test_list_stores_accepts_a_pickup_method() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "shipping_area.json"), requests)

    with Consum(transport=transport) as client:
        client.list_stores("46001", method=DeliveryMethod.SHOP)

    assert params_of(requests[0])["shippingMethod"] == ["T"]


def test_list_stores_validates_its_arguments() -> None:
    transport = recording(read_fixture(STORE, "shipping_area.json"), [])

    with Consum(transport=transport) as client:
        with pytest.raises(ConfigurationError, match="postal_code"):
            client.list_stores()
        with pytest.raises(ConfigurationError, match="postal_code"):
            client.list_stores("99999")
        with pytest.raises(ConfigurationError, match="method"):
            client.list_stores("46001", method="Z")


def test_from_postal_code_binds_the_zone_with_one_request() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "shipping_area.json"), requests)

    with Consum.from_postal_code("46001", transport=transport) as client:
        assert client.zone == 147
        assert client.store_id == "147"
        assert client._client.headers["X-TOL-ZONE"] == "147"

    assert len(requests) == 1
    assert "X-TOL-ZONE" not in requests[0].headers


def test_from_postal_code_raises_outside_the_delivery_area() -> None:
    transport = recording([], [])

    with pytest.raises(OutOfCoverageError, match="28001"):
        Consum.from_postal_code("28001", transport=transport)


def test_from_postal_code_validates_the_postal_code() -> None:
    with pytest.raises(ConfigurationError, match="postal_code"):
        Consum.from_postal_code("1234")


# --------------------------------------------------------------------- catalog


def test_iter_catalog_walks_offsets_a_hundred_rows_at_a_time() -> None:
    requests: list[httpx.Request] = []
    listing = read_fixture(STORE, "category_listing.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(params_of(request)["offset"][0])
        if offset == 0:
            return json_response(request, listing)
        if offset == 6:
            page = {**listing, "products": listing["products"][:1], "hasMore": False}
            return json_response(request, page)
        raise AssertionError(f"unexpected offset {offset}")

    with Consum(147, transport=httpx.MockTransport(handler)) as client:
        products = list(client.iter_catalog())

    assert [params_of(request)["limit"] for request in requests] == [["100"], ["100"]]
    assert [params_of(request)["offset"] for request in requests] == [["0"], ["6"]]
    assert [product.id for product in products] == [
        "1826",
        "12542",
        "53553",
        "78329",
        "1826",
    ]


def test_get_catalog_deduplicates_by_code() -> None:
    listing = read_fixture(STORE, "category_listing.json")

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(params_of(request)["offset"][0])
        if offset == 0:
            return json_response(request, listing)
        return json_response(
            request, {**listing, "products": listing["products"][:1], "hasMore": False}
        )

    with Consum(147, transport=httpx.MockTransport(handler)) as client:
        catalog = client.get_catalog()

    assert [product.id for product in catalog] == ["1826", "12542", "53553", "78329"]


def test_iter_catalog_stops_when_the_offset_stops_advancing() -> None:
    requests: list[httpx.Request] = []
    listing = read_fixture(STORE, "listing.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(params_of(request)["offset"][0])
        page = {**listing, "hasMore": True}
        return json_response(request, page if offset == 0 else {**page, "products": []})

    with Consum(147, transport=httpx.MockTransport(handler)) as client:
        products = list(client.iter_catalog())

    assert len(requests) == 2
    assert len(products) == 2


# ------------------------------------------------------- new arrivals, offers


def _last_page(data: dict[str, Any]) -> dict[str, Any]:
    return {**data, "hasMore": False}


def test_get_new_arrivals_filters_server_side() -> None:
    requests: list[httpx.Request] = []
    transport = recording(_last_page(read_fixture(STORE, "listing.json")), requests)

    with Consum(147, transport=transport) as client:
        products = client.get_new_arrivals()

    assert params_of(requests[0]) == {
        "limit": ["100"],
        "offset": ["0"],
        "orderById": ["12"],
        "filters": ["filter.novelty:true"],
    }
    assert len(requests) == 1
    assert len(products) == 2


def test_get_offers_asks_for_immediate_discounts() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "offers_immediate.json"), requests)

    with Consum(147, drop_sponsored=False, transport=transport) as client:
        products = client.get_offers()

    assert params_of(requests[0])["filters"] == ["filter.offerImmediate:true"]
    assert params_of(requests[0])["orderById"] == ["5"]
    assert products[0].id == "53553"
    assert products[0].price.amount == Decimal("1.69")
    assert products[0].price.previous == Decimal("1.99")


@pytest.mark.parametrize(
    ("method", "expected_filters"),
    [
        ("get_offers", "filter.offerImmediate:true"),
        ("get_new_arrivals", "filter.novelty:true"),
    ],
)
def test_offers_and_new_arrivals_walk_every_page(
    method: str, expected_filters: str
) -> None:
    # a thousand offers live in october 2026, ten times one page; stopping at
    # the first hundred silently dropped nine in ten
    requests: list[httpx.Request] = []
    listing = read_fixture(STORE, "category_listing.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if params_of(request)["offset"] == ["0"]:
            return json_response(request, listing)
        # the walk keeps the first placement of a row that shifted pages
        return json_response(request, _last_page(listing))

    with Consum(147, transport=httpx.MockTransport(handler)) as client:
        products = getattr(client, method)()

    assert [params_of(request)["offset"] for request in requests] == [["0"], ["6"]]
    assert {params_of(request)["filters"][0] for request in requests} == {
        expected_filters
    }
    assert [product.id for product in products] == ["1826", "12542", "53553", "78329"]


def test_offer_listings_are_ad_injected_too() -> None:
    # the captured page carries a retail-media row in the first slot, so the
    # default listing policy drops it here exactly as it does elsewhere.
    transport = recording(read_fixture(STORE, "offers_immediate.json"), [])

    with Consum(147, transport=transport) as client:
        assert client.get_offers() == ()


def test_get_offers_can_include_deferred_credit() -> None:
    # the two flags are or'd: in october 2026 immediate alone matched 1,090
    # products and immediate or deferred 1,291
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "offers_deferred.json"), requests)

    with Consum(147, transport=transport) as client:
        products = client.get_offers(include_deferred=True)

    assert params_of(requests[0])["filters"] == [
        "filter.offerImmediate:true;filter.offerDeferred:true"
    ]
    assert products[0].id == "337493"


# ------------------------------------------------------------------ extensions


def test_get_groups_parses_campaigns_and_subgroups() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "groups.json"), requests)

    with Consum(147, transport=transport) as client:
        groups = client.get_groups()

    assert str(requests[0].url) == f"{BASE}/catalog/group"
    assert tuple(group.code for group in groups) == ("OCSEPTIEMBRE26", "OFERTASEPT25")
    first = groups[0]
    assert first.id == "3933482"
    assert first.name == "¡Así se ahorra en Consum!"
    assert first.slug == "asi-se-ahorra-en-consum"
    assert first.description is None
    assert first.starts_at is not None
    assert first.ends_at is not None
    assert first.starts_at < first.ends_at
    assert first.is_hidden is False
    assert first.is_recipe is False
    assert first.subgroups == ()
    assert tuple(sub.code for sub in groups[1].subgroups) == (
        "UNEUROSEPT26",
        "DOSEUROSEPT26",
    )


def test_get_group_products_selects_by_campaign_code() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "listing.json"), requests)

    with Consum(147, transport=transport) as client:
        result = client.get_group_products("OCSEPTIEMBRE26", page_size=2, cursor="2")

    assert params_of(requests[0]) == {
        "limit": ["2"],
        "offset": ["2"],
        "groups": ["OCSEPTIEMBRE26"],
    }
    assert result.offset == 2


def test_get_sort_orders_returns_the_advertised_enumeration() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "orders.json"), requests)

    with Consum(147, transport=transport) as client:
        orders = client.get_sort_orders()

    assert str(requests[0].url) == f"{BASE}/catalog/orders"
    assert tuple(order.id for order in orders) == (1, 2, 3, 4, 5, 7, 11, 12, 13)
    assert orders[0].label == "order.price.asc"
    assert orders[0].description == "Más barato primero"
    # the live enumeration is authoritative, and SortOrder mirrors it
    assert {order.id for order in orders} <= {item.value for item in SortOrder}
    assert SortOrder.UNIT_PRICE_ASC == 11


def test_suggest_returns_spelling_completions() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "semantics.json"), requests)

    with Consum(147, transport=transport) as client:
        suggestions = client.suggest("lech", limit=3)

    assert params_of(requests[0]) == {"q": ["lech"], "limit": ["3"]}
    assert tuple(item.query for item in suggestions)[:3] == (
        "lechera",
        "lecho",
        "leche",
    )
    assert suggestions[0].tag is None


def test_suggest_tags_returns_refinements() -> None:
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "tags.json"), requests)

    with Consum(147, transport=transport) as client:
        suggestions = client.suggest_tags("leche")

    assert str(requests[0].url).startswith(f"{BASE}/catalog/product/tag/?")
    assert suggestions[0].tag == "almendras"
    assert suggestions[0].query == "leche almendras"


def test_suggestions_skip_entries_without_a_query() -> None:
    transport = recording([{"tag": "soja"}, {"query": "  "}, {"query": "leche"}], [])

    with Consum(147, transport=transport) as client:
        assert tuple(item.query for item in client.suggest("lech")) == ("leche",)


def test_suggest_validates_its_arguments() -> None:
    transport = recording(read_fixture(STORE, "semantics.json"), [])

    with Consum(147, transport=transport) as client:
        for query in ("", "   ", None):
            with pytest.raises(ConfigurationError, match="query"):
                client.suggest(query)  # type: ignore[arg-type]
            with pytest.raises(ConfigurationError, match="query"):
                client.suggest_tags(query)  # type: ignore[arg-type]
        for limit in (0, -1, True, "5"):
            with pytest.raises(ConfigurationError, match="limit"):
                client.suggest("lech", limit=limit)  # type: ignore[arg-type]


def test_download_photo_streams_the_requested_rendition(tmp_path: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host.startswith("cdn-"):
            return httpx.Response(200, request=request, content=b"jpeg-bytes")
        return json_response(request, read_fixture(STORE, "product.json"))

    destination = tmp_path / "photo.jpg"
    with Consum(147, transport=httpx.MockTransport(handler)) as client:
        product = client.get_product("7080604")
        assert product.thumbnail is not None
        saved = client.download_photo(
            product.thumbnail, destination, size=ImageSize.HIGH
        )

    assert saved == destination
    assert destination.read_bytes() == b"jpeg-bytes"
    assert "/img/1600x1600/7080604_001.jpg" in str(requests[1].url)


def test_download_photo_rejects_a_foreign_photo(tmp_path: Any) -> None:
    transport = recording(read_fixture(STORE, "product.json"), [])

    with (
        Consum(147, transport=transport) as client,
        pytest.raises(ConfigurationError, match="ConsumPhoto"),
    ):
        client.download_photo("https://example.test/a.jpg", tmp_path / "a.jpg")  # type: ignore[arg-type]


# ---------------------------------------------------------------------- errors


def test_a_404_marked_44_is_not_found_and_names_the_zone() -> None:
    # consum answers a product the zone does not carry and a code that never
    # existed with the same 404 and error type 44, so the client cannot claim
    # the product exists elsewhere: NotAvailableError would be a guess
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, request=request, json=read_fixture(STORE, "error_404.json")
        )

    with Consum(147, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NotFoundError, match="zone 147") as raised:
            client.get_product("53553")
        assert not isinstance(raised.value, NotAvailableError)
        assert raised.value.status_code == 404


def test_a_404_marked_44_without_a_zone_names_the_default_assortment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, request=request, json=read_fixture(STORE, "error_404.json")
        )

    with (
        Consum(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(NotFoundError, match="default assortment"),
    ):
        client.get_product("53553")


@pytest.mark.parametrize(
    "body",
    [
        {"meta": {"code": 404}},
        {"meta": {"code": 404, "errorType": 44}},
        {"meta": "broken"},
        ["not", "an", "object"],
        None,
    ],
)
def test_an_ordinary_404_stays_a_not_found_error(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request, json=body)

    with Consum(147, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NotFoundError) as raised:
            client.get_product("53553")
        assert not isinstance(raised.value, NotAvailableError)


def test_a_non_json_404_stays_a_not_found_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request, content=b"<html>gone</html>")

    with (
        Consum(147, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(NotFoundError),
    ):
        client.get_product("53553")


def test_a_bogus_zone_returns_an_empty_page_rather_than_an_error() -> None:
    # the storefront answers HTTP 200 for an unknown zone, with a catalog of
    # its own (2,661 rows for zone 99999 in october 2026, empty in september),
    # so a client cannot tell it apart from a real zone without validating.
    listing = read_fixture(STORE, "listing.json")
    transport = recording(
        {**listing, "products": [], "totalCount": 0, "hasMore": False}, []
    )

    with Consum(99999, transport=transport) as client:
        result = client.search_products("leche")

    assert result.products == ()
    assert result.total_hits == 0
    assert result.next_cursor is None


def test_prices_are_euros_not_cents() -> None:
    transport = recording(read_fixture(STORE, "product.json"), [])

    with Consum(147, transport=transport) as client:
        product = client.get_product("7080604")

    assert product.price.amount == Decimal("0.84")
    assert product.price.unit_price == Decimal("0.84")


def test_the_packed_filters_value_is_percent_encoded_on_the_wire() -> None:
    # a raw ';' is a parameter separator for some stacks, and a repeated
    # 'filters=' would be ignored, so the whole set travels as one escaped value
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "listing.json"), requests)

    with Consum(147, transport=transport) as client:
        client.search_products(
            "leche", filters=ProductFilters(brands=("CONSUM", "ARLA"), eco=True)
        )

    query = requests[0].url.query.decode()
    assert "filters=filter.brand%3ACONSUM%2CARLA%3Bfilter.eco%3Atrue" in query
    assert query.count("filters=") == 1
    assert ";" not in query
