"""synchronous alcampo client."""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx

from .._core.capabilities import Capability
from .._core.client import (
    BaseClient,
    ResponseVerdict,
    RetryPolicy,
    validate_identifier,
    validate_postal_code,
)
from .._core.coerce import as_text
from .._core.exceptions import ConfigurationError, InvalidResponseError
from .._core.models import Language
from .._platforms import ocado
from ._constants import (
    API_URL,
    CHALLENGE_BACKOFF,
    COLLECTION_METHOD,
    COLLECTION_POINTS_PATH,
    CUSTOMER_HEADER,
    DEFAULT_PAGE_SIZE,
    DEFAULT_REGION_ID,
    DEFAULT_SUGGESTION_LIMIT,
    MAX_CATEGORY_DEPTH,
    MAX_PAGE_SIZE,
    NEW_FILTER,
    SESSION_PATH,
    VISITOR_HEADER,
)
from .models import (
    PARSER,
    AlcampoCategory,
    AlcampoProduct,
    AlcampoSearchResult,
    AlcampoStore,
    SortOption,
    parse_stores,
)

# how long a region switch is trusted before the session is switched again; the
# anonymous session's cookies live an hour
_SESSION_TTL = 1800.0


def _validate_store(store: str | AlcampoStore | None) -> AlcampoStore | str | None:
    if store is None or isinstance(store, AlcampoStore):
        return store
    return validate_identifier(store, "store")


