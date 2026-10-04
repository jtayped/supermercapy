"""consum's models and parsers, built on the shared core models.

consum returns the same object for a listing row and for a product detail, so
there is one :class:`ConsumProduct` rather than a summary and a detail type.
the storefront exposes no ingredients, allergens, or nutrition table, so
``nutrition`` is always ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import IntEnum, StrEnum

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
from .._core.exceptions import ConfigurationError, InvalidResponseError
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
from .._core.units import UnitReader

__all__ = [
    "ActionType",
    "ConsumAttribute",
    "ConsumAvailability",
    "ConsumCategory",
    "ConsumFilter",
    "ConsumFilterGroup",
    "ConsumFilterValue",
    "ConsumGroup",
    "ConsumOffer",
    "ConsumPhoto",
    "ConsumProduct",
    "ConsumSearchResult",
    "ConsumStore",
    "DeliveryMethod",
    "ImageSize",
    "ProductFilters",
    "ProductKind",
    "PromotionType",
    "SortOption",
    "SortOrder",
    "Suggestion",
    "TargetType",
]

_RENDITION = re.compile(r"^\d+x\d+$")
_HUNDRED = Decimal(100)
# unitPriceUnitType reads "1 Kg", "100 Gr", "1 U"; these are the words the
# shared vocabulary does not know: dozen, wash and dose
_UNITS = UnitReader({"dc": "docena", "lv": "lavado", "do": "dosis"})


class ImageSize(StrEnum):
    """the three image renditions consum's cdn actually generates.

    the sizes are path segments, not query parameters; every other value in
    the storefront bundle's enum returns http 404.
    """

    LOW = "135x135"
    MEDIUM = "300x300"
    HIGH = "1600x1600"


class DeliveryMethod(StrEnum):
    """delivery types the platform defines; consum operates ``D`` and ``T``."""

    HOME = "D"
    SHOP = "T"
    LOCKER = "L"
    EXPRESS = "X"


class SortOrder(IntEnum):
    """values for the ``orderById`` sort parameter.

    :meth:`~supermercapy.consum.Consum.get_sort_orders` returns the orders the
    storefront currently advertises, which is the authoritative list; this
    enum names the ones observed in it.
    """

    PRICE_ASC = 1
    PRICE_DESC = 2
    NAME_ASC = 3
    NAME_DESC = 4
    OFFERS_FIRST = 5
    RELEVANCE = 7
    MOST_BOUGHT = 8
    UNIT_PRICE_ASC = 11
    NEWEST = 12
    DEFAULT = 13


class ProductKind(IntEnum):
    """values of ``productType``; compare against the raw integer field."""

    STANDARD = 1
    WEIGHT = 2
    WEIGHT_UNITS = 3
    KIT = 4
    SERVICE = 5
    GIFT = 6
    PRESENT = 7


class PromotionType(IntEnum):
    """values of an offer's ``promotion_type``."""

    NO_OFFER_PRICE = 0
    OFFER_PRICE = 1
    DEFERRED = 2


class TargetType(IntEnum):
    """what an offer applies to; values of ``target_type``."""

    PRODUCT = 1
    CATEGORY = 2
    HIERARCHY = 3
    TOTAL = 4
    LOT = 5
    AGRUPATION = 6
    DELIVERY_EXPENSES = 7
    PICKING_EXPENSES = 8
    NET_AMOUNT = 9
    BUNDLE = 11


class ActionType(IntEnum):
    """what an offer does; values of ``action_type``."""

    NET_DISCOUNT = 1
    NET_PRICE = 2
    PERCENTAGE = 3
    GIFT = 4
    PERCENTAGE_NEXT_PURCHASE = 5
    PROMO_GIFT_PRODUCT_TYPE_1 = 41
    PROMO_GIFT_PRODUCT_TYPE_1_THAT_COMPUTES = 410
    PROMO_GIFT = 46
    PROMO_GIFT_THAT_COMPUTES = 460
    PROMO_PRESENT = 47


