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
from jev_agent.cli.wiring import cli_context
from jev_agent.core.cache import Cache, SimHashEmbedder, decision_normalized_text

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
        help="Only clear entries created more than this many hours ago.",
    ),
]

_CALIBRATE_THRESHOLDS = (0.90, 0.95, 0.98, 0.99)
# Pairwise comparison is O(n²); cap the history we calibrate over.
_CALIBRATE_MAX_DECISIONS = 1500


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
        console.print('[red]error: --type must be "decisions" or "roles"[/red]')
        raise typer.Exit(1)
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
        decisions = []
        for run in ctx.store.list_runs():
            decisions.extend(ctx.store.list_decisions(run.id))
    # Only real dispatcher answers at the same decision point are comparable.
    by_point: dict[str, list] = {}
    for record in decisions:
        if record.decision.dispatcher == "rules":
            continue
        by_point.setdefault(record.decision.point, []).append(record)
    embedder = SimHashEmbedder()
    pairs = 0
    agree_at: dict[float, int] = {tau: 0 for tau in _CALIBRATE_THRESHOLDS}
    total_at: dict[float, int] = {tau: 0 for tau in _CALIBRATE_THRESHOLDS}
    for records in by_point.values():
        records = records[-_CALIBRATE_MAX_DECISIONS:]
        fingerprints = [
            (embedder.fingerprint(decision_normalized_text(r.request)), r.decision.answers)
            for r in records
        ]
        for i in range(len(fingerprints)):
            for j in range(i + 1, len(fingerprints)):
                sim = embedder.similarity(fingerprints[i][0], fingerprints[j][0])
                agree = fingerprints[i][1] == fingerprints[j][1]
                pairs += 1
                for tau in _CALIBRATE_THRESHOLDS:
                    if sim >= tau:
                        total_at[tau] += 1
                        if agree:
                            agree_at[tau] += 1
    table = Table(title="Semantic threshold calibration (decisions)")
    table.add_column("τ", justify="right")
    table.add_column("pairs ≥ τ", justify="right")
    table.add_column("P(same answer | sim ≥ τ)", justify="right")
    for tau in _CALIBRATE_THRESHOLDS:
        total = total_at[tau]
        prob = agree_at[tau] / total if total else float("nan")
        prob_text = f"{prob:.3f}" if total else "n/a"
        table.add_row(f"{tau:.2f}", str(total), prob_text)
    console.print(table)
    console.print(f"[dim]{pairs} comparable decision pairs across {len(by_point)} decision points.[/dim]")
    if pairs == 0:
        console.print("Not enough decision history to calibrate — run some runs first.")
        return
    best = max(
        (tau for tau in _CALIBRATE_THRESHOLDS if total_at[tau] and agree_at[tau] / total_at[tau] >= 0.99),
        default=None,
    )
    if best is None:
        console.print(
            "No threshold reaches ≥ 0.99 agreement: keep semantic serving off and rely on exact match."
        )
    else:
        console.print(
            f"Candidate: [green]{best:.2f}[/green] — set `[cache.semantic] decision_threshold`, "
            "then run in shadow mode to validate it live before serving."
        )
