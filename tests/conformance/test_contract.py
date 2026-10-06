"""the contract every store client honours, checked against each one."""

from __future__ import annotations

import dataclasses
import importlib
from dataclasses import FrozenInstanceError
from typing import Any

import httpx
import pytest

import supermercapy
from supermercapy import (
    BaseClient,
    Capability,
    Category,
    ConfigurationError,
    Language,
    NotFoundError,
    Product,
    ProductSummary,
    SearchResult,
    UnsupportedOperationError,
)
from supermercapy._core.capabilities import OPERATION_METHODS
from tests.harness import HARNESSES, Harness

pytestmark = pytest.mark.parametrize(
    "harness", list(HARNESSES.values()), ids=list(HARNESSES)
)


def _defining_class(cls: type, name: str) -> type:
    for base in cls.__mro__:
        if name in base.__dict__:
            return base
    raise AssertionError(f"{cls.__name__} has no attribute {name}")


def test_client_is_registered_and_described(harness: Harness) -> None:
    cls = harness.client_type
    assert cls in supermercapy.ALL_CLIENTS
    assert cls.__name__ in supermercapy.__all__
    assert issubclass(cls, BaseClient)
    assert cls.store_name and cls.store_name == cls.store_name.lower()
    assert isinstance(cls.capabilities, frozenset)
    assert cls.default_language in cls.supported_languages
    assert cls.default_page_size >= 1
    assert cls.max_page_size is None or cls.max_page_size >= cls.default_page_size
    module = importlib.import_module(harness.module)
    assert cls.__name__ in module.__all__


def test_declared_capabilities_match_overridden_methods(harness: Harness) -> None:
    cls = harness.client_type
    for capability, method in OPERATION_METHODS.items():
        overridden = _defining_class(cls, method) is not BaseClient
        assert overridden == cls.supports(capability), (
            f"{cls.__name__}.{method} override does not match {capability}"
        )


def test_unsupported_operations_raise_before_any_request(harness: Harness) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("an unsupported operation performed I/O")

    with harness.client(handler) as client:
        for capability, method in OPERATION_METHODS.items():
            if client.supports(capability):
                continue
            arguments: tuple[Any, ...] = (
                ("28001",) if capability is Capability.POSTAL_CODE else ()
            )
            if capability is Capability.EAN_LOOKUP:
                arguments = ("8480000000000",)
            target = (
                harness.client_type if capability is Capability.POSTAL_CODE else client
            )
            with pytest.raises(UnsupportedOperationError) as raised:
                result = getattr(target, method)(*arguments)
                if capability is Capability.CATALOG:
                    next(iter(result))
            assert raised.value.capability is capability
            assert raised.value.store == harness.client_type.store_name
        if not client.supports(Capability.CATALOG):
            with pytest.raises(UnsupportedOperationError):
                client.get_catalog()
    assert requests == []


def test_constructor_performs_no_io_and_exposes_properties(harness: Harness) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("constructor performed I/O")

    client = harness.client(handler, min_request_interval=0.25)
    assert client.language is harness.client_type.default_language
    assert client.store_id is None or isinstance(client.store_id, str)
    assert client.min_request_interval == 0.25
    assert client.user_agent
    assert not client.is_closed
    assert requests == []
    with client as entered:
        assert entered is client
    assert client.is_closed
    with pytest.raises(ConfigurationError, match="closed"), client:
        pass


@pytest.mark.parametrize("language", ["xx", "", None])
def test_language_is_validated_and_defaulted(
    harness: Harness, language: str | None
) -> None:
    if language is None:
        with harness.client(language=None) as client:
            assert client.language is harness.client_type.default_language
        return
    with pytest.raises(ConfigurationError, match="language"):
        harness.client(language=language)
    unsupported = set(Language) - harness.client_type.supported_languages
    for value in unsupported:
        with pytest.raises(ConfigurationError, match="language"):
            harness.client(language=value)


def test_search_returns_a_page_and_iter_search_terminates(harness: Harness) -> None:
    with harness.client() as client:
        result = client.search_products(harness.query)
        assert isinstance(result, SearchResult)
        assert result.query == harness.query
        assert isinstance(result.products, tuple)
        assert result.products
        assert all(isinstance(item, ProductSummary) for item in result.products)
        assert result.page_size >= 1
        assert result.total_hits is None or result.total_hits >= 0
        assert result.has_more == (result.next_cursor is not None)
        if result.next_cursor is not None:
            following = client.search_products(harness.query, cursor=result.next_cursor)
            assert isinstance(following, SearchResult)
        streamed = list(client.iter_search(harness.query))
        assert streamed[: len(result.products)] == list(result.products)


def test_get_product_returns_frozen_slotted_product(harness: Harness) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return harness.handler(request)

    with harness.client(handler) as client:
        product = client.get_product(harness.product_id)
        count = len(requests)
        assert isinstance(product, Product)
        assert product.id == harness.product_id
        assert product.name
        assert isinstance(product.photos, tuple)
        assert isinstance(product.category_ids, tuple)
        assert isinstance(product.promotions, tuple)
        assert product.price.currency == "EUR"
        assert len(requests) == count
    assert not hasattr(product, "__dict__")
    assert dataclasses.is_dataclass(product)
    with pytest.raises(FrozenInstanceError):
        product.name = "changed"  # type: ignore[misc]
    if not harness.client_type.supports(Capability.EAN):
        assert product.ean is None
    if not harness.client_type.supports(Capability.NUTRITION):
        assert product.nutrition is None
    if not harness.client_type.supports(Capability.PROMOTIONS):
        assert product.promotions == ()


def test_missing_product_raises_not_found(harness: Harness) -> None:
    with harness.client() as client, pytest.raises(NotFoundError):
        client.get_product(harness.missing_product_id)


def test_categories_and_category(harness: Harness) -> None:
    with harness.client() as client:
        categories = client.get_categories()
        assert isinstance(categories, tuple)
        assert categories
        assert all(isinstance(item, Category) for item in categories)
        category = client.get_category(harness.category_id)
        assert isinstance(category, Category)
        assert category.id == harness.category_id
        assert isinstance(category.children, tuple)
        assert isinstance(category.products, tuple)


def test_catalog_is_deduplicated_when_supported(harness: Harness) -> None:
    if not harness.client_type.supports(Capability.CATALOG):
        pytest.skip("store does not enumerate its catalog")
    with harness.client() as client:
        catalog = client.get_catalog()
    assert isinstance(catalog, tuple)
    assert all(isinstance(item, ProductSummary) for item in catalog)
    assert len({item.id for item in catalog}) == len(catalog)


def test_request_pacing_spaces_request_starts(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [10.0]
    delays: list[float] = []

    def sleep(delay: float) -> None:
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr("supermercapy._core.client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("supermercapy._core.client.time.sleep", sleep)
    with harness.client(min_request_interval=0.5) as client:
        client.get_categories()
        clock[0] += 0.2
        client.get_categories()
    assert delays == pytest.approx([0.3])


def test_identifiers_are_validated(harness: Harness) -> None:
    with harness.client() as client:
        for method in ("get_product", "get_category"):
            for argument in ("", "   ", True, []):
                with pytest.raises(ConfigurationError):
                    getattr(client, method)(argument)
        with pytest.raises(ConfigurationError):
            client.search_products(None)  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError, match="page_size"):
            client.search_products(harness.query, page_size=0)
