"""price one shopping list at every store near a postcode.

    uv run python examples/basket.py 08001
    uv run python examples/basket.py 08001 --save basket.json

each line of the list is what to buy, in which unit and how much of it, such
as six litres of semi-skimmed milk. at every store the script searches the
line, keeps the results ``find_alternatives`` judges to be that kind of
product and priced per that unit, whatever the brand, and takes the one that
covers the quantity for least: enough whole packs of it, or the weight itself
for a product sold by weight. a store selling milk in six-packs and one
selling single litres both price six litres, so the totals compare.

word a line the way the stores name the product, and list the other names
stores give it; each one is searched, because store search matches words. to
``find_alternatives`` a name that adds one word beyond a mere descriptor is
another product, which keeps quail eggs out of "huevos" and also keeps
"huevos camperos" out unless the line names them. a pick far dearer per unit
than the other stores' is left out as a different product sharing the words,
such as pringles for "patatas".

bonpreu and alcampo are left out by default: their firewalls allow a handful
of requests per address in half an hour, and a list is twenty searches.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal

from supermercapy import (
    ALL_CLIENTS,
    BaseClient,
    BlockedError,
    Capability,
    ChallengedError,
    Language,
    OutOfCoverageError,
    ProductSummary,
    RateLimitError,
    SupermercapyError,
    Unit,
    find_alternatives,
)

FIREWALLED = frozenset({"bonpreu", "alcampo"})
# a pick dearer per unit than this many times the line's median is taken for
# another product that happens to share the words, such as pringles for
# "patatas", and left out
IMPLAUSIBLE = Decimal("2.5")
# a pack size read back from a rounded unit price can be a little off, so a
# pack within this share of the quantity covers it
ROUNDING = Decimal("0.03")


@dataclass(frozen=True, slots=True)
class Line:
    description: str
    unit: Unit
    quantity: Decimal
    also: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.description, *self.also)


BASKET = (
    Line("leche semidesnatada", Unit.LITRE, Decimal(6)),
    Line("huevos", Unit.PIECE, Decimal(12), ("huevos frescos",)),
    Line("aceite de oliva virgen extra", Unit.LITRE, Decimal(1)),
    Line("aceite de girasol", Unit.LITRE, Decimal(1), ("aceite refinado de girasol",)),
    Line("arroz redondo", Unit.KILOGRAM, Decimal(1)),
    Line("espaguetis", Unit.KILOGRAM, Decimal(1), ("spaghetti",)),
    Line("tomate triturado", Unit.KILOGRAM, Decimal(1), ("tomate natural triturado",)),
    Line("garbanzos cocidos", Unit.KILOGRAM, Decimal(1)),
    Line("atún claro en aceite de oliva", Unit.KILOGRAM, Decimal("0.3")),
    Line("pan de molde blanco", Unit.KILOGRAM, Decimal("0.5"), ("pan de molde",)),
    Line("yogur natural", Unit.KILOGRAM, Decimal(1)),
    Line("café molido natural", Unit.KILOGRAM, Decimal("0.5")),
    Line("azúcar blanco", Unit.KILOGRAM, Decimal(1)),
    Line("harina de trigo", Unit.KILOGRAM, Decimal(1)),
    Line(
        "plátano de canarias",
        Unit.KILOGRAM,
        Decimal(1),
        ("plátano canario igp", "plátano de canarias igp"),
    ),
    Line("patatas", Unit.KILOGRAM, Decimal(2)),
    Line("cebollas", Unit.KILOGRAM, Decimal(1), ("cebolla seca",)),
    Line(
        "pechuga de pollo", Unit.KILOGRAM, Decimal(1), ("filetes de pechuga de pollo",)
    ),
    Line("agua mineral", Unit.LITRE, Decimal(6), ("agua mineral natural",)),
    Line("detergente líquido", Unit.DOSE, Decimal(40), ("detergente ropa líquido",)),
)


@dataclass(frozen=True, slots=True)
class Pick:
    line: Line
    product: ProductSummary
    unit_price: Decimal
    packs: int | None
    cost: Decimal


@dataclass(frozen=True, slots=True)
class StoreBasket:
    store: str
    store_id: str | None
    picks: dict[str, Pick]
    note: str | None = None


def open_store(client_type: type[BaseClient], postal_code: str) -> BaseClient:
    """build a client near ``postal_code`` that names products in spanish."""

    supported = Language.SPANISH in client_type.supported_languages
    language = Language.SPANISH if supported else None
    if client_type.supports(Capability.POSTAL_CODE):
        try:
            return client_type.from_postal_code(postal_code, language=language)
        except OutOfCoverageError:
            pass
    return client_type(language=language)


def covering(line: Line, product: ProductSummary) -> Pick | None:
    """what buying ``line``'s quantity of ``product`` costs, if it is priced."""

    price, reference = product.price.amount, product.price.reference
    if price is None or reference is None or reference.unit is not line.unit:
        return None
    if product.is_variable_weight:
        return Pick(
            line, product, reference.amount, None, reference.amount * line.quantity
        )
    pack = price / reference.amount
    packs = max(1, math.ceil(line.quantity / pack * (1 - ROUNDING)))
    return Pick(line, product, reference.amount, packs, price * packs)


def cheapest(store: str, line: Line, found: Sequence[ProductSummary]) -> Pick | None:
    listings = [(store, product) for product in found]
    picks = [
        pick
        for name in line.names
        for match in find_alternatives(ProductSummary(id="wanted", name=name), listings)
        if (pick := covering(line, match.right.product)) is not None
    ]
    return min(picks, key=lambda pick: (pick.cost, pick.unit_price), default=None)


