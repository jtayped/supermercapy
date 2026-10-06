"""the supermercapy command, driven offline through the canned storefronts."""

from __future__ import annotations

import argparse
import json
import re
import runpy
import sys
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from supermercapy import (
    ALL_CLIENTS,
    BaseClient,
    Capability,
    Category,
    Price,
    Product,
    SearchResult,
    __version__,
    cli,
)
from tests.canned import Storefronts, failing
from tests.harness import HARNESSES

ROOT = Path(__file__).resolve().parents[1]
STORE_NAMES = [client_type.store_name for client_type in ALL_CLIENTS]


class Run:
    """one invocation's exit code and output."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    @property
    def lines(self) -> list[str]:
        return self.out.splitlines()

    @property
    def notes(self) -> list[str]:
        return self.err.splitlines()


@pytest.fixture
def fronts(monkeypatch: pytest.MonkeyPatch) -> Storefronts:
    fronts = Storefronts()
    monkeypatch.setattr(
        cli,
        "_client_keywords",
        lambda: {"transport": fronts.transport, "min_request_interval": 0.0},
    )
    monkeypatch.setenv("COLUMNS", "80")
    return fronts


@pytest.fixture
def run(fronts: Storefronts, capsys: pytest.CaptureFixture[str]) -> Any:
    def invoke(*argv: str) -> Run:
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return invoke


def columns(line: str) -> list[str]:
    return re.split(r"\s{2,}", line.strip())


# -------------------------------------------------------------- the program


def test_version_prints_the_package_version(run: Any) -> None:
    result = run("--version")

    assert result.code == 0
    assert result.out == f"supermercapy {__version__}\n"


def test_no_command_is_a_usage_error(run: Any) -> None:
    result = run()

    assert result.code == 2
    assert "required: COMMAND" in result.err


def test_an_unknown_store_is_a_usage_error(run: Any, fronts: Storefronts) -> None:
    result = run("search", "llet", "--store", "elcorteingles")

    assert result.code == 2
    assert "unknown store 'elcorteingles'; expected one of mercadona" in result.err
    assert fronts.requests == []


@pytest.mark.parametrize(
    "argv",
    [
        ("search", "llet", "--postal-code", "99001"),
        ("search", "llet", "--limit", "0"),
        ("search", "llet", "--limit", "many"),
        ("ean", "1234"),
        ("ean", "84148075142x9"),
    ],
)
def test_invalid_arguments_are_usage_errors(run: Any, argv: tuple[str, ...]) -> None:
    result = run(*argv)

    assert result.code == 2
    assert result.out == ""
    assert "error: argument" in result.err


def test_a_keyboard_interrupt_exits_quietly(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "search_all", interrupted)

    result = run("search", "llet")

    assert (result.code, result.out, result.err) == (130, "", "")


def test_python_dash_m_runs_the_same_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["supermercapy", "--version"])

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("supermercapy", run_name="__main__")

    assert raised.value.code == 0
    assert capsys.readouterr().out == f"supermercapy {__version__}\n"


def test_the_console_script_points_at_main() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"] == {"supermercapy": "supermercapy.cli:main"}


def test_clients_keep_their_own_defaults() -> None:
    assert cli._client_keywords() == {}


def test_every_command_has_a_section_in_the_documentation() -> None:
    page = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    (commands,) = [
        action
        for action in cli._parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    ]

    assert set(commands.choices) == {
        "stores",
        "search",
        "compare",
        "product",
        "categories",
        "ean",
    }
    for name in commands.choices:
        assert f"\n## {name}\n" in page


# ------------------------------------------------------------------- stores


def test_stores_lists_every_store_without_a_request(
    run: Any, fronts: Storefronts
) -> None:
    result = run("stores")

    assert result.code == 0
    assert fronts.requests == []
    assert columns(result.lines[0]) == ["store", "class", "binds to", "capabilities"]
    starts = [line.split()[0] for line in result.lines[1:] if not line.startswith(" ")]
    assert starts == [client_type.store_name for client_type in ALL_CLIENTS]
    assert all(len(line) <= 80 for line in result.lines)
    text = result.out
    assert "mercadona  Mercadona  warehouse (required)" in text
    assert "zone (optional)" in text
    assert "center (default 12)" in text
    assert "sale point (optional)" in text
    assert "bonarea    Bonarea    nothing" in text
    assert "future_prices" in text


def test_a_wide_terminal_keeps_capabilities_on_one_line(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")

    result = run("stores")

    assert len(result.lines) == len(ALL_CLIENTS) + 1


def test_stores_json_describes_every_store(run: Any) -> None:
    result = run("stores", "--json")

    document = json.loads(result.out)
    assert [entry["store"] for entry in document] == [
        client_type.store_name for client_type in ALL_CLIENTS
    ]
    mercadona, consum, _, bonarea, *_ = document
    assert mercadona["class"] == "Mercadona"
    assert mercadona["binding"] == "warehouse"
    assert mercadona["binding_required"] is True
    assert consum["binding"] == "zone"
    assert consum["binding_required"] is False
    assert bonarea["binding"] is None
    assert bonarea["binding_required"] is False
    assert bonarea["capabilities"] == ["CATALOG", "NUTRITION"]


# ------------------------------------------------------------------- search


def test_search_asks_every_store_and_notes_what_it_could_not(
    run: Any, fronts: Storefronts
) -> None:
    result = run("search", "llet")

    assert result.code == 0
    assert columns(result.lines[0]) == ["store", "id", "name", "price", "unit price"]
    rows = [columns(line) for line in result.lines[1:]]
    assert rows[0] == [
        "consum",
        "7080604",
        "Leche Semidesnatada Brik",
        "0.84",
        "0.84/l",
    ]
    stores = [row[0] for row in rows]
    assert stores == sorted(stores, key=[c.store_name for c in ALL_CLIENTS].index)
    assert stores.count("consum") == 2
    assert stores.count("bonarea") == 5
    assert all(len(line) <= 80 for line in result.lines)
    assert result.notes == [
        "mercadona: skipped: needs a postal code to bind a warehouse",
        "carrefour: no results",
    ]
    assert fronts.count("mercadona") == 0


def test_search_prints_prices_as_written(run: Any) -> None:
    result = run("search", "leche", "--store", "mercadona", "--postal-code", "28001")

    assert result.code == 0
    rows = [columns(line) for line in result.lines[1:]]
    assert rows == [
        ["mercadona", "1001", "Leche entera Hacendado", "1.23"],
        ["mercadona", "2002", "Leche sin lactosa", "1.345"],
    ]


def test_search_with_a_postal_code_notes_stores_out_of_coverage(run: Any) -> None:
    result = run("search", "leche", "--postal-code", "28232")

    assert result.code == 0
    stores = [columns(line)[0] for line in result.lines[1:]]
    assert list(dict.fromkeys(stores)) == [
        name for name in STORE_NAMES if name not in {"plusfresc", "lidl", "condis"}
    ]
    assert result.notes == [
        "plusfresc: out of coverage: plusfresc does not deliver to 28232",
        "lidl: out of coverage: lidl has no store near 28232",
        "condis: out of coverage: condis does not deliver to 28232",
    ]


def test_a_failing_store_exits_one_and_keeps_the_rest(
    run: Any, fronts: Storefronts
) -> None:
    fronts.handlers["lidl"] = failing(400)

    result = run("search", "llet")

    assert result.code == 1
    stores = {columns(line)[0] for line in result.lines[1:]}
    # mercadona needs a postal code, carrefour finds no "llet", lidl fails
    assert stores == set(STORE_NAMES) - {"mercadona", "carrefour", "lidl"}
    (failure,) = [note for note in result.notes if note.startswith("lidl")]
    assert re.fullmatch(
        r"lidl: TransportError: GET https://www\.lidl\.es/q/api/search\S* "
        r"returned HTTP 400",
        failure,
    )


def test_named_stores_are_searched_alone_in_the_order_given(
    run: Any, fronts: Storefronts
) -> None:
    result = run("search", "llet", "--store", "lidl", "--store", "Consum")

    assert result.code == 0
    stores = [columns(line)[0] for line in result.lines[1:]]
    assert stores == ["lidl", "lidl", "consum", "consum"]
    assert {store for store, _ in fronts.requests} == {"lidl", "consum"}


def test_naming_a_store_that_needs_a_postal_code_is_a_usage_error(
    run: Any, fronts: Storefronts
) -> None:
    result = run("search", "llet", "--store", "mercadona")

    assert result.code == 2
    assert result.out == ""
    assert result.notes[-1] == (
        "supermercapy search: error: mercadona needs a postal code to bind a warehouse"
    )
    assert fronts.requests == []


def test_a_blank_query_is_a_usage_error(run: Any, fronts: Storefronts) -> None:
    result = run("search", "  ")

    assert result.code == 2
    assert result.notes[-1].endswith("error: query must be a non-empty string")
    assert fronts.requests == []


def test_limit_is_the_page_size_every_store_is_asked_for(run: Any) -> None:
    result = run("search", "llet", "--limit", "3", "--json")

    pages = [entry["result"] for entry in json.loads(result.out)["stores"]]
    # every store but mercadona, which needs a postal code, answers a page
    assert [page["page_size"] for page in pages if page] == [3] * (len(STORE_NAMES) - 1)


def test_long_names_are_cut_to_the_terminal_width(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "60")

    result = run("search", "llet", "--store", "bonpreu")

    assert all(len(line) <= 60 for line in result.lines)
    names = [columns(line)[2] for line in result.lines[1:]]
    assert all(name.endswith("…") for name in names)
    prices = [columns(line)[3:] for line in result.lines[1:]]
    assert prices[-1] == ["5.04", "0.84/l"]


def test_a_search_with_no_results_prints_no_table(run: Any) -> None:
    result = run("search", "nothing", "--store", "carrefour")

    assert result.code == 0
    assert result.out == ""
    assert result.notes == ["carrefour: no results"]


def test_search_json_is_one_document(run: Any, fronts: Storefronts) -> None:
    fronts.handlers["lidl"] = failing(400)

    result = run("search", "llet", "--json", "--limit", "2")

    assert result.code == 1
    document = json.loads(result.out)
    assert document["query"] == "llet"
    statuses = {entry["store"]: entry["status"] for entry in document["stores"]}
    assert statuses == {
        **dict.fromkeys(STORE_NAMES, "ok"),
        "mercadona": "skipped",
        "lidl": "failed",
    }
    consum = document["stores"][1]["result"]
    assert len(consum["products"]) == 2
    assert consum["products"][0]["price"]["amount"] == "0.84"
    assert any(note.startswith("lidl: TransportError: ") for note in result.notes)


# ------------------------------------------------------------------ product


def test_product_prints_one_field_per_line(run: Any, fronts: Storefronts) -> None:
    result = run("product", "consum", "7080604")

    assert result.code == 0
    assert result.lines == [
        "store       consum",
        "id          7080604",
        "name        Leche Semidesnatada Brik",
        "brand       CONSUM",
        "ean         8414807514219",
        "price       0.84",
        "unit price  0.84/l",
        "available   yes",
        "category    Semidesnatada",
        "url         https://tienda.consum.es/es/p/leche-semidesnatada-brik/7080604",
    ]
    assert fronts.count("consum") == 1


def test_product_shows_the_binding_and_the_previous_price(run: Any) -> None:
    result = run("product", "mercadona", "1001", "--postal-code", "28001")

    assert result.code == 0
    assert result.lines[0] == "store       mercadona, warehouse mad3"
    assert "price       1.23 (was 1.30)" in result.lines
    assert "unit price  1.23/l" in result.lines


@pytest.mark.parametrize(
    ("price", "shown"),
    [
        (Price(unit_price=Decimal("2.5"), unit_price_unit="kg"), "2.5/kg"),
        (Price(unit_price=Decimal("2.5")), "2.5"),
        (Price(unit_price_text="2,50 €/kg"), "2,50 €/kg"),
        (Price(), ""),
    ],
)
def test_unit_price_falls_back_to_what_the_store_published(
    price: Price, shown: str
) -> None:
    assert cli._unit_price(price) == shown


def test_product_lists_promotions(run: Any) -> None:
    result = run("product", "lidl", "11150856")

    assert result.code == 0
    assert "promotion  Con Lidl Plus" in result.lines
    assert "category   Mundos de necesidad > Comida y cerca de la comida > " in (
        result.out
    )


def test_product_json_is_the_whole_record(run: Any) -> None:
    result = run("product", "bonpreu", "29189", "--json")

    assert result.code == 0
    document = json.loads(result.out)
    assert document["id"] == "29189"
    assert document["name"] == "GALLO Farina de rebosteria"
    assert "nutrition" in document


def test_a_missing_product_exits_one_with_one_line(run: Any) -> None:
    result = run("product", "consum", "missing")

    assert result.code == 1
    assert result.out == ""
    assert len(result.notes) == 1
    assert result.notes[0].startswith("consum: NotFoundError: ")


def test_a_product_outside_the_coverage_exits_one(run: Any) -> None:
    result = run("product", "plusfresc", "002530", "--postal-code", "28001")

    assert result.code == 1
    assert result.notes == [
        "plusfresc: out of coverage: plusfresc does not deliver to 28001"
    ]


def test_a_product_the_store_cannot_bind_is_a_usage_error(
    run: Any, fronts: Storefronts
) -> None:
    result = run("product", "mercadona", "1001")

    assert result.code == 2
    assert result.notes[-1].endswith(
        "error: mercadona needs a postal code to bind a warehouse"
    )
    assert fronts.requests == []


def test_an_id_the_store_rejects_is_a_usage_error(
    run: Any, fronts: Storefronts
) -> None:
    result = run("product", "consum", "  ")

    assert result.code == 2
    assert result.notes[-1].startswith("supermercapy product: error: product_id")
    assert fronts.requests == []


# --------------------------------------------------------------- categories


def test_categories_indent_the_tree(run: Any) -> None:
    result = run("categories", "bonarea")

    assert result.code == 0
    assert result.lines[:4] == [
        "id                  name",
        "13*300              Alimentación",
        "13*300*010            Carnes y huevos",
        "13*300*010*010          Aves",
    ]


def test_categories_json_keeps_the_children(run: Any) -> None:
    result = run("categories", "consum", "--json")

    document = json.loads(result.out)
    assert [root["name"] for root in document] == ["Despensa", "Bebidas"]
    assert document[0]["children"]


def test_a_failing_category_tree_exits_one(run: Any, fronts: Storefronts) -> None:
    fronts.handlers["bonarea"] = failing(400)

    result = run("categories", "bonarea")

    assert result.code == 1
    assert result.out == ""
    assert result.notes[0].startswith("bonarea: TransportError: ")


# ---------------------------------------------------------------------- ean


def test_ean_asks_every_store_that_can_look_one_up(
    run: Any, fronts: Storefronts
) -> None:
    result = run("ean", "8414807514219")

    assert result.code == 0
    assert [columns(line) for line in result.lines] == [
        ["store", "id", "name", "price", "unit price"],
        ["consum", "7080604", "Leche Semidesnatada Brik", "0.84", "0.84/l"],
    ]
    assert result.notes == ["carrefour: not found"]
    eligible = {c.store_name for c in ALL_CLIENTS if c.supports(Capability.EAN_LOOKUP)}
    assert {store for store, _ in fronts.requests} == eligible


def test_ean_json_reports_every_store(run: Any, fronts: Storefronts) -> None:
    fronts.handlers["consum"] = failing(400)

    result = run("ean", "8431876011937", "--json", "--postal-code", "08019")

    assert result.code == 1
    document = json.loads(result.out)
    assert document["ean"] == "8431876011937"
    assert document["postal_code"] == "08019"
    consum, carrefour = document["stores"]
    assert consum["status"] == "failed"
    assert consum["error_type"] == "TransportError"
    assert consum["product"] is None
    assert carrefour["status"] == "out_of_coverage"
    assert carrefour["reason"] == "carrefour serves no store near 08019"


def test_ean_json_carries_each_product_or_why_not(run: Any) -> None:
    result = run("ean", "8414807514219", "--json")

    assert result.code == 0
    consum, carrefour = json.loads(result.out)["stores"]
    assert consum["status"] == "ok"
    assert consum["product"]["ean"] == "8414807514219"
    assert consum["reason"] is consum["error_type"] is None
    assert carrefour["status"] == "not_found"
    assert carrefour["product"] is None
    assert carrefour["error_type"] == "NotFoundError"


def test_ean_out_of_coverage_is_not_a_failure(run: Any) -> None:
    result = run("ean", "8414807514219", "--postal-code", "08019")

    assert result.code == 0
    assert result.notes == [
        "carrefour: out of coverage: carrefour serves no store near 08019"
    ]


class _Bound(BaseClient):
    """an ean store that cannot be built without a binding."""

    store_name = "bound"
    capabilities = frozenset({Capability.EAN_LOOKUP})

    def __init__(self, depot: str, **options: Any) -> None:
        super().__init__(**options)

    def search_products(
        self, query: str, *, page_size: int | None = None, cursor: str | None = None
    ) -> SearchResult:
        raise NotImplementedError

    def get_product(self, product_id: str | int) -> Product:
        raise NotImplementedError

    def get_categories(self) -> tuple[Category, ...]:
        raise NotImplementedError

    def get_category(self, category_id: str | int) -> Category:
        raise NotImplementedError


def test_ean_notes_a_store_it_had_to_skip(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "ALL_CLIENTS", (_Bound,))

    human = run("ean", "8414807514219")
    machine = run("ean", "8414807514219", "--json")

    assert human.code == machine.code == 0
    assert human.notes == [
        "bound: skipped: needs a depot, which a postal code cannot supply"
    ]
    (entry,) = json.loads(machine.out)["stores"]
    assert entry["status"] == "skipped"
    assert entry["reason"] == "needs a depot, which a postal code cannot supply"


# ------------------------------------------------------------------ compare


@pytest.fixture
def shared_catalog(fronts: Storefronts) -> Storefronts:
    """serve caprabo eroski's canned answers: the two run one catalogue."""

    fronts.handlers["caprabo"] = HARNESSES["eroski"].handler
    return fronts


