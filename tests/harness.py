"""one canned storefront per store, so generic tests can drive every client."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import httpx

from supermercapy import (
    Ahorramas,
    Alcampo,
    Aldi,
    BaseClient,
    Bonarea,
    Bonpreu,
    Caprabo,
    Carrefour,
    Condis,
    Consum,
    Dia,
    Eroski,
    Lidl,
    Mercadona,
    Plusfresc,
)
from tests.conftest import read_fixture

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass(frozen=True)
class Harness:
    """how to build a store client against a canned transport."""

    client_type: type[BaseClient]
    make: Callable[..., BaseClient]
    handler: Handler
    query: str
    product_id: str
    missing_product_id: str
    category_id: str
    module: str

    def client(self, handler: Handler | None = None, **options: Any) -> BaseClient:
        transport = httpx.MockTransport(handler or self.handler)
        return self.make(transport=transport, **options)


def _json(request: httpx.Request, data: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, request=request, json=data)


def _mercadona_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host.endswith("algolia.net"):
        return _json(request, read_fixture("mercadona", "search.json"))
    if path == "/api/postal-codes/actions/change-pc/":
        return httpx.Response(
            200, request=request, headers={"X-Customer-Wh": "mad3"}, json={}
        )
    if path == "/api/products/1001/":
        return _json(request, read_fixture("mercadona", "product_full.json"))
    if path.startswith("/api/products/"):
        return httpx.Response(404, request=request)
    if path == "/api/categories/":
        return _json(request, read_fixture("mercadona", "categories.json"))
    if path == "/api/categories/72/":
        return _json(request, read_fixture("mercadona", "category_72.json"))
    if path == "/api/categories/73/":
        return _json(request, read_fixture("mercadona", "category_73.json"))
    if path == "/api/home/":
        return _json(request, read_fixture("mercadona", "home.json"))
    if path == "/api/home/new-arrivals/":
        return _json(request, read_fixture("mercadona", "new_arrivals.json"))
    return httpx.Response(404, request=request)


def _consum_listing(request: httpx.Request) -> httpx.Response:
    """serve one canned page, and an empty last page once the offset moves."""

    listing = read_fixture("consum", "listing.json")
    if request.url.params.get("offset", "0") != "0":
        return _json(request, {**listing, "products": [], "hasMore": False})
    return _json(request, listing)


def _consum_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    prefix = "/api/rest/V1.0"
    if path == f"{prefix}/catalog/product":
        return _consum_listing(request)
    if path == f"{prefix}/catalog/product/code/7080604":
        return _json(request, read_fixture("consum", "product.json"))
    if path.startswith(f"{prefix}/catalog/product/code/"):
        # the live answer for a code that never existed, and for one the zone
        # does not carry
        return _json(request, read_fixture("consum", "error_404.json"), status=404)
    if path.startswith(f"{prefix}/catalog/product/codes/"):
        return _json(request, read_fixture("consum", "batch.json"))
    if path == f"{prefix}/shopping/category/menu":
        return _json(request, read_fixture("consum", "menu.json"))
    if path == f"{prefix}/shipping/area":
        return _json(request, read_fixture("consum", "shipping_area.json"))
    if path == f"{prefix}/catalog/orders":
        return _json(request, read_fixture("consum", "orders.json"))
    if path == f"{prefix}/catalog/group":
        return _json(request, read_fixture("consum", "groups.json"))
    if path == f"{prefix}/catalog/searcher/semantics":
        return _json(request, read_fixture("consum", "semantics.json"))
    if path == f"{prefix}/catalog/product/tag/":
        return _json(request, read_fixture("consum", "tags.json"))
    return httpx.Response(404, request=request)


def _plusfresc_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path == "/api/loginGuest/12":
        return _json(request, read_fixture("plusfresc", "guest_token.json"))
    routes = {
        "/api/categories/tree/12/Root": ("plusfresc", "tree.json"),
        "/api/categories/tree/12/010101": ("plusfresc", "tree_010101.json"),
        "/api/products/category/010101/12": ("plusfresc", "listing.json"),
        "/api/products/category/Root/12": ("plusfresc", "listing.json"),
        "/api/products/category/40/12": ("plusfresc", "listing.json"),
        "/api/products/category/Oferta2/12": ("plusfresc", "offers.json"),
        "/api/search/languages/ca/llet/products/12": ("plusfresc", "search.json"),
        "/api/productdetails/files/12/002530/ca": ("plusfresc", "product.json"),
        "/api/utils/centres": ("plusfresc", "pickup_points.json"),
    }
    fixture = routes.get(path)
    if fixture is not None:
        return _json(request, read_fixture(*fixture))
    return httpx.Response(404, request=request)


def _bonarea_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "GET" and path in {"/es", "/ca"}:
        # the language warm-up: the session cookie is what search reads
        return httpx.Response(
            200,
            request=request,
            headers={"Set-Cookie": "ASP.NET_SessionId=canned; path=/; HttpOnly"},
            html="<html lang='es'></html>",
        )
    form = parse_qs(request.content.decode(), keep_blank_values=True)
    if path.endswith("/shop/search"):
        return _json(request, read_fixture("bonarea", "search.json"))
    if path.endswith("/shop/Article"):
        if form.get("identifier") == ["13*5361"]:
            return _json(request, read_fixture("bonarea", "product.json"))
        return _json(request, read_fixture("bonarea", "product_missing.json"))
    if path.endswith("/shop/GetProductSheet"):
        if form.get("idArticle") == ["13*5361"]:
            return _json(request, read_fixture("bonarea", "product_sheet.json"))
        # an unknown id still gets a sheet, flagged by success alone
        return _json(request, read_fixture("bonarea", "product_sheet_missing.json"))
    if path.endswith("/shop/GetPostalCodes"):
        return _json(request, read_fixture("bonarea", "postal_codes.json"))
    if path.endswith("/shop/ShoppingBody"):
        listings = {
            "": "tree.json",
            "13*300": "category_root.json",
            "13*300*010": "category_menu.json",
            "13*300*010*010": "category.json",
            "13*320*080": "category_leaf.json",
        }
        fixture = listings.get((form.get("reference") or [""])[0])
        if fixture is not None:
            return _json(request, read_fixture("bonarea", fixture))
        # an unknown reference throws, and asp.net renders its stock error page
        return httpx.Response(
            500, request=request, html=read_fixture("bonarea", "runtime_error.html")
        )
    return httpx.Response(404, request=request)


def _carrefour_html(request: httpx.Request, name: str) -> httpx.Response:
    return httpx.Response(200, request=request, html=read_fixture("carrefour", name))


def _carrefour_index(request: httpx.Request) -> httpx.Response:
    """serve the search index, which answers every lookup as a free-text query."""

    params = request.url.params
    if request.url.path.endswith("/empathize"):
        return _json(request, read_fixture("carrefour", "empathize.json"))
    lookups = {
        "521007071": "lookup_brik.json",
        "8431876011937": "lookup_brik.json",
        "714713105": "lookup_lactosa.json",
        "vc4aecomm-481229": "lookup_pack.json",
    }
    query = params.get("query", "")
    if query in lookups:
        return _json(request, read_fixture("carrefour", lookups[query]))
    if query != "leche":
        return _json(request, read_fixture("carrefour", "search_empty.json"))
    page = "search.json" if params.get("start", "0") == "0" else "search_page2.json"
    return _json(request, read_fixture("carrefour", page))


def _carrefour_storefront(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    if path == "/supermercado":
        return _carrefour_html(request, "home.html")
    if path == "/search-api/query/v1/search":
        if "session" not in params:
            # the proxy answers a missing session with a firewall block
            return httpx.Response(
                403, request=request, html=read_fixture("carrefour", "blocked.html")
            )
        return _json(request, read_fixture("carrefour", "proxy_search.json"))
    if path == "/cloud-api/salepoints/v1/drives":
        return _json(request, read_fixture("carrefour", "drives.json"))
    if path == "/cloud-api/categories-api/v1/categories/menu":
        # the menu answers one node's children and knows nothing below aisles
        menus = {"foodRootCategory": "menu.json", "cat20001": "menu_despensa.json"}
        name = menus.get(params.get("current_category", ""), "menu_empty.json")
        return _json(request, read_fixture("carrefour", name))
    if path.startswith("/cloud-api/salepoints/v1/stores-location/"):
        postal_code = path.rsplit("/", 1)[-1]
        if postal_code in {"28230", "28232"}:
            return _json(request, read_fixture("carrefour", "stores_location.json"))
        if postal_code == "08019":
            return _json(
                request, read_fixture("carrefour", "stores_location_empty.json")
            )
        return httpx.Response(404, request=request)
    if path.endswith("/cat20093/c"):
        offset = params.get("offset", "0")
        return _carrefour_html(
            request, "listing.html" if offset == "0" else "listing_offset.html"
        )
    if path.endswith("/cat20011/c"):
        return _carrefour_html(request, "category_aisle.html")
    if path.startswith("/supermercado/c/"):
        # a category page answers any slug, and an unknown id leaves the catalog
        return httpx.Response(301, request=request, headers={"Location": "/moda"})
    if path.startswith("/supermercado/p/R-"):
        # every product url redirects to its canonical slug, and an unknown id
        # redirects to the home page instead
        identifier = path[len("/supermercado/p/R-") : -len("/p")]
        canonical = "/supermercado/leche-semidesnatada-carrefour-brik-1-l/R-521007071/p"
        location = canonical if identifier == "521007071" else "/"
        return httpx.Response(301, request=request, headers={"Location": location})
    if path == "/supermercado/leche-semidesnatada-carrefour-brik-1-l/R-521007071/p":
        return _carrefour_html(request, "product.html")
    return httpx.Response(404, request=request)


def _carrefour_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "api.empathy.co":
        return _carrefour_index(request)
    return _carrefour_storefront(request)


def _lidl_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    host = request.url.host
    if host == "live.api.schwarz":
        page = (
            "stores_page1.json" if params.get("offset") == "0" else "stores_page2.json"
        )
        return _json(request, read_fixture("lidl", page))
    if host == "endpoints.leaflets.schwarz":
        if path.endswith("/overview"):
            return _json(request, read_fixture("lidl", "leaflets.json"))
        if path.endswith("/flyer"):
            return _json(request, read_fixture("lidl", "leaflet.json"))
        return httpx.Response(404, request=request)
    if path == "/q/api/search":
        # the search api serves its vendor type alone and answers anything
        # narrower, such as a bare application/json, with http 406
        if (
            request.headers.get("Accept")
            != "application/mindshift.search+json;version=2"
        ):
            return _json(request, {"status": 406, "error": "Not Acceptable"}, 406)
        if params.get("category.id") == "10067538":
            return _json(request, read_fixture("lidl", "category_child.json"))
        if "category.id" in params:
            return _json(request, read_fixture("lidl", "category.json"))
        if "q" not in params:
            return _json(request, read_fixture("lidl", "categories.json"))
        page = "search.json" if params.get("offset", "0") == "0" else "search_last.json"
        return _json(request, read_fixture("lidl", page))
    details = {
        "/p/api/detail/11150856/ES/es": "product_grocery.json",
        "/p/api/detail/100399898/ES/es": "product_shop.json",
    }
    if path in details:
        return _json(request, read_fixture("lidl", details[path]))
    if path == "/p/export/ES/es/product_sitemap.xml.gz":
        sitemap = read_fixture("lidl", "product_sitemap.xml")
        return httpx.Response(
            200, request=request, content=gzip.compress(sitemap.encode())
        )
    if path.startswith("/c/"):
        return httpx.Response(
            200, request=request, html=read_fixture("lidl", "campaign.html")
        )
    return httpx.Response(404, request=request)


def _bonpreu_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    prefix = "/api/webproductpagews"
    if path == "/":
        return httpx.Response(
            200, request=request, html=read_fixture("bonpreu", "home.html")
        )
    if path == f"{prefix}/v1/categories":
        return _json(request, read_fixture("bonpreu", "categories.json"))
    if path == f"{prefix}/v5/products/bop":
        if params.get("retailerProductId") == "29189":
            return _json(request, read_fixture("bonpreu", "product.json"))
        return _json(
            request, read_fixture("bonpreu", "product_not_found.json"), status=404
        )
    if path == f"{prefix}/v5/products/similar":
        return _json(request, read_fixture("bonpreu", "similar.json"))
    if path == f"{prefix}/v5/products/related":
        return _json(request, read_fixture("bonpreu", "related.json"))
    if request.method == "PUT" and path == f"{prefix}/v6/products":
        return _json(request, read_fixture("bonpreu", "batch.json"))
    if path == f"{prefix}/v6/product-pages/search":
        page = "search.json" if "pageToken" not in params else "listing_page2.json"
        return _json(request, read_fixture("bonpreu", page))
    if path == f"{prefix}/v6/product-pages":
        category = params.get("categoryId")
        if "pageToken" in params:
            return _json(request, read_fixture("bonpreu", "listing_page2.json"))
        if category == "2068f9ba-b1af-4cf1-ad2b-3301c8d1a977":
            return _json(request, read_fixture("bonpreu", "new_arrivals.json"))
        if category == "130716f2-795a-4f0b-ad39-b449817921b3":
            return _json(request, read_fixture("bonpreu", "category.json"))
        # an unknown category has answered 404 since october 2026, where it
        # used to fall back to the root listing (category_root.json)
        return _json(
            request, read_fixture("bonpreu", "category_not_found.json"), status=404
        )
    if path == "/api/product-listing-pages/v1/pages/promotions":
        return _json(request, read_fixture("bonpreu", "promotions.json"))
    if path == "/api/search/v1/suggestions/primary":
        return _json(request, read_fixture("bonpreu", "suggestions.json"))
    return httpx.Response(404, request=request)


# the canary islands, which dia does not deliver to
_DIA_NO_SERVICE = ("35", "38")
_DIA = "/api/v1"


def _dia_cookie(request: httpx.Request, name: str, default: str) -> str:
    for part in request.headers.get("Cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return default


def _dia_json(
    request: httpx.Request, name: str, status: int = 200, page: int | None = None
) -> httpx.Response:
    """serve a fixture echoing the session's postcode and locale, as dia does.

    the canned storefront keeps no state: a postcode or locale put on the
    session comes back as a cookie, the way the real one is keyed on its
    session cookie.
    """

    data = read_fixture("dia", name)
    if "locale" in data:
        data["locale"] = _dia_cookie(request, "canned_locale", "es")
    if "cart" in data:
        postal_code = _dia_cookie(request, "canned_postal_code", "28041")
        data["cart"] = {**data["cart"], "postal_code": postal_code}
    if page is not None:
        data["pagination"]["page_number"] = page
    return _json(request, data, status)


def _dia_session(request: httpx.Request) -> httpx.Response | None:
    path = request.url.path
    if request.method == "PUT" and path.endswith("/save-shipping-address"):
        code = request.url.params.get("new_postal_code", "")
        if code.startswith(_DIA_NO_SERVICE):
            no_service = {"no_service": "No service for supplied postal code"}
            return _json(
                request,
                {"code": 206, "message": no_service, "type": "VALIDATION_ERROR"},
                206,
            )
        cookie = f"canned_postal_code={code}; Path=/"
        return httpx.Response(204, request=request, headers={"Set-Cookie": cookie})
    if request.method == "PATCH" and path.endswith("/current/locale"):
        locale = json.loads(request.content)["locale"]
        cookie = f"canned_locale={locale}; Path=/"
        return httpx.Response(204, request=request, headers={"Set-Cookie": cookie})
    if path.endswith("/check-service"):
        if request.url.params.get("postal_code", "").startswith(_DIA_NO_SERVICE):
            return httpx.Response(206, request=request)
        return _dia_json(request, "check_service.json")
    return None


def _dia_handler(request: httpx.Request) -> httpx.Response:
    session = _dia_session(request)
    if session is not None:
        return session
    path = request.url.path
    params = request.url.params
    page = int(params.get("page", "1"))
    if path == f"{_DIA}/common-aggregator/menu-data":
        return _dia_json(request, "menu.json")
    if path == f"{_DIA}/search-back/search/reduced":
        # a sku looked up by search finds nothing past the first page
        found = page == 1 and not params.get("q", "").isdigit()
        return _dia_json(request, "search.json" if found else "search_empty.json")
    if path == f"{_DIA}/pdp-back/504P6":
        return _dia_json(request, "product.json")
    if path.startswith(f"{_DIA}/pdp-back/"):
        # dia answers an unknown sku with a server error
        return _dia_json(request, "product_error.json", 500)
    leaves = {
        "/huevos-leche-y-mantequilla/leche/c/L2051": "listing.json",
        "/novedades-y-recomendados/novedades/c/L2302": "new_arrivals.json",
    }
    plp = f"{_DIA}/plp-back"
    if path.startswith(f"{plp}/reduced/") and path[len(f"{plp}/reduced") :] in leaves:
        return _dia_json(request, leaves[path[len(f"{plp}/reduced") :]], page=page)
    if path == f"{plp}/l1/all/L128/reduced":
        # a group with no listing of its own redirects to its first child
        location = "/novedades-y-recomendados/novedades/c/L2302"
        body = {"code": 301, "message": "Moved permanently"}
        return httpx.Response(
            301, request=request, headers={"Location": location}, json=body
        )
    if path == f"{plp}/l1/all/L108/reduced" and "category_id" not in params:
        return _dia_json(request, "listing_root.json")
    if path.startswith(f"{plp}/l1/all/"):
        return _dia_json(request, "listing_root_last.json")
    if path == f"{plp}/offers/reduced":
        return _dia_json(request, "offers.json")
    if path == f"{plp}/offers/reduced/L128":
        return _dia_json(request, "category_offers.json", page=page)
    if path.startswith(f"{plp}/offers/reduced/"):
        return _dia_json(request, "category_offers_last.json")
    if path.startswith(f"{plp}/reduced/"):
        return _dia_json(request, "not_found.json", 404)
    return httpx.Response(404, request=request)


def _tapestry_handler(store: str, product_id: str) -> Handler:
    """serve one tapestry storefront: the menu, the fragments, one product.

    eroski and caprabo run the same build, so one router serves both from
    each store's own fixtures. every listing fragment past the first page is
    the empty page the storefront answers once a listing runs out.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/es/":
            return httpx.Response(
                200, request=request, html=read_fixture(store, "menu.html")
            )
        if path.endswith(":loadpage"):
            if request.url.params.get("pageNumber", "0") != "0":
                return _json(request, read_fixture(store, "search_empty.json"))
            name = "search.json" if path.startswith("/es/search/") else "listing.json"
            return _json(request, read_fixture(store, name))
        if path.startswith("/es/productdetail/"):
            wanted = path.removeprefix("/es/productdetail/").split("-", 1)[0]
            if wanted == product_id:
                return httpx.Response(
                    200, request=request, html=read_fixture(store, "product.html")
                )
            # an unknown id redirects to the error page
            location = f"https://{request.url.host}:443/es/error/404/"
            return httpx.Response(301, request=request, headers={"Location": location})
        return httpx.Response(404, request=request)

    return handler


