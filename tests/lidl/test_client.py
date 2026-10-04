from __future__ import annotations

import gzip
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from supermercapy import (
    Capability,
    ConfigurationError,
    Language,
    Lidl,
    NotFoundError,
    OutOfCoverageError,
    UnsupportedOperationError,
)
from supermercapy.lidl import LidlProduct, OfferWeek, PriceZone, ProductFamily
from tests.conftest import read_fixture
from tests.harness import _lidl_handler

STORE = "lidl"
BASE = "https://www.lidl.es"
SEARCH = f"{BASE}/q/api/search"
STORES = "https://live.api.schwarz/odj/stores-api/v2/myapi/stores-frontend/stores"
LEAFLETS = "https://endpoints.leaflets.schwarz/v4"


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


def recording(data: object, requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json=data)

    return httpx.MockTransport(handler)


def canned(requests: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return _lidl_handler(request)

    return httpx.MockTransport(handler)


def client(requests: list[httpx.Request] | None = None, **options: Any) -> Lidl:
    return Lidl(transport=canned(requests), **options)


# ------------------------------------------------------------------- lifecycle


def test_constructor_performs_no_io_and_exposes_both_geographies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor performed I/O")

    with Lidl(26, transport=httpx.MockTransport(handler)) as lidl:
        assert lidl.region == 26
        assert lidl.store_id == "26"
        assert lidl.zone == PriceZone.PENINSULA
        assert lidl.family is None
        assert lidl.store is None
        assert lidl.language is Language.SPANISH


def test_bindings_are_optional() -> None:
    with client() as lidl:
        assert lidl.region is None
        assert lidl.store_id is None


@pytest.mark.parametrize("region", ["", "  ", "abc", -1, True, 1.5, "12a"])
def test_invalid_region_is_rejected(region: Any) -> None:
    with pytest.raises(ConfigurationError, match="region"):
        Lidl(region)


@pytest.mark.parametrize("zone", ["", "pen", "MAD", 3])
def test_invalid_zone_is_rejected(zone: Any) -> None:
    with pytest.raises(ConfigurationError, match="zone"):
        Lidl(zone=zone)


@pytest.mark.parametrize("family", ["", "food", 1])
def test_invalid_family_is_rejected(family: Any) -> None:
    with pytest.raises(ConfigurationError, match="family"):
        Lidl(family=family)


def test_no_request_carries_a_store_specific_header() -> None:
    # every lidl.es surface answered 200 with no user-agent at all during
    # reconnaissance; only the schwarz stores api needs a key, and only the
    # search api a media type of its own
    requests: list[httpx.Request] = []
    with client(requests, region=26) as lidl:
        lidl.get_product("11150856")

    headers = requests[0].headers
    assert headers["Accept"] == "application/json"
    assert headers["Accept-Language"] == "es"
    assert headers["User-Agent"].startswith("supermercapy/")
    assert "x-apikey" not in headers
    assert "Cookie" not in headers


def test_ean_lookup_and_home_are_not_supported() -> None:
    assert not Lidl.supports(Capability.EAN_LOOKUP)
    assert not Lidl.supports(Capability.NUTRITION)
    assert Lidl.supports(Capability.FUTURE_PRICES)
    with client() as lidl, pytest.raises(UnsupportedOperationError):
        lidl.get_product_by_ean("4052916891476")


# ---------------------------------------------------------------------- search


def test_search_sends_the_documented_request_and_pages_by_offset() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        page = lidl.search_products("pan", page_size=2)

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{SEARCH}?assortment=ES&locale=es_ES&version=2.1.0&fetchsize=2&offset=0&q=pan"
    )
    assert page.total_hits == 3
    assert page.offset == 0
    assert page.next_cursor == "2"
    assert page.has_more is True
    assert [product.id for product in page.products] == ["100408615001", "11137251"]


def test_search_follows_the_cursor_and_stops_at_the_last_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        first = lidl.search_products("pan", page_size=2)
        last = lidl.search_products("pan", page_size=2, cursor=first.next_cursor)

    assert params_of(requests[1])["offset"] == ["2"]
    assert last.next_cursor is None
    assert last.offset == 2


