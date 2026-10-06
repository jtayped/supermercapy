"""synchronous plusfresc client."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import os
import re
import time
from collections.abc import Iterator
from datetime import date
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
from .._core.coerce import as_dmy_date, as_object, as_text
from .._core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    InvalidResponseError,
    NotFoundError,
    OutOfCoverageError,
    TransportError,
)
from .._core.models import Language
from ._constants import (
    API_URL,
    DEFAULT_CENTER,
    HIGHLIGHTS_CATEGORY,
    MINIMUM_QUERY_LENGTH,
    NEW_ARRIVALS_CATEGORY,
    OFFERS_CATEGORY,
    ROOT_CATEGORY,
    TOKEN_REFRESH_MARGIN,
    TOKEN_TTL,
)
from .models import (
    ImageSize,
    PlusfrescCategory,
    PlusfrescPhoto,
    PlusfrescProduct,
    PlusfrescSearchResult,
    PlusfrescStore,
    PlusfrescUnit,
    parse_category,
    parse_center,
    parse_pickup_point,
    parse_product,
    parse_search_result,
    parse_unit,
    resolve_center_id,
)

# the backend answers a postcode it does not deliver to with a normal 409 and
# a body of "0", and a detail request for a composite placement id with a 416.
_OUT_OF_COVERAGE_STATUS = 409
_BAD_INPUT_STATUS = 416

# the guest token is a credential for the api host alone: images live on the
# storefront host, and download() takes any url a caller hands it.
_API_ORIGIN = httpx.URL(API_URL)

# search takes its query as a path segment, which iis refuses to route when it
# holds a slash, a backslash, a plus or a trailing dot (404), or one of
# asp.net's forbidden path characters (400). they become spaces instead.
_PATH_UNSAFE = re.compile(r"[/\\%&:*?<>+]")
_TRAILING_DOTS = re.compile(r"[\s.]+$")


def _validate_center(center: int | str) -> str:
    resolved = resolve_center_id(center)
    if resolved is None or resolved < 1:
        raise ConfigurationError("center must be a positive integer center id")
    return str(resolved)


def _validate_query(query: str) -> str:
    """return the query as the path segment the backend can route."""

    if not isinstance(query, str):
        raise ConfigurationError("query must be a string")
    text = " ".join(_PATH_UNSAFE.sub(" ", query).split())
    text = _TRAILING_DOTS.sub("", text)
    if len(text) < MINIMUM_QUERY_LENGTH:
        raise ConfigurationError(
            f"query must hold at least {MINIMUM_QUERY_LENGTH} characters"
        )
    return text


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise ConfigurationError("cursor must be an offset returned by search")
    return int(cursor)


def _token_expiry(token: str) -> float | None:
    """read ``exp`` out of a jwt payload without verifying anything."""

    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    expiry = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return None
    return float(expiry)


class Plusfresc(BaseClient):
    """a reusable synchronous client scoped to one plusfresc preparation center.

    a center decides the assortment; prices looked the same across the two
    centers compared during reconnaissance, but only one category was checked,
    so do not assume they are national. resolve one with
    :meth:`from_postal_code` or :meth:`list_stores` rather than guessing;
    center ``12`` (lleida) is the storefront's own default.

    the catalog, the category tree, and the postcode lookup need no
    credentials. search, product details, and the extended sheet need a guest
    token, which the client mints on demand with an unauthenticated post and
    refreshes a minute before it expires.
    """

    store_name = "plusfresc"
    capabilities = frozenset(
        {
            Capability.POSTAL_CODE,
            Capability.STORES,
            Capability.CATALOG,
            Capability.NEW_ARRIVALS,
            Capability.OFFERS,
            Capability.NUTRITION,
            Capability.PROMOTIONS,
        }
    )
    supported_languages = frozenset({Language.CATALAN, Language.SPANISH})
    default_language = Language.CATALAN
    default_page_size = 24
    max_page_size = 100
    default_min_request_interval = 0.5

    def __init__(
        self,
        center: int | str = DEFAULT_CENTER,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._center = _validate_center(center)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._minting = False
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
        """resolve a postal code with one request and return a scoped client.

        plusfresc delivers around lleida, barcelona, and tarragona only; a
        postcode outside that footprint is answered with an http 409 and
        raises :class:`~supermercapy.OutOfCoverageError`.
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
            client._center = client._resolve_center(postal_code)
            return client
        except BaseException:
            client.close()
            raise

    @property
    def center(self) -> int:
        """return the preparation center this client is scoped to."""

        return int(self._center)

    @property
    def store_id(self) -> str | None:
        """return the center id as a string; the same value as ``center``."""

        return self._center

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> PlusfrescSearchResult:
        """search one page of products.

        the backend takes no paging parameter of any kind and answers with at
        most a hundred rows, so every page is carved out of one response and
        the cursor is an offset into rows already fetched. a page whose
        ``truncated`` flag is set sat at the cap, which means ``total_hits``
        is a floor rather than a total and the rest of the matches cannot be
        reached; walk the category tree instead when you need them all.

        the query travels as a path segment, so the characters the backend
        cannot route there (``/ \\ % & : * ? < > +``) are sent as spaces and
        trailing dots are dropped; ``query`` on the result keeps the original.
        """

        text = _validate_query(query)
        offset = _validate_offset(cursor)
        size = self._resolve_page_size(page_size)
        rows = self._authenticated_rows(
            "GET",
            f"{API_URL}/search/languages/{self._language.value}"
            f"/{quote(text, safe='')}/products/{self._center}",
            label="search",
        )
        return parse_search_result(
            rows,
            query=query,
            offset=offset,
            page_size=size,
            language=self._language,
        )

    def get_product(self, product_id: str | int) -> PlusfrescProduct:
        """return one product by its six-digit item id.

        the composite placement id a listing row calls ``id`` is not accepted
        upstream and raises :class:`~supermercapy.NotFoundError`.
        """

        item = validate_identifier(product_id, "product_id")
        data = self._authenticated_json(
            "GET",
            f"{API_URL}/productdetails/files/{self._center}"
            f"/{quote(item, safe='')}/{self._language.value}",
        )
        return parse_product(data, language=self._language)

    def get_categories(self) -> tuple[PlusfrescCategory, ...]:
        """return the whole category tree, already nested, in one request."""

        return self._tree(ROOT_CATEGORY).children

    def get_category(
        self, category_id: str | int, *, page_size: int | None = None
    ) -> PlusfrescCategory:
        """return one category with its direct children and a page of products.

        the tree carries no products, so this costs one request for the node
        and one for its listing. the node endpoint names no parent, so
        ``parent_id`` is ``None`` here; :meth:`get_categories` sets it.
        """

        wanted = validate_identifier(category_id, "category_id")
        node = self._tree(wanted)
        listing = self.get_category_products(wanted, page_size=page_size)
        return dataclasses.replace(node, products=listing.products)

    def iter_catalog(self) -> Iterator[PlusfrescProduct]:
        """yield the whole catalogue from the one listing of the ``Root`` node.

        the backend answers with every product of the center in a single
        uncached response of several megabytes, so this costs one request
        and no token. a product placed under more than one category comes
        back once per placement; :meth:`get_catalog` deduplicates by id.
        gate repeated walks behind :meth:`get_catalog_version`.
        """

        yield from self._listing(ROOT_CATEGORY)

    def list_stores(self, postal_code: str | None = None) -> tuple[PlusfrescStore, ...]:
        """return every store and locker, or only those of one postcode's center.

        a locker is listed under the center that prepares its orders, so every
        ``id`` here can be passed straight back to the constructor.
        """

        wanted: str | None = None
        if postal_code is not None:
            wanted = self._resolve_center(validate_postal_code(postal_code))
        data = self._request_json_any("GET", f"{API_URL}/utils/centres")
        stores = tuple(
            store
            for item in _array(data, "pickup points")
            if (store := parse_pickup_point(item)) is not None
        )
        if wanted is None:
            return stores
        return tuple(store for store in stores if store.id == wanted)

    def get_new_arrivals(self) -> tuple[PlusfrescProduct, ...]:
        """return the products the storefront collects under "novetats".

        no row carries a novelty flag of its own, so membership of that
        category is what marks a product new here.
        """

        return tuple(
            dataclasses.replace(product, is_new=True)
            for product in self._listing(NEW_ARRIVALS_CATEGORY)
        )

    def get_offers(self, *, highlighted: bool = False) -> tuple[PlusfrescProduct, ...]:
        """return the products of the web-offers category.

        ``highlighted`` swaps it for the smaller carousel of featured
        campaigns the storefront promotes on its home page.
        """

        category = HIGHLIGHTS_CATEGORY if highlighted else OFFERS_CATEGORY
        return self._listing(category)

    # -------------------------------------------------------------- extensions

    def get_category_products(
        self,
        category_id: str | int,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> PlusfrescSearchResult:
        """list one page of a category, descendants included.

        the endpoint answers with the whole category at once and takes no
        paging parameter, so the page is carved out client-side just as it is
        for search. ``Root`` is the whole catalogue, which is several
        megabytes; gate it behind :meth:`get_catalog_version`.
        """

        wanted = validate_identifier(category_id, "category_id")
        offset = _validate_offset(cursor)
        size = self._resolve_page_size(page_size)
        return parse_search_result(
            self._rows(wanted),
            query="",
            offset=offset,
            page_size=size,
            language=self._language,
            # a listing is the whole category, never cut at the search cap
            limit=None,
        )

    def centers(self) -> tuple[PlusfrescStore, ...]:
        """return the eight preparation centers, without their lockers."""

        data = self._request_json_any("GET", f"{API_URL}/zones/preparationcenters")
        return tuple(
            store
            for item in _array(data, "preparation centers")
            if (store := parse_center(item)) is not None
        )

    def get_catalog_version(self) -> date:
        """return the date the catalogue last changed.

        the storefront caches its whole tree until this date moves, and so can
        a caller: nothing else here is cacheable, every listing is served
        uncached and the catalogue root alone is several megabytes.
        """

        data = self._request_json("GET", f"{API_URL}/categories/newest")
        version = as_dmy_date(data.get("unchanged_since"))
        if version is None:
            raise InvalidResponseError("catalog version response has no usable date")
        return version

    def get_units(self) -> tuple[PlusfrescUnit, ...]:
        """return the unit-of-measure codes a product's ``unit_measure`` uses."""

        data = self._request_json_any(
            "GET", f"{API_URL}/utils/units/{self._language.value}"
        )
        return tuple(
            unit
            for item in _array(data, "units")
            if (unit := parse_unit(item)) is not None
        )

    def download_photo(
        self,
        photo: PlusfrescPhoto,
        destination: str | os.PathLike[str],
        *,
        size: ImageSize | str = ImageSize.LARGE,
    ) -> Path:
        """download one image at a rendition through an atomic replacement."""

        if not isinstance(photo, PlusfrescPhoto):
            raise ConfigurationError("photo must be a PlusfrescPhoto instance")
        return self.download(photo.sized(size), destination)

    # ---------------------------------------------------------------- internals

    def _tree(self, category_id: str) -> PlusfrescCategory:
        data = self._request_json(
            "GET",
            f"{API_URL}/categories/tree/{self._center}/{quote(category_id, safe='')}",
        )
        node = as_object(data.get("category"))
        if not node:
            raise NotFoundError(f"plusfresc has no category {category_id}")
        return parse_category(node, language=self._language)

    def _rows(self, category_id: str) -> list[object]:
        data = self._request_json_any(
            "GET",
            f"{API_URL}/products/category/{quote(category_id, safe='')}/{self._center}",
        )
        return _array(data, "category listing")

    def _listing(self, category_id: str) -> tuple[PlusfrescProduct, ...]:
        return tuple(
            parse_product(item, language=self._language)
            for item in self._rows(category_id)
        )

    def _resolve_center(self, postal_code: str) -> str:
        try:
            data = self._request_json_any(
                "GET", f"{API_URL}/utils/{postal_code}/centre"
            )
        except TransportError as error:
            if error.status_code != _OUT_OF_COVERAGE_STATUS:
                raise
            raise OutOfCoverageError(
                f"plusfresc does not deliver to {postal_code}"
            ) from error
        center = resolve_center_id(data)
        if center is None or center < 1:
            raise OutOfCoverageError(f"plusfresc does not deliver to {postal_code}")
        return str(center)

    # -------------------------------------------------------------------- auth

    def _authenticated_json(self, method: str, url: str) -> object:
        self._ensure_token()
        return self._request_json_any(method, url)

    def _authenticated_rows(self, method: str, url: str, *, label: str) -> list[object]:
        return _array(self._authenticated_json(method, url), label)

    def _ensure_token(self) -> None:
        if (
            self._token is not None
            and time.time() < self._token_expires_at - TOKEN_REFRESH_MARGIN
        ):
            return
        self._mint_token()

    def _mint_token(self) -> None:
        # clearing first keeps a stale token off the mint request itself.
        self._token = None
        self._minting = True
        try:
            data = self._request_json_any(
                "POST",
                f"{API_URL}/loginGuest/{self._center}",
                content='""',
                headers={"Content-Type": "application/json"},
            )
        finally:
            self._minting = False
        token = (as_text(data) or "").strip()
        if not token:
            raise AuthenticationError("plusfresc returned no guest token")
        self._token = token
        expiry = _token_expiry(token)
        self._token_expires_at = time.time() + TOKEN_TTL if expiry is None else expiry

    def _prepare_request(self, request: httpx.Request) -> None:
        if self._token is not None and _is_api(request.url):
            request.headers["Authorization"] = f"Bearer {self._token}"

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        on_api = _is_api(response.request.url)
        if response.status_code == httpx.codes.UNAUTHORIZED and on_api:
            return ResponseVerdict.AUTH_EXPIRED
        if response.status_code == _BAD_INPUT_STATUS:
            return ResponseVerdict.NOT_FOUND
        return super()._classify(response)

    def _on_auth_expired(self) -> bool:
        # a 401 on the mint itself must not mint again, or it recurses until
        # the interpreter gives up.
        if self._minting:
            return False
        try:
            self._mint_token()
        except (TransportError, AuthenticationError):
            return False
        return True


def _is_api(url: httpx.URL) -> bool:
    return (url.scheme, url.host, url.port) == (
        _API_ORIGIN.scheme,
        _API_ORIGIN.host,
        _API_ORIGIN.port,
    )


def _array(data: object, label: str) -> list[object]:
    if not isinstance(data, list):
        raise InvalidResponseError(f"{label} response is not a json array")
    return data
