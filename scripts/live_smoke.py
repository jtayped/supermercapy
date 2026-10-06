"""make a handful of real requests against one store and print what came back.

usage: ``python scripts/live_smoke.py <store>``. this is the only code in the
repository that talks to a live storefront; the test suite never does. every
store gets three calls — a binding or category lookup, one search, and one
product detail — except bonpreu, whose bot protection is answered by asking for
less: categories and one search, and nothing else.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from supermercapy import (
    ALL_CLIENTS,
    BaseClient,
    Capability,
    RetryPolicy,
    SupermercapyError,
)

RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_factor=0.5, max_delay=2.0)
TIMEOUT = 15.0
PAGE_SIZE = 5


@dataclass(frozen=True)
class Plan:
    """what one store is asked for, and from where."""

    query: str
    postal_code: str | None = None
    detail: bool = True


PLANS: dict[str, Plan] = {
    "mercadona": Plan(query="leche", postal_code="28001"),
    "consum": Plan(query="arroz", postal_code="46001"),
    "plusfresc": Plan(query="llet", postal_code="25001"),
    "bonarea": Plan(query="pollastre"),
    "carrefour": Plan(query="leche", postal_code="28232"),
    "lidl": Plan(query="chocolate", postal_code="08013"),
    "bonpreu": Plan(query="llet", detail=False),
}


def client_types() -> dict[str, type[BaseClient]]:
    """return every registered client, keyed on its store name."""

    return {client_type.store_name: client_type for client_type in ALL_CLIENTS}


def open_client(client_type: type[BaseClient], plan: Plan) -> tuple[BaseClient, str]:
    """build a client, binding it to a postal code where the store supports one."""

    options: dict[str, Any] = {"timeout": TIMEOUT, "retry_policy": RETRY_POLICY}
    if plan.postal_code is not None and client_type.supports(Capability.POSTAL_CODE):
        client = client_type.from_postal_code(plan.postal_code, **options)
        return client, f"bound {plan.postal_code} to store_id {client.store_id!r}"
    client = client_type(**options)
    return client, f"built with store_id {client.store_id!r}"


def smoke(store: str) -> list[str]:
    """run one store's calls and return one summary line per call."""

    plan = PLANS[store]
    client_type = client_types()[store]
    lines: list[str] = []
    client, summary = open_client(client_type, plan)
    lines.append(f"open: {summary}")
    with client:
        if plan.postal_code is None or not client.supports(Capability.POSTAL_CODE):
            categories = client.get_categories()
            names = ", ".join(category.name for category in categories[:3])
            lines.append(f"categories: {len(categories)} roots ({names})")

        page = client.search_products(plan.query, page_size=PAGE_SIZE)
        lines.append(
            f"search {plan.query!r}: {len(page.products)} rows, "
            f"total_hits {page.total_hits}, more {page.has_more}"
        )
        if not page.products:
            raise SystemExit(f"{store}: live search returned no products")

        if plan.detail:
            first = page.products[0]
            product = client.get_product(first.id)
            lines.append(
                f"product {product.id}: {product.name!r} at {product.price.amount}"
            )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """run the smoke calls for one store named on the command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", choices=sorted(PLANS), help="which store to call")
    arguments = parser.parse_args(argv)
    store: str = arguments.store

    if store not in client_types():
        print(f"{store}: no client is registered under that name")
        return 1

    print(f"== {store} ==")
    try:
        lines = smoke(store)
    except SupermercapyError as error:
        print(f"{store}: {type(error).__name__}: {error}")
        return 1
    for line in lines:
        print(line)
    print(f"{store}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
