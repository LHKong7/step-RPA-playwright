"""``pwflow`` command line."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from .engine import Engine
from .errors import PwFlowError
from .loader import dump_schema, load_flow
from .observability import configure_logging
from .registry import canonical

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run Playwright automation from declarative YAML flows.",
)
console = Console()


def _log_format(explicit: str | None) -> str:
    """`--log-format`, else `$PWFLOW_LOG_FORMAT`, else console."""
    return explicit or os.environ.get("PWFLOW_LOG_FORMAT", "console")


def _setup_logging(verbose: bool, fmt: str = "console") -> None:
    configure_logging(
        level=logging.DEBUG if verbose else logging.INFO,
        fmt=fmt,
        console=console,
    )


def _parse_vars(pairs: list[str]) -> dict[str, Any]:
    """``--var pages=3`` — values are parsed as JSON when possible, else kept as strings."""
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"--var expects key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


@app.command()
def run(
    flow_file: Annotated[Path, typer.Argument(help="Path to a flow YAML file.")],
    var: Annotated[
        list[str], typer.Option("--var", "-v", help="Override a flow var: k=v")
    ] = [],  # noqa: B006 - Typer's idiom for a repeatable option
    headed: Annotated[bool, typer.Option(help="Show the browser window.")] = False,
    out: Annotated[Path | None, typer.Option(help="Write the run result JSON here.")] = None,
    trace: Annotated[bool, typer.Option(help="Record a Playwright trace.zip.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-V")] = False,
    log_format: Annotated[
        str | None,
        typer.Option("--log-format", help="console (default) or json for structured logs."),
    ] = None,
) -> None:
    """Run a flow."""
    _setup_logging(verbose, _log_format(log_format))
    try:
        flow = load_flow(flow_file)
    except PwFlowError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from e

    if headed:
        flow.browser.headless = False
    if trace:
        flow.browser.trace = True

    async def _go():
        async with Engine() as engine:
            return await engine.run(flow, vars=_parse_vars(var))

    result = asyncio.run(_go())
    _print_report(result)

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
        console.print(f"result -> [cyan]{out}[/cyan]")

    raise typer.Exit(0 if result.status == "success" else 1)


def _print_report(result) -> None:  # noqa: ANN001
    marks = {"ok": "[green]✓[/green]", "skipped": "[dim]·[/dim]",
             "recovered": "[yellow]↻[/yellow]", "failed": "[red]✗[/red]"}
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("", no_wrap=True)  # mark
    table.add_column("", style="dim", no_wrap=True)  # dotted index
    table.add_column("")  # action
    table.add_column("", style="dim")  # label
    table.add_column("", justify="right", style="dim", no_wrap=True)  # duration
    for s in result.steps:
        table.add_row(
            marks.get(s.status, "?"),
            s.index,
            s.action,
            s.label if s.label != s.action else "",
            f"{s.duration_ms}ms",
        )
    console.print(table)

    counts = {k: sum(1 for s in result.steps if s.status == k) for k in
              ("ok", "recovered", "skipped", "failed")}
    for key, value in result.data.items():
        size = f"{len(value)} records" if isinstance(value, list) else repr(value)
        console.print(f"  [cyan]data.{key}[/cyan]: {size}")

    if result.status == "success":
        console.print(
            f"\n[green]✓ {result.flow}[/green] in {result.duration_ms}ms "
            f"({counts['ok'] + counts['recovered']} ok, {counts['skipped']} skipped)"
        )
    else:
        console.print(f"\n[red]✗ {result.flow} failed[/red]: {result.error}")


@app.command()
def validate(
    flow_file: Annotated[Path, typer.Argument(help="Path to a flow YAML file.")],
) -> None:
    """Check a flow without opening a browser."""
    try:
        flow = load_flow(flow_file)
    except PwFlowError as e:
        console.print(f"[red]✗ {flow_file}[/red]\n{e}")
        raise typer.Exit(1) from e
    console.print(f"[green]✓[/green] {flow.name}: {len(flow.steps)} top-level steps, valid")


@app.command("actions")
def list_actions() -> None:
    """List every action the DSL understands."""
    table = Table("action", "params", "what it does", box=None)
    for spec in canonical():
        # models spell reserved words `in_` / `else_`; show the YAML key instead
        fields = ", ".join(
            (f.alias or n) for n, f in spec.model.model_fields.items()
        ) or "—"
        name = f"[cyan]{spec.name}[/cyan]"
        if spec.aliases:
            name += f" [dim]({'/'.join(spec.aliases)})[/dim]"
        table.add_row(name, f"[dim]{fields}[/dim]", spec.doc)
    console.print(table)


@app.command()
def schema(
    out: Annotated[Path | None, typer.Option(help="Write JSON Schema here.")] = None,
) -> None:
    """Dump the DSL as JSON Schema (point your editor at it for YAML autocompletion)."""
    text = json.dumps(dump_schema(), indent=2)
    if out:
        out.write_text(text)
        console.print(f"[green]✓[/green] {out}")
    else:
        console.print_json(text)


@app.command()
def serve(
    flows_dir: Annotated[Path, typer.Option(help="Directory of named flow YAMLs.")] = Path("flows"),
    host: str = "127.0.0.1",
    port: int = 8000,
    concurrency: Annotated[int, typer.Option(help="Max runs executing at once.")] = 4,
    state_dir: Annotated[
        Path, typer.Option(help="Where run records are persisted.")
    ] = Path(".pwflow"),
    loop: Annotated[
        str, typer.Option(help="Event loop: asyncio (safe for cloak) or uvloop (faster).")
    ] = "asyncio",
    log_format: Annotated[
        str | None,
        typer.Option("--log-format", help="console (default) or json for structured logs."),
    ] = None,
) -> None:
    """Serve the HTTP API.

    The loop defaults to asyncio, not uvloop: CloakBrowser's subprocess pipes hang under
    uvloop. If no flow uses `provider: cloak`, `--loop uvloop` is a small speedup.

    Metrics are exposed at ``GET /metrics`` in Prometheus text format. `--log-format json`
    emits one structured JSON line per log record, each tagged with its ``run_id``.
    """
    import uvicorn

    from .server.app import create_app

    _setup_logging(False, _log_format(log_format))
    uvicorn.run(
        create_app(flows_dir=flows_dir, concurrency=concurrency, state_dir=state_dir),
        host=host,
        port=port,
        loop=loop,
    )


if __name__ == "__main__":
    app()
