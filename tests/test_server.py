"""The HTTP surface, driven in-process against the real engine."""

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from pwflow.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def flows_dir(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    (d / "catalogue.yaml").write_text(
        """
name: catalogue
description: Scrape the widget list
vars:
  url: ""
steps:
  - goto: "{{ vars.url }}"
  - extract:
      name: items
      selector: ".item"
      list: true
      fields:
        sku: {type: attr, attr: data-sku}
        name: ".name"
"""
    )
    (d / "broken.yaml").write_text("name: broken\nsteps:\n  - clcik: .x\n")
    return d


@pytest.fixture
async def client(flows_dir: Path):
    app = create_app(flows_dir=flows_dir, concurrency=2)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):  # starts/stops the browser pool
            yield c


@pytest.fixture
def page1_url() -> str:
    return (FIXTURES / "page1.html").as_uri()


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_list_flows_reports_validity(client):
    flows = {f["name"]: f for f in (await client.get("/flows")).json()}
    assert flows["catalogue"]["valid"] is True
    assert flows["catalogue"]["description"] == "Scrape the widget list"
    assert flows["broken"]["valid"] is False
    assert "unknown action `clcik`" in flows["broken"]["error"]


async def test_run_named_flow_and_wait(client, page1_url):
    r = await client.post("/runs", json={"flow": "catalogue", "vars": {"url": page1_url}})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert len(body["data"]["items"]) == 3
    assert body["data"]["items"][0]["sku"] == "A-1"
    assert body["duration_ms"] > 0


async def test_run_inline_yaml(client, page1_url):
    yaml = f"""
    name: inline
    steps:
      - goto: "{page1_url}"
      - extract: {{name: heading, selector: "#heading"}}
    """
    r = await client.post("/runs", json={"yaml": yaml})
    assert r.status_code == 200
    assert r.json()["data"]["heading"] == "Widget catalogue"


async def test_background_run_is_pollable(client, page1_url):
    r = await client.post(
        "/runs", json={"flow": "catalogue", "vars": {"url": page1_url}, "wait": False}
    )
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "queued"

    for _ in range(60):
        body = (await client.get(f"/runs/{run_id}")).json()
        if body["status"] in ("success", "failed"):
            break
        await asyncio.sleep(0.25)

    assert body["status"] == "success", body.get("error")
    assert len(body["data"]["items"]) == 3


async def test_a_broken_flow_is_422_not_500(client):
    r = await client.post("/runs", json={"flow": "broken"})
    assert r.status_code == 422
    assert "unknown action" in r.json()["detail"]


async def test_flow_and_yaml_are_mutually_exclusive(client):
    r = await client.post("/runs", json={"flow": "catalogue", "yaml": "name: x\nsteps: []"})
    assert r.status_code == 422


async def test_flow_name_cannot_escape_the_flows_dir(client):
    r = await client.post("/runs", json={"flow": "../../../etc/passwd"})
    assert r.status_code == 400


async def test_unknown_run_is_404(client):
    assert (await client.get("/runs/nope")).status_code == 404


async def test_validate_does_not_run_anything(client):
    r = await client.post("/validate", json={"flow": "catalogue"})
    assert r.json() == {"valid": True, "name": "catalogue", "steps": 2}
    assert (await client.get("/runs")).json() == []


async def test_actions_endpoint_exposes_schemas(client):
    actions = (await client.get("/actions")).json()
    assert "extract" in actions and "if" in actions
    assert actions["if"]["control"] is True
    assert actions["assert"]["aliases"] == ["expect"]
    assert "selector" in actions["click"]["params"]["properties"]
