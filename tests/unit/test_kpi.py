"""Run KPIs, GitHub KPIs and the benchmark math (rolling medians, regression flags)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jev_agent.core.benchmarks import (
    KPI_BY_KEY,
    format_value,
    is_worse,
    rolling_median,
    summarize,
    trend,
    trends,
)
from jev_agent.core.kpi import (
    DiffStat,
    PrKpis,
    RunKpis,
    ci_status,
    compute_run_kpis,
    github_kpis,
    is_revert_of,
    load_pr_kpis,
    parse_shortstat,
    run_kpis,
)
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    TaskCard,
    TaskState,
    WorkerResult,
)
from jev_agent.core.store import LearningCounts, ShippedPrRecord, Store
from jev_agent.quality.models import CheckResult, Finding, GateResult


def _decide(
    store: Store,
    run_id: str,
    task: str | None,
    point: str,
    answers: dict[str, str],
    *,
    dispatcher: str = "jev",
    confidence: float | None = None,
    fallback: str | None = None,
    state: dict[str, object] | None = None,
) -> None:
    details = (
        {"answers": {k: {"choice": v, "confidence": confidence} for k, v in answers.items()}}
        if confidence is not None
        else {}
    )
    request = DecisionRequest(
        point=point,  # type: ignore[arg-type]
        state=state or {},
        questions={k: [v] for k, v in answers.items()},
    )
    decision = Decision(
        point=point,  # type: ignore[arg-type]
        answers=answers,
        dispatcher=dispatcher,
        details=details,
    )
    store.record_decision(run_id, task, request, decision, fallback)


def _attempt(
    store: Store, tmp: Path, run_id: str, task: str | None, worker: str, model: str | None
) -> None:
    attempt = store.start_attempt(
        run_id, task, 1, "coder", worker, model, lesson_ids=["L1", "L2"] if task else ()
    )
    store.finish_attempt(
        attempt, WorkerResult(status="success", summary="ok", raw_log_path=tmp / "log")
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store.open(tmp_path / "state.db")


def test_compute_run_kpis(store: Store, tmp_path: Path) -> None:
    run = store.create_run(Idea(text="x", repo_path=tmp_path), quality_level="basic")
    cards = [
        TaskCard(id="T1", title="a", description="a"),
        TaskCard(id="T2", title="b", description="b"),
        TaskCard(id="T3", title="Fix integration failures (round 1)", description="c"),
    ]
    store.set_run_plan(run.id, Plan(summary="s", tasks=cards), 1)
    store.upsert_tasks(run.id, Plan(summary="s", tasks=cards))
    for task, tries in (("T1", 1), ("T2", 3), ("T3", 1)):
        for _ in range(tries):
            store.increment_task_attempts(run.id, task)
        store.set_task_state(run.id, task, TaskState.DONE)
    _decide(store, run.id, None, "D1", {"clear": "plan"}, confidence=0.9)
    _decide(store, run.id, "T2", "D5", {"action": "retry"}, confidence=0.7)
    _decide(store, run.id, "T2", "D5", {"action": "escalate"}, dispatcher="rules", fallback="x")
    _decide(store, run.id, "T2", "D5", {"action": "done"}, confidence=0.8)
    _decide(
        store, run.id, None, "D6", {"action": "ship"}, dispatcher="rules", state={"tests": "passed"}
    )
    _attempt(store, tmp_path, run.id, "T1", "api", "qwen/qwen3-coder:free")
    _attempt(store, tmp_path, run.id, "T2", "api", "openai/gpt-5-mini")
    _attempt(store, tmp_path, run.id, None, "claude_code", "sonnet")
    store.add_spend(run.id, "T2", 1.25)
    finding = Finding(source="ruff", severity="minor", category="docs", title="t")
    gate = GateResult(
        level="basic",
        scope="integration",
        checks=[CheckResult(status="pass", findings=[finding])],
    )
    store.add_quality_report(
        run.id, GateResult(level="basic", scope="task", task_id="T2", blocked=True, reasons=["r"])
    )
    store.add_quality_report(run.id, gate)
    store.add_run_learning_counts(run.id, LearningCounts(added=2, updated=0, retired=0))

    k = compute_run_kpis(store, run.id, DiffStat(files=3, additions=10, deletions=2))
    assert k.total_cost_usd == 1.25 and k.tasks == 3 and k.tasks_done == 3
    assert k.attempts_total == 5 and k.attempts_per_task == pytest.approx(5 / 3)
    assert k.first_pass_rate == pytest.approx(2 / 3)
    assert (k.retries, k.escalations, k.reassigns, k.fix_rounds) == (1, 1, 0, 1)
    assert k.findings["minor"] == 1 and k.findings["critical"] == 0
    # The task gate that blocked is still its latest gate, so it counts; one gate blocked.
    assert k.quality_blocked_count == 1 and k.quality_level == "basic"
    assert k.tests_passed is True
    assert (k.diff_files, k.diff_additions, k.diff_deletions) == (3, 10, 2)
    assert (k.decisions_total, k.jev_decisions, k.rules_fallbacks) == (5, 3, 1)
    assert k.fallback_rate == pytest.approx(1 / 4)
    assert k.mean_jev_confidence == pytest.approx((0.9 + 0.7 + 0.8) / 3)
    assert (k.api_attempts, k.free_attempts, k.free_model_share) == (2, 1, 0.5)
    assert k.lessons_used == 2 and k.lessons_added == 2
    assert k.wall_clock_s is not None and k.wall_clock_s >= 0

    # No snapshot yet: computed on the fly (no diff); with one, the snapshot wins.
    assert run_kpis(store, run.id).diff_files is None
    store.set_run_kpis(run.id, k.model_dump_json())
    assert run_kpis(store, run.id) == k


def test_kpis_of_an_empty_run(store: Store, tmp_path: Path) -> None:
    run = store.create_run(Idea(text="x", repo_path=tmp_path))
    k = compute_run_kpis(store, run.id)
    assert k.tasks == 0 and k.attempts_per_task is None and k.first_pass_rate is None
    assert k.fallback_rate is None and k.mean_jev_confidence is None
    assert k.free_model_share is None and k.tests_passed is None and k.lessons_added is None


def test_lessons_added_comes_from_structured_counts_not_the_outcome_text(
    store: Store, tmp_path: Path
) -> None:
    """Issue #42: the human-readable outcome line is never parsed."""
    run = store.create_run(Idea(text="x", repo_path=tmp_path))
    store.set_run_learning_outcome(run.id, "learned into p: 7 added, 0 updated, 0 retired")
    assert compute_run_kpis(store, run.id).lessons_added is None
    store.add_run_learning_counts(run.id, LearningCounts(added=1, updated=2, retired=0))
    store.add_run_learning_counts(run.id, LearningCounts(added=2, updated=0, retired=1))
    assert store.get_run(run.id).learning_counts == LearningCounts(added=3, updated=2, retired=1)
    assert compute_run_kpis(store, run.id).lessons_added == 3


