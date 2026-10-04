"""synchronous consum client."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

import httpx

from .._core.capabilities import Capability
from .._core.client import (
    BaseClient,
    ResponseVerdict,
    RetryPolicy,
    validate_identifier,
    validate_postal_code,
)
from .._core.exceptions import (
    ConfigurationError,
    InvalidResponseError,
    NotFoundError,
    OutOfCoverageError,
)
from .._core.models import Language
from ._constants import API_URL
from .models import (
    ConsumCategory,
    ConsumGroup,
    ConsumPhoto,
    ConsumProduct,
    ConsumSearchResult,
    ConsumStore,
    DeliveryMethod,
    ImageSize,
    ProductFilters,
    SortOption,
    SortOrder,
    Suggestion,
    parse_category,
    parse_group,
    parse_product,
    parse_search_result,
    parse_sort_option,
    parse_stores,
    parse_suggestion,
)

# the storefront answers a product the zone does not carry with a 404 and this
# error type, and a code that never existed with exactly the same body, so it
# can only ever mean "not here" and never "exists elsewhere".
_ZONE_ERROR_TYPE = "44"
_BATCH_LIMIT = 50


def _validate_zone(zone: int | str | None) -> str | None:
    if zone is None:
        return None
    if isinstance(zone, bool) or not isinstance(zone, (int, str)):
        raise ConfigurationError("zone must be a positive integer")
    text = str(zone).strip()
    if not text.isdigit() or int(text) < 1:
        raise ConfigurationError("zone must be a positive integer")
    return str(int(text))


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise ConfigurationError("cursor must be an offset returned by search")
    return int(cursor)


def _validate_category(category_id: str | int) -> str:
    wanted = validate_identifier(category_id, "category_id")
    # anything but a numeric id is a 500 upstream, retried for nothing
    if not wanted.isascii() or not wanted.isdigit():
        raise ConfigurationError("category_id must be a numeric category id")
    return wanted


def _validate_order(order_by: SortOrder | int | None) -> int | None:
    if order_by is None:
        return None
    if isinstance(order_by, bool) or not isinstance(order_by, int):
        raise ConfigurationError("order_by must be a SortOrder or an integer")
    return int(order_by)


def _validate_filters(filters: ProductFilters | None) -> str | None:
    if filters is None:
        return None
    if not isinstance(filters, ProductFilters):
        raise ConfigurationError("filters must be a ProductFilters instance")
    return filters.render()


def _validate_method(method: DeliveryMethod | str) -> str:
    try:
        return DeliveryMethod(method).value
    except ValueError as error:
        supported = ", ".join(repr(item.value) for item in DeliveryMethod)
        raise ConfigurationError(f"method must be one of {supported}") from error


class Consum(BaseClient):
    """a reusable synchronous client, optionally scoped to one consum zone.

    a zone narrows the assortment to what one store carries; prices are the
    same nationwide. an unbound client sees a default assortment, not a
    superset of the zones. an unrecognised zone is answered with a catalog
    rather than an error, so resolve one with :meth:`from_postal_code` or
    :meth:`list_stores` instead of guessing.
    """

    store_name = "consum"
    capabilities = frozenset(
        {
            Capability.POSTAL_CODE,
            Capability.STORES,
            Capability.CATALOG,
            Capability.EAN_LOOKUP,
            Capability.NEW_ARRIVALS,
            Capability.OFFERS,
            Capability.EAN,
            Capability.PROMOTIONS,
        }
    )
    supported_languages = frozenset({Language.SPANISH, Language.VALENCIAN})
    default_page_size = 24
    max_page_size = 100

    def __init__(
        self,
        zone: int | str | None = None,
        *,
        drop_sponsored: bool = True,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._zone = _validate_zone(zone)
        self._drop_sponsored = bool(drop_sponsored)
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
        method: DeliveryMethod | str = DeliveryMethod.HOME,
        drop_sponsored: bool = True,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> Self:
        """resolve a postal code with one request and return a scoped client.

        the first area the storefront lists for the postal code wins; use
        :meth:`list_stores` when a postal code maps to more than one.
        """

        postal_code = validate_postal_code(postal_code)
        client = cls(
            drop_sponsored=drop_sponsored,
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )
        try:
            stores = client.list_stores(postal_code, method=method)
            if not stores:
                raise OutOfCoverageError(f"consum does not deliver to {postal_code}")
            client._bind_zone(stores[0].id)
            return client
        except BaseException:
            client.close()
            raise

    def _bind_zone(self, zone: int | str) -> None:
        self._zone = _validate_zone(zone)
        self._client.headers.update(self._zone_headers())

    @property
    def zone(self) -> int | None:
        """return the zone this client is scoped to, if any."""

        return None if self._zone is None else int(self._zone)

    @property
    def store_id(self) -> str | None:
        """return the zone id as a string; the same value as ``zone``."""

        return self._zone

    @property
    def drop_sponsored(self) -> bool:
        """whether injected retail-media rows are removed from listings."""

        return self._drop_sponsored

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        order_by: SortOrder | int | None = None,
        filters: ProductFilters | None = None,
        include_filters: bool = False,
    ) -> ConsumSearchResult:
        """search one page of products.

        the cursor is the next offset as a string; ``None`` starts at zero.
        """

        if not isinstance(query, str):
            raise ConfigurationError("query must be a string")
        return self._list_products(
            query=query,
            offset=_validate_offset(cursor),
            page_size=self._resolve_page_size(page_size),
            order_by=order_by,
            filters=filters,
            include_filters=include_filters,
            parameters={"q": query},
        )

    def get_product(self, product_id: str | int) -> ConsumProduct:
        """return one product by its code."""

        code = validate_identifier(product_id, "product_id")
        data = self._request_json(
            "GET", f"{API_URL}/catalog/product/code/{quote(code, safe='')}"
        )
        return parse_product(data)

    def get_categories(self) -> tuple[ConsumCategory, ...]:
        """return the whole category tree, already nested, in one request."""

        data = self._request_json_any("GET", f"{API_URL}/shopping/category/menu")
        return tuple(parse_category(item) for item in _array(data, "categories"))

    def get_category(self, category_id: str | int) -> ConsumCategory:
        """return one category from the tree, with its first page of products.

        the tree carries no products, so this costs one request for the tree
        and one for the listing.
        """

        wanted = validate_identifier(category_id, "category_id")
        node = _find_category(self.get_categories(), wanted)
        if node is None:
            raise NotFoundError(f"consum has no category {wanted}")
        listing = self.get_category_products(wanted)
        return dataclasses.replace(
            node, products=listing.products, product_count=listing.total_hits
        )

    def list_stores(
        self,
        postal_code: str | None = None,
        *,
        method: DeliveryMethod | str = DeliveryMethod.HOME,
    ) -> tuple[ConsumStore, ...]:
        """return the areas serving ``postal_code`` for one delivery method."""

        if postal_code is None:
            raise ConfigurationError("consum needs a postal_code to find stores")
        data = self._request_json_any(
            "GET",
            f"{API_URL}/shipping/area",
            params={
                "shippingMethod": _validate_method(method),
                "zipCode": validate_postal_code(postal_code),
            },
        )
        return parse_stores(_array(data, "shipping areas"))

    def iter_catalog(self) -> Iterator[ConsumProduct]:
        """yield the whole catalog by walking offsets a hundred rows at a time."""

        return self._walk()

    def get_product_by_ean(self, ean: str) -> ConsumProduct:
        """return one product by its ean, in a single request."""

        code = validate_identifier(ean, "ean")
        page = self._list_products(
            query="", offset=0, page_size=1, parameters={"ean": code}
        )
        if not page.products:
            raise NotFoundError(f"consum has no product with ean {code}")
        return page.products[0]

    def get_new_arrivals(self) -> tuple[ConsumProduct, ...]:
        """return every product the storefront flags as new, newest first.

        the listing is walked a hundred rows per request until it ends.
        """

        return _unique(
            self._walk(order_by=SortOrder.NEWEST, filters=ProductFilters(novelty=True))
        )

    def get_offers(
        self, *, include_deferred: bool = False
    ) -> tuple[ConsumProduct, ...]:
        """return every product carrying an immediate discount.

        the listing is walked a hundred rows per request until it ends, which
        was eleven requests for a thousand offers in october 2026.
        ``include_deferred`` widens the result to products that credit money
        back instead of cutting the shelf price.
        """

        return _unique(
            self._walk(
                order_by=SortOrder.OFFERS_FIRST,
                filters=ProductFilters(
                    offer_immediate=True, offer_deferred=include_deferred
                ),
            )
        )

    # -------------------------------------------------------------- extensions

    def get_category_products(
        self,
        category_id: str | int,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        order_by: SortOrder | int | None = None,
        filters: ProductFilters | None = None,
        include_filters: bool = False,
    ) -> ConsumSearchResult:
        """list one page of a category, descendants included."""

        wanted = _validate_category(category_id)
        return self._list_products(
            query="",
            offset=_validate_offset(cursor),
            page_size=self._resolve_page_size(page_size),
            order_by=order_by,
            filters=filters,
            include_filters=include_filters,
            parameters={"categories": wanted},
        )

    def get_products(self, codes: Iterable[str | int]) -> tuple[ConsumProduct, ...]:
        """return several products in one request, in the order given.

        codes the selected zone does not carry are omitted from the result
        rather than raising, so compare what comes back against what you asked
        for.
        """

        requested: Iterable[str | int] = (codes,) if isinstance(codes, str) else codes
        wanted = tuple(validate_identifier(code, "product code") for code in requested)
        if not wanted:
            raise ConfigurationError("codes must contain at least one product code")
        if len(wanted) > _BATCH_LIMIT:
            raise ConfigurationError(f"codes must hold at most {_BATCH_LIMIT} entries")
        path = quote(",".join(wanted), safe=",")
        data = self._request_json_any("GET", f"{API_URL}/catalog/product/codes/{path}")
        products = {
            product.id: product
            for item in _array(data, "products")
            for product in (parse_product(item),)
        }
        return tuple(products[code] for code in wanted if code in products)

    def get_groups(self) -> tuple[ConsumGroup, ...]:
        """return the promotional campaigns and their nested subgroups."""

        data = self._request_json_any("GET", f"{API_URL}/catalog/group")
        return tuple(parse_group(item) for item in _array(data, "groups"))

    def get_group_products(
        self,
        code: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        order_by: SortOrder | int | None = None,
    ) -> ConsumSearchResult:
        """list one page of a campaign, selected by its :class:`ConsumGroup` code."""

        group_code = validate_identifier(code, "group code")
        return self._list_products(
            query="",
            offset=_validate_offset(cursor),
            page_size=self._resolve_page_size(page_size),
            order_by=order_by,
            parameters={"groups": group_code},
        )

    def get_sort_orders(self) -> tuple[SortOption, ...]:
        """return the sort orders the storefront currently advertises."""

        data = self._request_json_any("GET", f"{API_URL}/catalog/orders")
        return tuple(parse_sort_option(item) for item in _array(data, "sort orders"))

    def suggest(self, query: str, *, limit: int = 5) -> tuple[Suggestion, ...]:
        """return spelling completions for a partial query."""

        if not isinstance(query, str) or not query.strip():
            raise ConfigurationError("query must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ConfigurationError("limit must be a positive integer")
        data = self._request_json_any(
            "GET",
            f"{API_URL}/catalog/searcher/semantics",
            params={"q": query, "limit": limit},
        )
        return self._suggestions(data)

    def suggest_tags(self, query: str) -> tuple[Suggestion, ...]:
        """return the refinement tags the search box offers for a query."""

        if not isinstance(query, str) or not query.strip():
            raise ConfigurationError("query must be a non-empty string")
        data = self._request_json_any(
            "GET", f"{API_URL}/catalog/product/tag/", params={"q": query}
        )
        return self._suggestions(data)

    def download_photo(
        self,
        photo: ConsumPhoto,
        destination: str | os.PathLike[str],
        *,
        size: ImageSize | str = ImageSize.MEDIUM,
    ) -> Path:
        """download one image at a rendition through an atomic replacement."""

        if not isinstance(photo, ConsumPhoto):
            raise ConfigurationError("photo must be a ConsumPhoto instance")
        return self.download(photo.sized(size), destination)

    # ---------------------------------------------------------------- internals

    def _walk(
        self,
        *,
        order_by: SortOrder | int | None = None,
        filters: ProductFilters | None = None,
    ) -> Iterator[ConsumProduct]:
        """yield every row of one listing, a hundred at a time, until it ends."""

        offset = 0
        while True:
            page = self._list_products(
                query="",
                offset=offset,
                page_size=self.max_page_size or 100,
                order_by=order_by,
                filters=filters,
            )
            yield from page.products
            if page.next_cursor is None:
                return
            offset = int(page.next_cursor)

    def _suggestions(self, data: object) -> tuple[Suggestion, ...]:
        return tuple(
            suggestion
            for item in _array(data, "suggestions")
            if (suggestion := parse_suggestion(item)) is not None
        )

    def _list_products(
        self,
        *,
        query: str,
        offset: int,
        page_size: int,
        order_by: SortOrder | int | None = None,
        filters: ProductFilters | None = None,
        include_filters: bool = False,
        parameters: dict[str, Any] | None = None,
    ) -> ConsumSearchResult:
        params: dict[str, Any] = {"limit": page_size, "offset": offset}
        params.update(parameters or {})
        order = _validate_order(order_by)
        if order is not None:
            params["orderById"] = order
        rendered = _validate_filters(filters)
        if rendered is not None:
            params["filters"] = rendered
        if include_filters:
            params["includeFilters"] = "true"
        data = self._request_json("GET", f"{API_URL}/catalog/product", params=params)
        return parse_search_result(
            data,
            query=query,
            offset=offset,
            page_size=page_size,
            drop_sponsored=self._drop_sponsored,
        )

    def _zone_headers(self) -> dict[str, str]:
        return {} if self._zone is None else {"X-TOL-ZONE": self._zone}

    def _default_headers(self) -> dict[str, str]:
        headers = super()._default_headers()
        headers["X-TOL-LOCALE"] = self._language.value
        headers.update(self._zone_headers())
        return headers

    def _raise_error(
        self,
        response: httpx.Response,
        verdict: ResponseVerdict,
        *,
        method: str,
        url: str,
    ) -> None:
        if verdict is ResponseVerdict.NOT_FOUND and _error_type(response) == (
            _ZONE_ERROR_TYPE
        ):
            response.close()
            scope = (
                "the default assortment" if self._zone is None else f"zone {self._zone}"
            )
            raise NotFoundError(
                f"{method} {url} is not in {scope}; consum answers a product the "
                "zone does not carry and a code that does not exist the same way"
            )
        super()._raise_error(response, verdict, method=method, url=url)


def _unique(products: Iterable[ConsumProduct]) -> tuple[ConsumProduct, ...]:
    """keep the first placement of each code across the pages of a walk."""

    found: dict[str, ConsumProduct] = {}
    for product in products:
        found.setdefault(product.id, product)
    return tuple(found.values())


def _array(data: object, label: str) -> list[object]:
    if not isinstance(data, list):
        raise InvalidResponseError(f"{label} response is not a json array")
    return data


def _error_type(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except (ValueError, httpx.ResponseNotRead, httpx.StreamError):
        return None
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    error_type = meta.get("errorType")
    return error_type if isinstance(error_type, str) else None


def _find_category(
    categories: tuple[ConsumCategory, ...], wanted: str
) -> ConsumCategory | None:
    for category in categories:
        if category.id == wanted:
            return category
        found = _find_category(category.children, wanted)
        if found is not None:
            return found
    return None
