"""Authoring mistakes must die at load time, before a browser is ever launched."""

import pytest

from pwflow import load_flow
from pwflow.errors import FlowLoadError


def test_minimal_flow_loads():
    flow = load_flow("name: t\nsteps:\n  - goto: https://example.com\n")
    assert flow.steps[0].action == "goto"
    assert flow.steps[0].parsed.url == "https://example.com"


def test_shorthand_and_modifiers_split():
    flow = load_flow(
        """
        name: t
        steps:
          - id: nav
            click: ".btn"
            when: "{{ true }}"
            retry: 3
            optional: true
        """
    )
    step = flow.steps[0]
    assert step.action == "click"
    assert step.parsed.selector == ".btn"  # scalar shorthand expanded
    assert step.id == "nav" and step.optional and step.retry.times == 3


def test_typo_in_action_name():
    with pytest.raises(FlowLoadError, match="unknown action `clcik`"):
        load_flow("name: t\nsteps:\n  - clcik: .btn\n")


def test_missing_required_param():
    with pytest.raises(FlowLoadError, match="value"):
        load_flow("name: t\nsteps:\n  - fill: {selector: '#a'}\n")


def test_templated_non_string_leaf_field_loads():
    """`sleep.ms` is an int, but `sleep: "{{ vars.n }}"` renders to one at runtime —
    the loader must not reject it just because the literal is a template string."""
    flow = load_flow(
        """
        name: t
        vars: {n: 800}
        steps:
          - sleep: "{{ vars.n }}"
          - foreach:
              in: "{{ vars.items }}"
              steps:
                - sleep: "{{ vars.n }}"
        """
    )
    assert flow.steps[0].action == "sleep"


def test_typo_still_caught_when_params_are_literal():
    # deferring templated params must not weaken typo-catching for literal payloads
    with pytest.raises(FlowLoadError, match="Extra inputs"):
        load_flow("name: t\nsteps:\n  - sleep: {ms: 100, msec: 200}\n")


def test_two_action_keys_in_one_step():
    with pytest.raises(FlowLoadError, match="exactly one action key"):
        load_flow("name: t\nsteps:\n  - click: .a\n    goto: https://x.com\n")


def test_unknown_param_is_rejected():
    with pytest.raises(FlowLoadError, match="Extra inputs"):
        load_flow("name: t\nsteps:\n  - goto: {url: 'https://x.com', wait_untl: load}\n")


def test_nested_steps_are_validated_too():
    with pytest.raises(FlowLoadError, match="unknown action `clik`"):
        load_flow(
            """
            name: t
            steps:
              - foreach:
                  in: "{{ data.rows }}"
                  steps:
                    - clik: ".x"
            """
        )


def test_selector_needs_exactly_one_engine():
    with pytest.raises(FlowLoadError, match="exactly one of"):
        load_flow("name: t\nsteps:\n  - click: {css: .a, xpath: //a}\n")


def test_extract_attr_without_attr_name():
    with pytest.raises(FlowLoadError, match="needs an `attr:` name"):
        load_flow(
            """
            name: t
            steps:
              - extract:
                  name: x
                  fields:
                    y: {selector: .a, type: attr}
            """
        )
