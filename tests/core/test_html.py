from __future__ import annotations

from typing import Any

import pytest

from supermercapy._core.html import (
    Section,
    attribute_values,
    extract_json_assignment,
    normalise_whitespace,
    strip_tags,
    strong_sections,
    table_rows,
)

# the shape bonàrea delivers a product's ingredient and nutrition data in
BLOB = (
    "<strong>INGREDIENTES*:</strong><p>LECHE fresca desnatada de vaca.</p><br/>"
    "<strong>AL&middot;L&Egrave;RGENS:</strong><p><b>Leche y sus derivados </b></p>"
    "<br/><strong>INFORMACI&Oacute;N NUTRICIONAL:</strong><p>Valor medio por 100 ml:"
    " <br>-Grasas                             < 0,5  g<br>-Sal      0,1  g</p><br/>"
    "<p>*La informaci&oacute;n puede estar sometida a modificaciones.</p>"
)


# ------------------------------------------------------------------ strip_tags


def test_tags_are_removed_and_entities_decoded() -> None:
    assert strip_tags("<p>leche <b>&amp;</b> caf&eacute;</p>").strip() == "leche & café"


def test_line_breaks_and_blocks_become_newlines() -> None:
    text = strip_tags("<p>uno<br>dos<br/>tres</p><div>cuatro</div>")

    assert [line for line in text.splitlines() if line] == [
        "uno",
        "dos",
        "tres",
        "cuatro",
    ]


def test_inline_tags_do_not_split_a_line() -> None:
    markup = "<p>uno <a href='/x'>dos</a> <img src='/y'/><span>tres</span></p>"

    assert strip_tags(markup).strip() == "uno dos tres"


def test_column_alignment_survives_so_callers_can_split_on_it() -> None:
    # the nutrition table is space-aligned plain text, and the alignment is
    # the only separator between a row's name and its measurement
    assert "Grasas                             < 0,5  g" in strip_tags(BLOB)


def test_script_and_style_contents_are_dropped() -> None:
    markup = (
        "<p>uno</p><script>var x = 1;</script><style>p{color:red}</style><p>dos</p>"
    )

    assert normalise_whitespace(strip_tags(markup)) == "uno dos"


def test_unbalanced_markup_is_tolerated() -> None:
    assert normalise_whitespace(strip_tags("<p>uno<div>dos")) == "uno dos"


@pytest.mark.parametrize("value", ["", None, 0, []])
def test_strip_tags_returns_an_empty_string_for_anything_but_markup(
    value: Any,
) -> None:
    assert strip_tags(value) == ""


# --------------------------------------------------------- normalise_whitespace


def test_whitespace_runs_collapse_to_one_space() -> None:
    assert normalise_whitespace("  uno\n\tdos   tres \r\n") == "uno dos tres"


def test_invisible_characters_count_as_whitespace() -> None:
    # a byte-order mark, a no-break space, a zero-width space and a
    # left-to-right mark, none of which a caller wants to keep
    text = "\ufeffuno\u00a0dos\u200btres\u200e"

    assert normalise_whitespace(text) == "uno dos tres"


@pytest.mark.parametrize("value", ["", None, 0, []])
def test_normalise_whitespace_returns_an_empty_string_for_non_text(
    value: Any,
) -> None:
    assert normalise_whitespace(value) == ""


# -------------------------------------------------------------- strong_sections


def test_sections_are_split_on_their_headings_in_order() -> None:
    sections = strong_sections(BLOB)

    assert [section.heading for section in sections] == [
        "INGREDIENTES*:",
        "AL·LÈRGENS:",
        "INFORMACIÓN NUTRICIONAL:",
    ]
    assert isinstance(sections[0], Section)
    assert sections[0].body == "<p>LECHE fresca desnatada de vaca.</p><br/>"


def test_a_section_body_keeps_its_markup_and_runs_to_the_next_heading() -> None:
    body = strong_sections(BLOB)[2].body

    assert body.startswith("<p>Valor medio por 100 ml:")
    assert "-Sal" in body
    # the trailing footnote has no heading of its own, so it belongs here,
    # entities and all: a body is markup, not text
    assert "<p>*La informaci&oacute;n" in body
    assert "<strong>" not in body


def test_headings_are_reduced_to_text() -> None:
    sections = strong_sections("<strong><i>ORIGEN</i>&nbsp;:</strong><p>España</p>")

    assert sections[0].heading == "ORIGEN :"


def test_markup_before_the_first_heading_is_dropped() -> None:
    sections = strong_sections("<p>intro</p><strong>ORIGEN</strong><p>España</p>")

    assert len(sections) == 1
    assert sections[0].body == "<p>España</p>"


def test_an_empty_heading_drops_its_block() -> None:
    markup = "<strong> </strong><p>uno</p><strong>ORIGEN</strong><p>España</p>"

    assert [section.heading for section in strong_sections(markup)] == ["ORIGEN"]


def test_attributes_on_the_heading_tag_are_ignored() -> None:
    sections = strong_sections('<strong class="t">ORIGEN</strong><p>España</p>')

    assert sections[0] == Section(heading="ORIGEN", body="<p>España</p>")


@pytest.mark.parametrize(
    "markup",
    ["", None, "<p>no headings at all</p>", "<strong>unclosed<p>España</p>"],
)
def test_markup_without_a_closed_heading_has_no_sections(markup: Any) -> None:
    assert strong_sections(markup) == ()


