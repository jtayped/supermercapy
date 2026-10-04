"""the apache tapestry storefront eroski and caprabo share.

both chains run one tapestry 5 build with the same paths and the same category
ids, and neither publishes a json api: every page is server-rendered html. what
the parsers read is what the browser reads:

- the category tree is the navigation menu every full page carries, about
  780 kb of nested links whose paths spell each node's ancestry, such as
  ``/es/supermercado/2059698-frescos/2059699-frutas/``.
- listings come from the zone fragment the storefront's own infinite scroll
  asks for, ``…:loadpage?…&t:zoneid=productListZone&pageNumber=N``: json
  holding twenty or so product tiles as html, about 230 kb a page against a
  megabyte for the full page, and an empty page once the listing ends.
- every tile carries a ga4 ``data-metrics`` json attribute with the id, name,
  brand, price and category ids; the visible markup adds the unit price, the
  struck-through price and the offer badges, and a script block adds the
  purchase limits.
- a product page adds the photos, the breadcrumb and the characteristics:
  ingredients, the nutrition table, the manufacturer, storage, alcohol.

nothing here is public. each store subclasses :class:`TapestryClient` with its
own host, its own model subclasses and its own languages.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, ClassVar, Generic, TypeVar, cast
from urllib.parse import quote, urlsplit

import httpx

from .._core.capabilities import Capability
from .._core.client import BaseClient, ResponseVerdict, RetryPolicy, validate_identifier
from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_decimal,
    as_euro_text,
    as_integer,
    as_items,
    as_object,
    as_text,
)
from .._core.exceptions import ConfigurationError, InvalidResponseError, NotFoundError
from .._core.html import normalise_whitespace
from .._core.models import (
    Availability,
    Category,
    Language,
    Nutrition,
    NutritionValue,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
)
from .._core.units import UnitReader

PAGE_SIZE = 20
"""the tiles one listing page holds; the storefront takes no size parameter."""

DEFAULT_MIN_REQUEST_INTERVAL = 1.0
"""one second between request starts: a catalog walk is hundreds of pages."""

ZONE_ID = "productListZone"
MENU_PATH = "/{language}/"
SEARCH_FRAGMENT_PATH = "/{language}/search/results:loadpage"
LISTING_FRAGMENT_PATH = "/{language}/supermarket:loadpage"
# the slug of a product url is not read: any text after the id answers the
# same page, and an id with no slug at all is a 404
PRODUCT_PATH = "/{language}/productdetail/{product_id}-producto/"

FRAGMENT_HEADERS: Mapping[str, str] = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}
PAGE_HEADERS: Mapping[str, str] = {"Accept": "text/html,application/xhtml+xml"}

# the edge refuses a handful of user agents (curl, python-requests, an empty
# one) with a bare 134-byte "403 Forbidden"; the library's own is accepted
_FORBIDDEN = 403
_REDIRECTS = frozenset({301, 302, 303, 307, 308})

_ADD_COMPONENT = "common/button/productListItemAddComponent:init"
_TRACKING = "common/tracking:init"
_FINISHED = "pages/supermarket:finishPagination"
_PAGE_INIT = '"t5/core/pageinit"'

_CATEGORY_PATH = re.compile(r"^/[a-z]{2}/supermercado/((?:\d+-[^/]+/)+)$")
_PRODUCT_PATH = re.compile(r"/productdetail/([^/]+?)-([^/]*)/?$")
_TILE_START = re.compile(
    r"""<div\s+class=["'](?P<classes>[^"']*\bproduct-item-lineal\b[^"']*)["']"""
)
_UNIT_PRICE = re.compile(r"^(?P<unit>.+?)\s+A\s+(?P<amount>\d[\d.,]*)\s*€?$")
_OFFER_CODE = re.compile(r"^o_(?P<code>(?P<quantity>\d+)x\d+x\d+)$")
_NUMBER = re.compile(r"^(?P<number>-?\d+(?:[.,]\d+)?)\s*(?P<unit>.*)$")
_PERCENT = re.compile(r"-?\s*(\d+(?:[.,]\d+)?)")
_HUNDRED = re.compile(r"^100\b")
_CURSOR = re.compile(r"(?P<page>[0-9]+)(?::(?P<skip>[0-9]+))?")

# the unit text is spanish whatever the language: "1 LITRO A 0,97 €". every
# word but the roll is in the shared vocabulary, and a roll is one piece
UNITS = UnitReader({"rollo": "ud", "rollos": "ud"})