def _alcampo_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    pages = "/api/webproductpagews"
    if path == "/":
        return httpx.Response(
            200, request=request, html=read_fixture("alcampo", "home.html")
        )
    if path == "/api/ecomdeliverydestinations/v4/delivery-addresses":
        return _json(request, read_fixture("alcampo", "stores.json"))
    if request.method == "PUT" and path == "/api/customersessions/v2/sessions/active":
        # the session moves to whichever point and region the put names
        session = read_fixture("alcampo", "session.json")
        session.update(json.loads(request.content))
        return _json(request, session)
    if path == f"{pages}/v1/categories":
        return _json(request, read_fixture("alcampo", "categories.json"))
    if path == f"{pages}/v5/products/bop":
        if params.get("retailerProductId") == "54180":
            return _json(request, read_fixture("alcampo", "product.json"))
        return _json(
            request, read_fixture("alcampo", "product_not_found.json"), status=404
        )
    if path == f"{pages}/v5/products/similar":
        return _json(request, read_fixture("alcampo", "similar.json"))
    if path == f"{pages}/v5/products/related":
        return _json(request, read_fixture("alcampo", "related.json"))
    if request.method == "PUT" and path == f"{pages}/v6/products":
        return _json(request, read_fixture("alcampo", "batch.json"))
    if path == f"{pages}/v6/product-pages/search":
        page = "search.json" if "pageToken" not in params else "listing_page2.json"
        return _json(request, read_fixture("alcampo", page))
    if path == f"{pages}/v6/product-pages":
        if "pageToken" in params:
            return _json(request, read_fixture("alcampo", "listing_page2.json"))
        if "categoryId" not in params and params.get("filters") == "dummyValue=new":
            return _json(request, read_fixture("alcampo", "new_arrivals.json"))
        if params.get("categoryId") == "25fd70b8-959e-4633-b14a-0cee7cd3669a":
            return _json(request, read_fixture("alcampo", "category.json"))
        return _json(
            request, read_fixture("alcampo", "category_not_found.json"), status=404
        )
    if path == "/api/product-listing-pages/v1/pages/promotions":
        return _json(request, read_fixture("alcampo", "promotions.json"))
    if path == "/api/search/v1/suggestions/primary":
        return _json(request, read_fixture("alcampo", "suggestions.json"))
    return httpx.Response(404, request=request)


