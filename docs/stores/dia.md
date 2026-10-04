# dia

`supermercapy.dia.Dia` reads dia's online shop at `www.dia.es`. everything it calls is anonymous json under `/api/v1/`, with no key and no token, and it answers the library's own user agent.

the server keys the shop on a session cookie, and that session holds the delivery postcode and the language, the two things worth choosing. the client sets both on its own anonymous session, the way the site does when a visitor types a postcode or picks a language, and never touches a cart, an account or a checkout.

a postcode changes what the shop lists and how many units it has in stock. it rarely changes a price. across 871 products listed in both madrid (28041) and barcelona (08001) in october 2026, one price differed. an unbound client sees the storefront's default postcode, which was 28041 in october 2026.

```python
from supermercapy import Dia

with Dia.from_postal_code("08001") as dia:
    dia.physical_store_id  # "959"
    page = dia.search_products("leche", page_size=50)
    for product in page.products:
        product.price.amount, product.price.reference, product.promotions

    milk = dia.get_product(page.products[0].id)
    milk.nutrition.per, milk.nutrition.values[0]

    category = dia.get_category("L2051")
    offers = dia.get_offers()  # about thirty requests, see below

with Dia(language="ca") as dia:
    dia.search_products("llet")
```

`Dia()` binds nothing and costs no request to build. `Dia("08001")` binds a postcode without checking it first, and a postcode dia does not deliver to then raises `OutOfCoverageError` at the first request.

## capabilities

| capability | supported | how |
|---|---|---|
| `POSTAL_CODE` | yes | `from_postal_code` runs the storefront's delivery check, then moves the session to the postcode |
| `CATALOG` | yes | `iter_catalog` walks every top-level category's listing, twenty rows a request |
| `NEW_ARRIVALS` | yes | `get_new_arrivals` walks the storefront's own "novedades" category |
| `OFFERS` | yes | `get_offers` walks the offer listing category by category |
| `NUTRITION` | yes | the product sheet carries ingredients and a nutrition table; allergens are read from the bold words in the ingredients |
| `PROMOTIONS` | yes | rows carry their shelf-label promotions, with a club dia flag |
| `STORES` | no | there is no store list; the delivery check names one store id and nothing else about it |
| `EAN` | no | no barcode appears in any response, and a search for one finds nothing |
| `EAN_LOOKUP` | no | same |
| `HOME` | no | the home page is html, and akamai refuses html pages to a library client |
| `FUTURE_PRICES` | no | no price or promotion carries a date |

## extensions

| method | what it does |
|---|---|
| `get_category_products(category_id, cursor=)` | one page of a category's listing, twenty rows, with a cursor for the next |
| `get_category_offers(category_id)` | every offer in one category, top-level or not |

the product models add what dia sends beyond the shared fields. `DiaPrice.is_club_price` marks a club dia price. `DiaPromotion` adds `short_description` and `is_online_only`. `DiaProduct` adds `is_dia_brand`, `brand_type`, `stamp` and `stamp_code` (the badge on the photo, such as "novedad"), `net_content`, `info_labels`, `manufacturer` and `manufacturer_address`. `DiaNutrition` keeps the energy line apart as `energy_kcal` and `energy_kj`, and `DiaAvailability.units_in_stock` is the bound store's stock.

## request counts

| call | requests |
|---|---|
| `Dia()` or `Dia("08001")` | none until the first call |
| first call of a client bound to a postcode | one more, to move the session |
| first call of a catalan or english client | one more, to set the language |
| `from_postal_code` | two: the delivery check and the move |
| `search_products`, `get_product`, `get_categories` | one |
| `get_product` for an unknown sku | two: the sheet, then one search to confirm |
| `get_category`, or `get_category_products` without a cursor | two: the tree and the page |
| `get_category_products` with a cursor | one |
| `get_new_arrivals` | one for the tree and one per twenty products: three in october 2026 |
| `get_offers` | one, then one per category with offers and one per extra page of twenty-four: thirty for 243 rows in october 2026 |
| `iter_catalog` | one for the tree and one per twenty rows of each child category. a full walk on 4 october 2026 took 456 requests, 4.1 mb and under four minutes, and yielded 6,841 rows for 5,863 distinct products, because a product can sit in more than one category. `get_catalog` keeps each once |

the default pacing is half a second between requests.

## quirks

**the session is the binding, and it lapses.** the session cookie expires an hour after its last use, and the server then starts a fresh one on its default postcode and locale without saying so. the client restates the postcode and language every 45 minutes. search, the product sheet and every listing echo the session's postcode or locale, and when one comes back different the client restates the session and repeats the request once. if it still differs, the client raises `InvalidResponseError` instead of returning a page priced for somewhere else.

**the language lives on the session too.** the server ignores `accept-language`. the client sends `PATCH /api/v1/common-aggregator/current/locale` for catalan or english. the search index is per language, so a catalan client searching "leche" finds two products where "llet" finds them all. category paths are translated (`/ca/fruites/c/L105`), and the client takes them from the tree it fetched in the same language. dia also serves portuguese, which `Language` does not have.

