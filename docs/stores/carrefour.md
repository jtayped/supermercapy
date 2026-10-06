# carrefour

`supermercapy.carrefour.Carrefour` reads carrefour's spanish food storefront. it talks to two hosts and asks each question of the one that answers it best.

- **the search index.** `api.empathy.co` serves the same empathy instance the storefront's own search box calls. it is unauthenticated, needs no cookie, and is the only path that honours a per-request sale point. searching, autocomplete and barcode lookups all go here.
- **the storefront.** `www.carrefour.es` renders its pages server-side and hands the browser the whole page state as one `window.__INITIAL_STATE__` assignment. product pages, category listings, the home page, stock, the navigation menu and the sale-point directory come from there, and nutrition exists nowhere else.

the catalog is not enumerable. carrefour's only complete listing was its sitemaps, and since october 2026 every sitemap answers a non-browser client with cloudflare's interactive challenge, so `CATALOG` is not declared and `iter_catalog`, `get_catalog` and `iter_sitemap_ids` do not exist on this client. `get_category` reaches the leaves of the category tree, and search reaches everything else.

a sale point changes both prices and assortment, like mercadona's warehouse. leaving it unset searches the national superset, which prices nothing.

```python
from supermercapy import Carrefour

with Carrefour.from_postal_code("28232") as carrefour:
    carrefour.sale_point  # "004737"
    page = carrefour.search_products("leche sin lactosa", sort="price asc")
    for summary in page.products:
        summary.price.amount, summary.category_ids

    milk = carrefour.get_product("521007071")
    milk.nutri_score  # "B"
    milk.nutrition.allergens  # "Leche"
    milk.app_price  # the app-exclusive price

    leche = carrefour.get_category("cat20093")
    for card in leche.products:
        card.promotions, card.restrictions, card.stock

with Carrefour("0000GP", catalog="cellar") as carrefour:
    carrefour.suggest("vin")
```

## capabilities

| capability | supported | how |
|---|---|---|
| `POSTAL_CODE` | yes | `from_postal_code` reads the shops near a postcode, then binds the drive in the same postcode or the nearest one |
| `STORES` | yes | `list_stores` returns the drive directory, or the physical shops near one postcode |
| `CATALOG` | no | the sitemaps are the only complete listing and they are challenged since october 2026; walk the tree with `get_category` instead |
| `EAN` | yes | every search document carries `ean13` and every product page an `ean` |
| `EAN_LOOKUP` | yes | `get_product_by_ean` matches the barcode as free text and then verifies it |
| `NUTRITION` | yes | ingredients, allergens, the nutrition table and the nutri-score, from the product page |
| `PROMOTIONS` | yes | campaign badges and purchase limits, from rendered listing cards |
| `HOME` | yes | `get_home` reads the cms product carousels of `/supermercado` |
| `NEW_ARRIVALS` | no | carrefour publishes no novelties feed; `ofertas` is an ordinary category |
| `OFFERS` | no | same, so read `cat20968591` through `get_category` instead |
| `FUTURE_PRICES` | no | prices carry a validity range but never a successor |

## extensions

| method | what it does |
|---|---|
| `get_stock(product_id)` | the unit count held by the session's sale point |
| `suggest(query, limit=)` | the completions the search box would offer |
| `get_categories(deep=True)` | the departments with their aisles, at one more menu request per department |
| `get_category_products(url, offset=)` | one rendered listing page by url |
| `download_photo(photo, path, width=)` | one image re-rendered at any width |

`search_products` takes one extra keyword, `sort`, in the index's own spelling (`"price asc"`, `"best_sellers_food desc"`). upstream refuses an unsortable field instead of ignoring it.

## quirks

**the user agent is the gate.** `www.carrefour.es` sits behind cloudflare, and the check is the user-agent string alone. cloudflare does not inspect the tls fingerprint. `default_user_agent` is therefore a browser string, and passing `user_agent="something/1.0"` makes every storefront request fail with http 403. neither the search index nor the images are affected.

**warm sessions.** cold storefront requests succeed only about three times in four. the first storefront request of a session fetches one html page to pick up `session_id` and `__cf_bm`, which makes the rest reliable. the cookie lives half an hour, and the client renews it every twenty-five minutes. requests that never touch the storefront never pay for it, so a client that only searches issues no warm-up at all. the navigation menu answers cold requests too, so `get_categories` skips the warm-up unless cloudflare challenges it, and then warms a session and asks once more.

**http 403 is two different things.** a body saying the request was blocked is a firewall rule and raises `BlockedError` after exactly one request. a body saying to wait a moment is the interactive challenge. for that one the client warms a new session, retries once, and raises `ChallengedError` if it is challenged again.

