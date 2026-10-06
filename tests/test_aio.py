"""the asyncio wrapper, driven through the canned storefronts.

pytest has no asyncio plugin here, so every test drives its coroutine with
``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time
from collections.abc import Iterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import httpx
import pytest

from supermercapy import (
    AsyncClient,
    BaseClient,
    Bonarea,
    CacheTransport,
    Carrefour,
    ConfigurationError,
    Lidl,
    Mercadona,
    NotFoundError,
    OutOfCoverageError,
    Plusfresc,
    UnsupportedOperationError,
)
from tests.harness import HARNESSES, Handler, Harness

WAIT = 5.0


class Recorder:
    """a canned storefront that remembers every request and its thread."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.threads: set[int] = set()
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            self.threads.add(threading.get_ident())
        return self.handler(request)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def transport(handler: Handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("harness", HARNESSES.values(), ids=list(HARNESSES))
def test_the_required_methods_answer_what_the_client_answers(harness: Harness) -> None:
    with harness.client(min_request_interval=0.0) as client:
        expected = (
            client.search_products(harness.query),
            client.get_product(harness.product_id),
            client.get_categories(),
            client.get_category(harness.category_id),
            list(client.iter_search(harness.query)),
        )

    async def main() -> tuple[object, ...]:
        async with AsyncClient(
            harness.make,
            transport=transport(harness.handler),
            min_request_interval=0.0,
        ) as client:
            return (
                await client.search_products(harness.query),
                await client.get_product(harness.product_id),
                await client.get_categories(),
                await client.get_category(harness.category_id),
                [item async for item in client.iter_search(harness.query)],
            )

    assert asyncio.run(main()) == expected


def test_the_optional_methods_answer_what_the_client_answers(tmp_path: Path) -> None:
    def image_or(handler: Handler) -> Handler:
        def serve(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".jpg"):
                return httpx.Response(200, request=request, content=b"jpeg bytes")
            return handler(request)

        return serve

    mercadona = HARNESSES["mercadona"]
    plusfresc = HARNESSES["plusfresc"]
    carrefour = HARNESSES["carrefour"]
    with mercadona.client() as client:
        home, arrivals = client.get_home(), client.get_new_arrivals()
    with plusfresc.client(min_request_interval=0.0) as client:
        offers, stores = client.get_offers(), client.list_stores()
        catalog, walked = client.get_catalog(), list(client.iter_catalog())
    with carrefour.client() as client:
        by_ean = client.get_product_by_ean("8431876011937")

    async def main() -> None:
        async with AsyncClient(
            mercadona.make, transport=transport(mercadona.handler)
        ) as client:
            assert await client.get_home() == home
            assert await client.get_new_arrivals() == arrivals
        async with AsyncClient(
            plusfresc.make,
            transport=transport(plusfresc.handler),
            min_request_interval=0.0,
        ) as client:
            assert await client.get_offers() == offers
            assert await client.list_stores() == stores
            assert await client.get_catalog() == catalog
            assert [item async for item in client.iter_catalog()] == walked
        async with AsyncClient(
            carrefour.make, transport=transport(image_or(carrefour.handler))
        ) as client:
            assert await client.get_product_by_ean("8431876011937") == by_ean
            target = tmp_path / "photo.jpg"
            url = "https://static.carrefour.es/photo.jpg"
            assert await client.download(url, target) == target
            assert target.read_bytes() == b"jpeg bytes"

    asyncio.run(main())


def test_an_undeclared_operation_raises_from_the_await() -> None:
    bonarea = HARNESSES["bonarea"]

    async def main() -> None:
        async with AsyncClient(
            bonarea.make, transport=transport(bonarea.handler)
        ) as client:
            with pytest.raises(UnsupportedOperationError):
                await client.get_offers()
            with pytest.raises(NotFoundError):
                await client.get_product(bonarea.missing_product_id)

    asyncio.run(main())


def test_run_reaches_extensions_with_the_store_types() -> None:
    lidl = HARNESSES["lidl"]
    with Lidl(26, transport=transport(lidl.handler)) as client:
        leaflets = client.get_leaflets()
        ids = list(client.iter_catalog_ids())

    async def main() -> None:
        async with AsyncClient(Lidl, 26, transport=transport(lidl.handler)) as client:
            assert await client.run(lambda lidl: lidl.get_leaflets()) == leaflets
            assert [
                item
                async for item in client.iterate(lambda lidl: lidl.iter_catalog_ids())
            ] == ids

    asyncio.run(main())


def test_construction_and_binding_run_in_a_worker_thread() -> None:
    recorder = Recorder(HARNESSES["mercadona"].handler)
    released = threading.Event()
    built_on: list[int] = []

    def build(postal_code: str) -> Mercadona:
        built_on.append(threading.get_ident())
        # the loop sets the event while this thread waits; a blocked loop
        # never would
        assert released.wait(WAIT), "the event loop was blocked"
        return Mercadona.from_postal_code(postal_code, transport=transport(recorder))

    async def main() -> str | None:
        client = AsyncClient(build, "28001")
        opening = asyncio.ensure_future(client.__aenter__())
        await asyncio.sleep(0.01)
        released.set()
        await opening
        try:
            return client.client.store_id
        finally:
            await client.aclose()

    assert asyncio.run(main()) == "mad3"
    assert built_on and built_on[0] != threading.get_ident()
    assert recorder.paths() == ["/api/postal-codes/actions/change-pc/"]
    assert threading.get_ident() not in recorder.threads


def test_a_request_in_flight_leaves_the_loop_free() -> None:
    harness = HARNESSES["consum"]
    released = threading.Event()

    def slow(request: httpx.Request) -> httpx.Response:
        assert released.wait(WAIT), "the event loop was blocked"
        return harness.handler(request)

    async def main() -> None:
        async with AsyncClient(harness.make, transport=transport(slow)) as client:
            call = asyncio.ensure_future(client.get_categories())
            await asyncio.sleep(0.01)
            released.set()
            assert await call

    asyncio.run(main())


def test_iteration_fetches_the_next_page_off_the_loop_and_only_when_asked() -> None:
    harness = HARNESSES["carrefour"]
    pages = threading.Event()
    recorder = Recorder(harness.handler)

    def gated(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("start", "0") != "0":
            assert pages.wait(WAIT), "the event loop was blocked between pages"
        return recorder(request)

    async def main() -> list[str]:
        async with AsyncClient(harness.make, transport=transport(gated)) as client:
            seen: list[str] = []
            async with aclosing(client.iter_search("leche")) as items:
                async for item in items:
                    seen.append(item.id)
                    if len(seen) == 2:
                        assert len(recorder.requests) == 1
                        # the third item needs the second page
                        asyncio.get_running_loop().call_later(0.01, pages.set)
            return seen

    assert len(asyncio.run(main())) == 3
    assert len(recorder.requests) == 2


def test_leaving_an_iteration_early_costs_no_request() -> None:
    harness = HARNESSES["carrefour"]
    recorder = Recorder(harness.handler)

    async def main() -> None:
        async with (
            AsyncClient(harness.make, transport=transport(recorder)) as client,
            aclosing(client.iter_search("leche")) as items,
        ):
            async for _ in items:
                break

    asyncio.run(main())
    assert len(recorder.requests) == 1


def test_other_calls_run_between_the_steps_of_an_iteration() -> None:
    harness = HARNESSES["mercadona"]

    async def main() -> int:
        async with AsyncClient(
            harness.make, transport=transport(harness.handler)
        ) as client:
            found = 0
            async for _ in client.iter_search(harness.query):
                # holding the client across the whole walk would deadlock here
                await asyncio.wait_for(client.get_product(harness.product_id), WAIT)
                found += 1
            return found

    assert asyncio.run(main()) == 4


def test_leaving_an_iteration_closes_the_generator_in_a_worker_thread() -> None:
    harness = HARNESSES["mercadona"]
    closed_on: list[int] = []

    def numbers(client: BaseClient) -> Iterator[int]:
        try:
            yield 1
            yield 2
        finally:
            closed_on.append(threading.get_ident())

    async def main() -> None:
        async with AsyncClient(
            harness.make, transport=transport(harness.handler)
        ) as client:
            async with aclosing(client.iterate(numbers)) as items:
                async for _ in items:
                    break
            assert closed_on
            assert [item async for item in client.iterate(numbers)] == [1, 2]

    asyncio.run(main())
    assert len(closed_on) == 2
    assert threading.get_ident() not in closed_on


def test_calls_on_one_client_never_overlap() -> None:
    harness = HARNESSES["plusfresc"]
    state = {"in_flight": 0, "most": 0}
    lock = threading.Lock()

    def tracked(request: httpx.Request) -> httpx.Response:
        with lock:
            state["in_flight"] += 1
            state["most"] = max(state["most"], state["in_flight"])
        time.sleep(0.01)
        try:
            return harness.handler(request)
        finally:
            with lock:
                state["in_flight"] -= 1

    recorder = Recorder(tracked)

    async def main() -> list[object]:
        async with AsyncClient(
            Plusfresc, 12, transport=transport(recorder), min_request_interval=0
        ) as client:
            return await asyncio.gather(
                *(client.search_products(harness.query) for _ in range(6)),
                client.get_product(harness.product_id),
                client.get_categories(),
            )

    results = asyncio.run(main())
    assert state["most"] == 1
    assert all(result == results[0] for result in results[:6])
    # six searches and a product page share one guest token: on a client
    # shared by threads, each could have seen no token and minted its own
    assert recorder.paths().count("/api/loginGuest/12") == 1
    assert len(recorder.requests) == 9


def test_separate_clients_run_at_the_same_time() -> None:
    together = threading.Barrier(2, timeout=WAIT)

    def meet(harness: Harness) -> Handler:
        def handler(request: httpx.Request) -> httpx.Response:
            # passes only while a request of the other client is in flight
            together.wait()
            return harness.handler(request)

        return handler

    lidl, consum = HARNESSES["lidl"], HARNESSES["consum"]

    async def main() -> None:
        async with (
            AsyncClient(lidl.make, transport=transport(meet(lidl))) as first,
            AsyncClient(consum.make, transport=transport(meet(consum))) as second,
        ):
            await asyncio.gather(first.get_categories(), second.get_categories())

    asyncio.run(main())


def test_a_cancelled_call_keeps_the_client_until_its_thread_ends() -> None:
    harness = HARNESSES["mercadona"]
    released = threading.Event()
    events: list[str] = []
    errors: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path
        events.append(f"start {name}")
        if name.startswith("/api/products/"):
            assert released.wait(WAIT)
        events.append(f"end {name}")
        return harness.handler(request)

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: errors.append(context)
        )
        async with AsyncClient(harness.make, transport=transport(handler)) as client:
            for product_id in ("missing", harness.product_id):
                released.clear()
                abandoned = asyncio.ensure_future(client.get_product(product_id))
                await asyncio.sleep(0.01)
                abandoned.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await abandoned
                follower = asyncio.ensure_future(client.get_categories())
                await asyncio.sleep(0.01)
                assert events[-1] == f"start /api/products/{product_id}/"
                released.set()
                assert await follower
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(main())
    assert events == [
        "start /api/products/missing/",
        "end /api/products/missing/",
        "start /api/categories/",
        "end /api/categories/",
        f"start /api/products/{harness.product_id}/",
        f"end /api/products/{harness.product_id}/",
        "start /api/categories/",
        "end /api/categories/",
    ]
    # the first abandoned call raised NotFoundError in its thread, unobserved,
    # and that is not reported as an error nobody retrieved
    assert errors == []


def test_a_callable_builds_the_client_on_the_first_call_without_async_with() -> None:
    harness = HARNESSES["mercadona"]
    recorder = Recorder(harness.handler)

    async def main() -> None:
        client = AsyncClient(Mercadona, "mad3", transport=transport(recorder))
        with pytest.raises(ConfigurationError, match="not built yet"):
            _ = client.client
        assert await client.get_categories()
        sync_client = client.client
        assert not sync_client.is_closed
        await client.aclose()
        await client.aclose()
        assert sync_client.is_closed
        with pytest.raises(ConfigurationError, match="closed"):
            await client.get_categories()
        with pytest.raises(ConfigurationError, match="closed"):
            await client.__aenter__()

    asyncio.run(main())
    assert recorder.paths() == ["/api/categories/"]


def test_a_client_instance_is_wrapped_and_closed_with_the_wrapper() -> None:
    harness = HARNESSES["mercadona"]
    sync_client = harness.client()

    async def main() -> None:
        client = AsyncClient(sync_client)
        assert client.client is sync_client
        async with client:
            assert await client.get_product(harness.product_id)

    asyncio.run(main())
    assert sync_client.is_closed


def test_closing_a_client_that_was_never_built_builds_nothing() -> None:
    built: list[Bonarea] = []

    def build() -> Bonarea:
        built.append(Bonarea())
        return built[-1]

    async def main() -> None:
        client = AsyncClient(build)
        await client.aclose()
        with pytest.raises(ConfigurationError, match="not built yet"):
            _ = client.client

    asyncio.run(main())
    assert built == []


def test_a_binding_that_fails_raises_from_async_with() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, request=request, json=0)

    async def main() -> AsyncClient[Plusfresc]:
        client = AsyncClient(
            Plusfresc.from_postal_code, "28001", transport=transport(refuse)
        )
        with pytest.raises(OutOfCoverageError):
            async with client:
                pass
        return client

    client = asyncio.run(main())
    with pytest.raises(ConfigurationError, match="not built yet"):
        _ = client.client


