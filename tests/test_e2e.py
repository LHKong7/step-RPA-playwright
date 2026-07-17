"""End-to-end: a real Chromium against local fixture pages."""

import json

import pytest

from pwflow import Engine
from pwflow.loader import load_flow

# Written as a flow-style mapping so it can be dropped into any YAML at any indent.
FIELDS = (
    '{sku: {type: attr, attr: data-sku}, name: ".name", '
    'url: {selector: ".name", type: link}, '
    'price: {selector: ".price", cast: float}, '
    'stock: {selector: ".stock", cast: int}, '
    'tags: {selector: ".tag", many: true}}'
)


@pytest.fixture
async def engine():
    async with Engine() as e:
        yield e


async def test_extract_records(engine, page1):
    flow = load_flow(
        f"""
        name: catalogue
        steps:
          - goto: "{{{{ vars.url }}}}"
          - extract:
              name: items
              selector: ".item"
              list: true
              fields: {FIELDS}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error

    items = result.data["items"]
    assert len(items) == 3
    assert items[0] == {
        "sku": "A-1",
        "name": "Blue widget",
        "url": "file:///widgets/1",  # relative href resolved against the page URL
        "price": 12.5,
        "stock": 3,
        "tags": ["blue", "small"],
    }
    assert items[1]["price"] == 1240.0  # "$1,240.00" survives the comma
    assert items[1]["stock"] == 0  # "0 in stock" is 0, not None
    assert items[2]["tags"] == []  # no tags, but the record is still kept


async def test_pagination_with_break(engine, page1):
    """The idiom for `while there is a next page`: repeat + a guarded break."""
    flow = load_flow(
        f"""
        name: paginate
        steps:
          - goto: "{{{{ vars.url }}}}"
          - repeat:
              times: 10
              as: page_no
              steps:
                - extract:
                    name: items
                    selector: ".item"
                    list: true
                    append: true
                    fields: {FIELDS}
                - extract: {{name: has_next, selector: "a.next", type: exists}}
                - break:
                  when: "{{{{ not data.has_next }}}}"
                - click: "a.next"
                - wait_for_load: domcontentloaded
          - assert: "{{{{ data.items | length == 5 }}}}"
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    assert [i["sku"] for i in result.data["items"]] == ["A-1", "A-2", "A-3", "B-1", "B-2"]


async def test_form_interaction(engine, form):
    flow = load_flow(
        """
        name: signin
        steps:
          - goto: "{{ vars.url }}"
          - fill: {selector: {placeholder: username}, value: "{{ vars.user }}"}
          - fill: {selector: "#pass", value: "hunter2"}
          - select: {selector: "#role", label: Admin}
          - check: "#tos"
          - click: {role: button, name: "Sign in"}
          - extract: {name: greeting, selector: "#result"}
          - assert: {selector: "#result", text: "welcome"}
        """
    )
    result = await engine.run(flow, vars={"url": form, "user": "ada"})
    assert result.status == "success", result.error
    assert result.data["greeting"] == "welcome ada (admin)"


async def test_failing_assertion_fails_the_run(engine, page1):
    flow = load_flow(
        """
        name: guard
        steps:
          - goto: "{{ vars.url }}"
          - assert: {selector: ".item", min_count: 99, message: "too few rows"}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "failed"
    assert "too few rows" in result.error


async def test_assertion_is_not_swallowed_by_optional(engine, page1):
    """`optional: true` covers flaky clicks. It must not cover a broken guarantee."""
    flow = load_flow(
        """
        name: guard
        steps:
          - goto: "{{ vars.url }}"
          - assert: {expr: "{{ 1 == 2 }}"}
            optional: true
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "failed"


async def test_optional_step_and_retry(engine, page1):
    flow = load_flow(
        """
        name: resilient
        steps:
          - goto: "{{ vars.url }}"
          - click: "#does-not-exist"
            optional: true
            timeout: 300
            retry: {times: 1, delay: 10}
          - extract: {name: heading, selector: "#heading"}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    assert result.data["heading"] == "Widget catalogue"
    failed = [s for s in result.steps if s.status == "failed"]
    assert len(failed) == 1 and failed[0].attempts == 2  # tried twice, then let go


async def test_when_skips_and_try_catches(engine, page1):
    flow = load_flow(
        """
        name: control
        steps:
          - goto: "{{ vars.url }}"
          - log: "never runs"
            when: "{{ false }}"
          - try:
              steps:
                - click: "#nope"
                  timeout: 300
              catch:
                - set: {recovered: true}
          - assert: "{{ vars.recovered }}"
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    assert [s.status for s in result.steps if s.action == "log"] == ["skipped"]


async def test_foreach_over_extracted_data(engine, page1):
    flow = load_flow(
        """
        name: fanout
        steps:
          - goto: "{{ vars.url }}"
          - extract:
              name: names
              selector: ".name"
              list: true
          - foreach:
              in: "{{ data.names }}"
              as: n
              steps:
                - log: "{{ index }}: {{ n }}"
                - set: {last_seen: "{{ n }}"}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    assert result.data["names"] == ["Blue widget", "Red widget", "Green widget"]


async def test_output_is_written(engine, page1, tmp_path):
    out = tmp_path / "items.csv"
    flow = load_flow(
        f"""
        name: export
        steps:
          - goto: "{{{{ vars.url }}}}"
          - extract:
              name: items
              selector: ".item"
              list: true
              fields:
                sku: {{type: attr, attr: data-sku}}
                name: ".name"
        output:
          path: "{out}"
          format: csv
          key: items
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    lines = out.read_text().strip().splitlines()
    assert lines[0] == "sku,name"
    assert lines[1] == "A-1,Blue widget"


async def test_output_shape_builds_a_custom_structure(engine, page1, tmp_path):
    out = tmp_path / "report.json"
    flow = load_flow(
        f"""
        name: shaped
        vars: {{source: catalogue}}
        steps:
          - goto: "{{{{ vars.url }}}}"
          - extract:
              name: items
              selector: ".item"
              list: true
              fields:
                sku: {{type: attr, attr: data-sku}}
                name: ".name"
        output:
          path: "{out}"
          shape:
            flow: "{{{{ flow.name }}}}"
            source: "{{{{ vars.source }}}}"
            count: "{{{{ data.items | length }}}}"
            names: "{{{{ data.items | map(attribute='name') | list }}}}"
            payload:
              items: "{{{{ data.items }}}}"
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error

    report = json.loads(out.read_text())
    assert report["flow"] == "shaped"
    assert report["source"] == "catalogue"
    assert report["count"] == 3  # native int, not the string "3"
    assert report["names"][0] == "Blue widget"
    assert report["payload"]["items"][0]["sku"] == "A-1"
    # the custom shape replaces the raw dump: no bare top-level `items`
    assert "items" not in report


async def test_output_key_and_shape_are_mutually_exclusive():
    from pwflow.errors import FlowLoadError

    with pytest.raises(FlowLoadError, match="not both"):
        load_flow(
            """
            name: bad
            steps:
              - goto: "about:blank"
            output:
              path: out.json
              key: items
              shape: {n: "{{ 1 }}"}
            """
        )


async def test_on_failure_runs_and_captures_a_screenshot(engine, page1, tmp_path):
    flow = load_flow(
        f"""
        name: diagnose
        steps:
          - goto: "{{{{ vars.url }}}}"
          - click: "#missing"
            timeout: 300
        on_failure:
          - screenshot: crash.png
          - log: "died: {{{{ error }}}}"
        output:
          artifacts_dir: "{tmp_path}"
        """
    )
    result = await engine.run(flow, vars={"url": page1}, artifacts_dir=tmp_path)
    assert result.status == "failed"
    assert (tmp_path / "crash.png").exists()
    assert str(tmp_path / "crash.png") in result.artifacts


async def test_evaluate_is_the_fast_path(engine, page1):
    flow = load_flow(
        """
        name: js
        steps:
          - goto: "{{ vars.url }}"
          - js:
              name: items
              script: |
                () => [...document.querySelectorAll('.item')].map(el => ({
                  sku: el.dataset.sku,
                  name: el.querySelector('.name').textContent,
                }))
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    assert len(result.data["items"]) == 3
    assert result.data["items"][0]["sku"] == "A-1"


async def test_run_result_serializes(engine, page1):
    flow = load_flow('name: t\nsteps:\n  - goto: "{{ vars.url }}"\n')
    result = await engine.run(flow, vars={"url": page1})
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["status"] == "success"
    assert payload["steps"][0]["action"] == "goto"


# -- P1/P2 additions -------------------------------------------------------


async def test_run_level_timeout_fails_and_runs_on_failure(engine, page1, tmp_path):
    marker = tmp_path / "on_failure_ran.json"
    flow = load_flow(
        f"""
        name: slow
        limits: {{max_duration: 1}}
        steps:
          - goto: "{{{{ vars.url }}}}"
          - sleep: 5000
        on_failure:
          - save: {{path: "{marker}", data: {{ran: true}}}}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "failed"
    assert "max_duration" in result.error
    assert marker.exists()  # teardown/on_failure still ran after the timeout cancel


async def test_try_does_not_swallow_a_failed_assert(engine, page1):
    flow = load_flow(
        """
        name: sacred
        steps:
          - goto: "{{ vars.url }}"
          - try:
              steps:
                - assert: {expr: "{{ 1 == 2 }}"}
              catch:
                - set: {caught: true}
          - set: {reached_end: true}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "failed"  # assert propagated through try/catch
    assert "caught" not in result.data and "reached_end" not in result.data


async def test_teardown_failure_surfaces_as_a_warning_not_a_lost_run(engine, page1, tmp_path):
    a_file = tmp_path / "afile"
    a_file.write_text("x")  # its "subdir" cannot be created -> save_storage_state fails
    flow = load_flow(
        f"""
        name: warn
        browser:
          save_storage_state: "{a_file / 'sub' / 'state.json'}"
        steps:
          - goto: "{{{{ vars.url }}}}"
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success"  # a failed side artifact does not fail the run
    assert any("save_storage_state" in w for w in result.warnings)


async def test_while_pagination(engine, page1):
    flow = load_flow(
        """
        name: paginate
        steps:
          - goto: "{{ vars.url }}"
          - extract: {name: has_next, selector: "a.next", type: exists}
          - while:
              cond: "{{ data.has_next }}"
              max: 5
              steps:
                - extract:
                    name: skus
                    selector: ".item"
                    list: true
                    append: true
                    fields: {sku: {type: attr, attr: data-sku}}
                - extract: {name: has_next, selector: "a.next", type: exists}
                - click: "a.next"
                  when: "{{ data.has_next }}"
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    assert [r["sku"] for r in result.data["skus"]] == ["A-1", "A-2", "A-3", "B-1", "B-2"]


async def test_save_action_writes_a_file(engine, page1, tmp_path):
    out = tmp_path / "saved.json"
    flow = load_flow(
        f"""
        name: save
        steps:
          - goto: "{{{{ vars.url }}}}"
          - extract:
              name: items
              selector: ".item"
              list: true
              fields: {{sku: {{type: attr, attr: data-sku}}}}
          - save: {{path: "{out}", data: "{{{{ data.items }}}}", format: json}}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    assert json.loads(out.read_text())[0]["sku"] == "A-1"
    assert str(out) in result.artifacts


async def test_save_action_builds_a_custom_structure(engine, page1, tmp_path):
    out = tmp_path / "wrapped.json"
    flow = load_flow(
        f"""
        name: save-shaped
        steps:
          - goto: "{{{{ vars.url }}}}"
          - extract:
              name: items
              selector: ".item"
              list: true
              fields: {{sku: {{type: attr, attr: data-sku}}}}
          - save:
              path: "{out}"
              data:
                total: "{{{{ data.items | length }}}}"
                skus: "{{{{ data.items | map(attribute='sku') | list }}}}"
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    saved = json.loads(out.read_text())
    assert saved["total"] == 3
    assert saved["skus"] == ["A-1", "A-2", "A-3"]


async def test_csv_serializes_list_cells_as_json(engine, page1, tmp_path):
    out = tmp_path / "items.csv"
    flow = load_flow(
        f"""
        name: csv
        steps:
          - goto: "{{{{ vars.url }}}}"
          - extract:
              name: items
              selector: ".item"
              list: true
              fields:
                sku: {{type: attr, attr: data-sku}}
                tags: {{selector: ".tag", many: true}}
        output: {{path: "{out}", format: csv, key: items}}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    row = out.read_text().splitlines()[1]  # A-1 has tags blue, small
    assert '"[""blue"", ""small""]"' in row  # a JSON array, CSV-quoted — not a python repr
    import csv as _csv

    parsed = list(_csv.DictReader(out.read_text().splitlines()))
    assert json.loads(parsed[0]["tags"]) == ["blue", "small"]


async def test_fast_path_and_locator_path_are_identical(engine, page1):
    """CSS list extract (fast path) vs the equivalent xpath (locator fallback) must match."""
    flow = load_flow(
        """
        name: identity
        steps:
          - goto: "{{ vars.url }}"
          - extract:
              name: css
              selector: ".item"
              list: true
              fields:
                sku:  {type: attr, attr: data-sku}
                name: ".name"
                price: {selector: ".price", cast: float}
                tags: {selector: ".tag", many: true}
          - extract:
              name: xp
              selector: "xpath=//li[contains(@class,'item')]"
              list: true
              fields:
                sku:  {type: attr, attr: data-sku}
                name: {selector: {xpath: ".//a[contains(@class,'name')]"}}
                price: {selector: {xpath: ".//span[contains(@class,'price')]"}, cast: float}
                tags: {selector: {xpath: ".//span[contains(@class,'tag')]"}, many: true}
        """
    )
    result = await engine.run(flow, vars={"url": page1})
    assert result.status == "success", result.error
    assert result.data["css"] == result.data["xp"]
    assert result.data["css"][0]["tags"] == ["blue", "small"]
