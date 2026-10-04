"""bonpreu's models and parsers, built on the shared core models.

the storefront runs on ocado smart platform, which publishes one decorated
product shape for listings and a richer envelope for the product sheet. both
parse into :class:`BonpreuProduct`; a listing row simply leaves the sheet-only
fields empty.

two things are worth knowing before reading a product. there is no ean, gtin
or barcode anywhere in the api, so ``ean`` is permanently ``None`` and
``retailer_product_id`` is the only public id. and everything descriptive —
ingredients, the nutrition table, storage, legal text — arrives as an html
fragment inside :attr:`BonpreuProduct.fields`, so each field is kept both raw
and stripped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .._core.models import (
    Category,
    Nutrition,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
)
from .._core.units import UnitReader
from .._platforms import ocado
from ._constants import UNIT_NAMES

__all__ = [
    "BonpreuCatchweight",
    "BonpreuCategory",
    "BonpreuField",
    "BonpreuFilterAttribute",
    "BonpreuFilterGroup",
    "BonpreuPhoto",
    "BonpreuPrice",
    "BonpreuProduct",
    "BonpreuPromotion",
    "BonpreuQuantity",
    "BonpreuSearchResult",
    "ProductType",
    "PromotionType",
    "SortOption",
    "encode_filters",
    "next_page_token",
    "parse_categories",
    "parse_category",
    "parse_decorated",
    "parse_detail",
    "parse_nutrition",
    "parse_product",
    "parse_product_ids",
    "parse_search_result",
    "parse_suggestions",
]

# the "before" price is prose in a promotion description, in either language
_PREVIOUS_PRICE = re.compile(r"^\s*(?:abans|antes)\b", re.IGNORECASE)
# a unit price names its unit twice, as a message key and as a unit name; the
# promotions listing sends the key alone, so both spellings are read
_UNITS = UnitReader(
    {
        "fop.price.per.each": "ud",
        "fop.price.per.kg": "kg",
        "fop.price.per.litre": "l",
        "fop.price.per.100ml": "100 ml",
        "fop.price.per.dozen": "docena",
        "per_1kg": "kg",
        "per_litre": "l",
        "per_100ml": "100 ml",
        "per_dozen": "docena",
    }
)


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
class BonpreuPhoto(Photo):
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
class BonpreuPrice(Price):
    """one price, with the unit its reference price is quoted in.

    :attr:`~supermercapy.Price.previous` is read out of a promotion description
    such as ``"Abans 0,58€"``, because no numeric field carries it.

    ``unit_price`` is the regular unit price even while a promotion sets
    ``amount``; ``reference`` follows ``amount`` and reads the promotional
    unit price on a promoted row.
    """

    unit_name: str | None = None
    unit_message_key: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonpreuQuantity:
    """one quantity of a catchweight range."""

    value: Decimal | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonpreuCatchweight:
    """the weight range a catchweight product is picked within.

    a catchweight item is billed on what was actually picked, so its
    :attr:`~supermercapy.Price.amount` is the typical price rather than the final
    one and :attr:`~supermercapy.ProductSummary.is_variable_weight` is set.
    """

    minimum: BonpreuQuantity | None = None
    maximum: BonpreuQuantity | None = None
    typical: BonpreuQuantity | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonpreuField:
    """one entry of a product sheet, raw and stripped."""

    title: str
    content: str
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BonpreuPromotion(Promotion):
    """one promotion, with the retailer-side identifiers it also carries."""

    retailer_id: str | None = None
    presentation_mode: str | None = None
    limit_reached: bool = False
    is_multi_buy: bool = False
    long_description: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonpreuCategory(Category):
    """one category node, keyed by uuid and by its hierarchical retailer code."""

    retailer_category_id: str | None = None
    children: tuple[BonpreuCategory, ...] = ()
    products: tuple[BonpreuProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BonpreuFilterAttribute:
    """one value a listing can be narrowed to."""

    id: str
    label: str | None = None
    selected: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class BonpreuFilterGroup:
    """one group of listing filters; groups are anded, values within one ored."""

    id: str
    label: str | None = None
    kind: str | None = None
    attributes: tuple[BonpreuFilterAttribute, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BonpreuProduct(Product):
    """one product, from a listing row or from the full product sheet.

    ``ean`` is always ``None``: the storefront publishes no barcode of any
    kind. :attr:`product_uuid` is the internal id the similar, related and
    batch endpoints speak, while :attr:`~supermercapy.ProductSummary.id` is the
    numeric ``retailerProductId`` the urls and the sheet endpoint take.
    """

    product_uuid: str | None = None
    product_type: ProductType = ProductType.UNKNOWN
    catchweight: BonpreuCatchweight | None = None
    fields: tuple[BonpreuField, ...] = ()
    price: BonpreuPrice = BonpreuPrice()
    thumbnail: BonpreuPhoto | None = None
    photos: tuple[BonpreuPhoto, ...] = ()
    promotions: tuple[BonpreuPromotion, ...] = ()
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
class BonpreuSearchResult(SearchResult):
    """one page of products, continued with an opaque page token.

    the token is session scoped, so the next page must be fetched by the same
    client; :attr:`~supermercapy.SearchResult.total_hits` is never published.
    """

    products: tuple[BonpreuProduct, ...] = ()
    categories: tuple[BonpreuCategory, ...] = ()
    filters: tuple[BonpreuFilterGroup, ...] = ()
    sort_options: tuple[str, ...] = ()


# --------------------------------------------------------------------- parsers

PARSER: ocado.Parser[BonpreuProduct, BonpreuCategory, BonpreuSearchResult] = (
    ocado.Parser(
        ocado.Models(
            photo=BonpreuPhoto,
            price=BonpreuPrice,
            quantity=BonpreuQuantity,
            catchweight=BonpreuCatchweight,
            field=BonpreuField,
            promotion=BonpreuPromotion,
            category=BonpreuCategory,
            filter_attribute=BonpreuFilterAttribute,
            filter_group=BonpreuFilterGroup,
            product=BonpreuProduct,
            search_result=BonpreuSearchResult,
            product_type=ProductType,
        ),
        units=_UNITS,
        unit_names=UNIT_NAMES,
        previous_price=_PREVIOUS_PRICE,
    )
)


def encode_filters(filters: object) -> str:
    """serialise a filter selection the way the storefront's own helper does.

    the mapping is flattened to ``group=value,value&group=value`` with every
    key and value percent-encoded, and that whole string then travels as the
    value of one ``filters`` parameter, so it is encoded a second time on the
    wire. brand ids hold spaces and end up ``%2520``; build the value here
    rather than by hand.
    """

    return ocado.encode_filters(filters)


def parse_product(data: object, *, group_type: str | None = None) -> BonpreuProduct:
    """parse one decorated product, as listings and the batch endpoint serve it."""

    return PARSER.product(data, group_type=group_type)


def parse_nutrition(fields: tuple[BonpreuField, ...]) -> Nutrition | None:
    """parse the sheet's nutrition table, ingredients, and allergens.

    the table is an html fragment whose first row names the serving the
    columns are quoted per; every other row becomes one
    :class:`~supermercapy.NutritionValue`. the raw markup is kept so a caller can
    reread a table this parser could not make sense of.
    """

    return ocado.parse_nutrition(fields)


def parse_detail(data: object) -> BonpreuProduct:
    """parse the product-sheet envelope into one fully populated product."""

    return PARSER.detail(data)


def parse_categories(data: object) -> tuple[BonpreuCategory, ...]:
    """parse the whole category tree, which arrives as a bare json array."""

    return PARSER.categories(data)


def next_page_token(data: object) -> str | None:
    """return the cursor that continues a listing, or ``None`` at the end."""

    return ocado.next_page_token(data)


def parse_search_result(
    data: object, *, query: str, page_size: int
) -> BonpreuSearchResult:
    """parse a listing envelope into one page and its continuation token."""

    return PARSER.search_result(data, query=query, page_size=page_size)


def parse_category(
    data: object, *, page: BonpreuSearchResult
) -> BonpreuCategory | None:
    """parse the category a listing reports it served, with its page attached.

    the node the listing echoes back carries no children of its own; the
    child categories, and the only real product counts the storefront
    publishes, travel beside it on the page.
    """

    return PARSER.category(data, products=page.products, children=page.categories)


def parse_product_ids(data: object) -> tuple[str, ...]:
    """parse the bare uuid array the similar and related endpoints return."""

    return ocado.parse_product_ids(data)


def parse_decorated(data: object) -> tuple[BonpreuProduct, ...]:
    """parse the batch decorate response into products."""

    return PARSER.decorated(data)


def parse_suggestions(data: object) -> tuple[str, ...]:
    """parse the autocomplete response, a flat array of strings."""

    return ocado.parse_suggestions(data)