# the centres the canned index knows: the default, and the one the canned
# postcode lookup assigns to every barcelona postcode
_CONDIS_CENTRES = ("718", "531")
_CONDIS_FACETS = {
    "is_novelty": "novelties.json",
    "on_sale": "on_sale.json",
    "on_promotion": "on_promotion.json",
}
_CONDIS_ACTION = "60ceecdfa8b37d1b66f9a3b97d656b6d8e97946615"
_CONDIS_ACTION_SCRIPT = "/_next/static/chunks/3i33gyr3gc5bp.js"
_CONDIS_PRODUCTS = ("704049", "704056")


def _condis_listing(request: httpx.Request, name: str) -> httpx.Response:
    """serve a canned index page, as the last one once the offset moves."""

    page = read_fixture("condis", name)
    start = int(request.url.params.get("start", "0"))
    if start:
        catalog = page["catalog"]
        catalog["numFound"] = start + len(catalog["content"])
    return _json(request, page)


def _condis_index(request: httpx.Request) -> httpx.Response:
    """serve the empathy index: search, browsing by facet, and suggestions."""

    params = request.url.params
    endpoint = request.url.path.rsplit("/", 1)[-1]
    empty = {"catalog": {"content": [], "numFound": 0}}
    if params.get("store") not in _CONDIS_CENTRES:
        # the index answers a centre it does not know with an empty catalogue
        return _json(request, empty)
    if endpoint == "empathize":
        return _json(request, read_fixture("condis", "empathize.json"))
    if endpoint == "search":
        # every query finds the canned milk
        first = params.get("start", "0") == "0"
        return _condis_listing(request, "search.json" if first else "search_last.json")
    field = params.get("browseField", "")
    if field in _CONDIS_FACETS:
        return _condis_listing(request, _CONDIS_FACETS[field])
    if field == "filterCategory" and params.get("browseValue", "").startswith(
        ("c07", "c03")
    ):
        return _condis_listing(request, "browse.json")
    return _json(request, empty)


