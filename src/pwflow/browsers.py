"""How a run gets a browser: the three providers.

The engine does not care *where* a ``Browser`` comes from — pooled Playwright, a
freshly-launched CloakBrowser, or a CDP attach. It only needs to know two things this
module answers per acquisition:

* ``owned`` — should the run close this browser when it finishes? Pooled browsers are
  shared and outlive the run; a CloakBrowser process or a CDP connection belongs to the
  one run and is closed with it.
* ``launch_level`` — which context options were already applied at launch/connect time,
  so the engine must *not* set them again on the ``BrowserContext``. For CloakBrowser,
  proxy/locale/timezone are compiled into the binary's behaviour; re-applying them as
  CDP emulation is exactly the fingerprint tell the stealth binary exists to avoid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.async_api import Browser, Playwright

from .errors import BrowserError
from .models import BrowserConfig, Proxy

log = logging.getLogger("pwflow")


@dataclass
class Acquired:
    browser: Browser
    owned: bool  # close the browser when the run ends?
    launch_level: frozenset[str] = field(default_factory=frozenset)  # context opts to skip


async def acquire(pw: Playwright, pool: dict[tuple, Browser], cfg: BrowserConfig) -> Acquired:
    if cfg.provider == "playwright":
        return await _playwright(pw, pool, cfg)
    if cfg.provider == "cloak":
        return await _cloak(cfg)
    if cfg.provider == "cdp":
        return await _cdp(pw, cfg)
    raise BrowserError(f"unknown provider {cfg.provider!r}")  # pragma: no cover


async def _playwright(pw: Playwright, pool: dict[tuple, Browser], cfg: BrowserConfig) -> Acquired:
    """A stock browser, launched once per (engine, headless, slow_mo) and reused."""
    key = (cfg.engine, cfg.headless, cfg.slow_mo)
    browser = pool.get(key)
    if browser is None or not browser.is_connected():
        launcher = getattr(pw, cfg.engine)
        browser = pool[key] = await launcher.launch(headless=cfg.headless, slow_mo=cfg.slow_mo)
        log.debug("launched %s (headless=%s)", cfg.engine, cfg.headless)
    return Acquired(browser, owned=False)


async def _cloak(cfg: BrowserConfig) -> Acquired:
    """A CloakBrowser stealth binary — a fresh process (and fingerprint seed) per run."""
    try:
        from cloakbrowser import launch_async
    except ImportError as e:
        raise BrowserError(
            "provider: cloak needs the cloakbrowser package — `uv add cloakbrowser` "
            "(or `pip install cloakbrowser`). The stealth Chromium binary (~200MB) "
            "downloads on first launch. See https://github.com/CloakHQ/cloakbrowser"
        ) from e

    kwargs = _cloak_launch_kwargs(cfg)
    log.debug("launching cloakbrowser: %s", {k: v for k, v in kwargs.items() if k != "proxy"})
    browser = await launch_async(**kwargs)
    # proxy/locale/timezone are baked into the binary at launch; the context must not
    # re-emulate them (that emulation is the detectable seam CloakBrowser removes).
    return Acquired(browser, owned=True, launch_level=frozenset({"proxy", "locale", "timezone"}))


def _cloak_launch_kwargs(cfg: BrowserConfig) -> dict:
    c = cfg.cloak
    kwargs: dict = {"headless": cfg.headless}
    if cfg.proxy is not None:
        kwargs["proxy"] = _proxy_arg(cfg.proxy)
    if cfg.locale:
        kwargs["locale"] = cfg.locale
    if cfg.timezone:
        kwargs["timezone"] = cfg.timezone
    if c.humanize:
        kwargs["humanize"] = True
    if c.human_preset:
        kwargs["human_preset"] = c.human_preset
    if c.geoip:
        kwargs["geoip"] = True
    if c.license_key:
        kwargs["license_key"] = c.license_key
    if not c.stealth_args:
        kwargs["stealth_args"] = False
    if c.extension_paths:
        kwargs["extension_paths"] = c.extension_paths
    if c.args:
        kwargs["args"] = c.args
    return kwargs


def _proxy_arg(proxy: Proxy) -> str | dict:
    """CloakBrowser takes a bare URL, or a dict when there is auth or a bypass list."""
    if proxy.username or proxy.password or proxy.bypass:
        return proxy.model_dump(exclude_none=True)
    return proxy.server


async def _cdp(pw: Playwright, cfg: BrowserConfig) -> Acquired:
    """Attach to a browser already running behind a CDP endpoint (e.g. cloakserve)."""
    assert cfg.cdp_url is not None  # the model validator guarantees this
    try:
        browser = await pw.chromium.connect_over_cdp(cfg.cdp_url)
    except Exception as e:  # noqa: BLE001 - surface a clear message, not a raw CDP error
        raise BrowserError(f"could not connect to CDP endpoint {cfg.cdp_url!r}: {e}") from e
    # The proxy is configured on the remote server (cloakserve --proxy-server=...), not
    # by the attaching client, so the context must not try to set one.
    return Acquired(browser, owned=True, launch_level=frozenset({"proxy"}))
