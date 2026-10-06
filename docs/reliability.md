# reliability and request behavior

supermercapy calls undocumented services owned by third parties. the clients make failures explicit, but they cannot guarantee that those services stay available or keep answering in the same shape. every store shares the base client's retry loop, pacing rule, and error vocabulary.

## timeouts

the default timeout is 10 seconds. pass a positive finite number for one value, or an `httpx.Timeout` to set connect, read, write, and pool timeouts separately.

```python
import httpx

from supermercapy import Consum

timeout = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0)
with Consum(timeout=timeout) as consum:
    categories = consum.get_categories()
```

the client retries connection errors and connect timeouts. every other request error, read timeouts included, raises `TransportError` after the first failure.

## pacing

`min_request_interval` sets the minimum gap between request starts made by one client. it limits start times, not concurrency or total duration, and it applies to retries and warm-up requests as well.

```python
from supermercapy import Carrefour

with Carrefour(min_request_interval=0.25) as carrefour:
    categories = carrefour.get_categories()
```

most clients default to no pacing. a store whose backend needs it ships a non-zero `default_min_request_interval`, which the constructor uses unless you pass your own. plusfresc, bonàrea, dia and condis pace at 0.5 seconds, eroski, caprabo and ahorramás at 1 second, and bonpreu and alcampo at 2 seconds.

## retries

the default `RetryPolicy` allows three attempts in total. the loop retries:

- connection errors and connect timeouts
- http `429`
- http `500`, `502`, `503`, and `504`
- a challenge, but only when the store's client knows how to answer one

the loop never retries ordinary `4xx` responses or other `5xx` responses.

without a usable `Retry-After`, the base delay before retry number `n` is `backoff_factor * 2 ** n`, starting at `n = 0`, capped by `max_delay`. `jitter_ratio` adds a random value between zero and that fraction of the base delay, and the cap still applies. the shipped defaults therefore wait 0.25 to 0.275 seconds before the second attempt and 0.5 to 0.55 before the third.

for http `429` the client accepts `Retry-After` as seconds or as an http date, waits for it but never longer than `max_delay`, and falls back to backoff when the header cannot be read. if the retry budget ends on a `429`, it raises `RateLimitError`.

```python
from supermercapy import Lidl, RateLimitError, RetryPolicy

retry = RetryPolicy(
    max_attempts=2,
    backoff_factor=0.2,
    max_delay=2.0,
    jitter_ratio=0.1,
)

try:
    with Lidl(timeout=5.0, retry_policy=retry) as lidl:
        categories = lidl.get_categories()
except RateLimitError as error:
    print("rate limit persisted", error.retry_after)
```

`retry_after` is the delay the server asked for, in seconds and not capped by `max_delay`, or `None` when the server sent nothing usable. `BlockedError` reports the same way.

## how a response is judged

the loop never decides what a response means. it asks the client, which returns one verdict. that is how a store adds a case without reimplementing retries.

| verdict | what the loop does |
| --- | --- |
| `OK` | returns the response |
| `RETRY` | waits and tries again while the budget lasts |
| `RATE_LIMITED` | honours `Retry-After`, then raises `RateLimitError` |
| `NOT_FOUND` | raises `NotFoundError` |
| `AUTH_EXPIRED` | refreshes credentials once per call, then retries once |
| `CHALLENGED` | asks the client to answer the challenge, and raises `ChallengedError` when it cannot |
| `BLOCKED` | raises `BlockedError` without retrying |
| `ERROR` | raises `TransportError` carrying the status code |

by default, `429` is rate limiting, the four transient `5xx` codes are retryable, `404` is not found, and anything else at `400` or above is an error. stores that answer differently narrow that reading. bonpreu tells the origin's csrf `403` apart from the waf's, carrefour tells its two `403` bodies apart, and plusfresc treats `401` as an expired guest token.

api calls never follow redirects. a json endpoint that answers with one has moved, so the client raises `TransportError` carrying the status code and the new location instead of trying to parse the empty body. `download()` is the exception. image hosts redirect often, so it follows them.

## warm-up requests

some storefronts only answer properly once a session exists. those clients override a warm-up hook that runs before the first real request and, where the session expires, again after its time-to-live. bonàrea fetches the language path once so search honours the selected language, and carrefour fetches one page every twenty-five minutes to hold a live cookie. both wait for a call that needs the warm-up, so browsing bonàrea or searching carrefour's index pays for none. dia restates the bound postcode, and any language but spanish, every 45 minutes, and alcampo moves its session to the bound point's region every half hour. an unbound alcampo client, or an unbound dia client in spanish, has no warm-up.

## logging

the library logs through the standard `logging` module and stays silent until you configure it, because the `supermercapy` logger carries a `NullHandler`. each client logs through a child logger named `supermercapy.<store>`, such as `supermercapy.mercadona`, so you can filter one store.

| level | what it shows |
| --- | --- |
| `DEBUG` | every request sent: method, url with its query, response status, and elapsed milliseconds |
| `INFO` | each retry (attempt number, the reason, and the delay it will sleep), each pacing wait longer than zero, each warm-up run, each credential refresh, and each challenge handed to the store |
| `WARNING` and above | nothing: failures are raised as exceptions, not logged |

