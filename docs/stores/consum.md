# consum

`supermercapy.consum.Consum` reads consum's online shop. one json api at `tienda.consum.es` answers everything, and it needs no cookie, token or key. two headers carry the whole session, `X-TOL-LOCALE` for the language and `X-TOL-ZONE` for the zone, and the client sets them on the connection pool.

a zone decides the assortment, not the price. it narrows the catalog to what one store carries, and prices are the same nationwide. binding one is optional, but an unbound client does not see a superset. it gets a default assortment of its own, which in october 2026 listed 9,084 products against 9,324 in zone 147.

```python
from supermercapy import Consum
from supermercapy.consum import ProductFilters, SortOrder

with Consum.from_postal_code("46001") as consum:
    consum.zone  # 147
    page = consum.search_products(
        "arroz",
        page_size=50,
        order_by=SortOrder.PRICE_ASC,
        filters=ProductFilters(offer_immediate=True),
    )
    for summary in page.products:
        summary.price.amount, summary.promotions

    rice = consum.get_product(page.products[0].id)
    rice.ean, rice.price.unit_price_unit  # "1 Kg"

    everything = consum.get_products(["1234", "5678"])
    by_barcode = consum.get_product_by_ean("8480000000000")
    # every page, a hundred rows per request: thirteen in october 2026
    offers = consum.get_offers(include_deferred=True)
```

## capabilities

| capability | supported | how |
|---|---|---|
| `POSTAL_CODE` | yes | `from_postal_code` reads the shipping areas of a postcode and binds the first one |
| `STORES` | yes | `list_stores` returns the areas serving one postcode for home delivery; pickup answers empty, see below |
| `CATALOG` | yes | `iter_catalog` walks the listing endpoint a hundred rows at a time |
| `EAN_LOOKUP` | yes | `get_product_by_ean` filters the listing on an exact barcode, in one request |
| `NEW_ARRIVALS` | yes | `get_new_arrivals` walks every product the storefront flags as new, newest first |
| `OFFERS` | yes | `get_offers` walks every product carrying an immediate discount, a hundred per request |
| `EAN` | yes | every product row carries its barcode |
| `PROMOTIONS` | yes | offers and coupons become `Promotion` records, with their windows |
| `HOME` | no | the home page is rendered from cms blocks with no product payload |
| `NUTRITION` | no | no ingredient, allergen or nutrition field exists in any response |
| `FUTURE_PRICES` | no | a deferred offer says money comes back later, not that a price changes later |

## extensions

| method | what it does |
|---|---|
| `get_products(codes)` | several products in one request, in the order given |
| `get_category_products(category_id, ...)` | one page of a category, descendants included, with sorting and filters |
| `get_groups()` | the promotional campaigns and their nested subgroups |
| `get_group_products(code, ...)` | one page of a campaign, selected by its group code |
| `get_sort_orders()` | the sort orders the storefront currently advertises |
| `suggest(query, limit=)` | spelling completions for a partial query |
| `suggest_tags(query)` | the refinement tags the search box would offer |
| `download_photo(photo, path, size=)` | one image at one of the three real renditions |

`search_products` and the two listing extensions take `order_by` (a `SortOrder`), `filters` (a `ProductFilters`) and `include_filters`, which asks the storefront to return the facet groups alongside the rows.

## quirks

**the zone is a filter, not a price band.** it changes what is listed, never what a listing costs. no zone sees everything. the unbound default, zone 147 and zone 207 each list products the others do not.

**an unknown zone is not an error.** a zone the storefront does not recognise answers http 200 with a catalog of its own. zone 99999 listed 2,661 products in october 2026, where it had listed none in september. resolve a zone through `from_postal_code` or `list_stores` instead of guessing, and treat an unexpectedly small result as a binding problem.

**"not here" and "no such product" are the same 404.** a product the bound zone does not carry and a code that never existed both answer http 404 with error type `44`, so the client raises `NotFoundError` for both and names the zone in the message. it never raises `NotAvailableError`, because it cannot tell that the product exists anywhere else.

**pickup areas come back empty.** `list_stores(postal_code, method="T")` sends the request the storefront defines, and in october 2026 every postcode tried answered `[]`, as did the storefront's own unfiltered pickup query. home delivery (`"D"`, the default) is what `from_postal_code` binds.

**category ids are numeric.** the listing answers anything else with http 500, so `get_category_products` refuses a non-numeric id before sending it.

**the cursor counts rows, including the ones you never see.** the storefront injects retail-media rows into ordinary listings, and the client drops them by default while still advancing the offset by what the storefront sent. that keeps paging correct and is why a page can come back shorter than `page_size`. pass `drop_sponsored=False` to keep them, flagged `is_sponsored`.

**one page holds at most a hundred rows.** the storefront caps `limit` without a word instead of refusing a larger value, so `max_page_size` is 100 and a bigger `page_size` raises `ConfigurationError` here instead of quietly returning a hundred.

**unknown parameters are ignored, not rejected.** a misspelt filter produces a successful, unfiltered response, which is why the test suite asserts on the reported total, not on the http status.

**the stable id is the code.** products carry both an internal integer and a `code`. the code is what every endpoint accepts and what `ProductSummary.id` holds. `get_products` accepts up to fifty at a time and omits codes the zone does not carry, so compare what came back with what you asked for.

**`centAmount` holds euros.** despite the name, `0.84` means 84 cents, and it becomes `Decimal("0.84")` through its decimal text, not float arithmetic. an immediate offer replaces the shelf amount and keeps the original in `previous`. a deferred one credits money back and leaves the shelf price alone.

**the wire spells it `inmediate`.** the misspelling is upstream, and the parser reads the flag exactly as sent.

**two languages, one of them valencian.** `es` and `vl` are the storefront's own locales, sent in `X-TOL-LOCALE`. product attributes come back keyed by language and are flattened across all of them.

**filters are packed into one parameter.** `ProductFilters` renders the packed string the storefront expects and percent-encodes it. it rejects values that would corrupt the packing instead of sending them.

**the unit carries its own quantity.** `unitPriceUnitType` reads `1 L`, `1 Kg`, `1 U`, `1 M`, `1 Dc` (a dozen), `1 Lv` (a wash), `1 Do` (a dose), `100 Gr` or `100 ml`. `price.reference` multiplies the per-100 figures by ten and divides the dozen by twelve, and it reads the offer's unit price whenever an offer sets `amount`.

## non-goals

- **carts, coupons and accounts.** coupon objects parse, but without a session they are always empty, and no method here creates one.
- **nutrition.** consum publishes none. `Product.nutrition` is `None` for every product, which is what the missing `NUTRITION` capability says.
- **the home page.** it carries no product json worth parsing, so `get_home` stays unsupported instead of returning something half-true.
