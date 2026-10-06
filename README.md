# supermercapy

typed clients for the public storefront apis of fourteen spanish supermarkets, for sync code and, through `AsyncClient`, for asyncio. a shared base client defines the standard operations, each store implements them against its own backend, and every client declares which optional operations and data shapes its storefront supports.

this is an unofficial project. it is not affiliated with, endorsed by, or maintained by any of the retailers it reads. the upstream apis are undocumented and may change without notice.

## installation

supermercapy supports cpython 3.11 through 3.14. the distribution and the import name are both `supermercapy`.

```bash
python -m pip install supermercapy
```

## quick start

bind a client to a place, search, then ask for the complete product:

```python
from supermercapy import Mercadona

with Mercadona.from_postal_code("28001") as mercadona:
    page = mercadona.search_products("leche sin lactosa", page_size=10)

    for summary in page.products:
        print(summary.name, summary.price.amount)

    if page.products:
        product = mercadona.get_product(page.products[0].id)
        print(product.ean)
        print(product.nutrition.ingredients)
```

every client is built the same way, so the same code reads another store:

```python
from supermercapy import Capability, Lidl

with Lidl.from_postal_code("08013") as lidl:
    page = lidl.search_products("chocolate", page_size=10)
    if lidl.supports(Capability.OFFERS):
        offers = lidl.get_offers()
```

