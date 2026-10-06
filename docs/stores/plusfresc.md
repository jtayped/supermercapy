# plusfresc

`supermercapy.plusfresc.Plusfresc` reads plusfresc's online shop, the lleida supermarket chain that also delivers around barcelona and tarragona. one api answers everything, and it asks for a credential, a guest token the client mints for itself.

a client binds to a preparation center, the kitchen and warehouse that assembles an order. the center decides the assortment. prices looked the same across the two centers compared during reconnaissance, but only one category was checked, so do not assume they are national. center `12`, lleida, is the storefront's own default and supermercapy's.

```python
from supermercapy import Plusfresc

with Plusfresc.from_postal_code("25001") as plusfresc:
    plusfresc.center  # 12
    page = plusfresc.search_products("llet", page_size=50)
    page.truncated, page.total_hits

    milk = plusfresc.get_product(page.products[0].id)
    milk.name, milk.nutrition.values, milk.promotions

    plusfresc.get_catalog_version()  # date(2026, 9, 14)
    offers = plusfresc.get_offers(highlighted=True)

with Plusfresc(center=12, language="es") as plusfresc:
    for product in plusfresc.iter_catalog():
        product.item_id, product.price.amount
```

## capabilities

| capability | supported | how |
|---|---|---|
| `POSTAL_CODE` | yes | `from_postal_code` asks which center serves a postcode, in one request |
| `STORES` | yes | `list_stores` returns every store and pickup locker, or those of one postcode's center |
| `CATALOG` | yes | `iter_catalog` reads the whole catalog from one listing of the root node |
| `NEW_ARRIVALS` | yes | `get_new_arrivals` returns the category the storefront collects under "novetats" |
| `OFFERS` | yes | `get_offers` returns the web-offers category, or the promoted carousel |
| `NUTRITION` | yes | the extended product sheet carries ingredients, allergens and a coded nutrition table |
| `PROMOTIONS` | yes | markdowns and multibuys, with their windows and combinable items |
| `EAN_LOOKUP` | no | no endpoint takes a barcode, because no response carries one |
| `EAN` | no | the storefront publishes no barcode for any article |
| `HOME` | no | the home page is a cms layout with no product payload of its own |
| `FUTURE_PRICES` | no | a promotion has a window, but no price is published before it applies |

## extensions

| method | what it does |
|---|---|
| `centers()` | the eight preparation centers, without their lockers |
| `get_catalog_version()` | the date the catalog last changed, which is the only cache key on offer |
| `get_category_products(category_id, ...)` | one page of a category, descendants included |
| `get_units()` | the unit-of-measure codes a product's `unit_measure` uses, in the selected language |
| `download_photo(photo, path, size=)` | one image at one of the two fixed renditions |

`get_offers` takes `highlighted`, which swaps the web-offers category for the smaller carousel of featured campaigns. `get_category` takes `page_size`.

## quirks

**the client mints its own token.** the catalog, the category tree and the postcode lookup are public. search, product details and the extended sheet need a guest token, which the client obtains with an unauthenticated post against the bound center, injects as a bearer header, and refreshes a minute before the token's own expiry. a token whose expiry cannot be read falls back to a thirty minute lifetime. only the api host gets the header. product images live on the storefront host and `download()` takes any url, so neither ever sees the token.

**a 401 buys exactly one re-mint.** the client mints a new token and retries the request once. a second 401 raises `AuthenticationError` instead of looping, and so does a mint that comes back blank or is itself answered with a 401. a 401 from any host other than the api is an ordinary `TransportError`.

**requests are paced by default.** `default_min_request_interval` is 0.5 seconds here, the pace the storefront was measured to tolerate. pass your own `min_request_interval` to change it.

**nothing paginates.** no endpoint takes a page, a size or a sort parameter. search answers with at most a hundred rows, and a listing answers with the whole category. the client therefore carves every page out of one response, and the cursor is an offset into rows it already holds.

**exactly a hundred rows means the answer was cut short.** the search cap has no marker of its own, so a full page sets `truncated`, which means `total_hits` is a floor and that query cannot reach the remaining matches. walk the category tree instead. a category listing has no cap, so its pages never set `truncated`. the client rejects a query shorter than two characters before any request, because the backend needs two.

**the query travels in the path.** iis refuses to route a path segment holding `/`, `\`, `+` or a trailing dot, and asp.net refuses `%`, `&`, `:`, `*`, `?`, `<` and `>`, so the client sends each of those as a space and drops trailing dots. `query` on the result keeps what you passed.

**the key is the item id, not the placement id.** a listing row calls its composite placement identifier `id`. upstream refuses that identifier in `get_product`, which then raises `NotFoundError`. `ProductSummary.id` is the six-digit item id, and `PlusfrescProduct.item_id` keeps it under its own name.

**the catalog is one multi-megabyte response.** `iter_catalog` asks the root node for its listing and gets every product of the center back in a single uncached payload of several megabytes, for one request and no token. a product placed under more than one category arrives once per placement, and `get_catalog` deduplicates by id. gate repeated walks behind `get_catalog_version`, which is the only thing here worth caching against.

**the tree carries no products.** `get_category` therefore costs two requests, one for the node and one for its listing. the node endpoint names no parent either, so its `parent_id` is `None`. `get_categories` sets every parent. ids are mostly positional, but a few nodes sit under a parent whose id is not their prefix, so the client never guesses a parent from an id.

**promo categories have no name.** the pseudo-categories the storefront uses for campaigns leave the display name empty and open their description with the headline instead, so the parser falls back to that headline, and then to the id.

**a multi-buy's quantity is read, a bundle's is not.** on a `CategoMxN_` campaign `requires_quantity` is the number of units the deal needs: 2 for "la 2ª ut. al 50%" or "2 per 1", 3 for "3 per 2". in october 2026 that held for all 281 such rows seen. the `CategoXXX_` bundles, which the storefront labels only "promoció", send 0, 1000 or 2000 in the same field, sometimes different values for the same item in the same bundle, so the client leaves `requires_quantity` as `None` for them.

**prices are integer cents.** they become exact `Decimal` euros without touching a float. a reduced price equal to the listed one is not a discount, and the client does not report it as one. dates are day-month-year, never iso.

**free text stays spanish.** asking in catalan translates labels and category names, but the product sheet's prose comes back in spanish either way. nutrition units arrive as codes. the client labels the known ones, and an unknown one keeps its code with no label instead of a guess.

**a locker resolves to its center.** locker ids embed the center that prepares their orders, so you can pass every `id` that `list_stores` returns straight back to the constructor.

**unit codes are the ones `get_units()` lists.** `unit_measure` is `Kg`, `L`, `Un`, `M`, `Dot` (a dozen) or `Do` (a dose of detergent). `price.reference` keeps each per one unit and divides the dozen by twelve. `value_x_unit` already follows a reduced price, including the per-item price of a two-for offer.

## non-goals

- **customer accounts.** the guest token is the only credential supermercapy mints, and it is minted to read the public catalog, not to act as anyone.
- **barcodes.** no response carries one, so there is nothing to map `EAN` onto and no lookup to expose.
- **pretending the search cap is a total.** supermercapy reports `truncated` instead of stitching queries together to fake completeness.