def fill(client_type: type[BaseClient], postal_code: str) -> StoreBasket:
    store = client_type.store_name
    try:
        client = open_store(client_type, postal_code)
    except SupermercapyError as error:
        return StoreBasket(store, None, {}, note=f"{type(error).__name__}: {error}")
    picks: dict[str, Pick] = {}
    note = None
    with client:
        size = min(48, client.max_page_size or 48)
        for line in BASKET:
            found: dict[str, ProductSummary] = {}
            for name in line.names:
                try:
                    page = client.search_products(name, page_size=size)
                except (BlockedError, ChallengedError, RateLimitError) as error:
                    note = f"stopped at {name!r}: {type(error).__name__}"
                    return StoreBasket(store, client.store_id, picks, note)
                except SupermercapyError as error:
                    note = f"{name!r} failed: {type(error).__name__}"
                    continue
                for product in page.products:
                    found.setdefault(product.id, product)
            pick = cheapest(store, line, list(found.values()))
            if pick is not None:
                picks[line.description] = pick
        return StoreBasket(store, client.store_id, picks, note)


def plausible(baskets: list[StoreBasket]) -> list[StoreBasket]:
    """drop every pick far dearer per unit than the line's median, and say so."""

    limits = {}
    for line in BASKET:
        prices = [
            basket.picks[line.description].unit_price
            for basket in baskets
            if line.description in basket.picks
        ]
        if prices:
            limits[line.description] = statistics.median(prices) * IMPLAUSIBLE
    kept = []
    for basket in baskets:
        picks = {}
        for description, pick in basket.picks.items():
            if pick.unit_price <= limits[description]:
                picks[description] = pick
                continue
            unit = pick.line.unit.value
            print(
                f"left out at {basket.store}: {pick.product.name},"
                f" {pick.unit_price}€/{unit} for {description!r}"
            )
        kept.append(StoreBasket(basket.store, basket.store_id, picks, basket.note))
    return kept


def report(baskets: list[StoreBasket]) -> None:
    # a store that found most of the list competes on the lines every such
    # store found, so a missing line never makes a basket look cheap
    enough = [basket for basket in baskets if len(basket.picks) >= 0.75 * len(BASKET)]
    shared = [
        line
        for line in BASKET
        if all(line.description in basket.picks for basket in enough)
    ]
    totals = {
        basket.store: sum(
            (basket.picks[line.description].cost for line in shared), Decimal(0)
        )
        for basket in enough
    }
    if not enough:
        print("no store found three lines in four")
    else:
        print(f"{len(shared)} lines every store below found:")
        print(f"  {', '.join(line.description for line in shared)}\n")
    low = min(totals.values(), default=Decimal(1))
    print(f"{'store':<10} {'found':>6} {'total':>9} {'vs cheapest':>12}  store id")
    for basket in sorted(enough, key=lambda basket: totals[basket.store]):
        total = totals[basket.store]
        print(
            f"{basket.store:<10} {len(basket.picks):>3}/{len(BASKET)} {total:>8.2f}€"
            f" {(float(total / low) - 1) * 100:>+11.1f}%  {basket.store_id or '-'}"
        )
    for basket in baskets:
        if basket not in enough:
            found = f"{len(basket.picks):>3}/{len(BASKET)}"
            print(f"{basket.store:<10} {found}  too few lines")
    for basket in baskets:
        if basket.note:
            print(f"  note, {basket.store}: {basket.note}")

    print("\ncheapest store per line:")
    for line in BASKET:
        offers = sorted(
            (basket.picks[line.description].cost, basket.store)
            for basket in baskets
            if line.description in basket.picks
        )
        if not offers:
            print(f"  {line.description:<30} nowhere")
            continue
        cost, store = offers[0]
        pick = next(b.picks[line.description] for b in baskets if b.store == store)
        middle = statistics.median(float(offer[0]) for offer in offers)
        packs = f"{pick.packs} x " if pick.packs else "by weight, "
        print(
            f"  {line.description:<30} {store:<10} {cost:>6.2f}€ median {middle:>6.2f}"
            f"  {packs}{pick.product.name}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("postal_code")
    parser.add_argument(
        "--stores", nargs="*", help="store names, firewalled ones included"
    )
    parser.add_argument("--save", help="write every pick to this json file")
    args = parser.parse_args()
    client_types = [
        client_type
        for client_type in ALL_CLIENTS
        if (
            client_type.store_name in args.stores
            if args.stores
            else client_type.store_name not in FIREWALLED
        )
    ]
    with ThreadPoolExecutor(max_workers=len(client_types)) as pool:
        baskets = list(
            pool.map(lambda kind: fill(kind, args.postal_code), client_types)
        )
    baskets = plausible(baskets)
    print()
    report(baskets)
    if args.save:
        saved = {
            basket.store: {
                "store_id": basket.store_id,
                "note": basket.note,
                "picks": {
                    description: {
                        "name": pick.product.name,
                        "brand": pick.product.brand,
                        "id": pick.product.id,
                        "shelf_price": str(pick.product.price.amount),
                        "unit_price": str(pick.unit_price),
                        "unit": pick.line.unit.value,
                        "packs": pick.packs,
                        "cost": str(pick.cost),
                    }
                    for description, pick in basket.picks.items()
                },
            }
            for basket in baskets
        }
        with open(args.save, "w", encoding="utf-8") as file:
            json.dump(saved, file, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
