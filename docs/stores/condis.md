# condis

`supermercapy.condis.Condis` reads condis's online shop, condisline, at `compraonline.condis.es` (`condisline.com` redirects there). two hosts answer it, and the client uses both.

the empathy search index at `api.empathy.co` serves search, the category listings, the novelty and offer facets, and the suggestions. it answers anonymous json, with no key and no cookie, behind cloudflare. the storefront at `compraonline.condis.es` is a next.js app that renders the product sheets and the category tree on the server. every page there first redirects a visitor without a session through an anonymous sign-in, which sets the session cookies. the client follows it once per client, as a browser does, and never logs in.

prices and assortment follow a picking centre, and every index request names one. a new anonymous session gets centre `718`, which is what `Condis()` reads. in october 2026 the centres differed in assortment, not in price. a search for "leche" found 416 rows at 718, 349 at 531 and 371 at 533, with the same price for every product two centres shared.

```python
from supermercapy import Condis

with Condis() as condis:
    page = condis.search_products("leche semidesnatada", page_size=10)
    for product in page.products:
        product.price.amount, product.price.reference, product.promotions

    milk = condis.get_product(page.products[0].id)
    milk.nutrition.values[0]  # valor energético, "63,00 kcal" per 100 g
    milk.category_path  # top level, family, leaf

    offers = condis.get_offers()  # three requests, see below

# eighteen requests to find and call the storefront's postcode action
with Condis.from_postal_code("08034") as condis:
    condis.picking_centre  # "531"
```

`Condis()` and `Condis("531")` cost no request to build. the index does not refuse a centre it does not know. it answers with an empty catalog, so a mistyped centre reads as an empty shop.

## capabilities

| capability | supported | how |
|---|---|---|
| `POSTAL_CODE` | yes | `from_postal_code` calls the storefront's own postcode action, which names the postcode's picking centre |
| `CATALOG` | yes | `iter_catalog` walks every top-level category of the tree, five hundred rows a request |
| `NEW_ARRIVALS` | yes | `get_new_arrivals` reads every row the index flags `is_novelty` |
| `OFFERS` | yes | `get_offers` reads every row on sale and every row on promotion, once each |
| `NUTRITION` | yes | the product sheet carries the ingredients, with the allergens in bold, and a nutrition table per 100 g |
| `PROMOTIONS` | yes | a row or sheet on promotion carries its multibuy, such as "segunda unidad 50%", as a promotion |
| `STORES` | no | the storefront publishes no list of centres; see the non-goals |
| `EAN` / `EAN_LOOKUP` | no | no ean, gtin or barcode exists in either source |
| `HOME` | no | not mapped; see the non-goals |
| `FUTURE_PRICES` | no | nothing forward-looking is published |

## extensions

| method | what it does |
|---|---|
| `get_category_products(category_id, page_size=, cursor=)` | one page of any node's rows, by offset, without reading the tree; an unknown id answers an empty page |
| `suggest(term)` | the index's suggestions for a partial term |
| `picking_centre` | the centre the client is bound to, the same value as `store_id` |

`search_products` also takes `filters`, a list of the index's own `"facet:value"` strings, each sent as one `filter` parameter: `"on_sale:true"`, `"on_promotion:true"`, `"is_novelty:true"`, `"filterCategory:c07"`, `"facetBrand:CONDIS"`, `"condis_brand:1"`, `"without_gluten:1"`, `"isEco:true"`. `get_category` takes `page_size` for its first page.

```python
from supermercapy import Condis

with Condis() as condis:
    page = condis.search_products("leche", filters=["on_sale:true"])
    condis.get_category_products("c07__cat00210003", page_size=50, cursor="50")
```

