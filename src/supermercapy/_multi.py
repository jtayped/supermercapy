"""ask several stores the same question at once: one thread and one client each.

nothing here talks to a storefront directly. every store is asked through its
own client, so retries, pacing, and errors behave exactly as they do when the
store is asked alone.
"""

from __future__ import annotations

import inspect
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, TypeVar

import httpx

from ._core.capabilities import Capability
from ._core.client import (
    BaseClient,
    RetryPolicy,
    validate_postal_code,
    validate_request_interval,
    validate_timeout,
)
from ._core.exceptions import (
    ConfigurationError,
    OutOfCoverageError,
    SupermercapyError,
)
from ._core.models import ProductSummary, SearchResult

__all__ = ["SearchAllResult", "SearchStatus", "StoreSearch", "search_all"]

T = TypeVar("T")

StoreSelection = str | type[BaseClient] | Iterable[str | type[BaseClient]]


class SearchStatus(StrEnum):
    """how one store's part of a :func:`search_all` call ended."""

    OK = "ok"
    """the store answered; ``result`` holds its page."""
    SKIPPED = "skipped"
    """the store was not asked, because it cannot be built without a binding."""
    OUT_OF_COVERAGE = "out_of_coverage"
    """the store does not serve the postal code."""
    FAILED = "failed"
    """the store raised a :class:`SupermercapyError`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreSearch:
    """one store's part of a :func:`search_all` call.

    ``store_id`` is the binding the search ran against, when the store has
    one. ``reason`` says why a store has no result: the exception message for
    a failure, or why it was skipped. ``error_type`` names the exception class,
    so the record stays plain data that :func:`to_json` can write.
    """

    store: str
    status: SearchStatus
    store_id: str | None = None
    result: SearchResult | None = None
    reason: str | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchAllResult:
    """what every store asked by :func:`search_all` answered, in order."""

    query: str
    postal_code: str | None
    stores: tuple[StoreSearch, ...]

    @property
    def pages(self) -> dict[str, SearchResult]:
        """the page each store that answered returned, keyed on store name."""

        return {
            entry.store: entry.result
            for entry in self.stores
            if entry.result is not None
        }

    @property
    def products(self) -> tuple[tuple[str, ProductSummary], ...]:
        """every summary found, paired with its store name, store by store."""

        return tuple(
            (entry.store, product)
            for entry in self.stores
            if entry.result is not None
            for product in entry.result.products
        )

    @property
    def failed(self) -> tuple[StoreSearch, ...]:
        """the stores that raised; skipped and out-of-coverage stores are not."""

        return tuple(
            entry for entry in self.stores if entry.status is SearchStatus.FAILED
        )


def search_all(
    query: str,
    *,
    stores: str | type[BaseClient] | Iterable[str | type[BaseClient]] | None = None,
    postal_code: str | None = None,
    page_size: int | None = None,
    max_workers: int | None = None,
    timeout: float | httpx.Timeout = 10.0,
    retry_policy: RetryPolicy | None = None,
    min_request_interval: float | None = None,
    user_agent: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> SearchAllResult:
    """search several stores in parallel and report each one separately.

    every store gets its own client on its own thread, and every client is
    closed before this returns. with a ``postal_code``, the stores that declare
    ``Capability.POSTAL_CODE`` bind through ``from_postal_code()``, which costs
    requests of its own; the rest are built unbound. without one, a store that
    cannot be built unbound, such as mercadona, is skipped. a store that does
    not serve the postal code reports ``OUT_OF_COVERAGE``, and one that raises
    any other :class:`SupermercapyError` reports ``FAILED``; neither hides what
    the other stores found. any other exception is a bug and propagates.

    ``stores`` takes store names, client classes, or a mix, and the result
    keeps their order: ``ALL_CLIENTS`` order by default, never the order the
    stores happened to finish in. ``page_size`` is capped at each store's
    ``max_page_size``, and ``None`` leaves every store its own default.
    ``max_workers`` defaults to one thread per store. the remaining keywords
    reach every client's constructor; ``transport`` is shared by all of them
    and stays open, because the caller who made it owns it.

    invalid arguments raise :class:`ConfigurationError` before any request.
    """

    if not isinstance(query, str) or not query.strip():
        raise ConfigurationError("query must be a non-empty string")
    client_types = resolve_stores(stores)
    code = None if postal_code is None else validate_postal_code(postal_code)
    size = _positive(page_size, "page_size")
    workers = _positive(max_workers, "max_workers")
    options = client_options(
        timeout=timeout,
        retry_policy=retry_policy,
        min_request_interval=min_request_interval,
        user_agent=user_agent,
        transport=transport,
    )

    def search(client: BaseClient) -> SearchResult:
        cap = client.max_page_size
        fitted = size if size is None or cap is None else min(size, cap)
        return client.search_products(query, page_size=fitted)

    outcomes = run_each(
        client_types, search, postal_code=code, options=options, max_workers=workers
    )
    return SearchAllResult(
        query=query,
        postal_code=code,
        stores=tuple(_store_search(outcome) for outcome in outcomes),
    )


# ------------------------------------------------------- shared with the cli


@dataclass(frozen=True, slots=True)
class Outcome(Generic[T]):
    """what asking one store ended with: a value, an error, or a skip."""

    client_type: type[BaseClient]
    value: T | None = None
    store_id: str | None = None
    error: SupermercapyError | None = None
    skipped: str | None = None

    @property
    def store(self) -> str:
        return self.client_type.store_name


def resolve_stores(stores: StoreSelection | None) -> tuple[type[BaseClient], ...]:
    """return the client classes a selection names, once each, in its order."""

    # the package defines ALL_CLIENTS after importing this module
    from . import ALL_CLIENTS

    if stores is None:
        return ALL_CLIENTS
    items = (stores,) if isinstance(stores, (str, type)) else stores
    if not isinstance(items, Iterable):
        raise ConfigurationError(
            "stores must be a store name, a client class, or a collection of them"
        )
    chosen = {resolve_store(item): None for item in items}
    if not chosen:
        raise ConfigurationError("stores must name at least one store")
    return tuple(chosen)


def resolve_store(item: object) -> type[BaseClient]:
    """return the client class a store name or class stands for."""

    from . import ALL_CLIENTS

    if (
        isinstance(item, type)
        and issubclass(item, BaseClient)
        and not inspect.isabstract(item)
    ):
        return item
    if isinstance(item, str):
        wanted = _plain(item)
        for client_type in ALL_CLIENTS:
            if client_type.store_name == wanted:
                return client_type
    names = ", ".join(client_type.store_name for client_type in ALL_CLIENTS)
    raise ConfigurationError(f"unknown store {item!r}; expected one of {names}")


def binding_parameter(client_type: type[BaseClient]) -> inspect.Parameter | None:
    """return the constructor argument a store binds through, if it has one."""

    for parameter in inspect.signature(client_type).parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return parameter
    return None


def skip_reason(client_type: type[BaseClient], postal_code: str | None) -> str | None:
    """say why a store cannot be built here, or return ``None`` when it can."""

    binding = binding_parameter(client_type)
    if binding is None or binding.default is not inspect.Parameter.empty:
        return None
    label = binding.name.replace("_", " ")
    if not client_type.supports(Capability.POSTAL_CODE):
        return f"needs a {label}, which a postal code cannot supply"
    if postal_code is None:
        return f"needs a postal code to bind a {label}"
    return None


def client_options(
    *,
    timeout: float | httpx.Timeout = 10.0,
    retry_policy: RetryPolicy | None = None,
    min_request_interval: float | None = None,
    user_agent: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """validate options every client will share, once rather than per store."""

    validate_timeout(timeout)
    if min_request_interval is not None:
        validate_request_interval(min_request_interval)
    if user_agent is not None and (
        not isinstance(user_agent, str) or not user_agent.strip()
    ):
        raise ConfigurationError("user_agent must be a non-empty string")
    return {
        "timeout": timeout,
        "retry_policy": retry_policy,
        "min_request_interval": min_request_interval,
        "user_agent": user_agent,
        "transport": None if transport is None else _SharedTransport(transport),
    }


def open_client(
    client_type: type[BaseClient],
    postal_code: str | None,
    options: Mapping[str, Any],
) -> BaseClient:
    """build a client, bound to ``postal_code`` when the store can resolve one."""

    if postal_code is not None and client_type.supports(Capability.POSTAL_CODE):
        return client_type.from_postal_code(postal_code, **options)
    return client_type(**options)


def run_each(
    client_types: Sequence[type[BaseClient]],
    call: Callable[[BaseClient], T],
    *,
    postal_code: str | None,
    options: Mapping[str, Any],
    max_workers: int | None = None,
) -> tuple[Outcome[T], ...]:
    """call ``call`` on a fresh client per store, in parallel, in store order."""

    workers = max_workers or len(client_types)
    with ThreadPoolExecutor(workers, thread_name_prefix="supermercapy") as executor:
        futures = [
            executor.submit(_run_one, client_type, call, postal_code, options)
            for client_type in client_types
        ]
        try:
            return tuple(future.result() for future in futures)
        except BaseException:
            # a bug in one store: start no others, let the running ones end
            for future in futures:
                future.cancel()
            raise


# ------------------------------------------------------------------- helpers


class _SharedTransport(httpx.BaseTransport):
    """lend one caller-owned transport to several clients without closing it."""

    def __init__(self, transport: httpx.BaseTransport) -> None:
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._transport.handle_request(request)


def _run_one(
    client_type: type[BaseClient],
    call: Callable[[BaseClient], T],
    postal_code: str | None,
    options: Mapping[str, Any],
) -> Outcome[T]:
    reason = skip_reason(client_type, postal_code)
    if reason is not None:
        return Outcome(client_type, skipped=reason)
    store_id: str | None = None
    try:
        with open_client(client_type, postal_code, options) as client:
            store_id = client.store_id
            value = call(client)
    except SupermercapyError as error:
        return Outcome(client_type, store_id=store_id, error=error)
    return Outcome(client_type, value=value, store_id=store_id)


def _store_search(outcome: Outcome[SearchResult]) -> StoreSearch:
    if outcome.skipped is not None:
        return StoreSearch(
            store=outcome.store, status=SearchStatus.SKIPPED, reason=outcome.skipped
        )
    if outcome.error is not None:
        return StoreSearch(
            store=outcome.store,
            status=(
                SearchStatus.OUT_OF_COVERAGE
                if isinstance(outcome.error, OutOfCoverageError)
                else SearchStatus.FAILED
            ),
            store_id=outcome.store_id,
            reason=str(outcome.error) or type(outcome.error).__name__,
            error_type=type(outcome.error).__name__,
        )
    return StoreSearch(
        store=outcome.store,
        status=SearchStatus.OK,
        store_id=outcome.store_id,
        result=outcome.value,
    )


def _positive(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigurationError(f"{label} must be a positive integer")
    return value


def _plain(name: str) -> str:
    """fold case and accents, so ``"Bonàrea"`` finds ``"bonarea"``."""

    decomposed = unicodedata.normalize("NFKD", name.strip().casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))
