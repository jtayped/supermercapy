"""hosts, paths, and the static ids the dia storefront is keyed on."""

from __future__ import annotations

from types import MappingProxyType

from .._core.models import Language

API_URL = "https://www.dia.es/api/v1"
# listing rows and product sheets give image and page paths relative to this
SITE_URL = "https://www.dia.es"

SEARCH_PATH = "/search-back/search/reduced"
MENU_PATH = "/common-aggregator/menu-data"
CHECK_SERVICE_PATH = "/common-aggregator/check-service"
SHIPPING_ADDRESS_PATH = "/common-aggregator/save-shipping-address"
LOCALE_PATH = "/common-aggregator/current/locale"
LISTING_PATH = "/plp-back"
PRODUCT_PATH = "/pdp-back"

LOCALES = MappingProxyType(
    {Language.SPANISH: "es", Language.CATALAN: "ca", Language.ENGLISH: "en"}
)
"""the locale each language is stored as on the session; ``es`` is the default."""

# the search answers any page size from thirty to a thousand and silently
# raises a smaller one to thirty; a `page` above fifty is refused by the edge
# with an html "bloqueado" page, so a deep page is reached with a wider one
MIN_SEARCH_PAGE_SIZE = 30
MAX_SEARCH_PAGE_SIZE = 1000
MAX_SEARCH_PAGE = 50

# "novedades", a child of "novedades y recomendados": the storefront's own
# listing of new products, every row of it stamped "novedad". pinned rather
# than found by name, since the name is translated.
NEW_ARRIVALS_CATEGORY_ID = "L2302"
# the stamp code behind the translated "novedad" badge on a listing row
NEW_STAMP_CODE = "2"

# the session cookie lapses an hour after its last use and the server then
# starts a fresh session on the default postcode and locale, so a bound or
# translated client restates both well inside that hour
SESSION_TTL = 45 * 60.0
