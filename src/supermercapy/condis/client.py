"""synchronous condis client."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Iterator
from typing import Any, Self

import httpx

from .._core.capabilities import Capability
from .._core.client import (
    BaseClient,
    ResponseVerdict,
    RetryPolicy,
    validate_identifier,
    validate_postal_code,
)
from .._core.coerce import as_object, as_text
from .._core.exceptions import (
    ConfigurationError,
    InvalidResponseError,
    NotFoundError,
    OutOfCoverageError,
)
from .._core.models import Language
from ._constants import (
    ACTION_HEADER,
    CATEGORY_FACET,
    COMPONENT_ACCEPT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_PICKING_CENTRE,
    EMPATHY_URL,
    HTML_ACCEPT,
    INDEX_LANGUAGE,
    LOCALE,
    MAX_PAGE_SIZE,
    MAX_REDIRECTS,
    MAX_START,
    NOVELTY_FACET,
    PLACEHOLDER_SLUG,
    POSTCODE_ACTION,
    PROMOTION_FACET,
    SALE_FACET,
    SITE_URL,
)
from .models import (
    CondisCategory,
    CondisProduct,
    CondisSearchResult,
    flight_payload,
    flight_value,
    page_url,
    parse_categories,
    parse_product,
    parse_search_result,
    parse_suggestions,
)

# the scripts a page loads, in the order it lists them
_SCRIPT = re.compile(r'src="(/_next/static/chunks/[^"]+\.js)"')
_STATIC_ACCEPT = {"Accept": "*/*"}
_FORBIDDEN = 403
# the most rows one facet value can hand out: the last offset plus a page
_REACHABLE = MAX_START + MAX_PAGE_SIZE


def _validate_centre(centre: str | int) -> str:
    text = validate_identifier(centre, "picking_centre")
    if not text.isascii() or not text.isdigit():
        raise ConfigurationError("picking_centre must be a numeric centre id")
    return text


def _validate_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a non-empty string")
    return value.strip()


def _validate_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit():
        raise ConfigurationError("cursor must be an offset returned by a listing")
    offset = int(cursor)
    if offset > MAX_START:
        raise ConfigurationError(f"cursor must be at most {MAX_START}")
    return offset


def _validate_filters(filters: Iterable[str] | None) -> list[str]:
    if filters is None:
        return []
    if isinstance(filters, str) or not isinstance(filters, Iterable):
        raise ConfigurationError('filters must be a list of "facet:value" strings')
    values = list(filters)
    for value in values:
        if not isinstance(value, str) or ":" not in value or not value.strip():
            raise ConfigurationError('filters must be a list of "facet:value" strings')
    return values


def _action_id(script: str, name: str) -> str | None:
    """return the build-specific id next.js gave the server action ``name``."""

    match = re.search(rf'"([0-9a-f]{{20,}})",[^"]*"{re.escape(name)}"\)', script)
    return match.group(1) if match else None


def _action_result(text: str) -> object:
    """return the value a server action answered with, from its flight lines."""

    for line in text.splitlines():
        if line.startswith("1:"):
            try:
                return json.loads(line[2:])
            except ValueError:
                break
    raise InvalidResponseError("condis answered a server action without a result")


def _payload(html: str, path: str) -> str:
    payload = flight_payload(html)
    if not payload:
        raise InvalidResponseError(f"condis rendered {path} without its data")
    return payload


class Condis(BaseClient):
    """a reusable synchronous client for condis's online shop, condisline.

    two hosts answer. the empathy search index at ``api.empathy.co`` serves
    search, category listings, the novelty and offer facets, and suggestions,
    with no key and no cookie; every request names a picking centre, which
    decides the assortment. the storefront at ``compraonline.condis.es``
    renders product sheets and the category tree on the server, behind an
    anonymous sign-in that every first page request redirects through; the
    client follows it once per session, as a browser does, and never logs in.

    a client is bound to a picking centre, ``718`` by default, which is the
    centre an anonymous visitor gets. :meth:`from_postal_code` resolves
    another through the storefront's own postcode picker.
    """

    store_name = "condis"
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
    supported_languages = frozenset({Language.SPANISH})
    default_page_size = DEFAULT_PAGE_SIZE
    max_page_size = MAX_PAGE_SIZE
    default_min_request_interval = 0.5

    def __init__(
        self,
        picking_centre: str | int = DEFAULT_PICKING_CENTRE,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._centre = _validate_centre(picking_centre)
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
        """bind the picking centre the storefront assigns to ``postal_code``.

        the storefront resolves a postcode with a next.js server action,
        ``fetchPostalCodeById``, whose id changes with every deployment and is
        published only inside the page's scripts. so this reads the home page,
        then its scripts in the order the page lists them until one names the
        action, then calls it. in october 2026 that was eighteen requests: the
        home page through the anonymous sign-in (five), twelve scripts of
        about 1.4 mb together, and the call. a postcode condis does not
        deliver to raises :class:`~supermercapy.OutOfCoverageError`.
        """

        code = validate_postal_code(postal_code)
        client = cls(
            language=language,
            timeout=timeout,
            retry_policy=retry_policy,
            min_request_interval=min_request_interval,
            transport=transport,
            user_agent=user_agent,
        )
        try:
            client._centre = client._resolve_centre(code)
            return client
        except BaseException:
            client.close()
            raise

    @property
    def picking_centre(self) -> str:
        """return the picking centre whose assortment the index answers with."""

        return self._centre

    @property
    def store_id(self) -> str:
        """return the picking centre; the same value as :attr:`picking_centre`."""

        return self._centre

    # ------------------------------------------------------ standard interface

    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        filters: Iterable[str] | None = None,
    ) -> CondisSearchResult:
        """search one page of the index.

        the cursor is the offset of the next row. ``filters`` takes the
        index's own ``"facet:value"`` strings, as its facets name them, such
        as ``"on_sale:true"`` or ``"filterCategory:c07"``, each sent as one
        ``filter`` parameter. one query reaches at most 2,494 rows plus one
        page; a page against that ceiling with rows left is flagged
        ``truncated``.
        """

        text = _validate_text(query, "query")
        return self._listing(
            "search",
            {"query": text},
            query=query,
            page_size=page_size,
            cursor=cursor,
            filters=filters,
        )

    def get_product(self, product_id: str | int) -> CondisProduct:
        """return one product sheet, read from its server-rendered page.

        the slug in a product url is not checked, so this asks for a
        placeholder one and reads the real address from the page. an id the
        shop does not know renders the page without a product, which raises
        :class:`~supermercapy.NotFoundError`.
        """

        identifier = validate_identifier(product_id, "product_id")
        if not identifier.isascii() or not identifier.isalnum():
            raise ConfigurationError("product_id must be a condis product code")
        path = f"/{PLACEHOLDER_SLUG}/p/{identifier}/{LOCALE}"
        html = self._page(path)
        payload = _payload(html, path)
        info = flight_value(payload, "productInformation")
        if not isinstance(info, dict):
            raise NotFoundError(f"condis has no product {identifier}")
        return parse_product(
            info,
            branch=flight_value(payload, "matchedChild"),
            section=flight_value(payload, "parentCategoryName"),
            url=page_url(html),
        )

    def get_categories(self) -> tuple[CondisCategory, ...]:
        """return the category tree, three levels deep.

        the tree is the storefront's own navigation, read from the home page:
        one request of about 250 kb, plus the anonymous sign-in on a client's
        first storefront request.
        """

        tree = parse_categories(
            flight_value(_payload(self._page("/"), "/"), "categoryList")
        )
        if not tree:
            raise InvalidResponseError("condis served no category tree")
        return tree

    def get_category(
        self, category_id: str | int, *, page_size: int | None = None
    ) -> CondisCategory:
        """return one category with its children and its first page of rows.

        ``product_count`` is the number of rows the index holds for the
        category at the bound picking centre; :meth:`get_category_products`
        pages through them. two requests: the tree, then the rows.
        """

        wanted = validate_identifier(category_id, "category_id")
        node = self._find(self.get_categories(), wanted)
        if node is None:
            raise NotFoundError(f"condis has no category {wanted}")
        page = self.get_category_products(wanted, page_size=page_size)
        return dataclasses.replace(
            node, products=page.products, product_count=page.total_hits
        )

    def iter_catalog(self) -> Iterator[CondisProduct]:
        """yield every row of every top-level category at the picking centre.

        each top-level category is walked five hundred rows at a time; the
        largest held 1,541 rows in october 2026, under the index's offset
        ceiling. a category too large to walk to its end is walked through
        its children instead.
        """

        for root in self.get_categories():
            yield from self._walk_category(root)

    def get_new_arrivals(self) -> tuple[CondisProduct, ...]:
        """return every row the index flags ``is_novelty``.

        one request for the 57 it held in october 2026.
        """

        return tuple(self._walk(NOVELTY_FACET, "true"))

    def get_offers(self) -> tuple[CondisProduct, ...]:
        """return every row on sale or on promotion, once each.

        ``on_sale`` marks a reduced price and ``on_promotion`` a multibuy
        such as "llévate 6 y paga 5"; the two facets are read one after the
        other, five hundred rows a request, about three requests in october
        2026.
        """

        found: dict[str, CondisProduct] = {}
        for facet in (SALE_FACET, PROMOTION_FACET):
            for product in self._walk(facet, "true"):
                found.setdefault(product.id, product)
        return tuple(found.values())

    # -------------------------------------------------------------- extensions

    def get_category_products(
        self,
        category_id: str | int,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> CondisSearchResult:
        """return one page of a category's rows, any level of the tree.

        the id is the path-shaped one the tree uses, such as
        ``"c07__cat00210003"``; an unknown id answers an empty page.
        """

        wanted = validate_identifier(category_id, "category_id")
        return self._browse(CATEGORY_FACET, wanted, page_size=page_size, cursor=cursor)

    def suggest(self, term: str) -> tuple[str, ...]:
        """return the index's query suggestions for a partial term."""

        text = _validate_text(term, "term")
        data = self._request_json(
            "GET",
            f"{EMPATHY_URL}/empathize",
            params={"query": text, "lang": INDEX_LANGUAGE, "store": self._centre},
        )
        return parse_suggestions(data)

    # ---------------------------------------------------------------- the index

    def _listing(
        self,
        endpoint: str,
        parameters: dict[str, Any],
        *,
        query: str,
        page_size: int | None,
        cursor: str | None,
        filters: Iterable[str] | None = None,
    ) -> CondisSearchResult:
        size = self._resolve_page_size(page_size)
        offset = _validate_offset(cursor)
        params: list[tuple[str, Any]] = [
            ("lang", INDEX_LANGUAGE),
            ("start", offset),
            ("rows", size),
            *parameters.items(),
            ("store", self._centre),
        ]
        params.extend(("filter", value) for value in _validate_filters(filters))
        data = self._request_json("GET", f"{EMPATHY_URL}/{endpoint}", params=params)
        return parse_search_result(
            data, query=query, offset=offset, page_size=size, max_start=MAX_START
        )

    def _browse(
        self,
        field: str,
        value: str,
        *,
        page_size: int | None,
        cursor: str | None,
    ) -> CondisSearchResult:
        return self._listing(
            "browse",
            {"browseField": field, "browseValue": value},
            query="",
            page_size=page_size,
            cursor=cursor,
        )

    def _walk(
        self, field: str, value: str, first: CondisSearchResult | None = None
    ) -> Iterator[CondisProduct]:
        """yield every row a facet value holds, from ``first`` if it is read."""

        page = (
            first
            if first is not None
            else self._browse(field, value, page_size=MAX_PAGE_SIZE, cursor=None)
        )
        while True:
            yield from page.products
            if page.next_cursor is None:
                return
            page = self._browse(
                field, value, page_size=MAX_PAGE_SIZE, cursor=page.next_cursor
            )

    def _walk_category(self, node: CondisCategory) -> Iterator[CondisProduct]:
        """walk one category, or its children when it holds too many rows."""

        first = self._browse(
            CATEGORY_FACET, node.id, page_size=MAX_PAGE_SIZE, cursor=None
        )
        if (
            first.total_hits is not None
            and first.total_hits > _REACHABLE
            and node.children
        ):
            for child in node.children:
                yield from self._walk_category(child)
            return
        yield from self._walk(CATEGORY_FACET, node.id, first)

    @staticmethod
    def _find(nodes: tuple[CondisCategory, ...], wanted: str) -> CondisCategory | None:
        for node in nodes:
            if node.id == wanted:
                return node
            found = Condis._find(node.children, wanted)
            if found is not None:
                return found
        return None

    # ----------------------------------------------------------- the storefront

    def _page(self, path: str) -> str:
        """return one storefront page, through the anonymous sign-in if need be.

        a request without a session is redirected to the anonymous sign-in,
        which sets the session cookies and redirects back; the client follows
        those hops itself, at most six, as a browser would.
        """

        url = f"{SITE_URL}{path}"
        for _ in range(MAX_REDIRECTS):
            response = self._request("GET", url, headers={"Accept": HTML_ACCEPT})
            if not response.is_redirect:
                return response.text
            location = response.headers.get("Location")
            if not location:
                raise InvalidResponseError("condis redirected to nowhere")
            url = str(response.url.join(location))
        raise InvalidResponseError(
            f"condis redirected more than {MAX_REDIRECTS} times for {path}"
        )

    def _resolve_centre(self, postal_code: str) -> str:
        home = self._page("/")
        action = None
        for script in dict.fromkeys(_SCRIPT.findall(home)):
            body = self._request_text(
                "GET", f"{SITE_URL}{script}", headers=_STATIC_ACCEPT
            )
            action = _action_id(body, POSTCODE_ACTION)
            if action is not None:
                break
        if action is None:
            raise InvalidResponseError(
                f"condis no longer publishes its {POSTCODE_ACTION} action"
            )
        response = self._request(
            "POST",
            f"{SITE_URL}/",
            headers={
                ACTION_HEADER: action,
                "Accept": COMPONENT_ACCEPT,
                "Content-Type": "text/plain;charset=UTF-8",
            },
            content=json.dumps([postal_code]),
        )
        if response.is_redirect:
            raise InvalidResponseError("condis redirected the postcode lookup")
        result = as_object(_action_result(response.text))
        found = as_object(result.get("postalCode"))
        centre = as_text(found.get("picking_centre_id"))
        if not centre:
            raise OutOfCoverageError(f"condis does not deliver to {postal_code}")
        if not centre.isascii() or not centre.isdigit():
            raise InvalidResponseError(
                f"condis assigned {postal_code} the unusable centre {centre!r}"
            )
        return centre

    # ---------------------------------------------------------- transport hooks

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        """tell the edges' refusals apart from the shop's own answers.

        neither host refused this client in october 2026, and neither sends
        a 403 of its own, so this reads the edges' standard answers:
        cloudflare, in front of the index, marks a challenge with
        ``cf-mitigated: challenge``, and a 403 from it or from cloudfront in
        front of the storefront is a block.
        """

        if response.headers.get("cf-mitigated", "").lower() == "challenge":
            return ResponseVerdict.CHALLENGED
        if response.status_code == _FORBIDDEN:
            return ResponseVerdict.BLOCKED
        return super()._classify(response)
