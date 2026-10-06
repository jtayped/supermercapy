"""hosts, paths, static keys, and campaign ids for the lidl storefront."""

from __future__ import annotations

from types import MappingProxyType

from .._core.models import Language

API_URL = "https://www.lidl.es"
SEARCH_PATH = "/q/api/search"
DETAIL_PATH = "/p/api/detail"
CAMPAIGN_PATH = "/c"
SITEMAP_PATH = "/p/export/{country}/{language}/product_sitemap.xml.gz"

STORES_URL = "https://live.api.schwarz/odj/stores-api/v2/myapi/stores-frontend"
LEAFLETS_URL = "https://endpoints.leaflets.schwarz/v4"

# the store finder ships this key in plain text in a versioned bundle
# (/s/storesearch-frontend/26_20_3/base.js on 2026-10-04, unchanged since
# 26_17_10) and every schwarz country site sends the same one; it identifies
# the public storefront, not a person, and the api answers 401 without it. it
# may rotate with a deploy.
STORES_API_KEY = "KxboQtt40BG4VpBL16IhaRd2CXh0QbAc"

ASSORTMENT = "ES"
COUNTRY = "ES"
SEARCH_VERSION = "2.1.0"
# the search api serves this vendor type alone and has answered a plain
# ``application/json`` with http 406 since october 2026. the storefront's own
# search bundle sends exactly this value.
SEARCH_ACCEPT = "application/mindshift.search+json;version=2"
STORES_LOCALE = "es-ES"
LEAFLET_CLIENT_LOCALE = "lidl/es-ES"
STORES_PAGE_SIZE = 250

# the same locale is spelled four ways across the four backends; the search api
# wants an underscore, /p/api takes path segments, the stores api a hyphen, and
# the leaflet api a tenant prefix.
SEARCH_LOCALES = MappingProxyType({Language.SPANISH: "es_ES"})
LANGUAGE_PATHS = MappingProxyType({Language.SPANISH: "es"})

DEFAULT_FETCH_SIZE = 48
MAX_FETCH_SIZE = 1000

# grocery offer pages, verified 2026-09-14. campaign ids change rarely but they
# are not eternal; :meth:`~supermercapy.lidl.Lidl.get_campaign_products` takes any
# other ``/c/<slug>/a<id>`` page.
OFFER_CAMPAIGNS = MappingProxyType(
    {
        "current": ("ofertas-semanales", "10089449"),
        "next": ("ofertas-proxima-semana", "10088432"),
        "weekend": ("super-finde", "10089450"),
        "weekend_next": ("super-finde-proxima-semana", "10089609"),
        "permanent_cuts": ("bajadas-permanentes", "10089468"),
        "other_brands": ("tus-otras-marcas-de-siempre", "10089613"),
    }
)
