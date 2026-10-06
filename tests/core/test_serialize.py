"""to_dict and to_json turn models into plain, json-compatible python."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum, IntEnum

import pytest

from supermercapy import (
    Availability,
    Category,
    HomeSection,
    Language,
    Nutrition,
    NutritionValue,
    Photo,
    Price,
    Product,
    ProductSummary,
    Promotion,
    SearchResult,
    Store,
    to_dict,
    to_json,
)
from tests.harness import HARNESSES

PHOTO = Photo(url="https://img.test/a.jpg", kind="main")
SUMMARY = ProductSummary(
    id="1",
    name="leche ñandú",
    price=Price(
        amount=Decimal("1.69"),
        valid_from=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
    ),
    availability=Availability(available=True, max_quantity=Decimal("6")),
    thumbnail=PHOTO,
    category_ids=("a", "b"),
    promotions=(Promotion(id="p", price=Decimal("1.5"), starts_at=None),),
)
PRODUCT = Product(
    id="2",
    name="p",
    photos=(PHOTO,),
    nutrition=Nutrition(values=(NutritionValue(name="fat", per_100="1 g"),)),
    category_path=(Category(id="c", name="c"),),
)
MODELS = [
    PHOTO,
    SUMMARY,
    PRODUCT,
    Price(amount=Decimal("0.10")),
    Availability(),
    Promotion(),
    Nutrition(),
    NutritionValue(name="n"),
    Category(id="1", name="n", children=(Category(id="2", name="m"),)),
    SearchResult(query="q", products=(SUMMARY,), page_size=10, next_cursor="x"),
    Store(id="1", name="s", latitude=41.5, longitude=2.1),
    HomeSection(layout="row", products=(SUMMARY,)),
]


@pytest.mark.parametrize("model", MODELS, ids=lambda model: type(model).__name__)
def test_every_core_model_round_trips_through_json(model: object) -> None:
    text = to_json(model)
    assert json.loads(text) == to_dict(model)
    assert list(json.loads(text)) == list(to_dict(model))


def test_fields_keep_declaration_order_and_properties_are_left_out() -> None:
    result = to_dict(SearchResult(query="q", products=(), page_size=3))
    assert list(result) == [
        "query",
        "products",
        "page_size",
        "total_hits",
        "next_cursor",
        "truncated",
    ]
    assert "has_more" not in result


def test_decimals_stay_exact_strings() -> None:
    assert to_dict(Decimal("1.69")) == "1.69"
    assert to_dict(SUMMARY)["price"]["amount"] == "1.69"
    assert to_dict(Decimal("0.10")) == "0.10"


def test_dates_times_and_enums() -> None:
    assert (
        to_dict(datetime(2026, 1, 2, 3, 4, tzinfo=UTC)) == "2026-01-02T03:04:00+00:00"
    )
    assert to_dict(date(2026, 1, 2)) == "2026-01-02"
    assert to_dict(time(3, 4)) == "03:04:00"
    assert to_dict(Language.CATALAN) == "ca"

    class Level(IntEnum):
        HIGH = 3

    class Mixed(Enum):
        WHEN = date(2026, 1, 2)

    assert to_dict(Level.HIGH) == 3
    assert to_dict(Mixed.WHEN) == "2026-01-02"


def test_scalars_pass_through() -> None:
    for value in ("a", 1, 1.5, True, None):
        assert to_dict(value) is value


def test_collections_become_lists_and_dicts() -> None:
    assert to_dict((1, (2, 3))) == [1, [2, 3]]
    assert to_dict([Photo(url="u")]) == [{"url": "u", "kind": None, "alt": None}]
    assert to_dict({"b", "a", "c"}) == ["a", "b", "c"]
    assert to_dict(frozenset({Decimal("2"), Decimal("10")})) == ["10", "2"]
    assert to_dict({1: Decimal("1"), Language.ENGLISH: (1,), "k": None}) == {
        "1": "1",
        "en": [1],
        "k": None,
    }
    assert to_dict({"p": PHOTO}) == {"p": to_dict(PHOTO)}


def test_a_list_of_models_gives_a_list() -> None:
    result = to_dict((SUMMARY, PRODUCT))
    assert isinstance(result, list)
    assert [item["id"] for item in result] == ["1", "2"]


def test_store_only_models_serialise() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Extra(Product):
        season: Language | None = None
        tags: frozenset[str] = frozenset()

    result = to_dict(
        Extra(id="1", name="n", season=Language.SPANISH, tags=frozenset("ba"))
    )
    assert result["season"] == "es"
    assert result["tags"] == ["a", "b"]


def test_a_class_is_not_treated_as_an_instance() -> None:
    with pytest.raises(TypeError, match="type"):
        to_dict(Photo)


def test_unsupported_objects_raise_naming_the_type() -> None:
    with pytest.raises(TypeError, match="object"):
        to_dict(object())
    with pytest.raises(TypeError, match="bytes"):
        to_json({"k": [b"x"]})


def test_to_json_options() -> None:
    assert to_json(Photo(url="ñ")) == '{"url": "ñ", "kind": null, "alt": null}'
    assert to_json({"a": 1}, indent=2) == '{\n  "a": 1\n}'


def test_a_parsed_store_model_serialises() -> None:
    harness = HARNESSES["mercadona"]
    with harness.client() as client:
        product = client.get_product(harness.product_id)
    data = json.loads(to_json(product))
    assert data["id"] == harness.product_id
    assert isinstance(data["price"]["amount"], str)
