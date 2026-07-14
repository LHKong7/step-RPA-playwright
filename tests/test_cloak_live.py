"""Opt-in live CloakBrowser smoke test.

Skipped unless PWFLOW_TEST_CLOAK=1, because it downloads the ~200MB stealth binary
and drives a real browser. Run it with:

    uv sync --extra cloak
    PWFLOW_TEST_CLOAK=1 uv run pytest tests/test_cloak_live.py -v
"""

import os

import pytest

from pwflow import Engine
from pwflow.loader import load_flow

pytestmark = pytest.mark.skipif(
    os.environ.get("PWFLOW_TEST_CLOAK") != "1",
    reason="set PWFLOW_TEST_CLOAK=1 (and `uv sync --extra cloak`) to run the live cloak test",
)


async def test_cloak_launches_and_scrapes():
    pytest.importorskip("cloakbrowser")
    flow = load_flow(
        """
        name: cloak-smoke
        browser:
          provider: cloak
          headless: true
        steps:
          - goto: https://example.com
          - extract: {name: heading, selector: h1}
          - assert: {selector: h1, state: visible}
        """
    )
    async with Engine() as engine:
        result = await engine.run(flow)
    assert result.status == "success", result.error
    assert "Example Domain" in result.data["heading"]
