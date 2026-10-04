# caprabo

`supermercapy.caprabo.Caprabo` reads caprabo's online supermarket at `www.capraboacasa.com`. caprabo belongs to the eroski group and runs the same apache tapestry build as [eroski](eroski.md): the same paths, the same category ids, the same markup and the same parser. this page covers what differs, and the eroski page explains the shared mechanics.

the catalog is caprabo's own. regional products carry their own ids and prices, so caprabo sells `18581678`, a catalan semi-skimmed milk at 0,99 €, where eroski sells `18672311`, a basque one at 1,06 €. an id from one store is not a product at the other.

nothing binds. the storefront pins the anonymous session to shop `8284` (caprabo's online platform), and choosing another needs an account, so `store_id` is `None` and each product carries the `shop_id` that priced it.

```python
from supermercapy import Caprabo

with Caprabo(language="ca") as caprabo:
    page = caprabo.search_products("llet")  # one request
    milk = page.products[0]
    milk.name  # "Llet semidesnatada de Catalunya EROSKI, brik 1 litre"
    milk.availability.min_quantity  # decimal("6"): sold in sixes

    tree = caprabo.get_categories()  # one request, about 770 kb
```

## capabilities

| capability | supported | how |
|---|---|---|
| `CATALOG` | yes | `iter_catalog` reads the menu, then walks each root category's listing to its empty page |
| `NUTRITION` | yes | ingredients and the nutrition table from the product page's characteristics |
| `PROMOTIONS` | yes | multi-buy badges become `Promotion` records |
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

the models add the same fields as eroski's: `shop_id`, `is_marketplace`, `is_lowered_price`, and from a product page `manufacturer`, `manufacturer_address`, `alcohol_percentage` and `features`. categories add `path` and `url`.

## request counts

| call | requests | size |
|---|---|---|
| `search_products` | one per page | about 220 kb |
| `get_product` | one | about 630 kb |
| `get_categories` | one | about 770 kb |
| `get_category`, `get_category_products` | one, plus the menu once per client | about 220 kb |
| `iter_catalog` | the menu, then one per page of each root plus its empty page | hundreds of pages |

as at eroski, the sizes are the html after decompression, and the wire carries about a tenth of that.

## quirks

**two languages, and the names follow them.** `/es/` and `/ca/` are the storefront's languages, and `/en/` redirects to the error page, so `language="en"` raises `ConfigurationError`. unlike eroski, caprabo translates product names into catalan as well as the menu, so the same id reads `Leche semidesnatada de Cataluña` in spanish and `Llet semidesnatada de Catalunya` in catalan.

**a smaller tree.** the menu holds about 930 nodes under twelve roots, the food and household ones eroski also has. eroski's electronics, appliances and bedding roots are not there.

**packs.** many caprabo products sell in fixed packs. the purchase options of a milk brik read a minimum of six in steps of six. `availability.min_quantity` and `availability.increment` carry those numbers, and `price.amount` stays the price of one unit.

**robots.txt.** caprabo's robots.txt disallows `/*:changelanguage/`, `*/search/results/?q=`, `*/?zipCode` and session ids. it has no `?t:ac=` rule, so the category listings the client reads at `/es/supermarket:loadpage?t:ac=…` are not disallowed. searches go to `/es/search/results:loadpage?q=…`, which does not match the search rule's path literally but is the same search. the owner chose to use these paths.

everything else, the fragments, the page cursor, the menu cache, the placeholder product slug, the redirects that mean "not found", the units, the edge's 403 for a few user agents and the one-second default pacing, works exactly as the [eroski page](eroski.md#quirks) describes.

## non-goals

- **choosing a shop.** the anonymous session cannot, and logging in to pick one is out of scope.
- **offers and filters.** the offers page needs a login, and the filter menu stores its state in the session.
- **reviews.** product pages carry customer reviews and ratings. the client does not parse them.
