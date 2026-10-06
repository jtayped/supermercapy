"""match products across stores, by two different questions.

only four stores publish barcodes, so "is this the same product at another
store?" mostly has to be answered from the name, the brand and the pack size.
this module answers two questions, and keeps them apart because they want
opposite things from a store brand:

- **the same product**: the same brand, the same product, the same pack, at
  another store. :func:`score_same` scores one pair; :func:`find_same`,
  :func:`pair_same` and :func:`group_same` apply it to lists. a hacendado milk
  and a consum milk are never the same product.
- **a comparable alternative**: the same kind of product under any brand,
  compared by ``price.reference``. :func:`score_alternative` scores one pair
  and :func:`find_alternatives` ranks candidates cheapest first. here the
  hacendado milk is exactly what a shopper wants to see.

every :class:`Match` carries a score in ``[0, 1]`` and the reasons behind it,
such as ``("brand", "size", "name 0.82")``, so a caller can set its own
threshold and show why. nothing here performs i/o: it reads the summaries it
is given.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from ._core.exceptions import ConfigurationError
from ._core.models import ProductSummary
from ._match_text import (
    Features,
    Phrase,
    features,
    find,
    likeness,
    remove,
    same_size,
    tokens,
)

__all__ = [
    "ALTERNATIVE_THRESHOLD",
    "SAME_THRESHOLD",
    "Listing",
    "Match",
    "MatchKind",
    "ProductGroup",
    "find_alternatives",
    "find_same",
    "group_same",
    "pair_same",
    "score_alternative",
    "score_same",
]

SAME_THRESHOLD = 0.8
"""the default score from which two listings count as the same product."""

ALTERNATIVE_THRESHOLD = 0.6
"""the default score from which a listing counts as a comparable alternative."""

# a pair whose brand, pack size, barcode or container contradicts keeps only
# this share of its score, so the reasons still show how close the name was
_CONTRADICTED = 0.5


class MatchKind(StrEnum):
    """which question a :class:`Match` answers."""

    SAME = "same"
    """the same brand, product and pack size, at another store."""
    ALTERNATIVE = "alternative"
    """the same kind of product under any brand, compared per unit."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Listing:
    """one product at one store, as ``search_all().products`` pairs them.

    ``store`` is ``None`` when the caller passed a bare summary.
    """

    store: str | None = None
    product: ProductSummary


@dataclass(frozen=True, slots=True, kw_only=True)
class Match:
    """one listing judged against another.

    ``score`` is in ``[0, 1]``, rounded to two places. ``reasons`` lists
    every signal that counted, agreeing or not: ``"ean"``, ``"brand"``,
    ``"size"``, ``"name 0.82"``, ``"brand differs"``, ``"size unknown"``, and
    for an alternative ``"kind 0.90"``, ``"unit l"`` or ``"store brand"``.
    """

    kind: MatchKind
    score: float
    reasons: tuple[str, ...]
    left: Listing
    right: Listing


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductGroup:
    """listings :func:`group_same` judged to be one product, one per store.

    a group is as sure as its weakest pair: ``score`` and ``reasons`` are that
    pair's, among the pairs that reached the threshold. a listing that matched
    nothing is a group of one, with ``score`` ``None`` and no reasons.
    """

    listings: tuple[Listing, ...]
    score: float | None = None
    reasons: tuple[str, ...] = ()


ListingLike: TypeAlias = Listing | ProductSummary | tuple[str, ProductSummary]
"""a :class:`Listing`, a bare summary, or a ``(store, summary)`` pair."""


# --------------------------------------------------------------- one pair


def score_same(a: ListingLike, b: ListingLike) -> Match:
    """score whether ``a`` and ``b`` are the same product.

    equal barcodes settle it at ``1.0``. otherwise the score is half the name
    likeness, plus a quarter when the brands agree and a quarter when the pack
    sizes agree, so a pair whose brand or size cannot be confirmed never
    reaches :data:`SAME_THRESHOLD`. a brand, a pack size, a barcode or a
    container (can, bottle, brick, jar, squeeze bottle) that contradicts
    halves the score.

    the names are compared without brand, pack size, packaging and stop
    words, in spanish and catalan alike, and only their differences count: a
    descriptor such as ``"refresco"`` or ``"soluble"`` costs little, any
    other word costs enough to fall below the threshold on its own, and a
    number or a negation (``"0%"``, ``"sin lactosa"``) costs most. the name
    likeness is ``1 / (1 + cost)``.
    """

    left, right = _listing(a), _listing(b)
    return _same(left, _features(left), right, _features(right))


