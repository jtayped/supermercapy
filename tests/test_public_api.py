"""the public api surface, pinned so a breaking change is always deliberate.

every name a package ``__all__`` exports is described: classes by their public
methods, properties, class attributes and dataclass fields, enums by their
members, functions by their signatures. the description is compared with the
committed snapshot in ``tests/fixtures/public_api.json``.

a removed or changed entry breaks callers and needs a major version (or, while
the version is ``0.x``, a minor one and a changelog line). an added entry is
not breaking but is still pinned, so it is reviewed. after an intended change,
rewrite the snapshot with ``python -m tests.test_public_api`` and commit it.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import re
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

import supermercapy

SNAPSHOT = Path(__file__).parent / "fixtures" / "public_api.json"
_DATACLASS_METHODS = frozenset({"__init__", "__eq__", "__repr__", "__hash__"})
# values that move with releases and browsers rather than with the api
_UNPINNED_VALUES = frozenset({"default_user_agent"})
_ADDRESS = re.compile(r" at 0x[0-9a-f]+>")


def _packages() -> list[ModuleType]:
    names = {"supermercapy"}
    for client_type in supermercapy.ALL_CLIENTS:
        names.add(client_type.__module__.rsplit(".", 1)[0])
    return [importlib.import_module(name) for name in sorted(names)]


def _qualified(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def _value(value: Any) -> str:
    if isinstance(value, (frozenset, set)):
        return "{" + ", ".join(sorted(_value(item) for item in value)) + "}"
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, type):
        return _qualified(value)
    if isinstance(value, tuple):
        return "(" + ", ".join(_value(item) for item in value) + ")"
    return repr(value)


def _signature(function: Any) -> str:
    try:
        signature = str(inspect.signature(function))
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return "(...)"
    # a default such as a function reprs with its address, which moves per run
    return _ADDRESS.sub(">", signature)


def _is_private(cls: type) -> bool:
    """return whether no package exports ``cls`` under a public module path."""

    module = cls.__module__
    return module.startswith("supermercapy.") and any(
        part.startswith("_") and part != "_core" for part in module.split(".")[1:]
    )


def _own_members(cls: type) -> dict[str, Any]:
    """return what ``cls`` defines, plus what it inherits from private bases."""

    members: dict[str, Any] = {}
    for base in cls.__mro__:
        if base is not cls and not _is_private(base):
            break
        for name, value in base.__dict__.items():
            members.setdefault(name, value)
    return members


def _describe_class(cls: type, surface: dict[str, str]) -> None:
    path = _qualified(cls)
    if issubclass(cls, Enum):
        members = ", ".join(f"{item.name}={item.value!r}" for item in cls)
        surface[path] = f"enum {cls.__mro__[1].__name__}: {members}"
        return
    bases = ", ".join(_qualified(base) for base in cls.__bases__)
    surface[path] = f"class({bases})"
    fields = {}
    if dataclasses.is_dataclass(cls):
        for field in dataclasses.fields(cls):
            if field.default is not dataclasses.MISSING:
                default = _value(field.default)
            elif field.default_factory is not dataclasses.MISSING:
                default = f"{field.default_factory.__name__}()"
            else:
                default = "required"
            kind = "field" if field.init else "field, not in __init__"
            fields[field.name] = f"{kind}: {field.type} = {default}"
    for name, description in fields.items():
        surface[f"{path}.{name}"] = description
    # inherited members are described once, on the public class that defines
    # them; the bases above say where a subclass inherits from. a private base,
    # such as a platform two stores share, is described on each public class
    # that inherits from it, because callers reach its members only there
    owned = _own_members(cls)
    for name in sorted(owned):
        if name in fields or (name.startswith("_") and name != "__init__"):
            continue
        if name in _DATACLASS_METHODS and dataclasses.is_dataclass(cls):
            continue
        static = owned[name]
        member = f"{path}.{name}"
        if isinstance(static, property):
            returns = inspect.signature(static.fget).return_annotation  # type: ignore[arg-type]
            surface[member] = f"property -> {returns}"
        elif isinstance(static, classmethod):
            # bound to the class, so the signature leaves out ``cls``
            surface[member] = f"classmethod{_signature(getattr(cls, name))}"
        elif isinstance(static, staticmethod):
            surface[member] = f"staticmethod{_signature(static.__func__)}"
        elif inspect.isfunction(static):
            surface[member] = f"method{_signature(static)}"
        elif name in _UNPINNED_VALUES:
            surface[member] = f"attribute: {type(static).__name__}"
        else:
            surface[member] = f"attribute = {_value(static)}"


def build_surface() -> dict[str, str]:
    """describe everything the packages export, keyed on a dotted path."""

    surface: dict[str, str] = {}
    described: set[str] = set()
    for package in _packages():
        for name in sorted(package.__all__):
            obj = getattr(package, name)
            export = f"export {package.__name__}.{name}"
            if name == "__version__":
                continue
            if isinstance(obj, type) or inspect.isfunction(obj):
                surface[export] = _qualified(obj)
                if _qualified(obj) in described:
                    continue
                described.add(_qualified(obj))
                if isinstance(obj, type):
                    _describe_class(obj, surface)
                else:
                    surface[_qualified(obj)] = f"function{_signature(obj)}"
            else:
                surface[export] = _value(obj)
    return dict(sorted(surface.items()))


def test_public_api_matches_the_snapshot() -> None:
    assert SNAPSHOT.exists(), (
        f"{SNAPSHOT} is missing; run python -m tests.test_public_api to create it"
    )
    expected: dict[str, str] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = build_surface()
    removed = sorted(expected.keys() - actual.keys())
    changed = sorted(
        key for key in expected.keys() & actual.keys() if expected[key] != actual[key]
    )
    added = sorted(actual.keys() - expected.keys())
    lines = [f"removed  {key}: {expected[key]}" for key in removed]
    lines += [
        f"changed  {key}\n  was: {expected[key]}\n  now: {actual[key]}"
        for key in changed
    ]
    lines += [f"added    {key}: {actual[key]}" for key in added]
    assert not lines, (
        "the public api differs from tests/fixtures/public_api.json. removed and "
        "changed entries break callers. if this is intended, run "
        "python -m tests.test_public_api and commit the snapshot.\n" + "\n".join(lines)
    )


def main() -> int:
    """rewrite the snapshot from the code as it stands."""

    surface = build_surface()
    SNAPSHOT.write_text(
        json.dumps(surface, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(surface)} entries to {SNAPSHOT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