def test_compare_lines_up_the_same_product_across_stores(
    run: Any, shared_catalog: Storefronts
) -> None:
    result = run(
        "compare",
        "aceite",
        "--store",
        "eroski",
        "--store",
        "caprabo",
        "--store",
        "consum",
    )

    assert result.code == 0
    assert columns(result.lines[0]) == [
        "group",
        "score",
        "store",
        "id",
        "name",
        "price",
        "unit price",
    ]
    rows = [columns(line) for line in result.lines[1:]]
    assert rows[0][:4] == ["1", "1.00", "eroski", "3854569"]
    assert rows[0][-2:] == ["12.80", "4.27/l"]
    assert rows[1][:2] == ["caprabo", "3854569"]
    assert [row[0] for row in rows if row[0].isdigit()] == ["1", "2", "3"]
    assert all(len(line) <= 80 for line in result.lines)
    assert result.notes == ["2 of 8 products matched no other store"]
    assert [
        shared_catalog.count(store) for store in ("eroski", "caprabo", "consum")
    ] == [
        1,
        1,
        1,
    ]


def test_compare_lists_the_cheapest_store_first(
    run: Any, shared_catalog: Storefronts
) -> None:
    def dearer(request: Any) -> Any:
        response = HARNESSES["eroski"].handler(request)
        return response.__class__(
            response.status_code,
            request=request,
            content=response.content.replace(b"12,80", b"13,10").replace(
                b"12.80", b"13.10"
            ),
            headers=response.headers,
        )

    shared_catalog.handlers["eroski"] = dearer

    result = run("compare", "aceite", "--store", "eroski", "--store", "caprabo")

    rows = [columns(line) for line in result.lines[1:]]
    assert rows[0][2:4] == ["caprabo", "3854569"]
    assert rows[1][:2] == ["eroski", "3854569"]


