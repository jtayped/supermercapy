"""mercadona: client, store models, and warehouse discovery."""

from .client import Mercadona
from .discovery import discover_warehouses, resolve_warehouse
from .models import (
    CatalogResult,
    HomeItem,
    HomeNotification,
    MercadonaAvailability,
    MercadonaCategory,
    MercadonaHomeSection,
    MercadonaPhoto,
    MercadonaPrice,
    MercadonaProduct,
    MercadonaProductSummary,
    MercadonaSearchResult,
    PhotoFit,
    ProductDetails,
    Season,
    SeasonSummary,
)

__all__ = [
    "CatalogResult",
    "HomeItem",
    "HomeNotification",
    "Mercadona",
    "MercadonaAvailability",
    "MercadonaCategory",
    "MercadonaHomeSection",
    "MercadonaPhoto",
    "MercadonaPrice",
    "MercadonaProduct",
    "MercadonaProductSummary",
    "MercadonaSearchResult",
    "PhotoFit",
    "ProductDetails",
    "Season",
    "SeasonSummary",
    "discover_warehouses",
    "resolve_warehouse",
]
