"""caprabo's client: its own host, two languages, the shared request flow."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from supermercapy import Capability, ConfigurationError, Language, NotFoundError
from supermercapy.caprabo import (
    Caprabo,
    CapraboCategory,
    CapraboProduct,
    CapraboSearchResult,
)
from tests.harness import HARNESSES

STORE = "caprabo"
HOST = "www.capraboacasa.com"


def recording(requests: list[httpx.Request], **options: Any) -> Caprabo:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return HARNESSES[STORE].handler(request)

    client = HARNESSES[STORE].client(handler, **options)
    assert isinstance(client, Caprabo)
    return client


def test_caprabo_speaks_spanish_and_catalan_only() -> None:
    assert Caprabo.supported_languages == {Language.SPANISH, Language.CATALAN}
    assert Caprabo.default_language is Language.SPANISH
    assert Caprabo.capabilities == {
        Capability.CATALOG,
        Capability.NUTRITION,
        Capability.PROMOTIONS,
    }
    with pytest.raises(ConfigurationError, match="language"):
        Caprabo(language="en")
    with Caprabo(language="ca") as client:
        assert client.language is Language.CATALAN
        assert client.store_id is None
        assert client.min_request_interval == 1.0


def test_every_request_goes_to_caprabos_own_host() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        result = client.search_products("leche")
        product = client.get_product("18581678")
        category = client.get_category("2059808")
        with pytest.raises(NotFoundError):
            client.get_product("99999999")

    assert {request.url.host for request in requests} == {HOST}
    assert [request.url.path for request in requests] == [
        "/es/search/results:loadpage",
        "/es/productdetail/18581678-producto/",
        "/es/",
        "/es/supermarket:loadpage",
        "/es/productdetail/99999999-producto/",
    ]
    assert isinstance(result, CapraboSearchResult)
    assert isinstance(product, CapraboProduct)
    assert isinstance(category, CapraboCategory)
    assert category.name == "Leche semidesnatada"
    assert requests[3].url.params["t:ac"] == (
        "2059806-alimentacion/2059807-leche-batidos-y-bebidas-vegetales/"
        "2059808-leche-semidesnatada"
    )


def test_a_catalog_walk_follows_each_root_to_its_empty_page() -> None:
    requests: list[httpx.Request] = []

    with recording(requests) as client:
        rows = list(client.iter_catalog())

    # three roots, one canned page each, then the empty page that ends it
    assert len(rows) == 6
    pages = [request.url.params.get("pageNumber") for request in requests]
    assert pages == [None, "0", "1", "0", "1", "0", "1"]


def test_the_catalan_storefront_lives_under_ca() -> None:
    requests: list[httpx.Request] = []

    with recording(requests, language="ca") as client, pytest.raises(NotFoundError):
        client.get_category("2059808")

    # the canned menu is served for spanish only, so the catalan one is a 404
    assert [request.url.path for request in requests] == ["/ca/"]