def test_search_accepts_a_sort_and_rejects_a_blank_one() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        lidl.search_products("pan", sort="price-desc")
        with pytest.raises(ConfigurationError, match="sort"):
            lidl.search_products("pan", sort="  ")

    assert params_of(requests[0])["sort"] == ["price-desc"]
    assert len(requests) == 1


def test_the_match_all_wildcard_is_refused() -> None:
    # q=* is indexed but capped at about a hundred rows and returns no grocery
    # items at all, so it reads like a catalog and is not one
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        with pytest.raises(ConfigurationError, match="iter_catalog"):
            lidl.search_products("*")
        with pytest.raises(ConfigurationError, match="query"):
            lidl.search_products("   ")

    assert requests == []


def test_page_size_is_capped_at_the_search_maximum() -> None:
    assert Lidl.max_page_size == 1000
    with client() as lidl, pytest.raises(ConfigurationError, match="page_size"):
        lidl.search_products("pan", page_size=1001)


def test_a_family_filter_narrows_a_page_without_a_second_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests, family=ProductFamily.GROCERY) as lidl:
        bound = lidl.search_products("pan", page_size=2)
    with client() as lidl:
        override = lidl.search_products("pan", page_size=2, family="shop")

    assert [product.id for product in bound.products] == ["11137251"]
    assert [product.id for product in override.products] == ["100408615001"]
    assert len(requests) == 1


# --------------------------------------------------------------------- product


def test_get_product_uses_the_language_path_and_returns_region_prices() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region=26) as lidl:
        product = lidl.get_product("11150856")

    assert len(requests) == 1
    assert str(requests[0].url) == f"{BASE}/p/api/detail/11150856/ES/es"
    assert isinstance(product, LidlProduct)
    assert product.family is ProductFamily.GROCERY
    assert product.price_band_id == "1"
    assert product.price.amount == Decimal("1.99")
    assert product.price.previous == Decimal("2.69")
    assert product.price.discount_percentage == Decimal("26")
    assert product.packaging_text == "A granel"
    assert product.nutrition is None


@pytest.mark.parametrize(
    ("given", "requested"),
    [
        ("11150856", "11150856"),
        ("11150856005", "11150856"),
        ("100399898", "100399898"),
        ("100408615001", "100408615"),
        (11150856, "11150856"),
    ],
)
def test_a_variant_id_is_normalised_to_its_parent(given: Any, requested: str) -> None:
    # the detail endpoint answers 404 for a variant id in both families
    requests: list[httpx.Request] = []
    transport = recording(read_fixture(STORE, "product_grocery.json"), requests)

    with Lidl(transport=transport) as lidl:
        lidl.get_product(given)

    assert requests[0].url.path == f"/p/api/detail/{requested}/ES/es"


def test_a_missing_product_raises_not_found() -> None:
    with client() as lidl, pytest.raises(NotFoundError):
        lidl.get_product("11999999")


def test_shop_prices_come_from_the_bound_delivery_zone() -> None:
    with client(zone="BAL") as lidl:
        balearic = lidl.get_product("100399898")
    with client() as lidl:
        peninsular = lidl.get_product("100399898")

    assert balearic.family is ProductFamily.SHOP
    assert balearic.price.amount == Decimal("52.99")
    assert peninsular.price.amount == Decimal("49.99")
    assert peninsular.zone("CAN") is not None
    assert peninsular.price_band_id is None


def test_a_detail_reads_the_brand_from_its_info_block() -> None:
    # listing tiles keep the brand at the top level; the detail moved it to
    # info.brand, so reading only the top level reported no brand at all
    raw = read_fixture(STORE, "product_shop.json")
    assert "brand" not in raw

    with client() as lidl:
        product = lidl.get_product("100399898")
        page = lidl.search_products("pan", page_size=2)

    assert product.brand == "PARKSIDE®"
    assert page.products[0].brand == "PARKSIDE®"


