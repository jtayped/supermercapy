# changelog

this project follows [semantic versioning](https://semver.org/).

## [0.1.0] - unreleased

the first release. one package, `supermercapy`, importable as `supermercapy`, with one client per store on a shared base, usable from sync code and from asyncio.

### added

#### core

- `BaseClient`: the standard storefront interface, one retry loop with a pluggable response verdict, per-client request pacing, session warm-up hooks, credential refresh, challenge handling, and an atomic streaming download that follows redirects.
- `Capability`: eleven flags declaring the optional operations and data shapes a store supports, with every optional method raising `UnsupportedOperationError` before any i/o when its flag is absent.
- one opaque-cursor pagination contract covering page, offset, token, row, and unpaged storefronts, plus `iter_search` and `iter_catalog`.
- frozen, slotted core models, which each store subclasses with the fields its storefront publishes: `Product`, `ProductSummary`, `Price`, `Availability`, `Promotion`, `Nutrition`, `NutritionValue`, `Photo`, `Category`, `SearchResult`, `Store` and `HomeSection`.
- `Price.reference`, a `UnitPrice` that restates each store's unit price per kilogram, litre, piece, dose, metre or square metre so prices compare across stores, with `Unit` and `UnitPrice` exported. `None` means unknown, not zero.
- an exception tree under `SupermercapyError` covering configuration, transport, rate limiting, missing and unavailable resources, coverage, blocks, challenges, authentication, and unusable responses. `RateLimitError.retry_after` and `BlockedError.retry_after` carry the delay the server asked for.
- `to_dict()` and `to_json()`, which turn any model, or a list, tuple or dict of models, into json-compatible python. decimals become exact strings.
- logging through the standard library on `supermercapy` and `supermercapy.<store>`: requests at debug; retries, pacing waits, warm-ups, credential refreshes and challenges at info. silent by default.
- `search_all()`, which searches several stores in parallel with one client each and returns one `StoreSearch` per store in a fixed order, so one store's failure never hides the others' results.
- `AsyncClient`, which runs any store client from asyncio in worker threads, one call at a time per client, with awaitable standard methods, async `iter_search()` and `iter_catalog()`, and `run()` and `iterate()` for store extensions.
- `CacheTransport`, an opt-in in-memory cache for a client's `transport` that keeps successful json answers for a chosen `ttl`, never stores errors or html, and never replays cookies on a hit.
- `supermercapy.match`, which matches products across stores without a request of its own. `score_same`, `find_same`, `pair_same` and `group_same` find the same product, brand and pack size at other stores, and `score_alternative` and `find_alternatives` find comparable products under any brand, cheapest first by `price.reference`. every `Match` carries a score and the reasons behind it. on 1,343 hand-labelled live listings, `group_same` kept precision at 1.00 with recall of 0.64 to 0.72 at the default threshold. the matching page says how that was measured.
- the `supermercapy` command, also run as `python -m supermercapy`, with `stores`, `search`, `compare`, `product`, `categories`, and `ean`. it prints aligned tables with comparable unit prices, or one json document with `--json`, and exits 0, 1 when a store failed, or 2 on a usage error.
- stdlib-only html helpers for the storefronts that publish data as markup.

#### stores

- `Mercadona`: warehouse binding through a postcode, algolia search, categories with a fallback for root ids, home sections, seasons, new arrivals, the category catalog, the reconciled indexed catalog, and warehouse discovery.
- `Consum`: optional zone binding, offset paging, batch product reads, barcode lookup, typed sort orders and filters, campaign groups, suggestions, and every page of offers and new arrivals.
- `Plusfresc`: preparation-center binding, self-minted guest tokens sent only to the api host, with one re-mint per call, client-side paging over unpaged responses, the single-request catalog, nutrition, and catalog versioning.
- `Bonarea`: form-post reads, a language warm-up for search, the tree that arrives with every listing, id normalisation, nutrition parsed out of label html, badge-filtered new products and price drops, delivery zones, and 0.5 s default pacing.
- `Carrefour`: sale-point binding, the direct search index, departments from the storefront menu, categories read from their listing pages, the cms carousels of the home page, cloudflare warm-up with a time-to-live, the two `403` bodies told apart, stock, and suggestions.
- `Lidl`: region and zone binding, both assortments in one client, region-resolved grocery prices, future prices, campaign pages, leaflets, and the sitemap catalog.
- `Bonpreu`: token paging, nutrition tables, promotions, new arrivals and offers, waf blocks told apart from csrf expiry, and 2 s default pacing.
- `Dia`: search with exact page sizes, product sheets with nutrition, the two-level category tree, the catalog walk, the "novedades" new arrivals, all offers by category, postcode binding through the anonymous session, and catalan and english.
- `Aldi`: the priced catalog per price region (península, baleares, canarias) through algolia, the category pages, the whole catalog in three requests, the weekly offers with their dates, prices published ahead of their start, and postcode-to-region mapping with no request.
- `Eroski`: search, product pages with nutrition, the category menu, and a catalog walk, read from the storefront's html.
- `Caprabo`: the same storefront build as eroski, in spanish and catalan.
- `Ahorramas`: search, product records, the category menu, the offers branch, and a catalog walk.
- `Alcampo`: search, product sheets with nutrition, the category tree, promotions and the storefront's own new-product filter, similar and related products, suggestions, and every click-and-collect point, with region binding through a point. it shares ocado smart platform code with `Bonpreu`, and paces requests 2 s apart under an aws waf budget of a few product-service requests per half hour.
- `Condis`: search, listings, new arrivals, offers and suggestions from the empathy index the storefront calls, with facet filters; product sheets with nutrition read from the server-rendered page behind the anonymous sign-in; the three-level category tree; the catalog walk; and binding to a picking centre directly or through the storefront's own postcode lookup. requests are paced 0.5 s apart.
- `ALL_CLIENTS`, and a conformance suite that runs the same contract against every client in it.

#### verification and tooling

- live contract tests under `tests/live`, one module per store, skipped unless pytest gets `--live`. they call every public method, assert shape, and compare each raw json response with its committed fixture so an upstream rename fails instead of turning into `None`. a bot block or challenge counts as inconclusive.
- the `live contract` workflow, weekly and on demand, one job per store, opening or updating one issue per failing store and one, labelled `inconclusive`, per store that refused every test, on the runner named by the `LIVE_RUNNER_LABELS` repository variable.
- a public api snapshot test, so a removed or changed export, signature or field is always a deliberate change.
- `scripts/ci_local.sh`, which runs the whole ci matrix locally, and `scripts/check_wheel.py`, shared by ci and the local run.
- documentation: usage and api guides, the capability matrix, reliability and request behavior, async, caching, matching, the command line, generated api reference, and one page per store.
- ci across cpython 3.11 through 3.14, a minimum-dependency job, a clean-wheel job that builds every client and lists the stores through the command line, and a documentation build.

### upstream changes absorbed before release

a live audit on 2026-10-04 called every public method of every store. it found these storefront changes, and the clients now handle them:

- lidl's search api answers `Accept: application/json` with http 406, so listings now send its `application/mindshift.search+json;version=2` type. product details moved the brand under `info.brand`.
- bonpreu's waf answers non-browser user agents with http 403 on every api path. the client sends a browser user agent and tells that block apart from csrf expiry.
- carrefour's sitemaps answer every non-browser request with a cloudflare challenge, so carrefour no longer declares `CATALOG`. the client reads its categories and home page from the storefront instead.
- mercadona's category endpoints stopped sending `level`, home notifications carry an action object, and new-arrival items lost `is_new_arrival`.
- consum gives a code it never had the same answer as a product the zone does not carry, so both raise `NotFoundError`.
- bonpreu's promotions listing sends numbers instead of strings and drops `unitName`.
- eroski's and caprabo's edge answers a datacenter address with google cloud armor's recaptcha page and http 200. the client raises `ChallengedError` for it instead of failing to parse the page.
- plusfresc's `CategoXXX_` bundles send quantities such as 1000 and 2000 that match nothing the storefront shows, so `requires_quantity` is read only for `CategoMxN_` multi-buys, where it is the number of units the deal needs.
