"""the unit normaliser restates every store's unit price in one shape."""

from __future__ import annotations

import dataclasses
from decimal import Decimal, localcontext

import pytest

from supermercapy import Price, Unit, UnitPrice, to_dict
from supermercapy._core.units import SHARED_UNITS, Quantity, UnitReader

KG, L, PIECE, DOSE, M, M2 = (
    Unit.KILOGRAM,
    Unit.LITRE,
    Unit.PIECE,
    Unit.DOSE,
    Unit.METRE,
    Unit.SQUARE_METRE,
)


@pytest.mark.parametrize(
    ("amount", "text", "expected", "unit"),
    [
        # one of the unit, in every spelling the stores send
        ("1.2300", "L", "1.23", L),
        ("0.84", "1 L", "0.84", L),
        ("0.89", "l.", "0.89", L),
        ("1.20", "kg", "1.20", KG),
        ("2.79", "1 Kg", "2.79", KG),
        ("25.67", "kg.", "25.67", KG),
        ("10.35", "kg..", "10.35", KG),
        ("19.90", "€/kg", "19.90", KG),
        ("0.08", "/ m", "0.08", M),
        ("0.07", "1 M", "0.07", M),
        ("8.32", "m²", "8.32", M2),
        ("8.32", "m2", "8.32", M2),
        ("0.21", "u.", "0.21", PIECE),
        ("0.21", "1 U", "0.21", PIECE),
        ("0.384", "ud", "0.384", PIECE),
        ("0.19", "Un", "0.19", PIECE),
        ("1.43", "EACH", "1.43", PIECE),
        ("0.07", "lavado", "0.07", DOSE),
        ("0.33", "dosis", "0.33", DOSE),
        ("1.00", "  KG  ", "1.00", KG),
        # a reference quantity other than one
        ("6.2", "100 Gr", "62.00", KG),
        ("4.6", "100 ml", "46.00", L),
        ("1.10", "75 cl", "1.4667", L),
        ("2", "1,5 l", "1.3333", L),
        ("2.62", "docena", "0.2183", PIECE),
        ("6.90", "dotzena", "0.575", PIECE),
        ("3.05", "0,5 docena", "0.5083", PIECE),
        ("0", "kg", "0.00", KG),
    ],
)
def test_shared_vocabulary_restates_the_amount_per_one_unit(
    amount: str, text: str, expected: str, unit: Unit
) -> None:
    result = SHARED_UNITS.unit_price(Decimal(amount), text)
    assert result == UnitPrice(amount=Decimal(expected), unit=unit)
    assert result is not None
    assert str(result.amount) == expected


@pytest.mark.parametrize(
    "text",
    [
        # store codes the shared vocabulary leaves to each store's aliases
        "dc",
        "dz",
        "lv",
        "Do",
        "Dot",
        "d.",
        "PER_1KG",
        "fop.price.per.kg",
        # not unit text at all
        "",
        "   ",
        ".",
        "12",
        "0 kg",
        "kg kg",
        "1 ud 6,77 €/kg / 2 uds 6,54 €/kg",
        None,
        5,
        ["kg"],
    ],
)
def test_unknown_text_gives_none(text: object) -> None:
    assert SHARED_UNITS.unit_price(Decimal("1.00"), text) is None
    assert SHARED_UNITS.quantity(text) is None


@pytest.mark.parametrize(
    "amount",
    [None, Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity"), Decimal("sNaN")],
)
def test_an_unusable_amount_gives_none(amount: Decimal | None) -> None:
    assert SHARED_UNITS.unit_price(amount, "kg") is None


@pytest.mark.parametrize(
    ("per", "expected"),
    [
        (Decimal(1), "5.25"),
        (Decimal(2), "2.625"),
        (Decimal("0.25"), "21.00"),
        (Decimal(80), "0.0656"),
    ],
)
def test_a_structured_quantity_divides_the_amount(per: Decimal, expected: str) -> None:
    result = SHARED_UNITS.unit_price(Decimal("5.25"), "l", per=per)
    assert result == UnitPrice(amount=Decimal(expected), unit=L)


@pytest.mark.parametrize(
    "per", [None, Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")]
)
def test_an_unusable_structured_quantity_gives_none(per: Decimal | None) -> None:
    assert SHARED_UNITS.unit_price(Decimal("5.25"), "l", per=per) is None


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("1.2300", "1.23"),
        ("2.625", "2.625"),
        ("10", "10.00"),
        ("0.21875", "0.2188"),
        ("0.00005", "0.0001"),
        ("0.00004", "0.00"),
        ("1234567.891", "1234567.891"),
    ],
)
def test_amounts_round_half_up_to_four_places_and_keep_at_least_two(
    amount: str, expected: str
) -> None:
    result = SHARED_UNITS.unit_price(Decimal(amount), "kg")
    assert result is not None
    assert str(result.amount) == expected