def test_a_canary_region_reports_no_grocery_price() -> None:
    # the canary regions carry no regionPriceId because they are priced with
    # igic; reading the mainland band for them would report a wrong price
    with client(region=49) as lidl:
        product = lidl.get_product("11150856")

    assert product.region("49") is not None
    assert product.region("49").price_id is None  # type: ignore[union-attr]
    assert product.price_band_id is None
    assert product.price.amount is None
    assert product.member_price is None


def test_a_member_only_price_is_flagged_and_promoted() -> None:
    with client(region=26) as lidl:
        product = lidl.get_product("11150856")

    assert product.price.is_member_price is True
    assert product.price.member_text == "Con Lidl Plus"
    assert product.price.highlight_text == "-26%"
    assert product.member_price is not None
    assert [promotion.member_only for promotion in product.promotions] == [True]


def test_vat_is_read_from_the_band_not_from_the_bare_price() -> None:
    raw = read_fixture(STORE, "product_grocery.json")
    assert raw["price"]["hasVat"] is False

    with client(region=26) as lidl:
        product = lidl.get_product("11150856")

    assert product.price.has_vat is True


def test_pack_size_and_unit_price_stay_free_text() -> None:
    # packaging and basePrice are spanish display strings with comma decimals
    with client(region=26) as lidl:
        product = lidl.get_product("11150856")

    assert product.pack_size_text == "A granel"
    assert product.price.unit_price is None
    assert product.price.base_price_text is None


# ------------------------------------------------------------------ categories


def test_categories_come_from_the_facet_of_an_empty_listing() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        categories = lidl.get_categories()

    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{SEARCH}?assortment=ES&locale=es_ES&version=2.1.0&fetchsize=1"
    )
    assert "q=" not in str(requests[0].url)
    assert [category.id for category in categories] == ["10067761", "10067764"]
    assert categories[0].product_count == 98


def test_one_category_carries_its_children_and_first_page() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        category = lidl.get_category("10067761")

    assert len(requests) == 1
    assert params_of(requests[0]) == {
        "assortment": ["ES"],
        "locale": ["es_ES"],
        "version": ["2.1.0"],
        "fetchsize": ["48"],
        "offset": ["0"],
        "category.id": ["10067761"],
    }
    assert [child.id for child in category.children] == ["10067538", "10067534"]
    assert [product.id for product in category.products] == ["100408615001"]


def test_an_unknown_category_raises_not_found() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl, pytest.raises(NotFoundError, match="10099999"):
        lidl.get_category("10099999")

    assert len(requests) == 1


def test_a_subcategory_is_found_below_its_ancestors() -> None:
    # a filtered listing's facet is the path from the root down to the
    # selected node, so a subcategory is never at the facet's top level
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        category = lidl.get_category("10067538")

    assert len(requests) == 1
    assert params_of(requests[0])["category.id"] == ["10067538"]
    assert category.id == "10067538"
    assert category.name == "Herramientas y accesorios para vehículos"
    assert category.parent_id == "10067761"
    assert category.product_count == 316
    assert [child.id for child in category.children] == ["10084754", "10084753"]
    assert all(child.parent_id == "10067538" for child in category.children)
    assert [product.id for product in category.products] == ["100406166"]


# ---------------------------------------------------------------------- stores


def test_stores_are_paged_and_carry_the_static_storefront_key() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        stores = lidl.list_stores()

    assert len(requests) == 2
    assert requests[0].url.host == "live.api.schwarz"
    assert str(requests[0].url) == (
        f"{STORES}?country_code=ES&locale=es-ES&limit=250&offset=0"
    )
    assert params_of(requests[1])["offset"] == ["2"]
    assert requests[0].headers["x-apikey"] == "KxboQtt40BG4VpBL16IhaRd2CXh0QbAc"
    assert [store.id for store in stores] == ["ES00219", "ES00215", "ES00272"]
    assert stores[0].offer_region == 26
    assert stores[0].zone == "PEN"
    assert stores[0].store_number == "219"
    assert stores[0].services == ("freeWiFi", "shopNGo")


