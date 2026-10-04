# eroski

`supermercapy.eroski.Eroski` reads eroski's online supermarket at `supermercado.eroski.es`. the storefront runs apache tapestry 5 and renders every page on the server. it publishes no json api, so the client reads html: listing tiles, product pages and the navigation menu. [caprabo](caprabo.md) runs the same build on its own host, and both clients share one parser.

nothing binds. the storefront pins the anonymous session to shop `157` (named bilbondo), which sets the prices and the regional range. choosing another shop needs an account, because the delivery pages redirect an anonymous visitor to a login, so `store_id` is `None` and each product carries the `shop_id` that priced it.

```python
from supermercapy import Eroski

with Eroski() as eroski:
    page = eroski.search_products("aceite de oliva")  # one request
    for row in page.products:
        row.price.amount, row.price.reference, row.promotions

    oil = eroski.get_product(page.products[0].id)  # one request
    oil.nutrition.values[0].name  # "Energía"
    oil.manufacturer, oil.storage

    tree = eroski.get_categories()  # one request, about a megabyte
    fruit = eroski.get_category("2059699")  # one more: the menu is kept
```

## capabilities

| capability | supported | how |
|---|---|---|
| `CATALOG` | yes | `iter_catalog` reads the menu, then walks each root category's listing to its empty page |
| `NUTRITION` | yes | ingredients and the nutrition table from the product page's characteristics |
| `PROMOTIONS` | yes | multi-buy badges such as "3x2" and "2ª unidad -70%" become `Promotion` records |
| `POSTAL_CODE` / `STORES` | no | the shop follows the account; an anonymous session cannot pick one |
| `OFFERS` | no | `/es/supermercado/filter/offers/` redirects an anonymous visitor to a login |
| `NEW_ARRIVALS` | no | nothing marks a product new |
| `HOME` | no | the home page carries banners and no product tiles |
| `EAN` / `EAN_LOOKUP` | no | no barcode appears in any page |
| `FUTURE_PRICES` | no | prices carry no dates |

## extensions

| method | what it does |
|---|---|
| `get_category_products(category_id, cursor=)` | one listing page of a category, its subtree included, paged like search |

the product model adds `shop_id`, `is_marketplace` for third-party sellers, `is_lowered_price` for the lowered-price badge, and, from a product page, `manufacturer`, `manufacturer_address`, `alcohol_percentage` and `features`, every characteristics box as a `(title, text)` pair. categories add `path`, the slug path a listing is addressed by, and `url`.

## request counts

| call | requests | size |
|---|---|---|
| `search_products` | one per page | about 230 kb |
| `get_product` | one | about 900 kb |
| `get_categories` | one | about a megabyte |
| `get_category`, `get_category_products` | one, plus the menu once per client | about 230 kb |
| `iter_catalog` | the menu, then one per page of each root plus its empty page | hundreds of pages |

the sizes are the html after decompression. the storefront compresses its answers, so the wire carries about a tenth of that: twelve requests that decompress to 4.5 mb arrived as 330 kb in october 2026.

## quirks

**listings come from the infinite scroll's fragments.** a full listing page weighs a megabyte, most of it the menu. the client asks for the zone fragment the storefront's own scroll loads instead, `…:loadpage?…&t:zoneid=productListZone&pageNumber=N`, which answers json holding about twenty tiles as html for about 230 kb. a page past the end answers with no tiles, and that empty page ends a walk. a one-page category says it is finished on its first page, so it costs no empty page.

**pages hold about twenty rows and take no size.** `max_page_size` is 20. the client cuts a smaller `page_size` out of one storefront page, so the cursor names the storefront page and the rows of it already returned, `"3"` or `"3:5"`, and walking a small page size asks for the same storefront page again until its rows run out. a root category's first page sometimes holds 21. the storefront publishes no total, so `total_hits` is always `None`.

**the menu is the category tree.** every full page carries the whole navigation, about 1,500 nodes four levels deep, and each link's path spells the node's ancestry: `/es/supermercado/2059698-frescos/2059699-frutas/`. the client rebuilds the tree from those paths. a few nodes sit under two parents in the menu, and the tree keeps both placements, with the same id and a different `path`. the menu includes eroski's non-food roots, such as electrónica and electrohogar.

