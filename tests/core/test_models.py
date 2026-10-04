from __future__ import annotations

import dataclasses

import pytest

import supermercapy
from supermercapy import (
    Capability,
    Photo,
    Product,
    ProductSummary,
    SearchResult,
    UnsupportedOperationError,
)
from supermercapy._core.capabilities import DATA_CAPABILITIES, OPERATION_METHODS


def test_photo_requires_a_url() -> None:
    assert Photo(url="https://x.test/a.jpg").kind is None
    for value in ("", "  ", None):
        with pytest.raises(ValueError, match="url"):
            Photo(url=value)  # type: ignore[arg-type]


def test_search_result_has_more_follows_cursor() -> None:
    result = SearchResult(query="q", products=(), page_size=10)
    assert not result.has_more
    assert SearchResult(query="q", products=(), page_size=1, next_cursor="1").has_more


def test_product_is_a_summary_with_slots() -> None:
    product = Product(id="1", name="n")
    assert isinstance(product, ProductSummary)
    assert not hasattr(product, "__dict__")
    assert product.nutrition is None
    assert dataclasses.asdict(product)["price"]["currency"] == "EUR"


def test_capabilities_partition_into_operations_and_data() -> None:
    assert set(OPERATION_METHODS) | DATA_CAPABILITIES == set(Capability)
    assert not set(OPERATION_METHODS) & DATA_CAPABILITIES
    error = UnsupportedOperationError(Capability.HOME, "stub")
    assert str(error) == "stub does not support home"
    assert isinstance(error, ValueError)


def test_public_api_is_deliberate_and_versioned() -> None:
    assert supermercapy.__version__ == "0.1.0"
    assert set(supermercapy.__all__) >= {"BaseClient", "Capability", "ALL_CLIENTS"}
    assert "parse_product" not in supermercapy.__all__
    assert "CatalogResult" not in supermercapy.__all__
    assert all(cls.__name__ in supermercapy.__all__ for cls in supermercapy.ALL_CLIENTS)
