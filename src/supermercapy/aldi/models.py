"""aldi's models and parsers, built on the shared core models.

aldi publishes a priced catalogue, not a shop: one algolia record per product
with its price, a unit price, any time-limited prices and the categories it
sits in. there is no barcode, no ingredient list and no nutrition table in any
record, so those fields stay empty rather than guessed. the category tree and
the weekly offers come from the json the site embeds in its own pages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_decimal,
    as_identifier,
    as_integer,
    as_items,
    as_object,
    as_text,
    required_text,
)
from .._core.exceptions import InvalidResponseError
from .._core.models import (
    Availability,
    Category,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
)
from .._core.units import UnitReader
from ._constants import SITE_URL

__all__ = [
    "AldiCategory",
    "AldiOfferGroup",
    "AldiPrice",
    "AldiProduct",
    "AldiSearchResult",
    "Region",
]

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
_HUNDRED = Decimal(100)
_PRODUCTS_PATH = "/productos/"
# basePriceScale is a slug: "kg", "l", "100-ml", "lavado", "m-(metro)". "g" and
# "100-g" are sent on prices that are per kilogram (1.59 € for 200 g with a
# base price of 7.95), so both read as unknown; "par" (a pair) has no shared
# unit. the rest are one piece of what the pack counts: capsules, tea bags,
# wipes and tissues.
_UNITS = UnitReader(
    {
        "m (metro)": "m",
        "capsula": "ud",
        "bolsita": "ud",
        "toallita": "ud",
        "panuelo": "ud",
        "g": None,
        "par": None,
    }
)


class Region(StrEnum):
    """aldi's three price regions; each has its own index and offers page."""

    PENINSULA = "pen"
    BALEARIC_ISLANDS = "bal"
    CANARY_ISLANDS = "can"