# ---------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True, kw_only=True)
class TapestryProduct(Product):
    """one product, from a listing tile or from its product page.

    a tile fills the summary fields; the page adds the photos, the category
    path, the nutrition and the characteristics below.
    """

    shop_id: str | None = None
    is_marketplace: bool = False
    is_lowered_price: bool = False
    manufacturer: str | None = None
    manufacturer_address: str | None = None
    alcohol_percentage: Decimal | None = None
    features: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class TapestryCategory(Category):
    """one node of the navigation menu, addressed by its slug path."""

    path: str = ""
    url: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TapestrySearchResult(SearchResult):
    """one listing page; the cursor is the next page number."""

    page: int = 0


ProductT = TypeVar("ProductT", bound=TapestryProduct)
CategoryT = TypeVar("CategoryT", bound=TapestryCategory)
ResultT = TypeVar("ResultT", bound=TapestrySearchResult)


# ---------------------------------------------------------------------- markup


@dataclass
class _Capture:
    """text collected from one element until its end tag closes it."""

    tag: str
    key: str
    depth: int = 0
    parts: list[str] = field(default_factory=list)


@dataclass
class _Feature:
    """one characteristics box of a product page."""

    classes: tuple[str, ...]
    title: str = ""
    lines: list[tuple[str, bool]] = field(default_factory=list)
    rows: list[tuple[str, str]] = field(default_factory=list)


