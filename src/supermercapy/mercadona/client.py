"""synchronous mercadona client."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Self
from urllib.parse import quote, urlencode

import httpx

from .._core.capabilities import Capability
from .._core.client import (
    BaseClient,
    RetryPolicy,
    validate_identifier,
    validate_postal_code,
)
from .._core.coerce import JsonObject
from .._core.exceptions import (
    ConfigurationError,
    InvalidResponseError,
    NotFoundError,
)
from .._core.models import Language, Store
from ._constants import ALGOLIA_API_KEY, ALGOLIA_APP_ID, ALGOLIA_URL, API_URL
from .models import (
    CatalogResult,
    MercadonaCategory,
    MercadonaHomeSection,
    MercadonaPhoto,
    MercadonaProduct,
    MercadonaProductSummary,
    MercadonaSearchResult,
    PhotoFit,
    Season,
    parse_category,
    parse_home_section,
    parse_product,
    parse_product_summary,
    parse_search_result,
    parse_season,
)

_WAREHOUSE_PATTERN = re.compile(r"^[a-z0-9]{2,16}$")
# the search service answers at most this many hits for one query, which is
# why the catalog is partitioned at all.
_INDEX_RESULT_CAP = 1000
# score partitions: the upper bound starts here and grows by this factor
# until a probe above it finds nothing; ranges stop halving at this width.
_SCORE_UPPER_START = 1024.0
_SCORE_GROWTH = 4
_SCORE_BOUND_PROBES = 16
_SCORE_RESOLUTION = 1e-6


def _validate_warehouse(warehouse: str) -> str:
    if not isinstance(warehouse, str):
        raise ConfigurationError("warehouse must be a string")
    normalized = warehouse.strip().lower()
    if not _WAREHOUSE_PATTERN.fullmatch(normalized):
        raise ConfigurationError(
            "warehouse must contain 2 to 16 lowercase letters or digits"
        )
    return normalized


def _validate_page(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise ConfigurationError("cursor must be a page number returned by search")
    return int(cursor)


class Mercadona(BaseClient):
    """a reusable synchronous client scoped to one mercadona warehouse."""

    store_name = "mercadona"
    capabilities = frozenset(
        {
            Capability.POSTAL_CODE,
            Capability.STORES,
            Capability.CATALOG,
            Capability.NEW_ARRIVALS,
            Capability.HOME,
            Capability.EAN,
            Capability.NUTRITION,
        }
    )
    supported_languages = frozenset(
        {Language.SPANISH, Language.ENGLISH, Language.CATALAN}
    )
    default_page_size = 20
    max_page_size = 1000

    def __init__(
        self,
        warehouse: str,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._warehouse = _validate_warehouse(warehouse)
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
        """resolve a postal code with one request and return a scoped client."""

        postal_code = validate_postal_code(postal_code)
        client = cls(
            "lookup",
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )
        try:
            client._warehouse = client._resolve_warehouse(postal_code)
            return client
        except BaseException:
            client.close()
            raise

    def _resolve_warehouse(self, postal_code: str) -> str:
        response = self._request(
            "PUT",
            f"{API_URL}/api/postal-codes/actions/change-pc/",
            json={"new_postal_code": postal_code},
        )
        warehouse = response.headers.get("X-Customer-Wh")
        if warehouse is None:
            raise InvalidResponseError(
                "postal-code response has no X-Customer-Wh header"
            )
        return _validate_warehouse(warehouse)

    @property
    def warehouse(self) -> str:
        """return the normalized warehouse code."""

        return self._warehouse

    @property
    def store_id(self) -> str:
        """return the warehouse code; the same value as ``warehouse``."""

        return self._warehouse

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        top_level_category_id: str | int | None = None,
    ) -> MercadonaSearchResult:
        """search one page of product summaries.

        the cursor is the zero-based page number as a string; ``None`` requests
        the first page.
        """

        if not isinstance(query, str):
            raise ConfigurationError("query must be a string")
        page = _validate_page(cursor)
        size = self._resolve_page_size(page_size)
        category_id = (
            validate_identifier(top_level_category_id, "top_level_category_id")
            if top_level_category_id is not None
            else None
        )
        data = self._search_data(
            query=query,
            page=page,
            page_size=size,
            top_level_category_id=category_id,
        )
        return parse_search_result(
            data, query=query, requested_page=page, requested_page_size=size
        )

    def get_product(self, product_id: str | int) -> MercadonaProduct:
        """return a complete product record by id."""

        product_id = validate_identifier(product_id, "product_id")
        data = self._get_api_json(f"/api/products/{quote(product_id, safe='')}/")
        return parse_product(data)

    def get_categories(self) -> tuple[MercadonaCategory, ...]:
        """return the storefront category tree."""

        data = self._get_api_json("/api/categories/")
        results = data.get("results")
        if not isinstance(results, list):
            raise InvalidResponseError("categories response has no results array")
        return tuple(parse_category(item, level=0) for item in results)

    def get_category(self, category_id: str | int) -> MercadonaCategory:
        """return one category with its groups and their products.

        only the second level has a category page: a top-level id answers
        http 404 there. on that 404 the category tree is fetched, one more
        request, and a root found in it is returned as the tree spells it,
        with its second-level categories as children and no products. an id
        the tree does not hold as a root, such as one of the groups below a
        second-level category, raises :class:`~supermercapy.NotFoundError`.
        """

        category_id = validate_identifier(category_id, "category_id")
        try:
            data = self._get_api_json(f"/api/categories/{quote(category_id, safe='')}/")
        except NotFoundError:
            for root in self.get_categories():
                if root.id == category_id:
                    return root
            raise
        return parse_category(data, level=1)

    def list_stores(self, postal_code: str | None = None) -> tuple[Store, ...]:
        """resolve ``postal_code`` to its warehouse and return it as a store."""

        if postal_code is None:
            raise ConfigurationError("mercadona needs a postal_code to find stores")
        warehouse = self._resolve_warehouse(validate_postal_code(postal_code))
        return (Store(id=warehouse, name=warehouse, kind="warehouse"),)

    def iter_catalog(self) -> Iterator[MercadonaProductSummary]:
        """yield summaries from the category tree and each listed group."""

        def walk(category: MercadonaCategory) -> Iterator[MercadonaProductSummary]:
            yield from category.products
            for child in category.children:
                yield from walk(child)

        for category in self.get_categories():
            yield from walk(category)
            for child in category.children:
                yield from walk(self.get_category(child.id))

    def get_catalog(self) -> tuple[MercadonaProductSummary, ...]:
        """collect deduplicated summaries from all listed category groups."""

        products: dict[str, MercadonaProductSummary] = {}
        for product in self.iter_catalog():
            products.setdefault(product.id, product)
        return tuple(products.values())

    def get_new_arrivals(self) -> tuple[MercadonaProductSummary, ...]:
        """return the current new-arrival product summaries."""

        data = self._get_api_json("/api/home/new-arrivals/")
        items = data.get("items")
        if not isinstance(items, list):
            raise InvalidResponseError("new-arrivals response has no items array")
        # the feed's items carry no is_new_arrival flag of their own; being
        # listed here is what makes them new
        return tuple(
            replace(parse_product_summary(item), is_new=True) for item in items
        )

    def get_home(self) -> tuple[MercadonaHomeSection, ...]:
        """return ordered storefront home sections."""

        data = self._get_api_json("/api/home/")
        sections = data.get("sections")
        if not isinstance(sections, list):
            raise InvalidResponseError("home response has no sections array")
        return tuple(parse_home_section(section) for section in sections)

    # -------------------------------------------------------------- extensions

    def get_season(self, season_id: str) -> Season:
        """return one seasonal product collection by id."""

        season_id = validate_identifier(season_id, "season_id")
        data = self._get_api_json(f"/api/home/sections/{quote(season_id, safe='')}/")
        return parse_season(data, season_id)

    def get_indexed_catalog(self) -> CatalogResult:
        """collect the complete algolia index through top-category partitions.

        an index that exposes no category facets is partitioned by score
        ranges instead: numeric filters work on every index, and a range that
        reports more hits than the result cap is halved until it fits. records
        without a score, or more tied scores than the cap, leave the result
        unreconciled rather than silently short.
        """

        overview = self._search_data(
            query="",
            page=0,
            page_size=1,
            facets="categories.id",
            max_values_per_facet=1000,
        )
        reported_total_hits = overview.get("nbHits")
        if (
            isinstance(reported_total_hits, bool)
            or not isinstance(reported_total_hits, int)
            or reported_total_hits < 0
        ):
            raise InvalidResponseError("catalog index response has no usable hit count")
        facets = overview.get("facets")
        if not isinstance(facets, dict):
            raise InvalidResponseError("catalog index response has no facets object")
        category_counts = facets.get("categories.id")

        products: dict[str, MercadonaProductSummary] = {}
        score_ranges: tuple[tuple[float, float], ...] = ()
        if isinstance(category_counts, dict) and category_counts:
            category_ids = tuple(sorted(str(value) for value in category_counts))
            for category_id in category_ids:
                self._collect_category_partition(products, category_id)
        else:
            category_ids = ()
            score_ranges = self._collect_score_partitions(products)

        return CatalogResult(
            products=tuple(products.values()),
            reported_total_hits=reported_total_hits,
            queried_category_ids=category_ids,
            queried_score_ranges=score_ranges,
            reconciled=len(products) == reported_total_hits,
        )

    def download_photo(
        self,
        photo: MercadonaPhoto,
        destination: str | os.PathLike[str],
        *,
        width: int | None = None,
        height: int | None = None,
        fit: PhotoFit | str = PhotoFit.CROP,
    ) -> Path:
        """download a photo at an optional size through an atomic replacement."""

        if not isinstance(photo, MercadonaPhoto):
            raise ConfigurationError("photo must be a MercadonaPhoto instance")
        return self.download(
            photo.sized(width=width, height=height, fit=fit), destination
        )

    # ---------------------------------------------------------------- internals

    def _collect_category_partition(
        self, products: dict[str, MercadonaProductSummary], category_id: str
    ) -> None:
        cursor: str | None = None
        while True:
            result = self.search_products(
                "",
                page_size=_INDEX_RESULT_CAP,
                cursor=cursor,
                top_level_category_id=category_id,
            )
            for product in result.products:
                products.setdefault(product.id, product)
            if result.next_cursor is None:
                break
            cursor = result.next_cursor

    def _score_partition(
        self, lower: float, upper: float | None, *, page_size: int
    ) -> MercadonaSearchResult:
        numeric_filters = f"score>={lower:.6f}"
        if upper is not None:
            numeric_filters += f",score<{upper:.6f}"
        data = self._search_data(
            query="", page=0, page_size=page_size, numeric_filters=numeric_filters
        )
        return parse_search_result(
            data, query="", requested_page=0, requested_page_size=page_size
        )

    def _collect_score_partitions(
        self, products: dict[str, MercadonaProductSummary]
    ) -> tuple[tuple[float, float], ...]:
        upper = _SCORE_UPPER_START
        for _ in range(_SCORE_BOUND_PROBES):
            if self._score_partition(upper, None, page_size=0).total_hits == 0:
                break
            upper *= _SCORE_GROWTH

        ranges: list[tuple[float, float]] = []
        pending: list[tuple[float, float]] = [(0.0, upper)]
        while pending:
            lower, upper = pending.pop()
            result = self._score_partition(lower, upper, page_size=_INDEX_RESULT_CAP)
            total_hits = result.total_hits or 0
            if total_hits == 0:
                continue
            if total_hits > _INDEX_RESULT_CAP and upper - lower > _SCORE_RESOLUTION:
                middle = (lower + upper) / 2
                pending.append((middle, upper))
                pending.append((lower, middle))
                continue
            ranges.append((lower, upper))
            for product in result.products:
                products.setdefault(product.id, product)
        return tuple(ranges)

    def _get_api_json(self, path: str) -> JsonObject:
        return self._request_json(
            "GET",
            f"{API_URL}{path}",
            params={"lang": self._language.value, "wh": self._warehouse},
        )

    def _search_data(
        self,
        *,
        query: str,
        page: int,
        page_size: int,
        top_level_category_id: str | None = None,
        facets: str | None = None,
        max_values_per_facet: int | None = None,
        numeric_filters: str | None = None,
    ) -> JsonObject:
        index = f"products_prod_{self._warehouse}_{self._language.value}"
        url = f"{ALGOLIA_URL}/1/indexes/{quote(index, safe='')}/query"
        parameters: list[tuple[str, str | int]] = [
            ("query", query),
            ("page", page),
            ("hitsPerPage", page_size),
        ]
        if top_level_category_id is not None:
            parameters.append(("filters", f"categories.id:{top_level_category_id}"))
        if facets is not None:
            parameters.append(("facets", facets))
        if max_values_per_facet is not None:
            parameters.append(("maxValuesPerFacet", max_values_per_facet))
        if numeric_filters is not None:
            parameters.append(("numericFilters", numeric_filters))
        return self._request_json(
            "POST",
            url,
            headers={
                "x-algolia-application-id": ALGOLIA_APP_ID,
                "x-algolia-api-key": ALGOLIA_API_KEY,
            },
            json={"params": urlencode(parameters)},
        )