def score_alternative(a: ListingLike, b: ListingLike) -> Match:
    """score whether ``b`` is a comparable alternative to ``a``.

    the score is the likeness of what the two names say the product is, with
    both brands taken out, so a store brand competes on equal terms; size does
    not count, because alternatives are compared per unit. it is ``1.0`` for
    the same description, stays above :data:`ALTERNATIVE_THRESHOLD` while the
    names differ only by descriptors, and falls below it for one other word.
    it is halved when both carry a ``price.reference`` in different units,
    since those prices cannot be compared, and it is ``0`` when either name
    says nothing once its brand is gone.
    """

    left, right = _listing(a), _listing(b)
    return _alternative(left, _features(left), right, _features(right))


# ------------------------------------------------------------------- lists


def find_same(
    product: ListingLike,
    candidates: Iterable[ListingLike],
    *,
    threshold: float = SAME_THRESHOLD,
) -> tuple[Match, ...]:
    """return the candidates that are the same product as ``product``.

    candidates from ``product``'s own store are skipped, and each other store
    keeps only its best candidate, so the answer reads "this product at those
    stores". matches come best first.
    """

    limit = _threshold(threshold)
    target = _listing(product)
    target_features = _features(target)
    best: dict[str | None, tuple[Match, int]] = {}
    for index, candidate in enumerate(_listings(candidates)):
        if _same_listing(target, candidate) or _same_store(target, candidate):
            continue
        match = _same(target, target_features, candidate, _features(candidate))
        if match.score < limit:
            continue
        key = candidate.store if candidate.store is not None else f"#{index}"
        if key not in best or match.score > best[key][0].score:
            best[key] = (match, index)
    ranked = sorted(best.values(), key=lambda item: (-item[0].score, item[1]))
    return tuple(match for match, _ in ranked)


def find_alternatives(
    product: ListingLike,
    candidates: Iterable[ListingLike],
    *,
    threshold: float = ALTERNATIVE_THRESHOLD,
) -> tuple[Match, ...]:
    """return the candidates that are comparable alternatives to ``product``.

    any store counts, ``product``'s own included, and so does any brand. the
    answer comes cheapest first by ``price.reference``; candidates without
    one come last, best score first.
    """

    limit = _threshold(threshold)
    target = _listing(product)
    target_features = _features(target)
    found: list[tuple[Match, int]] = []
    for index, candidate in enumerate(_listings(candidates)):
        if _same_listing(target, candidate):
            continue
        match = _alternative(target, target_features, candidate, _features(candidate))
        if match.score >= limit:
            found.append((match, index))

    def cheapest(item: tuple[Match, int]) -> tuple[bool, str, float, float, int]:
        match, index = item
        reference = match.right.product.price.reference
        if reference is None:
            return True, "", 0.0, -match.score, index
        return False, reference.unit.value, float(reference.amount), -match.score, index

    return tuple(match for match, _ in sorted(found, key=cheapest))


def pair_same(
    left: Iterable[ListingLike],
    right: Iterable[ListingLike],
    *,
    threshold: float = SAME_THRESHOLD,
) -> tuple[Match, ...]:
    """pair each listing of ``left`` with at most one of ``right``.

    the best-scoring pairs are taken first and no listing is used twice, so
    two arbitrary lists, such as a basket at one store and a search page at
    another, come back as one-to-one matches in ``left``'s order. listings
    that found no partner at ``threshold`` are left out.
    """

    limit = _threshold(threshold)
    lefts = [(listing, _features(listing)) for listing in _listings(left)]
    rights = [(listing, _features(listing)) for listing in _listings(right)]
    scored = [
        (match, i, j)
        for i, (a, a_features) in enumerate(lefts)
        for j, (b, b_features) in enumerate(rights)
        if (match := _same(a, a_features, b, b_features)).score >= limit
    ]
    scored.sort(key=lambda item: (-item[0].score, item[1], item[2]))
    taken_left: dict[int, Match] = {}
    taken_right: set[int] = set()
    for match, i, j in scored:
        if i not in taken_left and j not in taken_right:
            taken_left[i] = match
            taken_right.add(j)
    return tuple(taken_left[i] for i in sorted(taken_left))


