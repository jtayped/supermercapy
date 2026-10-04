"""synchronous ahorramás client."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any, cast

import httpx

from .._core.capabilities import Capability
from .._core.client import BaseClient, ResponseVerdict, RetryPolicy, validate_identifier
from .._core.exceptions import ConfigurationError, InvalidResponseError, NotFoundError
from .._core.models import Language
from ._constants import (
    GRID_PATH,
    OFFERS_CATEGORY,
    PRODUCT_PATH,
    ROOT_CATEGORY,
    SITE_URL,
)
from .models import (
    AhorramasCategory,
    AhorramasProduct,
    AhorramasSearchResult,
    grid_category,
    index_categories,
    parse_grid,
    parse_menu,
    parse_product,
)

_GRID_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "X-Requested-With": "XMLHttpRequest",
}
_RECORD_HEADERS = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
_PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
_SERVER_ERROR = 500
_FORBIDDEN = 403
# cloudflare's interstitial and its firewall page, lowercased for comparison
_CHALLENGE_MARKER = "just a moment"
_BLOCK_MARKERS = ("you have been blocked", "attention required")


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise ConfigurationError("query must be a string")
    if not query.strip():
        # a blank query is answered with the whole catalogue, unfiltered
        raise ConfigurationError("query must not be blank")
    return query.strip()


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not (cursor.isascii() and cursor.isdigit()):
        raise ConfigurationError("cursor must be an offset returned by a listing")
    return int(cursor)


def _body_of(response: httpx.Response) -> str:
    """read a response body even when the request asked for a stream."""

    try:
        return response.text
    except httpx.ResponseNotRead:
        return response.read().decode("utf-8", "replace")


class Ahorramas(BaseClient):
    """a reusable synchronous client for ahorramás's online supermarket.

    the shop runs salesforce commerce cloud behind cloudflare and publishes no
    product api. listings come from the html grid its "more results" button
    loads, product records from the json its product page asks for when a
    quantity changes, and the category tree from the menu every full page
    carries. both of the storefront's own endpoints sit under paths its
    robots.txt disallows, which the store's documentation lists.

    nothing binds. the shop serves one assortment at one price whatever the
    address, publishes no postcode step, and speaks spanish only. the client
    paces itself at one request a second, because a catalog walk is close to
    fifty pages of a megabyte each.

    ``NUTRITION`` is not declared: the product record has ingredient,
    nutrition and allergen fields, and every product sampled in october 2026
    left them empty.
    """

    store_name = "ahorramas"
    capabilities = frozenset(
        {Capability.CATALOG, Capability.OFFERS, Capability.PROMOTIONS}
    )
    supported_languages = frozenset({Language.SPANISH})
    default_page_size = 24
    max_page_size = 100
    default_min_request_interval = 1.0

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
        self._categories: dict[str, AhorramasCategory] | None = None
        super().__init__(
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> AhorramasSearchResult:
        """search one page of products; the cursor is the next offset.

        the grid publishes no total, so ``total_hits`` is ``None``, and a page
        shorter than ``page_size`` is the last one.
        """

        text = _validate_query(query)
        offset = _validate_offset(cursor)
        size = self._resolve_page_size(page_size)
        html = self._grid({"q": text}, offset=offset, size=size)
        return parse_grid(html, query=query, offset=offset, page_size=size)

    def get_product(self, product_id: str | int) -> AhorramasProduct:
        """return one product's record, the json its page reads, in one request.

        an unknown id is answered with http 500 and an error document, the
        same answer a server fault gets, so both raise
        :class:`~supermercapy.NotFoundError`.
        """

        identifier = validate_identifier(product_id, "product_id")
        try:
            data = self._request_json(
                "GET",
                f"{SITE_URL}{PRODUCT_PATH}",
                params={"pid": identifier},
                headers=_RECORD_HEADERS,
            )
        except NotFoundError as error:
            raise NotFoundError(f"ahorramas has no product {identifier}") from error
        return parse_product(data)

    def get_categories(self) -> tuple[AhorramasCategory, ...]:
        """return the category tree from the home page's menu, about 790 kb.

        the tree is kept on the client, so a later :meth:`get_category` needs
        no second copy.
        """

        response = self._request("GET", f"{SITE_URL}/", headers=_PAGE_HEADERS)
        roots = parse_menu(response.text)
        if not roots:
            raise InvalidResponseError("ahorramas served no category menu")
        self._categories = index_categories(roots)
        return roots

    def get_category(
        self, category_id: str | int, *, page_size: int | None = None
    ) -> AhorramasCategory:
        """return one category with its children and its first listing page.

        the name and the children come from the menu, which a client that has
        not read it yet reads first: two requests, then one per category. an
        id the menu does not hold raises :class:`~supermercapy.NotFoundError`.
        no product count is published.
        """

        wanted = validate_identifier(category_id, "category_id")
        if self._categories is None:
            self.get_categories()
        node = (self._categories or {}).get(wanted)
        if node is None:
            raise NotFoundError(f"ahorramas has no category {wanted}")
        listing = self.get_category_products(wanted, page_size=page_size)
        return dataclasses.replace(node, products=listing.products)

    def iter_catalog(self) -> Iterator[AhorramasProduct]:
        """yield every product by walking the catalogue's root a hundred at a time.

        the root category holds the whole catalogue: a full walk on 4 october
        2026 read 7,579 products in 76 requests of about a megabyte each, and
        took nine minutes because each page took about seven seconds.
        """

        return self._walk(ROOT_CATEGORY)

    def get_offers(self) -> tuple[AhorramasProduct, ...]:
        """return every product in the menu's offers branch, walked to the end."""

        found: dict[str, AhorramasProduct] = {}
        for product in self._walk(OFFERS_CATEGORY):
            found.setdefault(product.id, product)
        return tuple(found.values())

    # -------------------------------------------------------------- extensions

    def get_category_products(
        self,
        category_id: str | int,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> AhorramasSearchResult:
        """return one listing page of a category, its subtree included.

        the id is the menu's, such as ``"cordero_y_cabrito"``, and the call
        needs no menu. the storefront answers an unknown id with the whole
        catalogue rather than an error, so the category the grid reports
        serving is checked and a mismatch raises
        :class:`~supermercapy.NotFoundError`.
        """

        wanted = validate_identifier(category_id, "category_id")
        offset = _validate_offset(cursor)
        size = self._resolve_page_size(page_size)
        return self._category_page(wanted, offset=offset, size=size)

    # ---------------------------------------------------------- transport hooks

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        """tell cloudflare's answers and the record's not-found apart."""

        if response.headers.get("cf-mitigated", "").lower() == "challenge":
            return ResponseVerdict.CHALLENGED
        if response.status_code == _FORBIDDEN:
            body = _body_of(response).lower()
            if _CHALLENGE_MARKER in body:
                return ResponseVerdict.CHALLENGED
            return ResponseVerdict.BLOCKED
        if response.status_code == _SERVER_ERROR and _is_missing_record(response):
            return ResponseVerdict.NOT_FOUND
        return super()._classify(response)

    # ---------------------------------------------------------------- internals

    def _grid(self, filters: dict[str, Any], *, offset: int, size: int) -> str:
        params = {**filters, "start": offset, "sz": size}
        response = self._request(
            "GET", f"{SITE_URL}{GRID_PATH}", params=params, headers=_GRID_HEADERS
        )
        return response.text

    def _category_page(
        self, category_id: str, *, offset: int, size: int
    ) -> AhorramasSearchResult:
        html = self._grid({"cgid": category_id}, offset=offset, size=size)
        if grid_category(html) != category_id:
            raise NotFoundError(f"ahorramas has no category {category_id}")
        return parse_grid(html, query="", offset=offset, page_size=size)

    def _walk(self, category_id: str) -> Iterator[AhorramasProduct]:
        size = self.max_page_size or self.default_page_size
        offset = 0
        while True:
            page = self._category_page(category_id, offset=offset, size=size)
            yield from cast("tuple[AhorramasProduct, ...]", page.products)
            if page.next_cursor is None:
                return
            offset = int(page.next_cursor)


def _is_missing_record(response: httpx.Response) -> bool:
    """return whether a 500 is the record endpoint's answer to an unknown id."""

    if not response.request.url.path.endswith(PRODUCT_PATH):
        return False
    try:
        payload = json.loads(_body_of(response))
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("action") == "Product-Variation"
        and "error" in payload
    )
