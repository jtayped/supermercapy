from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

from supermercapy import ALL_CLIENTS, BaseClient, Capability

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FILES = tuple(sorted(ROOT.glob("*.md"))) + tuple(
    sorted((ROOT / "docs").rglob("*.md"))
)
FENCED_BLOCK = re.compile(r"^```.*?^```$", re.MULTILINE | re.DOTALL)
PYTHON_BLOCK = re.compile(r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`]*`")
LINK_DESTINATION = re.compile(r"\]\([^)]*\)")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
HTML_TAG = re.compile(r"<[^>]*>")
MKDOCSTRINGS_DIRECTIVE = re.compile(r"^:::\s+\S+$", re.MULTILINE)


def relative_name(path: Path) -> str:
    return str(path.relative_to(ROOT))


@pytest.mark.parametrize("path", MARKDOWN_FILES, ids=relative_name)
def test_documentation_prose_is_lowercase(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    prose = FENCED_BLOCK.sub("", source)
    prose = INLINE_CODE.sub("", prose)
    prose = LINK_DESTINATION.sub("]", prose)
    prose = HTML_TAG.sub("", prose)
    prose = MKDOCSTRINGS_DIRECTIVE.sub("", prose)
    violations = [
        f"{relative_name(path)}:{line_number}: {line}"
        for line_number, line in enumerate(prose.splitlines(), start=1)
        if re.search(r"[A-Z]", line)
    ]
    assert not violations, "uppercase documentation prose:\n" + "\n".join(violations)


@pytest.mark.parametrize("path", MARKDOWN_FILES, ids=relative_name)
def test_local_documentation_links_resolve(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for target in MARKDOWN_LINK.findall(source):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        relative_path, _, fragment = target.partition("#")
        destination = (path.parent / relative_path).resolve()
        assert destination.exists(), f"{relative_name(path)}: missing {target}"
        if not fragment or not destination.is_file():
            continue
        headings = destination.read_text(encoding="utf-8")
        anchors = {
            re.sub(r"[^a-z0-9 -]", "", heading.lower()).strip().replace(" ", "-")
            for heading in re.findall(r"^#{1,6} +(.*)$", headings, re.MULTILINE)
        }
        assert fragment in anchors, f"{relative_name(path)}: missing anchor {target}"


@pytest.mark.parametrize("path", MARKDOWN_FILES, ids=relative_name)
def test_python_documentation_blocks_compile(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for block_number, code in enumerate(PYTHON_BLOCK.findall(source), start=1):
        compile(
            code,
            f"{relative_name(path)}:python-block-{block_number}",
            "exec",
        )


def read_capability_matrix() -> dict[str, frozenset[str]]:
    """return the capability matrix of ``docs/api.md`` as store to flag names."""

    lines = (ROOT / "docs" / "api.md").read_text(encoding="utf-8").splitlines()
    starts = [
        number for number, line in enumerate(lines) if line.startswith("| capability |")
    ]
    assert len(starts) == 1, "docs/api.md must hold exactly one capability matrix"
    header = [cell.strip() for cell in lines[starts[0]].strip("|").split("|")]
    stores = header[1:]
    assert len(set(stores)) == len(stores), "the matrix repeats a store column"
    matrix: dict[str, set[str]] = {store: set() for store in stores}
    for line in lines[starts[0] + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == len(header), f"ragged matrix row: {cells[0]}"
        flag = cells[0].strip("`")
        for store, cell in zip(stores, cells[1:], strict=True):
            assert cell in {"yes", "no"}, f"{flag}/{store} says {cell!r}"
            if cell == "yes":
                matrix[store].add(flag)
    return {store: frozenset(flags) for store, flags in matrix.items()}


def test_capability_matrix_lists_every_flag() -> None:
    lines = (ROOT / "docs" / "api.md").read_text(encoding="utf-8").splitlines()
    start = next(
        number for number, line in enumerate(lines) if line.startswith("| capability |")
    )
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line.strip("|").split("|")[0].strip().strip("`"))
    assert rows == [capability.name for capability in Capability]


def test_capability_matrix_columns_are_every_client() -> None:
    matrix = read_capability_matrix()
    assert set(matrix) == {client.store_name for client in ALL_CLIENTS}


@pytest.mark.parametrize(
    "client", ALL_CLIENTS, ids=lambda client: str(client.store_name)
)
def test_capability_matrix_matches_declared_capabilities(
    client: type[BaseClient],
) -> None:
    matrix = read_capability_matrix()
    assert client.store_name in matrix, "the matrix has no column for this store"
    assert matrix[client.store_name] == frozenset(
        capability.name for capability in client.capabilities
    )


def test_readme_capability_table_matches_declared_capabilities() -> None:
    """the readme is the page pypi shows, so its short table is checked too."""

    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("| capability | stores |")
    table: dict[str, set[str]] = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        flag, stores = (cell.strip() for cell in line.strip("|").split("|"))
        table[flag.strip("`")] = {
            "".join(
                char
                for char in unicodedata.normalize("NFKD", store.strip())
                if not unicodedata.combining(char)
            )
            for store in stores.split(",")
        }
    declared = {
        capability.name: {
            client.store_name for client in ALL_CLIENTS if client.supports(capability)
        }
        for capability in Capability
    }
    assert table == declared
