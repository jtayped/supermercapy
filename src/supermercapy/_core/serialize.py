"""turn models into json-compatible plain python, and from there into json.

nothing here reads or writes anything: the functions only walk the value they
are given. properties such as ``SearchResult.has_more`` are not dataclass
fields, so they are not part of the output.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = ["to_dict", "to_json"]


def to_dict(value: Any) -> Any:
    """convert a model, or a collection of them, to json-compatible python.

    a dataclass becomes a dict of its fields in declaration order, so the
    result of a model is a dict and the result of a list or tuple of models,
    such as ``get_catalog()``, is a list. tuples, lists, and sets become lists
    (sets are sorted by the string form of each converted item so the output is
    deterministic), and dict keys become strings. ``Decimal`` becomes a string
    such as ``"1.69"``: a float would round the price the storefront published.
    dates and times become iso 8601 strings and enums become their value.
    strings, numbers, booleans, and ``None`` pass through. anything else raises
    ``TypeError`` naming its type.
    """

    if value is None or (
        isinstance(value, (str, int, float, bool)) and not isinstance(value, Enum)
    ):
        return value
    if isinstance(value, Enum):
        return to_dict(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_dict(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [to_dict(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((to_dict(item) for item in value), key=str)
    if isinstance(value, dict):
        return {_key(key): to_dict(item) for key, item in value.items()}
    raise TypeError(f"cannot serialise {type(value).__name__} to json")


def _key(key: Any) -> str:
    converted = to_dict(key)
    return converted if isinstance(converted, str) else str(converted)


def to_json(value: Any, *, indent: int | None = None) -> str:
    """return ``value`` as a json string, converted by :func:`to_dict`.

    non-ascii text is kept as written rather than escaped.
    """

    return json.dumps(to_dict(value), ensure_ascii=False, indent=indent)