**search pages are thirty to a thousand rows, and fifty pages deep.** the server raises a smaller `page_size` to thirty without saying so, and the edge refuses a `page` above fifty with an html "bloqueado" page. the cursor is a row offset, and the client picks the narrowest page that holds the rows asked for and trims it, so any `page_size` from one to a thousand comes back exact and deep pages stay under page fifty. no query reaches rows past the fifty-thousandth, and no search came near that in october 2026.

**an unknown sku is a server error.** the product sheet answers a sku that does not exist with http 500 and `{"code": 500}`. the client does not retry it, and confirms with one search for the sku. no hit raises `NotFoundError`, and a hit raises `TransportError`, because then the sheet really failed. the sheet still answers for a product the bound store does not stock, with zero units.

**the sheet has no brand, and rows have no nutrition.** listing rows carry `brand`, the product url, the stamp and the own-brand flag. the sheet carries none of them but adds photos, the breadcrumb, ingredients, the nutrition table, storage and preparation text. one `DiaProduct` holds either.

**the nutrition table says what it is per.** `nutri_size` and its unit, usually 100 g or 100 ml, become `nutrition.per`, and the rows go to `per_100` when that is a hundred and to `per_serving` otherwise, including when the sheet does not say. the client ignores the sheet's `value_per_100_g`, which disagrees with `value` on a table already per 100 ml and restates milligrams as grams.

**a top-level category is listed child by child.** its listing walks each child category in turn, twenty rows at a time, so its first page holds the first child's products, and `product_count` is the whole category's. the cursor is the storefront's own next step. "novedades y recomendados" (`L128`) has no listing of its own. the storefront redirects it to its first child, so `get_category("L128")` comes back without products.

**each top-level node lists itself as a child.** the menu repeats every top-level category as its first child ("todo frutas" under "frutas", same id). the client drops that entry, so the tree has two clean levels.

**new means the "novedades" category.** `get_new_arrivals` walks `L2302`, the storefront's listing of new products, whose rows are all stamped "novedad". the same stamp turns up on rows elsewhere and sets `is_new` there. in october 2026, fifteen searches found 57 rows stamped new and the category held 38. the rest of the "novedades y recomendados" group (best rated, air fryer, oktoberfest) is curated, not new.

**offers are grouped by category.** the offers page shows ten rows per category and chains to the next pair of categories. each category's own offer listing holds all of its offers in pages of twenty-four and names the next category with offers, so `get_offers` reads the first category from the offers page and follows that chain. the same product can be on offer in a curated group and in its own category, and `get_offers` returns it once.

**club dia prices are what an online shopper pays.** a promotion flagged `only_club_dia` sets `member_only`, and its price sets `price.is_club_price`. an online order needs a club dia account, so the client keeps that price as `price.amount` and the struck-through one as `price.previous`. a multi-buy promotion ("2ª ud al 50%") leaves the price alone and carries no amount.

**unit prices.** `measure_unit` reads `LITRO`, `KILO`, `UNIDAD`, `100 ML.`, `100 GR.`, `LAVADO`, `DOCENA` or `METRO`, all in the shared vocabulary, so `price.reference` restates every one per kilogram, litre, piece, dose or metre. the unit price is the one the shopper pays, promotion included.

**loose produce is sold by the piece at an average weight.** a row with `average_weight` (a banana, "900 g aprox.") is `is_variable_weight`, and its price is `is_approximate`.

**a shared cache cannot tell sessions apart.** the postcode and the language live in the session cookie, and a `CacheTransport` leaves cookies out of its key, so two dia clients bound to different postcodes or languages would read each other's pages from one cache. give each binding its own cache. a client whose cached search no longer matches its postcode raises `InvalidResponseError`. a cached category listing echoes no postcode, so the client cannot catch it.

**two refusals, both blocks.** akamai answers the html pages with http 403 "access denied", and the edge answers a search page above fifty with an html 404 "bloqueado". both raise `BlockedError` after one request, and the client never retries them. the json api's own 404s are json, or empty, and raise `NotFoundError`. akamai bot manager sets its cookies on the first response, but about three hundred json requests in october 2026 met no challenge and no rate limit.

**robots.txt.** it disallows `*/search/reduced?*`, which matches every `search_products` request (`/api/v1/search-back/search/reduced?q=leche`), and so every `get_product` that confirms an unknown sku. the other paths the client uses, the menu, the delivery check, the session calls, the listings, the offers and the product sheet, are not disallowed. the client sends no `?filters=` or `?sort=` parameter.

## non-goals

- **carts, accounts and checkout.** the session moves to a postcode the way the site moves it for an anonymous visitor, and nothing else on it changes.
- **filters, sorting and suggestions.** robots.txt asks crawlers to stay off `?filters=` and `?sort=`, and the client leaves the facets and suggestions in a search answer unread.
- **the html site.** the home page, recipes and store finder are html behind akamai's refusal.
- **portuguese.** dia serves it, but `Language` has no member for it.
