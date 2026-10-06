"""unit prices restated in one shape, so they compare across stores.

every store publishes its unit price against its own unit and in its own
words: ``"L"``, ``"1 Kg"``, ``"100 Gr"``, ``"dc"``, ``"€/u."``,
``"fop.price.per.kg"``. :class:`UnitReader` reads that text into the
:class:`Quantity` it stands for and divides the published amount by it, so a
price per 100 g, per dozen or per lavado comes out as a :class:`UnitPrice` per
kilogram, per piece or per dose.

mapping a new store's unit text, a checklist:

1. inventory first. collect every distinct unit string the store sends, from
   its fixtures and from a few polite live searches (milk, rice, eggs, olive
   oil, detergent, toilet paper, loose bananas, canned beer, wine, foil,
   dishwasher tablets, coffee capsules), and note what each one means and
   how much it refers to: one unit, a hundred grams, a dozen.
2. try :data:`SHARED_UNITS` on each string. it reads an optional leading
   number and one word of the shared vocabulary (``"kg"``, ``"100 g"``,
   ``"1 L"``, ``"docena"``, ``"lavado"``, ``"ud"``, ``"m"``), ignoring case,
   surrounding space, a leading ``€`` or ``/`` and trailing dots.
3. give every code the shared vocabulary does not know an alias in the
   store's own reader, written as shared text:
   ``UnitReader({"dc": "docena", "lv": "lavado"})``. an abbreviation that
   reads differently from store to store (``dc`` is docena at mercadona and
   could be decena elsewhere, ``d.`` is dosis at bonàrea) belongs in the
   store's aliases, never in the shared vocabulary.
4. alias a code the store is known to misuse to ``None`` so it reads as
   unknown: bonàrea writes ``€/ml.`` on a price that is per litre.
5. leave everything else unmapped. unknown text gives ``None``, never a
   guess, and ``None`` means unknown, not zero.
6. pass the amount the shopper pays now: where a promotion replaces the price
   and the store sends a promotional unit price, use that one.
7. compute a unit price from a pack size only when the store publishes the
   quantity as structured data in the unit it names (carrefour's
   ``unit_conversion_factor``), never from a name or free-text pack size, and
   say so on the store's page.
8. assert ``price.reference`` in the store's model tests and add one line on
   its units to ``docs/stores/<store>.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal
from enum import StrEnum
from types import MappingProxyType

from .coerce import as_euro_text

__all__ = ["SHARED_UNITS", "Quantity", "Unit", "UnitPrice", "UnitReader"]


class Unit(StrEnum):
    """the units a :class:`UnitPrice` is expressed in.

    a dozen is twelve pieces, and a lavado (one wash) is one dose, since
    detergent shelf labels price either per wash or per dose for the same
    thing.
    """

    KILOGRAM = "kg"
    LITRE = "l"
    PIECE = "piece"
    DOSE = "dose"
    METRE = "m"
    SQUARE_METRE = "m2"


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitPrice:
    """what one :class:`Unit` costs: one kilogram, one litre, one piece."""

    amount: Decimal
    unit: Unit


@dataclass(frozen=True, slots=True, kw_only=True)
class Quantity:
    """how much of a :class:`Unit` a piece of unit text stands for."""

    amount: Decimal
    unit: Unit


_ONE = Decimal(1)
_CONTEXT = Context(prec=28, rounding=ROUND_HALF_UP)
_PLACES = (Decimal("0.01"), Decimal("0.001"), Decimal("0.0001"))

# an optional "€" or "/", an optional count, then the unit word. the word must
# not start with a digit, and trailing dots are dropped.
_UNIT_TEXT = re.compile(
    r"(?:€\s*)?(?:/\s*)?(?:(?P<count>\d+(?:[.,]\d+)?)\s*)?(?P<word>[^\d\s].*?)\.*"
)
_PRICE_PER_UNIT = re.compile(
    r"\s*(?P<amount>\d+(?:[.,]\d{3})*(?:[.,]\d+)?)\s*€\s*/\s*(?P<unit>[^€/]+?)\s*"
)


def _quantity(amount: str, unit: Unit) -> Quantity:
    return Quantity(amount=Decimal(amount), unit=unit)


def _words(quantity: Quantity, *words: str) -> dict[str, Quantity]:
    return dict.fromkeys(words, quantity)


# unambiguous words only; a store's own abbreviations go in its aliases.
_VOCABULARY: Mapping[str, Quantity] = MappingProxyType(
    {
        **_words(
            _quantity("1", Unit.KILOGRAM),
            *("kg", "kgs", "kilo", "kilos", "kilogramo", "kilogramos"),
            *("kilogram", "kilograms", "quilo", "quilos", "quilogram", "quilograms"),
        ),
        **_words(
            _quantity("0.001", Unit.KILOGRAM),
            *("g", "gr", "grs", "gramo", "gramos", "gram", "grams"),
        ),
        **_words(
            _quantity("1", Unit.LITRE),
            *("l", "lt", "litro", "litros", "litre", "litres", "liter", "liters"),
        ),
        **_words(
            _quantity("0.01", Unit.LITRE),
            *("cl", "centilitro", "centilitros", "centilitre", "centilitres"),
        ),
        **_words(
            _quantity("0.001", Unit.LITRE),
            *("ml", "mililitro", "mililitros", "millilitre", "millilitres"),
        ),
        **_words(
            _quantity("1", Unit.PIECE),
            *("u", "ud", "uds", "un", "unidad", "unidades", "unitat", "unitats"),
            *("each", "piece", "pieces"),
        ),
        **_words(
            _quantity("12", Unit.PIECE),
            *("docena", "docenas", "dotzena", "dotzenes", "dozen"),
        ),
        **_words(
            _quantity("1", Unit.DOSE),
            *("dosis", "dosi", "dose", "doses", "lavado", "lavados"),
            *("rentat", "rentats", "rentada", "rentades", "wash", "washes"),
        ),
        **_words(
            _quantity("1", Unit.METRE),
            *("m", "metro", "metros", "metre", "metres", "meter", "meters"),
        ),
        **_words(_quantity("1", Unit.SQUARE_METRE), "m2", "m²"),
    }
)


def _split(text: object) -> tuple[Decimal | None, str] | None:
    """return the count, if any, and the unit word of one unit text."""

    if not isinstance(text, str):
        return None
    match = _UNIT_TEXT.fullmatch(" ".join(text.split()).casefold())
    if match is None:
        return None
    count = match.group("count")
    return (
        None if count is None else Decimal(count.replace(",", ".")),
        match.group("word"),
    )


def _read(text: object, aliases: Mapping[str, Quantity | None]) -> Quantity | None:
    parts = _split(text)
    if parts is None:
        return None
    count, word = parts
    base = aliases[word] if word in aliases else _VOCABULARY.get(word)
    if base is None:
        return None
    if count is None:
        return base
    if count <= 0:
        return None
    return Quantity(amount=_CONTEXT.multiply(base.amount, count), unit=base.unit)


def _quantise(value: Decimal) -> Decimal:
    """round half up to four places, then drop zeros down to two places."""

    rounded = value.quantize(_PLACES[-1], context=_CONTEXT)
    for places in _PLACES[:-1]:
        shorter = rounded.quantize(places, context=_CONTEXT)
        if shorter == rounded:
            return shorter
    return rounded


class UnitReader:
    """one store's unit vocabulary: the shared words plus the store's codes.

    ``aliases`` maps each code the store writes to shared unit text, such as
    ``{"dc": "docena"}`` or ``{"per_100ml": "100 ml"}``, or to ``None`` for a
    code that must read as unknown. codes are matched like any unit text,
    without regard to case or trailing dots. an alias whose target the
    shared vocabulary cannot read raises ``ValueError`` when the reader is
    built, so a typo fails at import rather than as a silent ``None``.
    """

    __slots__ = ("_aliases",)

    def __init__(self, aliases: Mapping[str, str | None] | None = None) -> None:
        resolved: dict[str, Quantity | None] = {}
        for code, meaning in (aliases or {}).items():
            parts = _split(code)
            if parts is None or parts[0] is not None:
                raise ValueError(f"unit alias {code!r} is not a bare unit code")
            if meaning is None:
                resolved[parts[1]] = None
                continue
            target = _read(meaning, {})
            if target is None:
                raise ValueError(
                    f"unit alias {code!r} means {meaning!r}, "
                    "which the shared vocabulary cannot read"
                )
            resolved[parts[1]] = target
        self._aliases: Mapping[str, Quantity | None] = MappingProxyType(resolved)

    def quantity(self, text: object) -> Quantity | None:
        """return what ``text`` stands for, or ``None`` when it is unknown.

        ``"100 Gr"`` is a tenth of a kilogram and ``"1 Dc"`` twelve pieces.
        """

        return _read(text, self._aliases)

    def unit_price(
        self, amount: Decimal | None, unit_text: object, *, per: Decimal | None = _ONE
    ) -> UnitPrice | None:
        """restate ``amount`` per one normalised unit, or return ``None``.

        ``amount`` is what ``per`` times ``unit_text`` costs; ``per`` is one
        unless the store publishes a structured quantity, such as carrefour's
        ``unit_conversion_factor``. the result is rounded half up to four
        decimal places and then trimmed to no fewer than two, so ``6.2`` per
        100 g reads ``62.00`` per kilogram and ``2.625`` per dozen reads
        ``0.2188`` per piece. a missing, negative or non-finite amount, a
        quantity that is not positive, and unknown text all give ``None``.
        """

        if amount is None or per is None:
            return None
        if not amount.is_finite() or amount < 0 or not per.is_finite() or per <= 0:
            return None
        quantity = self.quantity(unit_text)
        if quantity is None:
            return None
        divisor = _CONTEXT.multiply(quantity.amount, per)
        return UnitPrice(
            amount=_quantise(_CONTEXT.divide(amount, divisor)), unit=quantity.unit
        )

    def unit_price_from_text(self, text: object) -> UnitPrice | None:
        """read display text holding exactly one price per unit.

        ``"1,55 €/kg"`` and ``"0,08 €/m."`` are read; text with a second
        price, a prefix or a suffix, such as ``"1 ud 6,77 €/kg / 2 uds 6,54
        €/kg"``, gives ``None``.
        """

        if not isinstance(text, str):
            return None
        match = _PRICE_PER_UNIT.fullmatch(text)
        if match is None:
            return None
        return self.unit_price(as_euro_text(match.group("amount")), match.group("unit"))


SHARED_UNITS = UnitReader()
"""the reader for a store whose unit text uses the shared vocabulary only."""
