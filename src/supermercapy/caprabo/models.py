"""caprabo's models: the shared tapestry ones, under caprabo's own names.

caprabo and eroski run one storefront build, so the parsers live in
``supermercapy._platforms.tapestry`` and these classes only give each store its
own types.
"""

from __future__ import annotations

from dataclasses import dataclass

from .._platforms.tapestry import (
    TapestryCategory,
    TapestryProduct,
    TapestrySearchResult,
)

__all__ = ["CapraboCategory", "CapraboProduct", "CapraboSearchResult"]


@dataclass(frozen=True, slots=True, kw_only=True)
class CapraboProduct(TapestryProduct):
    """one caprabo product, from a listing tile or from its product page.

    ``shop_id`` is the shop that priced it, ``is_marketplace`` marks a third
    party seller's product, and ``is_lowered_price`` the storefront's
    lowered-price badge. a product page adds ``manufacturer``,
    ``manufacturer_address``, ``alcohol_percentage`` and ``features``, every
    characteristics box as a ``(title, text)`` pair.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class CapraboCategory(TapestryCategory):
    """one node of caprabo's category menu; ``path`` is its slug path."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CapraboSearchResult(TapestrySearchResult):
    """one listing page of about twenty caprabo products."""
