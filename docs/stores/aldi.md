# aldi

`supermercapy.aldi.Aldi` reads aldi's spanish product catalog. aldi runs no online shop. `www.aldi.es` publishes the fixed assortment and the weekly offers with their prices, and nothing can be ordered. the site reads its products from a public algolia index with a search-only key it ships in its own javascript, and the client does the same. the category tree and the weekly offers come from the json the site embeds in its pages.

aldi prices by region, and each region has its own index: the península (`pen`), the balearic islands (`bal`) and the canary islands (`can`). in october 2026 the balearic index held the same 2,342 products as the península one with 138 prices different, and the canary one held 2,241 products, 274 of them its own, with 1,217 of the shared prices different.

```python
from supermercapy import Aldi
from supermercapy.aldi import Region

with Aldi() as aldi:  # the península
    page = aldi.search_products("leche", page_size=50)
    for product in page.products:
        product.price.amount, product.price.reference, product.price.valid_from

    grapes = aldi.get_product("995700")
    grapes.promotion_prices  # every dated price, including later ones

    dairy = aldi.get_category("lacteos-y-huevos")
    milk = aldi.get_category("lacteos-y-huevos/leche-y-bebidas-vegetales")
    for group in aldi.get_offer_groups():
        group.title, group.starts_on, group.ends_on

with Aldi.from_postal_code("35001") as canarias:  # no request
    canarias.region  # Region.CANARY_ISLANDS
```

## capabilities

| capability | supported | how |
|---|---|---|
| `POSTAL_CODE` | yes | `from_postal_code` maps the province to a region locally, with no request |
| `CATALOG` | yes | `iter_catalog` reads the region's whole index, a thousand records a request |
| `OFFERS` | yes | `get_offers` reads the region's weekly offers page |
| `PROMOTIONS` | yes | every time-limited price becomes a `Promotion` with its window and shelf label |
| `FUTURE_PRICES` | yes | records carry prices whose window starts days ahead, see below |
| `STORES` | no | prices follow the region, so a store would bind nothing |
| `EAN` | no | no record carries a barcode |
| `EAN_LOOKUP` | no | same |
| `NUTRITION` | no | no record carries ingredients, allergens or a nutrition table |
| `NEW_ARRIVALS` | no | the site has no novelties listing |
| `HOME` | no | the home page is cms content with no product payload of its own |

## extensions

| method | what it does |
|---|---|
| `get_categories(deep=True)` | the top-level categories with their children, one more request per category |
| `get_category_products(key, page_size=, cursor=)` | one page of the products filed under a category key, in one request |
| `get_offer_groups()` | the sections of the weekly offers page, each with its start, end and publication dates |

`AldiProduct` adds `promotion_prices`, `article_number` (aldi's shelf article number), `main_category_id`, `long_description`, `certificates` (the label images, such as "origen nacional"), `is_coming_soon`, `is_recall` and `region`. `AldiPrice` adds `promo_text`, the shelf label such as `"-24%"`.

## request counts

| call | requests |
|---|---|
| `Aldi()` or `Aldi.from_postal_code(...)` | none |
| `search_products`, `get_product`, `get_category_products` | one, to algolia |
| `get_categories()` | one page |
| `get_categories(deep=True)` | one page plus one per top-level category: 21 in october 2026 |
| `get_category` | two: the category page and one algolia query for up to a thousand products |
| `iter_catalog` | one per thousand records: three for the 2,342 of the península in october 2026 |
| `get_offers`, `get_offer_groups` | one page, which embeds every record it shows |

the client does not pace itself by default, because neither the site nor algolia showed bot protection or a rate limit.

## quirks

**a price can start tomorrow.** each record's `currentPrice` carries its own window, and the index moves a product to its next price before that price applies. on sunday 4 october 2026, 113 península records already carried prices from monday, and some carried a weekend price starting the friday after. `price.valid_from` says when a price starts. aldi publishes no price for the days before it, so the client does not invent one.

**aldi publishes time-limited prices ahead.** `promotion_prices` lists every dated price a record carries, and on 4 october 2026, 74 records carried a second window that starts when the first ends. this is what `FUTURE_PRICES` stands for. the site's own next-week offers page, `/ofertas-proxima-semana.html`, redirected to the current offers that day, so the client does not read it.

**ids differ between the tree and the records.** a category's `id` is its page path below `/productos`, such as `lacteos-y-huevos/leche-y-bebidas-vegetales`, and that is what `get_category` takes. a product's `category_ids` hold category keys, such as `leche-y-bebidas-vegetales`, which `AldiCategory.key` carries and `get_category_products` takes. the two differ more than that example suggests. the page `fruta-y-verdura/fruta` holds the key `frutas`, and a top-level page's key never appears on a record.

**a top-level category lists its children together.** the site's top-level pages show tiles for their children and no products, so `get_category` lists the products filed under any child key. a child page names the filter the site lists it with, and the client sends that filter as it is.

**hidden categories stay hidden.** the overview carries categories the site hides, such as an out-of-season "verano", and the client leaves them out as the site does.

**unit prices.** `basePriceScale` is a slug: `kg`, `l`, `100-ml`, `unidad`, `lavado`, `m-(metro)`, and pieces counted as `capsula`, `bolsita`, `toallita` and `panuelo`, which `price.reference` restates per kilogram, litre, piece, dose or metre. aldi sends `g` and `100-g` on prices that are per kilogram (1.59 € for 200 g, base price 7.95), so the client reads both as unknown, and `par` (a pair) has no shared unit. about two hundred records, most of them fruit and vegetables, carry no price at all, and their `price.amount` is `None`. a pack of exactly one litre or one kilogram usually carries no base price either, as the one-litre milks did in october 2026, and then `price.reference` is `None`.

**the islands from a postcode.** `07` maps to the balearic region, `35` and `38` to the canary one, and every other province to the península. aldi's region picker offers only those three, so ceuta (`51`), which has no aldi store, and melilla (`52`), which has one, get the península prices. that is the client's reading of the picker, not something aldi states.

**the key is pinned.** the search-only key is a constant, `ALGOLIA_API_KEY`, copied from the site's category bundle, where it has not changed since the reconnaissance. reading it from the bundle at run time would cost two more requests per client and depend on a hashed file name and minified code. a rejected key raises `AuthenticationError` naming the constant, and the live tests check the bundle still carries it.

**no languages but spanish.** the site serves spanish only.

**robots.txt.** it disallows `/bal/` and `/can/`, and the client reads `/bal/ofertas.html` and `/can/ofertas.html` for the island regions' offers. everything else it reads, the overview and category pages, the península offers and the algolia index, is not disallowed.

## non-goals

- **the leaflet and the store finder.** the leaflet is a pdf and a store binds no price.
- **the next-week offers page.** it redirected to the current week when the client was written, so there is no shape to read.
- **barcodes and nutrition.** aldi publishes neither, and the client does not guess them.
