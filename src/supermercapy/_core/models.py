"""immutable values shared by every store client.

store packages subclass these models to add fields the store exposes. every
model is a frozen, slotted, keyword-only dataclass; collections are tuples,
ids are strings, and money and quantities use ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .coerce import JsonObject
from .units import UnitPrice

__all__ = [
    "Availability",
    "Category",
    "HomeSection",
    "JsonObject",
    "Language",
    "Nutrition",
    "NutritionValue",
    "Photo",
    "Price",
    "Product",
    "ProductSummary",
    "Promotion",
    "SearchResult",
    "Store",
]


class Language(StrEnum):
    """storefront languages; each client declares which ones it supports."""

    SPANISH = "es"
    CATALAN = "ca"
    ENGLISH = "en"
    VALENCIAN = "vl"


@dataclass(frozen=True, slots=True, kw_only=True)
class Price:
    """what one selling unit costs now, plus its unit price.

    ``unit_price``, ``unit_price_unit`` and ``unit_price_text`` keep the
    store's own figure, unit and wording. ``reference`` restates that figure
    per one kilogram, litre, piece, dose, metre or square metre, so prices
    compare across stores; ``None`` means the store published no unit price
    the package can read, not that it is zero.
    """

    amount: Decimal | None = None
    previous: Decimal | None = None
    unit_price: Decimal | None = None
    unit_price_unit: str | None = None
    unit_price_text: str | None = None
    reference: UnitPrice | None = None
    currency: str = "EUR"
    tax_percentage: Decimal | None = None
    is_discounted: bool = False
    discount_percentage: Decimal | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_approximate: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Availability:
    """whether a product can be bought and in what quantities."""

    available: bool | None = None
    status: str | None = None
    max_quantity: Decimal | None = None
    min_quantity: Decimal | None = None
    increment: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Photo:
    """one product image addressed by its full url."""

    url: str
    kind: str | None = None
    alt: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("photo url must be a non-empty string")


@dataclass(frozen=True, slots=True, kw_only=True)
class Promotion:
    """one offer attached to a product."""

    id: str | None = None
    description: str | None = None
    kind: str | None = None
    price: Decimal | None = None
    requires_quantity: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    member_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class NutritionValue:
    """one row of a nutrition table."""

    name: str
    per_100: str | None = None
    per_serving: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Nutrition:
    """ingredient, allergen, and nutrition-table data for a product."""

    ingredients: str | None = None
    allergens: str | None = None
    values: tuple[NutritionValue, ...] = ()
    per: str | None = None
    nutri_score: str | None = None
    raw_html: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Category:
    """one category node with optional children and product summaries."""

    id: str
    name: str
    parent_id: str | None = None
    level: int | None = None
    slug: str | None = None
    image_url: str | None = None
    product_count: int | None = None
    children: tuple[Category, ...] = ()
    products: tuple[ProductSummary, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductSummary:
    """partial product data returned by listing operations."""

    id: str
    name: str
    brand: str | None = None
    ean: str | None = None
    slug: str | None = None
    url: str | None = None
    pack_size_text: str | None = None
    thumbnail: Photo | None = None
    price: Price = Price()
    availability: Availability = Availability()
    category_ids: tuple[str, ...] = ()
    promotions: tuple[Promotion, ...] = ()
    is_new: bool = False
    is_sponsored: bool = False
    is_variable_weight: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Product(ProductSummary):
    """complete product data returned by a product-detail operation."""

    photos: tuple[Photo, ...] = ()
    description: str | None = None
    legal_name: str | None = None
    origin: str | None = None
    storage: str | None = None
    usage: str | None = None
    category_path: tuple[Category, ...] = ()
    nutrition: Nutrition | None = None
    requires_age_check: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResult:
    """one page of product summaries with an opaque continuation cursor."""

    query: str
    products: tuple[ProductSummary, ...]
    page_size: int
    total_hits: int | None = None
    next_cursor: str | None = None
    truncated: bool = False

    @property
    def has_more(self) -> bool:
        """whether passing ``next_cursor`` back would fetch another page."""

        return self.next_cursor is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class Store:
    """one physical store, warehouse, or delivery zone."""

    id: str
    name: str
    kind: str | None = None
    address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    province: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class HomeSection:
    """one ordered section of a storefront home page."""

    layout: str
    title: str | None = None
    products: tuple[ProductSummary, ...] = ()
