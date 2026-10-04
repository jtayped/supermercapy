"""plumbing for the live contract tests, which call the real storefronts.

nothing here runs by default. ``pytest tests/live --live --no-cov`` runs the
whole tier, and ``pytest tests/live/test_lidl.py --live --no-cov`` one store.

the rules every live module follows:

- assert shape, never values. prices, stock, and names move every day.
- take ids from a live response, not from a fixture, wherever the store lets a
  test find one, so a delisted product does not read as a broken api.
- a ``BlockedError`` or ``ChallengedError`` is inconclusive, not a failure: the
  test is reported as skipped with the reason, whether the test body or a
  fixture met it, because bot protection says nothing about whether the
  contract still holds.
- every request goes through a ``RecordingTransport``, so a test can compare
  the raw json the client parsed with the committed fixture through
  ``tests.drift.assert_no_drift``. coercion hides a renamed key; the drift
  check does not.
- one client per module where the store warms up a session or rations its
  requests, so the tier stays polite.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from supermercapy import BlockedError, ChallengedError, RetryPolicy

LIVE_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_factor=0.5, max_delay=2.0)
LIVE_TIMEOUT = 20.0


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """report a refusal as a skip, whether the test or a fixture met it.

    a module-scoped client warms up inside its fixture, and pytest replays a
    failed module fixture's exception to every later test without running the
    fixture hooks again, so the outcome is rewritten here instead.
    """

    report = yield
    if call.excinfo is not None and call.excinfo.errisinstance(
        (BlockedError, ChallengedError)
    ):
        report.outcome = "skipped"
        report.longrepr = (
            str(item.path),
            item.location[1] or 0,
            "inconclusive, the store refused us: "
            f"{call.excinfo.typename}: {call.excinfo.value}",
        )
    return report


@dataclass(frozen=True)
class Exchange:
    """one request the client sent and the response it got back."""

    request: httpx.Request
    response: httpx.Response

    def json(self) -> Any:
        return json.loads(self.response.content)


class RecordingTransport(httpx.BaseTransport):
    """a real transport that keeps every exchange, body included."""

    def __init__(self) -> None:
        self._inner = httpx.HTTPTransport()
        self.exchanges: list[Exchange] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        response.read()
        self.exchanges.append(Exchange(request, response))
        return response

    def close(self) -> None:
        """leave the pool open: one recorder may outlive several clients."""

    def shutdown(self) -> None:
        self._inner.close()

    def find(
        self, path: str, *, host: str | None = None, method: str | None = None
    ) -> Iterator[Exchange]:
        """yield exchanges whose url path contains ``path``, newest first."""

        for exchange in reversed(self.exchanges):
            url = exchange.request.url
            if path not in url.path:
                continue
            if host is not None and url.host != host:
                continue
            if method is not None and exchange.request.method != method:
                continue
            yield exchange

    def last(
        self, path: str, *, host: str | None = None, method: str | None = None
    ) -> Exchange:
        """return the newest exchange whose url path contains ``path``."""

        for exchange in self.find(path, host=host, method=method):
            return exchange
        sent = "\n".join(
            f"{item.request.method} {item.request.url}" for item in self.exchanges
        )
        raise AssertionError(f"no request to {path!r} was recorded; sent:\n{sent}")

    def last_json(
        self, path: str, *, host: str | None = None, method: str | None = None
    ) -> Any:
        return self.last(path, host=host, method=method).json()


def live_options(recorder: RecordingTransport, **overrides: Any) -> dict[str, Any]:
    """return constructor keywords that route a client through ``recorder``."""

    return {
        "transport": recorder,
        "timeout": LIVE_TIMEOUT,
        "retry_policy": LIVE_RETRY_POLICY,
        **overrides,
    }


@pytest.fixture
def recorder() -> Iterator[RecordingTransport]:
    transport = RecordingTransport()
    yield transport
    transport.shutdown()


@pytest.fixture(scope="module")
def module_recorder() -> Iterator[RecordingTransport]:
    transport = RecordingTransport()
    yield transport
    transport.shutdown()
