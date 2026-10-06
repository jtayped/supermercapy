"""keep catalog snapshots, report what changed between two, and compare stores.

    uv run python examples/tracker.py snapshot --postal-code 08001
    uv run python examples/tracker.py diff snapshots/2026-10-06 snapshots/2026-10-07
    uv run python examples/tracker.py compare snapshots/2026-10-06
    uv run python examples/tracker.py upcoming

``snapshot`` walks every store that declares ``Capability.CATALOG`` and writes
one json lines file per store, a product a line as ``to_json`` writes it, plus
a ``manifest.json`` with the binding, the count and any error. run it once a
day, from cron or a scheduled ci job, and ``diff`` any two of them.

``diff`` reports, store by store, what appeared, what went, which shelf
prices moved, and which products now cost more per kilogram or litre than
their shelf price explains: the pack shrank, or grew less than its price.

``compare`` finds the same product at several stores within one snapshot and
reports how each store's shelf prices stand against the others'.

``upcoming`` lists the prices lidl and aldi have published that start later.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import unicodedata
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from supermercapy import (
    ALL_CLIENTS,
    Aldi,
    BaseClient,
    Capability,
    Lidl,
    OutOfCoverageError,
    Price,
    ProductSummary,
    SupermercapyError,
    Unit,
    UnitPrice,
    group_same,
    to_json,
)

Record = dict[str, Any]

SPAIN = ZoneInfo("Europe/Madrid")

# lidl's walk hydrates one product per request, about six thousand of them,
# so a daily snapshot leaves it out unless it is named
SLOW_WALKS = frozenset({"lidl"})


# ---------------------------------------------------------------- snapshot


def open_store(
    client_type: type[BaseClient], postal_code: str | None
) -> tuple[BaseClient, str | None]:
    """build a client bound near ``postal_code``, or at the store's default."""

    if postal_code and client_type.supports(Capability.POSTAL_CODE):
        try:
            client = client_type.from_postal_code(postal_code)
            return client, postal_code
        except OutOfCoverageError:
            pass
    return client_type(), None


def walk(
    client_type: type[BaseClient], postal_code: str | None, folder: Path
) -> Record:
    """write one store's catalog to ``folder`` and describe how it went."""

    started = time.monotonic()
    entry: Record = {"store": client_type.store_name}
    try:
        client, bound_to = open_store(client_type, postal_code)
        with client:
            products = client.get_catalog()
            entry["store_id"] = client.store_id
    except SupermercapyError as error:
        entry["error"] = f"{type(error).__name__}: {error}"
        return entry
    path = folder / f"{client_type.store_name}.jsonl"
    with path.open("w", encoding="utf-8") as file:
        for product in products:
            file.write(to_json(product) + "\n")
    entry.update(
        postal_code=bound_to,
        products=len(products),
        seconds=round(time.monotonic() - started),
    )
    return entry