def group_same(
    listings: Iterable[ListingLike],
    *,
    threshold: float = SAME_THRESHOLD,
) -> tuple[ProductGroup, ...]:
    """split listings into groups of the same product, at most one per store.

    this is the shape ``search_all(...).products`` comes in. every listing
    lands in exactly one group, and a listing that matched nothing is a group
    of its own, and listings without a store are never kept apart by
    store. two groups join only when every pair across them scores at
    least ``threshold``, so one strong pair never drags in a listing that
    matches only half the group; listings that share a barcode stand in for
    each other there, so a product a store renamed still joins through its
    twin. groups keep the order of their first listing, and listings keep
    their input order inside a group.
    """

    limit = _threshold(threshold)
    items = [(listing, _features(listing)) for listing in _listings(listings)]
    scores: dict[tuple[int, int], Match] = {}
    for i, (a, a_features) in enumerate(items):
        for j in range(i + 1, len(items)):
            b, b_features = items[j]
            if _same_listing(a, b) or _same_store(a, b):
                continue
            match = _same(a, a_features, b, b_features)
            if match.score >= limit:
                scores[i, j] = match

    def linked(x: int, y: int) -> bool:
        return (min(x, y), max(x, y)) in scores

    def twins(k: int, members: list[int]) -> list[int]:
        """``k`` and the listings among ``members`` that share its barcode."""

        ean = items[k][1].ean
        if ean is None:
            return [k]
        return [other for other in members if items[other][1].ean == ean]

    groups: dict[int, list[int]] = {index: [index] for index in range(len(items))}
    owner = list(range(len(items)))
    for i, j in sorted(scores, key=lambda pair: (-scores[pair].score, pair)):
        first, second = groups[owner[i]], groups[owner[j]]
        if first is second:
            continue
        stores = [
            items[k][0].store for k in first + second if items[k][0].store is not None
        ]
        if len(stores) != len(set(stores)):
            continue
        union = first + second
        if not all(
            any(linked(x2, y2) for x2 in twins(x, union) for y2 in twins(y, union))
            for x in first
            for y in second
        ):
            continue
        keep, drop = sorted((owner[i], owner[j]))
        groups[keep] = sorted(first + second)
        for k in groups.pop(drop):
            owner[k] = keep
    result = []
    for members in groups.values():
        weakest = min(
            (scores[x, y] for x in members for y in members if (x, y) in scores),
            key=lambda match: match.score,
            default=None,
        )
        result.append(
            ProductGroup(
                listings=tuple(items[k][0] for k in members),
                score=None if weakest is None else weakest.score,
                reasons=() if weakest is None else weakest.reasons,
            )
        )
    return tuple(result)


# ---------------------------------------------------------------- scoring


@dataclass(frozen=True, slots=True)
class _Brands:
    """how two brands compare, and which brand words each name loses.

    ``strip_left`` and ``strip_right`` are phrases taken out of each name
    where they appear whole. ``shared`` is the brand both agree on, whose
    words also go one by one, so consum's ``"Refresco Cola"`` under the
    brand ``COCA-COLA`` loses its ``"cola"``.
    """

    verdict: bool | None
    strip_left: tuple[Phrase, ...] = ()
    strip_right: tuple[Phrase, ...] = ()
    shared: Phrase = ()


def _brands(a: Features, b: Features) -> _Brands:
    if a.brand and b.brand:
        return _both_brands(a, b)
    if not a.brand and not b.brand:
        return _Brands(None)
    branded, other = (a, b) if a.brand else (b, a)
    strip = (branded.brand,)
    if find(other.words, branded.brand) is not None:
        return _Brands(True, strip, strip, branded.brand)
    # a store brand is never on another chain's shelf
    if branded.owners and other.store is not None and other.store not in branded.owners:
        return _Brands(False, strip, strip)
    return _Brands(None, strip, strip)


