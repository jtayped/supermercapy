"""lidl's models and parsers, built on the shared core models.

lidl serves two product families through one pair of apis: the mail-order
shop (ids starting ``10``) and the in-store grocery assortment (ids starting
``11``). they carry different price shapes — the shop prices by delivery zone,
groceries by offer region — so :class:`LidlProduct` keeps both and the client
resolves the one its binding selects.

the storefront publishes no ingredients, allergens, or nutrition table for
either family, so ``nutrition`` is always ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_decimal,
    as_identifier,
    as_integer,
    as_iso_datetime,
    as_items,
    as_object,
    as_text,
    required_text,
)
from .._core.models import (
    Availability,
    Category,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
    Store,
)
from .._core.units import SHARED_UNITS, UnitPrice

__all__ = [
    "ImageSize",
    "LidlCategory",
    "LidlLeaflet",
    "LidlLeafletLink",
    "LidlLeafletPage",
    "LidlLeafletProduct",
    "LidlPhoto",
    "LidlPrice",
    "LidlProduct",
    "LidlRegion",
    "LidlRegionPrices",
    "LidlSearchResult",
    "LidlStore",
    "LidlZone",
    "OfferWeek",
    "PriceZone",
    "ProductFamily",
    "family_of",
    "parent_id_of",
    "parse_categories",
    "parse_category",
    "parse_leaflet",
    "parse_leaflets",
    "parse_price",
    "parse_product",
    "parse_search_result",
    "parse_sitemap_ids",
    "parse_stores",
]

_SITEMAP_PRODUCT = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_PRODUCT_URL = re.compile(r"/p/[^/]+/p(\d+)/?$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
# a variant id is its parent plus three digits, in both id families
_VARIANT_SUFFIX = 3
_GROCERY_PREFIX = "11"
_GROCERY_CATEGORIES = frozenset({"Food", "F+V", "P+F", "NonFood"})


class ProductFamily(StrEnum):
    """which half of the assortment a product belongs to."""

    SHOP = "shop"
    GROCERY = "grocery"


class PriceZone(StrEnum):
    """the three delivery zones the online shop prices separately."""

    PENINSULA = "PEN"
    BALEARES = "BAL"
    CANARIAS = "CAN"


class ImageSize(StrEnum):
    """the five pre-rendered image sizes the detail endpoint hands out.

    the sizes are separate objects with their own hashes, not query
    parameters: nothing appended to an image url changes the bytes returned.
    """

    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"
    BASKET = "basket"
    THUMB = "thumb"


class OfferWeek(StrEnum):
    """the grocery campaign pages :meth:`~supermercapy.lidl.Lidl.get_offers` reads."""

    CURRENT = "current"
    NEXT = "next"
    WEEKEND = "weekend"
    WEEKEND_NEXT = "weekend_next"
    PERMANENT_CUTS = "permanent_cuts"
    OTHER_BRANDS = "other_brands"


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlPhoto(Photo):
    """one product image, with the sizes the storefront pre-rendered for it.

    listing rows carry a single size, so the other urls are ``None`` there and
    :meth:`sized` falls back to ``url``.
    """

    large_url: str | None = None
    medium_url: str | None = None
    small_url: str | None = None
    basket_url: str | None = None
    thumb_url: str | None = None

    def sized(self, size: ImageSize | str) -> str:
        """return this image at another size, or ``url`` when it has only one."""

        try:
            wanted = ImageSize(size)
        except ValueError as error:
            supported = ", ".join(repr(item.value) for item in ImageSize)
            raise ValueError(f"size must be one of {supported}") from error
        chosen = {
            ImageSize.LARGE: self.large_url,
            ImageSize.MEDIUM: self.medium_url,
            ImageSize.SMALL: self.small_url,
            ImageSize.BASKET: self.basket_url,
            ImageSize.THUMB: self.thumb_url,
        }[wanted]
        return chosen or self.url


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlPrice(Price):
    """one price, live or future, for one region band or delivery zone.

    ``base_price_text`` keeps ``basePrice`` verbatim. on the few products that
    carry one, ``basePrice`` is either structured, a price per ``m`` or ``m²``
    on non-food items, or a sentence on food. a structured one fills
    ``unit_price`` and ``unit_price_unit`` as published; ``reference`` reads
    the structured kind and a sentence holding exactly one price per unit, such
    as ``"1,55 €/kg"``, and leaves any other sentence alone.
    """

    packaging_text: str | None = None
    base_price_text: str | None = None
    is_member_price: bool = False
    member_text: str | None = None
    highlight_text: str | None = None
    label: str | None = None
    ends_at_exclusive: datetime | None = None
    has_vat: bool | None = None
    theme: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlRegionPrices:
    """every price published for one price band.

    a band is named by ``regionPriceId``, not by region id: several offer
    regions share one band, and mainland spain currently uses a single one.
    """

    price_id: str
    current: LidlPrice | None = None
    current_member: LidlPrice | None = None
    future: tuple[LidlPrice, ...] = ()
    future_member: tuple[LidlPrice, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlRegion:
    """one of the fifty-nine offer regions a grocery product is sold in.

    the canary regions carry no ``price_id`` at all, because they are priced
    with igic rather than vat; their prices are simply not published.
    """

    id: str
    name: str | None = None
    price_id: str | None = None
    is_default: bool = False
    status: str | None = None
    has_vat: bool | None = None
    price_has_zero_vat: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlZone:
    """one delivery zone of the online shop, with its own price."""

    id: str
    name: str | None = None
    is_default: bool = False
    status: str | None = None
    price: LidlPrice = LidlPrice()


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlCategory(Category):
    """one node of the shop category tree, read from a search facet."""

    children: tuple[LidlCategory, ...] = ()
    products: tuple[LidlProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlProduct(Product):
    """one product from either family, in the shape both apis share.

    ``price`` is the one the client's binding selects: the zone price for a
    shop product, the offer-region band for a grocery one. when only a lidl
    plus price is published for the selected band, that is what ``price``
    holds and ``price.is_member_price`` is ``True``.
    """

    family: ProductFamily = ProductFamily.SHOP
    product_type: str | None = None
    parent_id: str | None = None
    variant_id: str | None = None
    item_id: str | None = None
    ians: tuple[str, ...] = ()
    store_stock_id: str | None = None
    packaging_text: str | None = None
    price: LidlPrice = LidlPrice()
    price_band_id: str | None = None
    member_price: LidlPrice | None = None
    future_prices: tuple[LidlPrice, ...] = ()
    lidl_plus: tuple[LidlPrice, ...] = ()
    region_prices: tuple[LidlRegionPrices, ...] = ()
    regions: tuple[LidlRegion, ...] = ()
    zones: tuple[LidlZone, ...] = ()
    badges: tuple[str, ...] = ()
    rating: Decimal | None = None
    rating_count: int | None = None
    campaign_ids: tuple[str, ...] = ()
    thumbnail: LidlPhoto | None = None
    photos: tuple[LidlPhoto, ...] = ()

    def band(self, price_id: str) -> LidlRegionPrices | None:
        """return the prices published for one ``regionPriceId``."""

        for band in self.region_prices:
            if band.price_id == price_id:
                return band
        return None

    def region(self, region_id: str | int) -> LidlRegion | None:
        """return one offer region's entry, or ``None`` when it is not sold there."""

        wanted = str(region_id)
        for region in self.regions:
            if region.id == wanted:
                return region
        return None

    def zone(self, zone_id: PriceZone | str) -> LidlZone | None:
        """return one delivery zone's entry, or ``None`` for a grocery product."""

        wanted = str(zone_id)
        for zone in self.zones:
            if zone.id == wanted:
                return zone
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlSearchResult(SearchResult):
    """one page of products, paged by offset."""

    offset: int = 0
    products: tuple[LidlProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlStore(Store):
    """one physical store, with the two geographies its prices depend on."""

    store_number: str | None = None
    offer_region: int | None = None
    offer_region_name: str | None = None
    zone: str | None = None
    zone_name: str | None = None
    status: str | None = None
    services: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlLeafletLink:
    """one clickable hotspot placed on a leaflet page."""

    id: str | None = None
    url: str | None = None
    title: str | None = None
    kind: str | None = None
    icon: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlLeafletPage:
    """one page of a leaflet, as three pre-rendered images."""

    id: str
    number: int | None = None
    width: int | None = None
    height: int | None = None
    kind: str | None = None
    image_url: str | None = None
    zoom_url: str | None = None
    thumbnail_url: str | None = None
    keywords: str | None = None
    alt_text: str | None = None
    links: tuple[LidlLeafletLink, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlLeafletProduct:
    """one product tile printed in a leaflet.

    only bazar leaflets carry these; food leaflets publish page images alone,
    so their ``products`` is always empty. see
    :meth:`~supermercapy.lidl.Lidl.get_leaflet`.
    """

    id: str
    title: str | None = None
    brand: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    image_url: str | None = None
    url: str | None = None
    description: str | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LidlLeaflet:
    """one weekly leaflet, its validity window, and its pages.

    every leaflet reports ``status: "current"``, including next week's, so
    read :attr:`offer_starts_on` rather than :attr:`status` to tell what is
    live.
    """

    id: str
    name: str
    title: str | None = None
    category: str | None = None
    subcategory: str | None = None
    is_active: bool = True
    status: str | None = None
    pdf_url: str | None = None
    hi_res_pdf_url: str | None = None
    file_size: int | None = None
    thumbnail_url: str | None = None
    viewer_url: str | None = None
    detail_url: str | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    offer_starts_on: date | None = None
    offer_ends_on: date | None = None
    regions: tuple[str, ...] = ()
    store_codes: tuple[str, ...] = ()
    pages: tuple[LidlLeafletPage, ...] = ()
    products: tuple[LidlLeafletProduct, ...] = ()

    def is_live_on(self, day: date) -> bool:
        """whether ``day`` falls inside the offer window, ends included."""

        if self.offer_starts_on is None or self.offer_ends_on is None:
            return False
        return self.offer_starts_on <= day <= self.offer_ends_on


# ------------------------------------------------------------------------- ids


def parent_id_of(product_id: str) -> str:
    """return the parent id ``/p/api/detail`` accepts for any id spelling.

    a variant id is its parent plus three digits, in both id families, and the
    detail endpoint answers http 404 for one.
    """

    if product_id.isdigit() and len(product_id) in {11, 12}:
        return product_id[:-_VARIANT_SUFFIX]
    return product_id


def family_of(product_id: str, *, category: str | None = None) -> ProductFamily:
    """return which family an id belongs to, from its prefix or its category."""

    if product_id.startswith(_GROCERY_PREFIX) or category in _GROCERY_CATEGORIES:
        return ProductFamily.GROCERY
    return ProductFamily.SHOP


def parse_sitemap_ids(
    text: str, *, family: ProductFamily | None = None
) -> tuple[str, ...]:
    """return the product ids of a sitemap, optionally one family only."""

    ids: dict[str, None] = {}
    for location in _SITEMAP_PRODUCT.findall(text):
        match = _PRODUCT_URL.search(location)
        if match is None:
            continue
        product_id = match.group(1)
        if family is not None and family_of(product_id) is not family:
            continue
        ids.setdefault(product_id, None)
    return tuple(ids)


# --------------------------------------------------------------------- parsers


def _date(value: object) -> date | None:
    text = as_text(value)
    if text is None:
        return None
    match = _ISO_DATE.fullmatch(text.strip())
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:  # pragma: no cover - the regex admits only real ranges
        return None


def parse_price(
    data: object, *, is_member_price: bool = False, member_text: str | None = None
) -> LidlPrice | None:
    """parse one price object, or return ``None`` when it names no amount."""

    value = as_object(data)
    amount = as_decimal(value.get("price"))
    if amount is None:
        return None
    discount = as_object(value.get("discount"))
    previous = as_decimal(value.get("oldPrice")) or as_decimal(
        discount.get("deletedPrice")
    )
    base_price = as_object(value.get("basePrice"))
    return LidlPrice(
        amount=amount,
        previous=previous,
        currency=as_text(value.get("currencyCode")) or "EUR",
        tax_percentage=as_decimal(value.get("tax")),
        is_discounted=previous is not None,
        discount_percentage=as_decimal(discount.get("percentageDiscount")),
        valid_from=as_iso_datetime(value.get("startDate")),
        valid_until=as_iso_datetime(value.get("endDate")),
        packaging_text=as_text(as_object(value.get("packaging")).get("text")),
        base_price_text=as_text(base_price.get("text")),
        unit_price=(
            as_decimal(base_price.get("price"))
            if as_text(base_price.get("unit")) is not None
            else None
        ),
        unit_price_unit=as_text(base_price.get("unit")),
        reference=_reference(base_price),
        is_member_price=is_member_price,
        member_text=member_text,
        label=as_text(discount.get("bargainHintText")),
        ends_at_exclusive=as_iso_datetime(value.get("endDateExclusive")),
        has_vat=as_boolean(value.get("hasVat")),
        theme=as_text(value.get("priceTheme")),
    )


def _reference(base_price: JsonObject) -> UnitPrice | None:
    unit = as_text(base_price.get("unit"))
    if unit is not None:
        return SHARED_UNITS.unit_price(as_decimal(base_price.get("price")), unit)
    return SHARED_UNITS.unit_price_from_text(base_price.get("text"))


def _member_price(data: object) -> LidlPrice | None:
    value = as_object(data)
    price = parse_price(
        value.get("price"),
        is_member_price=True,
        member_text=as_text(value.get("lidlPlusText")),
    )
    if price is None:
        return None
    highlight = as_text(value.get("highlightText"))
    return price if highlight is None else replace(price, highlight_text=highlight)


def _future_prices(data: object, *, is_member_price: bool) -> tuple[LidlPrice, ...]:
    prices: list[LidlPrice] = []
    for item in as_items(data):
        entry = as_object(item)
        price = parse_price(entry.get("price"), is_member_price=is_member_price)
        if price is not None:
            prices.append(price)
    return tuple(prices)


def parse_region_prices(data: object) -> tuple[LidlRegionPrices, ...]:
    """parse the ``regionsPrices`` mapping, keyed by ``regionPriceId``."""

    bands: list[LidlRegionPrices] = []
    for price_id, item in as_object(data).items():
        value = as_object(item)
        bands.append(
            LidlRegionPrices(
                price_id=price_id,
                current=parse_price(value.get("currentPrice")),
                current_member=_member_price(value.get("currentLidlPlusPrice")),
                future=_future_prices(value.get("futurePrices"), is_member_price=False),
                future_member=_future_prices(
                    value.get("futureLidlPlusPrices"), is_member_price=True
                ),
            )
        )
    return tuple(bands)


def parse_regions(data: object) -> tuple[LidlRegion, ...]:
    """parse ``regionsV2`` into one entry per offer region."""

    regions: list[LidlRegion] = []
    for region_id, item in as_object(data).items():
        value = as_object(item)
        regions.append(
            LidlRegion(
                id=region_id,
                name=as_text(value.get("regionName")),
                price_id=as_text(value.get("regionPriceId")),
                is_default=as_boolean(value.get("isDefault")) or False,
                status=as_text(value.get("status")),
                has_vat=as_boolean(value.get("hasVat")),
                price_has_zero_vat=as_boolean(value.get("priceHasZeroVat")) or False,
            )
        )
    return tuple(regions)


def parse_zones(data: object) -> tuple[LidlZone, ...]:
    """parse the ``zones`` mapping of a shop product."""

    zones: list[LidlZone] = []
    for zone_id, item in as_object(data).items():
        value = as_object(item)
        zones.append(
            LidlZone(
                id=zone_id,
                name=as_text(value.get("zone")),
                is_default=as_boolean(value.get("isDefault")) or False,
                status=as_text(value.get("statusOnlineActual")),
                price=parse_price(value.get("currentPrice")) or LidlPrice(),
            )
        )
    return tuple(zones)


def _photos(data: JsonObject) -> tuple[LidlPhoto, ...]:
    media = as_object(data.get("media"))
    image_map = as_object(media.get("imageMap"))
    order = [
        text
        for item in as_items(as_object(media.get("gallery")).get("images"))
        if (text := as_text(item) or (None if as_integer(item) is None else str(item)))
        is not None
    ]
    photos: list[LidlPhoto] = []
    for key in order or list(image_map):
        entry = as_object(image_map.get(key))
        large = as_text(entry.get("largeUrl"))
        url = large or as_text(entry.get("mediumUrl"))
        if url is None:
            continue
        photos.append(
            LidlPhoto(
                url=url,
                alt=as_text(entry.get("accessibility")),
                large_url=large,
                medium_url=as_text(entry.get("mediumUrl")),
                small_url=as_text(entry.get("smallUrl")),
                basket_url=as_text(entry.get("basketUrl")),
                thumb_url=as_text(entry.get("thumbUrl")),
            )
        )
    if photos:
        return tuple(photos)
    return _listing_photos(data)


def _listing_photos(data: JsonObject) -> tuple[LidlPhoto, ...]:
    photos: list[LidlPhoto] = []
    for item in as_items(data.get("imageList_V1")):
        entry = as_object(item)
        url = as_text(entry.get("image"))
        if url is not None:
            photos.append(LidlPhoto(url=url, alt=as_text(entry.get("accessibility"))))
    if photos:
        return tuple(photos)
    url = as_text(data.get("image"))
    return (LidlPhoto(url=url),) if url else ()


def _badges(data: JsonObject) -> tuple[str, ...]:
    stock = as_object(data.get("stockAvailability"))
    return tuple(
        text
        for item in as_items(as_object(stock.get("badgeInfo")).get("badges"))
        if (text := as_text(as_object(item).get("type"))) is not None
    )


def _availability(data: JsonObject, *, family: ProductFamily) -> Availability:
    stock = as_object(data.get("stockAvailability"))
    indicator = as_integer(stock.get("availabilityIndicator"))
    online = as_boolean(stock.get("onlineAvailable"))
    facts = as_object(data.get("storeFacts"))
    in_store = as_boolean(facts.get("store")) or as_boolean(data.get("store"))
    available = in_store if family is ProductFamily.GROCERY else online
    return Availability(
        available=available,
        status=None if indicator is None else str(indicator),
        min_quantity=as_decimal(stock.get("minOrderableQuantity")),
    )


def _category_path(data: JsonObject) -> tuple[Category, ...]:
    keyfacts = as_object(data.get("keyfacts"))
    names = as_text(keyfacts.get("wonCategoryPrimary")) or ""
    codes = as_text(keyfacts.get("wonCategoryPrimaryPath")) or ""
    labels = [part for part in names.split("/") if part]
    identifiers = [part for part in codes.split("/") if part]
    if not identifiers or len(identifiers) != len(labels):
        return ()
    path: list[Category] = []
    parent: str | None = None
    for level, (identifier, label) in enumerate(zip(identifiers, labels, strict=True)):
        path.append(Category(id=identifier, name=label, parent_id=parent, level=level))
        parent = identifier
    return tuple(path)


def _campaign_ids(meta: JsonObject) -> tuple[str, ...]:
    return tuple(
        identifier
        for path in as_items(meta.get("campaignPaths"))
        for item in as_items(path)
        if (identifier := as_text(as_object(item).get("id"))) is not None
    )


def _selected_band(
    bands: tuple[LidlRegionPrices, ...],
    regions: tuple[LidlRegion, ...],
    region: str | None,
) -> LidlRegionPrices | None:
    price_id: str | None = None
    if region is not None:
        entry = next((item for item in regions if item.id == region), None)
        # a canary region carries no price band at all, and an unlisted region
        # means the product is not sold there; either way there is no price.
        price_id = None if entry is None else entry.price_id
        if price_id is None:
            return None
    else:
        default = next((item for item in regions if item.is_default), None)
        price_id = default.price_id if default is not None else None
        if price_id is None and len(bands) == 1:
            price_id = bands[0].price_id
    return next((band for band in bands if band.price_id == price_id), None)


def parse_product(
    data: object,
    *,
    meta: object = None,
    region: str | None = None,
    zone: str | None = None,
) -> LidlProduct:
    """parse one product from a listing tile, a campaign tile, or a detail.

    ``region`` selects which price band ``price`` reports and ``zone`` which
    delivery zone; both are the client's binding.
    """

    value = as_object(data)
    meta_value = as_object(meta)
    identifier = as_identifier(value.get("erpNumber") or value.get("productId"), "id")
    category = as_text(value.get("category"))
    family = family_of(identifier, category=category)
    keyfacts = as_object(value.get("keyfacts"))
    title = (
        as_text(value.get("title"))
        or as_text(keyfacts.get("title"))
        or as_text(value.get("fullTitle"))
    )
    photos = _photos(value)
    bands = parse_region_prices(value.get("regionsPrices"))
    regions = parse_regions(value.get("regionsV2"))
    zones = parse_zones(value.get("zones"))
    fallback = parse_price(value.get("price"))
    band = _selected_band(bands, regions, region)
    selected_zone = next((item for item in zones if item.id == zone), None)
    member = band.current_member if band is not None else None
    if family is ProductFamily.GROCERY:
        price = (band.current if band is not None else None) or member
        if price is None and not regions and not bands:
            price = fallback
        future = band.future + band.future_member if band is not None else ()
    else:
        price = selected_zone.price if selected_zone is not None else fallback
        future = ()
    packaging = as_text(
        as_object(as_object(value.get("price")).get("packaging")).get("text")
    )
    ratings = as_object(value.get("ratings"))
    # a listing tile carries the brand at the top level; the detail moved it
    # under ``info`` in october 2026
    brand = as_object(value.get("brand")) or as_object(
        as_object(value.get("info")).get("brand")
    )
    return LidlProduct(
        id=identifier,
        name=required_text(title, "product title"),
        brand=as_text(brand.get("name")),
        ean=as_text(meta_value.get("ean")) or _first_ean(value),
        slug=_slug(as_text(value.get("canonicalPath"))),
        url=as_text(value.get("canonicalPath")),
        pack_size_text=(price.packaging_text if price is not None else None)
        or packaging,
        thumbnail=photos[0] if photos else None,
        photos=photos,
        price=price or LidlPrice(),
        availability=_availability(value, family=family),
        category_ids=tuple(item.id for item in _category_path(value)),
        promotions=_promotions(band, member),
        category_path=_category_path(value),
        description=as_text(keyfacts.get("description"))
        or as_text(keyfacts.get("supplementalDescription")),
        requires_age_check=as_boolean(value.get("ageRestriction")) or False,
        family=family,
        product_type=as_text(value.get("productType")),
        parent_id=as_text(value.get("parentId")) or parent_id_of(identifier),
        variant_id=_optional_id(value.get("variantId")),
        item_id=_optional_id(value.get("itemId")),
        ians=tuple(
            text for item in as_items(value.get("ians")) if (text := as_text(item))
        ),
        store_stock_id=as_text(value.get("storeStockId")),
        packaging_text=(price.packaging_text if price is not None else None)
        or packaging,
        price_band_id=band.price_id if band is not None else None,
        member_price=member,
        future_prices=future,
        lidl_plus=_lidl_plus(value.get("lidlPlus")),
        region_prices=bands,
        regions=regions,
        zones=zones,
        badges=_badges(value),
        rating=as_decimal(ratings.get("average")),
        rating_count=as_integer(ratings.get("count")),
        campaign_ids=_campaign_ids(meta_value),
    )


def _promotions(
    band: LidlRegionPrices | None, member: LidlPrice | None
) -> tuple[Promotion, ...]:
    promotions: list[Promotion] = []
    if member is not None:
        promotions.append(
            Promotion(
                description=member.member_text,
                kind="lidl_plus",
                price=member.amount,
                starts_at=member.valid_from,
                ends_at=member.valid_until,
                member_only=True,
            )
        )
    current = band.current if band is not None else None
    if current is not None and current.is_discounted:
        promotions.append(
            Promotion(
                description=current.label,
                kind="discount",
                price=current.amount,
                starts_at=current.valid_from,
                ends_at=current.valid_until,
            )
        )
    return tuple(promotions)


def _lidl_plus(data: object) -> tuple[LidlPrice, ...]:
    prices: list[LidlPrice] = []
    for item in as_items(data):
        price = _member_price(item)
        if price is not None:
            prices.append(price)
    return tuple(prices)


def _first_ean(data: JsonObject) -> str | None:
    for item in as_items(data.get("eans")):
        text = as_text(item)
        if text:
            return text
    return None


def _optional_id(value: object) -> str | None:
    if value is None:
        return None
    return as_identifier(value, "id")


def _slug(path: str | None) -> str | None:
    if path is None:
        return None
    parts = [part for part in path.split("/") if part]
    return parts[1] if len(parts) > 1 else None


def parse_search_result(
    data: object,
    *,
    query: str,
    offset: int,
    page_size: int,
    region: str | None = None,
    zone: str | None = None,
    family: ProductFamily | None = None,
) -> LidlSearchResult:
    """parse a search envelope into one page with an offset cursor."""

    value = as_object(data)
    rows = as_items(value.get("items"))
    products: list[LidlProduct] = []
    for item in rows:
        gridbox = as_object(as_object(item).get("gridbox"))
        product = parse_product(
            gridbox.get("data"), meta=gridbox.get("meta"), region=region, zone=zone
        )
        if family is None or product.family is family:
            products.append(product)
    total = as_integer(value.get("numFound"))
    consumed = offset + len(rows)
    has_more = bool(rows) and total is not None and consumed < total
    return LidlSearchResult(
        query=query,
        products=tuple(products),
        page_size=page_size,
        offset=offset,
        total_hits=total,
        next_cursor=str(consumed) if has_more else None,
    )


def _facet_values(data: object) -> list[JsonObject]:
    for item in as_items(as_object(data).get("facets")):
        facet = as_object(item)
        if as_text(facet.get("code")) == "category":
            return [as_object(value) for value in as_items(facet.get("values"))]
    return []


def _facet_category(data: JsonObject, *, parent_id: str | None = None) -> LidlCategory:
    identifier = as_identifier(data.get("value"), "category id")
    return LidlCategory(
        id=identifier,
        name=required_text(data.get("label"), "category name"),
        parent_id=parent_id,
        product_count=as_integer(data.get("count")),
        children=tuple(
            _facet_category(as_object(child), parent_id=identifier)
            for child in as_items(data.get("children"))
        ),
    )


def parse_categories(data: object) -> tuple[LidlCategory, ...]:
    """parse the ``category`` facet of a search response into a tree."""

    return tuple(_facet_category(value) for value in _facet_values(data))


def _facet_node(
    values: list[JsonObject], category_id: str, parent_id: str | None = None
) -> tuple[JsonObject, str | None] | None:
    for value in values:
        identifier = as_text(value.get("value"))
        if identifier == category_id:
            return value, parent_id
        children = [as_object(child) for child in as_items(value.get("children"))]
        found = _facet_node(children, category_id, identifier)
        if found is not None:
            return found
    return None


def parse_category(
    data: object, *, category_id: str, products: tuple[LidlProduct, ...] = ()
) -> LidlCategory | None:
    """return one facet node by id, with the listed products attached.

    a filtered listing's facet holds the path from the root down to the
    selected category, so a subcategory sits below its ancestors rather than
    at the top level.
    """

    found = _facet_node(_facet_values(data), category_id)
    if found is None:
        return None
    value, parent_id = found
    node = _facet_category(value, parent_id=parent_id)
    return LidlCategory(
        id=node.id,
        name=node.name,
        parent_id=node.parent_id,
        product_count=node.product_count,
        children=node.children,
        products=products,
    )


def _address(data: JsonObject) -> str | None:
    parts = [
        text
        for key in ("streetName", "streetNumber")
        if (text := (as_text(data.get(key)) or "").strip())
    ]
    return " ".join(parts).replace(" ,", ",") or None


def parse_stores(data: object) -> tuple[LidlStore, ...]:
    """parse one page of the schwarz stores api."""

    stores: list[LidlStore] = []
    for item in as_items(as_object(data).get("items")):
        value = as_object(item)
        number = as_text(value.get("objectNumber"))
        if number is None:
            continue
        address = as_object(value.get("address"))
        marketing = as_object(value.get("marketingData"))
        stores.append(
            LidlStore(
                id=number,
                name=as_text(value.get("storeName")) or number,
                kind="store",
                address=_address(address),
                postal_code=as_text(address.get("zip")),
                city=as_text(address.get("city")),
                province=as_text(address.get("state")),
                latitude=_coordinate(address.get("latitude")),
                longitude=_coordinate(address.get("longitude")),
                store_number=number.removeprefix("ES").lstrip("0") or None,
                offer_region=as_integer(marketing.get("offerRegion")),
                offer_region_name=as_text(marketing.get("offerRegionName")),
                zone=as_text(marketing.get("zone")),
                zone_name=as_text(marketing.get("zoneName")),
                status=as_text(as_object(value.get("status")).get("name")),
                services=tuple(
                    text
                    for icon in as_items(marketing.get("infoIcons"))
                    if (text := as_text(as_object(icon).get("name"))) is not None
                ),
            )
        )
    return tuple(stores)


def _coordinate(value: object) -> float | None:
    number = as_decimal(value)
    return None if number is None else float(number)


def store_total(data: object) -> int | None:
    """return how many stores the api says exist, for offset paging."""

    return as_integer(as_object(as_object(data).get("meta")).get("total"))


def _leaflet_links(data: object) -> tuple[LidlLeafletLink, ...]:
    links: list[LidlLeafletLink] = []
    for item in as_items(data):
        value = as_object(item)
        links.append(
            LidlLeafletLink(
                id=as_text(value.get("id")),
                url=as_text(value.get("url")),
                title=as_text(value.get("title")),
                kind=as_text(value.get("displayType")),
                icon=as_text(value.get("icon")),
            )
        )
    return tuple(links)


def _leaflet_pages(data: object) -> tuple[LidlLeafletPage, ...]:
    pages: list[LidlLeafletPage] = []
    for item in as_items(data):
        value = as_object(item)
        identifier = as_text(value.get("id"))
        if identifier is None:
            continue
        pages.append(
            LidlLeafletPage(
                id=identifier,
                number=as_integer(value.get("number")),
                width=as_integer(value.get("width")),
                height=as_integer(value.get("height")),
                kind=as_text(value.get("type")),
                image_url=as_text(value.get("image")),
                zoom_url=as_text(value.get("zoom")),
                thumbnail_url=as_text(value.get("thumbnail")),
                keywords=as_text(value.get("keyWords")),
                alt_text=as_text(value.get("altText")),
                links=_leaflet_links(value.get("links")),
            )
        )
    return tuple(pages)


def _leaflet_products(data: object) -> tuple[LidlLeafletProduct, ...]:
    entries = as_object(data)
    items = list(entries.values()) if entries else as_items(data)
    products: list[LidlLeafletProduct] = []
    for item in items:
        value = as_object(item)
        identifier = as_text(value.get("productId"))
        if identifier is None:
            continue
        products.append(
            LidlLeafletProduct(
                id=identifier,
                title=as_text(value.get("title")),
                brand=as_text(value.get("brand")),
                price=as_decimal(value.get("price")),
                currency=as_text(value.get("currencyText")),
                image_url=as_text(value.get("image")),
                url=as_text(value.get("canonicalUrl")),
                description=as_text(value.get("description")),
                category=as_text(value.get("categoryPrimary")),
            )
        )
    return tuple(products)


def _leaflet(
    data: JsonObject, *, category: str | None = None, subcategory: str | None = None
) -> LidlLeaflet:
    regions = [as_object(item) for item in as_items(data.get("regions"))]
    return LidlLeaflet(
        id=as_identifier(data.get("id"), "leaflet id"),
        name=required_text(data.get("name"), "leaflet name"),
        title=as_text(data.get("title")),
        category=category or as_text(data.get("category")),
        subcategory=subcategory or as_text(data.get("subcategory")),
        is_active=as_boolean(data.get("isActive")) is not False,
        status=as_text(data.get("status")),
        pdf_url=as_text(data.get("pdfUrl")),
        hi_res_pdf_url=as_text(data.get("hiResPdfUrl")),
        file_size=as_integer(data.get("fileSize")),
        thumbnail_url=as_text(data.get("thumbnailUrl")),
        viewer_url=as_text(data.get("flyerUrlAbsolute")),
        detail_url=as_text(data.get("flyerJson")),
        starts_on=_date(data.get("startDate")),
        ends_on=_date(data.get("endDate")),
        offer_starts_on=_date(data.get("offerStartDate")),
        offer_ends_on=_date(data.get("offerEndDate")),
        regions=tuple(
            code
            for region in regions
            if as_text(region.get("type")) != "store"
            and (code := as_text(region.get("code"))) is not None
        ),
        store_codes=tuple(
            code
            for region in regions
            if as_text(region.get("type")) == "store"
            and (code := as_text(region.get("code"))) is not None
        ),
        pages=_leaflet_pages(data.get("pages")),
        products=_leaflet_products(data.get("products")),
    )


def parse_leaflets(data: object) -> tuple[LidlLeaflet, ...]:
    """parse the leaflet overview, flattening its category and subcategory."""

    leaflets: list[LidlLeaflet] = []
    for category_item in as_items(as_object(data).get("categories")):
        category = as_object(category_item)
        name = as_text(category.get("name"))
        for subcategory_item in as_items(category.get("subcategories")):
            subcategory = as_object(subcategory_item)
            leaflets.extend(
                _leaflet(
                    as_object(flyer),
                    category=name,
                    subcategory=as_text(subcategory.get("name")),
                )
                for flyer in as_items(subcategory.get("flyers"))
            )
    return tuple(leaflets)


def parse_leaflet(data: object) -> LidlLeaflet:
    """parse one leaflet detail envelope."""

    return _leaflet(as_object(as_object(data).get("flyer")))