construction performs no request. `from_postal_code()` costs one request at mercadona and more at lidl, which pages through its store list. the [request counts](https://jtayped.github.io/supermercapy/reliability/#request-counts) give every store's figure. a client reuses its connections, so close it with a `with` block.

`search_all()` asks several stores at once, each on its own thread with its own client. it reports every store separately, so one store's failure never hides what the others found:

```python
from supermercapy import search_all

result = search_all("leche", postal_code="08013", page_size=5)
for store, product in result.products:
    print(store, product.name, product.price.amount)
for entry in result.failed:
    print(entry.store, entry.error_type, entry.reason)
```

## command line

installing the package also installs a `supermercapy` command, which prints aligned tables, or one json document with `--json`:

```bash
supermercapy stores
supermercapy search leche --postal-code 08013 --limit 3
supermercapy product consum 7080604
supermercapy ean 8431876011937 --json
```

the [command line guide](https://jtayped.github.io/supermercapy/cli/) covers every command, its request count, and its exit codes.

## stores

| store | class | binds to | page |
| --- | --- | --- | --- |
| mercadona | `Mercadona` | a warehouse | [docs](https://jtayped.github.io/supermercapy/stores/mercadona/) |
| consum | `Consum` | a zone, optionally | [docs](https://jtayped.github.io/supermercapy/stores/consum/) |
| plusfresc | `Plusfresc` | a preparation center | [docs](https://jtayped.github.io/supermercapy/stores/plusfresc/) |
| bonàrea | `Bonarea` | nothing | [docs](https://jtayped.github.io/supermercapy/stores/bonarea/) |
| carrefour | `Carrefour` | a sale point, optionally | [docs](https://jtayped.github.io/supermercapy/stores/carrefour/) |
| lidl | `Lidl` | an offer region and a price zone | [docs](https://jtayped.github.io/supermercapy/stores/lidl/) |
| bonpreu | `Bonpreu` | nothing | [docs](https://jtayped.github.io/supermercapy/stores/bonpreu/) |
| dia | `Dia` | a delivery postcode, optionally | [docs](https://jtayped.github.io/supermercapy/stores/dia/) |
| eroski | `Eroski` | nothing | [docs](https://jtayped.github.io/supermercapy/stores/eroski/) |
| caprabo | `Caprabo` | nothing | [docs](https://jtayped.github.io/supermercapy/stores/caprabo/) |
| aldi | `Aldi` | a price region | [docs](https://jtayped.github.io/supermercapy/stores/aldi/) |
| ahorramás | `Ahorramas` | nothing | [docs](https://jtayped.github.io/supermercapy/stores/ahorramas/) |
| alcampo | `Alcampo` | a click-and-collect point, optionally | [docs](https://jtayped.github.io/supermercapy/stores/alcampo/) |
| condis | `Condis` | a picking centre | [docs](https://jtayped.github.io/supermercapy/stores/condis/) |

`supermercapy.ALL_CLIENTS` holds all fourteen, so generic code never has to name one.

## capabilities

`search_products()`, `get_product()`, `get_categories()` and `get_category()` work everywhere. everything else is optional and declared, because no two storefronts publish the same things:

| capability | stores |
| --- | --- |
| `POSTAL_CODE` | mercadona, consum, plusfresc, carrefour, lidl, dia, aldi, condis |
| `STORES` | mercadona, consum, plusfresc, carrefour, lidl, alcampo |
| `CATALOG` | mercadona, consum, plusfresc, bonàrea, lidl, dia, eroski, caprabo, aldi, ahorramás, condis |
| `EAN_LOOKUP` | consum, carrefour |
| `NEW_ARRIVALS` | mercadona, consum, plusfresc, bonpreu, dia, alcampo, condis |
| `OFFERS` | consum, plusfresc, lidl, bonpreu, dia, aldi, ahorramás, alcampo, condis |
| `HOME` | mercadona, carrefour |
| `EAN` | mercadona, consum, carrefour, lidl |
| `NUTRITION` | mercadona, plusfresc, bonàrea, carrefour, bonpreu, dia, eroski, caprabo, alcampo, condis |
| `PROMOTIONS` | consum, plusfresc, carrefour, lidl, bonpreu, dia, eroski, caprabo, aldi, ahorramás, alcampo, condis |
| `FUTURE_PRICES` | lidl, aldi |

an undeclared operation raises `UnsupportedOperationError` before making any request, and `Client.supports(capability)` answers before a client is even built. a test checks the [full matrix](https://jtayped.github.io/supermercapy/api/#capability-matrix) against the code, so it cannot drift.

## behavior

- client methods make the requests. reading a returned model never performs i/o.
- listing calls return `ProductSummary`, and `get_product()` returns a complete `Product`. each store narrows both to its own subclass.
- models are frozen, slotted dataclasses. collections are tuples, ids are strings, and prices and quantities use `Decimal`.
- parsers ignore unknown response fields. missing optional values become `None` or an empty tuple.
- pagination is one opaque cursor, whatever the store does underneath.
- failures raise a `SupermercapyError` subclass. library code never prints.
- connection failures, http `429` and selected `5xx` responses go through one bounded retry policy. stores that answer with challenges or expired tokens extend the classification, not the loop.
- request pacing is per client, and a store that needs it ships a default.

## documentation

- [documentation index](https://jtayped.github.io/supermercapy/)
- [usage guide](https://jtayped.github.io/supermercapy/usage/)
- [command line](https://jtayped.github.io/supermercapy/cli/)
- [api guide](https://jtayped.github.io/supermercapy/api/)
- [generated api reference](https://jtayped.github.io/supermercapy/reference/)
- [reliability and request behavior](https://jtayped.github.io/supermercapy/reliability/)
- [async](https://jtayped.github.io/supermercapy/async/)
- [caching](https://jtayped.github.io/supermercapy/caching/)
- [matching products across stores](https://jtayped.github.io/supermercapy/matching/)
- [examples](https://github.com/jtayped/supermercapy/tree/main/examples): a shopping list priced at every store near a postcode, daily catalog snapshots and what changed between them, and the cheapest alternative to a branded product
- [contribution guide](https://github.com/jtayped/supermercapy/blob/main/CONTRIBUTING.md)
- [release process](https://github.com/jtayped/supermercapy/blob/main/RELEASING.md)
- [changelog](https://github.com/jtayped/supermercapy/blob/main/CHANGELOG.md)

supermercapy is distributed under the [mit license](https://github.com/jtayped/supermercapy/blob/main/LICENSE).
