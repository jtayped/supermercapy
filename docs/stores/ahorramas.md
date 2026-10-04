# ahorramás

`supermercapy.ahorramas.Ahorramas` reads ahorramás's online supermarket at `www.ahorramas.com`. the shop runs salesforce commerce cloud behind cloudflare and publishes no product api, so the client reads what the storefront's own scripts read:

- listings come from the html grid the "más resultados" button loads, `Search-UpdateGrid`, one tile per product with the prices, the unit price, the promotion callouts and the purchase limits.
- a product's record is the json its page asks for when the quantity changes, `Product-Variation`, which adds the photos, the promotion ids and dates, the general attributes and the description.
- the category tree is the menu every full page carries. the client reads the home page's.

nothing binds. the header offers a postcode box, and entering one stores a delivery postcode and a preferred shop in the anonymous session. in october 2026 the first 48 results for "leche" and every one of their prices were identical with no postcode, with `28001` and with `28903`, and the shop refused `08001` as outside the delivery area. so the postcode decides delivery, not the catalog, and `POSTAL_CODE` and `STORES` are not declared.

```python
from supermercapy import Ahorramas

with Ahorramas() as ahorramas:
    page = ahorramas.search_products("leche", page_size=24)  # one request
    for row in page.products:
        row.price.amount, row.price.reference, row.promotions

    milk = ahorramas.get_product(page.products[0].id)  # one request
    milk.photos, milk.general_info, milk.category_ids

    tree = ahorramas.get_categories()  # one request, about 790 kb
    lamb = ahorramas.get_category("cordero_y_cabrito")  # one more
```

## capabilities

| capability | supported | how |
|---|---|---|
| `CATALOG` | yes | `iter_catalog` walks the catalog's root category a hundred products at a time |
| `OFFERS` | yes | `get_offers` walks the menu's "ofertas" branch to its end |
| `PROMOTIONS` | yes | price drops and multi-buys become `Promotion` records with their price and dates |
| `NUTRITION` | no | the record has ingredient, nutrition and allergen fields, and all fifteen products sampled in october 2026 left them empty |
| `POSTAL_CODE` / `STORES` | no | a postcode picks a delivery shop and changes no price or product, see above |
| `NEW_ARRIVALS` | no | the record's novelty field was empty on every product sampled, and no category collects novelties |
| `HOME` | no | the home page carries no product tiles; its carousel is filled by a separate script request |
| `EAN` / `EAN_LOOKUP` | no | no barcode appears in any response |
| `FUTURE_PRICES` | no | a promotion's dates bound the price that applies now |

## extensions

| method | what it does |
|---|---|
| `get_category_products(category_id, page_size=, cursor=)` | one listing page of a category, its subtree included, without reading the menu |

the product model adds `category_names`, the names of the categories a product is filed under from the top down, `badges` such as `"SIN GLUTEN"` or `"PESO VARIABLE"`, `average_weight` in kilograms for products sold by weight, and, from a record, `general_info`, every filled general attribute as a `(label, value)` pair. categories add `path`, their address on the site, and `url`.

## request counts

| call | requests | size |
|---|---|---|
| `search_products`, `get_category_products` | one per page | about 11 kb per product |
| `get_product` | one | about 30 kb |
| `get_categories` | one | about 790 kb |
| `get_category` | one, plus the menu once per client | about 11 kb per product |
| `get_offers` | one per hundred offers, plus an empty page when the last is full | 1,000 to 1,500 offers in october 2026, a megabyte per hundred |
| `iter_catalog` | one per hundred products | a full walk on 4 october 2026 read 7,579 products in 76 requests and 88 mb, in nine minutes, because each page took about seven seconds to answer |

the sizes are the html and json after decompression. the shop compresses its answers, so the wire carries about a fourteenth of that: ten requests that decompress to 2.7 mb arrived as 190 kb in october 2026.

## quirks

