"""The YAML DSL schema.

A flow is a list of steps. Every step is a mapping with exactly one *action key*
(``click``, ``extract``, ``foreach``, ...) plus any number of *modifier keys*
(``id``, ``when``, ``retry``, ...) that apply uniformly to all actions::

    - id: next
      click: "a.morelink"
      when: "{{ page_no < vars.pages }}"
      optional: true

Action payloads are kept unrendered (``Step.raw``) until execution: templates can
only be resolved once earlier steps have produced their values. The loader still
checks the payload's *keys* against the action's params model, so typos and
missing arguments surface at load time rather than three minutes into a run.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Keys allowed on any step alongside the single action key.
MODIFIER_KEYS = frozenset(
    {"id", "name", "desc", "description", "when", "timeout", "optional", "retry"}
)


class Strict(BaseModel):
    """Base model that rejects unknown keys — a typo in YAML should be an error."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------
# Selectors
# --------------------------------------------------------------------------


class SelectorSpec(Strict):
    """Structured selector. Exactly one *engine* key, plus optional refiners.

    A selector may also be written as a bare string, in which case it is passed
    straight to Playwright (``"css=.item"``, ``"text=Login"``, ``".btn"``).
    """

    # engines (pick one)
    css: str | None = None
    xpath: str | None = None
    text: str | None = None
    role: str | None = None
    label: str | None = None
    placeholder: str | None = None
    test_id: str | None = Field(default=None, alias="testid")
    alt: str | None = None
    title: str | None = None

    # refiners
    name: str | None = None  # accessible name, only meaningful with `role`
    exact: bool = False
    has_text: str | None = None
    has: Selector | None = None  # ancestor must contain this descendant
    within: Selector | None = None  # scope the search under this parent
    nth: int | None = None
    first: bool = False
    last: bool = False

    ENGINES: ClassVar[tuple[str, ...]] = (
        "css", "xpath", "text", "role", "label", "placeholder", "test_id", "alt", "title",
    )

    @model_validator(mode="after")
    def _one_engine(self) -> SelectorSpec:
        used = [e for e in self.ENGINES if getattr(self, e) is not None]
        if len(used) != 1:
            raise ValueError(
                f"selector needs exactly one of {list(self.ENGINES)}, got {used or 'none'}"
            )
        if self.name is not None and self.role is None:
            raise ValueError("`name` refines `role`; use `text`/`has_text` for content matching")
        return self


Selector = str | SelectorSpec


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class Retry(Strict):
    times: int = 2
    delay: int = 500  # ms before the first retry
    backoff: float = 2.0  # delay multiplier per attempt

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, v: Any) -> Any:
        return {"times": v} if isinstance(v, int) else v


class Step(Strict):
    action: str
    raw: Any = None  # unrendered action payload
    id: str | None = None
    name: str | None = None
    when: str | None = None  # skip the step unless this expression is truthy
    timeout: int | None = None  # ms, overrides browser.timeout for this step
    optional: bool = False  # a failure is logged and the run continues
    retry: Retry | None = None

    # populated by the loader once the action registry is consulted
    spec: Any = Field(default=None, exclude=True, repr=False)  # ActionSpec
    parsed: Any = Field(default=None, exclude=True, repr=False)  # load-time params model

    @model_validator(mode="before")
    @classmethod
    def _split_action_from_modifiers(cls, v: Any) -> Any:
        """Turn ``{click: ".btn", when: "..."}`` into ``{action: "click", raw: ".btn", ...}``."""
        if not isinstance(v, dict):
            raise ValueError(f"a step must be a mapping, got {type(v).__name__}")
        if "action" in v:  # already normalized (round-trip / programmatic use)
            return v

        action_keys = [k for k in v if k not in MODIFIER_KEYS]
        if len(action_keys) != 1:
            raise ValueError(
                f"a step needs exactly one action key, found {sorted(action_keys) or 'none'}. "
                f"Modifiers ({', '.join(sorted(MODIFIER_KEYS))}) do not count as actions."
            )
        key = action_keys[0]
        out = {k: v[k] for k in v if k in MODIFIER_KEYS}
        out["action"] = key
        out["raw"] = v[key]
        # `desc`/`description` are aliases for `name` when no name is given
        if not out.get("name"):
            out["name"] = out.pop("desc", None) or out.pop("description", None)
        out.pop("desc", None)
        out.pop("description", None)
        return out

    @property
    def label(self) -> str:
        return self.name or self.id or self.action


# --------------------------------------------------------------------------
# Flow-level configuration
# --------------------------------------------------------------------------


class Viewport(Strict):
    width: int = 1280
    height: int = 800


class Proxy(Strict):
    server: str
    username: str | None = None
    password: str | None = None
    bypass: str | None = None


Resource = Literal["image", "font", "stylesheet", "media", "script", "xhr", "fetch"]


class BrowserConfig(Strict):
    engine: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    slow_mo: int = 0
    viewport: Viewport = Field(default_factory=Viewport)
    user_agent: str | None = None
    locale: str | None = None
    timezone: str | None = None
    extra_http_headers: dict[str, str] = Field(default_factory=dict)
    ignore_https_errors: bool = False
    proxy: Proxy | None = None

    timeout: int = 30_000  # default per-action timeout (ms)
    navigation_timeout: int = 30_000

    # Scraping levers: skip bytes you will never look at.
    block_resources: list[Resource] = Field(default_factory=list)

    # Session reuse: load cookies/localStorage from a file, and/or persist them after the run.
    storage_state: str | None = None
    save_storage_state: str | None = None

    trace: bool = False  # write a Playwright trace.zip into the artifacts dir
    record_video: bool = False


class OutputConfig(Strict):
    path: str | None = None  # where to write collected data (templated)
    format: Literal["json", "jsonl", "csv"] = "json"
    key: str | None = None  # export only this key of `data` (default: the whole dict)
    artifacts_dir: str = "artifacts"  # screenshots, traces, videos


class Flow(Strict):
    name: str
    description: str | None = None
    vars: dict[str, Any] = Field(default_factory=dict)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    steps: list[Step]
    on_failure: list[Step] = Field(default_factory=list)  # cleanup / diagnostics


SelectorSpec.model_rebuild()
