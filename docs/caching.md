# caching

storefronts are other people's servers, and a program that polls prices should not ask them the same question twice a minute. `CacheTransport` keeps answers in memory for as long as you choose. every client takes a `transport` argument, so the cache plugs in there and the client works as before.

```python
from supermercapy import CacheTransport, Mercadona

with Mercadona("mad3", transport=CacheTransport(ttl=600)) as mercadona:
    first = mercadona.get_product("4241")
    again = mercadona.get_product("4241")  # answered from memory
```

that example makes one request. the cache is opt-in, and a client built without one caches nothing.

## what it stores

- **successful json only.** the cache stores a response when its status is 2xx and its content type is `application/json` or ends in `+json`, as lidl's search type does. errors, redirects, images, and html pages always reach the storefront.
- **gets, unless you name more.** `methods` defaults to `("GET",)`. bonàrea reads everything through form posts and mercadona searches through a post, so `methods=("GET", "POST")` lets those in, keyed on the body as well as the url.
- **one key per question.** the cache reuses an answer for a request with the same method, url, body, and request headers. the key leaves out `cookie` and `authorization`, because session cookies and guest tokens change under a client without changing what it asks.
- **no cookies on a hit.** the stored copy drops `set-cookie`, so an answer from memory never rewinds the client's session. the live answer that filled the cache still sets its cookies.
- **`ttl` is the only freshness rule.** the cache ignores the storefronts' `cache-control` headers. in october 2026 mercadona, consum, plusfresc, bonàrea, bonpreu and carrefour's storefront all answered `no-cache` or `no-store`, so a cache that obeyed them would reuse nothing. lidl's search allowed five minutes of reuse, and carrefour's search index allowed the same to shared caches only.
- **bounded.** stored bodies count against `max_bytes`, 32 mebibytes by default, and the least recently used answers go first. the cache passes on an answer larger than the whole budget without storing it.

`clear()` forgets everything. closing a client closes its transport, which forgets everything too, so give each client its own `CacheTransport`.

`search_all()` is the exception. it lends one transport to every client it builds and leaves it open, so a cache passed to it serves each store across calls, and their different urls keep the stores apart.

```python
from supermercapy import CacheTransport, search_all

cache = CacheTransport(ttl=600)
first = search_all("leche", stores=["consum", "lidl"], transport=cache)
again = search_all("leche", stores=["consum", "lidl"], transport=cache)
cache.close()
```

the second call comes from memory, so that example makes two requests.

with the default `methods`, these requests never reach the cache:

| store | always sent |
| --- | --- |
| mercadona | the postcode binding, a put; search, a post |
| consum | nothing, since every read is a json get |
| plusfresc | the guest token, a post |
| bonàrea | every read, since all are posts; the language warm-up page, html |
| carrefour | product, category and home pages, html, which `get_product()`, `get_category()`, `get_home()` and the session warm-up read |
| lidl | campaign pages, html, which `get_offers()` reads; the sitemap, xml |
| bonpreu | the csrf page, html; the batch request behind `get_similar()` and `get_related()`, a put |
| dia | the session calls: the language, a patch, and the postcode, a put |
| eroski | product pages and the menu, html, which `get_product()`, `get_categories()`, the catalog walk and a client's first `get_category()` read |
| caprabo | the same pages as eroski |
| aldi | every algolia query behind search, listings and the catalog, a post; the overview, category and offers pages, html |
| ahorramás | every listing grid and the menu, html, which every method but `get_product()` reads |
| alcampo | the csrf page, html; the session move and the batch request behind `get_similar()` and `get_related()`, puts |
| condis | every storefront page, html, which `get_product()`, `get_categories()` and `get_category()` read, and the sign-in redirects before the first one; the scripts and the post behind `from_postal_code()` |

## when caching is safe

catalog and product reads are what the cache is for: product records, category trees, category listings, searches and store lists. the same question has the same answer for a while, and a stale answer is only old.

## when it is not

anything tied to a session or a token. the defaults keep those out. plusfresc mints its guest token with a post, and the pages that warm up carrefour's and bonàrea's sessions or carry bonpreu's csrf token are html. keep it that way, and in particular do not name `POST` for plusfresc. with a `ttl` longer than its guest token lasts, a client renewing the token would get the expired one back from memory.

dia keeps the postcode and the language in its session cookie, and the cache leaves cookies out of its key, so two dia clients with different bindings would read each other's pages from one cache. give each dia binding its own cache.

cursors that a storefront ties to a session are the other case. bonpreu's page tokens only work in the session that received them, and a page served from memory can outlive that session. page through bonpreu listings with a client that has no cache.

figures that move within the day, such as carrefour's stock counts from `get_stock()`, need a short `ttl` or no cache at all.

## pick a ttl

the `ttl` is the age of the oldest answer you are willing to act on. the storefronts publish no schedule for price changes. lidl's campaigns run by the week, and its search answers carry a five-minute lifetime of their own. a few minutes to an hour is a sensible start for prices, and category trees can take longer.

the `ttl` also decides what the cache saves. a poller that runs every fifteen minutes with a ten-minute `ttl` gets nothing from memory between runs. it still saves the repeats inside one run, such as a category tree that several calls read or a product asked for twice. a `ttl` longer than the interval serves whole runs from memory, which is the point, but then a run can report prices up to one `ttl` old.

## what a hit still costs

a hit waits for the client's pacing slot like any other request, because pacing runs before the request reaches the transport. on bonpreu that means two seconds a call even from memory. the hit does spare the storefront, which matters most on bonpreu, whose bot protection counts requests per ip address.

the `supermercapy.cache` logger records each hit and each stored answer at `DEBUG`, next to the request line the client logs for every call.

## proxies and other transports

`CacheTransport` sends what it cannot answer through its own `transport`. httpx ignores the proxy environment variables once a client has a transport, so the default transport reads them itself: a request goes through the proxy that `HTTP_PROXY`, `HTTPS_PROXY` or `ALL_PROXY` names, or directly when there is none or `NO_PROXY` covers the host, the way a client without a cache sends it. to choose a proxy in code instead, pass a transport:

```python
import httpx

from supermercapy import CacheTransport, Consum

network = httpx.HTTPTransport(proxy="http://127.0.0.1:3128")
with Consum(transport=CacheTransport(ttl=600, transport=network)) as consum:
    categories = consum.get_categories()
```

the same argument takes a test double such as `httpx.MockTransport`.

## with asyncio

the cache is thread safe and works the same under an [`AsyncClient`](async.md).

## a cache that outlives the process

`CacheTransport` lives in memory and goes away when the client closes. a cache on disk needs a third-party library such as hishel, and these storefronts fight its defaults. a test of hishel 1.4 in october 2026 found that the storefronts' answers forbid caching, so hishel has to be told to ignore http's caching rules. once it does, it keys a post on its url alone unless told to use the body, stores error responses unless filtered, replays the stored `set-cookie` on every hit, and never reuses bonàrea's answers at all, because they carry `Vary: *`.
