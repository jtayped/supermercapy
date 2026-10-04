# usage guide

this guide covers the workflow every store client shares. the [store pages](index.md#stores) cover what each one adds on top. every example uses a context manager so the underlying connection pool closes at the end of the block.

## install

supermercapy supports cpython 3.11 through 3.14. the distribution and the import name are both `supermercapy`.

```bash
python -m pip install supermercapy
```

## pick a client

each store has one client class, exported from the top-level package and from its own subpackage. construction performs no request.

```python
from supermercapy import Carrefour, Mercadona

with Mercadona("mad3") as mercadona:
    print(mercadona.store_id)

with Carrefour() as carrefour:
    print(carrefour.store_id)
```

`ALL_CLIENTS` holds every class in the order the stores were added, which is how generic code iterates them.

```python
from supermercapy import ALL_CLIENTS, Capability

for client_type in ALL_CLIENTS:
    print(client_type.store_name, client_type.supports(Capability.CATALOG))
```

`supports()` is a classmethod, so it answers before anything is constructed.

the clients are synchronous. from asyncio, wrap one in `AsyncClient`, which runs each call in a worker thread so the event loop never waits on a storefront. [async](async.md) covers opening, iterating and the one-call-at-a-time rule.

## bind to a place

a store whose prices or assortment depend on where you shop declares `Capability.POSTAL_CODE`, and its `from_postal_code()` resolves a five-digit spanish postcode to a binding. that costs one request at consum, as below. the cost differs by store, from none at aldi to eighteen at condis in october 2026, and [request counts](reliability.md#request-counts) gives each one.

```python
from supermercapy import Consum

with Consum.from_postal_code("46001") as consum:
    print(consum.store_id, consum.zone)
```

`store_id` is the uniform name for that binding. each client also keeps its own spelling of it, such as `Mercadona.warehouse`, `Consum.zone`, `Plusfresc.center`, `Carrefour.sale_point`, and `Lidl.region`. a store that binds nothing, like bonàrea, reports `None`.

on the stores that say so, a postcode outside the delivery footprint raises `OutOfCoverageError`. where a store also declares `Capability.STORES`, `list_stores()` shows what the binding could be before you commit to one.

```python
from supermercapy import Lidl

with Lidl() as lidl:
    for store in lidl.list_stores("08013"):
        print(store.id, store.name, store.city)
```

## search and paginate

`search_products()` returns one page and an opaque `next_cursor`. pass that cursor back to continue. `None` means there is nothing after this page. the cursor is a page number for one store and an offset for another, so never build one by hand.

```python
from supermercapy import Mercadona

with Mercadona("mad3") as mercadona:
    page = mercadona.search_products("café", page_size=20)
    print(page.total_hits, page.has_more)

    while page.has_more:
        page = mercadona.search_products("café", page_size=20, cursor=page.next_cursor)
        for summary in page.products:
            print(summary.id, summary.name, summary.price.amount)
```

`iter_search()` does that walk for you and yields summaries until the cursor runs out.

```python
from supermercapy import Mercadona

with Mercadona("mad3") as mercadona:
    for summary in mercadona.iter_search("café"):
        print(summary.name)
```

a page whose `truncated` flag is set sat against an upstream result ceiling with matches left over. its `total_hits` is then a floor, not a total, and that query cannot reach the remaining matches. narrow the query or walk the category tree instead.

## search several stores at once

`search_all()` sends one query to every store, or to the ones you name. each store gets its own client on its own thread, the call closes every client before it returns, and a store that fails never hides what the others found.

```python
from supermercapy import SearchStatus, search_all

result = search_all("leche", postal_code="08013", page_size=5)

for store, product in result.products:
    print(store, product.name, product.price.amount)

for entry in result.stores:
    if entry.status is not SearchStatus.OK:
        print(entry.store, entry.status, entry.reason)
```

`result.stores` holds one `StoreSearch` per store, in the order the stores were named, which is `ALL_CLIENTS` order by default. the order never depends on which store finished first. each entry's `status` is one of four values.

| status | meaning | fields set |
| --- | --- | --- |
| `OK` | the store answered | `result`, and `store_id` when the store is bound |
| `SKIPPED` | the store cannot be built without a binding and no postcode was given, so nothing was asked | `reason` |
| `OUT_OF_COVERAGE` | the store does not serve the postcode | `reason`, `error_type` |
| `FAILED` | the store raised a `SupermercapyError` | `reason`, `error_type`, and `store_id` once bound |

`result.pages` maps each store that answered to its `SearchResult`. `result.products` pairs every summary with its store name, store by store. `result.failed` lists the failed entries, and a skipped store or one out of coverage is not among them. any exception that is not a `SupermercapyError` is a bug, so it propagates once the stores already running have finished.

`reason` holds the exception message and `error_type` its class name rather than the exception itself, which keeps the result plain data. `to_json(result)` writes all of it, failures included.

`stores` takes store names, client classes, or a mix. a single name works too.

```python
from supermercapy import Consum, search_all

result = search_all("arroz", stores=["lidl", Consum], page_size=10, timeout=5.0)
print(result.pages["consum"].total_hits)
```

`search_all()` caps `page_size` at each store's `max_page_size`, and leaving it out gives every store its own default. `max_workers` defaults to one thread per store. `timeout`, `retry_policy`, `min_request_interval`, `user_agent`, and `transport` reach the constructor of every client. one `transport` serves all of them and stays open afterwards, because whoever created it owns it. a `user_agent` applies to every store, and carrefour's storefront, bonpreu and alcampo answer http 403 to one that is not a browser's.

invalid arguments, such as an unknown store, a blank query, or a malformed postcode, raise `ConfigurationError` before any request.

### request counts

with a postcode, every store that declares `POSTAL_CODE` binds through `from_postal_code()` before it searches, and some bindings cost more than one request. plusfresc also fetches a guest token and bonàrea its language page before their first search. counts are for one call, before retries.

| store | without a postcode | with one |
| --- | ---: | ---: |
| mercadona | 0, skipped | 2 |
| consum | 1 | 2 |
| plusfresc | 2 | 3 |
| bonàrea | 2 | 2 |
| carrefour | 1 | 4 |
| lidl | 1 | 1 plus one per 250 stores in its store list, 4 in october 2026 |
| bonpreu | 1 | 1 |
| dia | 1 | 3 |
| eroski | 1 | 1 |
| caprabo | 1 | 1 |
| aldi | 1 | 1 |
| ahorramás | 1 | 1 |
| alcampo | 1 | 1 |
| condis | 1 | 1 plus 18 to find and call the postcode action, in october 2026 |

a search of every store therefore cost 15 requests without a postcode and 45 with one in october 2026. a store that turns out not to serve the postcode stops after its binding requests.

## compare prices across stores

every store publishes its unit price in its own unit. mercadona sends `"L"` and `"dc"`, consum `"1 L"` and `"100 Gr"`, bonpreu `"PER_1KG"`. `price.reference` restates each one as a `UnitPrice` per kilogram, litre, piece, dose, metre or square metre, so two stores' rows sort together.

```python
from supermercapy import Consum, Mercadona, Unit

rows = []
with Mercadona.from_postal_code("46001") as mercadona:
    page = mercadona.search_products("aceite de oliva", page_size=10)
    rows += [("mercadona", product) for product in page.products]
with Consum.from_postal_code("46001") as consum:
    page = consum.search_products("aceite de oliva", page_size=10)
    rows += [("consum", product) for product in page.products]

per_litre = [
    (product.price.reference.amount, store, product.name)
    for store, product in rows
    if product.price.reference is not None
    and product.price.reference.unit is Unit.LITRE
]
for amount, store, name in sorted(per_litre):
    print(f"{amount} €/l  {store}  {name}")
```

that example makes four requests, two per store: one to bind the postcode and one to search.

`reference` is `None` when a store published no unit price or one in a unit the package doesn't know. treat that as unknown, not as zero, which is why the example drops those rows instead of sorting them first. keep the `unit` check too. a bottle priced per litre and a pack of capsules priced per piece have nothing to compare. the [api guide](api.md#prices) lists every conversion, and each store page says which units that store reports.

## match the same product across stores

two stores rarely share an id, and only four publish barcodes. `group_same()` lines up the listings of a `search_all()` result that are the same brand, product and pack at different stores. `find_alternatives()` lists comparable products under any brand, store brands included, cheapest first by `price.reference`. every match carries a score from 0 to 1 and the reasons behind it, and neither function makes a request.

```python
from supermercapy import group_same, search_all

result = search_all("nocilla", page_size=10)
for group in group_same(result.products):
    if len(group.listings) > 1:
        print(group.score, [listing.store for listing in group.listings])
```

the search costs what `search_all()` costs, listed under [request counts](#request-counts). [matching](matching.md) covers both questions, the signals, the default thresholds and the precision and recall they were measured at.

## fetch product details

listing calls return `ProductSummary`. ask for the complete `Product` when you need photos, the description, the category path, or nutrition.

```python
from supermercapy import Consum

with Consum.from_postal_code("46001") as consum:
    page = consum.search_products("arroz", page_size=1)
    if page.products:
        product = consum.get_product(page.products[0].id)
        print(product.name, product.ean)
        print(
            product.price.amount,
            product.price.unit_price,
            product.price.unit_price_unit,
        )
```

that example makes three requests: one to resolve the zone, one to search, and one for the product. reading an attribute of a returned model never makes another.

ids are opaque strings. an integer is accepted and becomes a string, but never assume an id is numeric or that two stores spell the same product the same way.

a program that asks for the same products again and again, such as a price poller, can keep answers in memory with `transport=CacheTransport(ttl=600)`. [caching](caching.md) explains what it stores, what it never stores, and how to pick the `ttl`.

## browse categories

`get_categories()` returns the tree and `get_category()` returns one node with its children and whatever products the store lists under it.

```python
from supermercapy import Bonarea

with Bonarea() as bonarea:
    for category in bonarea.get_categories():
        print(category.id, category.name, category.level)

    category = bonarea.get_category("13*300*010*010")
    for summary in category.products:
        print(summary.name)
```

stores that declare `Capability.CATALOG` can enumerate everything they sell. `iter_catalog()` streams, may repeat a product listed in more than one place, and costs as many requests as the store's own structure demands. `get_catalog()` collects the same stream and deduplicates it by id.

```python
from supermercapy import Plusfresc

with Plusfresc(center=12) as plusfresc:
    for product in plusfresc.iter_catalog():
        print(product.id, product.name)
```

read the store page before running a catalog walk. one store answers it from a single multi-megabyte response, another issues one request per product.

## ask only what a store answers

optional operations raise `UnsupportedOperationError` before any request when the store does not declare their capability, so a missing feature fails loudly instead of returning an empty tuple.

```python
from supermercapy import Bonarea, Capability, UnsupportedOperationError

with Bonarea() as bonarea:
    if bonarea.supports(Capability.OFFERS):
        offers = bonarea.get_offers()
    else:
        offers = ()

    try:
        bonarea.get_offers()
    except UnsupportedOperationError as error:
        print(error.store, error.capability)
```

data-shape capabilities work the same way for fields. `Capability.NUTRITION` says the store publishes nutrition data at all, which is what tells a `None` apart from "never available" there. `Capability.EAN`, `Capability.PROMOTIONS`, and `Capability.FUTURE_PRICES` do the same for barcodes, offers attached to a product, and prices that start later.

## choose a language

`Language` is a string enum of the four storefront languages supermercapy has seen: `SPANISH`, `CATALAN`, `ENGLISH`, and `VALENCIAN`. each client declares the ones it supports and rejects the rest with `ConfigurationError`.

```python
from supermercapy import Language, Plusfresc

print(sorted(Plusfresc.supported_languages))
print(Plusfresc.default_language is Language.CATALAN)

with Plusfresc(language="es") as plusfresc:
    print(plusfresc.language)
```

the string form works everywhere the enum does. a language changes the text a storefront returns, and on some of them the assortment a search reaches.

## download an image

`download()` streams any `Photo` or image url to a local path.

```python
from supermercapy import Carrefour

with Carrefour() as carrefour:
    product = carrefour.get_product("521007071")
    if product.photos:
        carrefour.download(product.photos[0], "images/milk.jpg")
```

the download streams into a temporary file beside the destination and replaces it only after the whole transfer succeeds, so a failed or interrupted download leaves an existing file intact. clients whose cdn can resize an image also ship a `download_photo()` that takes the store's own sizing arguments.

## saving results

`to_dict()` and `to_json()` turn any model into plain, json-compatible python, so you can write results to a file or hand them to another tool. they accept a model, a list or tuple of models such as the one `get_catalog()` returns, or a dict of them. a model gives a dict and a list gives a list.

```python
from pathlib import Path

from supermercapy import Mercadona, to_json

with Mercadona("mad3") as mercadona:
    product = mercadona.get_product("4241")
Path("product.json").write_text(to_json(product, indent=2), encoding="utf-8")
```

`Decimal` values become strings such as `"1.69"` rather than floats, because a float would round the price the storefront published. dates and times become iso 8601 strings, enums become their value, and sets become sorted lists. they include only dataclass fields, so properties such as `SearchResult.has_more` are left out. `to_dict()` raises `TypeError` for a value it does not know how to convert, and neither function does any i/o.

## work with model values

every model is a frozen, slotted, keyword-only dataclass. collections are tuples, ids are strings, and money and quantities use `Decimal`, which keeps the exact figure the storefront published.

```python
from dataclasses import asdict
from decimal import Decimal

from supermercapy import Price

price = Price(amount=Decimal("1.15"), unit_price=Decimal("2.30"))
assert price.amount == Decimal("1.15")
payload = asdict(price)
```

store packages subclass the core models to add fields their storefront publishes, and a client's return type narrows to its own subclass. a `MercadonaProduct` is a `Product`, so code written against the core model keeps working across stores.

```python
from supermercapy import Product
from supermercapy.mercadona import MercadonaProduct

assert issubclass(MercadonaProduct, Product)
```

model fields reflect what the upstream response supplied. optional fields may be `None` and optional collections may be empty.
