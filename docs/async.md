# async

the store clients are synchronous. `AsyncClient` runs one from asyncio by sending each call to a worker thread, so the event loop keeps running while a request is in flight. the retries, pacing, warm-ups and errors are the ones the synchronous client already has, because supermercapy reimplements nothing for asyncio.

```python
import asyncio

from supermercapy import AsyncClient, Consum, Mercadona


async def main() -> None:
    async with (
        AsyncClient(Mercadona.from_postal_code, "46001") as mercadona,
        AsyncClient(Consum.from_postal_code, "46001") as consum,
    ):
        pages = await asyncio.gather(
            mercadona.search_products("leche", page_size=5),
            consum.search_products("leche", page_size=5),
        )
    for page in pages:
        for summary in page.products:
            print(summary.name, summary.price.amount)


asyncio.run(main())
```

that example makes four requests, a postcode lookup and a search for each store, and the two stores answer at the same time.

## open a client

give `AsyncClient` a callable that builds a client, followed by that callable's arguments. the callable runs in a worker thread when the `async with` block starts, so neither the connection pool nor a postcode lookup blocks the loop. a binding that fails, such as one that raises `OutOfCoverageError`, raises from the `async with` line. a type checker checks the arguments against the callable's own signature.

```python
import asyncio

from supermercapy import AsyncClient, Lidl, Mercadona


async def main() -> None:
    async with AsyncClient(Mercadona, "mad3") as mercadona:
        print(mercadona.client.store_id)
    async with AsyncClient(Lidl, region=26) as lidl:
        print(lidl.client.region)


asyncio.run(main())
```

you can also wrap a client you already built, as in `AsyncClient(Mercadona("mad3"))`. the wrapper owns it from then on and closes it.

leaving the `async with` block closes the client. without the block, the first call builds the client and `await client.aclose()` closes it.

`client.client` is the synchronous client underneath. read properties such as `store_id` and `language` from it, but do not call its request methods from the event loop. they would block the loop and skip the one-call-at-a-time rule described below.

## call it

you can await every method of the standard interface: `search_products()`, `get_product()`, `get_categories()`, `get_category()`, `list_stores()`, `get_product_by_ean()`, `get_new_arrivals()`, `get_offers()`, `get_home()`, `get_catalog()`, and `download()`. they take the standard arguments and carry the core models' types, as `BaseClient` declares them. at runtime, each value they return is the store's own subclass. errors raise from the `await` as the usual `SupermercapyError` subclasses, and an undeclared operation raises `UnsupportedOperationError`.

`run()` reaches the rest: a store's extensions, the extra keywords a store adds to a standard method, and the store's own return types. it calls the function you give it with the client, in a worker thread.

```python
import asyncio

from supermercapy import AsyncClient, Lidl


async def main() -> None:
    async with AsyncClient(Lidl, region=26) as lidl:
        upcoming = await lidl.run(lambda client: client.get_offers(week="next"))
        leaflets = await lidl.run(lambda client: client.get_leaflets())
    print(len(upcoming), len(leaflets))


asyncio.run(main())
```

a type checker reads `upcoming` as `tuple[LidlProduct, ...]` there, not as the core `ProductSummary`. that example makes two requests.

## iterate

`iter_search()` and `iter_catalog()` return async iterators. the store's own iterator runs in a worker thread one step at a time, so it fetches a page only when the loop asks for that page's first item, and breaking out of the loop costs no further request. other calls on the same client can run between two steps, which is how the loop below fetches each product as it goes.

```python
import asyncio

from supermercapy import AsyncClient, Mercadona


async def main() -> None:
    async with AsyncClient(Mercadona, "mad3") as mercadona:
        seen = 0
        async for summary in mercadona.iter_search("café", page_size=5):
            product = await mercadona.get_product(summary.id)
            print(product.name, product.ean)
            seen += 1
            if seen == 3:
                break


asyncio.run(main())
```

that example makes four requests: one page of results and three products.

`iterate()` does the same for any method that returns an iterator, as in `lidl.iterate(lambda client: client.iter_catalog_ids())`. each item costs a hop from a worker thread, about a tenth of a millisecond. for a whole catalog, `get_catalog()` is faster because it runs the walk as one call and returns the deduplicated tuple.

## one call at a time per client

calls on one `AsyncClient` run one at a time, first come first served. the clients keep state between requests, such as plusfresc's guest token and its renewal, bonpreu's csrf token, carrefour's warm session and its renewal every twenty-five minutes, and bonàrea's session language. two threads inside one client could interleave that state, for instance by both finding no guest token and both minting one. one call at a time keeps each client exactly as safe as it is in synchronous code. `asyncio.gather()` over one client still works, and the calls queue.

concurrency comes from awaiting several clients at once, usually one per store. each `AsyncClient` occupies at most one thread of the event loop's default executor at a time.

two clients of the same store do run in parallel, but each one paces itself. `min_request_interval` is per client, so two clients send that storefront twice the rate of one. do not do this to bonpreu or alcampo, whose bot protection counts requests per ip address.

## several stores in one call

`search_all()` already asks its stores in parallel, each on its own thread. from asyncio, run the whole call in one worker thread instead of wrapping its stores one by one.

```python
import asyncio

from supermercapy import search_all


async def main() -> None:
    result = await asyncio.to_thread(search_all, "leche", page_size=5)
    for store, product in result.products:
        print(store, product.name)


asyncio.run(main())
```

the [usage guide](usage.md#request-counts) lists the requests that call makes for each store.

## cancellation and timeouts

nothing can interrupt a worker thread. when you cancel an `await`, or an `asyncio.timeout()` block ends it, the request still runs to the end in the background. the wrapper then drops its result or error, and the next call on that client waits until it has finished. the client's own `timeout` and `retry_policy` bound how long that takes, so set those to stop a slow storefront instead of relying on cancellation.

## caching

a [`CacheTransport`](caching.md) works the same under an `AsyncClient`. pass it as the `transport` argument after the callable.

```python
import asyncio

from supermercapy import AsyncClient, CacheTransport, Mercadona


async def main() -> None:
    cache = CacheTransport(ttl=600)
    async with AsyncClient(Mercadona, "mad3", transport=cache) as mercadona:
        first = await mercadona.get_product("4241")
        again = await mercadona.get_product("4241")
    print(first == again)


asyncio.run(main())
```

that example makes one request.