**a listing needs the full slug path.** a fragment addressed by a bare category id answers empty, and one addressed by the leaf's own segment left out a product the full path listed in october 2026. so `get_category` reads the menu first, once per client, and the client keeps the tree for later calls. `get_categories` always fetches a fresh one. an id the menu does not hold raises `NotFoundError`. no listing publishes a product count, so `product_count` is `None`.

**ids are opaque strings.** most are numeric, and marketplace products read `MP96290`. the storefront ignores the slug in a product url, so the client asks for `/es/productdetail/<id>-producto/`. an id with no slug at all, or an unknown one, redirects to `/es/error/404/`, which raises `NotFoundError`. product pages name no canonical url, so a product from `get_product` has no `slug`.

**errors inside fragments are redirects in json.** an event the storefront cannot serve answers http 200 with `{"_tapestry": {"redirectURL": …}}` instead of content. a redirect to the 404 page raises `NotFoundError`, and any other, such as the general error page a blank query gets, raises `InvalidResponseError`. the client refuses a blank query before sending it.

**the visible price wins over the analytics one.** each tile carries a ga4 `data-metrics` attribute with the id, name, brand, price and category ids. its price is a float that drops trailing zeros, so `price.amount` reads the price printed on the tile, and falls back to the analytics figure when the tile prints none. a struck-through price fills `previous`, and the percentage badge beside it fills `discount_percentage`.

**units.** the unit price reads `1 LITRO A 0,97 €` in every language. `KILO`, `LITRO`, `DOSIS`, `DOCENA`, `UNIDAD` and `METRO` are in the shared vocabulary, and `ROLLO` reads as one piece, so `price.reference` restates the figure per kilogram, litre, dose, piece or metre. most single-unit products print no unit price at all, and then `reference` is `None`.

**purchase limits come from a script.** the page's tapestry init script builds each add-to-cart button from options: the most a basket may hold, the smallest quantity, the pack size, whether the product is sold by weight, the seller and the shop. they fill `availability` and `shop_id`. produce sold by weight also shows a weight icon, and either source sets `is_variable_weight`.

**some products are filed nowhere.** a product page's breadcrumb sometimes stops at the home link, and the analytics event then names no category, so `category_path` and `category_ids` stay empty for those products.

**the client reads the nutrition table by position.** its first row states what the figures refer to, such as `100 mililitros`, and fills `nutrition.per`. figures against a hundred grams or millilitres land in `per_100`, anything else in `per_serving`. the manufacturer box alternates a bold label with its value, and the labels follow the language, so the first value is `manufacturer` and the second `manufacturer_address`.

**the user agent.** the edge refuses a few user agents, among them curl, python-requests and an empty one, with a bare 134-byte http 403. the edge accepts the library's own user agent, so it stays the default. a 403 raises `BlockedError` after one request.

**datacenter addresses get a recaptcha page.** google cloud armor answers a client it doubts with its "checking your browser" recaptcha page and http 200, in place of a page, a json fragment or a photo. on 4 october 2026 a hetzner server got it on every request, at eroski and caprabo alike, while a residential connection never did. the client recognises the page and raises `ChallengedError` after one request rather than trying to parse it, and it never tries to solve the challenge.

**pacing.** `min_request_interval` defaults to one second, because a catalog walk is hundreds of quarter-megabyte pages. eroski showed no rate limit or challenge in october 2026.

**languages.** the first path segment picks the language, `/es/`, `/ca/` or `/en/`. the menu and the page labels follow it, and product names stay spanish. the unit price text stays spanish in every language. eroski also speaks basque, galician and german, which `Language` does not hold.

**robots.txt.** eroski's robots.txt disallows `/*?t:ac=`, `/*?filter=`, `*/search/results/?q=`, `*/?zipCode`, the language switch, a chatbot event and session ids. the client sends category listings to `/es/supermarket:loadpage?t:ac=…`, which the `/*?t:ac=` rule covers. searches go to `/es/search/results:loadpage?q=…`, which does not match the search rule's path literally but is the same search. the owner chose to use these paths.

## non-goals

- **choosing a shop.** the anonymous session cannot, and `?zipCode=` changes nothing. logging in to pick a shop is out of scope.
- **offers and filters.** the offers page needs a login, and the filter menu stores its state in the session through tapestry events. the client never changes session state.
- **reviews.** product pages carry customer reviews and ratings. the client does not parse them.
