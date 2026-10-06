# bonàrea

`supermercapy.bonarea.Bonarea` reads the bonàrea online shop. every read is a form-urlencoded post to `/{language}/shop/{action}` on `www.bonarea-online.com`, and every one of them answers with the whole result at once.

there is nothing to bind. the catalog, the prices and the stock figures are national, so no postcode, zone or store selects them. language is the only axis, and it is the one piece of session state the storefront keeps.

```python
from supermercapy import Bonarea

with Bonarea(language="ca") as bonarea:
    page = bonarea.search_products("pollastre", page_size=24)
    page.total_hits

    for product in bonarea.iter_search("pollastre"):
        product.name, product.price.unit_price_text

    chicken = bonarea.get_product("13*5361")
    chicken.nutrition.ingredients, chicken.nutrition.values
    chicken.characteristics, chicken.variants

    reduced = bonarea.price_drops("13*300*010*010")
    codes = bonarea.delivery_zones("25")
```

## capabilities

| capability | supported | how |
|---|---|---|
| `CATALOG` | yes | `iter_catalog` walks the tree and asks each listing node for its articles |
| `NUTRITION` | yes | ingredients, allergens and the nutrition table, read out of the label blobs the json carries as html |
| `POSTAL_CODE` | no | nothing is scoped by postcode, so there would be nothing to bind |
| `STORES` | no | the shop sells one national catalog; `delivery_zones` covers delivery, not selection |
| `EAN_LOOKUP` | no | no endpoint takes a barcode |
| `EAN` | no | no article carries one |
| `NEW_ARRIVALS` | no | there is no novelties endpoint; the `new_products` extension filters on a badge instead |
| `OFFERS` | no | there is no discounts endpoint either; `price_drops` filters on the reduced badge |
| `HOME` | no | the home page carries no product json |
| `PROMOTIONS` | no | a badge is the only offer signal, and it carries no campaign, price or window |
| `FUTURE_PRICES` | no | prices carry no validity range at all |

## extensions

| method | what it does |
|---|---|
| `new_products(category_id=)` | the articles badged as new, deduplicated by id |
| `price_drops(category_id=)` | the articles badged as reduced, deduplicated by id |
| `get_product_sheet(product_id)` | the storefront's pre-rendered product modal, as raw html |
| `delivery_zones(province, town=)` | the postcodes bonàrea delivers to inside one province |
| `download_photo(photo, path, width=, height=)` | one image, optionally resized by the cdn |

`new_products` and `price_drops` cost one request for one category, and a whole catalog walk when none is given.

## quirks

**the warm-up exists for search alone.** search is the one endpoint that ignores the language in the path and reads it from the session instead, so a cookieless client gets catalan answers to a spanish query. the client fetches the language's home page once to set that cookie, and only when a search is about to ask for it. browsing never pays for a warm-up, and a search that fails leaves the session unprimed so the next one warms again.

**nothing paginates.** the storefront takes no page, size or sort parameter, and answers a query with every hit and a category with every article. the client therefore slices pages itself, so walking a cursor re-requests the same response. use `iter_search`, which streams the single response it already has. it validates `page_size` and then ignores it, because there is nothing to fetch.

**a missing article is an http 200.** an unknown id comes back as `{"article": null}` with a success status, which the client turns into `NotFoundError`. an unknown product sheet is not empty either. it is a rendered "this product does not exist" page flagged only by `"success": false`, and the client treats it the same way, as it does an empty one.

**a missing category is an http 500.** an unknown listing reference makes the storefront throw, and it answers with asp.net's stock runtime error page. the client never retries that page, because asking again only throws again. it fetches the tree once instead, and raises `NotFoundError` when the id is not in it or re-raises the original `TransportError` when it is. any other server error is retried as usual.

**category links are anchored on the site.** the tree sends `categorias/...` and a breadcrumb `~/categorias/...`, asp.net's application-relative spelling. both are the same page at the site root, so `BonareaCategory.url` is the absolute `https://www.bonarea-online.com/categorias/...` either way.

**two spellings of one id.** `13*5361` and `13_5361` address the same article. the first is the wire form and the second is the url form. the client accepts both and translates them before the request, and `BonareaProduct` exposes the url spelling under its own name.

**one request answers a category and the tree.** every listing response embeds the whole category tree beside its articles, which is why `get_categories` asks for a blank listing, the cheapest way to get the tree, and why `get_category` costs one request, not two. a menu-level node resolves to children and no products, because only the level the tree calls a listing level returns articles.

**`product_count` counts rows, not the storefront's total.** the listing reports its own figure, which disagrees. supermercapy reports the number of articles the listing returned.

**the catalog walk is one request per branch.** the tree costs one request and each listing node one more, 491 of them in october 2026, at 200 to 300 kb each. an article listed under more than one category is yielded once per listing, and `get_catalog` deduplicates by id. the client paces bonàrea requests by default. `default_min_request_interval` is 0.5 seconds, so the walk takes at least four minutes of pacing alone. pass `min_request_interval` to change it, or `0` to disable it.

**a price drop has no previous price.** the badge is the only signal bonàrea publishes. there is no former amount, no percentage, no window and no campaign, which is why `PROMOTIONS` is not declared. the client keeps the raw `discount` field of the json unparsed, because its meaning is unknown.

**unit prices are display strings.** the numeric price is clean, but the per-kilo or per-litre figure arrives as prose such as a euro amount and a unit glued together, and the client parses it out of that text. the original stays in `unit_price_text`.

**the units are kg, l, u., m and d.** each may carry one trailing dot or two, `k.` is a kilogram too, and `d.` is a dose of detergent. `price.reference` reads all of them. `€/ml.` is on figures that are per litre, such as 12,17 € for 200 ml labelled 60,85 €/ml., and it was on all 44 priced rows that carried it in the october 2026 catalog, so `ml.` reads per litre. a row whose `unitPrice` is an empty string, common on loose produce, has no reference.

**badges are catalan in both locales.** the badge vocabulary is a fixed catalan constant whatever language you ask in. known ones become `supermercapy.bonarea.Characteristic` members, and an unknown one stays a plain string instead of being dropped.

**the label data is html inside json.** ingredients, allergens, storage and the nutrition table arrive as `<strong>`-headed blocks and html tables inside json strings. the client parses them with the stdlib and keeps the original blobs on the model as a fallback. headings arrive html-escaped in catalan, and the parser keeps the storefront's closing footnote out of the last section.

**names and variants arrive escaped.** an article name can be html-escaped in the json, and the client decodes it. a variant group is sometimes named in the language you did not ask for.

## non-goals

- **carts, sessions and stock reservations.** the client never touches a cart or session endpoint, and the test suite asserts it.
- **a promotions model.** mapping a badge onto `Promotion` would invent a campaign, a price and a window that bonàrea does not publish.
- **the product modal as the structured route.** `get_product` is the structured one. `get_product_sheet` is a documented fallback for anything the json omits and the modal renders, and it returns raw html with no parsing promised.