def test_compare_json_carries_groups_and_what_matched_nothing(
    run: Any, shared_catalog: Storefronts
) -> None:
    shared_catalog.handlers["lidl"] = failing(400)

    result = run(
        "compare",
        "aceite",
        "--store",
        "eroski",
        "--store",
        "caprabo",
        "--store",
        "consum",
        "--store",
        "lidl",
        "--json",
        "--threshold",
        "0.9",
    )

    assert result.code == 1
    document = json.loads(result.out)
    assert document["query"] == "aceite"
    assert document["threshold"] == 0.9
    first = document["groups"][0]
    assert [listing["store"] for listing in first["listings"]] == ["eroski", "caprabo"]
    assert first["score"] == 1.0
    assert first["reasons"] == ["brand", "size", "name 1.00"]
    assert [listing["store"] for listing in document["unmatched"]] == [
        "consum",
        "consum",
    ]
    statuses = {entry["store"]: entry["status"] for entry in document["stores"]}
    assert statuses == {
        "eroski": "ok",
        "caprabo": "ok",
        "consum": "ok",
        "lidl": "failed",
    }
    assert any(note.startswith("lidl: TransportError: ") for note in result.notes)


def test_compare_with_nothing_shared_prints_no_table(run: Any) -> None:
    result = run("compare", "llet", "--store", "consum", "--store", "bonarea")

    assert result.code == 0
    assert result.out == ""
    assert result.notes == ["7 of 7 products matched no other store"]


@pytest.mark.parametrize("threshold", ["1.5", "-1", "high"])
def test_compare_refuses_a_threshold_outside_zero_to_one(
    run: Any, fronts: Storefronts, threshold: str
) -> None:
    result = run("compare", "llet", "--threshold", threshold)

    assert result.code == 2
    assert "expected a score from 0 to 1" in result.err
    assert fronts.requests == []
