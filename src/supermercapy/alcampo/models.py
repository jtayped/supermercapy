"""alcampo's models and parsers, built on the shared core models.

the storefront runs on ocado smart platform, as bonpreu's does, and the two
share their parsers (see ``supermercapy._platforms.ocado``). a listing row and
the product sheet both parse into :class:`AlcampoProduct`; a listing row simply
leaves the sheet-only fields empty.

there is no ean, gtin or barcode anywhere in the api, so ``ean`` is always
``None`` and ``retailer_product_id`` is the only public id. everything
descriptive — ingredients, the nutrition table, storage, legal text — arrives as
an html fragment inside :attr:`AlcampoProduct.fields`, kept both raw and
stripped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .._core.coerce import (
    JsonObject,
    as_decimal,
    as_integer,
    as_items,
    as_object,
    as_text,
)
from .._core.html import table_rows
from .._core.models import (
    Category,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
    Store,
)
from .._core.units import UnitReader
from .._platforms import ocado
from ._constants import UNIT_NAMES

__all__ = [
    "PARSER",
    "AlcampoCatchweight",
    "AlcampoCategory",
    "AlcampoField",
    "AlcampoFilterAttribute",
    "AlcampoFilterGroup",
    "AlcampoPhoto",
    "AlcampoPrice",
    "AlcampoProduct",
    "AlcampoPromotion",
    "AlcampoQuantity",
    "AlcampoSearchResult",
    "AlcampoStore",
    "ProductType",
    "PromotionType",
    "SortOption",
    "parse_stores",
]

# a unit price names its unit twice, as a message key and as a unit name; the
# promotions listing sends the key alone, so both spellings are read. the
# per-gram price is rounded to the cent, two orders of magnitude coarser than
# the figure it stands for, so it is left unread rather than multiplied up.
_UNITS = UnitReader(
    {
        "fop.price.per.each": "ud",
        "fop.price.per.kg": "kg",
        "fop.price.per.litre": "l",
        "fop.price.per.dozen": "docena",
        "fop.price.per.gram": None,
        "per_1kg": "kg",
        "per_litre": "l",
        "per_dozen": "docena",
    }
)
# the sheet's "features" table names the legal denomination and the country of
# origin in rows of its own
FEATURES_FIELD = "features"
LEGAL_NAME_FEATURE = "Denominación legal del alimento"
ORIGIN_FEATURE = "País de origen"
_POSTAL_CODE = re.compile(r"\d{5}")


class ProductType(StrEnum):
    """how a product is sold, which decides whether its price is exact."""

    REGULAR = "REGULAR"
    CATCHWEIGHT = "CATCHWEIGHT"
    VARIABLE_PRICE = "VARIABLE_PRICE"
    UNKNOWN = "UNKNOWN"


class PromotionType(StrEnum):
    """the kinds of promotion a product can carry."""

    LOYALTY = "LOYALTY"
    OFFER = "OFFER"
    COUPON = "COUPON"
    DELIVERY = "DELIVERY"
    FREE_GIFT = "FREE_GIFT"
    PAYMENT_PROMOTION = "PAYMENT_PROMOTION"
    UNKNOWN = "UNKNOWN"


class SortOption(StrEnum):
    """the orderings a listing accepts as ``sortOptionId``."""

    FAVORITE = "favorite"
    PRICE_ASCENDING = "priceAscending"
    PRICE_DESCENDING = "priceDescending"
    UNIT_PRICE_ASCENDING = "pricePerAscending"
    UNIT_PRICE_DESCENDING = "pricePerDescending"


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoPhoto(Photo):
    """one product image, addressable at any of the square renditions.

    the sizes are path segments rather than query parameters, so
    :meth:`sized` rebuilds the url from :attr:`base_url` instead of appending
    to :attr:`~supermercapy.Photo.url`.
    """

    image_id: str | None = None
    base_url: str | None = None

    def sized(self, size: int | str = 500, *, image_format: str = "jpg") -> str:
        """return this image's url at one of the twelve square renditions."""

        return ocado.sized_url(self.url, self.base_url, size, image_format)


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoPrice(Price):
    """one price, with the unit its reference price is quoted in.

    ``unit_price`` and ``reference`` follow ``amount``: on a row whose
    promotional price replaces the shelf price, both read the promotional
    unit price.
    """

    unit_name: str | None = None
    unit_message_key: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoQuantity:
    """one quantity of a catchweight range."""

    value: Decimal | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoCatchweight:
    """the weight range a catchweight product is picked within.

    a catchweight item is billed on what was actually picked, so its
    :attr:`~supermercapy.Price.amount` is the typical price rather than the
    final one and :attr:`~supermercapy.ProductSummary.is_variable_weight` is
    set.
    """

    minimum: AlcampoQuantity | None = None
    maximum: AlcampoQuantity | None = None
    typical: AlcampoQuantity | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoField:
    """one entry of a product sheet, raw and stripped."""

    title: str
    content: str
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoPromotion(Promotion):
    """one promotion, with the retailer-side identifiers it also carries.

    the validity window, where alcampo states one, is part of
    :attr:`~supermercapy.Promotion.description`, as in
    ``"Producto en Folleto (24/09/26_07/10/26)"``.
    """

    retailer_id: str | None = None
    presentation_mode: str | None = None
    limit_reached: bool = False
    is_multi_buy: bool = False
    long_description: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoCategory(Category):
    """one category node, keyed by uuid and by its retailer code."""

    retailer_category_id: str | None = None
    children: tuple[AlcampoCategory, ...] = ()
    products: tuple[AlcampoProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoFilterAttribute:
    """one value a listing can be narrowed to."""

    id: str
    label: str | None = None
    selected: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoFilterGroup:
    """one group of listing filters; groups are anded, values within one ored."""

    id: str
    label: str | None = None
    kind: str | None = None
    attributes: tuple[AlcampoFilterAttribute, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoProduct(Product):
    """one product, from a listing row or from the full product sheet.

    ``ean`` is always ``None``: the storefront publishes no barcode of any
    kind. :attr:`product_uuid` is the internal id the similar, related and
    batch endpoints speak, while :attr:`~supermercapy.ProductSummary.id` is
    the numeric ``retailerProductId`` the urls and the sheet endpoint take.
    :attr:`rating` is the average of :attr:`rating_count` shopper reviews, and
    ``None`` while there are none.
    """

    product_uuid: str | None = None
    product_type: ProductType = ProductType.UNKNOWN
    catchweight: AlcampoCatchweight | None = None
    fields: tuple[AlcampoField, ...] = ()
    price: AlcampoPrice = AlcampoPrice()
    thumbnail: AlcampoPhoto | None = None
    photos: tuple[AlcampoPhoto, ...] = ()
    promotions: tuple[AlcampoPromotion, ...] = ()
    rating: Decimal | None = None
    rating_count: int | None = None
    is_in_current_catalog: bool | None = None
    is_time_restricted: bool = False
    group_type: str | None = None
    campaign_id: str | None = None
    campaign_name: str | None = None
    external_advert_id: str | None = None

    def field(self, title: str) -> str | None:
        """return one sheet field's stripped text, or ``None`` when absent."""

        return ocado.find_field(self.fields, title, raw=False)

    def raw_field(self, title: str) -> str | None:
        """return one sheet field's html exactly as it was published."""

        return ocado.find_field(self.fields, title, raw=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoSearchResult(SearchResult):
    """one page of products, continued with an opaque page token.

    the token is session scoped, so the next page must be fetched by the same
    client; :attr:`~supermercapy.SearchResult.total_hits` is never published.
    """

    products: tuple[AlcampoProduct, ...] = ()
    categories: tuple[AlcampoCategory, ...] = ()
    filters: tuple[AlcampoFilterGroup, ...] = ()
    sort_options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AlcampoStore(Store):
    """one click-and-collect point, and the region that prices it.

    :attr:`~supermercapy.Store.id` is the point's delivery destination id and
    :attr:`region_id` the fulfilment region behind it; several points share a
    region, and the region is what prices and assortment follow.
    :attr:`~supermercapy.Store.kind` is ``"SERVICED"`` for a counter in a
    shop and ``"LOCKER"`` for a locker bank.
    """

    region_id: str
    address_id: str | None = None


# --------------------------------------------------------------------- parsers


def _sheet(fields: tuple[ocado.FieldLike, ...]) -> dict[str, object]:
    """read the legal name and the origin out of the "features" table."""

    features = {
        row[0]: row[1]
        for row in table_rows(ocado.find_field(fields, FEATURES_FIELD, raw=True) or "")
        if len(row) >= 2 and row[0] and row[1]
    }
    return {
        "legal_name": features.get(LEGAL_NAME_FEATURE),
        "origin": features.get(ORIGIN_FEATURE),
    }


def _rating(data: JsonObject) -> dict[str, object]:
    summary = as_object(data.get("ratingSummary"))
    count = as_integer(summary.get("count"))
    rating = as_decimal(summary.get("overallRating"))
    return {"rating": rating if count else None, "rating_count": count}


PARSER: ocado.Parser[AlcampoProduct, AlcampoCategory, AlcampoSearchResult] = (
    ocado.Parser(
        ocado.Models(
            photo=AlcampoPhoto,
            price=AlcampoPrice,
            quantity=AlcampoQuantity,
            catchweight=AlcampoCatchweight,
            field=AlcampoField,
            promotion=AlcampoPromotion,
            category=AlcampoCategory,
            filter_attribute=AlcampoFilterAttribute,
            filter_group=AlcampoFilterGroup,
            product=AlcampoProduct,
            search_result=AlcampoSearchResult,
            product_type=ProductType,
        ),
        units=_UNITS,
        unit_names=UNIT_NAMES,
        extras=_rating,
        sheet=_sheet,
    )
)
"""the shared ocado parsers, wired to alcampo's models."""


def _store(data: object) -> AlcampoStore | None:
    value = as_object(data)
    identifier = as_text(value.get("deliveryDestinationId"))
    region = as_text(value.get("resolvedRegionId"))
    name = as_text(value.get("name"))
    if not identifier or not region or not name or not name.strip():
        return None
    # a point with no postal code sends the literal text "EMPTY"
    postal_code = as_text(value.get("postalCode"))
    coordinates = as_object(value.get("coordinates"))
    latitude = as_decimal(coordinates.get("latitude"))
    longitude = as_decimal(coordinates.get("longitude"))
    return AlcampoStore(
        id=identifier,
        name=name.strip(),
        kind=as_text(value.get("collectionPointType")),
        address=as_text(value.get("formattedAddress")),
        postal_code=postal_code if _POSTAL_CODE.fullmatch(postal_code or "") else None,
        latitude=None if latitude is None else float(latitude),
        longitude=None if longitude is None else float(longitude),
        region_id=region,
        address_id=as_text(value.get("addressId")),
    )


def parse_stores(data: object) -> tuple[AlcampoStore, ...]:
    """parse the click-and-collect points, skipping any without a region."""

    return tuple(
        store for item in as_items(data) if (store := _store(item)) is not None
    )
