"""synchronous lidl client."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from typing import Any, Self

import httpx

from .._core.capabilities import Capability
from .._core.client import (
    BaseClient,
    RetryPolicy,
    validate_identifier,
    validate_postal_code,
)
from .._core.exceptions import (
    ConfigurationError,
    NotFoundError,
    OutOfCoverageError,
)
from .._core.html import attribute_values
from .._core.models import Language
from ._constants import (
    API_URL,
    ASSORTMENT,
    CAMPAIGN_PATH,
    COUNTRY,
    DEFAULT_FETCH_SIZE,
    DETAIL_PATH,
    LANGUAGE_PATHS,
    LEAFLET_CLIENT_LOCALE,
    LEAFLETS_URL,
    MAX_FETCH_SIZE,
    OFFER_CAMPAIGNS,
    SEARCH_ACCEPT,
    SEARCH_LOCALES,
    SEARCH_PATH,
    SEARCH_VERSION,
    SITEMAP_PATH,
    STORES_API_KEY,
    STORES_LOCALE,
    STORES_PAGE_SIZE,
    STORES_URL,
)
from .models import (
    LidlCategory,
    LidlLeaflet,
    LidlProduct,
    LidlSearchResult,
    LidlStore,
    OfferWeek,
    PriceZone,
    ProductFamily,
    parent_id_of,
    parse_categories,
    parse_category,
    parse_leaflet,
    parse_leaflets,
    parse_product,
    parse_search_result,
    parse_sitemap_ids,
    parse_stores,
    store_total,
)

_HTML_ACCEPT = "text/html,application/xhtml+xml"
_XML_ACCEPT = "application/xml"
_GZIP_MAGIC = b"\x1f\x8b"


def _validate_region(region: int | str | None) -> str | None:
    if region is None:
        return None
    if isinstance(region, bool) or not isinstance(region, (int, str)):
        raise ConfigurationError("region must be a non-negative integer")
    text = str(region).strip()
    if not text.isdigit():
        raise ConfigurationError("region must be a non-negative integer")
    return str(int(text))


def _validate_zone(zone: PriceZone | str) -> str:
    try:
        return PriceZone(zone).value
    except ValueError as error:
        supported = ", ".join(repr(item.value) for item in PriceZone)
        raise ConfigurationError(f"zone must be one of {supported}") from error


def _validate_family(family: ProductFamily | str | None) -> ProductFamily | None:
    if family is None:
        return None
    try:
        return ProductFamily(family)
    except ValueError as error:
        supported = ", ".join(repr(item.value) for item in ProductFamily)
        raise ConfigurationError(f"family must be one of {supported}") from error


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise ConfigurationError("cursor must be an offset returned by search")
    return int(cursor)


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ConfigurationError("query must be a non-empty string")
    # the wildcard is indexed but capped at about a hundred rows and carries no
    # grocery hits at all, so it looks like a catalog and is not one.
    if query.strip() == "*":
        raise ConfigurationError(
            "query must not be '*'; enumerate the catalog with iter_catalog"
        )
    return query


class Lidl(BaseClient):
    """a reusable synchronous client for lidl's spanish storefront.

    lidl publishes two assortments through one api: the mail-order shop, priced
    per delivery ``zone``, and the in-store grocery range, priced per offer
    ``region``. both geographies travel inside every response, so binding one is
    a client-side selection that costs no extra request; resolve a pair with
    :meth:`from_postal_code` or pick one from :meth:`list_stores`.

    grocery prices are promotional only. lidl publishes this week's and next
    week's campaign prices and permanent reductions, not a full shelf-price
    list, so a grocery product outside a campaign carries no price at all.

    the capability set follows the reconnaissance: ``EAN_LOOKUP`` is absent
    because no endpoint takes an ean, ``NUTRITION`` because no ingredient,
    allergen, or nutrition field exists anywhere in the public apis, and
    ``NEW_ARRIVALS`` and ``HOME`` because their surfaces are cms html with no
    product json behind them.
    """

    store_name = "lidl"
    capabilities = frozenset(
        {
            Capability.POSTAL_CODE,
            Capability.STORES,
            Capability.CATALOG,
            Capability.OFFERS,
            Capability.EAN,
            Capability.PROMOTIONS,
            Capability.FUTURE_PRICES,
        }
    )
    supported_languages = frozenset({Language.SPANISH})
    default_page_size = DEFAULT_FETCH_SIZE
    max_page_size = MAX_FETCH_SIZE

    def __init__(
        self,
        region: int | str | None = None,
        *,
        zone: PriceZone | str = PriceZone.PENINSULA,
        family: ProductFamily | str | None = None,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._region = _validate_region(region)
        self._zone = _validate_zone(zone)
        self._family = _validate_family(family)
        self._store: LidlStore | None = None
        super().__init__(
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )

    @classmethod
    def from_postal_code(
        cls,
        postal_code: str,
        *,
        family: ProductFamily | str | None = None,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> Self:
        """bind the offer region and price zone of the nearest store.

        the stores api has no postal-code filter, so this pages the whole
        spanish store list once and matches on it: a store in the same postal
        code first, then the first store of the same province. a postal code
        with neither raises :class:`~supermercapy.OutOfCoverageError`.
        """

        postal_code = validate_postal_code(postal_code)
        client = cls(
            family=family,
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )
        try:
            store = client._nearest_store(postal_code)
            if store is None:
                raise OutOfCoverageError(f"lidl has no store near {postal_code}")
            client._bind(store)
            return client
        except BaseException:
            client.close()
            raise

    def _bind(self, store: LidlStore) -> None:
        self._store = store
        self._region = _validate_region(store.offer_region)
        if store.zone is not None:
            self._zone = _validate_zone(store.zone)

    def _nearest_store(self, postal_code: str) -> LidlStore | None:
        stores = self._all_stores()
        exact = [store for store in stores if store.postal_code == postal_code]
        if exact:
            return exact[0]
        province = postal_code[:2]
        nearby = [
            store for store in stores if (store.postal_code or "").startswith(province)
        ]
        return nearby[0] if nearby else None

    # ----------------------------------------------------------------- binding

    @property
    def region(self) -> int | None:
        """return the offer region grocery prices are read for, if any."""

        return None if self._region is None else int(self._region)

    @property
    def zone(self) -> str:
        """return the delivery zone shop prices are read for."""

        return self._zone

    @property
    def family(self) -> ProductFamily | None:
        """return the assortment half listings are filtered to, if any."""

        return self._family

    @property
    def store(self) -> LidlStore | None:
        """return the store :meth:`from_postal_code` resolved, if any."""

        return self._store

    @property
    def store_id(self) -> str | None:
        """return the offer region as a string; the same value as ``region``."""

        return self._region

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        family: ProductFamily | str | None = None,
        sort: str | None = None,
    ) -> LidlSearchResult:
        """search one page of both assortments.

        the cursor is the next offset as a string; ``None`` starts at zero.
        ``family`` narrows the page to one assortment, defaulting to the
        client's own filter. the search index holds groceries, but only for
        real keyword queries.
        """

        return self._listing(
            {"q": _validate_query(query)},
            query=query,
            page_size=page_size,
            cursor=cursor,
            family=family,
            sort=sort,
        )

    def get_product(self, product_id: str | int) -> LidlProduct:
        """return one product, by either its parent or its variant id.

        the detail endpoint accepts parent ids only, so a variant id — the
        parent plus three digits — is normalised before the request.
        """

        wanted = parent_id_of(validate_identifier(product_id, "product_id"))
        data = self._request_json(
            "GET", f"{API_URL}{DETAIL_PATH}/{wanted}/{COUNTRY}/{self._language_path}"
        )
        return self._product(data)

    def get_categories(self) -> tuple[LidlCategory, ...]:
        """return the roots of the shop category tree, read from one facet.

        there is no category endpoint: ``/q/api/category`` is routed but
        answers http 404 for spain, so the roots come from the ``category``
        facet of an empty listing. that facet does not expand them, so their
        ``children`` are empty; :meth:`get_category` returns one level more.
        grocery items are not in the tree — their taxonomy is
        :attr:`LidlProduct.category_path`.
        """

        return parse_categories(self._search({"fetchsize": 1}))

    def get_category(self, category_id: str | int) -> LidlCategory:
        """return one shop category at any depth, its children, and a page."""

        wanted = validate_identifier(category_id, "category_id")
        data = self._search(
            {"fetchsize": self.default_page_size, "offset": 0, "category.id": wanted}
        )
        page = parse_search_result(
            data,
            query="",
            offset=0,
            page_size=self.default_page_size,
            region=self._region,
            zone=self._zone,
            family=self._family,
        )
        category = parse_category(data, category_id=wanted, products=page.products)
        if category is None:
            raise NotFoundError(f"lidl has no category {wanted}")
        return category

    def list_stores(self, postal_code: str | None = None) -> tuple[LidlStore, ...]:
        """return every spanish store, or the ones in one postal code.

        the api pages two hundred and fifty stores at a time and takes no
        postal-code filter, so a postal code is matched on the full list.
        """

        wanted = None if postal_code is None else validate_postal_code(postal_code)
        stores = self._all_stores()
        if wanted is None:
            return stores
        return tuple(store for store in stores if store.postal_code == wanted)

    def iter_catalog(
        self, *, family: ProductFamily | str | None = None
    ) -> Iterator[LidlProduct]:
        """yield every catalogued product, hydrating one sitemap id at a time.

        the sitemap holds about six thousand shop ids and six hundred grocery
        ids, and each one costs a request, so pass ``family`` — or bind one on
        the client — to walk half of it. ids the storefront has already
        retired are skipped rather than raising.
        """

        for product_id in self.iter_catalog_ids(family=family):
            try:
                yield self.get_product(product_id)
            except NotFoundError:
                continue

    def get_offers(
        self, *, week: OfferWeek | str = OfferWeek.CURRENT
    ) -> tuple[LidlProduct, ...]:
        """return one week's grocery campaign in a single request.

        ``week`` names one of the campaign pages lidl publishes:
        ``current``, ``next``, ``weekend``, ``weekend_next``,
        ``permanent_cuts``, or ``other_brands``. next week's page prices its
        products through :attr:`LidlProduct.future_prices` rather than
        :attr:`~supermercapy.Product.price`.
        """

        try:
            slug, campaign_id = OFFER_CAMPAIGNS[OfferWeek(week).value]
        except ValueError as error:
            supported = ", ".join(repr(item.value) for item in OfferWeek)
            raise ConfigurationError(f"week must be one of {supported}") from error
        return self.get_campaign_products(slug, campaign_id)

    # -------------------------------------------------------------- extensions

    def get_campaign_products(
        self, slug: str, campaign_id: str | int
    ) -> tuple[LidlProduct, ...]:
        """return the products of any ``/c/<slug>/a<id>`` campaign page.

        campaign listings have no json endpoint, so the page is fetched as
        html and each tile's ``data-grid-data`` attribute — the same gridbox a
        search hit carries — is parsed out of it. the pages are half a
        megabyte and more, so cache what comes back.
        """

        if not isinstance(slug, str) or not slug.strip():
            raise ConfigurationError("slug must be a non-empty string")
        identifier = validate_identifier(campaign_id, "campaign_id")
        page = self._request_text(
            "GET",
            f"{API_URL}{CAMPAIGN_PATH}/{slug.strip()}/a{identifier}",
            headers={"Accept": _HTML_ACCEPT},
        )
        products: list[LidlProduct] = []
        for value in attribute_values(page, "div", "data-grid-data"):
            try:
                tile = json.loads(value)
            except ValueError:
                continue
            product = self._product(tile)
            if self._family is None or product.family is self._family:
                products.append(product)
        return tuple(products)

    def iter_catalog_ids(
        self, *, family: ProductFamily | str | None = None
    ) -> Iterator[str]:
        """yield the product ids of the sitemap, one request for all of them.

        the leading digits split the two assortments — ``10`` is the online
        shop, ``11`` the in-store grocery range — so a family is filtered
        without fetching anything.
        """

        wanted = _validate_family(family) or self._family
        path = SITEMAP_PATH.format(country=COUNTRY, language=self._language_path)
        response = self._request(
            "GET", f"{API_URL}{path}", headers={"Accept": _XML_ACCEPT}
        )
        body = response.content
        if body.startswith(_GZIP_MAGIC):
            body = gzip.decompress(body)
        yield from parse_sitemap_ids(body.decode("utf-8", "replace"), family=wanted)

    def get_leaflets(
        self, *, region: int | str | None = None, store: str | None = None
    ) -> tuple[LidlLeaflet, ...]:
        """return the leaflets on offer, narrowed to a region or a store.

        ``region`` defaults to the client's binding; pass ``0`` for the
        national ones and leave both arguments out on an unbound client to
        enumerate every leaflet.
        """

        params: dict[str, Any] = {"client_locale": LEAFLET_CLIENT_LOCALE}
        chosen = _validate_region(region) if region is not None else self._region
        if chosen is not None:
            params["region_id"] = chosen
        if store is not None:
            params["store_id"] = validate_identifier(store, "store")
        return parse_leaflets(
            self._request_json("GET", f"{LEAFLETS_URL}/overview", params=params)
        )

    def get_leaflet(
        self,
        identifier: str,
        *,
        region: int | str | None = None,
        store: str | None = None,
    ) -> LidlLeaflet:
        """return one leaflet with its pages, by slug or by uuid.

        food leaflets carry page images and no product data at all; only bazar
        leaflets fill :attr:`LidlLeaflet.products`. slugs embed the week and a
        random suffix and change every week, so resolve one through
        :meth:`get_leaflets` rather than caching it.
        """

        params: dict[str, Any] = {
            "flyer_identifier": validate_identifier(identifier, "identifier")
        }
        chosen = _validate_region(region) if region is not None else self._region
        if chosen is not None:
            params["region_id"] = chosen
        if store is not None:
            params["store_id"] = validate_identifier(store, "store")
        return parse_leaflet(
            self._request_json("GET", f"{LEAFLETS_URL}/flyer", params=params)
        )

    # ---------------------------------------------------------------- internals

    @property
    def _language_path(self) -> str:
        return LANGUAGE_PATHS[self._language]

    def _product(self, data: object, meta: object = None) -> LidlProduct:
        return parse_product(data, meta=meta, region=self._region, zone=self._zone)

    def _search(self, parameters: dict[str, Any]) -> dict[str, object]:
        params: dict[str, Any] = {
            "assortment": ASSORTMENT,
            "locale": SEARCH_LOCALES[self._language],
            "version": SEARCH_VERSION,
        }
        params.update(parameters)
        return self._request_json(
            "GET",
            f"{API_URL}{SEARCH_PATH}",
            params=params,
            headers={"Accept": SEARCH_ACCEPT},
        )

    def _listing(
        self,
        parameters: dict[str, Any],
        *,
        query: str,
        page_size: int | None,
        cursor: str | None,
        family: ProductFamily | str | None,
        sort: str | None,
    ) -> LidlSearchResult:
        size = self._resolve_page_size(page_size)
        offset = _validate_offset(cursor)
        params: dict[str, Any] = {"fetchsize": size, "offset": offset}
        params.update(parameters)
        if sort is not None:
            if not isinstance(sort, str) or not sort.strip():
                raise ConfigurationError("sort must be a non-empty string")
            params["sort"] = sort
        return parse_search_result(
            self._search(params),
            query=query,
            offset=offset,
            page_size=size,
            region=self._region,
            zone=self._zone,
            family=_validate_family(family) or self._family,
        )

    def _all_stores(self) -> tuple[LidlStore, ...]:
        stores: list[LidlStore] = []
        offset = 0
        while True:
            data = self._request_json(
                "GET",
                f"{STORES_URL}/stores",
                params={
                    "country_code": COUNTRY,
                    "locale": STORES_LOCALE,
                    "limit": STORES_PAGE_SIZE,
                    "offset": offset,
                },
                headers={"x-apikey": STORES_API_KEY},
            )
            page = parse_stores(data)
            stores.extend(page)
            total = store_total(data)
            offset += len(page)
            if not page or total is None or offset >= total:
                return tuple(stores)
