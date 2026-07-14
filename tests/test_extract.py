"""extract fast-path eligibility (the `_css` gate). Behaviour is tested end-to-end."""

import pytest

from pwflow.actions.extract import _css


@pytest.mark.parametrize(
    "selector",
    [
        ".item",
        "a.morelink",
        ".titleline > a",
        "input[type=text]",
        '[data-id="5"]',
        "ul li",
    ],
)
def test_bare_css_is_eligible(selector):
    assert _css(selector) == selector.strip()


@pytest.mark.parametrize(
    "selector",
    [
        "//div[@class='x']",   # xpath by leading //
        "..",                  # xpath parent
        "xpath=//a",           # explicit xpath engine
        "text=Login",          # text engine
        "role=button",         # role engine
        "css=.item >> .child", # playwright chain
        {"role": "button"},    # a SelectorSpec, never plain CSS
        {"css": ".item"},      # even an explicit css SelectorSpec goes the locator route
        "",
    ],
)
def test_non_css_falls_back(selector):
    assert _css(selector) is None
