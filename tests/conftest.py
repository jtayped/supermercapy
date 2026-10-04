from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_TESTS = Path(__file__).parent / "live"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run the tests marked live, which call real storefronts",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    skip = pytest.mark.skip(reason="calls a real storefront; pass --live to run")
    run_live = config.getoption("--live")
    for item in items:
        if LIVE_TESTS in item.path.parents:
            item.add_marker(pytest.mark.live)
        if not run_live and item.get_closest_marker("live") is not None:
            item.add_marker(skip)


def read_fixture(store: str, name: str) -> Any:
    path = FIXTURES / store / name
    if path.suffix == ".json":
        with path.open(encoding="utf-8") as fixture_file:
            return json.load(fixture_file)
    return path.read_text(encoding="utf-8")


@pytest.fixture
def load_fixture() -> Any:
    def load(store: str, name: str) -> Any:
        return read_fixture(store, name)

    return load
