"""the ``supermercapy`` command: read the storefronts from a shell.

``main()`` runs one command and returns its exit code: 0 when every store asked
answered, 1 when a store failed (whatever the others found is still printed),
and 2 for a usage error. tables go to stdout and per-store notes to stderr.
``--json`` replaces the table with one json document written by ``to_json``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import textwrap
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from typing import Any, TypeVar

from . import ALL_CLIENTS
from ._core.capabilities import Capability
from ._core.client import BaseClient, validate_postal_code
from ._core.exceptions import (
    ConfigurationError,
    NotFoundError,
    OutOfCoverageError,
    SupermercapyError,
)
from ._core.models import Category, Price, Product, ProductSummary
from ._core.serialize import to_json
from ._multi import (
    Outcome,
    SearchAllResult,
    SearchStatus,
    binding_parameter,
    client_options,
    resolve_store,
    run_each,
    search_all,
    skip_reason,
)
from ._version import __version__
from .match import SAME_THRESHOLD, Listing, ProductGroup, group_same

T = TypeVar("T")

PROG = "supermercapy"
DEFAULT_LIMIT = 5
COMPARE_LIMIT = 10
GAP = "  "
MIN_FLEX_WIDTH = 16
EXPECTED = (NotFoundError, OutOfCoverageError)


def main(argv: Sequence[str] | None = None) -> int:
    """run the command line and return its exit code."""

    parser = _parser()
    try:
        arguments = parser.parse_args(argv)
        try:
            return int(arguments.handler(arguments))
        except ConfigurationError as error:
            command: argparse.ArgumentParser = arguments.command_parser
            command.error(str(error))
    except SystemExit as exit_:
        return int(exit_.code or 0)
    except KeyboardInterrupt:
        return 130


def _client_keywords() -> dict[str, Any]:
    """return the keyword options every client a command builds receives.

    the command passes none, so each store keeps its own defaults. the test
    suite replaces this function to hand the clients a canned transport.
    """

    return {}


# ------------------------------------------------------------------ parsing


def _parser() -> argparse.ArgumentParser:
    # python 3.14 colours help by default; this command prints no colour
    plain: dict[str, Any] = {"color": False} if sys.version_info >= (3, 14) else {}
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="read the product data of spanish supermarket storefronts.",
        **plain,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(title="commands", metavar="COMMAND", required=True)

    stores = _command(
        commands,
        "stores",
        _stores,
        "list every store, what it binds to, and what it can answer",
        plain,
    )
    _json_flag(stores)

    search = _command(
        commands, "search", _search, "search every store, or the ones named", plain
    )
    _search_flags(search, DEFAULT_LIMIT)
    _json_flag(search)

    compare = _command(
        commands,
        "compare",
        _compare,
        "search every store and line up the same product across them",
        plain,
    )
    _search_flags(compare, COMPARE_LIMIT)
    compare.add_argument(
        "--threshold",
        type=_score,
        default=SAME_THRESHOLD,
        metavar="SCORE",
        help=(
            "lowest score from 0 to 1 that counts as the same product "
            f"(default: {SAME_THRESHOLD})"
        ),
    )
    _json_flag(compare)

    product = _command(commands, "product", _product, "show one product", plain)
    product.add_argument("store", type=_store, metavar="STORE", help="the store")
    product.add_argument("id", metavar="ID", help="the store's product id")
    _postal_code_flag(product)
    _json_flag(product)

    categories = _command(
        commands, "categories", _categories, "show a store's category tree", plain
    )
    categories.add_argument("store", type=_store, metavar="STORE", help="the store")
    _postal_code_flag(categories)
    _json_flag(categories)

    ean = _command(
        commands,
        "ean",
        _ean,
        "look a barcode up in every store that can find one",
        plain,
    )
    ean.add_argument("ean", type=_ean_code, metavar="EAN", help="8 to 14 digits")
    _postal_code_flag(ean)
    _json_flag(ean)
    return parser


def _command(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    handler: Callable[[argparse.Namespace], int],
    summary: str,
    plain: dict[str, Any],
) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = commands.add_parser(
        name, help=summary, description=f"{summary}.", **plain
    )
    parser.set_defaults(handler=handler, command_parser=parser)
    return parser


def _search_flags(parser: argparse.ArgumentParser, limit: int) -> None:
    parser.add_argument("query", help="what to search for")
    parser.add_argument(
        "--store",
        dest="stores",
        action="append",
        type=_store,
        metavar="STORE",
        help="search only this store; repeat for more (default: every store)",
    )
    _postal_code_flag(parser)
    parser.add_argument(
        "--limit",
        type=_positive,
        default=limit,
        metavar="N",
        help=f"products per store (default: {limit})",
    )


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="print one json document, not a table"
    )


def _postal_code_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--postal-code",
        type=_postal_code,
        metavar="CODE",
        help="bind the stores that can resolve a five-digit postcode",
    )


def _store(value: str) -> type[BaseClient]:
    try:
        return resolve_store(value)
    except ConfigurationError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _postal_code(value: str) -> str:
    try:
        return validate_postal_code(value)
    except ConfigurationError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _positive(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, not {value!r}")
    return int(value)


def _score(value: str) -> float:
    try:
        score = float(value)
    except ValueError:
        score = -1.0
    if not 0 <= score <= 1:
        raise argparse.ArgumentTypeError(f"expected a score from 0 to 1, not {value!r}")
    return score


def _ean_code(value: str) -> str:
    if not value.isdigit() or not 8 <= len(value) <= 14:
        raise argparse.ArgumentTypeError(f"an ean has 8 to 14 digits, not {value!r}")
    return value


# ----------------------------------------------------------------- commands


def _stores(arguments: argparse.Namespace) -> int:
    if arguments.json:
        _out(to_json([_describe(client_type) for client_type in ALL_CLIENTS], indent=2))
        return 0
    rows = [
        (
            client_type.store_name,
            client_type.__name__,
            _binds_to(client_type),
            ", ".join(name.lower() for name in _capabilities(client_type)),
        )
        for client_type in ALL_CLIENTS
    ]
    _out_lines(_wrapped(("store", "class", "binds to", "capabilities"), rows))
    return 0


def _search(arguments: argparse.Namespace) -> int:
    result = _search_all(arguments)
    if arguments.json:
        _out(to_json(result, indent=2))
    else:
        _out_lines(_product_table(result.products))
    _search_notes(result)
    return 1 if result.failed else 0


def _compare(arguments: argparse.Namespace) -> int:
    result = _search_all(arguments)
    groups = group_same(result.products, threshold=arguments.threshold)
    shared = sorted(
        (group for group in groups if len(group.listings) > 1),
        key=lambda group: -len(group.listings),
    )
    unmatched = [group.listings[0] for group in groups if len(group.listings) == 1]
    if arguments.json:
        document = {
            "query": result.query,
            "postal_code": result.postal_code,
            "threshold": arguments.threshold,
            "groups": shared,
            "unmatched": unmatched,
            "stores": [
                {
                    "store": entry.store,
                    "status": entry.status,
                    "store_id": entry.store_id,
                    "reason": entry.reason,
                    "error_type": entry.error_type,
                }
                for entry in result.stores
            ],
        }
        _out(to_json(document, indent=2))
    else:
        _out_lines(_group_table(shared))
    _search_notes(result)
    if unmatched:
        total = len(result.products)
        _note(f"{len(unmatched)} of {total} products matched no other store", None)
    return 1 if result.failed else 0


def _search_all(arguments: argparse.Namespace) -> SearchAllResult:
    if arguments.stores:
        _require_bindable(arguments.stores, arguments.postal_code)
    return search_all(
        arguments.query,
        stores=arguments.stores,
        postal_code=arguments.postal_code,
        page_size=arguments.limit,
        **_client_keywords(),
    )


def _search_notes(result: SearchAllResult) -> None:
    for entry in result.stores:
        if entry.status is SearchStatus.OK:
            if entry.result is not None and not entry.result.products:
                _note(entry.store, "no results")
        elif entry.status is SearchStatus.SKIPPED:
            _note(entry.store, "skipped", entry.reason)
        elif entry.status is SearchStatus.OUT_OF_COVERAGE:
            _note(entry.store, "out of coverage", entry.reason)
        else:
            _note(entry.store, entry.error_type, entry.reason)


def _product(arguments: argparse.Namespace) -> int:
    outcome = _ask(arguments, lambda client: client.get_product(arguments.id))
    if outcome.value is None:
        return 1
    if arguments.json:
        _out(to_json(outcome.value, indent=2))
    else:
        _out_lines(_product_sheet(outcome.value, outcome))
    return 0


def _categories(arguments: argparse.Namespace) -> int:
    outcome = _ask(arguments, lambda client: client.get_categories())
    if outcome.value is None:
        return 1
    if arguments.json:
        _out(to_json(outcome.value, indent=2))
    else:
        rows = [
            (category.id, "  " * depth + category.name)
            for category, depth in _walk(outcome.value)
        ]
        _out_lines(_table(("id", "name"), rows, flex=1))
    return 0


def _ean(arguments: argparse.Namespace) -> int:
    client_types = [
        client_type
        for client_type in ALL_CLIENTS
        if client_type.supports(Capability.EAN_LOOKUP)
    ]
    outcomes = run_each(
        client_types,
        lambda client: client.get_product_by_ean(arguments.ean),
        postal_code=arguments.postal_code,
        options=client_options(**_client_keywords()),
    )
    if arguments.json:
        document = {
            "ean": arguments.ean,
            "postal_code": arguments.postal_code,
            "stores": [_ean_entry(outcome) for outcome in outcomes],
        }
        _out(to_json(document, indent=2))
    else:
        found = [
            (outcome.store, outcome.value)
            for outcome in outcomes
            if outcome.value is not None
        ]
        _out_lines(_product_table(found))
    for outcome in outcomes:
        if outcome.skipped is not None:
            _note(outcome.store, "skipped", outcome.skipped)
        elif isinstance(outcome.error, NotFoundError):
            _note(outcome.store, "not found")
        elif outcome.error is not None:
            _note_error(outcome.store, outcome.error)
    failed = any(
        outcome.error is not None and not isinstance(outcome.error, EXPECTED)
        for outcome in outcomes
    )
    return 1 if failed else 0


# ------------------------------------------------------------------ helpers


def _ask(arguments: argparse.Namespace, call: Callable[[BaseClient], T]) -> Outcome[T]:
    """ask the one store a command names, reporting any failure on stderr."""

    client_type: type[BaseClient] = arguments.store
    _require_bindable([client_type], arguments.postal_code)
    (outcome,) = run_each(
        [client_type],
        call,
        postal_code=arguments.postal_code,
        options=client_options(**_client_keywords()),
    )
    if isinstance(outcome.error, ConfigurationError):
        raise outcome.error
    if outcome.error is not None:
        _note_error(outcome.store, outcome.error)
    return outcome


def _require_bindable(
    client_types: Sequence[type[BaseClient]], postal_code: str | None
) -> None:
    for client_type in client_types:
        reason = skip_reason(client_type, postal_code)
        if reason is not None:
            raise ConfigurationError(f"{client_type.store_name} {reason}")


def _capabilities(client_type: type[BaseClient]) -> list[str]:
    return [flag.name or str(flag) for flag in Capability if client_type.supports(flag)]


def _binds_to(client_type: type[BaseClient]) -> str:
    binding = binding_parameter(client_type)
    if binding is None:
        return "nothing"
    label = binding.name.replace("_", " ")
    if binding.default is binding.empty:
        return f"{label} (required)"
    if binding.default is None:
        return f"{label} (optional)"
    return f"{label} (default {binding.default})"


def _describe(client_type: type[BaseClient]) -> dict[str, Any]:
    binding = binding_parameter(client_type)
    return {
        "store": client_type.store_name,
        "class": client_type.__name__,
        "binding": None if binding is None else binding.name,
        "binding_required": binding is not None and binding.default is binding.empty,
        "capabilities": _capabilities(client_type),
    }


def _ean_entry(outcome: Outcome[Product]) -> dict[str, Any]:
    if outcome.skipped is not None:
        status = "skipped"
    elif outcome.error is None:
        status = "ok"
    elif isinstance(outcome.error, NotFoundError):
        status = "not_found"
    elif isinstance(outcome.error, OutOfCoverageError):
        status = "out_of_coverage"
    else:
        status = "failed"
    error = outcome.error
    return {
        "store": outcome.store,
        "status": status,
        "store_id": outcome.store_id,
        "product": outcome.value,
        "reason": outcome.skipped if error is None else str(error),
        "error_type": None if error is None else type(error).__name__,
    }


def _walk(
    categories: Sequence[Category], depth: int = 0
) -> Iterator[tuple[Category, int]]:
    for category in categories:
        yield category, depth
        yield from _walk(category.children, depth + 1)


def _product_table(products: Sequence[tuple[str, ProductSummary]]) -> list[str]:
    rows = [
        (
            store,
            product.id,
            product.name,
            _money(product.price.amount),
            _unit_price(product.price),
        )
        for store, product in products
    ]
    header = ("store", "id", "name", "price", "unit price")
    return _table(header, rows, flex=2, right=3) if rows else []


def _group_table(groups: Sequence[ProductGroup]) -> list[str]:
    rows = []
    for number, group in enumerate(groups, start=1):
        for index, listing in enumerate(sorted(group.listings, key=_cheapest)):
            first = index == 0
            product = listing.product
            rows.append(
                (
                    str(number) if first else "",
                    f"{group.score:.2f}" if first and group.score is not None else "",
                    listing.store or "",
                    product.id,
                    product.name,
                    _money(product.price.amount),
                    _unit_price(product.price),
                )
            )
    header = ("group", "score", "store", "id", "name", "price", "unit price")
    return _table(header, rows, flex=4, right=5) if rows else []


def _cheapest(listing: Listing) -> tuple[bool, Decimal]:
    amount = listing.product.price.amount
    return amount is None, amount if amount is not None else Decimal(0)


def _product_sheet(product: Product, outcome: Outcome[Product]) -> list[str]:
    binding = binding_parameter(outcome.client_type)
    store = outcome.store
    if binding is not None and outcome.store_id is not None:
        store = f"{store}, {binding.name.replace('_', ' ')} {outcome.store_id}"
    price = product.price
    amount = _money(price.amount)
    if amount and price.previous is not None:
        amount = f"{amount} (was {_money(price.previous)})"
    available = product.availability.available
    fields: list[tuple[str, str | None]] = [
        ("store", store),
        ("id", product.id),
        ("name", product.name),
        ("brand", product.brand),
        ("ean", product.ean),
        ("pack", product.pack_size_text),
        ("price", amount),
        ("unit price", _unit_price(price)),
        ("available", None if available is None else ("yes" if available else "no")),
        ("category", " > ".join(category.name for category in product.category_path)),
        *(("promotion", promotion.description) for promotion in product.promotions),
        ("url", product.url),
    ]
    shown = [(label, value) for label, value in fields if value]
    width = max(len(label) for label, _ in shown)
    return [f"{label.ljust(width)}{GAP}{value}" for label, value in shown]


def _money(amount: Decimal | None) -> str:
    # fixed-point keeps the figure as published without ever writing 1E+1
    return "" if amount is None else f"{amount:f}"


def _unit_price(price: Price) -> str:
    # the normalised reference compares across stores; what the store published
    # is the fallback when the reference could not be read
    if price.reference is not None:
        return f"{_money(price.reference.amount)}/{price.reference.unit.value}"
    if price.unit_price is None:
        return price.unit_price_text or ""
    amount = _money(price.unit_price)
    return f"{amount}/{price.unit_price_unit}" if price.unit_price_unit else amount


def _table(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    flex: int,
    right: int | None = None,
) -> list[str]:
    """align columns, shrinking column ``flex`` to fit the terminal width."""

    columns = zip(header, *rows, strict=True)
    widths = [max(len(cell) for cell in column) for column in columns]
    others = sum(widths) - widths[flex] + len(GAP) * (len(widths) - 1)
    widths[flex] = max(MIN_FLEX_WIDTH, min(widths[flex], _width() - others))
    lines = []
    for row in (header, *rows):
        cells = [
            _fit(cell, width).rjust(width)
            if index == right
            else _fit(cell, width).ljust(width)
            for index, (cell, width) in enumerate(zip(row, widths, strict=True))
        ]
        lines.append(GAP.join(cells).rstrip())
    return lines


def _wrapped(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """align columns and wrap the last one under itself."""

    widths = [max(len(row[index]) for row in (header, *rows)) for index in (0, 1, 2)]
    indent = sum(widths) + len(GAP) * len(widths)
    room = max(MIN_FLEX_WIDTH, _width() - indent)
    lines = []
    for row in (header, *rows):
        cells = zip(row[:-1], widths, strict=True)
        lead = "".join(cell.ljust(width) + GAP for cell, width in cells)
        first, *rest = textwrap.wrap(row[-1], room) or [""]
        lines.append((lead + first).rstrip())
        lines.extend(" " * indent + part for part in rest)
    return lines


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _width() -> int:
    return shutil.get_terminal_size().columns


def _note_error(store: str, error: SupermercapyError) -> None:
    label = (
        "out of coverage"
        if isinstance(error, OutOfCoverageError)
        else type(error).__name__
    )
    _note(store, label, str(error))


def _note(store: str, label: str | None, detail: str | None = None) -> None:
    parts = [store, label, detail]
    print(": ".join(part for part in parts if part), file=sys.stderr)


def _out(text: str) -> None:
    print(text, file=sys.stdout)


def _out_lines(lines: Sequence[str]) -> None:
    for line in lines:
        _out(line)
