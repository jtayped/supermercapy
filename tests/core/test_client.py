"""transport behaviour of the base client, driven through a stub store."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import supermercapy._core.client
from supermercapy import (
    AuthenticationError,
    BaseClient,
    BlockedError,
    Capability,
    Category,
    ChallengedError,
    ConfigurationError,
    InvalidResponseError,
    Language,
    NotFoundError,
    Photo,
    Product,
    ProductSummary,
    RateLimitError,
    RetryPolicy,
    SearchResult,
    TransportError,
    UnsupportedOperationError,
)
from supermercapy._core.client import ResponseVerdict

URL = "https://stub.test/api"


class Stub(BaseClient):
    store_name = "stub"
    capabilities = frozenset({Capability.CATALOG})
    supported_languages = frozenset({Language.SPANISH, Language.CATALAN})
    max_page_size = 10

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.primed = 0
        self.refreshed = 0
        self.challenge_retry = False
        self.refresh_result = True
        self.prime_ttl: float | None = None
        self.prepared: list[httpx.Request] = []

    def search_products(
        self, query: str, *, page_size: int | None = None, cursor: str | None = None
    ) -> SearchResult:
        size = self._resolve_page_size(page_size)
        data = self._request_json("GET", URL, params={"q": query, "c": cursor or ""})
        return SearchResult(
            query=query,
            products=tuple(
                ProductSummary(id=str(item), name=str(item))
                for item in data.get("ids", [])  # type: ignore[union-attr]
            ),
            page_size=size,
            next_cursor=data.get("next"),  # type: ignore[arg-type]
        )

    def get_product(self, product_id: str | int) -> Product:
        data = self._request_json("GET", f"{URL}/{product_id}")
        return Product(id=str(data["id"]), name=str(data["name"]))

    def get_categories(self) -> tuple[Category, ...]:
        self._request_json("GET", URL)
        return ()

    def get_category(self, category_id: str | int) -> Category:
        return Category(id=str(category_id), name="c")

    def iter_catalog(self) -> Iterator[ProductSummary]:
        yield ProductSummary(id="1", name="a")
        yield ProductSummary(id="1", name="a again")
        yield ProductSummary(id="2", name="b")

    def _prime(self) -> None:
        self.primed += 1
        self._request("GET", f"{URL}/prime")

    def _prime_ttl(self) -> float | None:
        return self.prime_ttl

    def _prepare_request(self, request: httpx.Request) -> None:
        self.prepared.append(request)
        request.headers["X-Prepared"] = str(len(self.prepared))

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        if response.status_code == 401:
            return ResponseVerdict.AUTH_EXPIRED
        if response.status_code == 202:
            return ResponseVerdict.CHALLENGED
        if response.status_code == 403:
            return ResponseVerdict.BLOCKED
        return super()._classify(response)

    def _on_auth_expired(self) -> bool:
        self.refreshed += 1
        return self.refresh_result

    def _on_challenge(self, response: httpx.Response) -> bool:
        return self.challenge_retry


def stub_for(handler: Any, **options: Any) -> Stub:
    options.setdefault("retry_policy", RetryPolicy(jitter_ratio=0))
    return Stub(transport=httpx.MockTransport(handler), **options)


def ok(request: httpx.Request, data: object = None) -> httpx.Response:
    return httpx.Response(200, request=request, json=data if data is not None else {})


def test_base_client_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BaseClient()  # type: ignore[abstract]


def test_priming_runs_once_before_the_first_request_and_may_reenter() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return ok(request)

    with stub_for(handler) as client:
        client.get_categories()
        client.get_categories()
    assert client.primed == 1
    assert paths == ["/api/prime", "/api", "/api"]


def test_priming_repeats_after_its_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("supermercapy._core.client.time.monotonic", lambda: clock[0])
    with stub_for(ok) as client:
        client.prime_ttl = 30.0
        client.get_categories()
        clock[0] += 10
        client.get_categories()
        assert client.primed == 1
        clock[0] += 25
        client.get_categories()
        assert client.primed == 2
        client._invalidate_priming()
        client.get_categories()
        assert client.primed == 3


def test_prepare_request_runs_on_every_attempt() -> None:
    statuses = iter((503, 200))
    headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        headers.append(request.headers["X-Prepared"])
        return httpx.Response(next(statuses), request=request, json={})

    with stub_for(handler, retry_policy=RetryPolicy(backoff_factor=0)) as client:
        client.get_categories()
    assert headers == ["2", "3"]


def test_auth_expired_refreshes_once_and_retries_without_consuming_attempts() -> None:
    statuses = iter((401, 200))
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/api/prime":
            return ok(request)
        attempts += 1
        return httpx.Response(next(statuses), request=request, json={})

    with stub_for(handler, retry_policy=RetryPolicy(max_attempts=1)) as client:
        client.get_categories()
    assert attempts == 2
    assert client.refreshed == 1


def test_auth_expired_twice_raises_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401 if request.url.path == "/api" else 200, request=request
        )

    with stub_for(handler) as client, pytest.raises(AuthenticationError) as raised:
        client.get_categories()
    assert raised.value.status_code == 401
    assert client.refreshed == 1


def test_failed_refresh_raises_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401 if request.url.path == "/api" else 200, request=request
        )

    with stub_for(handler) as client:
        client.refresh_result = False
        with pytest.raises(AuthenticationError, match="could not be refreshed"):
            client.get_categories()


def test_challenge_raises_immediately_by_default() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/api/prime":
            return ok(request)
        attempts += 1
        return httpx.Response(202, request=request, headers={"x-waf": "challenge"})

    with stub_for(handler) as client, pytest.raises(ChallengedError) as raised:
        client.get_categories()
    assert attempts == 1
    assert raised.value.status_code == 202
    assert raised.value.retry_after == raised.value.suggested_backoff == 1800.0
    assert not raised.value.permanent


def test_challenge_can_be_retried_by_a_store(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses = iter((202, 202, 200))
    delays: list[float] = []
    monkeypatch.setattr("supermercapy._core.client.time.sleep", delays.append)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        return httpx.Response(next(statuses), request=request, json={})

    with stub_for(handler) as client:
        client.challenge_retry = True
        client.get_categories()
    assert delays == [0.25, 0.5]


def test_challenge_retries_exhaust_into_challenged_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("supermercapy._core.client.time.sleep", lambda delay: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        return httpx.Response(202, request=request)

    with stub_for(handler, retry_policy=RetryPolicy(max_attempts=2)) as client:
        client.challenge_retry = True
        with pytest.raises(ChallengedError):
            client.get_categories()


def test_blocked_raises_blocked_error_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        return httpx.Response(403, request=request, headers={"Retry-After": "3"})

    with stub_for(handler) as client, pytest.raises(BlockedError) as raised:
        client.get_categories()
    assert raised.value.status_code == 403
    assert raised.value.retry_after == 3.0
    assert not isinstance(raised.value, ChallengedError)


def test_json_any_accepts_arrays_and_text_returns_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/list":
            return httpx.Response(200, request=request, json=[1, 2])
        if request.url.path == "/api/page":
            return httpx.Response(200, request=request, text="<p>hi</p>")
        return ok(request)

    with stub_for(handler) as client:
        assert client._request_json_any("GET", f"{URL}/list") == [1, 2]
        assert client._request_text("GET", f"{URL}/page") == "<p>hi</p>"
        with pytest.raises(InvalidResponseError, match="non-object"):
            client._request_json("GET", f"{URL}/list")


def test_not_found_and_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("supermercapy._core.client.time.sleep", lambda delay: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        if request.url.path == "/api/missing":
            return httpx.Response(404, request=request)
        return httpx.Response(502, request=request)

    with stub_for(handler, retry_policy=RetryPolicy(max_attempts=2)) as client:
        with pytest.raises(NotFoundError) as not_found:
            client.get_product("missing")
        assert not_found.value.status_code == 404
        with pytest.raises(TransportError) as failed:
            client.get_categories()
        assert failed.value.status_code == 502


def test_iter_search_follows_cursors_and_stops_on_repeats() -> None:
    pages = {"": {"ids": [1, 2], "next": "a"}, "a": {"ids": [3], "next": "a"}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        return ok(request, pages[request.url.params["c"]])

    with stub_for(handler) as client:
        ids = [item.id for item in client.iter_search("x")]
    assert ids == ["1", "2", "3"]


def test_get_catalog_deduplicates_iter_catalog() -> None:
    with stub_for(ok) as client:
        catalog = client.get_catalog()
    assert tuple(item.id for item in catalog) == ("1", "2")
    assert catalog[0].name == "a"


def test_page_size_defaults_and_bounds() -> None:
    with stub_for(ok) as client:
        assert client._resolve_page_size(None) == 24
        assert client._resolve_page_size(10) == 10
        for value in (0, 11, True, "5"):
            with pytest.raises(ConfigurationError, match="page_size"):
                client._resolve_page_size(value)  # type: ignore[arg-type]


def test_unsupported_defaults_raise_without_io() -> None:
    class Bare(Stub):
        capabilities = frozenset()

        def iter_catalog(self) -> Iterator[ProductSummary]:
            return super().iter_catalog()

    with Bare(transport=httpx.MockTransport(ok)) as client:
        with pytest.raises(UnsupportedOperationError, match="does not support catalog"):
            client.get_catalog()
        for call in (
            lambda: client.list_stores(),
            lambda: client.get_product_by_ean("1"),
            lambda: client.get_new_arrivals(),
            lambda: client.get_offers(),
            lambda: client.get_home(),
            lambda: Bare.from_postal_code("28001"),
            lambda: next(BaseClient.iter_catalog(client)),
        ):
            with pytest.raises(UnsupportedOperationError):
                call()


def test_user_agent_default_and_override() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return ok(request)

    with stub_for(handler) as client:
        client.get_categories()
    with stub_for(handler, user_agent="custom/1") as client:
        client.get_categories()
    assert seen[0].startswith("supermercapy/")
    assert seen[-1] == "custom/1"
    with pytest.raises(ConfigurationError, match="user_agent"):
        stub_for(handler, user_agent="  ")


def test_download_accepts_photo_or_url_and_validates(tmp_path: Any) -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, request=request, content=b"img")

    with stub_for(handler) as client:
        first = client.download(Photo(url="https://img.test/a.jpg"), tmp_path / "a")
        second = client.download("https://img.test/b.jpg", tmp_path / "b")
        assert first.read_bytes() == second.read_bytes() == b"img"
        with pytest.raises(ConfigurationError, match="source"):
            client.download("", tmp_path / "c")
        with pytest.raises(ConfigurationError, match="file path"):
            client.download("https://img.test/c.jpg", tmp_path)
    assert urls[-2:] == ["https://img.test/a.jpg", "https://img.test/b.jpg"]


def test_download_follows_redirects_and_accepts_any_type(tmp_path: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "stub.test":
            return ok(request)
        seen.append(request)
        if request.url.host == "img.test":
            return httpx.Response(
                302, request=request, headers={"Location": "https://cdn.test/a.jpg"}
            )
        return httpx.Response(200, request=request, content=b"img")

    with stub_for(handler) as client:
        result = client.download("https://img.test/a.jpg", tmp_path / "a")
    assert result.read_bytes() == b"img"
    assert [str(request.url) for request in seen] == [
        "https://img.test/a.jpg",
        "https://cdn.test/a.jpg",
    ]
    assert all(request.headers["Accept"] == "*/*" for request in seen)


def test_a_download_gets_the_mode_a_new_file_gets(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"img")

    previous = os.umask(0o027)
    try:
        with stub_for(handler) as client:
            result = client.download("https://img.test/a.jpg", tmp_path / "a.jpg")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(result.stat().st_mode) == 0o640


def test_a_download_steps_around_a_temporary_name_already_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = iter(["taken", "free"])
    monkeypatch.setattr(supermercapy._core.client, "token_hex", lambda _: next(names))
    taken = tmp_path / ".a.jpg.taken.tmp"
    taken.write_bytes(b"someone else's")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"img")

    with stub_for(handler) as client:
        result = client.download("https://img.test/a.jpg", tmp_path / "a.jpg")
    assert result.read_bytes() == b"img"
    assert taken.read_bytes() == b"someone else's"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".a.jpg.taken.tmp",
        "a.jpg",
    ]


def test_json_redirect_is_reported_as_a_move() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/moved":
            return httpx.Response(
                301, request=request, headers={"Location": "https://stub.test/v2"}
            )
        if request.url.path == "/api/bare":
            return httpx.Response(302, request=request)
        return ok(request)

    with stub_for(handler) as client:
        with pytest.raises(TransportError, match=r"redirected.*v2") as raised:
            client._request_json("GET", f"{URL}/moved")
        assert raised.value.status_code == 301
        with pytest.raises(TransportError, match="to nowhere"):
            client._request_json("GET", f"{URL}/bare")


@pytest.mark.parametrize("header", ["60", "nan"])
def test_rate_limit_sleeps_capped_but_reports_the_server_delay(
    monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        return httpx.Response(429, request=request, headers={"Retry-After": header})

    monkeypatch.setattr("supermercapy._core.client.time.sleep", delays.append)
    policy = RetryPolicy(max_attempts=2, max_delay=1.0, jitter_ratio=0)
    with (
        stub_for(handler, retry_policy=policy) as client,
        pytest.raises(RateLimitError) as raised,
    ):
        client.get_categories()
    if header == "nan":
        # an unusable header falls back to backoff and reports nothing
        assert delays == [0.25]
        assert raised.value.retry_after is None
    else:
        assert delays == [1.0]
        assert raised.value.retry_after == 60.0


def test_a_retry_after_date_without_a_zone_is_read_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "-0000" makes the standard library return a naive datetime
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        headers = {"Retry-After": "Wed, 21 Oct 2015 07:28:00 -0000"}
        return httpx.Response(429, request=request, headers=headers)

    monkeypatch.setattr("supermercapy._core.client.time.sleep", lambda _: None)
    policy = RetryPolicy(max_attempts=1)
    with (
        stub_for(handler, retry_policy=policy) as client,
        pytest.raises(RateLimitError) as raised,
    ):
        client.get_categories()
    # a date in the past asks for no wait at all
    assert raised.value.retry_after == 0.0


def test_pacing_does_not_wait_once_the_interval_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([100.0, 200.0, 300.0])
    delays: list[float] = []
    monkeypatch.setattr("supermercapy._core.client.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("supermercapy._core.client.time.sleep", delays.append)
    with stub_for(ok, min_request_interval=5.0) as client:
        client._wait_for_request_slot()
        client._wait_for_request_slot()
    assert delays == []


def test_an_http_client_that_refuses_the_timeout_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**_: Any) -> httpx.Client:
        raise TypeError("bad timeout")

    monkeypatch.setattr("supermercapy._core.client.httpx.Client", refuse)
    with pytest.raises(ConfigurationError, match="valid httpx timeout"):
        Stub()


def test_timeout_object_and_default_hooks_are_accepted() -> None:
    class Plain(Stub):
        def _on_auth_expired(self) -> bool:
            return BaseClient._on_auth_expired(self)

        def _on_challenge(self, response: httpx.Response) -> bool:
            return BaseClient._on_challenge(self, response)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/prime":
            return ok(request)
        return httpx.Response(401, request=request)

    timeout = httpx.Timeout(5.0)
    with (
        Plain(transport=httpx.MockTransport(handler), timeout=timeout) as client,
        pytest.raises(AuthenticationError, match="could not be refreshed"),
    ):
        client.get_categories()
    assert not Plain._on_challenge(client, httpx.Response(202))
