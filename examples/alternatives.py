"""what a branded product costs per unit, and the cheapest thing like it.

    uv run python examples/alternatives.py 08001
    uv run python examples/alternatives.py 08001 --save alternatives.json

every brand on the list is searched twice at every store, once by name and
once by the kind of product it is. ``find_alternatives`` keeps the results of
that kind, cheapest per unit first, and splits them into the brand and
everything else. ``group_same`` then lines up the brand's identical packs
across stores, which shows what loyalty to one store costs on top of loyalty
to the brand. every alternative carries the reasons it matched, so a store
brand is labelled as one by the matcher, not by a list kept here.

bonpreu and alcampo are left out by default: their firewalls allow a handful
of requests per address in half an hour, and this is forty searches a store.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
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
    group_same,
    to_json,
)

FIREWALLED = frozenset({"bonpreu", "alcampo"})


@dataclass(frozen=True, slots=True)
class Brand:
    brand: str
    kind: str
    unit: Unit
    query: str


BRANDS = (
    Brand("Cola Cao", "cacao soluble", Unit.KILOGRAM, "cola cao"),
    Brand("Coca-Cola", "refresco de cola", Unit.LITRE, "coca cola"),
    Brand("Nocilla", "crema de cacao con avellanas", Unit.KILOGRAM, "nocilla"),
    Brand("Puleva", "leche semidesnatada", Unit.LITRE, "leche puleva"),
    Brand("Carbonell", "aceite de oliva virgen extra", Unit.LITRE, "carbonell"),
    Brand("Barilla", "espaguetis", Unit.KILOGRAM, "barilla"),
    Brand("Gallo", "macarrones", Unit.KILOGRAM, "pasta gallo"),
    Brand("Danone", "yogur natural", Unit.KILOGRAM, "danone natural"),
    Brand("Nescafé", "café soluble", Unit.KILOGRAM, "nescafe"),
    Brand("Kellogg's", "copos de maíz", Unit.KILOGRAM, "kellogg's corn flakes"),
    Brand("Philadelphia", "queso crema", Unit.KILOGRAM, "philadelphia"),
    Brand("Hellmann's", "mayonesa", Unit.KILOGRAM, "hellmann's"),
    Brand("Calvo", "atún claro en aceite de oliva", Unit.KILOGRAM, "atun calvo"),
    Brand("Fairy", "lavavajillas a mano", Unit.LITRE, "fairy"),
    Brand("Font Vella", "agua mineral", Unit.LITRE, "font vella"),
    Brand("Bimbo", "pan de molde", Unit.KILOGRAM, "pan bimbo"),
    Brand("Mahou", "cerveza", Unit.LITRE, "mahou"),
    Brand("ElPozo", "jamón cocido", Unit.KILOGRAM, "elpozo jamon cocido"),
)

Listing = tuple[str, ProductSummary]


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


def fold(text: str | None) -> str:
    """letters and digits only, so "COCA-COLA" meets "Coca Cola"."""

    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", plain.lower())


def is_brand(product: ProductSummary, brand: Brand) -> bool:
    wanted = fold(brand.brand)
    return fold(product.brand) == wanted or wanted in fold(product.name)


def gather(
    client_type: type[BaseClient], postal_code: str
) -> tuple[str, list[Listing], str | None]:
    """every result of both searches for every brand at one store."""

    store = client_type.store_name
    try:
        client = open_store(client_type, postal_code)
    except SupermercapyError as error:
        return store, [], f"{type(error).__name__}: {error}"
    found: dict[str, ProductSummary] = {}
    note = None
    with client:
        size = min(48, client.max_page_size or 48)
        queries = dict.fromkeys(
            query for brand in BRANDS for query in (brand.query, brand.kind)
        )
        for query in queries:
            try:
                page = client.search_products(query, page_size=size)
            except (BlockedError, ChallengedError, RateLimitError) as error:
                note = f"stopped at {query!r}: {type(error).__name__}"
                break
            except SupermercapyError as error:
                note = f"{query!r} failed: {type(error).__name__}"
                continue
            for product in page.products:
                found.setdefault(product.id, product)
    return store, [(store, product) for product in found.values()], note


@dataclass(frozen=True, slots=True)
class Offer:
    store: str
    product: ProductSummary
    per_unit: Decimal
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Finding:
    brand: Brand
    brand_cheapest: Offer
    brand_dearest: Offer
    alternative: Offer
    same_pack: tuple[tuple[str, Decimal], ...]
    same_pack_name: str

    @property
    def saving(self) -> Decimal:
        return 1 - self.alternative.per_unit / self.brand_cheapest.per_unit


def offers(brand: Brand, pool: Sequence[Listing]) -> list[Offer]:
    """the listings of ``brand``'s kind priced per its unit, cheapest first."""

    wanted = ProductSummary(id="wanted", name=brand.kind)
    found = []
    for match in find_alternatives(wanted, pool):
        product = match.right.product
        reference = product.price.reference
        if (
            reference is not None
            and reference.unit is brand.unit
            and not product.is_variable_weight
        ):
            found.append(
                Offer(match.right.store or "", product, reference.amount, match.reasons)
            )
    return found


