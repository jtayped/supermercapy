# api guide

supermercapy exposes its supported public names from the top-level `supermercapy` package, and each store's own models from `supermercapy.<store>`. the package ships `py.typed`.

## the shared client

every client subclasses `BaseClient`, which owns the transport, the retry loop, the request pacing, and the standard interface. you construct a client directly, construction performs no i/o, and the client closes its connection pool through `close()` or a `with` block.

```text
Client(
    <store binding, if the store has one>,
    *,
    language=None,
    timeout=10.0,
    retry_policy=None,
    min_request_interval=None,
    transport=None,
    user_agent=None,
)
```

`language` accepts a `Language` member or its string value and must be one the client declares. `timeout` accepts a positive finite number or an `httpx.Timeout`. `transport` accepts an `httpx.BaseTransport`, such as a [`CacheTransport`](caching.md) or a test double. `min_request_interval` sets the minimum gap between request starts, defaulting to the client's own `default_min_request_interval`. `user_agent` replaces the client's default. carrefour, bonpreu and alcampo default to a browser's user agent, because their edges refuse other ones.

the leading positional argument differs per store, because the binding does:

| client | binding argument | other keywords |
| --- | --- | --- |
| `Mercadona` | `warehouse` | none |
| `Consum` | `zone=None` | `drop_sponsored=True` |
| `Plusfresc` | `center=12` | none |
| `Bonarea` | none | none |
| `Carrefour` | `sale_point=None` | `catalog="food"` |
| `Lidl` | `region=None` | `zone="PEN"`, `family=None` |
| `Bonpreu` | none | none |
| `Dia` | `postal_code=None` | none |
| `Eroski` | none | none |
| `Caprabo` | none | none |
| `Aldi` | `region="pen"` | none |
| `Ahorramas` | none | none |
| `Alcampo` | `store=None` | none |
| `Condis` | `picking_centre="718"` | none |

### class attributes

these describe a client before one is built, so a program can plan against a store without constructing it.

| attribute | type | meaning |
| --- | --- | --- |
| `store_name` | `str` | the lowercase store key, also used in error messages |
| `capabilities` | `frozenset[Capability]` | every optional operation and data shape the store supports |
| `supported_languages` | `frozenset[Language]` | the languages the storefront answers in |
| `default_language` | `Language` | the language used when none is passed |
| `default_user_agent` | `str` | the user agent sent unless one is passed |
| `default_min_request_interval` | `float` | the pacing the store needs by default |
| `default_page_size` | `int` | the page size used when none is passed |
| `max_page_size` | optional `int` | the largest page the store accepts, or `None` when it caps none |

`supports(capability)` is a classmethod returning whether `capabilities` contains one flag.

### instance properties

| property | type | meaning |
| --- | --- | --- |
| `store_id` | optional `str` | the binding as a string, or `None` when the store binds nothing |
| `language` | `Language` | the selected language |
| `min_request_interval` | `float` | the minimum seconds between request starts |
| `user_agent` | `str` | the user agent every request carries |
| `is_closed` | `bool` | whether `close()` has run |

### required methods

every client implements these four. a store that cannot answer one of them honestly is not in supermercapy.

| method | return type |
| --- | --- |
| `search_products(query, *, page_size=None, cursor=None)` | `SearchResult` |
| `get_product(product_id)` | `Product` |
| `get_categories()` | `tuple[Category, ...]` |
| `get_category(category_id)` | `Category` |

a client may add typed keyword arguments after the standard ones, such as `Consum.search_products(..., order_by=, filters=)` or `Carrefour.search_products(..., sort=)`. the standard arguments keep their meaning everywhere.

### methods the base class implements

| method | return type | what it does |
| --- | --- | --- |
| `iter_search(query, *, page_size=None)` | `Iterator[ProductSummary]` | follows `next_cursor` until it is `None` |
| `get_catalog()` | `tuple[ProductSummary, ...]` | drains `iter_catalog()` and deduplicates by id |
| `download(source, destination)` | `Path` | streams a `Photo` or an image url to a file |

`get_catalog()` requires `Capability.CATALOG` like `iter_catalog()` does.

### optional methods

each of these raises `UnsupportedOperationError` before any i/o unless the client declares the matching capability.

| method | capability | return type |
| --- | --- | --- |
| `from_postal_code(postal_code, **options)` | `POSTAL_CODE` | a client |
| `list_stores(postal_code=None)` | `STORES` | `tuple[Store, ...]` |
| `iter_catalog()` | `CATALOG` | `Iterator[ProductSummary]` |
| `get_product_by_ean(ean)` | `EAN_LOOKUP` | `Product` |
| `get_new_arrivals()` | `NEW_ARRIVALS` | `tuple[ProductSummary, ...]` |
| `get_offers()` | `OFFERS` | `tuple[ProductSummary, ...]` |
| `get_home()` | `HOME` | `tuple[HomeSection, ...]` |