def test_stores_can_be_filtered_by_postal_code() -> None:
    with client() as lidl:
        stores = lidl.list_stores("08013")

    assert [store.id for store in stores] == ["ES00219"]


def test_an_invalid_postal_code_is_rejected_before_any_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl, pytest.raises(ConfigurationError, match="postal"):
        lidl.list_stores("8013")

    assert requests == []


def test_from_postal_code_binds_the_region_and_the_zone() -> None:
    requests: list[httpx.Request] = []

    with Lidl.from_postal_code("08013", transport=canned(requests)) as lidl:
        assert lidl.region == 26
        assert lidl.zone == "PEN"
        assert lidl.store is not None
        assert lidl.store.id == "ES00219"

    assert len(requests) == 2
    assert all(request.url.host == "live.api.schwarz" for request in requests)


def test_from_postal_code_falls_back_to_the_province() -> None:
    # the stores api takes no postal-code filter, so an unserved postal code
    # is answered with the first store of the same province
    with Lidl.from_postal_code("07199", transport=canned()) as lidl:
        assert lidl.region == 48
        assert lidl.zone == "BAL"


def test_from_postal_code_raises_out_of_coverage_and_closes_the_client() -> None:
    clients: list[Lidl] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, request=request, json={"meta": {"total": 0}, "items": []}
        )

    original = Lidl.__init__

    def record(self: Lidl, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        clients.append(self)

    Lidl.__init__ = record  # type: ignore[method-assign]
    try:
        with pytest.raises(OutOfCoverageError, match="28001"):
            Lidl.from_postal_code("28001", transport=httpx.MockTransport(handler))
    finally:
        Lidl.__init__ = original  # type: ignore[method-assign]
    assert clients and clients[0].is_closed


# --------------------------------------------------------------------- catalog


def test_catalog_ids_come_from_one_gzipped_sitemap_request() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        ids = list(lidl.iter_catalog_ids())

    assert len(requests) == 1
    assert str(requests[0].url) == f"{BASE}/p/export/ES/es/product_sitemap.xml.gz"
    assert requests[0].headers["Accept"] == "application/xml"
    assert ids == ["100214229", "100399898", "11150856", "11137251"]


def test_catalog_ids_split_the_two_assortments_by_prefix() -> None:
    with client() as lidl:
        grocery = list(lidl.iter_catalog_ids(family="grocery"))
    with client(family=ProductFamily.SHOP) as lidl:
        shop = list(lidl.iter_catalog_ids())

    assert grocery == ["11150856", "11137251"]
    assert shop == ["100214229", "100399898"]


def test_an_uncompressed_sitemap_is_read_too() -> None:
    body = read_fixture(STORE, "product_sitemap.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text=body)

    with Lidl(transport=httpx.MockTransport(handler)) as lidl:
        assert list(lidl.iter_catalog_ids(family="grocery")) == [
            "11150856",
            "11137251",
        ]


def test_iter_catalog_hydrates_ids_and_skips_retired_ones() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region=26) as lidl:
        products = list(lidl.iter_catalog(family="grocery"))

    # one sitemap request plus one detail request per id, and the id the
    # storefront has already dropped is skipped rather than raising
    assert len(requests) == 3
    assert [product.id for product in products] == ["11150856"]


def test_get_catalog_deduplicates_and_needs_the_capability() -> None:
    with client() as lidl:
        catalog = lidl.get_catalog()

    assert len({product.id for product in catalog}) == len(catalog)


# ---------------------------------------------------------------------- offers


def test_offers_parse_the_campaign_page_tiles() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region=26) as lidl:
        offers = lidl.get_offers()

    assert len(requests) == 1
    assert str(requests[0].url) == f"{BASE}/c/ofertas-semanales/a10089449"
    assert requests[0].headers["Accept"].startswith("text/html")
    assert [product.id for product in offers] == ["11038819", "11137251"]
    assert offers[0].price.amount == Decimal("1.25")
    assert offers[0].price.previous == Decimal("1.49")