def _condis_storefront(request: httpx.Request) -> httpx.Response:
    """serve the storefront as an anonymous session that has signed in sees it.

    the sign-in every first page request redirects through is left to
    ``tests/condis``, so a page costs one request here, as it does once a
    client holds its session.
    """

    path = request.url.path
    if path.startswith("/_next/static/chunks/"):
        name = "action_script.js" if path == _CONDIS_ACTION_SCRIPT else "script.js"
        return httpx.Response(200, request=request, text=read_fixture("condis", name))
    if request.method == "POST" and path == "/":
        if request.headers.get("Next-Action") != _CONDIS_ACTION:
            return httpx.Response(404, request=request, text="Server action not found.")
        postal_code = json.loads(request.content)[0]
        name = (
            "postal_code_08034.txt"
            if postal_code[:2] == "08"
            else "postal_code_28001.txt"
        )
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/x-component"},
            text=read_fixture("condis", name),
        )
    if path == "/":
        return httpx.Response(
            200, request=request, html=read_fixture("condis", "home.html")
        )
    parts = path.split("/")
    if len(parts) == 5 and parts[2] == "p":
        product = parts[3] if parts[3] in _CONDIS_PRODUCTS else "missing"
        # an unknown id renders the page without a product, with http 200
        html = read_fixture("condis", f"product_{product}.html")
        return httpx.Response(200, request=request, html=html)
    return httpx.Response(404, request=request)


