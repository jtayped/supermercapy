"""tolerant text extraction for stores that deliver data as html.

some storefronts answer with markup where a structured field belongs: a
nutrition table pasted into one string, a description split by ``<br>``. these
helpers turn such a blob into text with the standard library only, and never
raise on malformed markup; a caller that needs certainty keeps the raw html.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import NamedTuple

__all__ = [
    "Section",
    "attribute_values",
    "extract_json_assignment",
    "normalise_whitespace",
    "strip_tags",
    "strong_sections",
    "table_rows",
]

_STRONG = re.compile(r"<strong\b[^>]*>(.*?)</strong>", re.IGNORECASE | re.DOTALL)
_WHITESPACE = re.compile("[\\s\\u00a0\\u200b\\u200e\\ufeff]+")
# tags whose boundaries separate lines of text rather than words
_BREAKING_TAGS = frozenset(
    {
        "br",
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "tr",
        "td",
        "th",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
_IGNORED_TAGS = frozenset({"script", "style"})
_CELL_TAGS = frozenset({"td", "th"})


class Section(NamedTuple):
    """one heading and the raw markup that follows it."""

    heading: str
    body: str


class _TextExtractor(HTMLParser):
    """collect the text of a fragment, breaking lines on block boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignoring = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _IGNORED_TAGS:
            self._ignoring += 1
        elif tag in _BREAKING_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BREAKING_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _IGNORED_TAGS:
            self._ignoring = max(self._ignoring - 1, 0)
        elif tag in _BREAKING_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignoring:
            self._parts.append(data)

    @property
    def text(self) -> str:
        """return everything collected so far."""

        return "".join(self._parts)


class _AttributeCollector(HTMLParser):
    """collect one attribute's value from every occurrence of one tag."""

    def __init__(self, tag: str, attribute: str) -> None:
        super().__init__(convert_charrefs=True)
        self._tag = tag.lower()
        self._attribute = attribute.lower()
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != self._tag:
            return
        for name, value in attrs:
            if name.lower() == self._attribute and value is not None:
                self.values.append(value)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


class _TableCollector(HTMLParser):
    """collect one tuple of cell texts per table row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, ...]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._close_row()
            self._row = []
        elif tag in _CELL_TAGS:
            self._close_cell()
            if self._row is None:
                # a cell outside any row still belongs to a row of its own
                self._row = []
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _CELL_TAGS:
            self._close_cell()
        elif tag == "tr":
            self._close_row()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        super().close()
        self._close_row()

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(normalise_whitespace("".join(self._cell)))
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row:
            self.rows.append(tuple(self._row))
        self._row = None


def table_rows(html: str) -> tuple[tuple[str, ...], ...]:
    """return every table row of ``html`` as a tuple of its cell texts.

    ``th`` and ``td`` are treated alike, so a header row comes back as the
    first row rather than as a separate structure, and each cell's text is
    whitespace-collapsed with its own markup stripped. rows holding no cells
    are dropped; an unclosed row or cell is still returned, because a
    storefront that pastes a table into a json string rarely closes one.
    """

    if not isinstance(html, str) or not html:
        return ()
    parser = _TableCollector()
    parser.feed(html)
    parser.close()
    return tuple(parser.rows)


def attribute_values(html: str, tag: str, attribute: str) -> tuple[str, ...]:
    """return ``attribute``'s value for every ``tag`` element, in document order.

    the parser decodes entities itself, so a value holding escaped json comes
    back ready for :func:`json.loads`. elements carrying the tag without the
    attribute are skipped, and malformed markup never raises.
    """

    if not isinstance(html, str) or not html or not tag or not attribute:
        return ()
    parser = _AttributeCollector(tag, attribute)
    parser.feed(html)
    parser.close()
    return tuple(parser.values)


def strip_tags(html: str) -> str:
    """return the text of ``html``, one line per block or ``<br>``.

    entities are decoded, ``script`` and ``style`` contents are dropped, and
    every other tag is removed without touching the spacing around it, so a
    caller can still split column-aligned text on runs of spaces.
    """

    if not isinstance(html, str) or not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text


def normalise_whitespace(text: str) -> str:
    """collapse every run of whitespace, including newlines, to one space."""

    if not isinstance(text, str) or not text:
        return ""
    return _WHITESPACE.sub(" ", text).strip()


def strong_sections(html: str) -> tuple[Section, ...]:
    """split ``html`` into the ``<strong>heading</strong>body`` blocks it holds.

    the heading is reduced to text; the body keeps its markup and runs to the
    next heading, so nested lines survive for a caller that wants them. markup
    before the first heading, and a block whose heading has no text, are
    dropped.
    """

    if not isinstance(html, str) or not html:
        return ()
    matches = list(_STRONG.finditer(html))
    sections: list[Section] = []
    for index, match in enumerate(matches):
        heading = normalise_whitespace(strip_tags(match.group(1)))
        if not heading:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        sections.append(Section(heading=heading, body=html[match.end() : end]))
    return tuple(sections)


def extract_json_assignment(text: str, marker: str) -> object | None:
    """return the json value assigned to ``marker`` inside a script blob.

    server-rendered storefronts hand their page state to the browser as one
    ``window.__STATE__ = {…}`` statement. the value is read with
    :meth:`json.JSONDecoder.raw_decode` starting at the first ``{`` or ``[``
    after the marker, because a regular expression that stops at the closing
    brace either overshoots the object or stops inside a nested one.

    ``None`` comes back when the marker is absent or what follows it is not
    json, so a caller can fall back to another source instead of branching on
    an exception.
    """

    if not isinstance(text, str) or not isinstance(marker, str) or not marker:
        return None
    index = text.find(marker)
    if index < 0:
        return None
    start = -1
    for opener in ("{", "["):
        found = text.find(opener, index + len(marker))
        if found >= 0 and (start < 0 or found < start):
            start = found
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return None
    decoded: object = value
    return decoded