def test_next_week_prices_are_published_before_they_go_live() -> None:
    with client(region=26) as lidl:
        offers = lidl.get_offers(week=OfferWeek.CURRENT)

    upcoming = next(product for product in offers if product.id == "11137251")
    assert [price.amount for price in upcoming.future_prices] == [Decimal("0.62")]
    assert upcoming.future_prices[0].valid_from is not None
    assert upcoming.future_prices[0].valid_from.isoformat() == (
        "2026-09-20T22:00:00+00:00"
    )


@pytest.mark.parametrize(
    ("week", "url"),
    [
        ("current", "/c/ofertas-semanales/a10089449"),
        ("next", "/c/ofertas-proxima-semana/a10088432"),
        ("weekend", "/c/super-finde/a10089450"),
        ("weekend_next", "/c/super-finde-proxima-semana/a10089609"),
        ("permanent_cuts", "/c/bajadas-permanentes/a10089468"),
        ("other_brands", "/c/tus-otras-marcas-de-siempre/a10089613"),
    ],
)
def test_every_offer_week_maps_to_its_campaign_page(week: str, url: str) -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        lidl.get_offers(week=week)

    assert requests[0].url.path == url


def test_an_unknown_offer_week_is_rejected() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl, pytest.raises(ConfigurationError, match="week"):
        lidl.get_offers(week="yesterday")

    assert requests == []


def test_a_campaign_page_can_be_read_by_slug_and_id() -> None:
    requests: list[httpx.Request] = []

    with client(requests, family="grocery") as lidl:
        products = lidl.get_campaign_products("italiamo", 10091863)
        with pytest.raises(ConfigurationError, match="slug"):
            lidl.get_campaign_products("  ", 10091863)

    assert requests[0].url.path == "/c/italiamo/a10091863"
    assert all(product.family is ProductFamily.GROCERY for product in products)


def test_unparsable_tiles_are_skipped() -> None:
    page = '<div data-grid-data="{&quot;broken&quot;:"></div>' + read_fixture(
        STORE, "campaign.html"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, html=page)

    with Lidl(transport=httpx.MockTransport(handler)) as lidl:
        assert len(lidl.get_offers()) == 2


# -------------------------------------------------------------------- leaflets


def test_leaflets_use_the_bound_region_and_the_tenant_locale() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region=26) as lidl:
        leaflets = lidl.get_leaflets()

    assert len(requests) == 1
    assert requests[0].url.host == "endpoints.leaflets.schwarz"
    assert params_of(requests[0]) == {
        "client_locale": ["lidl/es-ES"],
        "region_id": ["26"],
    }
    assert [leaflet.name for leaflet in leaflets] == [
        "FOLLETO ALIMENTACIÓN 14/9",
        "FOLLETO BAZAR 14/9",
    ]
    assert leaflets[0].category == "Folletos"
    assert leaflets[0].subcategory
    assert "23" in leaflets[0].regions
    assert leaflets[0].pdf_url is not None


def test_leaflets_can_be_asked_for_one_region_or_one_store() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region=26) as lidl:
        lidl.get_leaflets(region=0)
        lidl.get_leaflets(store="ES00726")
    with client(requests) as lidl:
        lidl.get_leaflets()

    assert params_of(requests[0])["region_id"] == ["0"]
    assert params_of(requests[1])["store_id"] == ["ES00726"]
    assert "region_id" not in params_of(requests[2])


def test_a_leaflet_carries_pages_and_hotspots_but_no_food_products() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region=26) as lidl:
        leaflet = lidl.get_leaflet("folleto-alimentacion-14-9-14-9-26-20-9-26-6c4d9a")

    assert str(requests[0].url) == (
        f"{LEAFLETS}/flyer?flyer_identifier="
        "folleto-alimentacion-14-9-14-9-26-20-9-26-6c4d9a&region_id=26"
    )
    assert leaflet.products == ()
    assert [page.number for page in leaflet.pages] == [1, 2]
    assert leaflet.pages[0].image_url is not None
    assert leaflet.pages[0].links[0].kind == "standard"


