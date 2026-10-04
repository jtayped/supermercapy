# alcampo

`supermercapy.alcampo.Alcampo` reads alcampo's online shop at `www.compraonline.alcampo.es`. it runs on ocado smart platform, the same as bonpreu's shop, and the two clients share their parsers and request flow: anonymous json from one host, no key, a category tree, product pages paged by a session-scoped token, a product sheet, and a promotions listing.

prices and assortment follow a region. a region is one fulfilment centre, and several click-and-collect points hang off each, 294 points in 82 regions in october 2026. a new anonymous session lands in the "vaguada" region in madrid, which is what an unbound client reads, at no extra cost. binding a client to a point moves its own anonymous session to that point's region, exactly as the storefront's region picker does, before the first request.

the same kind of aws waf as bonpreu's sits in front, and its budget is small. it counts requests to the product pages service, `/api/webproductpagews`, which serves search, the product sheets, the listings, the tree and the batch put. in october 2026 one address got seven or eight of those per half hour before they answered http 202 with an empty body, while the promotions listing, the suggestions, the session and the click-and-collect points kept answering. plan around it the way bonpreu's page describes: few requests, two seconds apart, and stop on a challenge, which raises `ChallengedError` with a `suggested_backoff` of half an hour.

the same waf refuses a user agent it does not take for a browser on most catalog paths. search and the product sheet answered the library's own `supermercapy/<version>` with cloudfront's http 403 "request blocked" page, which raises `BlockedError`, while the category tree and the suggestions answered it. so the default user agent is a browser string, as on bonpreu.

```python
from supermercapy import Alcampo

with Alcampo() as alcampo:
    page = alcampo.search_products("leche semidesnatada", page_size=10)
    milk = alcampo.get_product(page.products[0].id)
    milk.price.amount, milk.price.reference
    milk.nutrition.values[0].name  # "valor energético (kj)"
    stores = alcampo.list_stores("08019")  # the points in barcelona province

# the csrf page and one put move the session before the first call
with Alcampo(stores[0]) as alcampo:
    alcampo.region_id
    alcampo.get_offers()
```

## capabilities

| capability | supported | how |
|---|---|---|
| `STORES` | yes | `list_stores` reads every click-and-collect point and the region behind it, in one request |
| `NUTRITION` | yes | ingredients, allergens and the nutrition table, parsed out of the product sheet's html |
| `PROMOTIONS` | yes | every row carries its promotions, flyer windows included in their descriptions |
| `NEW_ARRIVALS` | yes | `get_new_arrivals` applies the storefront's own "nuevo producto" filter to the whole-shop listing, three hundred rows a request |
| `OFFERS` | yes | `get_offers` reads the first page of the bound region's promotions listing |
| `POSTAL_CODE` | no | see the quirks; no postcode resolver answers for this tenant |
| `CATALOG` | no | see the non-goals; the budget cannot pay for a walk |
| `EAN` / `EAN_LOOKUP` | no | no ean, gtin, barcode or sku exists anywhere in the api |
| `HOME` | no | the home page is cms copy |
| `FUTURE_PRICES` | no | nothing forward-looking is published |

## extensions

| method | what it does |
|---|---|
| `get_promotions(category_id=, retailer_category_id=, page_size=, cursor=, sort=, filters=)` | the region's promotions listing with paging and filters; `get_offers` is its first page |
| `suggest(term, limit=)` | the autocomplete suggestions for a partial term, for the bound region |
| `get_similar(product_id)` | the products the storefront offers as similar, resolved to full rows |
| `get_related(product_id, limit=, high_relevance_only=)` | the cross-sell products, resolved the same way |

`search_products`, `get_category` and `get_promotions` also take `sort`, one of `favorite`, `priceAscending`, `priceDescending`, `pricePerAscending` or `pricePerDescending`, and `filters`, a mapping of filter group id to one or more attribute ids, as on bonpreu. alcampo's own groups are `boolean` (`onOffer`), `brands`, and `dummyValue`, which holds `new` ("nuevo producto") and `alcampoBrandGeneric` ("marca alcampo"):

```python
from supermercapy import Alcampo

with Alcampo() as alcampo:
    page = alcampo.search_products("yogur", filters={"dummyValue": "new"})
```

## binding

`Alcampo(store)` takes a click-and-collect point: an `AlcampoStore` from `list_stores`, or its id. the first request then moves the client's own anonymous session to the point's region, the way the storefront's region picker does:

1. the home page, once per client, for the csrf token and the session's visitor id.
2. `PUT /api/customersessions/v2/sessions/active` with the point and its region, which answers with the session as it now stands.