class Alcampo(BaseClient):
    """a reusable synchronous client for the alcampo online shop.

    the storefront runs on ocado smart platform, like bonpreu's, and serves
    anonymous json from one host with no key. prices and assortment follow a
    region, one fulfilment centre behind a group of click-and-collect points;
    a new anonymous session lands in the "vaguada" region in madrid. an
    unbound client reads that default region and costs no extra request.
    binding a client to one of the points :meth:`list_stores` returns moves
    the client's own anonymous session to that point's region, the way the
    storefront's own region picker does, before the first request.

    an aws waf budget is the constraint, as on bonpreu: it is per ip, seven or
    eight requests to the product pages service per half hour in october
    2026, and once spent that service answers http 202 with an empty body for
    tens of minutes. a challenge raises :class:`~supermercapy.ChallengedError`
    after one request and is never retried, and ``min_request_interval``
    defaults to two seconds. the same waf refuses search and the product sheet
    to a user agent it does not take for a browser, so the default user agent
    is a browser string.

    ``CATALOG`` is not declared: the product sitemap lists more than fifty
    thousand products, and no walk of the catalogue fits the budget.
    ``POSTAL_CODE`` is absent because the storefront resolves an address to a
    region from browser-side coordinates and its postcode services answer
    nothing for this tenant, and ``EAN`` because no barcode exists in the
    api.
    """

    store_name = "alcampo"
    capabilities = frozenset(
        {
            Capability.STORES,
            Capability.NUTRITION,
            Capability.PROMOTIONS,
            Capability.NEW_ARRIVALS,
            Capability.OFFERS,
        }
    )
    supported_languages = frozenset({Language.SPANISH})
    default_user_agent = ocado.BROWSER_USER_AGENT
    default_page_size = DEFAULT_PAGE_SIZE
    max_page_size = MAX_PAGE_SIZE
    default_min_request_interval = 2.0

    def __init__(
        self,
        store: str | AlcampoStore | None = None,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        chosen = _validate_store(store)
        self._store_id: str | None
        self._region_id: str | None
        if isinstance(chosen, AlcampoStore):
            self._store_id, self._region_id = chosen.id, chosen.region_id
        elif chosen is None:
            self._store_id, self._region_id = None, DEFAULT_REGION_ID
        else:
            self._store_id, self._region_id = chosen, None
        super().__init__(
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )
        self._ocado = ocado.Storefront(
            self,
            api_url=API_URL,
            parser=PARSER,
            sort_options=SortOption,
            challenge_backoff=CHALLENGE_BACKOFF,
        )

    @property
    def store_id(self) -> str | None:
        """return the click-and-collect point this client is bound to.

        ``None`` means unbound: the client reads the region a new anonymous
        session lands in.
        """

        return self._store_id

    @property
    def region_id(self) -> str | None:
        """return the region the client's prices and assortment come from.

        a client bound by a bare store id learns its region on the first
        request, and returns ``None`` until then.
        """

        return self._region_id

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
    ) -> AlcampoSearchResult:
        """search one page of products.

        the cursor is the storefront's own ``nextPageToken``, passed back
        verbatim. it is session scoped, so it only works from the client that
        produced it. ``category_id`` narrows the search to one category,
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

    def get_product(self, product_id: str | int) -> AlcampoProduct:
        """return one product sheet by its numeric ``retailerProductId``.

        the internal product uuid that listings, similar and related speak is
        not accepted here; it is kept on
        :attr:`~supermercapy.alcampo.AlcampoProduct.product_uuid`.
        """

        return self._ocado.product(product_id)

    def get_categories(
        self, *, depth: int = MAX_CATEGORY_DEPTH
    ) -> tuple[AlcampoCategory, ...]:
        """return the whole category tree, nested, in one request.

        ``depth`` trims the tree to fewer levels; the default is the full
        four.
        """

        return self._ocado.categories(depth, max_depth=MAX_CATEGORY_DEPTH)

    def get_category(
        self,
        category_id: str | int,
        *,
        page_size: int | None = None,
        sort: SortOption | str | None = None,
        filters: object = None,
    ) -> AlcampoCategory:
        """return one category with its children and its first page of products.

        an unknown category raises :class:`~supermercapy.NotFoundError`, and
        so does a listing that echoes back a category other than the one asked
        for. the category's ``product_count`` is the storefront's own count
        and can exceed what the bound region lists.
        """

        return self._ocado.category(
            category_id, page_size=page_size, sort=sort, filters=filters
        )

    def list_stores(self, postal_code: str | None = None) -> tuple[AlcampoStore, ...]:
        """return the click-and-collect points, with the region behind each.

        one request returns every point in the country. with a
        ``postal_code`` the list keeps the points in its province, those in
        the postal code itself first.
        """

        code = None if postal_code is None else validate_postal_code(postal_code)
        data = self._request_json_any(
            "GET",
            f"{API_URL}{COLLECTION_POINTS_PATH}",
            params={"deliveryMethod": COLLECTION_METHOD},
        )
        stores = parse_stores(data)
        if code is None:
            return stores
        nearby = [
            store
            for store in stores
            if store.postal_code is not None and store.postal_code[:2] == code[:2]
        ]
        return tuple(sorted(nearby, key=lambda store: store.postal_code != code))

    def get_new_arrivals(self) -> tuple[AlcampoProduct, ...]:
        """return every product the storefront's "nuevo producto" filter selects.

        the filter is the storefront's own, offered on every listing in its
        ``dummyValue`` group; applied to the whole-shop listing it is the
        novelties list. it is read in pages of three hundred until no page
        follows: one request for the 75 products it held in october 2026.
        """

        products = self._ocado.walk(page_size=MAX_PAGE_SIZE, filters=dict(NEW_FILTER))
        return tuple(dataclasses.replace(product, is_new=True) for product in products)

    def get_offers(self) -> tuple[AlcampoProduct, ...]:
        """return the first page of the region's promotions listing.

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
    ) -> AlcampoSearchResult:
        """return one page of the promotions listing, whole shop or one category.

        the listing is keyed on the client's region. unlike the category
        listing it takes either identifier: a ``category_id`` uuid or the
        ``retailer_category_id`` code such as ``"OC1701"``.
        """

        return self._ocado.promotions(
            region_id=self._region(),
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

        return self._ocado.suggest(term, limit=limit, region_id=self._region())

    def get_similar(self, product_id: str | int) -> tuple[AlcampoProduct, ...]:
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
    ) -> tuple[AlcampoProduct, ...]:
        """return the products the storefront cross-sells with one product."""

        parameters: dict[str, Any] = {}
        if limit is not None:
            parameters["limit"] = ocado.validate_limit(limit, "limit")
        if high_relevance_only:
            parameters["highRelevanceOnly"] = "true"
        return self._ocado.related("related", product_id, parameters)

    # ------------------------------------------------------------------ binding

    def _region(self) -> str:
        """return the bound region, binding the session first if need be."""

        self._ensure_primed()
        if self._region_id is None:  # pragma: no cover - priming sets it
            raise InvalidResponseError("alcampo resolved no region for the store")
        return self._region_id

    def _prime(self) -> None:
        if self._store_id is None:
            return
        if self._region_id is None:
            self._region_id = self._resolve_region(self._store_id)
        self._switch_region(self._store_id, self._region_id)

    def _prime_ttl(self) -> float | None:
        return None if self._store_id is None else _SESSION_TTL

    def _resolve_region(self, store_id: str) -> str:
        for store in self.list_stores():
            if store.id == store_id:
                return store.region_id
        raise ConfigurationError(f"alcampo has no click-and-collect point {store_id}")

    def _switch_region(self, store_id: str, region_id: str) -> None:
        """move the anonymous session to ``region_id`` through one point."""

        self._ocado.ensure_csrf()
        visitor = as_text(self._ocado.session.get("visitorId")) or ""
        session = self._request_json(
            "PUT",
            f"{API_URL}{SESSION_PATH}",
            json={"deliveryDestinationId": store_id, "regionId": region_id},
            headers={VISITOR_HEADER: visitor, CUSTOMER_HEADER: ""},
        )
        if as_text(session.get("regionId")) != region_id:
            raise InvalidResponseError(f"alcampo did not move to region {region_id}")

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