`from_postal_code()` is a classmethod taking the same keyword options as the constructor plus whatever the store's own constructor accepts. it validates a five-digit spanish postcode before requesting anything. consum, plusfresc, carrefour, lidl, dia and condis answer when they do not serve a postcode, and there it raises `OutOfCoverageError`.

## capabilities

`Capability` is a `Flag` enum with eleven members. seven of them unlock one optional method each, and the conformance suite asserts that a client declares one exactly when it overrides that method. the other four describe response data. they say whether a field can ever hold a value for that store, which is what tells an absent value apart from one the store never publishes.

| flag | kind | meaning |
| --- | --- | --- |
| `POSTAL_CODE` | operation | a postcode resolves to a binding |
| `STORES` | operation | the stores, zones, or warehouses can be listed |
| `CATALOG` | operation | the whole assortment can be enumerated |
| `EAN_LOOKUP` | operation | a barcode resolves to a product |
| `NEW_ARRIVALS` | operation | the storefront publishes a novelties feed |
| `OFFERS` | operation | the storefront publishes a discounts feed |
| `HOME` | operation | the home page's sections are machine-readable |
| `EAN` | data | products carry a barcode |
| `NUTRITION` | data | products carry ingredients, allergens, or a nutrition table |
| `PROMOTIONS` | data | products carry offers, member prices, or campaign badges |
| `FUTURE_PRICES` | data | a price that starts later is published before it applies |

### capability matrix

`no` means the store publishes nothing supermercapy could map onto that capability, not that the work is pending. the store pages explain each `no` in place, and a test parses this table against every client's declared `capabilities`, so it cannot drift from the code.

| capability | mercadona | consum | plusfresc | bonarea | carrefour | lidl | bonpreu | dia | eroski | caprabo | aldi | ahorramas | alcampo | condis |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `POSTAL_CODE` | yes | yes | yes | no | yes | yes | no | yes | no | no | yes | no | no | yes |
| `STORES` | yes | yes | yes | no | yes | yes | no | no | no | no | no | no | yes | no |
| `CATALOG` | yes | yes | yes | yes | no | yes | no | yes | yes | yes | yes | yes | no | yes |
| `EAN_LOOKUP` | no | yes | no | no | yes | no | no | no | no | no | no | no | no | no |
| `NEW_ARRIVALS` | yes | yes | yes | no | no | no | yes | yes | no | no | no | no | yes | yes |
| `OFFERS` | no | yes | yes | no | no | yes | yes | yes | no | no | yes | yes | yes | yes |
| `HOME` | yes | no | no | no | yes | no | no | no | no | no | no | no | no | no |
| `EAN` | yes | yes | no | no | yes | yes | no | no | no | no | no | no | no | no |
| `NUTRITION` | yes | no | yes | yes | yes | no | yes | yes | yes | yes | no | no | yes | yes |
| `PROMOTIONS` | no | yes | yes | no | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes |
| `FUTURE_PRICES` | no | no | no | no | no | yes | no | no | no | no | yes | no | no | no |

## pagination

the contract is one page plus an opaque cursor. `search_products()` takes `cursor=None` for the first page and returns `next_cursor`, which is `None` at the end. `iter_search()` follows it. the cursor is a string because its meaning is the store's business, and five different meanings are in use:

| style | stores | what the cursor holds |
| --- | --- | --- |
| page | mercadona | the next zero-based page number |
| page and row | eroski, caprabo | the storefront page, and the rows of it already returned when a smaller page was cut from it: `3` or `3:5` |
| offset | consum, carrefour, lidl, dia, aldi, ahorramás, condis | the number of rows consumed so far |
| token | bonpreu, alcampo | the continuation token the storefront issued |
| none | plusfresc, bonàrea | an offset into rows the client already holds |

the `none` style covers storefronts that take no paging parameter at all and answer with the whole result set. those clients slice the response themselves, so walking their cursor re-requests the same response. use `iter_search()` there, which streams one response without asking again.

`SearchResult` carries `page_size`, `total_hits` when the store reports one, `next_cursor`, `has_more`, and `truncated`. `truncated` marks a page that sat against an upstream result ceiling with matches still unreached, which happens on plusfresc at exactly one hundred rows, on carrefour beyond its start cap, on mercadona past its thousand-hit page limit, and on condis once the next offset would pass its ceiling of 2,494.

## exceptions

every failure supermercapy raises inherits from `SupermercapyError`.

```text
SupermercapyError
├── ConfigurationError(ValueError)
│   └── UnsupportedOperationError
├── TransportError
│   ├── RateLimitError
│   ├── NotFoundError
│   │   └── NotAvailableError
│   ├── OutOfCoverageError
│   ├── BlockedError
│   │   └── ChallengedError
│   └── AuthenticationError
└── InvalidResponseError
```