a bare id costs one more request first, the point list, to learn its region, and `region_id` is `None` until then. the client checks that the session reports the region it asked for and raises `InvalidResponseError` otherwise. the client repeats the move after half an hour, since the anonymous session's cookies live an hour. an unbound client does none of this, and the move does not touch the product pages service the waf counts.

in october 2026 the same milk six-pack cost 5,28 € in vaguada, 5,04 € at diagonal mar in barcelona and 4,92 € in la laguna, tenerife, and a search for "leche" shared 23 of its first 30 rows between vaguada and diagonal mar.

## request counts

| call | requests |
|---|---|
| `search_products`, `get_product`, `get_categories`, `get_category`, `get_promotions`, `get_offers`, `suggest` | 1 each |
| `get_new_arrivals` | 1 per 300 new products: 1 in october 2026 |
| `list_stores` | 1, about 210 kb for every point in the country |
| `get_similar`, `get_related` | 2, the uuids then one batch put, plus the csrf page once per client |
| the first call of a bound client | 2 more, the csrf page and the move, plus the point list for a bare id; 1 more put every half hour after |

## quirks

- **no postcode resolver.** the storefront resolves an address to a region by geocoding it in the browser and sending coordinates, and the address services behind its own postcode lookups (`/api/address/v1/addresses/by-postcode`, `/api/address/v3/address-lookup/by-postcode`, and the geocoder at `/api/address/v3/address-geocoding`) answer every postcode with http 400 `webaddressws-3000` for this tenant, while the region check itself (`/api/ecomdeliverydestinations/v2/deliverability`) refuses a postcode without coordinates. so alcampo has no `from_postal_code`. `list_stores(postal_code)` narrows the points to the postcode's province, those in the postcode itself first, and the caller picks one.
- **new arrivals are a filter, not a category.** there is no novelties category. every listing offers a `dummyValue` filter group whose `new` attribute the storefront labels "nuevo producto", and on the whole-shop listing, with no `categoryId`, it selected 75 products in october 2026, every one flagged `isNew`. that listing reports a product count of zero, so the walk follows page tokens instead of a count.
- **a category's `product_count` is not the region's.** the frutas listing reported 235 products and listed 146, all decorated, with no `otherProductIds` left over. paging it at a hundred rows ends after 146 too.
- **the per-gram unit price is too coarse to use.** a few rows quote `fop.price.per.gram`, rounded to the cent: 0,90 € for 45 g reads 0,02 €/g. `unit_price` keeps the figure as published, but `price.reference` is `None` for it rather than a per-kilogram price that could be off by a quarter. `price.reference` restates every other unit (`EACH`, `PER_1KG`, `PER_LITRE`, `PER_DOZEN`) per piece, kilogram or litre.
- **the legal name and origin are table rows.** the sheet's `features` field is an html table. its "denominación legal del alimento" row becomes `legal_name` and its "país de origen" row `origin`.
- **ratings.** rows carry a review summary. `rating` is its average and `rating_count` the number of reviews, and `rating` is `None` while the count is zero rather than a rating of zero.
- **pickup point addresses are free text.** `AlcampoStore.address` is the storefront's own string, the client does not parse the town out of it, and a point without a postcode sends the literal `"EMPTY"`, which reads as `None`.
- **two kinds of 403, one kind of 202.** they work exactly as on bonpreu. the origin's 403 carries `requestid` and means a stale csrf token, which the client mints again before retrying once. the waf's carries none and raises `BlockedError`. a 202 with an empty body and no `requestid` is the challenge, and raises `ChallengedError`.
- **robots.txt.** in october 2026 alcampo's robots.txt disallowed `/basket`, `/delivery`, `/checkout`, `/sso-login` and `sortBy=` urls, and none of the paths this client uses.

## non-goals

- **the catalog.** the product sitemap lists more than fifty thousand products across two files, and its entries carry an id and a slug, not a name or a price. walking it means a sheet request per product, and walking the 3,653 leaf categories means thousands of listing requests. either is thousands of times the waf budget. `CATALOG` is not declared.
- **solving the challenge.** the browser satisfies it with aws's captcha script, which sets an `aws-waf-token` cookie. the client does not attempt to obtain, replay or forge one.
- **home delivery.** binding a home address needs its coordinates, from a geocoder this client does not have. a pickup point selects a region directly.
- **carts, slots and accounts.** the region move touches only the anonymous session, and nothing here adds to the cart, books a slot or logs in.