def snapshot(args: argparse.Namespace) -> None:
    folder = Path(args.output) / date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    wanted = set(args.stores) if args.stores else None
    client_types = [
        client_type
        for client_type in ALL_CLIENTS
        if client_type.supports(Capability.CATALOG)
        and (
            client_type.store_name in wanted
            if wanted is not None
            else client_type.store_name not in SLOW_WALKS
        )
    ]
    # every store is a different host, so the walks run side by side and
    # each client still paces its own requests
    with ThreadPoolExecutor(max_workers=len(client_types)) as pool:
        entries = list(
            pool.map(
                lambda client_type: walk(client_type, args.postal_code, folder),
                client_types,
            )
        )
    # a second run the same day, say for a store that failed, adds to the
    # manifest instead of replacing it
    path = folder / "manifest.json"
    walked = {entry["store"] for entry in entries}
    earlier = json.loads(path.read_text())["stores"] if path.exists() else []
    manifest = {
        "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "postal_code": args.postal_code,
        "stores": [entry for entry in earlier if entry["store"] not in walked]
        + entries,
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    for entry in entries:
        if "error" in entry:
            print(f"{entry['store']:<10} failed: {entry['error']}")
        else:
            print(
                f"{entry['store']:<10} {entry['products']:>6} products"
                f" in {entry['seconds']:>4}s, store {entry['store_id']}"
            )
    print(f"written to {folder}")


# -------------------------------------------------------------------- read


def load(folder: Path) -> dict[str, dict[str, Record]]:
    """every store's records in a snapshot, keyed on store and product id."""

    stores: dict[str, dict[str, Record]] = {}
    for path in sorted(folder.glob("*.jsonl")):
        with path.open(encoding="utf-8") as file:
            stores[path.stem] = {
                record["id"]: record for record in map(json.loads, file)
            }
    return stores


def amount(record: Record) -> Decimal | None:
    value = (record.get("price") or {}).get("amount")
    return None if value is None else Decimal(value)


def reference(record: Record) -> UnitPrice | None:
    value = (record.get("price") or {}).get("reference")
    if value is None:
        return None
    return UnitPrice(amount=Decimal(value["amount"]), unit=Unit(value["unit"]))


def summary(record: Record) -> ProductSummary:
    """rebuild the parts of a summary that matching reads."""

    return ProductSummary(
        id=record["id"],
        name=record["name"],
        brand=record.get("brand"),
        ean=record.get("ean"),
        pack_size_text=record.get("pack_size_text"),
        price=Price(amount=amount(record), reference=reference(record)),
        is_variable_weight=record.get("is_variable_weight", False),
    )


def percent(ratio: Decimal | float) -> str:
    return f"{(float(ratio) - 1) * 100:+.1f}%"


def described(record: Record) -> str:
    pack = record.get("pack_size_text")
    return f"{record['name']}" + (f" ({pack})" if pack else "")


# -------------------------------------------------------------------- diff


@dataclass(frozen=True, slots=True)
class Change:
    old: Record
    new: Record
    shelf: Decimal
    per_unit: Decimal | None


def changes(old: dict[str, Record], new: dict[str, Record]) -> Iterator[Change]:
    for product_id in old.keys() & new.keys():
        before, after = old[product_id], new[product_id]
        if after.get("is_variable_weight"):
            # a variable-weight shelf price is an estimate for one typical piece
            continue
        old_amount, new_amount = amount(before), amount(after)
        if not old_amount or not new_amount:
            continue
        old_reference, new_reference = reference(before), reference(after)
        per_unit = None
        if (
            old_reference is not None
            and new_reference is not None
            and old_reference.unit is new_reference.unit
            and old_reference.amount
        ):
            per_unit = new_reference.amount / old_reference.amount
        yield Change(before, after, new_amount / old_amount, per_unit)


def diff(args: argparse.Namespace) -> None:
    old_snapshot, new_snapshot = load(Path(args.old)), load(Path(args.new))
    for store in sorted(old_snapshot.keys() & new_snapshot.keys()):
        old, new = old_snapshot[store], new_snapshot[store]
        found = list(changes(old, new))
        moved = [change for change in found if change.shelf != 1]
        # a discount that starts or ends moves the shelf and the unit price
        # together; a smaller pack moves only the unit price
        shrunk = [
            change
            for change in found
            if change.per_unit is not None
            and change.per_unit > change.shelf * Decimal("1.02")
        ]
        up = sum(change.shelf > 1 for change in moved)
        print(f"\n== {store}")
        print(
            f"{len(new) - len(new.keys() - old.keys())} kept,"
            f" {len(new.keys() - old.keys())} new, {len(old.keys() - new.keys())} gone;"
            f" {len(moved)} shelf prices moved, {up} up and {len(moved) - up} down"
        )
        if moved:
            middle = statistics.median(float(change.shelf) for change in moved)
            print(f"median move among those: {percent(middle)}")
        for change in sorted(moved, key=lambda change: change.shelf, reverse=True)[
            : args.top
        ]:
            before, after = amount(change.old), amount(change.new)
            print(
                f"  {percent(change.shelf):>8}  {before} -> {after}"
                f"  {described(change.new)}"
            )
        if shrunk:
            print("costs more per unit than its shelf price explains:")
        for change in sorted(
            shrunk, key=lambda change: change.per_unit or 0, reverse=True
        )[: args.top]:
            shelf, per_unit = percent(change.shelf), percent(change.per_unit or 0)
            print(
                f"  shelf {shelf:>7}, per unit {per_unit:>7}"
                f"  {described(change.old)} -> {described(change.new)}"
            )


# ----------------------------------------------------------------- compare


def words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", folded.lower())


def brand_key(brand: str | None) -> str | None:
    """fold a brand to letters and digits, so "COCA-COLA" meets "Coca Cola"."""

    return "".join(words(brand or "")) or None


def brand_in_name(name: str, known: set[str]) -> str | None:
    """the known brand a name ends nearest to, such as mercadona's
    "Tónica original Schweppes", whose listings carry no brand of their own."""

    found = words(name)
    for end in range(len(found), 0, -1):
        for start in range(max(0, end - 3), end):
            key = "".join(found[start:end])
            if key in known:
                return key
    return None


def compare(args: argparse.Namespace) -> None:
    stores = load(Path(args.snapshot))
    by_brand: dict[str, list[tuple[str, ProductSummary]]] = defaultdict(list)
    brandless: list[tuple[str, Record]] = []
    for store, records in stores.items():
        for record in records.values():
            if not amount(record) or record.get("is_variable_weight"):
                continue
            key = brand_key(record.get("brand"))
            if key is None:
                brandless.append((store, record))
            else:
                by_brand[key].append((store, summary(record)))
    known = set(by_brand)
    for store, record in brandless:
        key = brand_in_name(record["name"], known)
        if key is not None:
            by_brand[key].append((store, summary(record)))
    # grouping is pairwise, so it runs one brand at a time; a product the
    # stores file under different brands is not compared
    groups = [
        group
        for listings in by_brand.values()
        if len({store for store, _ in listings}) > 1
        for group in group_same(listings)
        if len(group.listings) > 1
    ]
    print(f"{len(groups)} products found at two stores or more")
    # (price, store, product) for every listing of a group, cheapest first
    priced = [
        sorted(
            (
                listing.product.price.amount or Decimal(0),
                listing.store or "",
                listing.product,
            )
            for listing in group.listings
        )
        for group in groups
    ]

    # each store's price against the average of the stores selling the same
    # pack, averaged geometrically so +50% and -33% cancel out
    ratios: dict[str, list[float]] = defaultdict(list)
    cheapest: dict[str, int] = defaultdict(int)
    for listings in priced:
        mean = statistics.fmean(float(price) for price, _, _ in listings)
        for price, store, _ in listings:
            ratios[store].append(float(price) / mean)
            cheapest[store] += price == listings[0][0]
    print(f"\n{'store':<10} {'shared':>7} {'cheapest':>9} {'vs average':>11}")
    for store in sorted(
        ratios, key=lambda store: statistics.geometric_mean(ratios[store])
    ):
        shared = len(ratios[store])
        index = statistics.geometric_mean(ratios[store])
        share = cheapest[store] / shared
        print(f"{store:<10} {shared:>7} {share:>9.0%} {percent(index):>11}")

    print("\nwidest gaps for the same product:")
    widest = sorted(
        priced, key=lambda listings: listings[-1][0] / listings[0][0], reverse=True
    )
    for listings in widest[: args.top]:
        (low, low_store, product), (high, high_store, _) = listings[0], listings[-1]
        pack = product.pack_size_text or "-"
        print(
            f"  {percent(high / low):>8}  {product.name} ({pack}):"
            f" {low_store} {low} vs {high_store} {high}"
        )


# ---------------------------------------------------------------- upcoming


def upcoming(args: argparse.Namespace) -> None:
    today = datetime.now(SPAIN).date()
    with Lidl() as lidl:
        products = lidl.get_offers(week="next")
    print(f"lidl, next week: {len(products)} products")
    for product in products[: args.top]:
        for price in product.future_prices[:1]:
            starts = (
                price.valid_from.astimezone(SPAIN).date() if price.valid_from else "?"
            )
            print(
                f"  from {starts}  {price.amount} €  {product.name}"
                f" ({price.packaging_text or '-'})"
            )

    with Aldi.from_postal_code(args.postal_code) as aldi:
        groups = aldi.get_offer_groups()
    later = [group for group in groups if group.starts_on and group.starts_on > today]
    print(f"\naldi: {len(later)} offer sections not started yet")
    for group in later:
        print(
            f"  {group.starts_on} to {group.ends_on}  {group.title},"
            f" {len(group.products)} products"
        )
        for offer in group.products[:3]:
            print(f"      {offer.price.amount} €  {offer.name}")


# --------------------------------------------------------------------- cli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(required=True)

    taking = commands.add_parser("snapshot", help="walk every catalog to disk")
    taking.add_argument("--postal-code", default="08001")
    taking.add_argument("--output", default="snapshots")
    taking.add_argument("--stores", nargs="*", help="store names, lidl included")
    taking.set_defaults(run=snapshot)

    diffing = commands.add_parser("diff", help="what changed between two snapshots")
    diffing.add_argument("old")
    diffing.add_argument("new")
    diffing.add_argument("--top", type=int, default=10)
    diffing.set_defaults(run=diff)

    comparing = commands.add_parser("compare", help="the same products across stores")
    comparing.add_argument("snapshot")
    comparing.add_argument("--top", type=int, default=15)
    comparing.set_defaults(run=compare)

    coming = commands.add_parser("upcoming", help="prices that start later")
    coming.add_argument("--postal-code", default="08001")
    coming.add_argument("--top", type=int, default=10)
    coming.set_defaults(run=upcoming)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
