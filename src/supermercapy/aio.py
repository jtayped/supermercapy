"""asyncio access to the store clients, through worker threads."""

from __future__ import annotations

import asyncio
import contextvars
import os
from collections.abc import AsyncGenerator, Callable, Generator, Iterable
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Generic, ParamSpec, Self, TypeVar, overload

from ._core.client import BaseClient
from ._core.exceptions import ConfigurationError
from ._core.models import (
    Category,
    HomeSection,
    Photo,
    Product,
    ProductSummary,
    SearchResult,
    Store,
)

__all__ = ["AsyncClient"]

ClientT = TypeVar("ClientT", bound=BaseClient)
ResultT = TypeVar("ResultT")
ItemT = TypeVar("ItemT")
P = ParamSpec("P")


class _End:
    """the value a step hands back once the wrapped iterator is exhausted."""


_END = _End()


class AsyncClient(Generic[ClientT]):
    """run one store client from asyncio without blocking the event loop.

    every call runs the synchronous client in a worker thread of the loop's
    default executor, so retries, pacing, warm-ups and errors behave exactly
    as they do without asyncio. calls on one ``AsyncClient`` run one at a
    time, first come first served, because a client keeps per-session state
    such as guest tokens, csrf tokens and warm-up clocks that two threads
    could interleave; concurrency comes from awaiting several clients at once.

    pass a store client, or a callable that builds one and its arguments,
    such as ``AsyncClient(Mercadona.from_postal_code, "28001")``. a callable
    runs in a worker thread when the ``async with`` block starts or, without
    one, on the first call, so neither the connection pool nor a postcode
    lookup blocks the loop. leaving the block or awaiting :meth:`aclose`
    closes the client.
    """

    @overload
    def __init__(self, client: ClientT, /) -> None: ...

    @overload
    def __init__(
        self, factory: Callable[P, ClientT], /, *args: P.args, **kwargs: P.kwargs
    ) -> None: ...

    def __init__(
        self,
        source: ClientT | Callable[..., ClientT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._client: ClientT | None = None
        self._factory: Callable[[], ClientT]
        if isinstance(source, BaseClient):
            if args or kwargs:
                raise ConfigurationError(
                    "arguments are only accepted with a callable that builds a client"
                )
            self._client = source
            # never called: the client already exists
            self._factory = lambda: source
        elif callable(source):
            self._factory = partial(source, *args, **kwargs)
        else:
            raise ConfigurationError(
                "AsyncClient needs a store client or a callable that returns one"
            )
        self._closed = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    @property
    def client(self) -> ClientT:
        """return the wrapped synchronous client.

        read its properties, such as ``store_id``, here. calling one of its
        request methods directly would block the event loop and skip the
        one-call-at-a-time rule; use :meth:`run` instead.
        """

        if self._client is None:
            raise ConfigurationError(
                "the client is not built yet; enter the async with block or "
                "await a call first"
            )
        return self._client

    async def aclose(self) -> None:
        """close the wrapped client once the calls already queued have run."""

        await self._submit(self._close)

    async def __aenter__(self) -> Self:
        await self._submit(self._open)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------- escape hatches

    async def run(self, call: Callable[[ClientT], ResultT], /) -> ResultT:
        """run ``call`` with the client in a worker thread and return its result.

        this reaches everything the awaitable methods do not: store extensions,
        and the extra keywords a store adds to a standard method, with the
        store's own return types, as in
        ``await lidl.run(lambda client: client.get_leaflets())``.
        """

        return await self._submit(lambda: call(self._open()))

    async def iterate(
        self, call: Callable[[ClientT], Iterable[ItemT]], /
    ) -> AsyncGenerator[ItemT, None]:
        """yield what an iterator-returning method of the client yields.

        the method and each step of its iterator run in a worker thread, so
        a page is fetched only when the loop asks for its first item, and
        leaving the loop early costs no request. other calls on this client
        may run between two steps.
        """

        iterator = await self.run(lambda client: iter(call(client)))
        try:
            while True:
                item = await self._submit(partial(next, iterator, _END))
                if isinstance(item, _End):
                    return
                yield item
        finally:
            if isinstance(iterator, Generator):
                await self._submit(iterator.close)

    # ------------------------------------------------------- standard interface

    async def search_products(
        self, query: str, *, page_size: int | None = None, cursor: str | None = None
    ) -> SearchResult:
        """await :meth:`BaseClient.search_products`."""

        return await self.run(
            lambda client: client.search_products(
                query, page_size=page_size, cursor=cursor
            )
        )

    async def get_product(self, product_id: str | int) -> Product:
        """await :meth:`BaseClient.get_product`."""

        return await self.run(lambda client: client.get_product(product_id))

    async def get_categories(self) -> tuple[Category, ...]:
        """await :meth:`BaseClient.get_categories`."""

        return await self.run(lambda client: client.get_categories())

    async def get_category(self, category_id: str | int) -> Category:
        """await :meth:`BaseClient.get_category`."""

        return await self.run(lambda client: client.get_category(category_id))

    def iter_search(
        self, query: str, *, page_size: int | None = None
    ) -> AsyncGenerator[ProductSummary, None]:
        """iterate :meth:`BaseClient.iter_search` without blocking the loop."""

        return self.iterate(
            lambda client: client.iter_search(query, page_size=page_size)
        )

    def iter_catalog(self) -> AsyncGenerator[ProductSummary, None]:
        """iterate :meth:`BaseClient.iter_catalog` without blocking the loop."""

        return self.iterate(lambda client: client.iter_catalog())

    async def get_catalog(self) -> tuple[ProductSummary, ...]:
        """await :meth:`BaseClient.get_catalog`, the whole walk in one call."""

        return await self.run(lambda client: client.get_catalog())

    async def list_stores(self, postal_code: str | None = None) -> tuple[Store, ...]:
        """await :meth:`BaseClient.list_stores`."""

        return await self.run(lambda client: client.list_stores(postal_code))

    async def get_product_by_ean(self, ean: str) -> Product:
        """await :meth:`BaseClient.get_product_by_ean`."""

        return await self.run(lambda client: client.get_product_by_ean(ean))

    async def get_new_arrivals(self) -> tuple[ProductSummary, ...]:
        """await :meth:`BaseClient.get_new_arrivals`."""

        return await self.run(lambda client: client.get_new_arrivals())

    async def get_offers(self) -> tuple[ProductSummary, ...]:
        """await :meth:`BaseClient.get_offers`."""

        return await self.run(lambda client: client.get_offers())

    async def get_home(self) -> tuple[HomeSection, ...]:
        """await :meth:`BaseClient.get_home`."""

        return await self.run(lambda client: client.get_home())

    async def download(
        self, source: Photo | str, destination: str | os.PathLike[str]
    ) -> Path:
        """await :meth:`BaseClient.download`."""

        return await self.run(lambda client: client.download(source, destination))

    # ---------------------------------------------------------------- internals

    def _open(self) -> ClientT:
        # runs in a worker thread while the lock is held
        if self._closed:
            raise ConfigurationError("the async client is closed")
        if self._client is None:
            client = self._factory()
            if not isinstance(client, BaseClient):
                raise ConfigurationError(
                    "AsyncClient's callable must return a store client"
                )
            self._client = client
        return self._client

    def _close(self) -> None:
        self._closed = True
        if self._client is not None:
            self._client.close()

    async def _submit(self, work: Callable[[], ResultT]) -> ResultT:
        """run ``work`` in a worker thread once every earlier call has finished.

        the caller awaits a future of its own rather than the thread's, and
        the thread's future releases the lock: a cancelled caller cannot stop
        a thread, so the client stays reserved until the call really ends,
        and what it returns or raises is then dropped, as
        ``asyncio.to_thread`` drops it.
        """

        await self._lock.acquire()
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(
                None, partial(contextvars.copy_context().run, work)
            )
        except BaseException:
            self._lock.release()
            raise
        waiter: asyncio.Future[ResultT] = loop.create_future()
        future.add_done_callback(partial(self._settle, waiter))
        return await waiter

    def _settle(
        self, waiter: asyncio.Future[ResultT], future: asyncio.Future[ResultT]
    ) -> None:
        self._lock.release()
        try:
            result = future.result()
        except BaseException as error:
            if not waiter.cancelled():
                waiter.set_exception(error)
            return
        if not waiter.cancelled():
            waiter.set_result(result)
