"""synchronous aldi client."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from types import MappingProxyType
from typing import Any, Self
from urllib.parse import quote, urlencode

import httpx

from .._core.capabilities import Capability
from .._core.client import (
    BaseClient,
    ResponseVerdict,
    RetryPolicy,
    validate_identifier,
    validate_postal_code,
)
from .._core.coerce import JsonObject
from .._core.exceptions import AuthenticationError, ConfigurationError, NotFoundError
from .._core.models import Language
from ._constants import (
    ALGOLIA_API_KEY,
    ALGOLIA_APP_ID,
    ALGOLIA_URL,
    INDEX_TEMPLATE,
    MAX_PAGE_SIZE,
    SITE_URL,
)
from .models import (
    REGION_PATHS,
    AldiCategory,
    AldiOfferGroup,
    AldiProduct,
    AldiSearchResult,
    Region,
    api_data,
    next_data,
    page_props,
    parse_category_page,
    parse_children,
    parse_hits,
    parse_offer_groups,
    parse_product,
)

_HTML_ACCEPT = "text/html,application/xhtml+xml"
_OBJECT_ID = re.compile(r"\d+")
# a category id is the page path below /productos: one or two slugs
_CATEGORY_ID = re.compile(r"[a-z0-9_-]+(?:/[a-z0-9_-]+)?")
_CATEGORY_KEY = re.compile(r"[a-z0-9_-]+")
# the parameters every query sends: no highlighting, which only doubles the
# answer with marked-up copies of the matched fields
_QUERY_DEFAULTS = {"attributesToHighlight": "[]"}
_ALGOLIA_HOST = httpx.URL(ALGOLIA_URL).host
# the province, the first two digits of a postcode, that leaves the península
# price list. aldi's region picker offers only these three regions, so ceuta
# (51), which has no aldi store, and melilla (52), which has one, read the
# península prices like every other province.
_PROVINCE_REGIONS = MappingProxyType(
    {
        "07": Region.BALEARIC_ISLANDS,
        "35": Region.CANARY_ISLANDS,
        "38": Region.CANARY_ISLANDS,
    }
)


def region_for_postal_code(postal_code: str) -> Region:
    """return the price region a postcode's province belongs to, locally."""

    province = validate_postal_code(postal_code)[:2]
    return _PROVINCE_REGIONS.get(province, Region.PENINSULA)


def _validate_region(region: Region | str) -> Region:
    try:
        return Region(region)
    except ValueError as error:
        supported = ", ".join(repr(item.value) for item in Region)
        raise ConfigurationError(f"region must be one of {supported}") from error


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit():
        raise ConfigurationError("cursor must be an offset returned by search")
    return int(cursor)


def _filter_value(value: str) -> str:
    # algolia filter values are quoted strings; a key is a plain slug
    return f'"{value}"'