def test_a_leaflet_reports_current_even_before_its_offers_start() -> None:
    from datetime import date

    with client() as lidl:
        leaflet = lidl.get_leaflet("folleto-alimentacion")

    assert leaflet.status == "current"
    assert leaflet.starts_on == date(2026, 9, 7)
    assert leaflet.offer_starts_on == date(2026, 9, 14)
    assert leaflet.is_live_on(date(2026, 9, 10)) is False
    assert leaflet.is_live_on(date(2026, 9, 15)) is True


def test_the_leaflet_api_reports_unused_filters_instead_of_failing() -> None:
    # a warning in the envelope is not an error; the client ignores it
    with client() as lidl:
        leaflet = lidl.get_leaflet("folleto-alimentacion", store="ES00319")

    assert leaflet.id


def test_a_bazar_leaflet_carries_structured_products() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, request=request, json=read_fixture(STORE, "leaflet_bazar.json")
        )

    with Lidl(transport=httpx.MockTransport(handler)) as lidl:
        leaflet = lidl.get_leaflet("folleto-bazar-14-9-14-9-26-20-9-26-125083")

    assert len(leaflet.products) == 2
    assert leaflet.products[0].price == Decimal("12.99")
    assert leaflet.products[0].brand


# ---------------------------------------------------------------------- quirks


def test_the_same_locale_is_spelled_four_ways() -> None:
    requests: list[httpx.Request] = []

    with client(requests, region=26) as lidl:
        lidl.search_products("pan")
        lidl.get_product("11150856")
        lidl.list_stores()
        lidl.get_leaflets()

    assert params_of(requests[0])["locale"] == ["es_ES"]
    assert requests[0].url.path == "/q/api/search"
    assert requests[1].url.path.endswith("/ES/es")
    assert params_of(requests[2])["locale"] == ["es-ES"]
    assert params_of(requests[-1])["client_locale"] == ["lidl/es-ES"]


def test_every_listing_asks_for_the_search_vendor_type() -> None:
    # the search api answers a bare application/json with http 406, while the
    # other lidl.es endpoints keep answering it
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        lidl.search_products("pan")
        lidl.get_categories()
        lidl.get_category("10067761")
        lidl.get_product("11150856")

    assert len(requests) == 4
    assert [request.headers["Accept"] for request in requests[:3]] == [
        "application/mindshift.search+json;version=2"
    ] * 3
    assert requests[3].headers["Accept"] == "application/json"


def test_the_search_api_version_is_sent_on_every_listing() -> None:
    requests: list[httpx.Request] = []

    with client(requests) as lidl:
        lidl.search_products("pan")
        lidl.get_categories()
        lidl.get_category("10067761")

    assert all(params_of(request)["version"] == ["2.1.0"] for request in requests)
    assert all(params_of(request)["assortment"] == ["ES"] for request in requests)


def test_a_gzipped_sitemap_body_is_decompressed_once() -> None:
    body = gzip.compress(read_fixture(STORE, "product_sitemap.xml").encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=body)

    with Lidl(transport=httpx.MockTransport(handler)) as lidl:
        assert len(list(lidl.iter_catalog_ids())) == 4


@pytest.mark.parametrize("cursor", ["", "  ", "two", "-2", 2])
def test_an_invalid_cursor_is_rejected(cursor: Any) -> None:
    with client() as lidl, pytest.raises(ConfigurationError, match="cursor"):
        lidl.search_products("pan", cursor=cursor)


def test_a_store_without_a_zone_leaves_the_binding_alone() -> None:
    page = read_fixture(STORE, "stores_page1.json")
    trimmed = {
        "meta": {"total": 1},
        "items": [{**page["items"][0], "marketingData": {"offerRegion": 26}}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=trimmed)

    with Lidl.from_postal_code("08013", transport=httpx.MockTransport(handler)) as lidl:
        assert lidl.region == 26
        assert lidl.zone == PriceZone.PENINSULA


def test_a_campaign_page_honours_a_shop_family_filter() -> None:
    # every tile on a grocery campaign page is a grocery product, so a shop
    # filter empties the page rather than half-filling it
    with client(family="shop") as lidl:
        assert lidl.get_offers() == ()
