import pytest

from pwflow.errors import TemplateError
from pwflow.template import render, render_deep, truthy

CTX = {
    "vars": {"pages": 3, "name": "hn"},
    "data": {"rows": [{"a": 1}, {"a": 2}]},
    "index": 1,
}


def test_sole_expression_keeps_the_native_type():
    assert render("{{ vars.pages }}", CTX) == 3
    assert render("{{ data.rows }}", CTX) == [{"a": 1}, {"a": 2}]
    assert isinstance(render("{{ vars.pages }}", CTX), int)


def test_mixed_string_renders_to_text():
    assert render("out/{{ vars.name }}-{{ vars.pages }}.json", CTX) == "out/hn-3.json"


def test_two_expressions_do_not_get_mangled_by_the_greedy_match():
    assert render("{{ vars.name }}/{{ vars.pages }}", CTX) == "hn/3"


def test_non_templates_pass_through():
    assert render("plain", CTX) == "plain"
    assert render(7, CTX) == 7
    assert render(None, CTX) is None


def test_render_deep_walks_nested_payloads():
    payload = {"a": ["{{ vars.pages }}", {"b": "{{ vars.name }}"}]}
    assert render_deep(payload, CTX) == {"a": [3, {"b": "hn"}]}


def test_scraping_filters():
    assert render("{{ '1,240 points' | to_int }}", CTX) == 1240
    assert render("{{ 'Rank 12.' | regex('(\\\\d+)') }}", CTX) == "12"
    assert render("{{ '/a/b' | absurl('https://x.com/c/d') }}", CTX) == "https://x.com/a/b"


def test_truthy_accepts_bare_and_wrapped_conditions():
    assert truthy("vars.pages > 2", CTX)
    assert truthy("{{ vars.pages > 2 }}", CTX)
    assert not truthy("{{ vars.pages > 9 }}", CTX)
    assert not truthy("{{ data.missing_key is defined and data.missing_key }}", CTX)


def test_dict_keys_beat_dict_methods():
    """`data.items` must be the records you scraped, not `dict.items`."""
    ctx = {"data": {"items": [1, 2, 3], "keys": "k", "count": 9, "get": "g"}}
    assert render("{{ data.items }}", ctx) == [1, 2, 3]
    assert render("{{ data.items | length }}", ctx) == 3
    assert render("{{ data.count }}", ctx) == 9
    assert render("{{ data.keys }}", ctx) == "k"
    assert truthy("data.items | length == 3", ctx)


def test_undefined_variable_is_an_error_not_a_silent_empty_string():
    with pytest.raises(TemplateError):
        render("{{ vars.nope }}", CTX)


def test_sandbox_blocks_attribute_escapes():
    with pytest.raises(TemplateError):
        render("{{ ''.__class__.__mro__ }}", CTX)
