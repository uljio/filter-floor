"""Filter Floor CLI. Verdicts: AVOID, CAUTION, PASS_FILTER. No BUY language."""

from __future__ import annotations

import sys

import typer
from dotenv import load_dotenv

from filter_floor.explain import explain_scan
from filter_floor.labeling import DEFAULT_LABEL_LIMIT, label_due
from filter_floor.models import Chain
from filter_floor.pipeline import data_dir_from_env, run_scan
from filter_floor.storage.cases import read_case_md, read_scan
from filter_floor.storage.funnel import compute_and_write
from filter_floor.storage.memory import MemoryStore

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Filter Floor — local pre-buy filter. Verdicts: AVOID | CAUTION | PASS_FILTER.",
)

memory_app = typer.Typer(
    no_args_is_help=True,
    help="Local deployer/cluster memory. History is never deleted without --i-understand.",
)
app.add_typer(memory_app, name="memory")

STUB_MESSAGE = "not in this milestone"


@app.callback()
def _root() -> None:
    load_dotenv()


@app.command()
def scan(
    chain: Chain = typer.Option(..., "--chain", help="solana | base | robinhood"),
    token: str = typer.Option(..., "--token", help="mint or token address"),
) -> None:
    """Run Layer A + Layer B graph, update memory, and write scan JSON + case markdown."""
    result = run_scan(chain, token, data_dir_from_env())
    typer.echo(
        f"{result.case_id} {result.chain.value} {result.token} "
        f"{result.score_0_100} {result.verdict.value}"
    )


@app.command()
def show(case_id: str = typer.Argument(..., help="case id from ff scan")) -> None:
    """Print a stored case (markdown + verdict line)."""
    data_dir = data_dir_from_env()
    try:
        result = read_scan(case_id, data_dir)
        md = read_case_md(case_id, data_dir)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(md)
    typer.echo(
        f"{result.case_id} {result.chain.value} {result.token} "
        f"{result.score_0_100} {result.verdict.value}"
    )


@memory_app.command("reset")
def memory_reset(
    i_understand: bool = typer.Option(
        False,
        "--i-understand",
        help="Required. Permanently delete local deployer/cluster JSON.",
    ),
) -> None:
    """Delete data/memory/deployers and data/memory/clusters. Refuses without --i-understand."""
    if not i_understand:
        typer.echo("refusing to delete memory without --i-understand")
        raise typer.Exit(code=1)
    removed = MemoryStore(data_dir_from_env()).reset()
    typer.echo(f"deleted {removed} memory files")


@app.command()
def watch(
    chain: Chain = typer.Option(..., "--chain"),
    min_score_alert: int = typer.Option(50, "--min-score-alert"),
    once: bool = typer.Option(
        False,
        "--once",
        help="Single poll pass then exit.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Solana: Pump.fun program signatures per poll. Default 20.",
    ),
) -> None:
    """Watch new launches. Solana polls Pump.fun; Base polls Uniswap V3 PoolCreated."""
    if chain is Chain.robinhood:
        typer.echo(
            "Robinhood watch disabled until Pons factory is filled and ROBINHOOD_ENABLED=1"
        )
        raise typer.Exit(code=1)
    if limit < 1:
        typer.echo("limit must be >= 1", err=True)
        raise typer.Exit(code=1)
    if chain is Chain.base:
        from filter_floor.listeners.evm_ws import start_watch as start_base_watch

        start_base_watch(min_score_alert=min_score_alert, once=once)
        return
    from filter_floor.listeners.solana_ws import start_watch

    start_watch(min_score_alert=min_score_alert, once=once, limit=limit)


def _echo(line: str) -> None:
    typer.echo(line)
    try:
        sys.stdout.flush()
    except Exception:
        pass


@app.command()
def label(
    due: bool = typer.Option(False, "--due", help="label outcomes that are due"),
    limit: int = typer.Option(
        DEFAULT_LABEL_LIMIT,
        "--limit",
        help="Max horizons to label this run. Default 20.",
    ),
) -> None:
    """Label due 1h/6h/24h outcomes from DexScreener or native quotes. Else unknown."""
    if not due:
        typer.echo("label requires --due")
        raise typer.Exit(code=1)
    if limit < 1:
        typer.echo("limit must be >= 1", err=True)
        raise typer.Exit(code=1)
    labeled = label_due(
        data_dir_from_env(),
        limit=limit,
        progress=_echo,
    )
    _echo(f"labeled {len(labeled)} due horizon(s)")


@app.command()
def funnel(
    days: int = typer.Option(7, "--days"),
) -> None:
    """Count scanned → vetoed → caution → pass → later rugged; append data/funnel.jsonl."""
    if days < 1:
        typer.echo("days must be >= 1")
        raise typer.Exit(code=1)
    counts = compute_and_write(data_dir_from_env(), days=days)
    typer.echo(counts.one_line())


@app.command()
def explain(
    case_id: str = typer.Argument(..., help="case id from ff scan"),
) -> None:
    """Optional LLM explainer. Reads data/scans/{case_id}.json. Never changes the verdict."""
    data_dir = data_dir_from_env()
    try:
        result = read_scan(case_id, data_dir)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    outcome = explain_scan(result)
    if outcome.skipped:
        typer.echo(outcome.reason)
        return
    typer.echo(outcome.prose)
    typer.echo(f"verdict {outcome.verdict.value}")


@app.command("api")
def api_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(3001, "--port"),
) -> None:
    """Run FastAPI on 127.0.0.1:3001 only. No auth because it binds localhost."""
    bind = host.strip().lower()
    if bind not in {"127.0.0.1", "localhost"}:
        typer.echo("API binds 127.0.0.1 only; refusing non-localhost host")
        raise typer.Exit(code=1)
    import uvicorn

    from filter_floor.api import HOST, app as fastapi_app

    uvicorn.run(fastapi_app, host=HOST, port=port)


if __name__ == "__main__":
    app()
