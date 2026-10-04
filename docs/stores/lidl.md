# lidl

`supermercapy.lidl.Lidl` reads lidl's spanish storefront: the mail-order shop, the in-store grocery assortment, the weekly leaflets, and the store list. four unauthenticated backends sit behind it and none of them needs a cookie or a token. two want a header the client already sends: the schwarz stores api its static storefront key, and the search api its own media type.

lidl prices two things by two different geographies, and both travel inside every response:

- `zone`, one of `PEN`, `BAL` or `CAN`, prices the online shop.
- `region`, `0` for national and `1` to `59` otherwise, prices groceries and selects leaflets.

binding either one is a client-side choice that costs no extra request, so a client with no binding still works and reports the default price band.

```python
from supermercapy import Lidl
from supermercapy.lidl import ProductFamily

with Lidl.from_postal_code("08013") as lidl:
    lidl.region  # 26
    lidl.zone  # "PEN"
    offers = lidl.get_offers()
    upcoming = lidl.get_offers(week="next")
    drill = lidl.get_product("100399898")
    drill.brand, drill.price.amount, drill.zone("BAL")
    for leaflet in lidl.get_leaflets():
        leaflet.name, leaflet.offer_starts_on, leaflet.pdf_url

with Lidl(region=26, family=ProductFamily.GROCERY) as lidl:
    page = lidl.search_products("chocolate")
```

## capabilities

| capability | supported | how |
|---|---|---|
| `POSTAL_CODE` | yes | `from_postal_code` pages the schwarz stores api and binds the nearest store's region and zone |
| `STORES` | yes | `list_stores` returns all seven hundred-odd spanish stores, or the ones in one postcode |
| `CATALOG` | yes | `iter_catalog` walks the product sitemap and hydrates each id |
| `OFFERS` | yes | `get_offers` parses one grocery campaign page |
| `EAN` | yes | shop products carry a real gtin-13; grocery ones carry lidl's internal eight-digit code |
| `PROMOTIONS` | yes | lidl plus member prices and struck-through campaign discounts |
| `FUTURE_PRICES` | yes | next week's prices ship days before they go live |
| `EAN_LOOKUP` | no | no endpoint takes an ean |
| `NUTRITION` | no | no ingredient, allergen, or nutrition field exists in any public response |
| `NEW_ARRIVALS` | no | the surface is a cms page with no product json behind it |
| `HOME` | no | same |

## extensions

| method | what it does |
|---|---|
| `get_offers(week=)` | one of `current`, `next`, `weekend`, `weekend_next`, `permanent_cuts`, `other_brands` |
| `get_campaign_products(slug, campaign_id)` | any other `/c/<slug>/a<id>` campaign page |
| `get_leaflets(region=, store=)` | the leaflets on offer, with dates, pdfs, and thumbnails |
| `get_leaflet(identifier, region=, store=)` | one leaflet's pages, images, and hotspots |
| `iter_catalog_ids(family=)` | the sitemap ids alone, one request, no hydration |
| `search_products(..., family=, sort=)` | narrow a page to one assortment, or sort it |

you can bind `family`, either `shop` or `grocery`, on the constructor or pass it per call. it is a client-side filter over ids that already say which half they belong to, so it never costs a request.

## quirks

- **two ids per product.** a variant id is its parent plus three digits in both families, and the detail endpoint answers http 404 for one. `get_product` normalises the id before requesting.
- **region prices are keyed by price band.** `regionsPrices` is keyed by `regionPriceId`, not by region id, so the client resolves it through `regionsV2`. mainland spain currently uses a single band.
- **the canary regions publish no price at all.** they are billed with igic rather than vat and carry no `regionPriceId`, so a client bound to region 49, 50, 53, 54, or 55 reports no grocery price rather than the mainland one.
- **a grocery product may have no price.** lidl publishes this week's and next week's campaigns and permanent reductions, not a shelf-price list. about half the grocery items in the catalog carry no price on any given day.
- **a member price can be the only price.** when a band publishes only `currentLidlPlusPrice`, that is what `price` holds and `price.is_member_price` is `True`.
- **read vat from the band.** the bare `price` object reports `hasVat: false` for a number the band reports as `true`.
- **pack size and unit price are mostly free text.** `packaging_text` is a spanish display string such as `"6x70 g"` or `"A granel"`, and the client never parses it. `base_price_text` keeps `basePrice` verbatim. most products publish no unit price at all. the few that do send either a structured one, per `m` or `m²` on non-food items, which also fills `unit_price` and `unit_price_unit` as published, or a sentence such as `"1,55 €/kg"` on food, which leaves them `None`. `price.reference` reads both, and a sentence with more than one price, such as `"1 ud 6,77 €/kg / 2 uds 6,54 €/kg"`, gives `None`.
- **campaign pages are html.** grocery offers have no json endpoint, so the client fetches the page and reads each tile's `data-grid-data` attribute, the same gridbox a search hit carries. the pages run to two megabytes, so cache what comes back.
- **the search api speaks one media type.** it answers `Accept: application/mindshift.search+json;version=2`, the value the storefront's own bundle sends, and refuses a bare `application/json` with http 406. every other lidl.es endpoint still takes `application/json`.
- **the category tree comes one level at a time.** `get_categories` returns the shop's roots with no children, because the facet of an unfiltered listing does not expand them. `get_category` filters on one node, and that facet carries the path from the root down to it plus its own children, so walk the tree by calling it on each child.
- **the detail and the listing put the brand in different places.** a listing tile carries `brand` at the top level and a detail carries `info.brand`, and the client reads either.
- **the wildcard query is refused.** upstream indexes `q=*` but caps it at about a hundred rows and leaves out every grocery, so it looks like a catalog and is not one. the client refuses it with `ConfigurationError`. use `iter_catalog` instead.
- **one locale, four spellings.** `es_ES` for search, `/ES/es` for product detail, `es-ES` for the stores api, and `lidl/es-ES` for leaflets.
- **food leaflets carry no products.** only bazar leaflets fill `LidlLeaflet.products`. for food, the only machine-readable text is the per-page `keywords` bag of words. the grocery prices are in the campaign pages instead.
- **every leaflet says it is current,** including next week's, so filter on `offer_starts_on`. leaflet slugs embed the week and change every week. resolve one through `get_leaflets` instead of caching it.
- **the leaflet api reports unknown filters** in a `warnings` array instead of failing, so a typo in a filter silently widens the result.
- **the stores key may rotate.** lidl bakes it into a versioned bundle path, the same risk as a public search key.

## non-goals

- **per-store stock.** `/p/api/storestock` answers for every spanish store and every product with `UNKNOWN`. the feature is dark in spain, so no method ships.
- **the lidl plus app api.** it is phone-number and sms gated, scoped to receipts and coupons, and its one field that matters for the catalog, the member price, is already public here.
- **nutrition and nutri-score.** nutri-score appears only inside ai-written image alt text, which is not a data source.
- **image resizing.** there are no resize parameters. the five sizes are separate pre-rendered objects, reachable through `LidlPhoto.sized`.
- **response freshness.** lidl's edge caches search responses for five minutes, so a price can be that stale. the client does not try to bust that cache.