**the proxy ignores the sale point.** `get_stock` reads carrefour's own proxied search, which is the only publisher of stock. that search takes `store` from the session and ignores the one in the request, so the figure belongs to the session's default sale point. treat it as indicative unless the two agree. the endpoint also answers a request with no `session` parameter with a firewall block, not an api error, so the client always sends one. nothing reads its value.

**one query reaches at most 2,998 documents.** the index caps `rows` at 500 and `start` at 2,498. a page that sat against the ceiling with matches left over comes back with `truncated` set and no cursor. narrow the query, or walk the category tree.

**search documents carry only the leaf category.** filtering by an ancestor matches nothing, so "everything under la despensa" means expanding the subtree to its leaves first. the site's own `category_ids_2` filter does nothing on this index, where it returns the unfiltered count, and supermercapy never sends it.

**four id spellings.** `714713105`, `VC4AECOMM-481229`, `prod190222` and `fprod1280750` all address the same kind of thing and all slot into the same url. ids are opaque strings, never integers, and `product_id`, `sku_id`, `catalog_ref_id` and `sms` are four different values for one product. sale point codes are alphanumeric too, and `0000GP` sits beside `005290`.

**the slug is not load-bearing.** `get_product` asks for a placeholder slug and follows the redirect carrefour answers with, and `get_category` asks for one the category page answers directly. carrefour redirects an unknown id out of the food catalog instead, to the home page or to another business such as `/moda`, and any redirect that leaves `/supermercado/` raises `NotFoundError`.

**categories come from two places.** the navigation menu answers one node's children per request, names them in the client's language, and stops at the aisles. a leaf is not in it at all. so `get_categories` reads the departments from the menu, and `get_category` reads one node, its ancestors and its children off the node's own listing page. that page's navigation strip lists a node's children, except on a leaf, where it lists the leaf and its siblings. the client reads a strip that holds the node itself as "no children". the offers entry at the top of each department links to a filtered listing rather than a category page, so its `slug_path` is empty.

**two price vocabularies.** the search index sends numbers, and every rendered page sends localised strings like `"7,29 €"`. the client parses both into `Decimal`, but a summary that came from a search has no `app_price`, `stock`, `restrictions` or `promotions`, because those exist only in rendered pages.

**listing pages page in twenty-fours.** the url cannot set `page_size`, and upstream floors `offset` to a multiple of 24. the page prints two disagreeing totals. `product_count` reports the inner one, which is the one paging follows.

**sponsored rows.** the client never reads the ad carousels beside a grid. a card ad-injected into the grid itself keeps its ad id and is flagged `is_sponsored`.

**the sitemaps are challenged.** since october 2026 every carrefour sitemap, on either host, answers a non-browser client with cloudflare's interactive challenge while the pages and images beside them do not. the client therefore has no sitemap walk and no `sitemap_interval` option.

**the home page is a cms page.** its carousels arrive already filled, in page order, under the rendered state's `cms.featured_products`, and each one's title, internal name and departments live in the cms document it was built from. the `auto-ofers-*` carousels are the offers of the departments in `category_ids`. the home page is also the warm-up page, so a cold `get_home` fetches it twice.

**three languages, three indexes.** `es`, `ca` and `en` are separate indexes with very different coverage rather than translations of one another, so a catalan search finds a fraction of what a spanish one does, and `suggest` returned nothing in either of the other two in october 2026. the navigation menu follows the language. rendered pages are spanish whatever the language, and the labels inside a nutrition block are spanish free text rather than a stable vocabulary.

**analytics beacons.** every search document carries a `tagging` object of click and add-to-cart urls. those are writes, not reads. no parser copies them onto a model, and the client never follows one.

**the search index has no unit price, so the client computes one.** each document sends `measure_unit` (`kg`, `l`, `ud`, `docena`, `lavado` or `m`) and, as a number, how much of it one selling unit holds in `unit_conversion_factor`. `price.reference` is `active_price` divided by that factor, so 24 eggs at 5,25 € become 0.2188 per piece. `unit_price` stays `None` on a search result, and a variable-weight document gets no reference, because its factor is a `0.001` placeholder. listing cards and product pages publish `price_per_unit`, which the client reads as published. wine is priced per bottle (`ud`) rather than per litre, so it doesn't compare with stores that price wine per litre.

## non-goals

- **changing the session's sale point.** carrefour exposes no resolver for it, and the storefront sets it with a state-changing request. supermercapy sends the sale point per request to the index instead, which prices correctly but leaves `get_stock` reading the session default.
- **the proxied search as a general search path.** it adds merchandising facets and sponsored rows, ignores `store`, and misbehaves silently above 95 rows. only `get_stock` uses it, and only because nothing else publishes stock.
- **the cellar and non-food catalogs.** `catalog="cellar"` and `catalog="nonfood"` reach the index, but the parsers and the category tree are the food ones.
