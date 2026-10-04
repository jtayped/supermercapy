"""compare a live json response against the committed fixture it stands for.

the model coercion turns a missing key into ``None`` or an empty tuple, so an
upstream rename never fails the offline suite: the fixture still has the old
key. this module reads the structure out of both documents and reports every
fixture path the live document no longer carries, or carries as another type.
new upstream keys are not drift, and neither is a ``null`` on either side.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

Shape = dict[str, set[str]]


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"not a json value: {type(value).__name__}")


def shape(document: Any, path: str = "$") -> Shape:
    """map every path in ``document`` to the json kinds seen there.

    array elements share one path, ``items[]``, so a list of products yields
    the union of every product's keys rather than one path per index.
    """

    found: Shape = {path: {_kind(document)}}
    children: Iterable[tuple[str, Any]] = ()
    if isinstance(document, dict):
        children = ((f"{path}.{key}", value) for key, value in document.items())
    elif isinstance(document, list):
        children = ((f"{path}[]", item) for item in document)
    for child_path, child in children:
        for key, kinds in shape(child, child_path).items():
            found.setdefault(key, set()).update(kinds)
    return found


def _covered_by_empty_parent(path: str, live: Shape) -> bool:
    """return whether an ancestor of ``path`` is live but empty or null."""

    parent = path
    while True:
        cut = max(parent.rfind("."), parent.rfind("[]"))
        if cut <= 0:
            return False
        parent = parent[:cut]
        kinds = live.get(parent)
        if kinds is None:
            continue
        if "null" in kinds:
            return True
        # an array path with no element paths below it was always empty live
        return "array" in kinds and not any(
            key.startswith(f"{parent}[]") for key in live
        )


def _below(path: str, ancestor: str) -> bool:
    return path == ancestor or path.startswith((f"{ancestor}.", f"{ancestor}[]"))


def drift(live: Any, fixture: Any, *, ignore: Iterable[str] = ()) -> list[str]:
    """return one line per fixture path the live document dropped or retyped.

    ``ignore`` names paths whose presence depends on the product rather than on
    the api, such as an optional promotion block; everything below an ignored
    path is ignored with it. a missing object is reported once, not once per
    key inside it.
    """

    ignored = tuple(ignore)
    live_shape = shape(live)
    missing: list[str] = []
    problems: list[str] = []
    for path, kinds in sorted(shape(fixture).items()):
        if any(_below(path, item) for item in (*ignored, *missing)):
            continue
        live_kinds = live_shape.get(path)
        if live_kinds is None:
            if not _covered_by_empty_parent(path, live_shape):
                missing.append(path)
                problems.append(f"missing {path} (fixture: {'/'.join(sorted(kinds))})")
            continue
        if "null" in kinds or "null" in live_kinds:
            continue
        if not kinds & live_kinds:
            problems.append(
                f"retyped {path}: fixture {'/'.join(sorted(kinds))}, "
                f"live {'/'.join(sorted(live_kinds))}"
            )
    return problems


def assert_no_drift(
    live: Any, fixture: Any, *, label: str, ignore: Iterable[str] = ()
) -> None:
    """fail with every drifted path when ``live`` no longer matches ``fixture``."""

    problems = drift(live, fixture, ignore=ignore)
    assert not problems, f"{label} drifted from its fixture:\n" + "\n".join(problems)