def test_github_kpis_state_is_a_known_pr_state() -> None:
    assert github_kpis({"state": "MERGED"}, None).state == "merged"
    assert github_kpis({"state": "CLOSED"}, None).state == "closed"
    # Anything gh might print that is not a PR state reads as open (the column's default).
    assert github_kpis({"state": "DRAFT?"}, None).state == "open"
    assert github_kpis({"state": None}, None).state == "open"
    assert github_kpis({}, None).state == "open"


def test_parse_shortstat() -> None:
    assert parse_shortstat(" 3 files changed, 10 insertions(+), 2 deletions(-)\n") == DiffStat(
        files=3, additions=10, deletions=2
    )
    assert parse_shortstat(" 1 file changed, 1 insertion(+)") == DiffStat(files=1, additions=1)
    assert parse_shortstat("") == DiffStat()


def test_ci_status() -> None:
    ok = {"status": "COMPLETED", "conclusion": "SUCCESS"}
    assert ci_status([]) == "none" and ci_status(None) == "none"
    assert (
        ci_status([ok, {"state": "SUCCESS"}, {"conclusion": "SKIPPED", "status": "COMPLETED"}])
        == "success"
    )
    assert ci_status([ok, {"status": "IN_PROGRESS", "conclusion": ""}]) == "pending"
    assert ci_status([ok, {"state": "PENDING"}]) == "pending"
    assert ci_status([{"status": "IN_PROGRESS"}, {"conclusion": "FAILURE"}]) == "failure"
    assert ci_status([{"state": "ERROR"}]) == "failure"