def test_the_wrapper_refuses_what_it_cannot_wrap() -> None:
    with pytest.raises(ConfigurationError, match="store client or a callable"):
        AsyncClient(42)  # type: ignore[call-overload]
    with (
        Carrefour() as carrefour,
        pytest.raises(ConfigurationError, match="only accepted with a callable"),
    ):
        AsyncClient(carrefour, 1)  # type: ignore[call-overload]

    async def main() -> None:
        async with AsyncClient(lambda: "not a client"):  # type: ignore[call-overload]
            pass

    with pytest.raises(ConfigurationError, match="must return a store client"):
        asyncio.run(main())


def test_a_call_that_cannot_be_scheduled_releases_the_client() -> None:
    harness = HARNESSES["mercadona"]

    async def main() -> None:
        client = AsyncClient(harness.client())
        await asyncio.get_running_loop().shutdown_default_executor()
        for _ in range(2):
            # a leaked lock would make the second attempt wait for ever
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(client.get_categories(), WAIT)
        client.client.close()

    asyncio.run(main())


def test_a_cache_transport_serves_repeated_awaits() -> None:
    harness = HARNESSES["mercadona"]
    recorder = Recorder(harness.handler)

    async def main() -> None:
        cache = CacheTransport(ttl=60, transport=transport(recorder))
        async with AsyncClient(Mercadona, "mad3", transport=cache) as client:
            first, second = await asyncio.gather(
                client.get_categories(), client.get_categories()
            )
            assert first == second

    asyncio.run(main())
    assert recorder.paths() == ["/api/categories/"]