def _condis_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.empathy.co":
        return _condis_index(request)
    return _condis_storefront(request)


_ALDI_KEY = "83df5acd172c42ab174afa4583232b5d"
_ALDI_RECORDS = {
    "995700": "product.json",
    "856703": "product_two_windows.json",
    "998600": "product_loose.json",
}
_ALDI_PAGES = {
    "/productos.html": "productos.html",
    "/productos/lacteos-y-huevos.html": "category_root.html",
    "/productos/lacteos-y-huevos/leche-y-bebidas-vegetales.html": "category_leaf.html",
    "/ofertas.html": "offers.html",
    "/bal/ofertas.html": "offers.html",
    "/can/ofertas.html": "offers.html",
}


def _aldi_index(request: httpx.Request) -> httpx.Response:
    """serve the algolia index: a window of a canned answer, or one record."""

    if request.headers.get("X-Algolia-API-Key") != _ALDI_KEY:
        return _json(request, read_fixture("aldi", "invalid_key.json"), 403)
    _, _, rest = request.url.path.partition("_products2")
    if rest == "/query":
        params = parse_qs(json.loads(request.content)["params"])
        filtered = "filters" in params
        data = read_fixture("aldi", "category.json" if filtered else "search.json")
        offset = int(params.get("offset", ["0"])[0])
        length = int(params.get("length", ["20"])[0])
        hits = data["hits"]
        data.update(
            hits=hits[offset : offset + length],
            nbHits=len(hits),
            offset=offset,
            length=length,
        )
        return _json(request, data)
    record = _ALDI_RECORDS.get(rest.lstrip("/"))
    if record is not None:
        return _json(request, read_fixture("aldi", record))
    return _json(request, read_fixture("aldi", "not_found.json"), 404)