_WEIGHT_KINDS = frozenset({ProductKind.WEIGHT, ProductKind.WEIGHT_UNITS})


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumPhoto(Photo):
    """one product image, resizable through :meth:`sized`."""

    order: int | None = None

    def sized(self, size: ImageSize | str) -> str:
        """return the same image at another rendition without performing i/o.

        the url is returned unchanged when its path carries no rendition
        segment to swap.
        """

        try:
            rendition = ImageSize(size).value
        except ValueError as error:
            supported = ", ".join(repr(item.value) for item in ImageSize)
            raise ValueError(f"size must be one of {supported}") from error
        head, separator, tail = self.url.rpartition("/")
        parent, parent_separator, directory = head.rpartition("/")
        if not separator or not parent_separator or not _RENDITION.fullmatch(directory):
            return self.url
        return f"{parent}/{rendition}/{tail}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumAvailability(Availability):
    """whether a product can be bought, and in what quantities."""

    temporarily_out_of_stock: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumOffer(Promotion):
    """one entry of a product's ``offers`` or ``coupons`` array.

    ``amount`` is the discounted price for an immediate offer and the amount
    credited back for a deferred one, so only the immediate variant sets the
    inherited ``price``. compare the three type fields against
    :class:`PromotionType`, :class:`TargetType`, and :class:`ActionType`;
    they stay plain integers so an unfamiliar campaign still round-trips.
    """

    promotion_id: str | None = None
    min_description: str | None = None
    short_description: str | None = None
    long_description: str | None = None
    image_url: str | None = None
    promotion_type: int | None = None
    target_type: int | None = None
    action_type: int | None = None
    is_immediate: bool = False
    amount: Decimal | None = None
    picto_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumCategory(Category):
    """one node of the storefront category tree."""

    url: str | None = None
    kind: int | None = None
    children: tuple[ConsumCategory, ...] = ()
    products: tuple[ConsumProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumAttribute:
    """one product attribute, with its values flattened across languages."""

    code: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumProduct(Product):
    """one product, in the single shape consum uses for listings and details.

    ``nutrition`` is always ``None``: the storefront publishes no ingredient,
    allergen, or nutrition data for any product.
    """

    internal_id: str | None = None
    product_type: int | None = None
    pack_format: str | None = None
    is_featured: bool = False
    contains_allergens: bool = False
    allows_comments: bool = False
    thumbnail: ConsumPhoto | None = None
    photos: tuple[ConsumPhoto, ...] = ()
    availability: ConsumAvailability = ConsumAvailability()
    categories: tuple[ConsumCategory, ...] = ()
    offers: tuple[ConsumOffer, ...] = ()
    coupons: tuple[ConsumOffer, ...] = ()
    attributes: tuple[ConsumAttribute, ...] = ()

    def attribute(self, code: str) -> tuple[str, ...]:
        """return one attribute's values, or an empty tuple when it is absent."""

        for attribute in self.attributes:
            if attribute.code == code:
                return attribute.values
        return ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumFilterValue:
    """one selectable value of a facet."""

    id: str
    name: str
    count: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumFilter:
    """one facet, such as ``filter.brand``."""

    id: str
    kind: str | None = None
    order: int | None = None
    values: tuple[ConsumFilterValue, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumFilterGroup:
    """one group of facets; values are or'd inside a group, and'd across them."""

    name: str
    filters: tuple[ConsumFilter, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductFilters:
    """a typed builder for the packed ``filters`` query parameter.

    the storefront ignores unknown filter ids silently and answers with an
    unfiltered result, so listing methods take this instead of a raw string.
    """

    brands: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    leaf_categories: tuple[str, ...] = ()
    offer_immediate: bool = False
    offer_deferred: bool = False
    eco: bool = False
    own_brand: bool = False
    best_score: bool = False
    novelty: bool = False

    def render(self) -> str | None:
        """return the packed ``filters`` value, or ``None`` when empty."""

        clauses = [
            f"{filter_id}:{','.join(_filter_values(values, filter_id))}"
            for filter_id, values in (
                ("filter.brand", self.brands),
                ("filter.category", self.categories),
                ("filter.categoryLeaf", self.leaf_categories),
            )
            if values
        ]
        clauses.extend(
            f"{filter_id}:true"
            for filter_id, enabled in (
                ("filter.offerImmediate", self.offer_immediate),
                ("filter.offerDeferred", self.offer_deferred),
                ("filter.eco", self.eco),
                ("filter.ownBrand", self.own_brand),
                ("filter.bestScore", self.best_score),
                ("filter.novelty", self.novelty),
            )
            if enabled
        )
        return ";".join(clauses) or None

    def __bool__(self) -> bool:
        return self.render() is not None


def _filter_values(values: tuple[str, ...], filter_id: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ConfigurationError(f"{filter_id} values must be strings or integers")
        text = str(value).strip()
        if not text or "," in text or ";" in text or ":" in text:
            raise ConfigurationError(
                f"{filter_id} values must be non-empty and free of ',', ';', and ':'"
            )
        cleaned.append(text)
    return tuple(dict.fromkeys(cleaned))


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumStore(Store):
    """one delivery zone or pickup point returned for a postal code."""

    zone_id: int | None = None
    store_code: str | None = None
    shipping_zone_id: str | None = None
    delivery_method: str | None = None
    is_enabled: bool = True
    pickup_point: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumGroup:
    """one promotional campaign; ``code`` selects its products."""

    id: str
    code: str
    name: str
    title: str | None = None
    description: str | None = None
    slug: str | None = None
    url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_hidden: bool = False
    is_recipe: bool = False
    subgroups: tuple[ConsumGroup, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SortOption:
    """one sort order the storefront advertises."""

    id: int
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Suggestion:
    """one autocomplete suggestion, optionally carrying the tag behind it."""

    query: str
    tag: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumSearchResult(SearchResult):
    """one page of products, with the facets and paging consum reports.

    ``next_cursor`` counts the rows the storefront returned, including any
    sponsored ones dropped from ``products``, so paging stays aligned.
    """

    offset: int = 0
    total_recipe_count: int = 0
    filter_groups: tuple[ConsumFilterGroup, ...] = ()
    dropped_sponsored: int = 0
    products: tuple[ConsumProduct, ...] = ()


# --------------------------------------------------------------------- parsers


def _brand(data: object) -> str | None:
    if isinstance(data, str):
        return data.strip() or None
    name = as_text(as_object(data).get("name"))
    return name.strip() or None if name is not None else None


def _photo(data: object) -> ConsumPhoto | None:
    value = as_object(data)
    url = as_text(value.get("url"))
    if url is None or not url.strip():
        return None
    return ConsumPhoto(
        url=url, kind=as_text(value.get("type")), order=as_integer(value.get("order"))
    )


def _attributes(data: object) -> tuple[ConsumAttribute, ...]:
    attributes: list[ConsumAttribute] = []
    for item in as_items(data):
        value = as_object(item)
        code = as_text(value.get("code"))
        if code is None:
            continue
        values = tuple(
            text
            for language in as_items(value.get("languages"))
            for item_value in as_items(as_object(language).get("values"))
            if (text := as_text(item_value)) is not None
        )
        attributes.append(ConsumAttribute(code=code, values=values))
    return tuple(attributes)


def _prices(data: JsonObject) -> dict[str, JsonObject]:
    prices: dict[str, JsonObject] = {}
    for item in as_items(data.get("prices")):
        value = as_object(item)
        price_id = as_text(value.get("id"))
        if price_id is not None:
            prices.setdefault(price_id, as_object(value.get("value")))
    return prices


def _price(data: object, *, is_variable_weight: bool) -> Price:
    value = as_object(data)
    prices = _prices(value)
    base = prices.get("PRICE", {})
    offer = prices.get("OFFER_PRICE")
    current = base if offer is None else offer
    amount = as_decimal(current.get("centAmount"))
    previous = as_decimal(base.get("centAmount")) if offer is not None else None
    discount: Decimal | None = None
    if previous is not None and amount is not None and previous > 0:
        discount = ((previous - amount) / previous * _HUNDRED).quantize(Decimal("0.01"))
    unit_price = as_decimal(current.get("centUnitAmount"))
    unit_price_unit = as_text(value.get("unitPriceUnitType")) or None
    return Price(
        amount=amount,
        previous=previous,
        unit_price=unit_price,
        unit_price_unit=unit_price_unit,
        reference=_UNITS.unit_price(unit_price, unit_price_unit),
        tax_percentage=as_decimal(value.get("taxPercentage")),
        is_discounted=offer is not None,
        discount_percentage=discount,
        is_approximate=is_variable_weight,
    )


def _availability(
    product_data: JsonObject, price_data: JsonObject
) -> ConsumAvailability:
    status = as_text(product_data.get("availability"))
    out_of_stock = as_boolean(product_data.get("temporaryOutOfStock")) or False
    return ConsumAvailability(
        available=None if status is None else status == "1" and not out_of_stock,
        status=status,
        min_quantity=as_decimal(price_data.get("minimumUnit")),
        max_quantity=as_decimal(price_data.get("maximumUnit")),
        increment=as_decimal(price_data.get("intervalUnit")),
        temporarily_out_of_stock=out_of_stock,
    )


def parse_offer(data: object) -> ConsumOffer:
    """parse one entry of a product's ``offers`` or ``coupons`` array."""

    value = as_object(data)
    # the wire spelling really is "inmediate"; do not correct it here.
    immediate = as_boolean(value.get("inmediate")) or False
    amount = as_decimal(value.get("amount"))
    long_description = as_text(value.get("longDescription"))
    short_description = as_text(value.get("shortDescription"))
    return ConsumOffer(
        id=as_identifier(value.get("id"), "offer id"),
        description=long_description or short_description,
        kind="immediate" if immediate else "deferred",
        price=amount if immediate else None,
        starts_at=as_iso_datetime(value.get("from")),
        ends_at=as_iso_datetime(value.get("to")),
        promotion_id=(
            None
            if value.get("promotionId") is None
            else as_identifier(value.get("promotionId"), "promotion id")
        ),
        min_description=as_text(value.get("minDescription")),
        short_description=short_description,
        long_description=long_description,
        image_url=as_text(value.get("image")),
        promotion_type=as_integer(value.get("promotionType")),
        target_type=as_integer(value.get("applicationTargetType")),
        action_type=as_integer(value.get("applicationActionType")),
        is_immediate=immediate,
        amount=amount,
        picto_type=as_text(value.get("pictoType")),
    )


def _leaf_category(data: object) -> ConsumCategory:
    value = as_object(data)
    return ConsumCategory(
        id=as_identifier(value.get("id"), "category id"),
        name=required_text(value.get("name"), "category name"),
        kind=as_integer(value.get("type")),
    )


def parse_product(data: object) -> ConsumProduct:
    """parse one product from a listing row or the product-detail endpoint."""

    value = as_object(data)
    code = as_identifier(value.get("code"), "product code")
    product_data = as_object(value.get("productData"))
    name = required_text(product_data.get("name"), "product name")
    price_data = as_object(value.get("priceData"))
    product_type = as_integer(value.get("productType"))
    is_variable_weight = product_type in _WEIGHT_KINDS
    photos = tuple(
        photo for item in as_items(value.get("media")) if (photo := _photo(item))
    )
    categories = tuple(
        _leaf_category(item) for item in as_items(value.get("categories"))
    )
    offers = tuple(parse_offer(item) for item in as_items(value.get("offers")))
    pack_format = as_text(product_data.get("format")) or None
    return ConsumProduct(
        id=code,
        name=name,
        brand=_brand(product_data.get("brand")),
        ean=as_text(value.get("ean")) or None,
        slug=as_text(product_data.get("seo")),
        url=as_text(product_data.get("url")),
        pack_size_text=pack_format,
        thumbnail=photos[0] if photos else None,
        photos=photos,
        price=_price(price_data, is_variable_weight=is_variable_weight),
        availability=_availability(product_data, price_data),
        category_ids=tuple(category.id for category in categories),
        categories=categories,
        category_path=categories,
        promotions=offers,
        offers=offers,
        coupons=tuple(parse_offer(item) for item in as_items(value.get("coupons"))),
        description=as_text(product_data.get("description")),
        is_new=as_boolean(product_data.get("novelty")) or False,
        is_sponsored=as_boolean(product_data.get("sponsored")) or False,
        is_variable_weight=is_variable_weight,
        internal_id=(
            None
            if value.get("id") is None
            else as_identifier(value.get("id"), "internal product id")
        ),
        product_type=product_type,
        pack_format=pack_format,
        is_featured=as_boolean(product_data.get("featured")) or False,
        contains_allergens=(
            as_boolean(product_data.get("containAllergensIntolernacies")) or False
        ),
        allows_comments=(
            as_boolean(as_object(value.get("purchaseData")).get("allowComments"))
            or False
        ),
        attributes=_attributes(product_data.get("attributes")),
    )


def parse_category(data: object, *, parent_id: str | None = None) -> ConsumCategory:
    """parse one node of the category tree and every node beneath it."""

    value = as_object(data)
    category_id = as_identifier(value.get("id"), "category id")
    return ConsumCategory(
        id=category_id,
        name=required_text(value.get("name"), "category name"),
        parent_id=parent_id,
        level=as_integer(value.get("level")),
        slug=as_text(value.get("seo")),
        image_url=as_text(value.get("iconUrl")),
        url=as_text(value.get("url")),
        kind=as_integer(value.get("type")),
        children=tuple(
            parse_category(item, parent_id=category_id)
            for item in as_items(value.get("subcategories"))
        ),
    )


def parse_filter_groups(data: object) -> tuple[ConsumFilterGroup, ...]:
    """parse the facet block, dropping the blank value the brand facet carries."""

    groups: list[ConsumFilterGroup] = []
    for item in as_items(data):
        group = as_object(item)
        name = as_text(group.get("groupName"))
        if name is None:
            continue
        filters: list[ConsumFilter] = []
        for filter_item in as_items(group.get("filters")):
            filter_value = as_object(filter_item)
            filter_id = as_text(filter_value.get("id"))
            if filter_id is None:
                continue
            values = tuple(
                ConsumFilterValue(
                    id=value_id,
                    name=as_text(entry.get("name")) or value_id,
                    count=as_integer(entry.get("count")),
                )
                for value in as_items(filter_value.get("values"))
                if (entry := as_object(value))
                and (value_id := (as_text(entry.get("id")) or "").strip())
            )
            filters.append(
                ConsumFilter(
                    id=filter_id,
                    kind=as_text(filter_value.get("type")),
                    order=as_integer(filter_value.get("order")),
                    values=values,
                )
            )
        groups.append(ConsumFilterGroup(name=name, filters=tuple(filters)))
    return tuple(groups)


def parse_search_result(
    data: object,
    *,
    query: str,
    offset: int,
    page_size: int,
    drop_sponsored: bool,
) -> ConsumSearchResult:
    """parse a listing envelope into one page with an offset cursor."""

    value = as_object(data)
    rows = value.get("products")
    if not isinstance(rows, list):
        raise InvalidResponseError("listing response has no products array")
    products = tuple(parse_product(item) for item in rows)
    kept = (
        tuple(item for item in products if not item.is_sponsored)
        if drop_sponsored
        else products
    )
    has_more = as_boolean(value.get("hasMore")) or False
    return ConsumSearchResult(
        query=query,
        products=kept,
        page_size=page_size,
        offset=offset,
        total_hits=as_integer(value.get("totalCount")),
        total_recipe_count=as_integer(value.get("totalRecipeCount")) or 0,
        next_cursor=str(offset + len(products)) if has_more and products else None,
        filter_groups=parse_filter_groups(value.get("filters")),
        dropped_sponsored=len(products) - len(kept),
    )


def _address(data: JsonObject) -> str | None:
    parts = [
        text
        for key in ("street", "number", "stair", "floor", "letter")
        if (text := (as_text(data.get(key)) or "").strip())
    ]
    return " ".join(parts) or None


def parse_stores(data: object) -> tuple[ConsumStore, ...]:
    """parse the shipping-area groups into one store per area."""

    stores: list[ConsumStore] = []
    for item in as_items(data):
        group = as_object(item)
        for area_item in as_items(group.get("shippingAreas")):
            area = as_object(area_item)
            zone = as_object(area.get("zone"))
            zone_id = as_integer(zone.get("id"))
            if zone_id is None:
                continue
            address = as_object(zone.get("address"))
            method = as_text(area.get("deliveryTypeId"))
            pickup = as_object(area.get("pickupPoint"))
            stores.append(
                ConsumStore(
                    id=str(zone_id),
                    name=as_text(zone.get("name")) or str(zone_id),
                    kind="pickup" if method == DeliveryMethod.SHOP else "delivery",
                    address=_address(address),
                    postal_code=as_text(address.get("zipCode")) or None,
                    city=as_text(address.get("city")) or None,
                    province=as_text(address.get("region"))
                    or as_text(group.get("groupName")),
                    latitude=_coordinate(pickup.get("latitude")),
                    longitude=_coordinate(pickup.get("longitude")),
                    zone_id=zone_id,
                    store_code=as_text(zone.get("storeCode")) or None,
                    shipping_zone_id=as_text(area.get("shippingZoneId")),
                    delivery_method=method,
                    is_enabled=as_boolean(area.get("enabled")) is not False,
                    pickup_point=as_text(pickup.get("name")) or None,
                )
            )
    return tuple(stores)


def _coordinate(value: object) -> float | None:
    number = as_decimal(value)
    return None if number is None else float(number)


def parse_group(data: object) -> ConsumGroup:
    """parse one promotional campaign and its subgroups."""

    value = as_object(data)
    attributes = {
        code: tuple(
            text
            for item in as_items(as_object(entry).get("values"))
            if (text := as_text(item)) is not None
        )
        for entry in as_items(value.get("attributes"))
        if (code := as_text(as_object(entry).get("groupAttributeTypeId"))) is not None
    }
    return ConsumGroup(
        id=as_identifier(value.get("id"), "group id"),
        code=required_text(value.get("code"), "group code"),
        name=required_text(value.get("name"), "group name"),
        title=as_text(value.get("title")) or None,
        description=as_text(value.get("description")) or None,
        slug=as_text(value.get("seo")) or None,
        url=as_text(value.get("url")) or None,
        starts_at=as_iso_datetime(value.get("from")),
        ends_at=as_iso_datetime(value.get("to")),
        is_hidden=attributes.get("hidden", ()) == ("1",),
        is_recipe=attributes.get("recipe", ()) == ("true",),
        subgroups=tuple(parse_group(item) for item in as_items(value.get("subgroups"))),
    )


def parse_sort_option(data: object) -> SortOption:
    """parse one entry of the sort-order enumeration."""

    value = as_object(data)
    identifier = as_integer(value.get("id"))
    if identifier is None:
        raise InvalidResponseError("response has no usable sort order id")
    return SortOption(
        id=identifier,
        label=required_text(value.get("label"), "sort order label"),
        description=as_text(value.get("description")) or None,
    )


def parse_suggestion(data: object) -> Suggestion | None:
    """parse one autocomplete entry from either suggestion endpoint."""

    value = as_object(data)
    query = as_text(value.get("query"))
    if query is None or not query.strip():
        return None
    return Suggestion(query=query, tag=as_text(value.get("tag")) or None)
