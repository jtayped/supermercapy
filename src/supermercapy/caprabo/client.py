"""synchronous caprabo client."""

from __future__ import annotations

from .._core.capabilities import Capability
from .._core.models import Language
from .._platforms.tapestry import (
    DEFAULT_MIN_REQUEST_INTERVAL,
    PAGE_SIZE,
    TapestryClient,
)
from ._constants import SITE_URL
from .models import CapraboCategory, CapraboProduct, CapraboSearchResult


class Caprabo(TapestryClient[CapraboProduct, CapraboCategory, CapraboSearchResult]):
    """a reusable synchronous client for caprabo's online supermarket.

    caprabo belongs to the eroski group and runs the same storefront build
    on its own host: the same paths, the same category ids and the same
    markup, read by the same parsers. its catalog is its own, though, and so
    are its regional products, which carry their own ids and prices. the
    client paces itself at one request a second by default.

    nothing binds. the anonymous session is pinned to one shop, ``8284``
    (caprabo's platform), and choosing another one needs an account, so
    ``store_id`` is ``None`` and every product carries the ``shop_id`` that
    priced it. the storefront speaks spanish and catalan, and translates
    product names into catalan as well as the menu.
    """

    store_name = "caprabo"
    capabilities = frozenset(
        {Capability.CATALOG, Capability.NUTRITION, Capability.PROMOTIONS}
    )
    default_page_size = PAGE_SIZE
    max_page_size = PAGE_SIZE
    default_min_request_interval = DEFAULT_MIN_REQUEST_INTERVAL
    _site_url = SITE_URL
    _product_type = CapraboProduct
    _category_type = CapraboCategory
    _result_type = CapraboSearchResult
    supported_languages = frozenset({Language.SPANISH, Language.CATALAN})
