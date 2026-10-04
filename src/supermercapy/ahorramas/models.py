"""ahorramás's models and parsers, built on the shared core models.

the storefront runs salesforce commerce cloud and publishes its products in two
shapes. listings are the html grid its own "more results" button loads, one
tile per product, with the visible prices, the unit price, the promotion
callouts and the purchase limits in data attributes. a product's own record is
the json its product page asks for when a quantity changes, which adds the
photos, the promotion ids and dates, the general attributes and the description.
both normalise into :class:`AhorramasProduct`.

the json also carries ``ingredients``, ``nutritionalValues`` and ``allergens``,
and every product sampled in october 2026 left them empty, so nothing here reads
them and ``NUTRITION`` is not declared.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_decimal,
    as_euro_text,
    as_items,
    as_object,
    as_text,
)
from .._core.exceptions import InvalidResponseError
from .._core.html import normalise_whitespace, strip_tags
from .._core.models import (
    Availability,
    Category,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
)
from .._core.units import SHARED_UNITS
from ._constants import SITE_URL

__all__ = ["AhorramasCategory", "AhorramasProduct", "AhorramasSearchResult"]

_TILE_START = re.compile(r'<div class="product" data-pid=')
_PRODUCT_URL = re.compile(r"/(?P<slug>[^/]+)-(?P<id>[^/-]+)\.html$")
_CATEGORY_URL = re.compile(r"^/(?:[a-z0-9-]+/)+$")
_UNIT_PRICE = re.compile(
    r"^(?P<amount>\d+(?:[.,]\d{3})*(?:[.,]\d+)?)\s*€\s*/\s*(?P<unit>\S.*)$"
)
_DATE_RANGE = re.compile(
    r"(?P<start>\d{1,2}/\d{1,2}/\d{2,4})\s*(?:-|al)\s*(?P<end>\d{1,2}/\d{1,2}/\d{2,4})"
)
_REQUIRED = re.compile(r"\bcomprando\s+(\d+)", re.IGNORECASE)
_SKIPPED_MENU_IDS = re.compile(r"^ver_todo", re.IGNORECASE)


@dataclass(frozen=True, slots=True, kw_only=True)
class AhorramasProduct(Product):
    """one ahorramás product, from a listing tile or from its own record.

    ``category_names`` are the names of the categories the storefront files it
    under, from the top down; a record also carries the primary category's id
    in ``category_ids``. ``badges`` are the attribute badges the tile shows,
    such as ``"SIN GLUTEN"`` or ``"PESO VARIABLE"``. a product sold by weight
    is priced per kilogram, and ``average_weight`` is the weight of an average
    piece in kilograms. a record adds ``general_info``, every filled general
    attribute as a ``(label, value)`` pair.
    """

    category_names: tuple[str, ...] = ()
    badges: tuple[str, ...] = ()
    average_weight: Decimal | None = None
    general_info: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AhorramasCategory(Category):
    """one node of the storefront's menu; ``path`` is its address on the site."""

    path: str = ""
    url: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AhorramasSearchResult(SearchResult):
    """one listing page; the cursor is the next offset."""

    offset: int = 0


# ---------------------------------------------------------------------- helpers


def absolute_url(value: str | None) -> str | None:
    """return ``value`` as an absolute url on the storefront's host."""

    if not value or not value.strip():
        return None
    text = value.strip()
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("/"):
        return f"{SITE_URL}{text}"
    return text


def _text(value: object) -> str | None:
    text = as_text(value)
    if text is None:
        return None
    text = normalise_whitespace(text)
    return text or None


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    return {"true": True, "false": False}.get(value.strip().lower())


def _date(text: str) -> datetime | None:
    day, month, year = (int(part) for part in text.split("/"))
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _promotion(
    text: str | None,
    price: str | None,
    dates: str | None,
    *,
    identifier: str | None = None,
    kind: str | None = None,
) -> Promotion | None:
    """build one promotion from its callout, its price and its date range.

    a callout reads ``"Comprando 2, la unidad te sale a 0.50€"``; the quantity
    it names is the one the offer needs.
    """

    description = _text(text)
    if description is None:
        return None
    match = _DATE_RANGE.search(dates or "")
    required = _REQUIRED.search(description)
    return Promotion(
        id=identifier,
        description=description,
        kind=kind,
        price=as_euro_text(price),
        requires_quantity=int(required.group(1)) if required else None,
        starts_at=_date(match.group("start")) if match else None,
        ends_at=_date(match.group("end")) if match else None,
    )


