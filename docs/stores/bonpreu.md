# bonpreu

`supermercapy.bonpreu.Bonpreu` reads the bonpreu and esclat online shop at `compraonline.bonpreuesclat.cat`. it runs on ocado smart platform and serves anonymous json from one host: no key, no token, no store binding, no postcode step. the tenant has a single region, and the server itself reports that applying a delivery destination a hundred kilometres away changes neither the region nor the product assignment, so there is nothing to select.

the one thing worth planning around is an aws waf budget. it is per ip, worth twenty to thirty requests from a cold start in september 2026 and only eight to thirteen in october, and once it is spent every catalog path answers http 202 with an empty body for tens of minutes. pacing delays that but does not prevent it. treat this client as something for lookups and small crawls, and read the challenge section below before writing a loop.

the same waf refuses `/api/` outright to a user agent it does not take for a browser, so the client's default user agent is a browser string. passing `user_agent="something/1.0"` makes every catalog call raise `BlockedError`.

```python
from supermercapy import Bonpreu

with Bonpreu() as bonpreu:
    tree = bonpreu.get_categories()  # the cheap call, see below
    fruit = bonpreu.get_category(tree[0].children[0].id)
    page = bonpreu.search_products("llet sense lactosa", page_size=10)
    while page.has_more:
        page = bonpreu.search_products("llet sense lactosa", cursor=page.next_cursor)

with Bonpreu(language="es") as bonpreu:
    flour = bonpreu.get_product("29189")
    flour.price.amount  # decimal("1.69")
    flour.nutrition.values[0].name  # "valor energético"
    flour.field("ingredients")
```

## capabilities

| capability | supported | how |
|---|---|---|
| `NUTRITION` | yes | ingredients, allergens and the nutrition table, parsed out of the product sheet's html |
| `PROMOTIONS` | yes | every row carries its promotions, and the "before" price is read from their prose |
| `NEW_ARRIVALS` | yes | `get_new_arrivals` walks the whole "novetats" category by page token, three hundred rows a request |
| `OFFERS` | yes | `get_offers` reads the shop-wide promotions listing |
| `CATALOG` | no | see the non-goals below; the budget cannot pay for a walk |
| `EAN` / `EAN_LOOKUP` | no | no ean, gtin, barcode or sku exists anywhere in the api |
| `POSTAL_CODE` / `STORES` | no | one region serves the whole footprint and prices never vary by destination |
| `HOME` | no | the home page is contentful copy behind a csrf-gated graphql endpoint |
| `FUTURE_PRICES` | no | nothing forward-looking is published |

## extensions

| method | what it does |
|---|---|
| `get_promotions(category_id=, retailer_category_id=, page_size=, cursor=, sort=, filters=)` | the promotions listing with paging and filters; `get_offers` is its first page |
| `suggest(term, limit=)` | the autocomplete suggestions for a partial term |
| `get_similar(product_id)` | the products the storefront offers as similar, resolved to full rows |
| `get_related(product_id, limit=, high_relevance_only=)` | the cross-sell products, resolved the same way |

`search_products`, `get_category` and `get_promotions` also take `sort`, one of `favorite`, `priceAscending`, `priceDescending`, `pricePerAscending` or `pricePerDescending`, and `filters`, a mapping of filter group id to one or more attribute ids:

```python
from supermercapy import Bonpreu

with Bonpreu() as bonpreu:
    page = bonpreu.search_products(
        "poma",
        sort="priceAscending",
        filters={"boolean": "onOffer", "dietaryAndLifestyle": ["eco", "vegan"]},
    )
```

the storefront ands the groups and ors the values within a group. the wire carries each value percent-encoded twice, which is why the client builds it for you. a brand id holding a space lands as `LA%2520COLLITA`.

## the waf challenge

a challenged response is http 202 with an empty body, an `x-amzn-waf-action: challenge` header, and no `requestid` header, which every genuine response carries. the client classifies that as a challenge and raises `ChallengedError` after exactly one request. it never retries, because every challenged request appears to spend from the same budget, so polling until it clears keeps it alive rather than clearing it.

a blocked response is different. it is http 403, cloudfront's html "request blocked" page, and again no `requestid`. that is the waf refusing the caller instead of asking it to prove itself, and it raises `BlockedError` after one request. in october 2026 it was what every `/api/` path gave the library's own `supermercapy/<version>` user agent, while the same request with a browser user agent answered normally.

```python
from supermercapy import Bonpreu, ChallengedError

with Bonpreu() as bonpreu:
    try:
        page = bonpreu.search_products("llet")
    except ChallengedError as challenge:
        challenge.suggested_backoff  # 1800.0 seconds
```

during reconnaissance, two independent sessions tripped it after twenty to thirty-five requests within three to five minutes, one of them paced at a second and a half. a sixteen-round poll produced sixty-three challenges and one success. seven minutes of complete silence afterwards bought back nothing. so `suggested_backoff` is half an hour, and it is a floor, not a promise.

the october 2026 audit found the budget smaller. three runs from one address, half an hour apart and paced at two seconds, were challenged at their tenth, fourteenth and ninth request, on whichever path came next. once the challenge started, a fresh client with no cookies was challenged too, while the image host kept answering. a search page token answered normally as the second request of a rested budget, so it is the count that trips the rule, not paging as such.

