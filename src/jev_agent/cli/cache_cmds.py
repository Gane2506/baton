"""`jev cache ...`: inspect and manage the decision/role cache (issue #10).

- `jev cache stats`: entries, hits, dollars saved, shadow-mode agreement.
- `jev cache clear [--type decisions|roles] [--older-than HOURS]`: flush entries.
- `jev cache calibrate`: pick similarity thresholds from stored decision history
  (phase-3 gate, issue #10 §2.2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import cli_context, fail
from jev_agent.core.cache import Cache, calibrate_thresholds

console = Console()

cache_app = typer.Typer(
    name="cache",
    help="Inspect and manage the decision/role cache (issue #10).",
    no_args_is_help=True,
)

TypeOption = Annotated[
    str | None,
    typer.Option(
        "--type",
        help='Only clear this kind: "decisions" or "roles".',
    ),
]
OlderThanOption = Annotated[
    float | None,
    typer.Option(
        "--older-than",
        metavar="HOURS",
        min=0.0,
        help="Only clear entries created more than this many hours ago.",
    ),
]


@cache_app.command("stats")
def cache_stats(repo: RepoOption = Path(".")) -> None:
    """Show cache entries, hits, dollars saved, and shadow-mode agreement."""
    with cli_context(repo) as ctx:
        cache = Cache(ctx.store, ctx.config.cache, ctx.root)
        stats = cache.stats()
    table = Table(title="Decision/role cache", show_header=False)
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Enabled", "yes" if ctx.config.cache.enabled else "no")
    table.add_row("Entries", str(stats.entries))
    table.add_row("  decisions", str(stats.decisions))
    table.add_row("  roles", str(stats.roles))
    table.add_row("Hits served", str(stats.hits))
    table.add_row("Saved", f"${stats.saved_usd:.2f}")
    table.add_row("Size", f"{stats.size_bytes / 1024:.1f} KiB")
    if stats.shadow_agreement is not None:
        table.add_row("Shadow agreement", f"{stats.shadow_agreement:.3f}")
    else:
        table.add_row("Shadow agreement", "no measurements yet")
    console.print(table)


@cache_app.command("clear")
def cache_clear(
    repo: RepoOption = Path("."),
    kind: TypeOption = None,
    older_than: OlderThanOption = None,
) -> None:
    """Flush cache entries (all, or filtered by --type / --older-than)."""
    if kind is not None and kind not in ("decisions", "roles"):
        # The CLI error contract (wiring.py): `error:` on stderr, exit 1 — a
        # plain console.print here used to send it to stdout (issue #223).
        raise fail('--type must be "decisions" or "roles"')
    store_kind = {"decisions": "decision", "roles": "role"}.get(kind) if kind else None
    with cli_context(repo) as ctx:
        cache = Cache(ctx.store, ctx.config.cache, ctx.root)
        removed = cache.clear(kind=store_kind, older_than_hours=older_than)
    console.print(f"Cleared {removed} cache {'entry' if removed == 1 else 'entries'}.")


@cache_app.command("calibrate")
def cache_calibrate(repo: RepoOption = Path(".")) -> None:
    """Pick semantic thresholds from stored decision history (phase-3 gate).

    Reads settled decisions from the state DB, computes pairwise SimHash similarity
    over the normalized requests, and reports P(same answers | similarity >= τ).
    Ship a threshold only where the curve shows ≥ 0.99 agreement with a comfortable
    margin — then validate it again in shadow mode before serving anything.
    """
    with cli_context(repo) as ctx:
        result = calibrate_thresholds(ctx.store)
    table = Table(title="Semantic threshold calibration (decisions)")
    table.add_column("τ", justify="right")
    table.add_column("pairs ≥ τ", justify="right")
    table.add_column("P(same answer | sim ≥ τ)", justify="right")
    for row in result.rows:
        prob_text = f"{row.probability:.3f}" if row.probability is not None else "n/a"
        table.add_row(f"{row.tau:.2f}", str(row.pairs), prob_text)
    console.print(table)
    console.print(
        f"[dim]{result.pairs} comparable decision pairs across "
        f"{result.points} decision points.[/dim]"
    )
    if result.pairs == 0:
        console.print("Not enough decision history to calibrate — run some runs first.")
        return
    if result.best is None:
        console.print(
            "No threshold reaches ≥ 0.99 agreement: keep semantic serving off "
            "and rely on exact match."
        )
    else:
        console.print(
            f"Candidate: [green]{result.best:.2f}[/green] — set "
            "`[cache.semantic] decision_threshold`, "
            "then run in shadow mode to validate it live before serving."
        )
