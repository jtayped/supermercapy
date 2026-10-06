"""the transport logs through the standard library and never leaks secrets."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from supermercapy import (
    AuthenticationError,
    BaseClient,
    Category,
    ChallengedError,
    Product,
    RetryPolicy,
    SearchResult,
)
from supermercapy._core.client import ResponseVerdict

URL = "https://stub.test/api"


class Stub(BaseClient):
    store_name = "stub"

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.refresh = False

    def search_products(
        self, query: str, *, page_size: int | None = None, cursor: str | None = None
    ) -> SearchResult:
        raise NotImplementedError

    def get_product(self, product_id: str | int) -> Product:
        raise NotImplementedError

    def get_categories(self) -> tuple[Category, ...]:
        self._request_json("GET", URL, params={"q": "x"})
        return ()

    def get_category(self, category_id: str | int) -> Category:
        raise NotImplementedError

    def _prepare_request(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = "Bearer secret-token"

    def _on_auth_expired(self) -> bool:
        return self.refresh

    def _on_challenge(self, response: httpx.Response) -> bool:
        return self.refresh

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        if response.status_code == 401:
            return ResponseVerdict.AUTH_EXPIRED
        if response.status_code == 202:
            return ResponseVerdict.CHALLENGED
        return super()._classify(response)


class Warm(Stub):
    def _prime(self) -> None:
        self._request("GET", "https://stub.test/prime")


def build(handler: Any, **options: Any) -> Stub:
    kind = options.pop("kind", Stub)
    options.setdefault("retry_policy", RetryPolicy(backoff_factor=0, jitter_ratio=0))
    options.setdefault("user_agent", "secret-ua")
    return kind(transport=httpx.MockTransport(handler), **options)


def reply(status: int = 200) -> Any:
    return lambda request: httpx.Response(status, request=request, json={})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("supermercapy._core.client.time.sleep", lambda _: None)


def records(
    caplog: pytest.LogCaptureFixture, level: int | None = None
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.startswith("supermercapy")
        and (level is None or record.levelno == level)
    ]


def test_the_package_logger_has_a_null_handler() -> None:
    handlers = logging.getLogger("supermercapy").handlers
    assert any(isinstance(handler, logging.NullHandler) for handler in handlers)


def test_a_request_logs_one_debug_record(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="supermercapy")
    with build(reply()) as client:
        client.get_categories()
    debug = records(caplog, logging.DEBUG)
    assert len(debug) == 1
    assert debug[0].name == "supermercapy.stub"
    message = debug[0].getMessage()
    assert "GET" in message
    assert "https://stub.test/api?q=x" in message
    assert "200" in message
    assert " ms" in message
    assert not records(caplog, logging.INFO)


def test_a_retried_503_logs_the_retry_and_its_delay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="supermercapy")
    statuses = iter([503, 503, 200])
    with build(
        lambda request: httpx.Response(next(statuses), request=request, json={}),
        retry_policy=RetryPolicy(backoff_factor=0.5, jitter_ratio=0),
    ) as client:
        client.get_categories()
    info = [record.getMessage() for record in records(caplog, logging.INFO)]
    assert len(info) == 2
    assert "retry 2 of 3" in info[0]
    assert "RETRY" in info[0]
    assert "sleeping 0.50s" in info[0]
    assert "retry 3 of 3" in info[1]
    assert "sleeping 1.00s" in info[1]
    assert len(records(caplog, logging.DEBUG)) == 3


def test_a_connection_failure_retry_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="supermercapy")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, request=request, json={})

    with build(handler) as client:
        client.get_categories()
    info = [record.getMessage() for record in records(caplog, logging.INFO)]
    assert len(info) == 1
    assert "retry 2 of 3" in info[0]
    assert "ConnectError" in info[0]


def test_a_pacing_wait_is_logged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger="supermercapy")
    monkeypatch.setattr("supermercapy._core.client.time.monotonic", lambda: 10.0)
    with build(reply(), min_request_interval=2.0) as client:
        client.get_categories()
        assert not records(caplog)
        client.get_categories()
    info = [record.getMessage() for record in records(caplog, logging.INFO)]
    assert info == ["pacing: waiting 2.00s"]


def test_a_warm_up_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="supermercapy")
    with build(reply(), kind=Warm) as client:
        client.get_categories()
        client.get_categories()
    info = [record.getMessage() for record in records(caplog, logging.INFO)]
    assert info == ["running session warm-up"]


def test_a_credential_refresh_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="supermercapy")
    with build(reply(401)) as client, pytest.raises(AuthenticationError):
        client.get_categories()
    info = [record.getMessage() for record in records(caplog, logging.INFO)]
    assert len(info) == 1
    assert "refreshing credentials" in info[0]
    assert "401" in info[0]


def test_a_challenge_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="supermercapy")
    with build(reply(202)) as client, pytest.raises(ChallengedError):
        client.get_categories()
    info = [record.getMessage() for record in records(caplog, logging.INFO)]
    assert len(info) == 1
    assert "challenge" in info[0]


def test_failures_are_raised_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="supermercapy")
    with build(reply(500)) as client, pytest.raises(Exception, match="HTTP 500"):
        client.get_categories()
    assert not [r for r in records(caplog) if r.levelno >= logging.WARNING]


def test_secrets_never_reach_a_record(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="supermercapy")
    statuses = iter([503, 401, 202, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), request=request, json={})

    with build(handler) as client:
        client.refresh = True
        client.get_categories()
    assert records(caplog)
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        assert "secret-ua" not in text
        assert "secret-token" not in text
        assert "Authorization" not in text


def test_a_client_without_a_store_name_uses_the_package_logger() -> None:
    class Anonymous(Stub):
        store_name = ""

    client = Anonymous(transport=httpx.MockTransport(reply()))
    assert client._logger is logging.getLogger("supermercapy")
    client.close()