`min_request_interval` defaults to two seconds for the same reason. lowering it does not buy more requests, it only spends them sooner.

`get_categories` is the exception. the category tree answered http 200 on all sixty-four probes taken while the challenge was active. the cdn cache almost certainly serves it, so it never reaches the rule, which makes it the right first call and a good health check. do not rely on the exemption for anything else.

## quirks

- **`categoryId`, never `category`.** the listing endpoint accepts `?category=<uuid>` without complaint and answers with the root pseudo-category and generic products. the client only ever sends `categoryId`. an unknown `categoryId` answers http 404 and raises `NotFoundError`. until september 2026 it fell back to the root listing instead, so `get_category` also raises `NotFoundError` when the listing echoes back a category other than the one asked for, instead of handing back the wrong page.
- **page tokens are session scoped.** the cursor is the storefront's own `nextPageToken`, passed back verbatim, and it only works alongside the cookies of the request that produced it. those live on the client, so a walk works as long as it stays on one client. a token cannot be saved and replayed from another. a token the session does not hold answers http 401 with code `CC-090`, which raises `TransportError` with that status and no retry.
- **the language is a cookie.** the storefront ignores `accept-language`. the client sets `language=ca-ES` or `language=es-ES` from its `language` argument. catalan is the storefront default, and the client's too.
- **no barcode of any kind.** `retailer_product_id`, the numeric id in the urls and on `product.id`, is the only public identifier. `product_uuid` is the internal id that the similar, related and batch endpoints speak, and the sheet endpoint does not accept it.
- **everything descriptive is html.** ingredients, storage, legal text and the nutrition table all arrive as markup inside a json string. the client keeps every one on `product.fields`, raw through `raw_field` and stripped through `field`, and parses the nutrition table into rows, with the raw markup kept on `nutrition.raw_html`.
- **allergens are bolded, not listed.** when the sheet publishes no `allergens` field, the client reads the `<b>`-marked words out of the ingredients html instead.
- **the "before" price is prose.** no numeric field carries it. a promotion description reading `"abans 0,58 €"` is all there is, and the client parses it into `price.previous`.
- **`unitPrice.unit` is a message key.** branch on `price.unit_name` (`PER_1KG`, `EACH`, …). `price.unit_message_key` keeps the key itself.
- **five units, no dose.** a unit price is `EACH`, `PER_1KG`, `PER_LITRE`, `PER_100ML` or `PER_DOZEN`, and detergent is priced per litre or per pack. `price.reference` reads the key or the name, multiplies per 100 ml by ten and divides the dozen by twelve. on a promoted row both `price.reference` and `unit_price` read `promoUnitPrice`, so they follow `amount`. a promoted row with no promotional unit price gets neither.
- **the promotions listing speaks its own dialect.** since october 2026 it sends prices as json numbers rather than strings, drops `unitName` and keeps only the message key, and names its default order `relevance` where the other listings say `favorite`. the client reads `unit_name` off the key for the five keys the other listings pair with a name (`fop.price.per.each`, `.kg`, `.litre`, `.100ml` and `.dozen`) and leaves it `None` for any other.
- **search injects ads.** sponsored rows arrive in a `featured` product group with an `externalAdvertId`. the client keeps them, flagged with `is_sponsored` and carrying `campaign_id` and `group_type`, so a caller can filter them.
- **a product can appear in two groups.** the client flattens a page in group order and keeps each product once, at its first placement.
- **the client cuts a query at fifty characters,** as the storefront cuts its own.
- **there are two kinds of 403.** the storefront attaches `X-CSRF-TOKEN` to every non-get and to nothing else. the client does the same, minting one from the home page the first time it needs it. the origin answers a missing or stale token with an empty 403 carrying `requestid` and `ecom-csrf-failure: true`, and the client then mints a fresh token and retries once. cloudfront's own "request blocked" page, with no `requestid`, is the waf refusing the caller, and raises `BlockedError` straight away.
- **only one request is a non-get.** the batch resolve behind `get_similar` and `get_related`. everything else is a plain get with no token at all.
- **`/api/` is disallowed in the shop's robots.txt.** this client reads it anyway, as a browser does. that is a decision for the caller to make knowingly.

## non-goals

- **the catalog.** it holds twenty-one thousand products. paging every leaf category, or reading the sitemap plus one sheet request each, needs orders of magnitude more requests than the budget allows. `CATALOG` is not declared, and `iter_catalog` and `get_catalog` raise `UnsupportedOperationError` without touching the network, instead of shipping a walk that always dies part way through.
- **solving the challenge.** the browser satisfies it by running an aws captcha script that sets an `aws-waf-token` cookie. the client does not attempt to obtain, replay or forge one.
- **delivery eligibility.** it is a coordinate check behind a csrf token, it says nothing about prices or assortment, and a postcode would have to be geocoded first. the address lookup service the storefront ships is not even populated for this tenant.
- **recipes, the home page, and the cms.** the recipe endpoints exist and the home page is contentful copy behind graphql. neither carries product data this client does not already have.
- **a custom `regionId`.** the api accepts one value and answers a bogus one with http 400, so it is a constant, not an argument.
