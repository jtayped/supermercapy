"""optional operations and data shapes a store client may declare."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Flag, auto
from types import MappingProxyType


class Capability(Flag):
    """optional behaviour a store client declares on its class.

    operation capabilities correspond to one optional client method each.
    data-shape capabilities say whether a field can ever be populated for the
    store, so a ``None`` value can be told apart from "never available".
    """

    POSTAL_CODE = auto()
    STORES = auto()
    CATALOG = auto()
    EAN_LOOKUP = auto()
    NEW_ARRIVALS = auto()
    OFFERS = auto()
    HOME = auto()

    EAN = auto()
    NUTRITION = auto()
    PROMOTIONS = auto()
    FUTURE_PRICES = auto()


OPERATION_METHODS: Mapping[Capability, str] = MappingProxyType(
    {
        Capability.POSTAL_CODE: "from_postal_code",
        Capability.STORES: "list_stores",
        Capability.CATALOG: "iter_catalog",
        Capability.EAN_LOOKUP: "get_product_by_ean",
        Capability.NEW_ARRIVALS: "get_new_arrivals",
        Capability.OFFERS: "get_offers",
        Capability.HOME: "get_home",
    }
)
"""the optional client method each operation capability unlocks."""

DATA_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.EAN,
        Capability.NUTRITION,
        Capability.PROMOTIONS,
        Capability.FUTURE_PRICES,
    }
)
"""capabilities that describe response data rather than a client method."""