class _Markup(HTMLParser):
    """read one tile, or one product page, keeping the first of everything.

    the parser is tolerant by construction: an element it never sees closed
    simply keeps collecting, and nothing here raises on malformed markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metrics: list[str] = []
        self.links: list[str] = []
        self.images: list[str] = []
        self.texts: dict[str, str] = {}
        self.offer_classes: list[str] = []
        self.offer_parts: list[str] = []
        self.weighed = False
        self.thumbnails: list[tuple[str, str | None]] = []
        self.breadcrumb: list[tuple[str, str]] = []
        self.features: list[_Feature] = []
        self._captures: list[_Capture] = []
        # open div scopes and how deep inside each one the parser is
        self._scopes: dict[str, int] = {}
        self._read_scopes: set[str] = set()
        self._offer_depth: int | None = None
        self._link: tuple[str, list[str]] | None = None
        self._label = False
        self._row: list[str] | None = None
        self._value: list[str] | None = None

    # ---------------------------------------------------------------- events

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = tuple(attributes.get("class", "").split())
        for capture in self._captures:
            if capture.tag == tag:
                capture.depth += 1
        if tag == "div":
            self._enter_div(classes)
        elif tag == "a":
            self._anchor(attributes, classes)
        elif tag == "img":
            self._image(attributes, classes)
        elif tag == "span":
            self._span(classes)
        elif tag == "p":
            self._paragraph(classes)
        elif tag == "h1" and "description-title" in classes:
            self._captures.append(_Capture(tag="h1", key="name"))
        elif tag == "li" and "feature" in self._scopes:
            self._row = []
        elif tag == "strong" and "feature" in self._scopes:
            self._label = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # a self-closed element opens nothing, so it must not deepen a capture
        if tag == "img":
            attributes = {name: value or "" for name, value in attrs}
            self._image(attributes, tuple(attributes.get("class", "").split()))

    def handle_endtag(self, tag: str) -> None:
        for capture in list(self._captures):
            if capture.tag != tag:
                continue
            if capture.depth:
                capture.depth -= 1
                continue
            self._captures.remove(capture)
            self._finish(capture)
        if tag == "div":
            self._leave_div()
        elif tag == "a" and self._link is not None:
            href, parts = self._link
            self.breadcrumb.append((href, normalise_whitespace("".join(parts))))
            self._link = None
        elif tag == "span" and self._offer_depth is not None:
            self._offer_depth = self._offer_depth - 1 if self._offer_depth else None
        elif tag == "li" and self._row is not None:
            self._close_row()

    def handle_data(self, data: str) -> None:
        for capture in self._captures:
            capture.parts.append(data)
        if self._offer_depth is not None and data.strip():
            self.offer_parts.append(data.strip())
        if self._link is not None:
            self._link[1].append(data)
        if self._value is not None:
            self._value.append(data)
        elif self._row is not None:
            self._row.append(data)

    # -------------------------------------------------------------- elements

    def _enter_div(self, classes: tuple[str, ...]) -> None:
        for key in self._scopes:
            self._scopes[key] += 1
        if "product-offer" in classes and not self.offer_classes:
            self.offer_classes.extend(classes)
        for key, marker in (
            ("thumbnails", "product-thumbnails"),
            ("breadcrumb", "m__breadcrumb__path"),
        ):
            if marker in classes and key not in {*self._scopes, *self._read_scopes}:
                self._scopes[key] = 0
        if "feature" in classes and "feature" not in self._scopes:
            self.features.append(_Feature(classes=classes))
            self._scopes["feature"] = 0

    def _leave_div(self) -> None:
        for key in list(self._scopes):
            if self._scopes[key]:
                self._scopes[key] -= 1
                continue
            del self._scopes[key]
            self._read_scopes.add(key)

    def _anchor(self, attributes: dict[str, str], classes: tuple[str, ...]) -> None:
        href = attributes.get("href", "")
        if "product-title-link" in classes:
            if attributes.get("data-metrics"):
                self.metrics.append(attributes["data-metrics"])
            if href:
                self.links.append(href)
        elif "breadcrumb" in self._scopes and href:
            self._link = (href, [])

    def _image(self, attributes: dict[str, str], classes: tuple[str, ...]) -> None:
        source = attributes.get("src", "")
        if "weight-icon" in source:
            self.weighed = True
        if not source:
            return
        if "thumbnails" in self._scopes:
            self.thumbnails.append((source, attributes.get("data-bigimage") or None))
        elif "product-img" in classes or attributes.get("id") == "main-product-image":
            self.images.append(source)

    def _span(self, classes: tuple[str, ...]) -> None:
        if self._offer_depth is not None:
            self._offer_depth += 1
        elif "partner-price" in classes:
            self.offer_classes.extend(classes)
            self._offer_depth = 0
        elif "price-offer-before" in classes or "offer-before" in classes:
            self._captures.append(_Capture(tag="span", key="before"))
        elif "price-offer-now" in classes or "offer-now" in classes:
            self._captures.append(_Capture(tag="span", key="now"))
        elif "title" in classes and "feature" in self._scopes:
            self._captures.append(_Capture(tag="span", key="feature-title"))
        elif self._row is not None and self._value is None:
            self._value = []

    def _paragraph(self, classes: tuple[str, ...]) -> None:
        if "quantity-text" in classes:
            self._captures.append(_Capture(tag="p", key="unit"))
        elif "text" in classes and "feature" in self._scopes:
            self._label = False
            self._captures.append(_Capture(tag="p", key="feature-line"))

    # -------------------------------------------------------------- captures

    def _finish(self, capture: _Capture) -> None:
        # a feature capture only opens inside a feature box, so one exists
        text = normalise_whitespace("".join(capture.parts))
        if capture.key == "feature-title":
            feature = self.features[-1]
            feature.title = feature.title or text
        elif capture.key == "feature-line":
            self.features[-1].lines.append((text, self._label))
            self._label = False
        else:
            self.texts.setdefault(capture.key, text)

    def _close_row(self) -> None:
        name = normalise_whitespace("".join(self._row or []))
        value = normalise_whitespace("".join(self._value or []))
        if name:
            self.features[-1].rows.append((name, value))
        self._row = None
        self._value = None


def _read(html: str) -> _Markup:
    markup = _Markup()
    markup.feed(html)
    markup.close()
    return markup


def tile_markup(content: str) -> list[_Markup]:
    """split listing html into its product tiles and read each one.

    a tile starts at a ``product-item-lineal`` div and runs to the next one;
    the advertising slots share the class and are dropped, because the
    storefront fills them in the browser and they arrive empty.
    """

    if not isinstance(content, str) or not content:
        return []
    starts = list(_TILE_START.finditer(content))
    tiles: list[_Markup] = []
    for index, match in enumerate(starts):
        if "criteoItem" in match.group("classes").split():
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(content)
        tiles.append(_read(content[match.start() : end]))
    return tiles


# ----------------------------------------------------------------------- inits


def page_inits(html: str) -> list[object]:
    """return the component initialisations a full page hands tapestry.

    a page ends with ``require(["t5/core/pageinit"], function(pi) {
    pi([libraries], [inits]); })``; the second array is read with
    :meth:`json.JSONDecoder.raw_decode`. a page without it gives ``[]``.
    """

    if not isinstance(html, str):
        return []
    index = html.find(_PAGE_INIT)
    if index < 0:
        return []
    call = html.find("pi(", index + len(_PAGE_INIT))
    if call < 0:
        return []
    decoder = json.JSONDecoder()
    try:
        _, position = decoder.raw_decode(html, _skip(html, call + 3))
        position = _skip(html, position)
        if html[position : position + 1] != ",":
            return []
        inits, _ = decoder.raw_decode(html, _skip(html, position + 1))
    except ValueError:
        return []
    return inits if isinstance(inits, list) else []


def _skip(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _init_arguments(inits: list[object], module: str) -> list[list[object]]:
    return [
        item for item in inits if isinstance(item, list) and item and item[0] == module
    ]


def _purchase_options(inits: list[object]) -> dict[str, JsonObject]:
    """map each product id to the options its add-to-cart button was built with."""

    options: dict[str, JsonObject] = {}
    for arguments in _init_arguments(inits, _ADD_COMPONENT):
        settings = as_object(arguments[1] if len(arguments) > 1 else None)
        reference = as_text(settings.get("productRef"))
        if reference:
            options.setdefault(reference, settings)
    return options


def _tracked_item(inits: list[object]) -> JsonObject:
    """return the item of the page's ``view_item`` analytics event, if any."""

    for arguments in _init_arguments(inits, _TRACKING):
        raw = as_text(arguments[1] if len(arguments) > 1 else None)
        if raw is None:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        payload = as_object(event)
        if payload.get("event") != "view_item":
            continue
        items = as_items(as_object(payload.get("ecommerce")).get("items"))
        if items:
            return as_object(items[0])
    return {}


