# generated api

this page renders signatures and docstrings from the installed package. the [api guide](api.md) explains the interface and the capability matrix, [reliability](reliability.md#request-counts) gives the request counts, and the [store pages](index.md#stores) cover each storefront's own models and extensions.

## serialisation

::: supermercapy.to_dict

::: supermercapy.to_json

## base client

::: supermercapy.BaseClient

::: supermercapy.RetryPolicy

## store clients

::: supermercapy.Mercadona

::: supermercapy.Consum

::: supermercapy.Plusfresc

::: supermercapy.Bonarea

::: supermercapy.Carrefour

::: supermercapy.Lidl

::: supermercapy.Bonpreu

::: supermercapy.Dia

::: supermercapy.Eroski

::: supermercapy.Caprabo

::: supermercapy.Aldi

::: supermercapy.Ahorramas

::: supermercapy.Alcampo

::: supermercapy.Condis

## cross-store search

::: supermercapy.search_all

::: supermercapy.SearchAllResult

::: supermercapy.StoreSearch

::: supermercapy.SearchStatus

## matching

::: supermercapy.group_same

::: supermercapy.find_same

::: supermercapy.pair_same

::: supermercapy.score_same

::: supermercapy.find_alternatives

::: supermercapy.score_alternative

::: supermercapy.Listing

::: supermercapy.Match

::: supermercapy.MatchKind

::: supermercapy.ProductGroup

::: supermercapy.match.SAME_THRESHOLD

::: supermercapy.match.ALTERNATIVE_THRESHOLD

## async and caching

::: supermercapy.AsyncClient

::: supermercapy.CacheTransport

## product models

::: supermercapy.ProductSummary

::: supermercapy.Product

::: supermercapy.Price

::: supermercapy.UnitPrice

::: supermercapy.Availability

::: supermercapy.Promotion

::: supermercapy.Nutrition

::: supermercapy.NutritionValue

::: supermercapy.Photo

## listing models

::: supermercapy.Category

::: supermercapy.SearchResult

::: supermercapy.HomeSection

::: supermercapy.Store

## controlled values

::: supermercapy.Capability

::: supermercapy.Language

::: supermercapy.Unit

## exceptions

::: supermercapy.SupermercapyError

::: supermercapy.ConfigurationError

::: supermercapy.UnsupportedOperationError

::: supermercapy.TransportError

::: supermercapy.RateLimitError

::: supermercapy.NotFoundError

::: supermercapy.NotAvailableError

::: supermercapy.OutOfCoverageError

::: supermercapy.BlockedError

::: supermercapy.ChallengedError

::: supermercapy.AuthenticationError

::: supermercapy.InvalidResponseError
