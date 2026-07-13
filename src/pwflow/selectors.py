"""Turn a YAML selector into a Playwright ``Locator``.

Bare strings go straight to Playwright's selector engine, so everything you already
know still works (``".btn"``, ``"text=Login"``, ``"xpath=//a"``). The structured form
exists because the *good* selectors — the ones that survive a redesign — are the
accessibility ones, and they are awkward to express as strings::

    click:
      role: button
      name: "Add to cart"
      within: { test_id: "product-card" }
      nth: 0
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Locator, Page

from .errors import SelectorError
from .models import SelectorSpec

Scope = Page | Locator


def _base_locator(scope: Scope, spec: SelectorSpec) -> Locator:
    exact = spec.exact
    if spec.css is not None:
        return scope.locator(spec.css)
    if spec.xpath is not None:
        return scope.locator(f"xpath={spec.xpath}")
    if spec.text is not None:
        return scope.get_by_text(spec.text, exact=exact)
    if spec.role is not None:
        kwargs: dict[str, Any] = {}
        if spec.name is not None:
            kwargs["name"] = spec.name
            kwargs["exact"] = exact
        return scope.get_by_role(spec.role, **kwargs)  # type: ignore[arg-type]
    if spec.label is not None:
        return scope.get_by_label(spec.label, exact=exact)
    if spec.placeholder is not None:
        return scope.get_by_placeholder(spec.placeholder, exact=exact)
    if spec.test_id is not None:
        return scope.get_by_test_id(spec.test_id)
    if spec.alt is not None:
        return scope.get_by_alt_text(spec.alt, exact=exact)
    if spec.title is not None:
        return scope.get_by_title(spec.title, exact=exact)
    raise SelectorError(f"selector has no engine key: {spec!r}")


def resolve(scope: Scope, selector: Any) -> Locator:
    """Resolve a selector (string or mapping) against a page or a parent locator."""
    if isinstance(selector, Locator):
        return selector
    if isinstance(selector, str):
        return scope.locator(selector)
    if isinstance(selector, dict):
        try:
            selector = SelectorSpec.model_validate(selector)
        except Exception as e:
            raise SelectorError(f"bad selector {selector!r}: {e}") from e
    if not isinstance(selector, SelectorSpec):
        raise SelectorError(f"a selector must be a string or a mapping, got {selector!r}")

    if selector.within is not None:
        scope = resolve(scope, selector.within)

    loc = _base_locator(scope, selector)

    if selector.has_text is not None:
        loc = loc.filter(has_text=selector.has_text)
    if selector.has is not None:
        loc = loc.filter(has=resolve(scope, selector.has))
    if selector.first:
        loc = loc.first
    elif selector.last:
        loc = loc.last
    elif selector.nth is not None:
        loc = loc.nth(selector.nth)
    return loc
