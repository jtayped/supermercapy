"""synchronous dia client."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Iterator
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
from .._core.coerce import JsonObject, as_integer, as_items, as_object, as_text
from .._core.exceptions import (
    ConfigurationError,
    InvalidResponseError,
    NotFoundError,
    OutOfCoverageError,
    TransportError,
)
from .._core.models import Language
from ._constants import (
    API_URL,
    CHECK_SERVICE_PATH,
    LISTING_PATH,
    LOCALE_PATH,
    LOCALES,
    MAX_SEARCH_PAGE,
    MAX_SEARCH_PAGE_SIZE,
    MENU_PATH,
    MIN_SEARCH_PAGE_SIZE,
    NEW_ARRIVALS_CATEGORY_ID,
    PRODUCT_PATH,
    SEARCH_PATH,
    SESSION_TTL,
    SHIPPING_ADDRESS_PATH,
)
from .models import (
    DiaCategory,
    DiaProduct,
    DiaSearchResult,
    category_id_of,
    parse_categories,
    parse_product,
    parse_row,
)

_SKU = re.compile(r"[A-Za-z0-9]+")
_CATEGORY = re.compile(r"L\d+")
# a listing cursor is the storefront's own next page, relative to /plp-back:
# a leaf category's page, or the next step of a top-level category's walk
_LEAF_CURSOR = re.compile(r"/reduced(?:/[a-z0-9-]+)+/c/(?P<id>L\d+)\?page=\d+")
_ROOT_CURSOR = re.compile(r"/l1/all/(?P<id>L\d+)/reduced\?category_id=L\d+&page=\d+")
_NO_SERVICE = 206
_PRODUCT_ERROR = 500


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ConfigurationError("query must be a non-empty string")
    return query


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit():
        raise ConfigurationError("cursor must be an offset returned by search")
    return int(cursor)


def _validate_sku(product_id: str | int) -> str:
    sku = validate_identifier(product_id, "product_id")
    if not _SKU.fullmatch(sku):
        raise ConfigurationError("product_id must be a dia sku such as '504P6'")
    return sku


def _validate_category(category_id: str | int) -> str:
    wanted = validate_identifier(category_id, "category_id")
    if not _CATEGORY.fullmatch(wanted):
        raise ConfigurationError("category_id must be a dia category id like 'L2051'")
    return wanted


def _validate_listing_cursor(cursor: str, category_id: str) -> str:
    if isinstance(cursor, str):
        match = _LEAF_CURSOR.fullmatch(cursor) or _ROOT_CURSOR.fullmatch(cursor)
        if match is not None and match.group("id") == category_id:
            return cursor
    raise ConfigurationError(
        f"cursor must be one returned by a listing of category {category_id}"
    )


def _node_path(category: DiaCategory) -> str:
    """return the listing path of a tree node, relative to ``/plp-back``.

    a top-level category is listed by id, child by child; a child category is
    listed by its storefront path, which is in the session's language.
    """

    if category.parent_id is None:
        return f"/l1/all/{category.id}/reduced"
    return f"/reduced{category.url or ''}"


def search_window(offset: int, size: int) -> tuple[int, int, int]:
    """return the page, page size and first row that serve ``size`` rows.

    the search serves pages of thirty to a thousand rows and never a page
    above fifty, so a window that does not start on a page boundary, or lies
    deeper than fifty pages, is read from the narrowest wider page that holds
    it. when none of a thousand rows does, the page that starts it is used and
    the result comes back shorter.
    """

    for width in range(max(size, MIN_SEARCH_PAGE_SIZE), MAX_SEARCH_PAGE_SIZE + 1):
        page, start = divmod(offset, width)
        if page < MAX_SEARCH_PAGE and start + size <= width:
            return page + 1, width, start
    page, start = divmod(offset, MAX_SEARCH_PAGE_SIZE)
    if page >= MAX_SEARCH_PAGE:
        limit = MAX_SEARCH_PAGE * MAX_SEARCH_PAGE_SIZE
        raise ConfigurationError(f"dia serves search results to row {limit} only")
    return page + 1, MAX_SEARCH_PAGE_SIZE, start


class Dia(BaseClient):
    """a reusable synchronous client for dia's online shop at ``www.dia.es``.

    every call is anonymous json under ``/api/v1/``. the session cookie the
    server sets carries the two things worth choosing, the delivery postcode
    and the language, so the client sets both on its own session before its
    first catalogue request and restates them while it lives. a postcode
    changes the assortment and the stock, and now and then a price; an
    unbound client sees the storefront's default postcode, 28041 (madrid) in
    october 2026.

    the home page and every other html page refuse a library client with an
    akamai "access denied" page; the json api does not, and answers a library
    user agent. both refusals raise :class:`~supermercapy.BlockedError`.

    ``STORES`` is left out because there is no store list: the postcode check
    names one store id and nothing else about it. ``EAN`` and ``EAN_LOOKUP``
    are left out because no barcode is published, ``HOME`` because the home
    page is html behind the refusal, and ``FUTURE_PRICES`` because nothing
    carries a date.
    """

    store_name = "dia"
    capabilities = frozenset(
        {
            Capability.POSTAL_CODE,
            Capability.CATALOG,
            Capability.NEW_ARRIVALS,
            Capability.OFFERS,
            Capability.NUTRITION,
            Capability.PROMOTIONS,
        }
    )
    supported_languages = frozenset(LOCALES)
    default_page_size = MIN_SEARCH_PAGE_SIZE
    max_page_size = MAX_SEARCH_PAGE_SIZE
    default_min_request_interval = 0.5

    def __init__(
        self,
        postal_code: str | None = None,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._postal_code = (
            None if postal_code is None else validate_postal_code(postal_code)
        )
        self._physical_store_id: str | None = None
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
        """check that dia delivers to a postcode and return a client bound to it.

        two requests: the storefront's own delivery check, which also names
        the store that would serve the postcode, and the move of the client's
        anonymous session to it. a postcode dia does not deliver to raises
        :class:`~supermercapy.OutOfCoverageError` after the first.
        """

        postal_code = validate_postal_code(postal_code)
        client = cls(
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )
        try:
            response = client._request(
                "GET",
                f"{API_URL}{CHECK_SERVICE_PATH}",
                params={"postal_code": postal_code},
            )
            store_id = None
            if response.status_code != _NO_SERVICE and response.content:
                store_id = as_text(as_object(response.json()).get("physical_store_id"))
            if not store_id:
                raise OutOfCoverageError(f"dia does not deliver to {postal_code}")
            client._physical_store_id = store_id
            client._postal_code = postal_code
            client._move_session()
            return client
        except ValueError as error:
            client.close()
            raise InvalidResponseError(
                "postcode check returned invalid JSON"
            ) from error
        except BaseException:
            client.close()
            raise

    @property
    def postal_code(self) -> str | None:
        """return the postcode the session is bound to, or ``None`` for the default."""

        return self._postal_code

    @property
    def store_id(self) -> str | None:
        """return the bound postcode; the same value as ``postal_code``."""

        return self._postal_code

    @property
    def physical_store_id(self) -> str | None:
        """return the store serving the postcode, as :meth:`from_postal_code` found."""

        return self._physical_store_id

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> DiaSearchResult:
        """search one page of products.

        the cursor is the next row offset as a string. the storefront pages by
        number with at least thirty rows a page and refuses a page above
        fifty, so the client picks the page that holds the rows asked for and
        trims it, which keeps any ``page_size`` from one to a thousand exact.
        """

        text = _validate_query(query)
        offset = _validate_offset(cursor)
        size = self._resolve_page_size(page_size)
        page, width, start = search_window(offset, size)
        data = self._session_json(
            f"{API_URL}{SEARCH_PATH}",
            params={"q": text, "page": page, "page_size": width},
        )
        rows = data.get("search_items")
        if not isinstance(rows, list):
            raise InvalidResponseError("search response has no search_items array")
        products = tuple(parse_row(item) for item in rows[start : start + size])
        total = as_integer(data.get("total_items"))
        following = offset + len(products)
        more = bool(products) and (total is None or following < total)
        return DiaSearchResult(
            query=query,
            products=products,
            page_size=size,
            total_hits=total,
            next_cursor=str(following) if more else None,
            postal_code=_cart_postal_code(data),
        )

    def get_product(self, product_id: str | int) -> DiaProduct:
        """return one product sheet by its sku.

        dia answers an unknown sku with http 500 rather than 404, so a 500
        from the sheet is checked with one search for the sku: no hit raises
        :class:`~supermercapy.NotFoundError`, a hit means the sheet really
        failed and raises :class:`~supermercapy.TransportError`.
        """

        sku = _validate_sku(product_id)
        try:
            data = self._session_json(f"{API_URL}{PRODUCT_PATH}/{quote(sku, safe='')}")
        except NotFoundError:
            if self._search_lists(sku):
                raise TransportError(
                    f"dia's product sheet failed for {sku}, which search still lists",
                    status_code=_PRODUCT_ERROR,
                ) from None
            raise NotFoundError(f"dia has no product {sku}") from None
        return parse_product(data)

    def get_categories(self) -> tuple[DiaCategory, ...]:
        """return the two-level category tree in one request."""

        data = self._request_json("GET", f"{API_URL}{MENU_PATH}")
        return parse_categories(data)

    def get_category(self, category_id: str | int) -> DiaCategory:
        """return one category with its children and its first listing page.

        this costs one request for the tree and one for the listing. a
        top-level category lists its children one after another, so its first
        page holds the first child's products; ``product_count`` is the whole
        category's. "novedades y recomendados" has no listing of its own (the
        storefront redirects it to its first child), so it comes back without
        products.
        """

        node = self._find_category(_validate_category(category_id))
        page = self._listing_page(_node_path(node))
        return dataclasses.replace(
            node, products=page.products, product_count=page.total_hits
        )

    def iter_catalog(self) -> Iterator[DiaProduct]:
        """yield every product by walking each top-level category in turn.

        one request for the tree, then one per twenty rows of each child
        category: an estimated 450 requests for the 6,500 rows the tree held in
        october 2026. a product listed in two categories is yielded twice;
        :meth:`get_catalog` keeps one.
        """

        return self._walk_catalog()

    def get_new_arrivals(self) -> tuple[DiaProduct, ...]:
        """return the products of the storefront's own "novedades" category.

        every row of it carries the "novedad" stamp. it costs one request for
        the tree and one per twenty products: three for the 38 it held in
        october 2026. rows stamped "novedad" also turn up elsewhere with
        ``is_new`` set, and not all of them are in this category.
        """

        node = self._find_category(NEW_ARRIVALS_CATEGORY_ID)
        return _unique(
            dataclasses.replace(product, is_new=True)
            for product in self._walk_listing(node)
        )

    def get_offers(self) -> tuple[DiaProduct, ...]:
        """return every product on offer, across every category.

        the offers page groups its rows by category and shows ten of each, so
        the client reads the first group's id from it and then walks each
        category's own offer listing, which chains to the next category: one
        request, then one per category with offers and one more per extra
        page of twenty-four, about thirty in october 2026. a product offered
        in two categories is returned once.
        """

        data = self._request_json("GET", f"{API_URL}{LISTING_PATH}/offers/reduced")
        groups = as_items(data.get("plp_items"))
        first = as_text(as_object(groups[0]).get("category_id")) if groups else None
        return _unique(self._walk_offers(first))

    # -------------------------------------------------------------- extensions

    def get_category_products(
        self, category_id: str | int, *, cursor: str | None = None
    ) -> DiaSearchResult:
        """return one page of a category's listing, twenty rows at a time.

        without a cursor this costs one request for the tree and one for the
        page; with the ``next_cursor`` of an earlier page it costs one. a
        top-level category walks its children one after another, and each
        page's ``category_id`` names the child it came from.
        """

        wanted = _validate_category(category_id)
        if cursor is not None:
            return self._listing_page(_validate_listing_cursor(cursor, wanted))
        return self._listing_page(_node_path(self._find_category(wanted)))

    def get_category_offers(self, category_id: str | int) -> tuple[DiaProduct, ...]:
        """return every product on offer in one category, top-level or not.

        one request per twenty-four offers.
        """

        rows, _ = self._category_offers(_validate_category(category_id))
        return _unique(rows)

    # ---------------------------------------------------------------- internals

    def _find_category(self, wanted: str) -> DiaCategory:
        for root in self.get_categories():
            for node in (root, *root.children):
                if node.id == wanted:
                    return node
        raise NotFoundError(f"dia has no category {wanted}")

    def _listing_page(self, path: str) -> DiaSearchResult:
        """read one listing page by its path relative to ``/plp-back``."""

        url = f"{API_URL}{LISTING_PATH}{path}"
        response = self._request("GET", url)
        is_root = path.startswith("/l1/")
        if response.is_redirect and is_root:
            # a top-level group with no listing of its own sends the browser
            # to its first child instead
            return DiaSearchResult(query="", products=(), page_size=0)
        data = self._session_checked(response, url)
        return _root_page(data) if is_root else _leaf_page(data)

    def _walk_listing(self, category: DiaCategory) -> Iterator[DiaProduct]:
        path: str | None = _node_path(category)
        while path is not None:
            page = self._listing_page(path)
            yield from page.products
            path = page.next_cursor if page.next_cursor != path else None

    def _walk_catalog(self) -> Iterator[DiaProduct]:
        for root in self.get_categories():
            yield from self._walk_listing(root)

    def _category_offers(self, category_id: str) -> tuple[list[DiaProduct], str | None]:
        """return one category's offers across its pages, and the next category."""

        rows: list[DiaProduct] = []
        following: str | None = None
        page = 1
        while True:
            params = {"page": page} if page > 1 else None
            data = self._session_json(
                f"{API_URL}{LISTING_PATH}/offers/reduced/{category_id}", params=params
            )
            items = as_items(data.get("plp_items"))
            rows.extend(parse_row(item, category_id=category_id) for item in items)
            following = as_text(as_object(data.get("next_category")).get("category_id"))
            pages = as_integer(as_object(data.get("pagination")).get("total_pages"))
            if not items or pages is None or page >= pages:
                return rows, following
            page += 1

    def _walk_offers(self, category_id: str | None) -> Iterator[DiaProduct]:
        seen: set[str] = set()
        while category_id is not None and category_id not in seen:
            seen.add(category_id)
            rows, category_id = self._category_offers(category_id)
            yield from rows

    def _search_lists(self, sku: str) -> bool:
        page = self.search_products(sku, page_size=MIN_SEARCH_PAGE_SIZE)
        return any(product.id == sku for product in page.products)

    # ------------------------------------------------------------------ session

    def _session_json(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> JsonObject:
        response = self._request("GET", url, params=params)
        return self._session_checked(response, url, params=params)

    def _session_checked(
        self,
        response: httpx.Response,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> JsonObject:
        """return a json answer, restating the session once if it was lost.

        the server starts a new session on its defaults when the old one
        lapses, and says so only through the postcode and locale it echoes.
        """

        data = _json_object(response, url)
        if self._session_matches(data):
            return data
        self._invalidate_priming()
        data = _json_object(self._request("GET", url, params=params), url)
        if not self._session_matches(data):
            raise InvalidResponseError(
                "dia kept answering for postcode "
                f"{_cart_postal_code(data)} and locale {as_text(data.get('locale'))} "
                f"after the session was set to {self._postal_code} and "
                f"{LOCALES[self._language]}"
            )
        return data

    def _session_matches(self, data: JsonObject) -> bool:
        postal_code = _cart_postal_code(data)
        locale = as_text(data.get("locale"))
        if locale is not None and locale != LOCALES[self._language]:
            return False
        return (
            self._postal_code is None
            or postal_code is None
            or postal_code == self._postal_code
        )

    def _prime(self) -> None:
        locale = LOCALES[self._language]
        if self._language is not Language.SPANISH:
            self._request("PATCH", f"{API_URL}{LOCALE_PATH}", json={"locale": locale})
        if self._postal_code is not None:
            self._move_session()

    def _move_session(self) -> None:
        """point the anonymous session at the bound postcode, as the site does."""

        response = self._request(
            "PUT",
            f"{API_URL}{SHIPPING_ADDRESS_PATH}",
            params={"new_postal_code": self._postal_code},
        )
        if response.status_code == _NO_SERVICE:
            raise OutOfCoverageError(f"dia does not deliver to {self._postal_code}")

    def _prime_ttl(self) -> float | None:
        return SESSION_TTL

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        status = response.status_code
        content_type = response.headers.get("Content-Type", "")
        if status == 403:
            # akamai's "access denied" page; the api sends no 403 of its own
            return ResponseVerdict.BLOCKED
        if status == 404 and content_type.startswith("text/html"):
            # the edge's "bloqueado" page; the api's own 404s are json or empty
            return ResponseVerdict.BLOCKED
        if (
            status == _PRODUCT_ERROR
            and response.request.url.path.startswith(
                httpx.URL(f"{API_URL}{PRODUCT_PATH}/").path
            )
            and _error_code(response) == _PRODUCT_ERROR
        ):
            # the sheet's answer for an unknown sku; get_product confirms it
            return ResponseVerdict.NOT_FOUND
        return super()._classify(response)


def _json_object(response: httpx.Response, url: str) -> JsonObject:
    if response.is_redirect:
        location = response.headers.get("Location", "")
        raise TransportError(
            f"GET {url} redirected (HTTP {response.status_code}) "
            f"to {location or 'nowhere'}",
            status_code=response.status_code,
        )
    try:
        data = response.json()
    except ValueError as error:
        raise InvalidResponseError(f"GET {url} returned invalid JSON") from error
    if not isinstance(data, dict):
        raise InvalidResponseError(f"GET {url} returned a non-object JSON value")
    return data


def _error_code(response: httpx.Response) -> int | None:
    try:
        payload = response.json()
    except (ValueError, httpx.ResponseNotRead):
        return None
    return as_integer(as_object(payload).get("code"))


def _cart_postal_code(data: JsonObject) -> str | None:
    return as_text(as_object(data.get("cart")).get("postal_code")) or None


def _leaf_page(data: JsonObject) -> DiaSearchResult:
    category_id = as_text(data.get("selected_category_id")) or category_id_of(
        data.get("current_category_url")
    )
    rows = tuple(
        parse_row(item, category_id=category_id)
        for item in as_items(data.get("plp_items"))
    )
    pagination = as_object(data.get("pagination"))
    number = as_integer(pagination.get("page_number"))
    pages = as_integer(pagination.get("total_pages"))
    link = as_text(data.get("current_category_url"))
    following = (
        f"/reduced{link}?page={number + 1}"
        if rows and link and number is not None and pages is not None and number < pages
        else None
    )
    return DiaSearchResult(
        query="",
        products=rows,
        page_size=as_integer(pagination.get("page_size")) or len(rows),
        total_hits=as_integer(data.get("total_items")),
        next_cursor=following,
        category_id=category_id,
    )


def _root_page(data: JsonObject) -> DiaSearchResult:
    category_id = as_text(as_object(data.get("current_subcategory")).get("id"))
    rows = tuple(
        parse_row(item, category_id=category_id) for item in as_items(data.get("items"))
    )
    pagination = as_object(data.get("pagination"))
    next_url = as_text(pagination.get("next_url"))
    prefix = httpx.URL(f"{API_URL}{LISTING_PATH}").path
    following = (
        next_url[len(prefix) :]
        if next_url is not None and next_url.startswith(f"{prefix}/l1/")
        else None
    )
    return DiaSearchResult(
        query="",
        products=rows,
        page_size=as_integer(pagination.get("page_size")) or len(rows),
        total_hits=as_integer(data.get("total_items")),
        next_cursor=following,
        category_id=category_id,
    )


def _unique(products: Iterable[DiaProduct]) -> tuple[DiaProduct, ...]:
    """keep the first placement of each sku."""

    found: dict[str, DiaProduct] = {}
    for product in products:
        found.setdefault(product.id, product)
    return tuple(found.values())
