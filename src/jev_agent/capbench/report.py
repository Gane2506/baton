"""The public scoreboard: `docs/benchmarks.md` and the shields.io badge JSON.

The scoreboard publishes the raw dimensions (pass rate, ship score, $, minutes),
not just the composite - a single number invites Goodhart (design §3). Until a
real run happens the page is an explicit template: nothing here is measured or
fabricated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jev_agent.capbench.models import (
    COST_FULL_USD,
    COST_ZERO_USD,
    TIME_FULL_MIN,
    TIME_ZERO_MIN,
    W_CORRECTNESS,
    W_COST,
    W_SHIP,
    W_TIME,
    SuiteResult,
    TaskResult,
    load_tasks,
)

DISAMBIGUATION = (
    "> **Capability benchmark** - fixed tasks run through Baton (plan → validate → "
    "build → review → quality gate), published as a public scoreboard. "
    "For the **KPI benchmarks** (regression detection over shipped PRs: cost per PR, "
    "wall time, first-pass rate), see [docs/ship.md](ship.md)."
)

UNEVALUATED_NOTICE = (
    "> **Status: unevaluated.** No benchmark run has been performed yet - this page "
    "is a template describing the suite and the rubric. Run `jev capbench run --all` "
    "to produce real scores. Nothing below is measured."
)


def badge_payload(suite: SuiteResult) -> dict[str, Any]:
    """The shields.io dynamic-endpoint JSON committed as `docs/benchmarks.json`.

    Shields reads `schemaVersion`, `label`, `message` and `color`; the extra
    keys are for humans and for the dashboard. An unevaluated suite badges as
    "not run yet", never as a score.
    """
    score = suite.score
    if not suite.evaluated or score is None:
        return {
            "schemaVersion": 1,
            "label": "capbench",
            "message": "not run yet",
            "color": "lightgrey",
            "evaluated": False,
            "suite_score": None,
        }
    return {
        "schemaVersion": 1,
        "label": "capbench",
        "message": f"{score:.0f}/100",
        "color": _badge_color(score),
        "evaluated": True,
        "suite_score": round(score, 1),
        "profile": suite.profile,
        "generated_at": suite.generated_at,
    }


def _badge_color(score: float) -> str:
    if score >= 85:
        return "brightgreen"
    if score >= 70:
        return "green"
    if score >= 50:
        return "yellow"
    if score >= 30:
        return "orange"
    return "red"


def _fmt_score(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def _task_row(task: TaskResult, evaluated: bool) -> str:
    if not evaluated:
        return f"| `{task.task_id}` | {task.title} | not run | - | - | - | - | - | - |"
    grade = task.grade
    tests = f"{grade.tests_passed}/{grade.tests_total}" if grade else "-"
    ship = "n/a" if task.ship_score is None else str(task.ship_score)
    verdict = "✅ pass" if task.passed else "❌ fail"
    return (
        f"| `{task.task_id}` | {task.title} | {verdict} | {tests} "
        f"| {task.correctness:.0f} | {ship} | ${task.cost_usd:.2f} "
        f"| {task.duration_min:.1f} min | {task.composite:.0f} |"
    )


def render_scoreboard(suite: SuiteResult, tasks_dir: Path | None = None) -> str:
    """Render `docs/benchmarks.md` from a suite result (evaluated or template)."""
    lines = ["# Capability benchmark scoreboard", "", DISAMBIGUATION, ""]
    if not suite.evaluated:
        lines += [UNEVALUATED_NOTICE, ""]
    else:
        lines += [
            f"> Suite score: **{suite.score:.0f}/100** (mean of task composites) · "
            f"profile `{suite.profile}` · {len(suite.tasks)} task(s), "
            f"{suite.repeats_per_task} run each (n={suite.repeats_per_task}) · "
            f"run {suite.generated_at[:10]}"
            + (f" · baton commit `{suite.commit}`" if suite.commit else ""),
            "",
        ]
        if suite.note:
            lines += [f"> {suite.note}", ""]
    lines += [
        "## Tasks",
        "",
        "| task | what | verdict | hidden tests | correctness | ship score "
        "| cost | time | composite |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for task in suite.tasks:
        lines.append(_task_row(task, suite.evaluated))
    lines += ["", "## The tasks in detail", ""]
    for spec in load_tasks(tasks_dir):
        lines += [
            f"### `{spec.id}` - {spec.title}",
            "",
            f"Grading: hidden pytest + diff within {spec.max_added_lines} added lines / "
            f"{spec.max_files} files + the gate's ship score.",
            "",
            "<details>",
            "<summary>brief.md (the idea the agent sees)</summary>",
            "",
            spec.brief,
            "",
            "</details>",
            "",
        ]
    lines += [
        "## Rubric",
        "",
        "Per task, four dimensions normalized to 0-100 (design §3):",
        "",
        f"- **Correctness** ({W_CORRECTNESS:.0%}): hidden-test fraction - the grader's "
        "pytest, executed by the harness, never by the agent.",
        f"- **Ship score** ({W_SHIP:.0%}): the quality gate's deterministic 0-100 ship "
        "score at the `basic` level on the merged result branch.",
        f"- **Cost** ({W_COST:.0%}): 100 at ≤ "
        f"${COST_FULL_USD:.2f}, 0 at ≥ ${COST_ZERO_USD:.2f}, linear between.",
        f"- **Time** ({W_TIME:.0%}): 100 at ≤ {TIME_FULL_MIN:.0f} min, 0 at ≥ "
        f"{TIME_ZERO_MIN:.0f} min, linear between.",
        "",
        "Suite score = mean of the task composites. The table above publishes the raw "
        "dimensions, not just the composite.",
        "",
        "## How it runs",
        "",
        "- Manual trigger only: `jev capbench run --task <id>` or `jev capbench run --all`.",
        "- Each task gets a throwaway sandbox repo copied from its fixture, a fixed "
        f"`[budget] per_run_usd = ${2.0:.0f}` cap, `basic` quality level, and the pinned "
        "worker profile; grading happens on the merged result branch.",
        "- **No nightly schedule exists.** A nightly run would cost ~$3-7/night in model "
        "spend and needs the maintainer's explicit go-ahead before it is built.",
        "- Grading is deterministic only (hidden pytest, diff-size bands, ship score). "
        "No LLM judge.",
        "- Anti-gaming: hidden tests live outside the fixture the agent sees; raw "
        "dimensions are published; tasks at 100% for 90 days get replaced or hardened.",
        "",
        "_Generated by `jev capbench report`; do not edit by hand._",
        "",
    ]
    return "\n".join(lines)


def write_scoreboard(
    suite: SuiteResult,
    *,
    md_path: Path,
    json_path: Path,
    tasks_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Write `docs/benchmarks.md` and `docs/benchmarks.json`; return their paths."""
    md_path.write_text(render_scoreboard(suite, tasks_dir), encoding="utf-8")
    json_path.write_text(json.dumps(badge_payload(suite), indent=2) + "\n", encoding="utf-8")
    return md_path, json_path