def _is_finished(inits: list[object]) -> bool:
    return any(
        item == _FINISHED or (isinstance(item, list) and item[:1] == [_FINISHED])
        for item in inits
    )


# --------------------------------------------------------------------- helpers


def absolute_url(site_url: str, value: str | None) -> str | None:
    """return ``value`` as an absolute url on ``site_url``, tidied.

    the storefront writes ``https://host:443/es/…`` and ``https://host//images/…``;
    both answer without the explicit port and the doubled slash, so neither is
    kept.
    """

    if not value or not value.strip():
        return None
    text = value.strip()
    if text.startswith("//"):
        text = f"https:{text}"
    elif text.startswith("/"):
        text = f"{site_url}{text}"
    parts = urlsplit(text)
    host = parts.netloc[:-4] if parts.netloc.endswith(":443") else parts.netloc
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{host}{path}{query}"


def category_path(url: str | None) -> str | None:
    """return the ``2059698-frescos/2059699-frutas`` path of a category url."""

    if not url:
        return None
    match = _CATEGORY_PATH.match(urlsplit(url).path)
    return match.group(1).rstrip("/") if match else None


def _segment_id(segment: str) -> str:
    return segment.split("-", 1)[0]


def _segment_slug(segment: str) -> str | None:
    _, _, slug = segment.partition("-")
    return slug or None


def _photo(site_url: str, value: str | None, kind: str) -> Photo | None:
    url = absolute_url(site_url, value)
    return None if url is None else Photo(url=url, kind=kind)


def _metrics(markup: _Markup) -> JsonObject:
    for raw in markup.metrics:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        items = as_items(as_object(as_object(event).get("ecommerce")).get("items"))
        if items:
            return as_object(items[0])
    return {}


def _text(value: object) -> str | None:
    text = as_text(value)
    if text is None:
        return None
    text = normalise_whitespace(text)
    return text or None


