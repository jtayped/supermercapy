"""synchronous carrefour client."""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Self
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
from .._core.coerce import JsonObject, as_integer, as_items, as_object
from .._core.exceptions import (
    ChallengedError,
    ConfigurationError,
    InvalidResponseError,
    NotFoundError,
    OutOfCoverageError,
)
from .._core.html import extract_json_assignment
from .._core.models import Language
from ._constants import (
    ACCEPT_LANGUAGES,
    BLOCKED_MARKER,
    BROWSER_USER_AGENT,
    CATALOGS,
    CATEGORY_PATH,
    CHALLENGE_MARKER,
    DEFAULT_CATALOG,
    DOCUMENT_HEADERS,
    EMPATHY_URL,
    FOOD_ROOT,
    MAX_ROWS,
    MAX_START,
    MENU_URL,
    PRIME_TTL,
    PROXY_MAX_ROWS,
    PROXY_SEARCH_URL,
    PROXY_SESSION,
    SALEPOINTS_URL,
    SITE_URL,
    STATE_MARKER,
    SUPERMARKET_PATH,
    XHR_HEADERS,
)
from .models import (
    CarrefourCategory,
    CarrefourHomeSection,
    CarrefourListing,
    CarrefourPhoto,
    CarrefourProduct,
    CarrefourSearchResult,
    CarrefourStore,
    normalise_category_id,
    parse_category_page,
    parse_drives,
    parse_home,
    parse_listing,
    parse_menu,
    parse_product,
    parse_search_result,
    parse_stores_location,
    parse_suggestions,
)

_FORBIDDEN = 403
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 3
# an unknown product or category id is redirected out of the food catalog:
# usually to the home page, sometimes to another business unit such as /moda
_CATALOG_PREFIX = f"{SUPERMARKET_PATH}/"
_EARTH_RADIUS_KM = 6371.0


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise ConfigurationError("query must be a string")
    if not query.strip():
        raise ConfigurationError("query must not be blank")
    return query


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not (cursor.isascii() and cursor.isdigit()):
        raise ConfigurationError("cursor must be an offset returned by search")
    offset = int(cursor)
    if offset > MAX_START:
        raise ConfigurationError(f"cursor must not start beyond document {MAX_START}")
    return offset


def _validate_catalog(catalog: str) -> str:
    if not isinstance(catalog, str) or catalog.strip().lower() not in CATALOGS:
        allowed = ", ".join(repr(item) for item in sorted(CATALOGS))
        raise ConfigurationError(f"catalog must be one of {allowed}")
    return catalog.strip().lower()


def _validate_sale_point(sale_point: str | int | None) -> str | None:
    if sale_point is None:
        return None
    return validate_identifier(sale_point, "sale_point")


def _validate_offset_argument(offset: int, label: str) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ConfigurationError(f"{label} must be a non-negative integer")
    return offset


def _distance_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    """return the great-circle distance between two coordinates."""

    lat1, lon1 = radians(first[0]), radians(first[1])
    lat2, lon2 = radians(second[0]), radians(second[1])
    half = (
        sin((lat2 - lat1) / 2) ** 2
        + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(half))