# the page prefix of each region's site; the península one is the root
REGION_PATHS = MappingProxyType(
    {
        Region.PENINSULA: "",
        Region.BALEARIC_ISLANDS: "/bal",
        Region.CANARY_ISLANDS: "/can",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AldiPrice(Price):
    """one price with the window aldi publishes for it.

    ``valid_from`` can lie ahead: the index switches a product to its next
    price up to a few days before it applies, and aldi publishes no price for
    the days in between. ``promo_text`` is the shelf label, such as ``"-24%"``.
    """

    promo_text: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AldiCategory(Category):
    """one category page of the site.

    ``id`` is the page path below ``/productos``, such as
    ``"lacteos-y-huevos/leche-y-bebidas-vegetales"``; ``key`` is the category
    key a product's ``category_ids`` hold, such as
    ``"leche-y-bebidas-vegetales"``. the two differ more often than the
    example suggests (the page ``fruta`` holds the key ``frutas``).
    """

    key: str | None = None
    url: str | None = None
    is_marketing: bool = False
    children: tuple[AldiCategory, ...] = ()
    products: tuple[AldiProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AldiProduct(Product):
    """one product record, the same shape in search, listings and offers.

    ``price`` is the record's ``currentPrice``; loose produce sold by weight
    has none. ``promotion_prices`` are the time-limited prices the record
    carries, each with its window, including ones that start later.
    ``category_ids`` are category keys, see :class:`AldiCategory`.
    """

    price: AldiPrice = AldiPrice()
    promotion_prices: tuple[AldiPrice, ...] = ()
    article_number: str | None = None
    main_category_id: str | None = None
    long_description: str | None = None
    certificates: tuple[Photo, ...] = ()
    is_coming_soon: bool = False
    is_recall: bool = False
    region: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AldiOfferGroup:
    """one section of the weekly offers page and the period it runs for."""

    title: str
    period: str | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    published_on: date | None = None
    products: tuple[AldiProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AldiSearchResult(SearchResult):
    """one page of search or listing results, priced for one region."""

    products: tuple[AldiProduct, ...] = ()
    offset: int = 0
    region: str | None = None


# --------------------------------------------------------------------- parsers


def _timestamp(value: object) -> datetime | None:
    seconds = as_integer(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC)


def _local_date(value: object) -> date | None:
    text = as_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_price(data: object) -> AldiPrice:
    """parse a ``currentPrice`` or ``promotionPrices`` entry."""

    value = as_object(data)
    amount = as_decimal(value.get("priceValue"))
    struck = as_decimal(as_object(value.get("strikePrice")).get("strikePriceValue"))
    previous = (
        struck
        if struck is not None and amount is not None and struck > amount
        else None
    )
    discount: Decimal | None = None
    if previous is not None and amount is not None:
        discount = ((previous - amount) / previous * _HUNDRED).quantize(Decimal("0.01"))
    base = as_object(next(iter(as_items(value.get("basePrice"))), None))
    unit_price = as_decimal(base.get("basePriceValue"))
    scale = as_text(base.get("basePriceScale")) or None
    return AldiPrice(
        amount=amount,
        previous=previous,
        unit_price=unit_price,
        unit_price_unit=scale,
        reference=(
            None
            if scale is None
            else _UNITS.unit_price(unit_price, scale.replace("-", " "))
        ),
        is_discounted=previous is not None,
        discount_percentage=discount,
        valid_from=_timestamp(value.get("validFrom")),
        valid_until=_timestamp(value.get("validUntil")),
        promo_text=as_text(as_object(value.get("priceTagLabels")).get("promoText1"))
        or None,
    )


def _promotion(price: AldiPrice) -> Promotion:
    return Promotion(
        description=price.promo_text,
        kind="promotion_price",
        price=price.amount,
        starts_at=price.valid_from,
        ends_at=price.valid_until,
    )


def _assets(data: object) -> tuple[list[Photo], list[Photo]]:
    photos: list[Photo] = []
    certificates: list[Photo] = []
    for item in as_items(data):
        value = as_object(item)
        url = as_text(value.get("url"))
        if url is None or not url.strip():
            continue
        kind = as_text(value.get("type"))
        photo = Photo(url=url, kind=kind, alt=as_text(value.get("altText")) or None)
        (certificates if kind == "certificate" else photos).append(photo)
    return photos, certificates


def _article_number(data: object) -> str | None:
    for item in as_items(data):
        value = as_object(item)
        if value.get("type") == "KVArticleNumber":
            return as_text(value.get("value")) or None
    return None


def parse_product(data: object, *, region: Region | str | None = None) -> AldiProduct:
    """parse one algolia product record."""

    value = as_object(data)
    object_id = as_identifier(value.get("objectID"), "objectID")
    photos, certificates = _assets(value.get("assets"))
    promotion_prices = tuple(
        parse_price(item) for item in as_items(value.get("promotionPrices"))
    )
    slug = as_text(value.get("productSlug")) or None
    prefix = "" if region is None else REGION_PATHS.get(Region(region), "")
    is_coming_soon = as_boolean(value.get("isComingSoon")) or False
    available = as_boolean(value.get("isAvailable"))
    return AldiProduct(
        id=object_id,
        name=required_text(value.get("name"), "product name"),
        brand=as_text(value.get("brandName")) or None,
        slug=slug,
        url=None if slug is None else f"{SITE_URL}{prefix}/producto/{slug}.html",
        pack_size_text=as_text(value.get("salesUnit")) or None,
        thumbnail=photos[0] if photos else None,
        photos=tuple(photos),
        price=parse_price(value.get("currentPrice")),
        availability=Availability(available=available),
        category_ids=tuple(
            text
            for item in as_items(value.get("categoryIDs"))
            if (text := as_text(item))
        ),
        promotions=tuple(_promotion(price) for price in promotion_prices),
        description=as_text(value.get("shortDescription")) or None,
        promotion_prices=promotion_prices,
        article_number=_article_number(value.get("productReferences")),
        main_category_id=as_text(value.get("mainCategoryID")) or None,
        long_description=as_text(value.get("longDescription")) or None,
        certificates=tuple(certificates),
        is_coming_soon=is_coming_soon,
        is_recall=as_boolean(value.get("isRecall")) or False,
        region=None if region is None else Region(region).value,
    )


def parse_hits(
    data: object, *, query: str, offset: int, page_size: int, region: Region
) -> AldiSearchResult:
    """parse an algolia query answer into one page with an offset cursor."""

    value = as_object(data)
    hits = value.get("hits")
    if not isinstance(hits, list):
        raise InvalidResponseError("search response has no hits array")
    products = tuple(parse_product(item, region=region) for item in hits)
    total = as_integer(value.get("nbHits"))
    following = offset + len(products)
    more = bool(products) and (total is None or following < total)
    return AldiSearchResult(
        query=query,
        products=products,
        page_size=page_size,
        total_hits=total,
        next_cursor=str(following) if more else None,
        offset=offset,
        region=region.value,
    )


# ------------------------------------------------------------ embedded pages


def next_data(html: str) -> JsonObject:
    """return the ``__NEXT_DATA__`` document a site page embeds."""

    match = _NEXT_DATA.search(html)
    if match is None:
        raise InvalidResponseError("page has no __NEXT_DATA__ script")
    try:
        data = json.loads(match.group(1))
    except ValueError as error:
        raise InvalidResponseError("page has an unreadable __NEXT_DATA__") from error
    if not isinstance(data, dict):
        raise InvalidResponseError("page __NEXT_DATA__ is not an object")
    return data


def page_props(document: JsonObject) -> JsonObject:
    """return the page props of a ``__NEXT_DATA__`` document."""

    return as_object(as_object(document.get("props")).get("pageProps"))


def api_data(props: JsonObject) -> dict[str, Any]:
    """return each embedded api answer by name, from the page's ``apiData``."""

    raw = props.get("apiData")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as error:
            raise InvalidResponseError("page apiData is unreadable") from error
    answers: dict[str, Any] = {}
    for item in as_items(raw):
        pair = as_items(item)
        if len(pair) == 2 and isinstance(pair[0], str):
            answers.setdefault(pair[0], as_object(pair[1]).get("res"))
    return answers


def _page_id(path: object) -> str | None:
    """return a category id from a page path such as ``/productos/a/b``."""

    text = as_text(path)
    if text is None:
        return None
    _, marker, rest = text.partition(_PRODUCTS_PATH)
    rest = rest.removesuffix(".html").strip("/")
    return rest if marker and rest else None


def parse_category_node(
    data: object, *, parent_id: str | None, level: int
) -> AldiCategory | None:
    """parse one entry of an embedded category children list."""

    value = as_object(data)
    identifier = _page_id(as_object(value.get("reference")).get("path"))
    name = as_text(value.get("navigationTitle")) or as_text(value.get("title"))
    if identifier is None or not name or as_boolean(value.get("hideInCategory")):
        return None
    return AldiCategory(
        id=identifier,
        name=name,
        parent_id=parent_id,
        level=level,
        slug=identifier.rsplit("/", 1)[-1],
        image_url=as_text(value.get("teaserImageSmall")) or None,
        key=as_text(value.get("categoryKey")) or None,
        url=f"{SITE_URL}{_PRODUCTS_PATH}{identifier}.html",
        is_marketing=as_boolean(value.get("marketingCategory")) or False,
    )


def parse_children(
    answers: dict[str, Any], *, parent_id: str | None, level: int
) -> tuple[AldiCategory, ...]:
    """parse the children list a category page embeds, skipping hidden ones."""

    return tuple(
        node
        for item in as_items(answers.get("PRODUCT_MGNL_CATEGORY_CHILDREN_GET"))
        if (node := parse_category_node(item, parent_id=parent_id, level=level))
        is not None
    )


def parse_category_page(document: JsonObject) -> tuple[AldiCategory, str | None]:
    """parse a category page into its node and the algolia filter it lists.

    a top-level page embeds its children and no filter; a child page embeds
    the filter the storefront lists it with, and its parent.
    """

    props = page_props(document)
    page = as_object(props.get("page"))
    identifier = _page_id(page.get("@path"))
    name = as_text(page.get("navigationTitle")) or as_text(page.get("title"))
    if identifier is None or not name:
        raise InvalidResponseError("category page names no category")
    answers = api_data(props)
    parent = as_object(
        as_object(
            as_object(answers.get("PRODUCT_MGNL_CATEGORY_PARENT_GET")).get("data")
        ).get("parent")
    )
    parent_id = _page_id(as_object(parent.get("reference")).get("path"))
    level = 1 if parent_id is None else 2
    children = parse_children(answers, parent_id=identifier, level=level + 1)
    node = AldiCategory(
        id=identifier,
        name=name,
        parent_id=parent_id,
        level=level,
        slug=identifier.rsplit("/", 1)[-1],
        key=as_text(page.get("categoryKey")) or None,
        url=f"{SITE_URL}{_PRODUCTS_PATH}{identifier}.html",
        is_marketing=as_boolean(page.get("marketingCategory")) or False,
        children=children,
    )
    algolia = as_object(props.get("algoliaConfig"))
    return node, as_text(algolia.get("filters")) or None


def parse_offer_groups(
    document: JsonObject, *, region: Region | str | None = None
) -> tuple[AldiOfferGroup, ...]:
    """parse the weekly offers page into its sections, in page order.

    a product the page names but does not embed is left out.
    """

    offers = as_object(api_data(page_props(document)).get("OFFER_GET"))
    records = as_object(offers.get("algoliaDataMap"))
    groups: list[AldiOfferGroup] = []
    for period_item in as_items(offers.get("categories")):
        period = as_object(period_item)
        for section_item in as_items(period.get("content")):
            section = as_object(section_item)
            title = as_text(section.get("title"))
            if not title:
                continue
            products = tuple(
                parse_product(records[product_id], region=region)
                for item in as_items(section.get("productIds"))
                if (product_id := as_text(item)) is not None and product_id in records
            )
            groups.append(
                AldiOfferGroup(
                    title=title,
                    period=as_text(period.get("title")) or None,
                    starts_on=_local_date(period.get("startDate")),
                    ends_on=_local_date(period.get("endDate")),
                    published_on=_local_date(period.get("goLiveDate")),
                    products=products,
                )
            )
    return tuple(groups)
