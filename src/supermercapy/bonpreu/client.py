"""synchronous bonpreu client."""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx

from .._core.capabilities import Capability
from .._core.client import BaseClient, ResponseVerdict, RetryPolicy
from .._core.models import Language
from .._platforms import ocado
from ._constants import (
    API_URL,
    CHALLENGE_BACKOFF,
    DEFAULT_PAGE_SIZE,
    DEFAULT_SUGGESTION_LIMIT,
    HOST,
    LANGUAGE_COOKIE,
    LANGUAGE_VALUES,
    MAX_CATEGORY_DEPTH,
    MAX_PAGE_SIZE,
    NEW_ARRIVALS_CATEGORY_ID,
    REGION_ID,
)
from .models import (
    PARSER,
    BonpreuCategory,
    BonpreuProduct,
    BonpreuSearchResult,
    SortOption,
)


class Bonpreu(BaseClient):
    """a reusable synchronous client for the bonpreu and esclat online shop.

    the storefront runs on ocado smart platform and serves anonymous json from
    one host, with no key, no binding and no postcode step: the tenant has a
    single region, and a delivery destination a hundred kilometres away
    resolves to that same region with the cart reporting its assortment
    unchanged. language is the only lever, and it travels as a cookie.

    an aws waf budget is the real constraint. it is per ip, worth twenty to
    thirty requests cold in september 2026 and eight to thirteen in october,
    and once spent it answers http 202 with an empty body for tens of minutes;
    pacing delays it but does not prevent it, and every challenged request
    appears to spend from it again. so a challenge raises
    :class:`~supermercapy.ChallengedError` after one request and is never retried,
    and ``min_request_interval`` defaults to two seconds. the category tree
    was the one path never seen challenged, which makes :meth:`get_categories`
    the cheap first call.

    the same waf refuses ``/api/`` outright to a user agent it does not take
    for a browser, with cloudfront's http 403 "request blocked" page, so the
    default user agent is a browser string. that refusal raises
    :class:`~supermercapy.BlockedError`; the origin's own 403, a missing csrf
    token, is told apart by the ``requestid`` header it carries.

    ``CATALOG`` is deliberately not declared. the catalogue is twenty-one
    thousand products and the only ways to walk it — paging every leaf, or the
    sitemap plus one sheet request each — both need far more requests than the
    budget allows, so an ``iter_catalog`` that always dies part way through
    would be a promise the store cannot keep. ``EAN`` is absent because no
    barcode exists anywhere in the api, and ``POSTAL_CODE``, ``STORES`` and
    ``HOME`` because there is nothing for them to select or serve.
    """

    store_name = "bonpreu"
    capabilities = frozenset(
        {
            Capability.NUTRITION,
            Capability.PROMOTIONS,
            Capability.NEW_ARRIVALS,
            Capability.OFFERS,
        }
    )
    supported_languages = frozenset({Language.CATALAN, Language.SPANISH})
    default_language = Language.CATALAN
    default_user_agent = ocado.BROWSER_USER_AGENT
    default_page_size = DEFAULT_PAGE_SIZE
    max_page_size = MAX_PAGE_SIZE
    default_min_request_interval = 2.0

    def __init__(
        self,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        super().__init__(
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )
        # the storefront reads the locale from a cookie and ignores
        # accept-language, so it lives in the jar the page tokens also use
        self._client.cookies.set(
            LANGUAGE_COOKIE, LANGUAGE_VALUES[self._language], domain=HOST, path="/"
        )
        self._ocado = ocado.Storefront(
            self,
            api_url=API_URL,
            parser=PARSER,
            sort_options=SortOption,
            challenge_backoff=CHALLENGE_BACKOFF,
        )

    @property
    def region_id(self) -> str:
        """return the one region the tenant serves; there is nothing to select."""

        return REGION_ID

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        category_id: str | int | None = None,
        sort: SortOption | str | None = None,
        filters: object = None,
    ) -> BonpreuSearchResult:
        """search one page of products.

        the cursor is the storefront's own ``nextPageToken``, passed back
        verbatim. it is session scoped, so it only works from the client that
        produced it — the cookies that scope it live on this client and are
        replayed for you. ``category_id`` narrows the search to one category,
        ``sort`` reorders it, and ``filters`` takes a mapping of filter group
        id to one or more attribute ids.

        queries longer than fifty characters are truncated, as the storefront
        truncates its own.
        """

        return self._ocado.search(
            query,
            page_size=page_size,
            cursor=cursor,
            category_id=category_id,
            sort=sort,
            filters=filters,
        )

    def get_product(self, product_id: str | int) -> BonpreuProduct:
        """return one product sheet by its numeric ``retailerProductId``.

        the internal product uuid that listings, similar and related speak is
        not accepted here; it is kept on
        :attr:`~supermercapy.bonpreu.BonpreuProduct.product_uuid`.
        """

        return self._ocado.product(product_id)

    def get_categories(
        self, *, depth: int = MAX_CATEGORY_DEPTH
    ) -> tuple[BonpreuCategory, ...]:
        """return the whole category tree, nested, in one request.

        this is the one path that kept answering while the challenge was
        active, so treat it as the cheap call and the health check. ``depth``
        trims the tree to fewer levels; the default is the full four.
        """

        return self._ocado.categories(depth, max_depth=MAX_CATEGORY_DEPTH)

    def get_category(
        self,
        category_id: str | int,
        *,
        page_size: int | None = None,
        sort: SortOption | str | None = None,
        filters: object = None,
    ) -> BonpreuCategory:
        """return one category with its children and its first page of products.

        an unknown category answers http 404, which raises
        :class:`~supermercapy.NotFoundError`. until september 2026 it answered with
        the root pseudo-category and a generic page instead, so the category
        the listing reports serving is still checked against the one that was
        asked for, and a mismatch raises the same error.
        """

        return self._ocado.category(
            category_id, page_size=page_size, sort=sort, filters=filters
        )

    def get_new_arrivals(self) -> tuple[BonpreuProduct, ...]:
        """return every product the storefront collects under "novetats".

        membership of that category is what marks a product new here; the
        ``isNew`` flag on a row is about the row, not the category. the
        category is read in pages of three hundred, the largest the listing
        serves, until the count it reports on the first page is reached: two
        requests for the 510 products it held in october 2026.
        """

        products = self._ocado.walk(
            page_size=MAX_PAGE_SIZE, category_id=NEW_ARRIVALS_CATEGORY_ID
        )
        return tuple(dataclasses.replace(product, is_new=True) for product in products)

    def get_offers(self) -> tuple[BonpreuProduct, ...]:
        """return the first page of the shop-wide promotions listing.

        :meth:`get_promotions` is the same listing with paging, a category
        filter, and the page's own filter and sort metadata.
        """

        return self.get_promotions().products

    # -------------------------------------------------------------- extensions

    def get_promotions(
        self,
        *,
        category_id: str | int | None = None,
        retailer_category_id: str | int | None = None,
        page_size: int | None = None,
        cursor: str | None = None,
        sort: SortOption | str | None = None,
        filters: object = None,
    ) -> BonpreuSearchResult:
        """return one page of the promotions listing, whole shop or one category.

        unlike the category listing this one takes either identifier: a
        ``category_id`` uuid or the hierarchical ``retailer_category_id`` code
        such as ``"0301"``.
        """

        return self._ocado.promotions(
            region_id=REGION_ID,
            category_id=category_id,
            retailer_category_id=retailer_category_id,
            page_size=page_size,
            cursor=cursor,
            sort=sort,
            filters=filters,
        )

    def suggest(
        self, term: str, *, limit: int = DEFAULT_SUGGESTION_LIMIT
    ) -> tuple[str, ...]:
        """return the autocomplete suggestions for a partial search term."""

        return self._ocado.suggest(term, limit=limit, region_id=REGION_ID)

    def get_similar(self, product_id: str | int) -> tuple[BonpreuProduct, ...]:
        """return the products the storefront offers as similar to one product.

        the endpoint answers with internal uuids alone, so the rows are
        resolved with one batch request; an empty answer costs nothing more.
        """

        return self._ocado.related("similar", product_id, {})

    def get_related(
        self,
        product_id: str | int,
        *,
        limit: int | None = None,
        high_relevance_only: bool = False,
    ) -> tuple[BonpreuProduct, ...]:
        """return the products the storefront cross-sells with one product."""

        parameters: dict[str, Any] = {}
        if limit is not None:
            parameters["limit"] = ocado.validate_limit(limit, "limit")
        if high_relevance_only:
            parameters["highRelevanceOnly"] = "true"
        return self._ocado.related("related", product_id, parameters)

    # ---------------------------------------------------------- transport hooks

    def _prepare_request(self, request: httpx.Request) -> None:
        self._ocado.prepare(request)

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        verdict = self._ocado.classify(response)
        return super()._classify(response) if verdict is None else verdict

    def _on_auth_expired(self) -> bool:
        return self._ocado.on_auth_expired()

    def _on_challenge(self, response: httpx.Response) -> bool:
        return self._ocado.on_challenge(response)
