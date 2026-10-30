"""The PR body: KPI card and Markdown description."""

from __future__ import annotations

from jev_agent.core.kpi import RunKpis
from jev_agent.core.ship.urls import FOOTER
from jev_agent.core.store import Store
from jev_agent.core.workflow_profiles import assurance_label_text
from jev_agent.quality.report import cell, latest_per_gate
from jev_agent.roles.schemas import SEVERITIES
from jev_agent.runlog.redact import redact


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes = round(seconds / 60)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"


def kpi_rows(k: RunKpis) -> list[tuple[str, str]]:
    """The KPI card, as (name, value) rows; shared by the PR body and the confirmation."""
    if k.tests_passed is True:
        tests = "passed"
    elif k.tests_passed is False:
        tests = "failed"
    else:
        tests = "not run"
    diff = (
        "-"
        if k.diff_files is None
        else f"{k.diff_files} files, +{k.diff_additions} / -{k.diff_deletions}"
    )
    lessons = f"{k.lessons_used} used" + (
        f", {k.lessons_added} added" if k.lessons_added is not None else ""
    )
    return [
        ("Total cost", _usd(k.total_cost_usd)),
        ("Wall-clock time", _duration(k.wall_clock_s)),
        ("Tasks", f"{k.tasks_done} of {k.tasks} done"),
        (
            "Attempts per task",
            "-" if k.attempts_per_task is None else f"{k.attempts_per_task:.2f}",
        ),
        ("First-pass rate", _pct(k.first_pass_rate)),
        ("Retries / escalations / reassigns", f"{k.retries} / {k.escalations} / {k.reassigns}"),
        ("Fix rounds", str(k.fix_rounds)),
        ("Integration tests", tests),
        ("Diff", diff),
        ("Free model share (api attempts)", _pct(k.free_model_share)),
        ("Lessons", lessons),
    ]


def render_pr_body(store: Store, run_id: str, kpis: RunKpis) -> str:
    """The PR description in Markdown, redacted like every log."""
    run = store.get_run(run_id)
    tasks = store.list_tasks(run_id)
    reports = store.list_quality_reports(run_id)
    lines = ["## Idea", ""]
    lines += [f"> {line}" if line.strip() else ">" for line in run.idea.text.strip().splitlines()]
    lines += ["", "## Plan", "", run.plan.summary if run.plan else "(no plan recorded)", ""]
    if tasks:
        lines += [
            "| Task | Title | Worker | Model | Effort | Attempts | Cost |",
            "| --- | --- | --- | --- | --- | ---: | ---: |",
        ]
        for t in tasks:
            row = [t.id, t.card.title, t.worker or "-", t.model or "default", t.effort or "-"]
            lines.append(
                "| " + " | ".join(cell(v) for v in row) + f" | {t.attempts} | {_usd(t.spend_usd)} |"
            )
        lines.append("")
    lines += ["## Quality", ""]
    gates = latest_per_gate([r.result for r in reports])
    if not gates:
        lines.append(f"Level `{run.quality_level}`: no quality gate ran.")
    else:
        blocked = [g for g in gates if g.blocked]
        blocking = sum(len(g.reasons) for g in blocked)
        passed = [g.counts() for g in gates if not g.blocked]
        nonblocking = {s: sum(c[s] for c in passed) for s in SEVERITIES}
        found = ", ".join(f"{n} {s}" for s, n in nonblocking.items() if n) or "none"
        verdict = (
            f"{len(blocked)} of {len(gates)} gate(s) blocked"
            if blocked
            else f"all {len(gates)} gate(s) passed"
        )
        lines += [
            f"Level `{run.quality_level}`: {verdict}.",
            "",
            f"- Blocking problems: {blocking}",
            f"- Non-blocking findings: {sum(nonblocking.values())} ({found})",
            "",
            f"The full review stays in the local repo: `jev quality show {run_id}`.",
        ]
    # Assurance label (issue #368, design 10.3): the same text as the run
    # page and the board row, written by this one writer for both the
    # dashboard and CLI ship paths — including a hand-shipped reduced run,
    # whose label travels and whose ship proceeds with no extra step.
    if run.assurance is not None:
        lines += [
            "",
            "## Assurance",
            "",
            assurance_label_text(run.assurance, run.assurance_absent),
            "",
        ]
    lines += ["", "## KPIs", "", "| KPI | Value |", "| --- | --- |"]
    lines += [f"| {cell(name)} | {cell(value)} |" for name, value in kpi_rows(kpis)]
    rules_only = kpis.decisions_total - kpis.jev_decisions - kpis.rules_fallbacks
    confidence = "-" if kpis.mean_jev_confidence is None else f"{kpis.mean_jev_confidence:.2f}"
    lines += [
        "",
        "## Decisions",
        "",
        f"{kpis.decisions_total} decisions: {kpis.jev_decisions} by Jev, "
        f"{kpis.rules_fallbacks} rules fallbacks ({_pct(kpis.fallback_rate)} of those asked of "
        f"Jev), {rules_only} by the rules dispatcher directly. Mean Jev confidence: "
        f"{confidence}.",
        "",
        "---",
        "",
        f"{FOOTER} Run `{run_id}`.",
        "",
    ]
    return redact("\n".join(lines))