def _price(
    amount: Decimal | None,
    previous: Decimal | None,
    unit_amount: Decimal | None,
    unit: str | None,
    unit_text: str | None,
    *,
    discount: Decimal | None = None,
    weighed: bool = False,
) -> Price:
    """restate a price; a product sold by weight is priced per kilogram."""

    if weighed and unit_amount is None:
        unit_amount, unit = amount, "Kg"
    discounted = previous is not None and amount is not None and previous > amount
    return Price(
        amount=amount,
        previous=previous,
        unit_price=unit_amount,
        unit_price_unit=unit if unit_amount is not None else None,
        unit_price_text=unit_text,
        reference=SHARED_UNITS.unit_price(unit_amount, unit),
        is_discounted=discounted,
        discount_percentage=discount if discounted else None,
    )


def _slug(url: str | None) -> str | None:
    if url is None:
        return None
    match = _PRODUCT_URL.search(urlsplit(url).path)
    return match.group("slug") if match else None


# ---------------------------------------------------------------------- tiles


@dataclass
class _Tile:
    """what one grid tile shows, as text and attributes."""

    attributes: dict[str, str] = field(default_factory=dict)
    cart: dict[str, str] = field(default_factory=dict)
    gtm: str | None = None
    href: str | None = None
    image: str | None = None
    name: str | None = None
    sales: str | None = None
    listed: str | None = None
    unit: str | None = None
    badges: list[str] = field(default_factory=list)
    weighed: bool = False
    callouts: list[tuple[str, str | None, str | None]] = field(default_factory=list)


