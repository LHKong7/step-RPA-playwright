"""The browser block resolves {{ env.X }} / {{ vars.x }} before launch."""

from pwflow.engine import _render_browser_config
from pwflow.models import BrowserConfig


def test_env_and_vars_resolve_in_browser_block(monkeypatch):
    monkeypatch.setenv("PROXY_PASS", "s3cret")
    monkeypatch.setenv("CB_KEY", "cb_live_123")
    cfg = BrowserConfig.model_validate(
        {
            "provider": "cloak",
            "proxy": {
                "server": "http://gw:8080",
                "username": "u",
                "password": "{{ env.PROXY_PASS }}",
            },
            "timezone": "{{ vars.tz }}",
            "cloak": {"license_key": "{{ env.CB_KEY }}"},
        }
    )
    out = _render_browser_config(cfg, {"tz": "Europe/London"})
    assert out.proxy.password == "s3cret"  # secret came from the environment, not the YAML
    assert out.timezone == "Europe/London"
    assert out.cloak.license_key == "cb_live_123"


def test_plain_config_is_untouched():
    cfg = BrowserConfig(headless=False, timezone="UTC")
    out = _render_browser_config(cfg, {})
    assert out.headless is False and out.timezone == "UTC"