def _aldi_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "l9knu74io7-dsn.algolia.net":
        return _aldi_index(request)
    page = _ALDI_PAGES.get(request.url.path)
    if page is not None:
        return httpx.Response(200, request=request, html=read_fixture("aldi", page))
    return httpx.Response(
        404, request=request, html=read_fixture("aldi", "not_found.html")
    )


def _ahorramas_html(
    request: httpx.Request, name: str, status: int = 200
) -> httpx.Response:
    return httpx.Response(status, request=request, html=read_fixture("ahorramas", name))


def _ahorramas_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    if path == "/":
        return _ahorramas_html(request, "menu.html")
    if path.endswith("/Search-UpdateGrid"):
        cgid = params.get("cgid")
        if params.get("start", "0") != "0" and cgid is None:
            return _ahorramas_html(request, "search_empty.html")
        if params.get("start", "0") != "0":
            # the empty page past a category's end, answering for that category
            page = read_fixture("ahorramas", "listing_end.html")
            return httpx.Response(
                200,
                request=request,
                html=page.replace("cgid=cordero_y_cabrito", f"cgid={cgid}"),
            )
        if cgid is None:
            return _ahorramas_html(request, "search.html")
        listings = {
            "cordero_y_cabrito": "listing.html",
            "ofertas": "offers.html",
            "root": "catalog.html",
        }
        # an unknown category answers the whole catalogue, unfiltered
        name = listings.get(cgid, "listing_unknown.html")
        return _ahorramas_html(request, name)
    if path.endswith("/Product-Variation"):
        records = {"52028": "product.json", "75248": "product_weighed.json"}
        record = records.get(params.get("pid", ""))
        if record is not None:
            return _json(request, read_fixture("ahorramas", record))
        # an unknown id is a server error with an error document
        return _json(request, read_fixture("ahorramas", "product_missing.json"), 500)
    return httpx.Response(404, request=request)


