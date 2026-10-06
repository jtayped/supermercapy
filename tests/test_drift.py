"""the drift comparator the live suite relies on, checked offline."""

from __future__ import annotations

import pytest

from tests.drift import assert_no_drift, drift, shape


def test_shape_merges_array_elements_and_records_kinds() -> None:
    document = {"items": [{"id": 1, "name": "a"}, {"id": "2", "tags": []}]}
    assert shape(document) == {
        "$": {"object"},
        "$.items": {"array"},
        "$.items[]": {"object"},
        "$.items[].id": {"number", "string"},
        "$.items[].name": {"string"},
        "$.items[].tags": {"array"},
    }


def test_identical_and_wider_documents_do_not_drift() -> None:
    fixture = {"product": {"id": "1", "price": 1.5, "flags": [True]}}
    live = {"product": {"id": "9", "price": 2, "flags": [False], "new": {}}}
    assert drift(fixture, fixture) == []
    assert drift(live, fixture) == []


def test_missing_and_retyped_paths_are_reported() -> None:
    fixture = {"product": {"id": "1", "price": 1.5, "photos": [{"url": "x"}]}}
    live = {"product": {"id": 1, "photos": [{"href": "x"}]}}
    assert drift(live, fixture) == [
        "retyped $.product.id: fixture string, live number",
        "missing $.product.photos[].url (fixture: string)",
        "missing $.product.price (fixture: number)",
    ]


def test_nulls_and_empty_live_containers_are_not_drift() -> None:
    fixture = {"promo": {"label": "2x1"}, "photos": [{"url": "x"}], "ean": "84"}
    live = {"promo": None, "photos": [], "ean": None}
    assert drift(live, fixture) == []


def test_ignored_paths_skip_everything_below_them() -> None:
    fixture = {"promo": {"label": "2x1"}, "promotions": [1], "id": "1"}
    live = {"id": "1", "promotions": [2]}
    assert drift(live, fixture, ignore=("$.promo",)) == []
    assert drift(live, fixture) == ["missing $.promo (fixture: object)"]


def test_assert_no_drift_names_the_document() -> None:
    assert_no_drift({"a": 1}, {"a": 2}, label="ok")
    with pytest.raises(AssertionError, match="search drifted from its fixture"):
        assert_no_drift({}, {"a": 1}, label="search")
