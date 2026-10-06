# mercadona

`supermercapy.mercadona.Mercadona` reads mercadona's online shop. it is the store supermercapy started from. the client is a port of [mercapy](https://github.com/jtayped/mercapy), rebuilt on the shared base client, and it keeps that library's behavior.

two backends sit behind it and neither needs a credential:

- **the storefront api.** `tienda.mercadona.es` serves products, categories, the home page and the seasonal collections as json. every request carries the warehouse and the language as query parameters, so one client is one warehouse in one language.
- **the search index.** searching is a post to mercadona's algolia instance, addressed by an index named after the warehouse and the language. the application id and search key are the public ones the storefront ships.

a warehouse is what prices and stocks everything. it is a short code such as `mad3`, two to sixteen letters or digits, normalised to lowercase. it is required, because there is no national view.

```python
from supermercapy import Mercadona

with Mercadona.from_postal_code("28001") as mercadona:
    mercadona.warehouse  # "mad3"
    page = mercadona.search_products("leche sin lactosa", page_size=20)
    page.page, page.total_pages, page.total_hits

    milk = mercadona.get_product(page.products[0].id)
    milk.ean, milk.details.legal_name
    milk.nutrition.ingredients
    milk.price.bulk, milk.price.unit_price_text

    for section in mercadona.get_home():
        section.layout, section.title

with Mercadona("mad3", language="ca") as mercadona:
    catalog = mercadona.get_indexed_catalog()
    catalog.reconciled, len(catalog.products)
```

## capabilities

| capability | supported | how |
|---|---|---|
| `POSTAL_CODE` | yes | `from_postal_code` asks the storefront to change postcode and reads the warehouse back from a response header |
| `STORES` | yes | `list_stores` resolves one postcode to the warehouse that serves it; there is no directory to list |
| `CATALOG` | yes | `iter_catalog` walks the category tree and requests every listed group |
| `NEW_ARRIVALS` | yes | `get_new_arrivals` reads the storefront's own novelties feed |
| `HOME` | yes | `get_home` returns the home sections in upstream order, products, seasons and notifications included |
| `EAN` | yes | the complete product carries one; a search summary does not |
| `NUTRITION` | yes | ingredients and allergens, on the complete product |
| `EAN_LOOKUP` | no | no endpoint takes a barcode; the index does not match one either |
| `OFFERS` | no | mercadona publishes no discounts feed, only a per-price `is_discounted` flag |
| `PROMOTIONS` | no | there is no promotion object anywhere in the responses |
| `FUTURE_PRICES` | no | a price is the current one, with no successor and no validity window |

## extensions

| method | what it does |
|---|---|
| `get_indexed_catalog()` | the complete search index, collected through partitions, with an upstream hit count to reconcile against |
| `get_season(season_id)` | one seasonal collection, the kind `get_home` links to |
| `download_photo(photo, path, width=, height=, fit=)` | one image re-rendered by the cdn |
| `MercadonaPhoto.url(width=, height=, fit=)` | the same url without any network access |
| `supermercapy.mercadona.resolve_warehouse(postal_code)` | one postcode to one warehouse, without building a client |
| `supermercapy.mercadona.discover_warehouses(postal_codes, max_workers=)` | the same over an iterable the caller owns, concurrently |

`search_products` takes one extra keyword, `top_level_category_id`, which limits a query to one top-level storefront category.

## quirks

**the postcode lookup is a write that answers in a header.** binding a postcode is an http `PUT` to the storefront's change-postcode endpoint, and the warehouse comes back in `X-Customer-Wh` rather than in the body. a response without that header raises `InvalidResponseError`, and `from_postal_code` closes the half-built client before the error escapes.

**the cursor is a page number.** search pages are zero-based, `page_size` runs from 1 to 1000, and `next_cursor` is the next page as a string, absent on the last page. `MercadonaSearchResult` also keeps `page`, `total_pages` and `processing_time_ms` for callers that want the index's own vocabulary.

**one query reaches at most a thousand hits.** the last page the index serves for a broader query has no cursor and sets `truncated`, while `total_hits` keeps the full count. that ceiling is why `get_indexed_catalog` exists. it partitions the index by top-level category, and where an index publishes no category facets it partitions by score ranges instead, halving a range until it fits under the cap. records with no score, or more tied scores than the cap, leave `reconciled` false rather than silently short. always check that flag before treating a collection as complete.

**`get_catalog` and `get_indexed_catalog` are different collections.** the first walks the storefront's category tree, which is one request for the tree plus one per direct child, and sees only what a category lists. the second reads the search index. they do not have to agree, and neither calls the product-detail endpoint.

**the category tree carries products.** a category response nests its children and their product summaries, so `get_category` costs one request. the summaries are listing data: no ean, no ingredients, no photos beyond the thumbnail.

**only the second level has a category page, and a root falls back to the tree.** upstream serves `/api/categories/{id}/` for second-level ids only. `get_category` with a second-level id costs one request. with a root's id, upstream answers http 404, so the client then fetches the category tree and, if the id is a root there, returns that root with its second-level categories as `children` and no products, for two requests in all. an id that is not a root either, such as one of the groups inside a category, also costs two requests and raises `NotFoundError`. the category endpoints no longer spell a `level`, so the client fills it from the position in the tree: `0` for a root, `1` for the category a second-level id returns, and `2` for its groups.

**new arrivals are new because the feed says so.** the items of `get_new_arrivals` carry no flag of their own, so the client sets `is_new` on every one of them. other listings report `is_new` only where an item carries `is_new_arrival`.

**a home notification's action is a url.** `HomeNotification.action` holds the `redirect_url` of the notification's button, and `HomeNotification.action_title` the button's `title`. an older response spelled the action as a bare path, which the client keeps as `action` with `action_title` left `None`, as it is when the notification has no action at all.

**three languages, three indexes.** `es`, `en` and `ca` each address their own algolia index and their own storefront translation. the catalan index of some warehouses has been published without category facets, which is the case `get_indexed_catalog` falls back on score ranges for. every index checked on 4 october 2026, in all three languages, was faceted.

**prices keep mercadona's own vocabulary.** `MercadonaPrice` adds `bulk`, `unit_size`, `pack_size`, `total_units`, `drained_weight`, `minimum_amount`, `increment_amount`, `unit_name`, `size_format`, `is_pack` and `is_new` beside the core fields. a variable-weight product prices per kilo and flags `is_approximate`.

**photos are filenames, not urls.** `MercadonaPhoto` keeps the `file_name` and builds an imgix url on demand, so the caller picks a size at call time and the response fixes none. invalid sizes and fit values raise `ValueError` without touching the network.

**the search key is public and static.** it is the one the storefront ships to every browser. it may rotate, which would look like an authentication failure on search alone while the storefront api keeps working.

**seven unit codes.** `reference_format` is `L`, `kg`, `ud`, `m`, `lv` (one wash of detergent), or `dz` and `dc`, which both mean a dozen eggs. `price.reference` turns them into per litre, kilogram, piece, metre and dose, dividing a dozen by twelve, and any other code leaves it `None`.

## non-goals

- **carts, accounts and orders.** the storefront exposes them, and supermercapy reads product data only.
- **postcode enumeration.** `discover_warehouses` resolves the postcodes you hand it and no more. deciding which postcodes to ask about, and at what rate, is the caller's responsibility.
- **a national catalog.** every response is warehouse-scoped, so "what does mercadona sell" is only ever answerable one warehouse at a time.