# ----------------------------------------------------------- attribute values


def test_attribute_values_decode_entities_and_keep_document_order() -> None:
    # how lidl ships a campaign page: one json gridbox per tile, escaped into
    # a data attribute
    markup = (
        '<div data-grid-data="{&quot;id&quot;:&quot;1&quot;}"></div>'
        "<div class='other'></div>"
        '<div data-grid-data="{&quot;id&quot;:&quot;2&quot;}"></div>'
    )

    assert attribute_values(markup, "div", "data-grid-data") == (
        '{"id":"1"}',
        '{"id":"2"}',
    )


def test_attribute_values_match_the_tag_and_the_attribute_case_insensitively() -> None:
    markup = '<DIV DATA-GRID-DATA="x"><span data-grid-data="y"></span></DIV>'

    assert attribute_values(markup, "div", "data-grid-data") == ("x",)


def test_attribute_values_read_self_closing_tags_too() -> None:
    assert attribute_values('<img src="a.jpg"/><img/>', "img", "src") == ("a.jpg",)


@pytest.mark.parametrize(
    ("markup", "tag", "attribute"),
    [
        ("", "div", "id"),
        (None, "div", "id"),
        ("<div id=1>", "", "id"),
        ("<div id=1>", "div", ""),
        ("<div hidden>", "div", "id"),
    ],
)
def test_attribute_values_never_raise_on_unusable_input(
    markup: Any, tag: str, attribute: str
) -> None:
    assert attribute_values(markup, tag, attribute) == ()


# the shape carrefour hands its rendered page state to the browser in
STATE = (
    "<script>window.__INITIAL_STATE__ = "
    '{"pdp": {"product": {"name": "a};</script> b"}}, "n": [1, 2]};'
    "window.__VUE__=true;</script>"
)


def test_extract_json_assignment_reads_past_braces_inside_strings() -> None:
    state = extract_json_assignment(STATE, "window.__INITIAL_STATE__")
    assert isinstance(state, dict)
    # a greedy regex would stop at the first "};" inside the name
    assert state["pdp"]["product"]["name"] == "a};</script> b"
    assert state["n"] == [1, 2]


def test_extract_json_assignment_reads_an_array_assignment() -> None:
    assert extract_json_assignment('var rows = [{"a": 1}];', "var rows") == [{"a": 1}]


def test_extract_json_assignment_takes_the_first_opener_after_the_marker() -> None:
    assert extract_json_assignment('a = [1] and b = {"x": 2}', "a =") == [1]


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("", "window.__STATE__"),
        (None, "window.__STATE__"),
        ("window.__STATE__ = {}", ""),
        ("window.__STATE__ = {}", None),
        ("nothing here", "window.__STATE__"),
        ("window.__STATE__ = undefined;", "window.__STATE__"),
        ("window.__STATE__ = {broken;", "window.__STATE__"),
    ],
)
def test_extract_json_assignment_never_raises_on_unusable_input(
    text: Any, marker: Any
) -> None:
    assert extract_json_assignment(text, marker) is None


# --------------------------------------------------------------- table rows


# the shape bonpreu pastes a nutrition table into one json string in
NUTRITION_TABLE = (
    "<table><tbody><tr><td></td><td> per 100 g </td></tr>"
    "<tr><td> Valor energ&egrave;tic </td><td> 363 kcal / 1537 kJ </td></tr>"
    "<tr><td> Greixos </td><td> 2 g </td></tr></tbody></table>"
)


def test_table_rows_reads_one_tuple_per_row_with_cells_collapsed() -> None:
    assert table_rows(NUTRITION_TABLE) == (
        ("", "per 100 g"),
        ("Valor energètic", "363 kcal / 1537 kJ"),
        ("Greixos", "2 g"),
    )


def test_header_cells_come_back_as_the_first_row() -> None:
    markup = "<table><tr><th>nutrient</th><th>per 100 g</th></tr>"
    markup += "<tr><td>sal</td><td>0,5 g</td></tr></table>"

    assert table_rows(markup) == (("nutrient", "per 100 g"), ("sal", "0,5 g"))


def test_markup_inside_a_cell_is_stripped_and_entities_decoded() -> None:
    markup = "<table><tr><td><b>sal</b> i <i>sucre</i></td><td>0,5&nbsp;g</td></tr>"

    assert table_rows(markup) == (("sal i sucre", "0,5 g"),)


def test_an_unclosed_row_or_cell_is_still_returned() -> None:
    # storefronts that paste a table into a string rarely close one
    assert table_rows("<table><tr><td>sal<td>0,5 g") == (("sal", "0,5 g"),)


def test_rows_holding_no_cells_are_dropped() -> None:
    assert table_rows("<table><tr></tr><tr><td>sal</td></tr></table>") == (("sal",),)


def test_several_tables_are_read_as_one_sequence_of_rows() -> None:
    markup = "<table><tr><td>a</td></tr></table><table><tr><td>b</td></tr></table>"

    assert table_rows(markup) == (("a",), ("b",))


def test_a_cell_outside_any_row_still_becomes_a_row() -> None:
    assert table_rows("<table><td>sal</td></table>") == (("sal",),)


@pytest.mark.parametrize("markup", ["", None, 7, "<p>no table at all</p>"])
def test_table_rows_never_raise_on_unusable_input(markup: Any) -> None:
    assert table_rows(markup) == ()