def _identifier(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text(value)


def _category_ids(item: JsonObject) -> tuple[str, ...]:
    keys = ("item_category", *(f"item_category{index}" for index in range(2, 6)))
    return tuple(
        identifier for key in keys if (identifier := _identifier(item.get(key)))
    )


def _unit_price(text: str | None) -> tuple[Decimal | None, str | None]:
    if not text:
        return None, None
    match = _UNIT_PRICE.match(text)
    if match is None:
        return None, None
    return as_euro_text(match.group("amount")), match.group("unit")


def _percentage(text: str) -> Decimal | None:
    match = _PERCENT.search(text)
    if match is None:
        return None
    try:
        return Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:  # pragma: no cover - the regex prevents this
        return None


def _offer(markup: _Markup) -> tuple[tuple[Promotion, ...], Decimal | None, bool]:
    """read the badge beside the price: a multi-buy, a percentage, a price drop."""

    classes = set(markup.offer_classes)
    lowered = "loweredPrice" in classes
    text = re.sub(r"\s+%", "%", " ".join(markup.offer_parts)).strip()
    if "product-offer-only-percent" in classes:
        return (), _percentage(text), lowered
    if not text:
        return (), None, lowered
    code: str | None = None
    quantity: int | None = None
    for name in sorted(classes):
        match = _OFFER_CODE.match(name)
        if match is not None:
            code = match.group("code")
            quantity = int(match.group("quantity"))
            break
    promotion = Promotion(kind=code, description=text, requires_quantity=quantity)
    return (promotion,), None, lowered


def _price(item: JsonObject, markup: _Markup) -> Price:
    # the visible price is what the shopper pays and always has two decimals;
    # the analytics figure is a float that drops trailing zeros
    now = as_euro_text(markup.texts.get("now"))
    previous = as_euro_text(markup.texts.get("before"))
    unit_text = markup.texts.get("unit") or None
    unit_price, unit = _unit_price(unit_text)
    _, percentage, _ = _offer(markup)
    return Price(
        amount=now if now is not None else as_decimal(item.get("price")),
        previous=previous,
        unit_price=unit_price,
        unit_price_unit=unit,
        unit_price_text=unit_text,
        reference=UNITS.unit_price(unit_price, unit),
        is_discounted=previous is not None or percentage is not None,
        discount_percentage=percentage,
    )


def _availability(options: JsonObject) -> Availability:
    if not options:
        return Availability()
    minimum = as_integer(options.get("minGranel"))
    increment = as_integer(options.get("productUnitsPerPack"))
    maximum = as_integer(options.get("maximumQuantity"))
    return Availability(
        max_quantity=None if maximum is None else Decimal(maximum),
        min_quantity=None if minimum is None else Decimal(minimum),
        increment=None if increment is None else Decimal(increment),
    )


def _summary_fields(
    item: JsonObject,
    markup: _Markup,
    options: JsonObject,
    *,
    site_url: str,
    fallback_id: str | None = None,
) -> dict[str, Any] | None:
    """return the keyword arguments a tile or a page gives every product."""

    identifier = _identifier(item.get("item_id")) or fallback_id
    name = _text(item.get("item_name")) or markup.texts.get("name") or None
    if identifier is None or name is None:
        return None
    url = absolute_url(site_url, markup.links[0] if markup.links else None)
    slug_match = _PRODUCT_PATH.search(urlsplit(url).path) if url else None
    image = markup.images[0] if markup.images else None
    promotions, _, lowered = _offer(markup)
    weighed = as_boolean(options.get("isWeightOptionsAvailable")) or markup.weighed
    return {
        "id": identifier,
        "name": name,
        "brand": _text(item.get("item_brand")),
        "slug": (slug_match.group(2) or None) if slug_match else None,
        "url": url,
        "thumbnail": _photo(site_url, image, "thumbnail"),
        "price": _price(item, markup),
        "availability": _availability(options),
        "category_ids": _category_ids(item),
        "promotions": promotions,
        "is_variable_weight": weighed,
        "is_lowered_price": lowered,
        "is_marketplace": as_boolean(options.get("isMarketplace")) is True,
        "shop_id": _identifier(options.get("shopRef")),
    }


# ---------------------------------------------------------------------- parsers


def parse_listing(
    data: JsonObject,
    *,
    product_type: type[ProductT],
    result_type: type[ResultT],
    site_url: str,
    query: str,
    page: int,
) -> ResultT:
    """read one zone fragment into a page of products.

    tiles without an id or a name are skipped rather than raised on: the
    advertising slots share the tile markup. the cursor stays set while a page
    holds tiles and the storefront has not said the listing is over, so the
    walk ends on the first empty page.
    """

    content = as_text(data.get("content")) or ""
    inits = as_items(as_object(data.get("_tapestry")).get("inits"))
    options = _purchase_options(inits)
    products: list[ProductT] = []
    for markup in tile_markup(content):
        item = _metrics(markup)
        identifier = _identifier(item.get("item_id")) or ""
        fields = _summary_fields(
            item, markup, options.get(identifier, {}), site_url=site_url
        )
        if fields is not None:
            products.append(product_type(**fields))
    more = bool(products) and not _is_finished(inits)
    return result_type(
        query=query,
        products=tuple(products),
        page_size=PAGE_SIZE,
        next_cursor=str(page + 1) if more else None,
        page=page,
    )


def _nutrition(features: list[_Feature]) -> Nutrition | None:
    ingredients: str | None = None
    values: list[NutritionValue] = []
    per: str | None = None
    for feature in features:
        if "feature-text-ingredients" in feature.classes:
            ingredients = _joined(feature) or ingredients
        elif "feature-list" in feature.classes and feature.rows:
            per, values = _nutrition_rows(feature.rows)
    if ingredients is None and not values:
        return None
    return Nutrition(ingredients=ingredients, values=tuple(values), per=per)


def _nutrition_rows(
    rows: list[tuple[str, str]],
) -> tuple[str | None, list[NutritionValue]]:
    """split the table into its reference amount and its rows.

    the first row states what the figures refer to ("100 mililitros"); rows
    against a hundred grams or millilitres go in ``per_100``, anything else in
    ``per_serving``.
    """

    (_, per), *rest = rows
    per_hundred = bool(_HUNDRED.match(per))
    values: list[NutritionValue] = []
    for name, text in rest:
        match = _NUMBER.match(text)
        number = match.group("number") if match else (text or None)
        unit = (match.group("unit") or None) if match else None
        values.append(
            NutritionValue(
                name=name,
                per_100=number if per_hundred else None,
                per_serving=None if per_hundred else number,
                unit=unit,
            )
        )
    return per or None, values


def _joined(feature: _Feature) -> str | None:
    lines = [text for text, _ in feature.lines if text]
    return "\n".join(lines) or None


def _feature_text(feature: _Feature) -> str:
    if feature.rows:
        return "\n".join(f"{name} {value}".strip() for name, value in feature.rows)
    return _joined(feature) or ""


def _feature(features: list[_Feature], name: str) -> _Feature | None:
    return next((item for item in features if name in item.classes), None)


def _company(features: list[_Feature]) -> tuple[str | None, str | None]:
    """return the manufacturer's name and address, the box's two values.

    the box alternates a bold label ("Nombre", "Dirección") with its value,
    and the labels follow the language, so the values are read by position.
    """

    feature = _feature(features, "feature-company")
    if feature is None:
        return None, None
    values = [text for text, label in feature.lines if text and not label]
    return (
        values[0] if values else None,
        values[1] if len(values) > 1 else None,
    )


def _alcohol(features: list[_Feature]) -> Decimal | None:
    feature = _feature(features, "feature-alcoholic")
    if feature is None:
        return None
    text = _joined(feature)
    match = _NUMBER.match(text or "")
    return as_decimal(match.group("number").replace(",", ".")) if match else None


def _photos(markup: _Markup, site_url: str) -> tuple[Photo, ...]:
    """return the large rendition of every image, in gallery order."""

    photos: dict[str, Photo] = {}
    for source, large in markup.thumbnails:
        photo = _photo(site_url, large or source, "large")
        if photo is not None:
            photos.setdefault(photo.url, photo)
    return tuple(photos.values())


def _breadcrumb(
    markup: _Markup, category_type: type[CategoryT], site_url: str
) -> tuple[CategoryT, ...]:
    path: list[CategoryT] = []
    for href, text in markup.breadcrumb:
        url = absolute_url(site_url, href)
        slug_path = category_path(url)
        if slug_path is None:
            continue
        segments = slug_path.split("/")
        path.append(
            category_type(
                id=_segment_id(segments[-1]),
                name=text or segments[-1],
                parent_id=_segment_id(segments[-2]) if len(segments) > 1 else None,
                level=len(segments),
                slug=_segment_slug(segments[-1]),
                path=slug_path,
                url=url,
            )
        )
    return tuple(path)


def parse_product(
    html: str,
    *,
    product_type: type[ProductT],
    category_type: type[CategoryT],
    site_url: str,
    product_id: str,
    url: str,
) -> ProductT:
    """read a full product page.

    the analytics event the page fires names the product, its brand, its price
    and its category ids; the markup adds everything else. when the event is
    missing, the heading and the requested id stand in for it. the page names
    no canonical url, so ``url`` is the one that was asked for and the slug
    stays unknown.
    """

    inits = page_inits(html)
    item = _tracked_item(inits)
    identifier = _identifier(item.get("item_id")) or product_id
    options = _purchase_options(inits).get(identifier, {})
    markup = _read(html)
    fields = _summary_fields(
        item, markup, options, site_url=site_url, fallback_id=product_id
    )
    if fields is None:
        raise InvalidResponseError(f"product page {product_id} carries no product")
    fields["url"] = fields["url"] or url
    photos = _photos(markup, site_url)
    if fields["thumbnail"] is None and markup.thumbnails:
        fields["thumbnail"] = _photo(site_url, markup.thumbnails[0][0], "thumbnail")
    path = _breadcrumb(markup, category_type, site_url)
    if not fields["category_ids"]:
        fields["category_ids"] = tuple(node.id for node in path)
    manufacturer, address = _company(markup.features)
    preservation = _feature(markup.features, "feature-text-preservation")
    return product_type(
        **fields,
        photos=photos,
        category_path=path,
        nutrition=_nutrition(markup.features),
        storage=None if preservation is None else _joined(preservation),
        manufacturer=manufacturer,
        manufacturer_address=address,
        alcohol_percentage=_alcohol(markup.features),
        features=tuple(
            (feature.title, _feature_text(feature))
            for feature in markup.features
            if feature.title
        ),
    )


class _MenuLinks(HTMLParser):
    """collect every category link of a page with its first non-empty text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.names: dict[str, str] = {}
        self.order: list[str] = []
        self.urls: dict[str, str] = {}
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        self._close()
        href = dict(attrs).get("href") or ""
        if href:
            self._href = href
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._close()

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def close(self) -> None:
        super().close()
        self._close()

    def _close(self) -> None:
        href, self._href = self._href, None
        if href is None:
            return
        path = category_path(href)
        if path is None:
            return
        if path not in self.urls:
            self.order.append(path)
            self.urls[path] = href
        text = normalise_whitespace("".join(self._parts))
        if text and path not in self.names:
            self.names[path] = text


def parse_menu(
    html: str, *, category_type: type[CategoryT], site_url: str
) -> tuple[CategoryT, ...]:
    """build the category tree from the category links of one full page.

    a link's path spells the node's ancestry, so the tree is rebuilt from the
    paths rather than from the nesting of the menu markup. the first text a
    path is linked with names it: the menu names a node before its "see all"
    entry repeats the path. a node listed under two parents appears under
    both, with the same id and a different path, exactly as the menu shows it.
    """

    links = _MenuLinks()
    links.feed(html if isinstance(html, str) else "")
    links.close()
    children: dict[str | None, list[str]] = {}
    known = set(links.order)
    for path in links.order:
        parent = path.rsplit("/", 1)[0] if "/" in path else None
        if parent is not None and parent not in known:
            continue
        children.setdefault(parent, []).append(path)

    def build(path: str) -> CategoryT:
        segments = path.split("/")
        return category_type(
            id=_segment_id(segments[-1]),
            name=links.names.get(path) or segments[-1],
            parent_id=_segment_id(segments[-2]) if len(segments) > 1 else None,
            level=len(segments),
            slug=_segment_slug(segments[-1]),
            path=path,
            url=absolute_url(site_url, links.urls[path]),
            children=tuple(build(child) for child in children.get(path, ())),
        )

    return tuple(build(path) for path in children.get(None, ()))


def index_categories(
    roots: tuple[CategoryT, ...],
) -> dict[str, CategoryT]:
    """map every id in a tree to its first node, depth first."""

    found: dict[str, CategoryT] = {}
    pending: list[Category] = list(reversed(roots))
    while pending:
        node = pending.pop()
        found.setdefault(node.id, cast("CategoryT", node))
        pending.extend(reversed(node.children))
    return found


# ---------------------------------------------------------------------- client


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise ConfigurationError("query must be a string")
    if not query.strip():
        # a blank query is answered with the storefront's general error page
        raise ConfigurationError("query must not be blank")
    return query.strip()


def _validate_cursor(cursor: str | None) -> tuple[int, int]:
    """return the storefront page a cursor names and the rows already read."""

    if cursor is None:
        return 0, 0
    match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise ConfigurationError("cursor must be a cursor returned by a listing")
    return int(match.group("page")), int(match.group("skip") or 0)


def _sliced(result: ResultT, *, page: int, skip: int, size: int | None) -> ResultT:
    """cut ``size`` rows out of one storefront page, starting at ``skip``.

    the storefront serves pages of about twenty and takes no size, so a
    smaller page is a slice of one, and the cursor names the storefront page
    and the rows of it already returned: ``"3"`` or ``"3:5"``.
    """

    rows = result.products
    end = len(rows) if size is None else skip + size
    if end < len(rows):
        following: str | None = f"{page}:{end}"
    else:
        following = result.next_cursor
    return dataclasses.replace(
        result,
        products=rows[skip:end],
        page_size=PAGE_SIZE if size is None else size,
        next_cursor=following,
    )


def _is_not_found(location: str) -> bool:
    path = urlsplit(location).path
    return "/error/404" in path or "/error/error404" in path


class TapestryClient(BaseClient, Generic[ProductT, CategoryT, ResultT]):
    """the request flow of a tapestry storefront; each store adds its host.

    the anonymous session is pinned to one shop the storefront picks, which
    sets the prices and the regional assortment, and choosing another needs an
    account: the delivery pages redirect to a login. so nothing here binds,
    and ``store_id`` stays ``None``; each product carries the ``shop_id`` that
    priced it.
    """

    _site_url: ClassVar[str] = ""
    _product_type: type[ProductT]
    _category_type: type[CategoryT]
    _result_type: type[ResultT]

    capabilities = frozenset(
        {Capability.CATALOG, Capability.NUTRITION, Capability.PROMOTIONS}
    )
    default_page_size = PAGE_SIZE
    max_page_size = PAGE_SIZE
    default_min_request_interval = DEFAULT_MIN_REQUEST_INTERVAL

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
        self._categories: dict[str, CategoryT] | None = None
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
    ) -> ResultT:
        """search one page of products, twenty by default and at most.

        the storefront serves pages of about twenty and takes no size, so a
        smaller ``page_size`` is cut out of one storefront page, and walking it
        asks for that page again until its rows run out. the cursor names the
        storefront page and the rows already returned. the walk ends on the
        first page that comes back empty, which costs one small extra request
        at the end of :meth:`iter_search`. no total is published.
        """

        text = _validate_query(query)
        page, skip = _validate_cursor(cursor)
        size = self._resolve_page_size(page_size)
        data = self._fragment(
            SEARCH_FRAGMENT_PATH, {"q": text, "t:zoneid": ZONE_ID, "pageNumber": page}
        )
        result = self._listing_result(data, query=query, page=page)
        return _sliced(result, page=page, skip=skip, size=size)

    def get_product(self, product_id: str | int) -> ProductT:
        """return one complete product from its page, 600 to 900 kb of html.

        ids are opaque strings: most are numeric, marketplace ones read
        ``MP103205``. an unknown id redirects to the error page, which raises
        :class:`~supermercapy.NotFoundError`.
        """

        identifier = validate_identifier(product_id, "product_id")
        path = PRODUCT_PATH.format(
            language=self._language.value, product_id=quote(identifier, safe="")
        )
        html = self._page(
            path, missing=f"{self.store_name} has no product {identifier}"
        )
        return parse_product(
            html,
            product_type=self._product_type,
            category_type=self._category_type,
            site_url=self._site_url,
            product_id=identifier,
            url=f"{self._site_url}{path}",
        )

    def get_categories(self) -> tuple[CategoryT, ...]:
        """return the whole category tree, read from the home page's menu.

        one request, 770 kb to a megabyte of html. the tree is also kept on
        the client, so a later :meth:`get_category` needs no second copy.
        """

        html = self._page(MENU_PATH.format(language=self._language.value))
        roots = parse_menu(
            html, category_type=self._category_type, site_url=self._site_url
        )
        if not roots:
            raise InvalidResponseError(f"{self.store_name} served no category menu")
        self._categories = index_categories(roots)
        return roots

    def get_category(self, category_id: str | int) -> CategoryT:
        """return one category with its children and its first listing page.

        the listing needs the category's full slug path, which only the menu
        holds, so a client that has not read the menu yet reads it first: two
        requests, then one per category. an id the menu does not hold raises
        :class:`~supermercapy.NotFoundError`. no product count is published.
        """

        node = self._category(category_id)
        listing = self._listing(node.path, 0)
        return dataclasses.replace(node, products=listing.products)

    def iter_catalog(self) -> Iterator[ProductT]:
        """yield every product by walking each root category's listing.

        one request for the menu, then one per page of about twenty products
        plus one empty page per root. a root's listing covers its whole
        subtree, so a product appears once per root it is filed under.
        """

        roots = self.get_categories()
        for root in roots:
            page = 0
            while True:
                listing = self._listing(root.path, page)
                yield from cast("tuple[ProductT, ...]", listing.products)
                if listing.next_cursor is None:
                    break
                page += 1

    # -------------------------------------------------------------- extensions

    def get_category_products(
        self, category_id: str | int, *, cursor: str | None = None
    ) -> ResultT:
        """return one listing page of a category, its subtree included.

        the cursor works as for search; reading the menu first costs one more
        request when the client has not read it yet.
        """

        page, skip = _validate_cursor(cursor)
        node = self._category(category_id)
        result = self._listing(node.path, page)
        return _sliced(result, page=page, skip=skip, size=None)

    # ---------------------------------------------------------- transport hooks

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        """treat the edge's bare http 403 as a refusal that will not clear."""

        if response.status_code == _FORBIDDEN:
            return ResponseVerdict.BLOCKED
        return super()._classify(response)

    # ---------------------------------------------------------------- internals

    def _category(self, category_id: str | int) -> CategoryT:
        wanted = validate_identifier(category_id, "category_id")
        if self._categories is None:
            self.get_categories()
        node = (self._categories or {}).get(wanted)
        if node is None:
            raise NotFoundError(f"{self.store_name} has no category {wanted}")
        return node

    def _listing(self, path: str, page: int) -> ResultT:
        data = self._fragment(
            LISTING_FRAGMENT_PATH,
            {"t:ac": path, "t:zoneid": ZONE_ID, "pageNumber": page},
        )
        return self._listing_result(data, query="", page=page)

    def _listing_result(self, data: JsonObject, *, query: str, page: int) -> ResultT:
        return parse_listing(
            data,
            product_type=self._product_type,
            result_type=self._result_type,
            site_url=self._site_url,
            query=query,
            page=page,
        )

    def _fragment(self, template: str, params: dict[str, Any]) -> JsonObject:
        """fetch one zone fragment, as the storefront's infinite scroll does.

        an event the storefront cannot serve is not an http error: it answers
        200 with a ``redirectURL`` to its error page instead of content.
        """

        url = f"{self._site_url}{template.format(language=self._language.value)}"
        data = self._request_json("GET", url, params=params, headers=FRAGMENT_HEADERS)
        location = as_text(as_object(data.get("_tapestry")).get("redirectURL"))
        if location is not None:
            if _is_not_found(location):
                raise NotFoundError(f"GET {url} redirected to {location}")
            raise InvalidResponseError(f"GET {url} redirected to {location}")
        return data

    def _page(self, path: str, *, missing: str | None = None) -> str:
        """fetch one full page; a redirect to the error page means not found."""

        url = f"{self._site_url}{path}"
        response = self._request("GET", url, headers=PAGE_HEADERS)
        if response.status_code in _REDIRECTS:
            location = response.headers.get("Location", "")
            if missing is not None and _is_not_found(location):
                raise NotFoundError(missing)
            raise InvalidResponseError(
                f"GET {url} redirected to {location or 'nowhere'}"
            )
        return response.text