def judge(brand: Brand, pool: Sequence[Listing]) -> Finding | None:
    of_kind = offers(brand, pool)
    branded = [offer for offer in of_kind if is_brand(offer.product, brand)]
    others = [offer for offer in of_kind if not is_brand(offer.product, brand)]
    if not branded or not others:
        return None
    groups = group_same([(offer.store, offer.product) for offer in branded])
    widest = max(groups, key=lambda group: len(group.listings))
    same_pack = sorted(
        (listing.store or "", listing.product.price.amount)
        for listing in widest.listings
        if listing.product.price.amount is not None
    )
    return Finding(
        brand=brand,
        brand_cheapest=branded[0],
        brand_dearest=branded[-1],
        alternative=others[0],
        same_pack=tuple(sorted(same_pack, key=lambda item: item[1])),
        same_pack_name=widest.listings[0].product.name,
    )


def show(finding: Finding) -> None:
    brand, unit = finding.brand, finding.brand.unit.value
    cheapest, dearest = finding.brand_cheapest, finding.brand_dearest
    other = finding.alternative
    print(f"\n{brand.brand}, {brand.kind}")
    print(
        f"  brand per {unit}: {cheapest.per_unit:.2f}€ at {cheapest.store}"
        f" ({cheapest.product.name}) to {dearest.per_unit:.2f}€ at {dearest.store}"
    )
    saving = finding.saving
    difference = f"{saving:.0%} less" if saving >= 0 else f"{-saving:.0%} more"
    print(
        f"  cheapest alike: {other.per_unit:.2f}€ at {other.store}"
        f" ({other.product.name}, {other.product.brand or 'no brand'}), {difference}"
    )
    print(f"  matched on: {', '.join(other.reasons)}")
    if len(finding.same_pack) > 1:
        prices = ", ".join(f"{store} {price}" for store, price in finding.same_pack)
        print(f"  same pack, {finding.same_pack_name}: {prices}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("postal_code")
    parser.add_argument(
        "--stores", nargs="*", help="store names, firewalled ones included"
    )
    parser.add_argument("--save", help="write the findings to this json file")
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
        gathered = list(
            pool.map(lambda kind: gather(kind, args.postal_code), client_types)
        )
    for store, _, note in gathered:
        if note:
            print(f"note, {store}: {note}")
    listings = [listing for _, found, _ in gathered for listing in found]

    findings = []
    for brand in BRANDS:
        finding = judge(brand, listings)
        if finding is None:
            print(f"\n{brand.brand}: not enough priced results to compare")
            continue
        findings.append(finding)
        show(finding)

    if args.save:
        with open(args.save, "w", encoding="utf-8") as file:
            file.write(to_json(findings, indent=2))


if __name__ == "__main__":
    main()