the library never logs headers, request bodies, cookies, or tokens. it does log the url, so a store that puts a token in a query string would show it at `DEBUG`.

```python
import logging

logging.basicConfig(format="%(asctime)s %(name)s %(message)s")
logging.getLogger("supermercapy.mercadona").setLevel(logging.DEBUG)
```

## error model

every failure inherits from `SupermercapyError`.

| exception | meaning | useful attributes |
| --- | --- | --- |
| `ConfigurationError` | an option, identifier, postcode, or argument is invalid | none |
| `UnsupportedOperationError` | the store does not implement an optional operation | `capability`, `store` |
| `TransportError` | a request failed or returned an unhandled status | `status_code` |
| `RateLimitError` | the retry budget ended on http `429` | `status_code`, `retry_after` |
| `NotFoundError` | the resource does not exist | `status_code` |
| `NotAvailableError` | the resource exists but is not offered to this binding | `status_code` |
| `OutOfCoverageError` | the postcode is not served by the store | `status_code` |
| `BlockedError` | bot protection refused the request | `status_code`, `retry_after`, `permanent` |
| `ChallengedError` | the answer was a browser challenge, not data | `status_code`, `retry_after`, `suggested_backoff` |
| `AuthenticationError` | credentials could not be obtained or refreshed | `status_code` |
| `InvalidResponseError` | the body is not valid json or has no usable structure | none |

`UnsupportedOperationError` is a `ConfigurationError`, and therefore also a `ValueError`, and the client raises it before any request. `NotAvailableError` narrows `NotFoundError`, and `ChallengedError` narrows `BlockedError`, so catching the wider class still works. catch the narrow one first when different failures need different handling.

```python
from supermercapy import (
    Consum,
    NotFoundError,
    RateLimitError,
    TransportError,
)

try:
    with Consum.from_postal_code("46001") as consum:
        product = consum.get_product("missing-id")
except NotFoundError:
    print("no such product")
except RateLimitError:
    print("request limit reached")
except TransportError as error:
    print("request failed", error.status_code)
```

a challenge is not a transient failure. `ChallengedError.suggested_backoff` carries how long to wait before trying that store again. retrying at once makes the block worse.

## response parsing

parsers ignore unknown fields and accept absent optional ones. they raise `InvalidResponseError` when a body is not json, when a required collection has the wrong type, or when a record has no usable identity such as an id and a name.

models never retain the raw response dictionary, except where a store's own model keeps a raw html blob as a documented fallback. do not depend on undocumented upstream fields that supermercapy does not expose.

## downloads

`download()` creates the destination directory when needed, streams into a temporary sibling file, closes the response, then replaces the destination. a failed status, an interrupted stream, or any exception removes the temporary file and leaves an existing destination untouched.

## request counts

counts are per call at a typical store, before retries and before any warm-up, token or sign-in the client owes. a store that differs gives its own counts on its page.

| operation | requests |
| --- | ---: |
| constructing a client | 0 |
| `from_postal_code(...)` | 1 at mercadona, consum and plusfresc, 0 at aldi, 2 at carrefour and dia, 1 per 250 stores at lidl, and 18 at condis in october 2026, sign-in included |
| `search_products(...)` | 1 |
| `get_product(...)` | 1 |
| `get_categories()` | 1 |
| `get_home()` | 1 |
| `get_new_arrivals()`, `get_offers()` | 1 per page of the feed, which the store pages give |
| `list_stores(...)` | 1, or 1 per 250 stores at lidl |
| `get_product_by_ean(...)` | 1 |
| `get_category(...)` | 1 or 2, depending on whether the store's tree carries products |
| `iter_search(...)` | 1 per page until the cursor ends |
| `iter_catalog()`, `get_catalog()` | store-specific, and sometimes very large |
| reading an attribute of a returned model | 0 |

the catalog walk is the one to plan for, because its cost differs between stores by orders of magnitude. plusfresc answers it from a single multi-megabyte response, mercadona and bonàrea walk their category trees, and lidl hydrates one product per request from a sitemap. carrefour, bonpreu and alcampo have no catalog walk. the store pages give the figures. `min_request_interval` changes timing, not counts.

## verification

the offline suite uses committed fixtures and `httpx.MockTransport`. it covers every public operation of every client, the exact urls, parameters, headers and request counts each one sends, immutable models, parser edge cases, retries, rate limits, pacing, and atomic downloads. a conformance suite runs the same contract against every client in `ALL_CLIENTS`, including that a declared capability matches an overridden method and that an undeclared operation raises before any i/o. ci runs all of it on cpython 3.11 through 3.14 and enforces at least 90% branch coverage.

a separate weekly and manually triggered live contract workflow calls every public method of every store against the real storefront, each store in its own job, and no store's failure fails another's. besides checking what the client returns, it compares each raw json response with the committed fixture, so an upstream key that was renamed or dropped fails the run instead of quietly turning into `None`. a passing run confirms that those response shapes matched the client during that run. it does not guarantee future availability.