def test_github_kpis() -> None:
    view = {
        "state": "MERGED",
        "isDraft": False,
        "createdAt": "2026-10-12T10:00:00Z",
        "mergedAt": "2026-10-12T12:30:00Z",
        "reviews": [{"body": "nit"}, {"body": ""}],
        "comments": [{"body": "a"}],
        "commits": [{}, {}, {}],
        "additions": 5,
        "deletions": 1,
        "changedFiles": 2,
        "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
    }
    k = github_kpis(view, pushed_commits=2)
    assert k.state == "merged" and not k.draft and k.reviews == 2 and k.review_comments == 2
    assert k.commits_after_open == 1 and k.ci_status == "success" and k.changed_files == 2
    assert k.merged_at == datetime(2026, 10, 12, 12, 30, tzinfo=UTC)
    bare = github_kpis({"state": "weird"}, None)
    assert bare.state == "open" and bare.review_comments is None and bare.commits_after_open is None
    assert bare.ci_status == "none"


def test_is_revert_of() -> None:
    assert is_revert_of({"body": "Reverts acme/w#42"}, "acme/w", 42, "t")
    assert is_revert_of({"title": 'Revert "Add search"'}, "acme/w", 42, "Add search")
    assert not is_revert_of({"body": "mentions acme/w#42", "title": "x"}, "acme/w", 42, "t")
    assert not is_revert_of({"body": "Reverts acme/w#421", "title": "x"}, "acme/w", 42, "t")


# ---- benchmarks ----------------------------------------------------------------------------


def test_rolling_median() -> None:
    assert rolling_median([]) == []
    assert rolling_median([1, 3, 2, 10, 4, 5, 6], window=5) == [1, 2, 2, 2.5, 3, 4, 5]
    assert rolling_median([4, 2], window=1) == [4, 2]


@pytest.mark.parametrize(
    ("key", "value", "baseline", "worse"),
    [
        ("cost_usd", 1.51, 1.0, True),
        ("cost_usd", 1.5, 1.0, False),  # exactly 1.5x is not "above"
        ("cost_usd", 0.1, 1.0, False),  # cheaper is better
        ("cost_usd", 5.0, 0.0, False),  # never against a zero median
        ("wall_clock_h", 4.0, 2.0, True),
        ("attempts_per_task", 2.0, 1.2, True),
        ("first_pass_rate", 0.60, 0.80, True),  # 20 points below
        ("first_pass_rate", 0.66, 0.80, False),  # 14 points below
        ("first_pass_rate", 1.0, 0.5, False),
        ("fallback_rate", 0.40, 0.20, True),
        ("fallback_rate", 0.30, 0.20, False),
        ("mean_jev_confidence", 0.60, 0.80, True),
        ("mean_jev_confidence", 0.70, 0.80, False),
    ],
)
def test_is_worse(key: str, value: float, baseline: float, worse: bool) -> None:
    assert is_worse(value, baseline, KPI_BY_KEY[key]) is worse


def test_trend_flags_against_previous_window() -> None:
    kpi = KPI_BY_KEY["cost_usd"]
    values = [1.0, 1.2, 0.8, 1.0, 1.1, 0.9, 2.0, 1.0]
    t = trend(kpi, [(f"#{i}", v) for i, v in enumerate(values)])
    flags = [p.regression for p in t.points]
    # The first three have too little history; #6 (2.0) beats 1.5 x median(1.2,.8,1,1.1,.9)=1.5.
    assert flags == [False, False, False, False, False, False, True, False]
    assert t.points[6].baseline == pytest.approx(1.0)
    assert t.points[6].rolling == pytest.approx(1.0)  # median of 0.8, 1.0, 1.1, 0.9, 2.0
    assert not t.regressed  # the latest point is fine
    t2 = trend(kpi, [(f"#{i}", v) for i, v in enumerate(values[:7])])
    assert t2.regressed and "#6 $2.00 is worse than the median $1.00" in t2.message()


