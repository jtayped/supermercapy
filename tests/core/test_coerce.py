from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from supermercapy import InvalidResponseError
from supermercapy._core.coerce import (
    as_boolean,
    as_cents,
    as_decimal,
    as_dmy_date,
    as_euro_text,
    as_identifier,
    as_integer,
    as_iso_datetime,
    as_items,
    as_object,
    as_text,
    required_text,
)


def test_shape_helpers_fall_back_to_empty_values() -> None:
    assert as_object({"a": 1}) == {"a": 1}
    assert as_object([1]) == {}
    assert as_items([1]) == [1]
    assert as_items("x") == []
    assert as_text("x") == "x"
    assert as_text(1) is None
    assert as_integer(3) == 3
    assert as_integer(True) is None
    assert as_boolean(False) is False
    assert as_boolean(0) is None


def test_identity_helpers_raise_when_missing() -> None:
    assert as_identifier(7, "id") == "7"
    assert as_identifier(" a ", "id") == "a"
    assert required_text("name", "name") == "name"
    for value in (None, True, "", "  ", [], 1.5):
        with pytest.raises(InvalidResponseError, match="id"):
            as_identifier(value, "id")
    for value in (None, "", "  ", 3):
        with pytest.raises(InvalidResponseError, match="name"):
            required_text(value, "name")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" 1.200 ", Decimal("1.200")),
        (3, Decimal("3")),
        (1.5, Decimal("1.5")),
        (Decimal("2"), Decimal("2")),
        ("abc", None),
        (True, None),
        (None, None),
        ([], None),
    ],
)
def test_as_decimal(value: object, expected: Decimal | None) -> None:
    assert as_decimal(value) == expected


def test_as_cents() -> None:
    assert as_cents(199) == Decimal("1.99")
    assert as_cents(5) == Decimal("0.05")
    assert as_cents("199") is None
    assert as_cents(True) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5,05 €/kg", Decimal("5.05")),
        ("1.234,56 €", Decimal("1234.56")),
        ("1,234.56", Decimal("1234.56")),
        ("12.50", Decimal("12.50")),
        ("1.234.567", Decimal("1234567")),
        ("1,234", Decimal("1234")),
        ("0,99", Decimal("0.99")),
        ("-3,5", Decimal("-3.5")),
        ("gratis", None),
        (12, None),
    ],
)
def test_as_euro_text(value: object, expected: Decimal | None) -> None:
    assert as_euro_text(value) == expected


def test_as_iso_datetime() -> None:
    assert as_iso_datetime("2026-09-14T10:00:00Z") == datetime(
        2026, 9, 14, 10, tzinfo=UTC
    )
    assert as_iso_datetime("2026-09-14T10:00:00+02:00") == datetime(
        2026, 9, 14, 10, tzinfo=timezone(timedelta(hours=2))
    )
    assert as_iso_datetime("2026-09-14T10:00:00") == datetime(
        2026, 9, 14, 10, tzinfo=UTC
    )
    assert as_iso_datetime("2026-09-14") == datetime(2026, 9, 14, tzinfo=UTC)
    for value in ("", "  ", "soon", 5, None):
        assert as_iso_datetime(value) is None


def test_as_dmy_date() -> None:
    assert as_dmy_date("14/09/2026") == date(2026, 9, 14)
    assert as_dmy_date(" 1-2-2026 ") == date(2026, 2, 1)
    for value in ("2026-09-14", "31/02/2026", "x", 3):
        assert as_dmy_date(value) is None