see the [error model](reliability.md#error-model) for what each one means and which attributes it carries.

## models

all models are frozen, slotted, keyword-only dataclasses. collections are tuples, ids are `str`, money and quantities are `Decimal`, and timestamps are `datetime`. store packages subclass them to add store-specific fields, and each client's return annotations narrow to its own subclasses.

| model | fields |
| --- | --- |
| `ProductSummary` | `id`, `name`, `brand`, `ean`, `slug`, `url`, `pack_size_text`, `thumbnail`, `price`, `availability`, `category_ids`, `promotions`, `is_new`, `is_sponsored`, `is_variable_weight` |
| `Product` | every `ProductSummary` field plus `photos`, `description`, `legal_name`, `origin`, `storage`, `usage`, `category_path`, `nutrition`, `requires_age_check` |
| `Price` | `amount`, `previous`, `unit_price`, `unit_price_unit`, `unit_price_text`, `reference`, `currency`, `tax_percentage`, `is_discounted`, `discount_percentage`, `valid_from`, `valid_until`, `is_approximate` |
| `UnitPrice` | `amount`, `unit` |
| `Availability` | `available`, `status`, `max_quantity`, `min_quantity`, `increment` |
| `Promotion` | `id`, `description`, `kind`, `price`, `requires_quantity`, `starts_at`, `ends_at`, `member_only` |
| `Nutrition` | `ingredients`, `allergens`, `values`, `per`, `nutri_score`, `raw_html` |
| `NutritionValue` | `name`, `per_100`, `per_serving`, `unit` |
| `Photo` | `url`, `kind`, `alt` |
| `Category` | `id`, `name`, `parent_id`, `level`, `slug`, `image_url`, `product_count`, `children`, `products` |
| `SearchResult` | `query`, `products`, `page_size`, `total_hits`, `next_cursor`, `truncated`, and the `has_more` property |
| `Store` | `id`, `name`, `kind`, `address`, `postal_code`, `city`, `province`, `latitude`, `longitude` |
| `HomeSection` | `layout`, `title`, `products` |

`Photo.url` is a complete url. a store whose cdn can resize an image exposes that through its own photo subclass, not through this field.

### prices

`Price.amount` is what one selling unit costs now. the three `unit_price` fields keep the store's own unit price untouched. `unit_price` is the store's figure, `unit_price_unit` its unit exactly as sent (`"L"`, `"1 Kg"`, `"100 Gr"`, `"dc"`, `"PER_1KG"`), and `unit_price_text` its display string where it sends one. they differ from store to store, so they don't compare.

`Price.reference` restates that figure in a shared unit. it is a `UnitPrice`, whose `amount` is what one `unit` costs, and `Unit` is a string enum.

| `Unit` | value | what the stores' units become |
| --- | --- | --- |
| `KILOGRAM` | `"kg"` | per kilogram as is, per gram ×1000, per 100 g ×10 |
| `LITRE` | `"l"` | per litre as is, per 100 ml ×10, per centilitre ×100, per millilitre ×1000 |
| `PIECE` | `"piece"` | per unit (ud, u., un, each) as is, per dozen ÷12 |
| `DOSE` | `"dose"` | per dose (dosis, do, d.) and per wash (lavado, lv) as is |
| `METRE` | `"m"` | per metre as is |
| `SQUARE_METRE` | `"m2"` | per square metre as is |

a wash counts as one dose, because detergent labels use the two words for the same thing.

the conversion is `Decimal` arithmetic. the package rounds the result half up to four decimal places, then drops trailing zeros down to two places. `6.2` per 100 g becomes `62.00` per kilogram, and `2.625` per dozen becomes `0.2188` per piece.

the reference follows `amount`. when a promotion replaces the price and the store sends a promotional unit price, the reference comes from the promotional one.

`reference` is `None` when the store published no unit price, or published one in a unit the package doesn't recognise. `None` means unknown, never zero. drop those rows before sorting, and only compare amounts whose `unit` matches.

no reference comes from a product name or a free-text pack size. carrefour's search index is the one place the package computes a unit price itself, by dividing the price by a quantity the index sends as a number. each [store page](index.md#stores) lists the units that store reports.

## serialising models

`to_dict(value)` converts a model, a list or tuple of models, or a dict of them into json-compatible python, and `to_json(value, indent=None)` returns the same as a json string. `Decimal` becomes a string so prices stay exact. see [saving results](usage.md#saving-results).

## controlled values

`Language` is a string enum with `SPANISH = "es"`, `CATALAN = "ca"`, `ENGLISH = "en"`, and `VALENCIAN = "vl"`. a client rejects a language outside its `supported_languages` with `ConfigurationError`.

`Unit` is a string enum with `KILOGRAM = "kg"`, `LITRE = "l"`, `PIECE = "piece"`, `DOSE = "dose"`, `METRE = "m"`, and `SQUARE_METRE = "m2"`. see [prices](#prices).

`RetryPolicy` is a frozen dataclass of `max_attempts=3`, `backoff_factor=0.25`, `max_delay=5.0`, and `jitter_ratio=0.1`. see [reliability and request behavior](reliability.md) for how the client applies them.

## version

`supermercapy.__version__` holds the installed package version, which is the version of the `supermercapy` distribution.