class Carrefour(BaseClient):
    """a reusable synchronous client for carrefour's spanish food storefront.

    the client talks to two hosts. searching, autocomplete and every lookup by
    id or barcode go to the empathy search index carrefour's own storefront
    calls, which is unauthenticated, needs no cookies and is the only path
    that honours a per-request sale point. product pages, category listings,
    stock figures and the sale-point directory come from the storefront
    itself, which sits behind cloudflare.

    that gate is a user-agent check, so the default user agent here is a
    browser string rather than supermercapy's own; replacing it with a library
    user agent makes every storefront request fail with http 403. cold
    requests are also only about three times in four reliable, so the first
    storefront request of a session warms a cookie jar with one html fetch
    and :meth:`_prime` renews it every twenty-five minutes. requests that
    never touch the storefront never pay for a warm-up.

    a sale point changes both prices and assortment, exactly like mercadona's
    warehouse. bind one with :meth:`from_postal_code` or pick an id out of
    :meth:`list_stores`; leaving it unset searches carrefour's national
    superset, which prices nothing.
    """

    store_name = "carrefour"
    capabilities = frozenset(
        {
            Capability.POSTAL_CODE,
            Capability.STORES,
            Capability.EAN,
            Capability.EAN_LOOKUP,
            Capability.NUTRITION,
            Capability.PROMOTIONS,
            Capability.HOME,
        }
    )
    supported_languages = frozenset(
        {Language.SPANISH, Language.CATALAN, Language.ENGLISH}
    )
    default_language = Language.SPANISH
    default_user_agent = BROWSER_USER_AGENT
    default_page_size = 24
    max_page_size = MAX_ROWS

    def __init__(
        self,
        sale_point: str | int | None = None,
        *,
        catalog: str = DEFAULT_CATALOG,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._sale_point = _validate_sale_point(sale_point)
        self._catalog = _validate_catalog(catalog)
        self._storefront_pending = False
        self._warm_until: float | None = None
        self._warm_epoch = 0
        self._challenged_epoch = -1
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
        catalog: str = DEFAULT_CATALOG,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> Self:
        """bind the sale point that serves ``postal_code``.

        carrefour publishes no postcode-to-sale-point resolver: the postcode
        endpoint answers with physical shops, whose ids belong to a different
        namespace, and the storefront picks a sale point by mutating session
        state. so this costs two requests — the shops near the postcode, then
        the drive directory — and binds the sale point in the same postcode
        if there is one, otherwise the one nearest the closest shop.

        a postcode carrefour does not serve raises
        :class:`~supermercapy.OutOfCoverageError`.
        """

        postal_code = validate_postal_code(postal_code)
        client = cls(
            catalog=catalog,
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )
        try:
            client._sale_point = client._resolve_sale_point(postal_code)
            return client
        except BaseException:
            client.close()
            raise

    @property
    def sale_point(self) -> str | None:
        """return the six-character sale point this client prices against."""

        return self._sale_point

    @property
    def catalog(self) -> str:
        """return the catalog searches are scoped to; ``food`` by default."""

        return self._catalog

    @property
    def store_id(self) -> str | None:
        """return the sale point id; the same value as :attr:`sale_point`."""

        return self._sale_point

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        sort: str | None = None,
    ) -> CarrefourSearchResult:
        """search one page of products on the direct index.

        the query is lowercased before it is sent, as carrefour's own client
        does, so both share the cdn's cache key. ``sort`` takes the index's
        own spelling — ``"price asc"``, ``"best_sellers_food desc"`` — and an
        unsortable field is refused upstream rather than ignored.

        one query reaches at most 2,998 documents: the offset ceiling is
        2,498 and a page at most 500 rows. a page whose ``truncated`` flag is
        set sat against that ceiling with matches left over.
        """

        text = _validate_query(query)
        offset = _validate_offset(cursor)
        size = self._resolve_page_size(page_size)
        if sort is not None and (not isinstance(sort, str) or not sort.strip()):
            raise ConfigurationError("sort must be a non-empty string")
        params = self._search_params(text, rows=size, start=offset)
        if sort is not None:
            params["sort"] = sort
        data = self._request_json("GET", f"{EMPATHY_URL}/search", params=params)
        return parse_search_result(data, query=query, offset=offset, page_size=size)

    def get_product(self, product_id: str | int) -> CarrefourProduct:
        """return one complete product, nutrition included, from its page.

        ids come in four unrelated spellings and are opaque strings, never
        integers. the slug in a product url is not load-bearing: this asks for
        a placeholder one and follows the redirect carrefour answers with,
        which is also how an unknown id is detected — it redirects to the home
        page instead of to a product.
        """

        identifier = validate_identifier(product_id, "product_id")
        return parse_product(self._product_state(identifier))

    def get_product_by_ean(self, ean: str) -> CarrefourProduct:
        """return one complete product by its barcode.

        the index has no barcode endpoint; a barcode is matched as ordinary
        free text, which returns exactly one document when it is known. the
        document is then fetched again as a page, because nutrition lives
        nowhere else, and the barcode is checked against it so a fuzzy text
        match cannot answer with the wrong product.
        """

        code = validate_identifier(ean, "ean")
        summary = self._lookup(code)
        if summary.ean is not None and summary.ean != code:
            raise NotFoundError(f"carrefour has no product with ean {code}")
        product = self.get_product(summary.id)
        if product.ean is not None and product.ean != code:
            raise NotFoundError(f"carrefour has no product with ean {code}")
        return product

    def get_categories(self, *, deep: bool = False) -> tuple[CarrefourCategory, ...]:
        """return the food departments from the storefront's navigation menu.

        one request returns the ten departments, named in the client's
        language. ``deep=True`` adds each department's aisles at one more
        request per department. the menu stops at the aisles; the leaves
        under an aisle are the children :meth:`get_category` returns for it.
        the menu answers without a warm session, so neither costs the
        warm-up page unless cloudflare challenges the first request.
        """

        departments = parse_menu(self._menu(FOOD_ROOT), FOOD_ROOT)
        if not deep:
            return departments
        return tuple(
            dataclasses.replace(
                department,
                children=parse_menu(self._menu(department.id), department.id, level=2),
            )
            for department in departments
        )

    def get_category(
        self, category_id: str | int, *, offset: int = 0
    ) -> CarrefourCategory:
        """return one category with its children and one page of its products.

        everything comes from the category's listing page, which answers any
        placeholder slug, so this costs one page fetch. the page is fixed at
        24 products and floors ``offset`` to a multiple of 24; the page size
        cannot be set from the url at all. an unknown id redirects to the
        home page, which raises :class:`~supermercapy.NotFoundError`.
        """

        wanted = normalise_category_id(validate_identifier(category_id, "category_id"))
        start = _validate_offset_argument(offset, "offset")
        params = {"offset": str(start)} if start else None
        state = self._storefront_state(
            CATEGORY_PATH.format(category_id=wanted), params=params
        )
        listing = parse_listing(state)
        return dataclasses.replace(
            parse_category_page(state, wanted),
            products=listing.products,
            product_count=listing.total_results,
        )

    def list_stores(self, postal_code: str | None = None) -> tuple[CarrefourStore, ...]:
        """return the sale points, or the physical shops near one postcode.

        without a postcode this is the drive directory, and every id in it can
        be passed straight back to the constructor. with one it is the shops
        near that postcode, ordered by distance; those ids identify buildings
        rather than sale points and the constructor does not accept them.
        """

        if postal_code is None:
            return parse_drives(self._drives())
        return parse_stores_location(self._shops(validate_postal_code(postal_code)))

    def get_home(self) -> tuple[CarrefourHomeSection, ...]:
        """return the product carousels of the supermarket home page.

        the page is a cms page whose carousels arrive already filled, in page
        order, with their titles in the cms documents beside them. it is
        also the warm-up page, so a cold client fetches it twice.
        """

        return parse_home(self._storefront_state(SUPERMARKET_PATH))

    # -------------------------------------------------------------- extensions

    def get_category_products(
        self, category_url: str, *, offset: int = 0
    ) -> CarrefourListing:
        """return one rendered page of a category listing by its url.

        the url comes from a :class:`CarrefourCategory`. sponsored rows are
        dropped: the ad-injected carousels are never read and a card carrying
        an ad id inside the grid is flagged ``is_sponsored``.
        """

        if not isinstance(category_url, str) or not category_url.strip():
            raise ConfigurationError("category_url must be a non-empty url")
        start = _validate_offset_argument(offset, "offset")
        path = _path_of(category_url)
        params = {"offset": str(start)} if start else None
        return parse_listing(self._storefront_state(path, params=params))

    def get_stock(self, product_id: str | int) -> int | None:
        """return how many units of a product the session's sale point holds.

        stock is published by carrefour's own proxied search and by nothing
        else. that proxy ignores ``store``, so this figure belongs to the
        session's default sale point rather than to :attr:`sale_point`; the
        two agree only when the session already sits on that sale point.

        the proxy also refuses a request without a ``session`` parameter with
        a firewall block rather than an api error, so one is always sent; its
        value is never read.
        """

        identifier = validate_identifier(product_id, "product_id")
        data = self._storefront_json(
            "GET",
            PROXY_SEARCH_URL,
            params={
                "query": identifier.lower(),
                "lang": self._language.value,
                "catalog": self._catalog,
                "session": PROXY_SESSION,
                "rows": str(min(self.default_page_size, PROXY_MAX_ROWS)),
                "start": "0",
            },
        )
        for item in as_items(as_object(data.get("content")).get("docs")):
            document = as_object(item)
            if str(document.get("product_id") or "") == identifier:
                return as_integer(document.get("stock"))
        raise NotFoundError(f"carrefour has no product {identifier}")

    def suggest(self, query: str, *, limit: int = 5) -> tuple[str, ...]:
        """return the query completions the search box would offer."""

        text = _validate_query(query)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ConfigurationError("limit must be a positive integer")
        data = self._request_json(
            "GET",
            f"{EMPATHY_URL}/empathize",
            params={
                "query": text.strip().lower(),
                "lang": self._language.value,
                "rows": str(limit),
            },
        )
        return parse_suggestions(data)

    def download_photo(
        self,
        photo: CarrefourPhoto,
        destination: str | os.PathLike[str],
        *,
        width: int | None = None,
    ) -> Path:
        """download one image, optionally re-rendered at ``width`` pixels."""

        if not isinstance(photo, CarrefourPhoto):
            raise ConfigurationError("photo must be a CarrefourPhoto instance")
        if width is None:
            return self.download(photo.url, destination)
        return self.download(photo.sized(width), destination)

    # ---------------------------------------------------------- transport hooks

    def _default_headers(self) -> dict[str, str]:
        headers = super()._default_headers()
        headers["Accept-Language"] = ACCEPT_LANGUAGES.get(
            self._language.value, self._language.value
        )
        return headers

    def _prime(self) -> None:
        """warm a cloudflare session before the first storefront request.

        one html fetch sets ``session_id`` and ``__cf_bm`` on the connection
        pool, which turns an occasional cold 403 into a reliable 200. the
        cookie lives half an hour, so it is renewed every twenty-five
        minutes, and requests to the search index or the static host skip the
        warm-up entirely because neither is gated.
        """

        if not self._storefront_pending:
            return
        self._request("GET", f"{SITE_URL}{SUPERMARKET_PATH}", headers=DOCUMENT_HEADERS)
        self._warm_until = time.monotonic() + PRIME_TTL
        self._warm_epoch += 1

    def _prime_ttl(self) -> float | None:
        """keep one warm session for twenty-five minutes."""

        return PRIME_TTL

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        """tell cloudflare's two 403 pages apart.

        a firewall rule and an interactive challenge share the status code and
        differ only in the body. the challenge clears after a new session is
        warmed, and the rule does not clear at all, so only the first is worth
        a retry.
        """

        if response.status_code == _FORBIDDEN:
            body = _body_of(response).lower()
            if BLOCKED_MARKER in body:
                return ResponseVerdict.BLOCKED
            if CHALLENGE_MARKER in body:
                return ResponseVerdict.CHALLENGED
        return super()._classify(response)

    def _on_challenge(self, response: httpx.Response) -> bool:
        """warm a new session once, then let the challenge be raised."""

        if not self._storefront_pending or self._challenged_epoch == self._warm_epoch:
            return False
        self._warm_until = None
        self._invalidate_priming()
        self._ensure_primed()
        self._challenged_epoch = self._warm_epoch
        return True

    # ---------------------------------------------------------------- internals

    def _search_params(self, query: str, *, rows: int, start: int) -> dict[str, str]:
        params = {
            "query": query.strip().lower(),
            "lang": self._language.value,
            "catalog": self._catalog,
            "rows": str(rows),
            "start": str(start),
        }
        if self._sale_point is not None:
            params["store"] = self._sale_point
        return params

    def _lookup(self, identifier: str) -> CarrefourProduct:
        """resolve one id or barcode to a summary through the search index."""

        data = self._request_json(
            "GET",
            f"{EMPATHY_URL}/search",
            params=self._search_params(identifier, rows=1, start=0),
        )
        result = parse_search_result(data, query=identifier, offset=0, page_size=1)
        if not result.products:
            raise NotFoundError(f"carrefour has no product matching {identifier}")
        return result.products[0]

    @contextmanager
    def _storefront(self) -> Iterator[None]:
        """mark the requests inside as needing a warm cloudflare session."""

        previous = self._storefront_pending
        self._storefront_pending = True
        if self._warm_until is None or time.monotonic() >= self._warm_until:
            self._invalidate_priming()
        try:
            yield
        finally:
            self._storefront_pending = previous

    def _storefront_json(
        self, method: str, url: str, *, params: Mapping[str, str] | None = None
    ) -> JsonObject:
        with self._storefront():
            return self._request_json(method, url, params=params, headers=XHR_HEADERS)

    def _storefront_state(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> JsonObject:
        """fetch one rendered page and return the state it hands the browser."""

        with self._storefront():
            response = self._follow(f"{SITE_URL}{path}", params=params)
        return _state_of(response.text, response.url.path)

    def _follow(
        self, url: str, *, params: Mapping[str, str] | None = None
    ) -> httpx.Response:
        """send one page request and follow carrefour's canonical redirects."""

        response = self._request("GET", url, params=params, headers=DOCUMENT_HEADERS)
        for _ in range(_MAX_REDIRECTS):
            if response.status_code not in _REDIRECTS:
                return response
            location = response.headers.get("Location", "")
            if not httpx.URL(location).path.startswith(_CATALOG_PREFIX):
                raise NotFoundError(f"GET {url} redirected away from the catalog")
            target = (
                location if location.startswith("http") else f"{SITE_URL}{location}"
            )
            # a canonical redirect that drops the query would lose the page
            carried = None if "?" in location else params
            response = self._request(
                "GET", target, params=carried, headers=DOCUMENT_HEADERS
            )
        raise InvalidResponseError(f"GET {url} redirected more than {_MAX_REDIRECTS}")

    def _product_state(self, identifier: str) -> JsonObject:
        path = f"{SUPERMARKET_PATH}/p/R-{quote(identifier, safe='')}/p"
        try:
            return self._storefront_state(path)
        except InvalidResponseError as error:
            raise NotFoundError(f"carrefour has no product {identifier}") from error

    def _menu(self, current: str) -> JsonObject:
        params = {
            "current_category": current,
            "depth": "1",
            "lang": self._language.value,
        }
        if self._sale_point is not None:
            params["sale_point"] = self._sale_point
        # the menu answers cold requests, so it skips the 380 kB warm-up page
        # unless cloudflare challenges it, and then warms a session and retries
        try:
            return self._request_json(
                "GET", MENU_URL, params=params, headers=XHR_HEADERS
            )
        except ChallengedError:
            return self._storefront_json("GET", MENU_URL, params=params)

    def _drives(self) -> JsonObject:
        return self._storefront_json("GET", f"{SALEPOINTS_URL}/drives")

    def _shops(self, postal_code: str) -> JsonObject:
        try:
            return self._storefront_json(
                "GET", f"{SALEPOINTS_URL}/stores-location/{postal_code}"
            )
        except NotFoundError as error:
            raise OutOfCoverageError(
                f"carrefour serves no store near {postal_code}"
            ) from error

    def _resolve_sale_point(self, postal_code: str) -> str:
        shops = parse_stores_location(self._shops(postal_code))
        if not shops:
            raise OutOfCoverageError(f"carrefour serves no store near {postal_code}")
        drives = parse_drives(self._drives())
        exact = next(
            (drive.id for drive in drives if drive.postal_code == postal_code), None
        )
        if exact is not None:
            return exact
        nearest = _nearest(drives, shops[0])
        if nearest is None:
            raise OutOfCoverageError(
                f"carrefour has no sale point serving {postal_code}"
            )
        return nearest


def _body_of(response: httpx.Response) -> str:
    """read a response body even when the request asked for a stream."""

    try:
        return response.text
    except httpx.ResponseNotRead:  # pragma: no cover - only streamed downloads
        return response.read().decode("utf-8", "replace")


def _state_of(html: str, label: str) -> JsonObject:
    state = extract_json_assignment(html, STATE_MARKER)
    if not isinstance(state, dict):
        raise InvalidResponseError(f"{label} carries no rendered state")
    return state


def _path_of(url: str) -> str:
    if url.startswith(SITE_URL):
        return url[len(SITE_URL) :]
    if url.startswith("/"):
        return url
    raise ConfigurationError("category_url must be a carrefour storefront url")


def _nearest(drives: tuple[CarrefourStore, ...], shop: CarrefourStore) -> str | None:
    if shop.latitude is None or shop.longitude is None:
        return drives[0].id if drives else None
    ranked = [
        (
            _distance_km(
                (shop.latitude, shop.longitude), (drive.latitude, drive.longitude)
            ),
            drive.id,
        )
        for drive in drives
        if drive.latitude is not None and drive.longitude is not None
    ]
    if not ranked:
        return drives[0].id if drives else None
    return min(ranked)[1]