class Aldi(BaseClient):
    """a reusable synchronous client for aldi's spanish product catalogue.

    aldi runs no online shop. ``www.aldi.es`` publishes the fixed assortment
    and the weekly offers with prices, and reads both from a public algolia
    index, one per price region: the península, the balearic islands and the
    canary islands. search, product records, category listings and the
    catalogue come straight from that index with the site's own search-only
    key; the category tree and the offers come from the json the site embeds
    in its pages.

    a region is the only binding and a postcode maps to one by its province
    without a request, so :meth:`from_postal_code` is a local lookup. regions
    change prices and, for the canary islands, the assortment.

    ``EAN``, ``EAN_LOOKUP`` and ``NUTRITION`` are left out because no record
    carries a barcode, ingredients or a nutrition table. ``STORES`` is left
    out because a store binds nothing: prices follow the region. there is no
    novelties feed and no machine-readable home page.
    """

    store_name = "aldi"
    capabilities = frozenset(
        {
            Capability.POSTAL_CODE,
            Capability.CATALOG,
            Capability.OFFERS,
            Capability.PROMOTIONS,
            Capability.FUTURE_PRICES,
        }
    )
    supported_languages = frozenset({Language.SPANISH})
    default_page_size = 24
    max_page_size = MAX_PAGE_SIZE

    def __init__(
        self,
        region: Region | str = Region.PENINSULA,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._region = _validate_region(region)
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
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> Self:
        """return a client for the price region of a postcode's province.

        this makes no request: balearic postcodes (07) map to the balearic
        region, las palmas (35) and santa cruz de tenerife (38) to the canary
        one, and every other province, ceuta and melilla included, to the
        península.
        """

        return cls(
            region_for_postal_code(postal_code),
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )

    @property
    def region(self) -> Region:
        """return the price region this client reads."""

        return self._region

    @property
    def store_id(self) -> str:
        """return the region code, such as ``"pen"``."""

        return self._region.value

    @property
    def index_name(self) -> str:
        """return the algolia index of the client's region."""

        return INDEX_TEMPLATE.format(region=self._region.value)

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> AldiSearchResult:
        """search one page of the region's index.

        the cursor is the next row offset as a string; algolia serves any
        window of up to a thousand rows.
        """

        if not isinstance(query, str) or not query.strip():
            raise ConfigurationError("query must be a non-empty string")
        return self._query(
            query=query,
            offset=_validate_offset(cursor),
            page_size=self._resolve_page_size(page_size),
        )

    def get_product(self, product_id: str | int) -> AldiProduct:
        """return one product record by its numeric object id, in one request."""

        object_id = validate_identifier(product_id, "product_id")
        if not _OBJECT_ID.fullmatch(object_id):
            raise ConfigurationError("product_id must be a numeric aldi object id")
        try:
            data = self._request_json(
                "GET",
                f"{self._index_url}/{quote(object_id, safe='')}",
                headers=self._algolia_headers(),
            )
        except NotFoundError:
            raise NotFoundError(
                f"aldi has no product {object_id} in the {self._region.value} index"
            ) from None
        return parse_product(data, region=self._region)

    def get_categories(self, *, deep: bool = False) -> tuple[AldiCategory, ...]:
        """return the top-level categories of the site's product overview.

        one request reads the overview page. ``deep=True`` adds each
        category's children at one more request per category, twenty more in
        october 2026. categories the site hides, such as an out-of-season
        "verano", are left out.
        """

        answers = api_data(page_props(self._page("/productos.html")))
        roots = parse_children(answers, parent_id=None, level=1)
        if not deep:
            return roots
        return tuple(
            dataclasses.replace(root, children=self._category_page(root.id)[0].children)
            for root in roots
        )

    def get_category(self, category_id: str | int) -> AldiCategory:
        """return one category with its children and every product it lists.

        two requests: the category page, for the node, its children and the
        filter the site lists it with, and one query of the region's index
        for up to a thousand products. a top-level page lists no products of
        its own, so its products are those of all its children together.
        """

        wanted = validate_identifier(category_id, "category_id")
        if not _CATEGORY_ID.fullmatch(wanted):
            raise ConfigurationError(
                "category_id must be a page path such as "
                "'lacteos-y-huevos/leche-y-bebidas-vegetales'"
            )
        node, filters = self._category_page(wanted)
        if filters is None:
            keys = [child.key for child in node.children if child.key]
            filters = " OR ".join(f"categoryIDs:{_filter_value(key)}" for key in keys)
        if not filters:
            return node
        page = self._query(query="", offset=0, page_size=MAX_PAGE_SIZE, filters=filters)
        return dataclasses.replace(
            node, products=page.products, product_count=page.total_hits
        )

    def iter_catalog(self) -> Iterator[AldiProduct]:
        """yield every record of the region's index, a thousand per request.

        three requests for the 2,342 records of the península index in
        october 2026.
        """

        return self._walk()

    def get_offers(self) -> tuple[AldiProduct, ...]:
        """return this week's offers, once each, in the order the page lists them.

        one request for the region's offers page, which embeds every record
        it shows. :meth:`get_offer_groups` keeps the page's sections and
        their dates.
        """

        found: dict[str, AldiProduct] = {}
        for group in self.get_offer_groups():
            for product in group.products:
                found.setdefault(product.id, product)
        return tuple(found.values())

    # -------------------------------------------------------------- extensions

    def get_offer_groups(self) -> tuple[AldiOfferGroup, ...]:
        """return the sections of this week's offers page with their dates."""

        document = self._page(f"{REGION_PATHS[self._region]}/ofertas.html")
        return parse_offer_groups(document, region=self._region)

    def get_category_products(
        self,
        key: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> AldiSearchResult:
        """return one page of the products filed under a category key.

        the key is what a product's ``category_ids`` and
        :attr:`AldiCategory.key` hold, so this reaches a product's category
        in one request without its page.
        """

        if not isinstance(key, str) or not _CATEGORY_KEY.fullmatch(key):
            raise ConfigurationError("key must be a category key such as 'frutas'")
        return self._query(
            query="",
            offset=_validate_offset(cursor),
            page_size=self._resolve_page_size(page_size),
            filters=f"categoryIDs:{_filter_value(key)}",
        )

    # ---------------------------------------------------------------- internals

    def _walk(self) -> Iterator[AldiProduct]:
        offset = 0
        while True:
            page = self._query(query="", offset=offset, page_size=MAX_PAGE_SIZE)
            yield from page.products
            if page.next_cursor is None:
                return
            offset = int(page.next_cursor)

    def _query(
        self,
        *,
        query: str,
        offset: int,
        page_size: int,
        filters: str | None = None,
    ) -> AldiSearchResult:
        params: dict[str, Any] = {
            "query": query,
            "offset": offset,
            "length": page_size,
            **_QUERY_DEFAULTS,
        }
        if filters is not None:
            params["filters"] = filters
        data = self._request_json(
            "POST",
            f"{self._index_url}/query",
            headers=self._algolia_headers(),
            json={"params": urlencode(params)},
        )
        return parse_hits(
            data,
            query=query,
            offset=offset,
            page_size=page_size,
            region=self._region,
        )

    def _page(self, path: str) -> JsonObject:
        html = self._request_text(
            "GET", f"{SITE_URL}{path}", headers={"Accept": _HTML_ACCEPT}
        )
        return next_data(html)

    def _category_page(self, category_id: str) -> tuple[AldiCategory, str | None]:
        return parse_category_page(self._page(f"/productos/{category_id}.html"))

    @property
    def _index_url(self) -> str:
        return f"{ALGOLIA_URL}/1/indexes/{self.index_name}"

    def _algolia_headers(self) -> dict[str, str]:
        return {
            "X-Algolia-Application-Id": ALGOLIA_APP_ID,
            "X-Algolia-API-Key": ALGOLIA_API_KEY,
        }

    def _raise_error(
        self,
        response: httpx.Response,
        verdict: ResponseVerdict,
        *,
        method: str,
        url: str,
    ) -> None:
        # algolia answers a key it no longer accepts with 403, and nothing
        # else this client calls answers 403
        if response.status_code == 403 and response.url.host == _ALGOLIA_HOST:
            response.close()
            raise AuthenticationError(
                f"{method} {url} rejected the pinned algolia key; read the current "
                "one from the site's product-overview bundle into ALGOLIA_API_KEY",
                status_code=response.status_code,
            )
        super()._raise_error(response, verdict, method=method, url=url)
