# supermercapy documentation

supermercapy gives read-only access to the product and storefront data behind fourteen spanish supermarkets, from sync code or, through `AsyncClient`, from asyncio. one base client defines the interface, one client class per store implements it, and each client declares which optional operations and data shapes the store supports. no client manages carts, accounts, orders, or payment data.

## documents

| document | purpose |
| --- | --- |
| [usage guide](usage.md) | installation patterns and complete examples |
| [command line](cli.md) | the `supermercapy` command, its request counts, and its exit codes |
| [api guide](api.md) | the shared interface, the capability matrix, and the models |
| [generated api](reference.md) | signatures and docstrings read from the package |
| [reliability and request behavior](reliability.md) | timeouts, retries, pacing, errors, and request counts |
| [async](async.md) | running the clients from asyncio, one call at a time per client |
| [caching](caching.md) | the opt-in response cache, what it stores, and how to pick a ttl |
| [matching](matching.md) | finding the same product, or a cheaper alternative, across stores |
| [contribution guide](https://github.com/jtayped/supermercapy/blob/main/CONTRIBUTING.md) | local setup, tests, style, and pull requests |
| [release process](https://github.com/jtayped/supermercapy/blob/main/RELEASING.md) | versioning, verification, and trusted publishing |
| [changelog](https://github.com/jtayped/supermercapy/blob/main/CHANGELOG.md) | released and pending user-visible changes |

## stores

each page documents one store: what a client binds to, which capabilities it declares, the extra methods it adds, the quirks to know before trusting a field, and what supermercapy does not do there.

| store | class | page |
| --- | --- | --- |
| mercadona | `Mercadona` | [mercadona](stores/mercadona.md) |
| consum | `Consum` | [consum](stores/consum.md) |
| plusfresc | `Plusfresc` | [plusfresc](stores/plusfresc.md) |
| bonàrea | `Bonarea` | [bonàrea](stores/bonarea.md) |
| carrefour | `Carrefour` | [carrefour](stores/carrefour.md) |
| lidl | `Lidl` | [lidl](stores/lidl.md) |
| bonpreu | `Bonpreu` | [bonpreu](stores/bonpreu.md) |
| dia | `Dia` | [dia](stores/dia.md) |
| eroski | `Eroski` | [eroski](stores/eroski.md) |
| caprabo | `Caprabo` | [caprabo](stores/caprabo.md) |
| aldi | `Aldi` | [aldi](stores/aldi.md) |
| ahorramás | `Ahorramas` | [ahorramás](stores/ahorramas.md) |
| alcampo | `Alcampo` | [alcampo](stores/alcampo.md) |
| condis | `Condis` | [condis](stores/condis.md) |

`supermercapy.ALL_CLIENTS` holds the same fourteen classes in this order, so a program can iterate over every store without naming one. the [capability matrix](api.md#capability-matrix) says which of them answers which question.

## support policy

the current version supports cpython 3.11 through 3.14. the public api is the set of names exported by the top-level `supermercapy` package, plus the client and model names each `supermercapy.<store>` package exports. a change that removes or renames one of those names needs a new major version.

the clients depend on undocumented upstream apis. a supermercapy release records the response shapes its fixtures and its live contract run covered. it does not promise that any storefront will keep answering the same way.

## project status

the project is unofficial and has no affiliation with any of the retailers it reads. report supermercapy defects through the [github issue tracker](https://github.com/jtayped/supermercapy/issues). do not send third-party service incidents or account questions to this project.
