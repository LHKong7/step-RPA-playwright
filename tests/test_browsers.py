"""Provider selection and CloakBrowser argument assembly.

These do not launch a real CloakBrowser — that needs a ~200MB binary and (for the Pro
build) a license. They pin the *decisions*: which options move to the launch layer, when
a browser is owned by the run, and how proxy/stealth flags are shaped for cloakbrowser.
A real cloak smoke test lives in test_cloak_live.py, opt-in behind an env var.
"""

import sys
import types

import pytest

from pwflow.browsers import _cloak_launch_kwargs, _proxy_arg, acquire
from pwflow.errors import BrowserError, FlowLoadError
from pwflow.loader import load_flow
from pwflow.models import BrowserConfig, Proxy


def cfg(**kw) -> BrowserConfig:
    return BrowserConfig(**kw)


# -- config validation -----------------------------------------------------


def test_cdp_provider_requires_a_url():
    with pytest.raises(FlowLoadError, match="needs a `cdp_url"):
        load_flow("name: t\nbrowser: {provider: cdp}\nsteps:\n  - goto: https://x.com\n")


def test_cloak_block_parses():
    flow = load_flow(
        """
        name: t
        browser:
          provider: cloak
          headless: false
          cloak: {humanize: true, human_preset: careful, geoip: true}
        steps:
          - goto: https://x.com
        """
    )
    assert flow.browser.provider == "cloak"
    assert flow.browser.cloak.humanize is True
    assert flow.browser.cloak.human_preset == "careful"


def test_unknown_cloak_key_is_rejected():
    with pytest.raises(FlowLoadError, match="Extra inputs"):
        load_flow(
            "name: t\nbrowser:\n  provider: cloak\n  cloak: {humanise: true}\n"
            "steps:\n  - goto: https://x.com\n"
        )


# -- cloak launch kwargs ---------------------------------------------------


def test_cloak_kwargs_minimal():
    assert _cloak_launch_kwargs(cfg(provider="cloak")) == {"headless": True}


def test_cloak_kwargs_full():
    c = cfg(
        provider="cloak",
        headless=False,
        locale="en-US",
        timezone="America/New_York",
        proxy=Proxy(server="http://p:8080"),
        cloak={"humanize": True, "human_preset": "careful", "geoip": True,
               "license_key": "cb_x", "stealth_args": False, "args": ["--mute-audio"]},
    )
    kw = _cloak_launch_kwargs(c)
    assert kw == {
        "headless": False,
        "proxy": "http://p:8080",
        "locale": "en-US",
        "timezone": "America/New_York",
        "humanize": True,
        "human_preset": "careful",
        "geoip": True,
        "license_key": "cb_x",
        "stealth_args": False,
        "args": ["--mute-audio"],
    }


def test_proxy_arg_bare_url_vs_dict():
    assert _proxy_arg(Proxy(server="http://p:8080")) == "http://p:8080"
    with_auth = _proxy_arg(Proxy(server="http://p:8080", username="u", password="pw"))
    assert with_auth == {"server": "http://p:8080", "username": "u", "password": "pw"}
    with_bypass = _proxy_arg(Proxy(server="http://p:8080", bypass=".internal"))
    assert with_bypass == {"server": "http://p:8080", "bypass": ".internal"}


# -- acquire() dispatch ----------------------------------------------------


async def test_cloak_missing_package_gives_actionable_error(monkeypatch):
    # Ensure `import cloakbrowser` fails even if it happens to be installed.
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)
    with pytest.raises(BrowserError, match="uv add cloakbrowser"):
        await acquire(pw=None, pool={}, cfg=cfg(provider="cloak"))


async def test_cloak_dispatches_to_launch_async_and_is_owned(monkeypatch):
    seen = {}

    async def fake_launch_async(**kwargs):
        seen.update(kwargs)
        return "FAKE_BROWSER"

    fake_mod = types.ModuleType("cloakbrowser")
    fake_mod.launch_async = fake_launch_async
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake_mod)

    acq = await acquire(
        pw=None, pool={}, cfg=cfg(provider="cloak", cloak={"humanize": True})
    )
    assert acq.browser == "FAKE_BROWSER"
    assert acq.owned is True  # a per-run process, closed with the run
    # launch-layer options the engine must not re-emulate on the context
    assert acq.launch_level == frozenset({"proxy", "locale", "timezone"})
    assert seen == {"headless": True, "humanize": True}


async def test_cdp_attaches_and_is_owned(monkeypatch):
    captured = {}

    async def fake_connect(url):
        captured["url"] = url
        return "CDP_BROWSER"

    pw = types.SimpleNamespace(chromium=types.SimpleNamespace(connect_over_cdp=fake_connect))
    acq = await acquire(pw=pw, pool={}, cfg=cfg(provider="cdp", cdp_url="http://localhost:9222"))
    assert acq.browser == "CDP_BROWSER"
    assert acq.owned is True
    assert acq.launch_level == frozenset({"proxy"})  # proxy lives on the remote server
    assert captured["url"] == "http://localhost:9222"


async def test_cdp_connection_failure_is_wrapped(monkeypatch):
    async def boom(url):
        raise RuntimeError("connection refused")

    pw = types.SimpleNamespace(chromium=types.SimpleNamespace(connect_over_cdp=boom))
    with pytest.raises(BrowserError, match="could not connect to CDP endpoint"):
        await acquire(pw=pw, pool={}, cfg=cfg(provider="cdp", cdp_url="http://localhost:9999"))


async def test_playwright_provider_pools_and_is_not_owned(monkeypatch):
    launches = []

    class FakeBrowser:
        def is_connected(self):
            return True

    async def fake_launch(**kw):
        launches.append(kw)
        return FakeBrowser()

    pw = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=fake_launch))
    pool: dict = {}
    a1 = await acquire(pw=pw, pool=pool, cfg=cfg())
    a2 = await acquire(pw=pw, pool=pool, cfg=cfg())
    assert a1.owned is False
    assert a1.browser is a2.browser  # reused from the pool
    assert len(launches) == 1  # launched once
