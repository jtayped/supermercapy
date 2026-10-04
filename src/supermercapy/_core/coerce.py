"""tolerant conversions from raw json values to model field values.

every helper returns ``None`` (or an empty container) for a value of the wrong
type, so a parser can read optional fields without branching. only
:func:`as_identifier` and :func:`required_text` raise, because a record without
an identity is not usable at all.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TypeAlias

from .exceptions import InvalidResponseError

JsonObject: TypeAlias = dict[str, object]

_EURO_TEXT = re.compile(r"-?\d+(?:[.,]\d{3})*(?:[.,]\d+)?")


def as_object(value: object) -> JsonObject:
    """return ``value`` when it is a json object, otherwise an empty one."""

    return value if isinstance(value, dict) else {}


def as_items(value: object) -> list[object]:
    """return ``value`` when it is a json array, otherwise an empty list."""

    return value if isinstance(value, list) else []


def as_text(value: object) -> str | None:
    """return ``value`` when it is a string."""

    return value if isinstance(value, str) else None


def as_identifier(value: object, label: str) -> str:
    """return a non-empty string id from a string or integer, or raise."""

    if isinstance(value, bool) or value is None:
        raise InvalidResponseError(f"response has no usable {label}")
    if isinstance(value, (str, int)):
        result = str(value).strip()
        if result:
            return result
    raise InvalidResponseError(f"response has no usable {label}")


def required_text(value: object, label: str) -> str:
    """return a non-blank string, or raise."""

    if isinstance(value, str) and value.strip():
        return value
    raise InvalidResponseError(f"response has no usable {label}")


def as_decimal(value: object) -> Decimal | None:
    """convert a number or numeric string to ``Decimal``."""

    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def as_integer(value: object) -> int | None:
    """return ``value`` when it is an integer and not a boolean."""

    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_boolean(value: object) -> bool | None:
    """return ``value`` when it is a boolean."""

    return value if isinstance(value, bool) else None


def as_cents(value: object) -> Decimal | None:
    """convert an integer amount of cents to a two-decimal euro value."""

    cents = as_integer(value)
    if cents is None:
        return None
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def as_euro_text(value: object) -> Decimal | None:
    """extract the first number from display text such as ``"5,05 €/kg"``."""

    text = as_text(value)
    if text is None:
        return None
    match = _EURO_TEXT.search(text)
    if match is None:
        return None
    number = match.group(0)
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        parts = number.split(",")
        number = (
            "".join(parts)
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3)
            else number.replace(",", ".")
        )
    elif number.count(".") > 1:
        number = number.replace(".", "")
    try:
        return Decimal(number)
    except InvalidOperation:  # pragma: no cover - the regex prevents this
        return None


def as_iso_datetime(value: object) -> datetime | None:
    """parse an iso 8601 timestamp; naive values are taken as utc."""

    text = as_text(value)
    if text is None or not text.strip():
        return None
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def as_dmy_date(value: object) -> date | None:
    """parse a ``dd/mm/yyyy`` or ``dd-mm-yyyy`` date."""

    text = as_text(value)
    if text is None:
        return None
    match = re.fullmatch(r"\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*", text)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None