class _TileReader(HTMLParser):
    """read one tile; nothing here raises on malformed markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tile = _Tile()
        self._spans: list[tuple[str, ...]] = []
        # span captures: what they fill, how deep their span sits, their text
        self._captures: list[tuple[str, int, list[str]]] = []
        self._heading: tuple[int, list[str]] | None = None
        self._callout: list[str] | None = None
        self._callout_depth = 0
        self._callout_parts: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = tuple(attributes.get("class", "").split())
        tile = self.tile
        if self._heading is not None and tag == "h2":
            self._heading = (self._heading[0] + 1, self._heading[1])
        if self._callout is not None and tag == "div":
            self._callout_depth += 1
        if tag == "div":
            self._div(attributes, classes)
        elif tag == "a" and "product-pdp-link" in classes:
            tile.href = tile.href or attributes.get("href") or None
            tile.gtm = tile.gtm or attributes.get("data-gtm-layer") or None
        elif tag == "img":
            self._image(attributes, classes)
        elif tag == "h2" and "product-name-gtm" in classes and tile.name is None:
            self._heading = (0, [])
        elif tag == "span":
            self._span(attributes, classes)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            attributes = {name: value or "" for name, value in attrs}
            self._image(attributes, tuple(attributes.get("class", "").split()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._heading is not None:
            depth, parts = self._heading
            if depth:
                self._heading = (depth - 1, parts)
            else:
                self.tile.name = normalise_whitespace("".join(parts)) or None
                self._heading = None
        elif tag == "span" and self._spans:
            depth = len(self._spans)
            self._spans.pop()
            for capture in [item for item in self._captures if item[1] == depth]:
                self._captures.remove(capture)
                self._finish(capture[0], normalise_whitespace("".join(capture[2])))
        elif tag == "div" and self._callout is not None:
            if self._callout_depth:
                self._callout_depth -= 1
            else:
                self._close_callout()

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading[1].append(data)
        for _, _, parts in self._captures:
            parts.append(data)
        if self._callout is not None:
            self._callout.append(data)

    def close(self) -> None:
        super().close()
        if self._callout is not None:
            self._close_callout()

    # -------------------------------------------------------------- elements

    def _div(self, attributes: dict[str, str], classes: tuple[str, ...]) -> None:
        tile = self.tile
        if "product-tile" in classes and not tile.attributes:
            tile.attributes = attributes
            if "citrusAdPrint" in classes:
                tile.attributes.setdefault("data-citrusad", "true")
        elif "add-to-cart" in classes and not tile.cart:
            tile.cart = attributes
        elif "promo-info-label" in classes and self._callout is None:
            self._callout = []
            self._callout_depth = 0
            self._callout_parts = {}

    def _image(self, attributes: dict[str, str], classes: tuple[str, ...]) -> None:
        tile = self.tile
        if "tile-image" in classes:
            tile.image = tile.image or attributes.get("src") or None
        if "badge-icon" in classes:
            title = _text(attributes.get("title"))
            if title is not None and title not in tile.badges:
                tile.badges.append(title)
            if "weight-icon" in classes:
                tile.weighed = True

    def _span(self, attributes: dict[str, str], classes: tuple[str, ...]) -> None:
        self._spans.append(classes)
        tile = self.tile
        inside = {name for span in self._spans for name in span}
        key: str | None = None
        if "value" in classes and attributes.get("content"):
            if "list" in inside and tile.listed is None:
                tile.listed = attributes["content"]
            elif "sales" in inside and tile.sales is None:
                tile.sales = attributes["content"]
        elif "unit-price-per-unit" in classes and "strike-through" not in classes:
            key = "unit"
        elif self._callout is not None and "promo-price" in classes:
            key = "promo-price"
        elif self._callout is not None and "promo-date-range" in classes:
            key = "promo-date-range"
        if key is not None:
            self._captures.append((key, len(self._spans), []))

    def _finish(self, key: str, text: str) -> None:
        if key == "unit":
            self.tile.unit = self.tile.unit or text or None
        elif text:
            self._callout_parts.setdefault(key, text)

    def _close_callout(self) -> None:
        text = "".join(self._callout or [])
        dates = self._callout_parts.get("promo-date-range")
        if dates:
            text = text.replace(dates, "")
        price = self._callout_parts.get("promo-price")
        self.tile.callouts.append((text, price, dates))
        self._callout = None


def _gtm(raw: str | None) -> JsonObject:
    if raw is None:
        return {}
    try:
        return as_object(json.loads(unquote(raw)))
    except ValueError:
        return {}


def _tiles(html: str) -> list[tuple[str, _Tile]]:
    """split the grid into its tiles and read each one."""

    if not isinstance(html, str) or not html:
        return []
    starts = [match.start() for match in _TILE_START.finditer(html)]
    tiles: list[tuple[str, _Tile]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(html)
        chunk = html[start:end]
        pid = re.match(r'<div class="product" data-pid="([^"]*)"', chunk)
        reader = _TileReader()
        reader.feed(chunk)
        reader.close()
        tiles.append((pid.group(1) if pid else "", reader.tile))
    return tiles


def parse_tile(pid: str, tile: _Tile) -> AhorramasProduct | None:
    """read one tile into a product, or ``None`` when it names none."""

    gtm = _gtm(tile.gtm)
    identifier = _text(pid) or _text(gtm.get("id"))
    name = tile.name or _text(gtm.get("name"))
    if identifier is None or name is None:
        return None
    attributes = tile.attributes
    cart = tile.cart
    url = absolute_url(tile.href)
    unit_amount: Decimal | None = None
    unit: str | None = None
    match = _UNIT_PRICE.match(tile.unit or "")
    if match is not None:
        unit_amount = as_euro_text(match.group("amount"))
        unit = match.group("unit").strip()
    weighed = _flag(cart.get("data-hasunitweight")) is True or tile.weighed
    promotions = tuple(
        promotion
        for text, price, dates in tile.callouts
        if (promotion := _promotion(text, price, dates)) is not None
    )
    names = tuple(
        label
        for key in (f"data-category{index}" for index in range(1, 6))
        if (label := _text(attributes.get(key)))
    )
    available = _flag(cart.get("data-available"))
    image = absolute_url(tile.image)
    return AhorramasProduct(
        id=identifier,
        name=name,
        brand=_text(attributes.get("data-brand")) or _text(gtm.get("brand")),
        slug=_slug(url),
        url=url,
        thumbnail=None if image is None else Photo(url=image, kind="thumbnail"),
        price=_price(
            as_decimal(tile.sales),
            as_decimal(tile.listed),
            unit_amount,
            unit,
            tile.unit,
        ),
        availability=Availability(
            available=available,
            min_quantity=as_decimal(cart.get("data-min-qty")),
            increment=as_decimal(cart.get("data-step-qty")),
        ),
        promotions=promotions,
        is_sponsored=_flag(attributes.get("data-citrusad")) is True,
        is_variable_weight=weighed,
        category_names=names,
        badges=tuple(tile.badges),
        average_weight=as_decimal(cart.get("data-mediumweight")) if weighed else None,
    )


def grid_category(html: str) -> str | None:
    """return the category a grid answered for, from its sort options.

    an unknown ``cgid`` is not an error upstream: the grid answers the whole
    catalogue instead, and only its sort links, which repeat the request's
    filters, leave the category out.
    """

    match = re.search(r'data-sort-options="([^"]*)"', html or "")
    if match is None:
        return None
    try:
        options = as_object(json.loads(unescape(match.group(1))))
    except ValueError:
        return None
    for option in as_items(options.get("options")):
        url = as_text(as_object(option).get("url")) or ""
        values = parse_qs(urlsplit(url).query).get("cgid")
        if values:
            return values[0]
    return None


def parse_grid(
    html: str, *, query: str, offset: int, page_size: int
) -> AhorramasSearchResult:
    """read one grid page; the cursor moves on while pages come back full.

    the grid publishes no total and keeps its "more results" button on its
    last page, so a page shorter than asked for is the end; a last page that is
    exactly full costs one empty request more.
    """

    tiles = _tiles(html)
    products = tuple(
        product for pid, tile in tiles if (product := parse_tile(pid, tile)) is not None
    )
    following = offset + len(tiles)
    return AhorramasSearchResult(
        query=query,
        products=products,
        page_size=page_size,
        next_cursor=str(following) if len(tiles) >= page_size else None,
        offset=offset,
    )


# --------------------------------------------------------------------- records


def _record_promotion(value: object) -> Promotion | None:
    item = as_object(value)
    if as_boolean(item.get("enabled")) is False:
        return None
    details = _text(item.get("details")) or _text(item.get("calloutMsg"))
    price = _text(item.get("price"))
    text = f"{details} {price}" if details and price else details
    return _promotion(
        text,
        price,
        _text(item.get("detailsDate")) or _text(item.get("calloutDate")),
        identifier=_text(item.get("id")),
        kind=_text(item.get("name")),
    )


def _general_info(value: object) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for item in as_items(value):
        entry = as_object(item)
        label = _text(entry.get("label")) or _text(entry.get("name"))
        raw = entry.get("value")
        if isinstance(raw, bool):
            text: str | None = "true" if raw else None
        else:
            text = _markup_text(raw)
        if label is not None and text is not None:
            found.append((label, text))
    return tuple(found)


def _markup_text(value: object) -> str | None:
    """return a value's text, without the markup some attributes carry."""

    text = as_text(value)
    if text is None:
        return None
    return normalise_whitespace(strip_tags(text)) or None