def test_the_callers_decimal_context_does_not_change_the_result() -> None:
    with localcontext() as context:
        context.prec = 2
        result = SHARED_UNITS.unit_price(Decimal("2.625"), "docena")
    assert result == UnitPrice(amount=Decimal("0.2188"), unit=PIECE)


def test_quantity_reads_the_count_and_the_unit() -> None:
    assert SHARED_UNITS.quantity("100 Gr") == Quantity(amount=Decimal("0.100"), unit=KG)
    assert SHARED_UNITS.quantity("kg") == Quantity(amount=Decimal(1), unit=KG)
    assert SHARED_UNITS.quantity("1 docena") == Quantity(amount=Decimal(12), unit=PIECE)


def test_store_aliases_extend_and_override_the_shared_vocabulary() -> None:
    reader = UnitReader(
        {"dc": "docena", "LV.": "lavado", "per_100ml": "100 ml", "ml": None}
    )
    assert reader.unit_price(Decimal("3.05"), "dc") == UnitPrice(
        amount=Decimal("0.2542"), unit=PIECE
    )
    assert reader.unit_price(Decimal("7.38"), "1 Dc") == UnitPrice(
        amount=Decimal("0.615"), unit=PIECE
    )
    assert reader.unit_price(Decimal("0.07"), "lv") == UnitPrice(
        amount=Decimal("0.07"), unit=DOSE
    )
    assert reader.unit_price(Decimal("1.50"), "PER_100ML") == UnitPrice(
        amount=Decimal("15.00"), unit=L
    )
    # an alias to None blocks a shared word, and the rest stay shared
    assert reader.unit_price(Decimal("60.85"), "ml.") is None
    assert reader.unit_price(Decimal("4.6"), "100 ml") is None
    assert reader.unit_price(Decimal("1.23"), "L") == UnitPrice(
        amount=Decimal("1.23"), unit=L
    )
    # aliases belong to the reader that declared them
    assert SHARED_UNITS.unit_price(Decimal("3.05"), "dc") is None


@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        ({"dc": "decena"}, "cannot read"),
        ({"dc": "dz"}, "cannot read"),
        ({"dc": ""}, "cannot read"),
        ({"1 dc": "docena"}, "not a bare unit code"),
        ({"": "docena"}, "not a bare unit code"),
    ],
)
def test_an_alias_that_cannot_resolve_fails_when_the_reader_is_built(
    aliases: dict[str, str | None], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        UnitReader(aliases)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,55 €/kg", UnitPrice(amount=Decimal("1.55"), unit=KG)),
        ("0,08 €/m.", UnitPrice(amount=Decimal("0.08"), unit=M)),
        ("  5,05 € / kg ", UnitPrice(amount=Decimal("5.05"), unit=KG)),
        ("9,5 €/kg", UnitPrice(amount=Decimal("9.50"), unit=KG)),
        ("1.234,50 €/kg", UnitPrice(amount=Decimal("1234.50"), unit=KG)),
        ("6,20 €/100 g", UnitPrice(amount=Decimal("62.00"), unit=KG)),
        ("1 ud 6,77 €/kg / 2 uds 6,54 €/kg", None),
        ("desde 1,55 €/kg", None),
        ("1,55 €/kg aprox", None),
        ("5,05 €", None),
        ("€/kg", None),
        ("1,55 €/dc", None),
        ("", None),
        (None, None),
    ],
)
def test_display_text_holding_one_price_per_unit_is_read(
    text: object, expected: UnitPrice | None
) -> None:
    assert SHARED_UNITS.unit_price_from_text(text) == expected


def test_units_are_short_string_values() -> None:
    assert {unit.name: unit.value for unit in Unit} == {
        "KILOGRAM": "kg",
        "LITRE": "l",
        "PIECE": "piece",
        "DOSE": "dose",
        "METRE": "m",
        "SQUARE_METRE": "m2",
    }


def test_the_reference_is_an_additive_frozen_field_on_price() -> None:
    assert Price().reference is None
    price = Price(
        amount=Decimal("1.55"),
        unit_price=Decimal("6.2"),
        unit_price_unit="100 Gr",
        reference=UnitPrice(amount=Decimal("62.00"), unit=KG),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        price.reference.amount = Decimal(1)  # type: ignore[misc,union-attr]
    assert not hasattr(price.reference, "__dict__")
    assert to_dict(price)["reference"] == {"amount": "62.00", "unit": "kg"}
    assert to_dict(price)["unit_price_unit"] == "100 Gr"
