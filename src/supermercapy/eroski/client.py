"""synchronous eroski client."""

from __future__ import annotations

from .._core.capabilities import Capability
from .._core.models import Language
from .._platforms.tapestry import (
    DEFAULT_MIN_REQUEST_INTERVAL,
    PAGE_SIZE,
    TapestryClient,
)
from ._constants import SITE_URL
from .models import EroskiCategory, EroskiProduct, EroskiSearchResult


class Eroski(TapestryClient[EroskiProduct, EroskiCategory, EroskiSearchResult]):
    """a reusable synchronous client for eroski's online supermarket.

    the storefront is server-rendered html with no json api, so every method
    reads markup: listings from the twenty-tile fragments the storefront's
    own infinite scroll loads, products from their pages, and the category
    tree from the menu every full page carries. full pages weigh close to a
    megabyte and fragments about a quarter of one, so the client paces
    itself at one request a second by default.

    nothing binds. the anonymous session is pinned to one shop, ``157``
    (named bilbondo), which sets the prices and the regional range,
    and choosing another one needs an account, so ``store_id`` is ``None`` and
    every product carries the ``shop_id`` that priced it. the storefront also
    speaks basque, galician and german, which :class:`~supermercapy.Language`
    does not hold.
    """

    store_name = "eroski"
    capabilities = frozenset(
        {Capability.CATALOG, Capability.NUTRITION, Capability.PROMOTIONS}
    )
    default_page_size = PAGE_SIZE
    max_page_size = PAGE_SIZE
    default_min_request_interval = DEFAULT_MIN_REQUEST_INTERVAL
    _site_url = SITE_URL
    _product_type = EroskiProduct
    _category_type = EroskiCategory
    _result_type = EroskiSearchResult
    supported_languages = frozenset(
        {Language.SPANISH, Language.CATALAN, Language.ENGLISH}
    )