def test_trend_skips_missing_values_and_short_series() -> None:
    kpi = KPI_BY_KEY["time_to_merge_h"]
    t = trend(kpi, [("#1", None), ("#2", 2.0), ("#3", None), ("#4", 100.0)])
    assert [p.label for p in t.points] == ["#2", "#4"]
    assert not any(p.regression for p in t.points)  # not enough history
    assert trend(kpi, []).latest is None and trend(kpi, []).message() == ""


def test_rate_regression_and_lower_is_worse() -> None:
    kpi = KPI_BY_KEY["first_pass_rate"]
    t = trend(kpi, [("a", 1.0), ("b", 0.9), ("c", 1.0), ("d", 0.7)])
    assert t.points[-1].regression  # 0.7 < median(1, .9, 1)=1.0 - 0.15
    t = trend(kpi, [("a", 1.0), ("b", 0.9), ("c", 1.0), ("d", 0.9)])
    assert not t.points[-1].regression


def test_format_value() -> None:
    assert format_value(None, "usd") == "-"
    assert format_value(1.234, "usd") == "$1.23"
    assert format_value(0.5, "hours") == "30m" and format_value(5.25, "hours") == "5.2h"
    assert format_value(0.333, "percent") == "33%"
    assert format_value(2.0, "count") == "2" and format_value(1.5, "count") == "1.5"


def _row(n: int, cost: float, state: str = "open", merged_h: float | None = None) -> PrKpis:
    created = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=n)
    pr = ShippedPrRecord(
        id=n,
        run_id=f"r{n}",
        remote="origin",
        repo="a/b",
        branch=f"jev/r{n}",
        base="main",
        number=n,
        url=None,
        title="t",
        draft=False,
        head_oid=None,
        pushed_commits=1,
        created_at=created,
        state=state,  # type: ignore[arg-type]
        merged_at=None if merged_h is None else created + timedelta(hours=merged_h),
    )
    run = RunKpis(
        run_id=f"r{n}",
        total_cost_usd=cost,
        wall_clock_s=600,
        tasks=2,
        tasks_done=2,
        attempts_total=2,
        attempts_per_task=1.0,
        first_pass_rate=1.0,
        retries=0,
        escalations=0,
        reassigns=0,
        fix_rounds=0,
        quality_level="none",
    )
    return PrKpis(pr, run)


def test_summarize_and_trends() -> None:
    rows = [_row(1, 1.0, "merged", 2), _row(2, 2.0, "merged", 4), _row(3, 3.0), _row(4, 9.0)]
    s = summarize(rows)
    assert s.prs == 4 and s.merged == 2 and s.merged_share == 0.5
    assert s.median_cost_usd == 2.5 and s.median_time_to_merge_h == 3.0
    assert s.median_first_pass_rate == 1.0 and s.median_fallback_rate is None
    found = {t.kpi.key: t for t in trends(rows)}
    assert found["cost_usd"].regressed  # 9 > 1.5 x median(1, 2, 3)
    assert [p.label for p in found["time_to_merge_h"].points] == ["#1", "#2"]
    assert all(len(t.points) <= 2 for t in trends(rows, last=2))
    assert summarize([]).merged_share is None


def test_load_pr_kpis(store: Store, tmp_path: Path) -> None:
    run = store.create_run(Idea(text="x", repo_path=tmp_path))
    for n in range(3):
        store.add_shipped_pr(
            run.id,
            remote="origin",
            repo="a/b",
            branch=f"b{n}",
            base="main",
            number=n + 1,
            url=None,
            title="t",
            draft=True,
        )
    rows = load_pr_kpis(store, last=2)
    assert [r.label for r in rows] == ["#2", "#3"]
    assert rows[0].run is rows[1].run  # one run, computed once
    assert load_pr_kpis(store, last=0) == []
    assert rows[0].value("nonexistent") is None and rows[0].value("cost_usd") == 0.0
