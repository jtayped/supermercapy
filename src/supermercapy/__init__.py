"""supermercapy's public api: one typed client per spanish supermarket."""

import logging

from ._core.capabilities import Capability
from ._core.client import BaseClient, RetryPolicy
from ._core.exceptions import (
    AuthenticationError,
    BlockedError,
    ChallengedError,
    ConfigurationError,
    InvalidResponseError,
    NotAvailableError,
    NotFoundError,
    OutOfCoverageError,
    RateLimitError,
    SupermercapyError,
    TransportError,
    UnsupportedOperationError,
)
from ._core.models import (
    Availability,
    Category,
    HomeSection,
    Language,
    Nutrition,
    NutritionValue,
    Photo,
    Price,
    Product,
    ProductSummary,
    Promotion,
    SearchResult,
    Store,
)
from ._core.serialize import to_dict, to_json
from ._core.units import Unit, UnitPrice
from ._multi import SearchAllResult, SearchStatus, StoreSearch, search_all
from ._version import __version__
from .ahorramas import Ahorramas
from .aio import AsyncClient
from .alcampo import Alcampo
from .aldi import Aldi
from .bonarea import Bonarea
from .bonpreu import Bonpreu
from .cache import CacheTransport
from .caprabo import Caprabo
from .carrefour import Carrefour
from .condis import Condis
from .consum import Consum
from .dia import Dia
from .eroski import Eroski
from .lidl import Lidl
from .match import (
    Listing,
    Match,
    MatchKind,
    ProductGroup,
    find_alternatives,
    find_same,
    group_same,
    pair_same,
    score_alternative,
    score_same,
)
from .mercadona import Mercadona
from .plusfresc import Plusfresc

logging.getLogger("supermercapy").addHandler(logging.NullHandler())

ALL_CLIENTS: tuple[type[BaseClient], ...] = (
    Mercadona,
    Consum,
    Plusfresc,
    Bonarea,
    Carrefour,
    Lidl,
    Bonpreu,
    Dia,
    Eroski,
    Caprabo,
    Aldi,
    Ahorramas,
    Alcampo,
    Condis,
)
"""every store client, in the order they were added to the package."""

__all__ = [
    "ALL_CLIENTS",
    "Ahorramas",
    "Alcampo",
    "Aldi",
    "AsyncClient",
    "AuthenticationError",
    "Availability",
    "BaseClient",
    "BlockedError",
    "Bonarea",
    "Bonpreu",
    "CacheTransport",
    "Capability",
    "Caprabo",
    "Carrefour",
    "Category",
    "ChallengedError",
    "Condis",
    "ConfigurationError",
    "Consum",
    "Dia",
    "Eroski",
    "HomeSection",
    "InvalidResponseError",
    "Language",
    "Lidl",
    "Listing",
    "Match",
    "MatchKind",
    "Mercadona",
    "NotAvailableError",
    "NotFoundError",
    "Nutrition",
    "NutritionValue",
    "OutOfCoverageError",
    "Photo",
    "Plusfresc",
    "Price",
    "Product",
    "ProductGroup",
    "ProductSummary",
    "Promotion",
    "RateLimitError",
    "RetryPolicy",
    "SearchAllResult",
    "SearchResult",
    "SearchStatus",
    "Store",
    "StoreSearch",
    "SupermercapyError",
    "TransportError",
    "Unit",
    "UnitPrice",
    "UnsupportedOperationError",
    "__version__",
    "find_alternatives",
    "find_same",
    "group_same",
    "pair_same",
    "score_alternative",
    "score_same",
    "search_all",
    "to_dict",
    "to_json",
]
