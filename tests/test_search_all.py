"""search_all asks several stores at once and reports each one on its own."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from supermercapy import (
    ALL_CLIENTS,
    BaseClient,
    Bonarea,
    Category,
    ConfigurationError,
    Consum,
    InvalidResponseError,
    Plusfresc,
    Product,
    RetryPolicy,
    SearchAllResult,
    SearchResult,
    SearchStatus,
    StoreSearch,
    _multi,
    search_all,
    to_json,
)
from tests.canned import Storefronts, failing

STORE_NAMES = [client_type.store_name for client_type in ALL_CLIENTS]


def run(fronts: Storefronts, query: str = "llet", **options: Any) -> SearchAllResult:
    return search_all(
        query, transport=fronts.transport, min_request_interval=0.0, **options
    )


def by_store(result: SearchAllResult) -> dict[str, StoreSearch]:
    return {entry.store: entry for entry in result.stores}


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[BaseClient]:
    """record every client search_all builds."""

    clients: list[BaseClient] = []
    real = _multi.open_client

    def spy(*args: Any) -> BaseClient:
        client = real(*args)
        clients.append(client)
        return client

    monkeypatch.setattr(_multi, "open_client", spy)
    return clients


class _Unserviceable(BaseClient):
    """a store that needs a binding no postal code can resolve."""

    store_name = "unserviceable"

    def __init__(self, warehouse: str, **options: Any) -> None:
        super().__init__(**options)

    def search_products(
        self, query: str, *, page_size: int | None = None, cursor: str | None = None
    ) -> SearchResult:
        raise AssertionError("a skipped store is never asked")

    def get_product(self, product_id: str | int) -> Product:
        raise NotImplementedError

    def get_categories(self) -> tuple[Category, ...]:
        raise NotImplementedError

    def get_category(self, category_id: str | int) -> Category:
        raise NotImplementedError


class _Mute(_Unserviceable):
    """a store that binds nothing and fails without a message."""

    store_name = "mute"

    def __init__(self, **options: Any) -> None:
        BaseClient.__init__(self, **options)

    def search_products(
        self, query: str, *, page_size: int | None = None, cursor: str | None = None
    ) -> SearchResult:
        raise InvalidResponseError()


# ------------------------------------------------------------------ outcomes


def test_every_store_reports_in_all_clients_order() -> None:
    fronts = Storefronts()

    result = run(fronts)

    assert result.query == "llet"
    assert result.postal_code is None
    assert [entry.store for entry in result.stores] == STORE_NAMES
    entries = by_store(result)
    assert entries["mercadona"] == StoreSearch(
        store="mercadona",
        status=SearchStatus.SKIPPED,
        reason="needs a postal code to bind a warehouse",
    )
    assert fronts.count("mercadona") == 0
    for name in STORE_NAMES[1:]:
        assert entries[name].status is SearchStatus.OK
        assert isinstance(entries[name].result, SearchResult)
    assert entries["plusfresc"].store_id == "12"
    assert entries["consum"].store_id is None
    assert result.failed == ()


def test_a_postal_code_binds_every_store_that_declares_one() -> None:
    fronts = Storefronts()

    result = run(fronts, "leche", postal_code="28232")

    assert result.postal_code == "28232"
    entries = by_store(result)
    # every store binds or searches unbound, except the three that do not serve it
    assert {name: entries[name].status for name in STORE_NAMES} == {
        **dict.fromkeys(STORE_NAMES, SearchStatus.OK),
        "plusfresc": SearchStatus.OUT_OF_COVERAGE,
        "lidl": SearchStatus.OUT_OF_COVERAGE,
        "condis": SearchStatus.OUT_OF_COVERAGE,
    }
    assert entries["mercadona"].store_id == "mad3"
    assert entries["consum"].store_id == "147"
    assert entries["carrefour"].store_id == "005290"
    assert entries["dia"].store_id == "28232"
    assert entries["aldi"].store_id == "pen"
    assert entries["bonarea"].store_id is None
    assert entries["plusfresc"] == StoreSearch(
        store="plusfresc",
        status=SearchStatus.OUT_OF_COVERAGE,
        reason="plusfresc does not deliver to 28232",
        error_type="OutOfCoverageError",
    )
    assert result.failed == ()


def test_a_failing_store_does_not_hide_the_others() -> None:
    fronts = Storefronts({"lidl": failing(400)})

    result = run(fronts)

    entries = by_store(result)
    lidl = entries["lidl"]
    assert lidl.status is SearchStatus.FAILED
    assert lidl.error_type == "TransportError"
    assert lidl.reason is not None
    assert lidl.reason.endswith("returned HTTP 400")
    assert lidl.result is None
    assert result.failed == (lidl,)
    assert "lidl" not in result.pages
    assert {"consum", "plusfresc", "bonarea", "carrefour", "bonpreu"} <= set(
        result.pages
    )


def test_an_exception_without_a_message_reports_its_type() -> None:
    result = search_all("llet", stores=_Mute)

    (entry,) = result.stores
    assert entry.status is SearchStatus.FAILED
    assert entry.reason == entry.error_type == "InvalidResponseError"


def test_a_binding_no_postal_code_can_supply_is_always_skipped() -> None:
    result = search_all("llet", stores=_Unserviceable, postal_code="28001")

    assert result.stores == (
        StoreSearch(
            store="unserviceable",
            status=SearchStatus.SKIPPED,
            reason="needs a warehouse, which a postal code cannot supply",
        ),
    )


def test_a_bug_propagates_after_every_client_closes(
    opened: list[BaseClient],
) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug in a parser")

    fronts = Storefronts({"bonarea": broken})

    with pytest.raises(RuntimeError, match="bug in a parser"):
        run(fronts, max_workers=1)

    # one worker opens the stores in order; those still queued are cancelled
    assert [type(client) for client in opened][:3] == [Consum, Plusfresc, Bonarea]
    assert all(client.is_closed for client in opened)


def test_every_client_is_closed(opened: list[BaseClient]) -> None:
    fronts = Storefronts({"lidl": failing(400)})

    run(fronts, postal_code="28232")

    # plusfresc, lidl and condis never opened
    assert len(opened) == len(ALL_CLIENTS) - 3
    assert all(client.is_closed for client in opened)


def test_the_shared_transport_is_left_open() -> None:
    fronts = Storefronts()
    closed: list[bool] = []

    class Owned(httpx.MockTransport):
        def close(self) -> None:
            closed.append(True)

    search_all(
        "llet",
        stores="consum",
        transport=Owned(fronts.transport.handler),
    )

    assert closed == []


# ------------------------------------------------------------ request counts


@pytest.mark.parametrize(
    ("store", "unbound", "postal_code", "bound"),
    [
        ("mercadona", 0, "28001", 2),
        ("consum", 1, "46001", 2),
        ("plusfresc", 2, "25001", 3),
        ("bonarea", 2, "25001", 2),
        ("carrefour", 1, "28232", 4),
        # one request per 250 stores in the list; the fixture pages twice
        ("lidl", 1, "08013", 3),
        ("bonpreu", 1, "08013", 1),
        # the delivery check and the move of the session, then the search
        ("dia", 1, "08001", 3),
        # the region comes from the province, with no request
        ("aldi", 1, "07001", 1),
    ],
)
def test_request_counts_match_the_documentation(
    store: str, unbound: int, postal_code: str, bound: int
) -> None:
    fronts = Storefronts()
    run(fronts, stores=store)
    assert fronts.count(store) == unbound

    fronts = Storefronts()
    result = run(fronts, stores=store, postal_code=postal_code)
    assert result.stores[0].status is SearchStatus.OK
    assert fronts.count(store) == bound


def test_one_worker_asks_the_stores_one_after_another() -> None:
    fronts = Storefronts()

    result = run(fronts, max_workers=1)

    assert [store for store, _ in fronts.requests] == [
        "consum",
        "plusfresc",
        "plusfresc",
        "bonarea",
        "bonarea",
        "carrefour",
        "lidl",
        "bonpreu",
        "dia",
        "eroski",
        "caprabo",
        "aldi",
        "ahorramas",
        "alcampo",
        "condis",
    ]
    assert result == run(Storefronts())


# -------------------------------------------------------------- selection


def test_stores_are_chosen_by_name_or_class_and_keep_their_order() -> None:
    fronts = Storefronts()

    result = run(fronts, stores=["Lidl", Consum, " bonàrea ", "lidl", Bonarea])

    assert [entry.store for entry in result.stores] == ["lidl", "consum", "bonarea"]
    assert {store for store, _ in fronts.requests} == {"lidl", "consum", "bonarea"}


@pytest.mark.parametrize("stores", ["consum", Consum])
def test_a_single_store_needs_no_list(stores: Any) -> None:
    result = run(Storefronts(), stores=stores)

    assert [entry.store for entry in result.stores] == ["consum"]


@pytest.mark.parametrize(
    ("stores", "message"),
    [
        (
            ["elcorteingles"],
            "unknown store 'elcorteingles'; expected one of mercadona, consum",
        ),
        ([BaseClient], "unknown store"),
        ([str], "unknown store"),
        ([3], "unknown store 3"),
        ([], "at least one store"),
        (3, "a collection of them"),
    ],
)
def test_an_unknown_selection_raises_before_any_request(
    stores: Any, message: str
) -> None:
    fronts = Storefronts()

    with pytest.raises(ConfigurationError, match=message):
        run(fronts, stores=stores)
    assert fronts.requests == []


# ---------------------------------------------------------------- options


def test_page_size_is_capped_at_each_store_maximum() -> None:
    fronts = Storefronts()

    result = run(fronts, page_size=500, postal_code="08013")

    sizes = {store: page.page_size for store, page in result.pages.items()}
    assert sizes["consum"] == 100
    assert sizes["plusfresc"] == 100
    assert sizes["bonpreu"] == 300
    assert sizes["bonarea"] == sizes["lidl"] == 500
    mercadona = [request for store, request in fronts.requests if store == "mercadona"]
    assert b"hitsPerPage=500" in mercadona[-1].content


def test_no_page_size_leaves_every_store_its_default() -> None:
    result = run(Storefronts())

    for client_type in ALL_CLIENTS[1:]:
        page = result.pages[client_type.store_name]
        assert page.page_size == client_type.default_page_size


def test_client_options_reach_every_client(opened: list[BaseClient]) -> None:
    fronts = Storefronts()
    policy = RetryPolicy(max_attempts=1)

    run(
        fronts,
        postal_code="28232",
        timeout=3.0,
        retry_policy=policy,
        user_agent="probe/1.0",
    )

    assert opened
    for client in opened:
        assert client.user_agent == "probe/1.0"
        assert client.min_request_interval == 0.0
        assert client._retry_policy is policy
        assert client._client.timeout == httpx.Timeout(3.0)
    assert {request.headers["User-Agent"] for _, request in fronts.requests} == {
        "probe/1.0"
    }


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"query": ""}, "query must be a non-empty string"),
        ({"query": "   "}, "query must be a non-empty string"),
        ({"query": 5}, "query must be a non-empty string"),
        ({"postal_code": "99001"}, "postal_code"),
        ({"page_size": 0}, "page_size must be a positive integer"),
        ({"page_size": True}, "page_size must be a positive integer"),
        ({"max_workers": 0}, "max_workers must be a positive integer"),
        ({"timeout": -1}, "timeout"),
        ({"min_request_interval": -1}, "min_request_interval"),
        ({"user_agent": " "}, "user_agent must be a non-empty string"),
    ],
)
def test_invalid_arguments_raise_before_any_request(
    options: dict[str, Any], message: str
) -> None:
    fronts = Storefronts()
    arguments = {"query": "llet", "transport": fronts.transport, **options}

    with pytest.raises(ConfigurationError, match=message):
        search_all(**arguments)
    assert fronts.requests == []


# ----------------------------------------------------------------- results


def test_products_pair_every_summary_with_its_store_in_order() -> None:
    result = run(Storefronts())

    expected = [
        (store, product)
        for store, page in result.pages.items()
        for product in page.products
    ]
    assert list(result.products) == expected
    assert [store for store, _ in result.products][:1] == ["consum"]


def test_the_result_serialises_to_json() -> None:
    result = run(Storefronts({"lidl": failing(400)}), postal_code="28232")

    document = json.loads(to_json(result))

    assert document["query"] == "llet"
    assert document["postal_code"] == "28232"
    stores = {entry["store"]: entry for entry in document["stores"]}
    assert list(stores) == STORE_NAMES
    assert stores["consum"]["status"] == "ok"
    assert stores["consum"]["store_id"] == "147"
    assert stores["consum"]["result"]["products"][0]["price"]["amount"] == "0.84"
    assert stores["plusfresc"]["status"] == "out_of_coverage"
    assert stores["plusfresc"]["result"] is None
    assert stores["lidl"]["status"] == "failed"
    assert stores["lidl"]["error_type"] == "TransportError"
    assert set(stores["lidl"]) == {
        "store",
        "status",
        "store_id",
        "result",
        "reason",
        "error_type",
    }