`CondisProduct` adds `section`, `family` and `variety` (the product's three levels of the tree, top first), `category_names`, the card's `badges` (such as the prime subscribers' discount and "marca propia"), `manufacturer`, `kcal`, `net_weight`, and the flags `is_own_brand`, `is_eco`, `is_prime`, `is_gluten_free`, `is_lactose_free`, `is_seasonal`, `is_delivered_in_48h`, `has_lowered_price` and `units_limited`. `CondisCategory` adds `external_id`, the node's own code. `CondisSearchResult` adds `offset`.

## binding

`Condis(picking_centre="718")` takes a centre id, a string or an integer of digits. `Condis.from_postal_code(postal_code)` asks the storefront which centre serves the postcode, the way its postcode picker does. that picker is a next.js server action, `fetchPostalCodeById`, called by an id that changes with every deployment and that only the page's scripts publish, so the client:

1. reads the home page, through the anonymous sign-in on its fresh session, for five requests.
2. reads the home page's scripts in the order the page lists them until one names the action, which took twelve scripts and about 1.4 mb in october 2026.
3. posts the postcode to the action, which answers with the postcode's centre.

a postcode condis does not deliver to answers with no centre and raises `OutOfCoverageError`. in october 2026 08034 resolved to 531 and 08870 to 533, and 28001 to nothing. the storefront's own postcode list held 280 postcodes, 216 of them in barcelona province, 41 in girona, 17 in lleida, 5 in tarragona and 1 in the balearics. if a deployment stops publishing the action, `from_postal_code` raises `InvalidResponseError`, and `Condis(centre)` still works.

there is no cheaper route to the action. the html never carries its id, every server action the storefront has sits in one script of about 600 kb, and next.js derives the ids from a build secret, so they cannot be computed. resolve a postcode once, keep `picking_centre`, and build later clients with `Condis(centre)`, which costs nothing extra:

```python
from supermercapy import Condis

with Condis.from_postal_code("08034") as condis:
    centre = condis.picking_centre  # "531", worth storing

with Condis(centre) as condis:
    condis.search_products("leche")
```

## request counts

| call | requests |
|---|---|
| `search_products`, `get_category_products`, `suggest` | 1 each |
| `get_product` | 1, about 95 kb |
| `get_categories` | 1, about 250 kb |
| `get_category` | 2: the tree, then the first page of rows |
| `get_new_arrivals` | 1 per 500 new products: 1 for 57 in october 2026 |
| `get_offers` | 1 per 500 rows of each facet: 3 for 385 on sale and 569 on promotion in october 2026, about 3.7 mb |
| `iter_catalog`, `get_catalog` | the tree, then 1 per 500 rows of each of the eleven top-level categories. a full walk on 4 october 2026 read 7,545 products in 26 requests, the sign-in included, and 27 mb in 13 seconds |
| `from_postal_code` | 18 in october 2026, about 1.6 mb, as above |
| a client's first storefront page | 4 more, the anonymous sign-in |

the index rows are heavy, about 12 kb each with the search analytics they carry. a five-row search page was 59 kb and a five-hundred-row page about 1.8 mb.

## quirks

- **the anonymous sign-in.** a storefront request without a session is redirected to `/api/proxy/signin?provider=anonymous`, then to `/api/anonymous/oauth2/authorize`, then to `/api/auth/callback/anonymous`, which sets an auth.js session cookie and redirects back to the page. the client follows those four redirects itself, at most six requests a page, and keeps the cookies for the rest of its life. the session is a throwaway anonymous customer, and the client neither reads nor changes anything about it. the index needs none of this.
- **the product sheet is a page.** the storefront renders `/<slug>/p/<id>/es_ES` on the server and streams the data in the page's react server components payload, where the client reads the `productInformation` object. the storefront does not check the slug, so the client asks for `/p/p/<id>/es_ES` and reads the real address from the page's canonical link into `url` and `slug`. an id the shop does not know answers http 200 with an "error 404" page and no product, which raises `NotFoundError`.
- **two price formats.** the index sends euros as numbers, `price.current` and `price.regular`, with `price.discounted` added on sale. the sheet sends cents, `list_price` and `sale_price`, where `sale_price` is zero unless the product is on sale. either way `amount` is what the shopper pays now and `previous` the regular price when it is higher.
- **units.** the unit price is display text after the price: `"0,99€/Litro"`, `"8,11€/Kilo"`, `"28,96€/Kg"`, `"1,25€/100 ml"`, `"2,99€/Unidad"`. the shared vocabulary reads every one, so `price.reference` restates them per litre, kilogram or piece. one row in about 490 had no unit price.
- **two kinds of offer.** `on_sale` marks a reduced price, which shows as `previous`. `on_promotion` marks a multibuy such as "segunda unidad 50%", "2ªu 70%,prod.-valor" or "llévate 6 y paga 5", which becomes a `Promotion` of kind `"promotion"` with the storefront's text. the same text field says "novedad" on new products that are not on promotion, so only a product flagged `on_promotion` gets one. a product can be both.
- **category ids are paths.** a node's id joins its ancestors' codes: `"c07__cat00210003__cat002100030003"` is leche entera under leche under bebidas, and the index browses by it. a row's `category_ids` hold its family, the second level, as the index sends it. a sheet names only its leaf's code, so the client places it with the branch of the tree the page renders around it, and its `category_ids` hold the leaf. `section`, `family` and `variety` name the three levels on both.
- **the offset ceiling.** the index refuses a `start` of 2,495 or more with http 400, so one query or facet reaches at most 2,494 rows plus a page. the client flags a search page against that ceiling with rows left as `truncated`. the largest top-level category held 1,541 rows in october 2026, and `iter_catalog` walks a category that holds more than the ceiling through its children instead.
- **images.** the index's `images` paths, `/images/catalog/large/<id>.jpg` on `cdn.condis.es`, answer http 404. the storefront names each image by file and asks the cdn for it at a size, `https://cdn.condis.es/fit-in/<width>x<height>/es/products/<name>.jpg`. the client uses 1920x1080, the size of the product page's zoom, about 90 kb an image.
- **bot protection.** cloudflare fronts the index and cloudfront with an f5 cookie the storefront. neither refused the library's own user agent or the half-second default pacing in october 2026, across about 140 requests in one day. a 403 from either raises `BlockedError`, and cloudflare's `cf-mitigated: challenge` raises `ChallengedError`. the client does not retry either.
- **robots.txt.** in october 2026 the storefront's robots.txt, itself behind the anonymous sign-in, disallowed only `/private/`, and none of the paths this client uses. `api.empathy.co` served no robots.txt (http 404).

## non-goals

- **a list of centres.** the storefront has no centre list. its postcode list, another server action, names 280 postcodes without their centres, and only the per-postcode action names one, so `STORES` is not declared. bind by postcode or by a known centre id.
- **the home page.** it mixes prismic cms banners with product carousels in another row shape, and `HOME` is not mapped.
- **catalan.** the storefront also renders product pages under `ca_ES`, and the client reads the spanish ones only.
- **carts, slots and accounts.** the client uses the anonymous session to read pages and nothing else, and nothing here adds to a cart, books a slot or logs in.