def _general_value(value: object, name: str) -> str | None:
    for item in as_items(value):
        entry = as_object(item)
        if entry.get("name") == name:
            return _markup_text(entry.get("value"))
    return None


def _description(product: JsonObject) -> str | None:
    for item in as_items(product.get("attrProductDecription")):
        entry = as_object(item)
        if entry.get("name") == "productDescription":
            text = _markup_text(entry.get("value"))
            if text:
                return text
    return _text(product.get("longDescription"))


def _photos(images: JsonObject) -> tuple[Photo, ...]:
    photos: dict[str, Photo] = {}
    for item in as_items(images.get("large")):
        entry = as_object(item)
        url = absolute_url(as_text(entry.get("url")))
        if url is not None:
            photos.setdefault(
                url, Photo(url=url, kind="large", alt=_text(entry.get("alt")))
            )
    return tuple(photos.values())


def parse_product(data: JsonObject) -> AhorramasProduct:
    """read the json record a product page asks for when its quantity changes."""

    product = as_object(data.get("product"))
    identifier = _text(product.get("id"))
    name = _text(product.get("productName"))
    if identifier is None or name is None:
        raise InvalidResponseError("ahorramas returned a product without an id")
    price = as_object(product.get("price"))
    amount = as_decimal(as_object(price.get("sales")).get("value"))
    previous = as_decimal(as_object(price.get("list")).get("value"))
    weighed = as_boolean(product.get("akeneoTipoVentaBalanza")) is True
    unit_amount: Decimal | None = None
    unit: str | None = None
    if not weighed:
        unit = _text(product.get("akeneo_codigoMedida"))
        measure = as_object(product.get("akeneo_unidadMedida"))
        unit_amount = as_euro_text(measure.get("sales")) if unit else None
    images = as_object(product.get("images"))
    small = [as_object(item) for item in as_items(images.get("small"))]
    thumbnail = absolute_url(as_text(small[0].get("url")) if small else None)
    availability = as_object(product.get("availability"))
    messages = [
        text for item in as_items(availability.get("messages")) if (text := _text(item))
    ]
    general = product.get("attrGeneralInfo")
    url_path = urlsplit(as_text(product.get("selectedProductUrl")) or "").path
    url = absolute_url(url_path or None)
    primary = _text(product.get("primaryCategory"))
    discount = as_decimal(product.get("discountPercent"))
    return AhorramasProduct(
        id=identifier,
        name=name,
        brand=_text(product.get("brand")),
        slug=_slug(url),
        url=url,
        pack_size_text=_general_value(general, "productVolume")
        or _general_value(general, "netWeight"),
        thumbnail=None if thumbnail is None else Photo(url=thumbnail, kind="thumbnail"),
        price=_price(
            amount,
            previous,
            unit_amount,
            unit,
            None,
            discount=discount if discount else None,
            weighed=weighed,
        ),
        availability=Availability(
            available=as_boolean(product.get("available")),
            status=messages[0] if messages else None,
            min_quantity=as_decimal(product.get("minOrderQuantity")),
            max_quantity=as_decimal(product.get("maxOrderQuantity")),
            increment=as_decimal(product.get("stepQuantity")),
        ),
        category_ids=(primary,) if primary else (),
        promotions=tuple(
            promotion
            for item in as_items(product.get("promotions"))
            if (promotion := _record_promotion(item)) is not None
        ),
        is_variable_weight=weighed,
        photos=_photos(images),
        description=_description(product),
        storage=_general_value(general, "conservationMode"),
        usage=_general_value(general, "productUse"),
        category_names=tuple(
            label
            for item in as_items(product.get("categoryId"))
            if (label := _text(item))
        ),
        badges=tuple(
            title
            for item in as_items(product.get("attrAkeneoBadges"))
            if as_object(item).get("value")
            and (title := _text(as_object(item).get("title")))
        ),
        average_weight=as_decimal(product.get("akeneoPesoMedio")) if weighed else None,
        general_info=_general_info(general),
    )


