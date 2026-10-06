"""aldi: client, store models, and the price regions."""

from .client import Aldi
from .models import (
    AldiCategory,
    AldiOfferGroup,
    AldiPrice,
    AldiProduct,
    AldiSearchResult,
    Region,
)

__all__ = [
    "Aldi",
    "AldiCategory",
    "AldiOfferGroup",
    "AldiPrice",
    "AldiProduct",
    "AldiSearchResult",
    "Region",
]