HARNESSES: dict[str, Harness] = {
    "mercadona": Harness(
        client_type=Mercadona,
        make=lambda **options: Mercadona("mad3", **options),
        handler=_mercadona_handler,
        query="leche",
        product_id="1001",
        missing_product_id="missing",
        category_id="72",
        module="supermercapy.mercadona",
    ),
    "consum": Harness(
        client_type=Consum,
        make=lambda **options: Consum(147, **options),
        handler=_consum_handler,
        query="leche",
        product_id="7080604",
        missing_product_id="missing",
        category_id="1970",
        module="supermercapy.consum",
    ),
    "plusfresc": Harness(
        client_type=Plusfresc,
        make=lambda **options: Plusfresc(12, **options),
        handler=_plusfresc_handler,
        query="llet",
        product_id="002530",
        missing_product_id="missing",
        category_id="010101",
        module="supermercapy.plusfresc",
    ),
    "bonarea": Harness(
        client_type=Bonarea,
        make=lambda **options: Bonarea(**{"min_request_interval": 0.0, **options}),
        handler=_bonarea_handler,
        query="leche",
        product_id="13*5361",
        missing_product_id="13*9999999",
        category_id="13*300*010*010",
        module="supermercapy.bonarea",
    ),
    "carrefour": Harness(
        client_type=Carrefour,
        make=lambda **options: Carrefour("005290", **options),
        handler=_carrefour_handler,
        query="leche",
        product_id="521007071",
        missing_product_id="999999999",
        category_id="cat20093",
        module="supermercapy.carrefour",
    ),
    "lidl": Harness(
        client_type=Lidl,
        make=lambda **options: Lidl(26, **options),
        handler=_lidl_handler,
        query="pan",
        product_id="11150856",
        missing_product_id="missing",
        category_id="10067761",
        module="supermercapy.lidl",
    ),
    "bonpreu": Harness(
        client_type=Bonpreu,
        # the real default is two seconds; the canned transport needs no pacing
        make=lambda **options: Bonpreu(**{"min_request_interval": 0.0, **options}),
        handler=_bonpreu_handler,
        query="llet",
        product_id="29189",
        missing_product_id="00000",
        category_id="130716f2-795a-4f0b-ad39-b449817921b3",
        module="supermercapy.bonpreu",
    ),
    "dia": Harness(
        client_type=Dia,
        # the real default is half a second; the canned transport needs none
        make=lambda **options: Dia(**{"min_request_interval": 0.0, **options}),
        handler=_dia_handler,
        query="leche",
        product_id="504P6",
        missing_product_id="999999",
        category_id="L2051",
        module="supermercapy.dia",
    ),
    "eroski": Harness(
        client_type=Eroski,
        # the real default is one second; the canned transport needs no pacing
        make=lambda **options: Eroski(**{"min_request_interval": 0.0, **options}),
        handler=_tapestry_handler("eroski", "3430469"),
        query="aceite oliva",
        product_id="3430469",
        missing_product_id="99999999",
        category_id="2059702",
        module="supermercapy.eroski",
    ),
    "caprabo": Harness(
        client_type=Caprabo,
        # the real default is one second; the canned transport needs no pacing
        make=lambda **options: Caprabo(**{"min_request_interval": 0.0, **options}),
        handler=_tapestry_handler("caprabo", "18581678"),
        query="leche",
        product_id="18581678",
        missing_product_id="99999999",
        category_id="2059808",
        module="supermercapy.caprabo",
    ),
    "aldi": Harness(
        client_type=Aldi,
        make=lambda **options: Aldi(**options),
        handler=_aldi_handler,
        query="leche",
        product_id="995700",
        missing_product_id="999999999",
        category_id="lacteos-y-huevos/leche-y-bebidas-vegetales",
        module="supermercapy.aldi",
    ),
    "ahorramas": Harness(
        client_type=Ahorramas,
        # the real default is one second; the canned transport needs no pacing
        make=lambda **options: Ahorramas(**{"min_request_interval": 0.0, **options}),
        handler=_ahorramas_handler,
        query="agua",
        product_id="52028",
        missing_product_id="99999999",
        category_id="cordero_y_cabrito",
        module="supermercapy.ahorramas",
    ),
    "alcampo": Harness(
        client_type=Alcampo,
        # the real default is two seconds; the canned transport needs no pacing
        make=lambda **options: Alcampo(**{"min_request_interval": 0.0, **options}),
        handler=_alcampo_handler,
        query="leche",
        product_id="54180",
        missing_product_id="1",
        category_id="25fd70b8-959e-4633-b14a-0cee7cd3669a",
        module="supermercapy.alcampo",
    ),
    "condis": Harness(
        client_type=Condis,
        # the real default is half a second; the canned transport needs none
        make=lambda **options: Condis(**{"min_request_interval": 0.0, **options}),
        handler=_condis_handler,
        query="leche",
        product_id="704049",
        missing_product_id="999999999",
        category_id="c07__cat00210002",
        module="supermercapy.condis",
    ),
}
