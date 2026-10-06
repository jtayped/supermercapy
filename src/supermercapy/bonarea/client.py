"""synchronous bonàrea client."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from pathlib import Path

import httpx

from .._core.capabilities import Capability
from .._core.client import (
    BaseClient,
    ResponseVerdict,
    RetryPolicy,
    validate_identifier,
)
from .._core.coerce import JsonObject, as_boolean, as_text
from .._core.exceptions import ConfigurationError, NotFoundError, TransportError
from .._core.models import Language
from ._constants import API_URL, RUNTIME_ERROR_MARKER
from .models import (
    BonareaCategory,
    BonareaPhoto,
    BonareaProduct,
    BonareaSearchResult,
    Characteristic,
    listing_categories,
    parse_articles,
    parse_category_tree,
    parse_postal_codes,
    parse_product,
    parse_search_result,
    to_api_id,
)

# the listing endpoint answers a blank reference with the whole category tree
# and no articles, which is the cheapest way to ask for the tree alone.
_WHOLE_TREE = ""
_SERVER_ERROR = 500


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not (cursor.isascii() and cursor.isdigit()):
        raise ConfigurationError("cursor must be an offset returned by search")
    return int(cursor)


class Bonarea(BaseClient):
    """a reusable synchronous client for the bonàrea online shop.

    there is nothing to bind: the catalogue, the prices and the stock figures
    are national, so no postal code, zone or store selects them. language is
    the only axis, and it is the one piece of session state the storefront
    keeps — see :meth:`_prime`.

    every read is a form post to ``/{language}/shop/{action}``, and none of
    them paginates: one search returns every hit and one category listing
    returns every article, so responses are large and paging happens here.
    """

    store_name = "bonarea"
    capabilities = frozenset({Capability.CATALOG, Capability.NUTRITION})
    supported_languages = frozenset({Language.SPANISH, Language.CATALAN})
    default_page_size = 24
    default_min_request_interval = 0.5

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
        self._session_language: Language | None = None
        self._search_pending = False
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
    ) -> BonareaSearchResult:
        """return one page of hits, sliced from the whole result set.

        the storefront takes no page, size or sort parameter and answers every
        query with all of its hits at once, so a cursor walk re-requests the
        same response; :meth:`iter_search` streams it in one request instead.
        """

        if not isinstance(query, str):
            raise ConfigurationError("query must be a string")
        offset = _validate_offset(cursor)
        page_size = self._resolve_page_size(page_size)
        return parse_search_result(
            self._search(query), query=query, offset=offset, page_size=page_size
        )

    def iter_search(
        self, query: str, *, page_size: int | None = None
    ) -> Iterator[BonareaProduct]:
        """yield every hit for ``query`` from the single response search returns.

        ``page_size`` is validated and then ignored: there are no pages to
        fetch, so streaming never issues a second request.
        """

        self._resolve_page_size(page_size)
        return iter(parse_articles(self._search(query), label="search"))

    def get_product(self, product_id: str | int) -> BonareaProduct:
        """return one article, in either the ``13*5361`` or ``13_5361`` spelling.

        an unknown id is answered with ``{"article": null}`` and http 200
        rather than a 404, which becomes a :class:`NotFoundError` here.
        """

        identifier = to_api_id(validate_identifier(product_id, "product_id"))
        data = self._post("Article", {"identifier": identifier})
        article = data.get("article")
        if article is None:
            raise NotFoundError(f"bonarea has no article {identifier}")
        return parse_product(article)

    def get_categories(self) -> tuple[BonareaCategory, ...]:
        """return the whole category tree, already nested, in one request.

        every listing response embeds the tree, so asking for a blank listing
        is the cheapest way to fetch it.
        """

        return parse_category_tree(self._listing(_WHOLE_TREE).get("nivells"))

    def get_category(self, category_id: str | int) -> BonareaCategory:
        """return one category with its children and every article it lists.

        one request answers both, because a listing response carries the whole
        tree alongside its articles. a menu-level category resolves to a node
        with children and no products: only the level the tree calls a listing
        level returns articles.

        an unknown reference makes the storefront throw, and it answers with
        an http 500 error page rather than a 404. that page is not retried,
        and the id is checked against the tree with one more request before
        it is reported as :class:`NotFoundError`; an id the tree does know
        re-raises the original error.
        """

        wanted = to_api_id(validate_identifier(category_id, "category_id"))
        try:
            data = self._listing(wanted)
        except TransportError as error:
            if error.status_code != _SERVER_ERROR:
                raise
            if _find_category(self.get_categories(), wanted) is not None:
                raise
            raise NotFoundError(f"bonarea has no category {wanted}") from error
        node = _find_category(parse_category_tree(data.get("nivells")), wanted)
        if node is None:
            raise NotFoundError(f"bonarea has no category {wanted}")
        products = _articles(data)
        return dataclasses.replace(node, products=products, product_count=len(products))

    def iter_catalog(self) -> Iterator[BonareaProduct]:
        """yield every article by walking one listing per branch of the tree.

        one request fetches the tree and one more follows for each listing
        node, which is 493 of them at the time of writing. articles listed
        under more than one category are yielded more than once;
        :meth:`get_catalog` deduplicates them by id.
        """

        for node in listing_categories(self.get_categories()):
            yield from _articles(self._listing(node.id))

    # -------------------------------------------------------------- extensions

    def new_products(
        self, category_id: str | int | None = None
    ) -> tuple[BonareaProduct, ...]:
        """return the articles badged as new, deduplicated by id.

        bonàrea publishes no novelties endpoint, so this filters listings on
        the :attr:`Characteristic.NEW` badge. one category costs one request;
        passing none walks the whole catalog.
        """

        return self._badged(Characteristic.NEW, category_id)

    def price_drops(
        self, category_id: str | int | None = None
    ) -> tuple[BonareaProduct, ...]:
        """return the articles badged as reduced, deduplicated by id.

        the badge is the only price-drop signal bonàrea publishes: no previous
        price accompanies it. the cost is the same as :meth:`new_products`.
        """

        return self._badged(Characteristic.PRICE_DROP, category_id)

    def delivery_zones(
        self, province: str | int, town: str | None = None
    ) -> tuple[str, ...]:
        """return the postal codes bonàrea delivers to inside one province.

        ``province`` is the two-digit spanish province code, and ``town``
        narrows the answer to one town. this is delivery coverage only: it
        selects nothing, because the catalogue and its prices are national.
        """

        code = validate_identifier(province, "province")
        if town is not None and not isinstance(town, str):
            raise ConfigurationError("town must be a string")
        data = self._request_json_any(
            "POST",
            self._action_url("GetPostalCodes"),
            data={"province": code, "town": town or ""},
        )
        return parse_postal_codes(data)

    def get_product_sheet(self, product_id: str | int) -> str:
        """return the storefront's pre-rendered product modal as raw html.

        :meth:`get_product` is the structured route; this is the fallback for
        anything the json omits but the modal renders.
        """

        identifier = to_api_id(validate_identifier(product_id, "product_id"))
        data = self._post("GetProductSheet", {"idArticle": identifier})
        sheet = as_text(data.get("htmlProductSheet"))
        # an unknown id still gets a sheet, a "this product does not exist"
        # page, flagged only by success being false
        if (
            as_boolean(data.get("success")) is False
            or sheet is None
            or not sheet.strip()
        ):
            raise NotFoundError(f"bonarea has no product sheet for {identifier}")
        return sheet

    def download_photo(
        self,
        photo: BonareaPhoto,
        destination: str | os.PathLike[str],
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> Path:
        """download one image, optionally resized by the cdn, atomically.

        omitting both dimensions downloads the original upload; the storefront
        itself asks for 500 px in a grid and 1000 px on a product page.
        """

        if not isinstance(photo, BonareaPhoto):
            raise ConfigurationError("photo must be a BonareaPhoto instance")
        if (width is None) != (height is None):
            raise ConfigurationError("width and height must be given together")
        if width is None or height is None:
            return self.download(photo.url, destination)
        return self.download(photo.sized(width, height), destination)

    # ---------------------------------------------------------- transport hooks

    def _prime(self) -> None:
        """warm an asp.net session so search answers in the chosen language.

        ``search`` is the one endpoint that ignores the language in the path
        and reads it from the session instead, which leaves a cookieless
        client reading catalan answers to a spanish query. fetching the
        language's home page once sets the session cookie the connection pool
        then reuses.

        listings and product details honour the path, so the warm-up is
        deferred until the first search asks for it and never costs a request
        for a client that only browses.
        """

        if not self._search_pending or self._session_language is self._language:
            return
        self._request_text(
            "GET",
            f"{API_URL}/{self._language.value}",
            headers={"Accept": "text/html"},
        )
        self._session_language = self._language

    def _prime_ttl(self) -> float | None:
        """expire the warm-up immediately so :meth:`_prime` decides each time."""

        return 0.0

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        """do not retry asp.net's runtime error page.

        it is what an unhandled exception renders, such as the one an unknown
        category reference throws, so asking again only throws again. any
        other server error is still retried.
        """

        if response.status_code == _SERVER_ERROR and _is_runtime_error(response):
            return ResponseVerdict.ERROR
        return super()._classify(response)

    # ---------------------------------------------------------------- internals

    def _action_url(self, action: str) -> str:
        return f"{API_URL}/{self._language.value}/shop/{action}"

    def _post(self, action: str, data: dict[str, str]) -> JsonObject:
        """post one form-urlencoded body; every read endpoint takes one."""

        return self._request_json("POST", self._action_url(action), data=data)

    def _listing(self, reference: str) -> JsonObject:
        return self._post("ShoppingBody", {"reference": reference})

    def _search(self, query: str) -> JsonObject:
        self._search_pending = True
        try:
            return self._post("search", {"strQuery": query})
        finally:
            self._search_pending = False

    def _badged(
        self, characteristic: Characteristic, category_id: str | int | None
    ) -> tuple[BonareaProduct, ...]:
        products = (
            self.iter_catalog()
            if category_id is None
            else iter(self.get_category(category_id).products)
        )
        badged: dict[str, BonareaProduct] = {}
        for product in products:
            if product.has(characteristic):
                badged.setdefault(product.id, product)
        return tuple(badged.values())


def _is_runtime_error(response: httpx.Response) -> bool:
    try:
        body = response.text
    except httpx.ResponseNotRead:
        # a streamed download is never an asp.net page
        return False
    return RUNTIME_ERROR_MARKER in body.lower()


def _articles(data: JsonObject) -> tuple[BonareaProduct, ...]:
    return parse_articles(data, label="listing")


def _find_category(
    categories: tuple[BonareaCategory, ...], wanted: str
) -> BonareaCategory | None:
    for category in categories:
        if category.id == wanted:
            return category
        found = _find_category(category.children, wanted)
        if found is not None:
            return found
    return None