**the grid is the listing.** `Search-UpdateGrid` answers html tiles and nothing else around them. it takes `q` or `cgid`, `start` and `sz`, so the cursor is an offset. the grid publishes no total, so `total_hits` is `None`, and it keeps its "more results" button on its last page, so a page shorter than `page_size` is the end and a full last page costs one empty request more.

**pages can be large.** the storefront answered 300 tiles in one 3.9 mb response, so it caps nothing a client is likely to ask for. `max_page_size` is the client's own cap of 100, to keep one response near a megabyte, and the walks use it.

**an unknown category is not an error.** a `cgid` the storefront does not know answers the whole catalog, unfiltered, with http 200. every grid carries its sort links, which repeat the request's filters, so the client checks that they name the category it asked for and raises `NotFoundError` when they do not. a blank query also answers the whole catalog, so the client refuses one before sending it.

**category ids are the menu's link ids.** they are what `cgid` takes, and they do not always match the path: `cordero_y_cabrito` lives at `/frescos/carniceria/cordero-y-lechal/`. `get_categories` builds the tree from the link paths and takes each id from its link. the client skips the "ver todo" links, because some point into another branch.

**the analytics price is the old one.** each tile carries an analytics attribute whose price is the list price during a price drop. `price.amount` reads the price the tile shows, and a struck-through price fills `previous`.

**products sold by weight are priced per kilogram.** fresh meat, fish and produce show a price per kilogram, a `PESO VARIABLE` badge, and purchase steps in kilograms, such as `0.19` for one banana. `is_variable_weight` is set, `average_weight` is the weight of an average piece, and `price.reference` is the price per kilogram. the record's own measure fields disagree with the tile for these products, sometimes by a factor of five, so the client never reads them for a weighed product.

**units.** tiles print the unit price as `0,83€/LITRO`, and the units seen were `LITRO`, `KILO`, `Kg`, `UNIDAD`, `DOCENA` and `LAVADO`, all in the shared vocabulary, so `price.reference` restates them per litre, kilogram, piece or dose. a record carries the same figure and unit as two separate fields, which fill `unit_price` and `unit_price_unit` and leave `unit_price_text` empty.

**promotions carry their dates.** a callout reads `Bajada de precio a 0.48€ (01/10/26 - 28/10/26)` or `Comprando 2, la unidad te sale a 0.50€ (24/09/26 - 28/10/26)`. each becomes a `Promotion` with its price, its start and end dates at midnight utc, and, for a multi-buy, `requires_quantity`. a record adds the promotion's id and its name in `kind`, such as `Pack 2 unidades`.

**an unknown product is a server error.** `Product-Variation` answers an id it does not know with http 500 and an error document, the same answer a fault would get. the client reads that document as `NotFoundError` and does not retry it. a 500 without it is retried as usual.

**sponsored tiles.** the full search page mixes retail-media tiles into its results. the grid the client reads had none in october 2026, and a tile the storefront flags as sponsored comes back with `is_sponsored` set.

**cloudflare.** the shop answered the library's own user agent in october 2026 without a challenge. a challenge, whether a `cf-mitigated: challenge` header or the "just a moment" page, raises `ChallengedError` after one request, and any other 403 raises `BlockedError`. `min_request_interval` defaults to one second, because a catalog walk is close to fifty pages of a megabyte each.

**robots.txt.** ahorramás's robots.txt disallows `/on/demandware.store/Sites-Ahorramas-Site/es/Product-Variation*`, which `get_product` reads, along with `Search-ShowAjax*`, `/buscador?*`, `*&prefn1*`, `*?pmax*`, `*ver-todo*`, the cart and the login. the client never sends the others, and `Search-UpdateGrid` is not disallowed. the owner chose to use these paths.

## non-goals

- **the delivery postcode.** entering one changes the session and the delivery slots, not the catalog, so the client never sends one.
- **nutrition.** the fields exist and were empty everywhere. the client parses nothing until a product carries them.
- **carts, lists and accounts.** the tiles carry add-to-cart and wishlist endpoints. the client never calls them.