# ------------------------------------------------------------------------ menu


class _MenuLinks(HTMLParser):
    """collect the menu's category links with their ids and first text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: dict[str, tuple[str, str]] = {}
        self._current: tuple[str, str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = {name: value or "" for name, value in attrs}
        classes = attributes.get("class", "").split()
        identifier = attributes.get("id", "")
        path = urlsplit(attributes.get("href", "")).path
        if (
            ("nav-link" in classes or "dropdown-link" in classes)
            and identifier
            and not _SKIPPED_MENU_IDS.match(identifier)
            and not urlsplit(attributes.get("href", "")).query
            and _CATEGORY_URL.match(path)
        ):
            self._current = (identifier, path, [])
        else:
            self._current = None

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            identifier, path, parts = self._current
            self.links.setdefault(
                path, (identifier, normalise_whitespace("".join(parts)))
            )
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current[2].append(data)


def parse_menu(html: str) -> tuple[AhorramasCategory, ...]:
    """build the category tree from the menu every full page carries.

    a node's id is its menu link's id, which is the ``cgid`` listings take
    and does not always match the path: ``cordero_y_cabrito`` lives at
    ``/frescos/carniceria/cordero-y-lechal/``. the tree follows the paths, and
    the "ver todo" links are skipped because some point at another branch.
    """

    links = _MenuLinks()
    links.feed(html if isinstance(html, str) else "")
    links.close()
    children: dict[str | None, list[str]] = {}
    for path in links.links:
        segments = path.strip("/").split("/")
        parent = "/" + "/".join(segments[:-1]) + "/" if len(segments) > 1 else None
        if parent is not None and parent not in links.links:
            continue
        children.setdefault(parent, []).append(path)

    def build(path: str, parent_id: str | None, level: int) -> AhorramasCategory:
        identifier, name = links.links[path]
        return AhorramasCategory(
            id=identifier,
            name=name or path.strip("/").rsplit("/", 1)[-1],
            parent_id=parent_id,
            level=level,
            slug=path.strip("/").rsplit("/", 1)[-1],
            path=path,
            url=absolute_url(path),
            children=tuple(
                build(child, identifier, level + 1) for child in children.get(path, ())
            ),
        )

    return tuple(build(path, None, 1) for path in children.get(None, ()))


def index_categories(
    roots: tuple[AhorramasCategory, ...],
) -> dict[str, AhorramasCategory]:
    """map every id in a tree to its node."""

    found: dict[str, AhorramasCategory] = {}
    pending: list[Category] = list(reversed(roots))
    while pending:
        node = pending.pop()
        found.setdefault(node.id, cast("AhorramasCategory", node))
        pending.extend(reversed(node.children))
    return found