def _both_brands(a: Features, b: Features) -> _Brands:
    x, y = a.brand, b.brand
    if "".join(x) == "".join(y):
        return _Brands(True, (x,), (y,), x)
    short, long = (x, y) if len(x) <= len(y) else (y, x)
    if long[: len(short)] == short:
        # "coca cola zero" extends "coca cola": the extra words name a
        # variant, so they stay in the name to be compared
        strip_left: tuple[Phrase, ...] = (short,)
        strip_right: tuple[Phrase, ...] = (short,)
    elif long[-len(short) :] == short:
        # "central lechera asturiana" ends in "asturiana": the extra words
        # name the maker, so they go
        strip_left, strip_right = (x, short), (y, short)
    else:
        return _Brands(False, (x,), (y,))
    # "carrefour bio" is not "bio": a store brand only extends itself
    if a.owners != b.owners:
        return _Brands(False, strip_left, strip_right)
    return _Brands(True, strip_left, strip_right, short)


def _same(left: Listing, a: Features, right: Listing, b: Features) -> Match:
    if a.ean is not None and a.ean == b.ean:
        return Match(
            kind=MatchKind.SAME, score=1.0, reasons=("ean",), left=left, right=right
        )
    brands = _brands(a, b)
    name = likeness(
        tokens(remove(a.words, brands.strip_left), brands.shared),
        tokens(remove(b.words, brands.strip_right), brands.shared),
    )
    size = same_size(a.size, b.size)
    score = name / 2 + (brands.verdict is True) / 4 + (size is True) / 4
    reasons: list[str] = []
    contradicted = brands.verdict is False or size is False
    if a.ean is not None and b.ean is not None:
        reasons.append("ean differs")
        contradicted = True
    reasons.append(_verdict("brand", brands.verdict))
    reasons.append(_verdict("size", size))
    reasons.append(f"name {name:.2f}")
    if a.containers and b.containers and a.containers.isdisjoint(b.containers):
        reasons.append("container differs")
        contradicted = True
    if contradicted:
        score *= _CONTRADICTED
    return Match(
        kind=MatchKind.SAME,
        score=round(score, 2),
        reasons=tuple(reasons),
        left=left,
        right=right,
    )


def _alternative(left: Listing, a: Features, right: Listing, b: Features) -> Match:
    brands = tuple(brand for brand in (a.brand, b.brand) if brand)
    a_tokens = tokens(remove(a.words, brands))
    b_tokens = tokens(remove(b.words, brands))
    reasons: list[str] = []
    if a_tokens and b_tokens:
        kind = likeness(a_tokens, b_tokens)
        reasons.append(f"kind {kind:.2f}")
    else:
        kind = 0.0
        reasons.append("kind unknown")
    if a.reference is not None and b.reference is not None:
        if a.reference.unit is b.reference.unit:
            reasons.append(f"unit {a.reference.unit.value}")
        else:
            reasons.append("unit differs")
            kind *= _CONTRADICTED
    if b.owners:
        reasons.append("store brand")
    return Match(
        kind=MatchKind.ALTERNATIVE,
        score=round(kind, 2),
        reasons=tuple(reasons),
        left=left,
        right=right,
    )


# ---------------------------------------------------------------- helpers


def _verdict(signal: str, verdict: bool | None) -> str:
    if verdict is None:
        return f"{signal} unknown"
    return signal if verdict else f"{signal} differs"


def _listing(item: object) -> Listing:
    if isinstance(item, Listing):
        return item
    if isinstance(item, ProductSummary):
        return Listing(product=item)
    if (
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], ProductSummary)
    ):
        return Listing(store=item[0], product=item[1])
    raise TypeError(
        "expected a Listing, a ProductSummary, or a (store, ProductSummary) "
        f"pair, not {type(item).__name__}"
    )


def _listings(items: Iterable[ListingLike]) -> list[Listing]:
    if isinstance(items, (str, bytes, ProductSummary, Listing)):
        raise TypeError("expected a collection of listings")
    return [_listing(item) for item in items]


def _features(listing: Listing) -> Features:
    return features(listing.store, listing.product)


def _same_store(a: Listing, b: Listing) -> bool:
    return a.store is not None and a.store == b.store


def _same_listing(a: Listing, b: Listing) -> bool:
    # ids are only unique within a store: consum and eroski share some
    return a.product is b.product or (
        a.store is not None and a.store == b.store and a.product.id == b.product.id
    )


def _threshold(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ConfigurationError("threshold must be a number from 0 to 1")
    return float(value)
