"""KPIs of runs and of the pull requests `jev ship` opened for them (docs/ship.md).

- `compute_run_kpis`: what the run itself says (cost, time, attempts, retries, quality findings,
  decisions, free model share, lessons). A pure function of the store, plus the result
  branch's diff size when the caller passes it (`diff_stat` reads it from git).
- `github_kpis`: what `gh pr view --json ...` says about a PR (state, time to merge, reviews,
  CI, follow-up commits), as a pure function of that JSON.
- `PrKpis` joins a shipped PR with its run's KPIs; `load_pr_kpis` reads every shipped PR.
"""

from __future__ import annotations

import re
import statistics
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jev_agent.core.integration import fix_rounds
from jev_agent.core.models import TaskState
from jev_agent.core.store import ShippedPrRecord, Store
from jev_agent.quality.report import latest_per_gate
from jev_agent.roles.schemas import SEVERITIES
from jev_agent.workers.openrouter_models import is_free_slug

_ADDED = re.compile(r"(\d+) added")


class DiffStat(BaseModel):
    files: int = 0
    additions: int = 0
    deletions: int = 0


class RunKpis(BaseModel):
    """KPIs of one run. Rates are fractions (0..1); None means "not applicable or unknown"."""

    run_id: str
    total_cost_usd: float
    wall_clock_s: float | None  # run created -> its last recorded activity
    tasks: int
    tasks_done: int
    attempts_total: int  # coder attempts over all tasks
    attempts_per_task: float | None
    first_pass_rate: float | None  # tasks DONE on their first attempt / tasks
    retries: int
    escalations: int
    reassigns: int
    fix_rounds: int
    quality_level: str
    findings: dict[str, int] = Field(default_factory=dict)  # severity -> count, latest gates
    quality_blocked_count: int = 0  # gates that blocked, every attempt and round
    tests_passed: bool | None = None  # integration tests at the last D6; None: none ran
    diff_files: int | None = None
    diff_additions: int | None = None
    diff_deletions: int | None = None
    decisions_total: int = 0
    jev_decisions: int = 0
    rules_fallbacks: int = 0
    fallback_rate: float | None = None  # fallbacks / decisions Jev was asked to make
    mean_jev_confidence: float | None = None
    api_attempts: int = 0
    free_attempts: int = 0
    free_model_share: float | None = None  # free-model attempts / api attempts
    lessons_used: int = 0
    lessons_added: int | None = None

    @property
    def findings_total(self) -> int:
        return sum(self.findings.values())


def diff_stat(repo: Path, base: str, head: str) -> DiffStat | None:
    """`git diff --shortstat base..head` as numbers; None when git cannot tell."""
    proc = subprocess.run(
        ["git", "diff", "--shortstat", f"{base}..{head}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return parse_shortstat(proc.stdout)


def parse_shortstat(text: str) -> DiffStat:
    """` 3 files changed, 10 insertions(+), 2 deletions(-)` (any part may be missing)."""

    def number(pattern: str) -> int:
        match = re.search(pattern, text)
        return int(match.group(1)) if match else 0

    return DiffStat(
        files=number(r"(\d+) files? changed"),
        additions=number(r"(\d+) insertions?\(\+\)"),
        deletions=number(r"(\d+) deletions?\(-\)"),
    )


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _confidences(details: Mapping[str, Any]) -> list[float]:
    answers = details.get("answers")
    if not isinstance(answers, dict):
        return []
    found: list[float] = []
    for info in answers.values():
        value = info.get("confidence") if isinstance(info, dict) else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            found.append(float(value))
    return found


def compute_run_kpis(store: Store, run_id: str, diff: DiffStat | None = None) -> RunKpis:
    """The run's KPIs from the store. `diff` is the result branch's size (`diff_stat`)."""
    run = store.get_run(run_id)  # NotFoundError for an unknown run
    tasks = store.list_tasks(run_id)
    attempts = store.list_attempts(run_id)
    decisions = store.list_decisions(run_id)
    reports = store.list_quality_reports(run_id)

    times = [run.created_at, run.updated_at]
    times += [a.finished_at or a.started_at for a in attempts]
    times += [d.created_at for d in decisions]
    times += [r.created_at for r in reports]
    end = max(times)
    wall_clock = (end - run.created_at).total_seconds()

    done = [t for t in tasks if t.state is TaskState.DONE]
    first_pass = [t for t in done if t.attempts == 1]
    attempts_total = sum(t.attempts for t in tasks)

    actions = [d.decision.answers.get("action") for d in decisions if d.decision.point == "D5"]
    d6 = [d for d in decisions if d.decision.point == "D6"]
    tests = d6[-1].request.state.get("tests") if d6 else None

    findings: dict[str, int] = dict.fromkeys(SEVERITIES, 0)
    for gate in latest_per_gate([r.result for r in reports]):
        for severity, count in gate.counts().items():
            findings[severity] += count

    jev = [d for d in decisions if d.decision.dispatcher == "jev"]
    fallbacks = [d for d in decisions if d.fallback_reason]
    asked_jev = len(jev) + len(fallbacks)
    confidences = [c for d in jev for c in _confidences(d.decision.details)]

    api = [a for a in attempts if a.worker == "api"]
    free = [a for a in api if is_free_slug(a.model)]
    lesson_ids = {lesson for a in attempts for lesson in a.lesson_ids}
    added = _ADDED.search(run.learning_outcome or "")

    return RunKpis(
        run_id=run_id,
        total_cost_usd=round(run.spend_usd, 6),
        wall_clock_s=round(wall_clock, 3),
        tasks=len(tasks),
        tasks_done=len(done),
        attempts_total=attempts_total,
        attempts_per_task=attempts_total / len(tasks) if tasks else None,
        first_pass_rate=len(first_pass) / len(tasks) if tasks else None,
        retries=actions.count("retry"),
        escalations=actions.count("escalate"),
        reassigns=actions.count("reassign"),
        fix_rounds=fix_rounds(run.plan.tasks) if run.plan else 0,
        quality_level=run.quality_level,
        findings=findings,
        quality_blocked_count=sum(1 for r in reports if r.result.blocked),
        tests_passed={"passed": True, "failed": False}.get(str(tests)),
        diff_files=None if diff is None else diff.files,
        diff_additions=None if diff is None else diff.additions,
        diff_deletions=None if diff is None else diff.deletions,
        decisions_total=len(decisions),
        jev_decisions=len(jev),
        rules_fallbacks=len(fallbacks),
        fallback_rate=len(fallbacks) / asked_jev if asked_jev else None,
        mean_jev_confidence=_mean(confidences),
        api_attempts=len(api),
        free_attempts=len(free),
        free_model_share=len(free) / len(api) if api else None,
        lessons_used=len(lesson_ids),
        lessons_added=int(added.group(1)) if added else None,
    )


def run_kpis(store: Store, run_id: str) -> RunKpis:
    """The run's KPI snapshot (taken when it was shipped), else computed now (no diff size)."""
    snapshot = store.get_run_kpis(run_id)
    if snapshot is not None:
        return RunKpis.model_validate_json(snapshot[1])
    return compute_run_kpis(store, run_id)


# ---- GitHub ------------------------------------------------------------------------------

# Fields `jev ship sync` asks `gh pr view --json` for (docs/verification.md section 8).
PR_VIEW_FIELDS = (
    "state",
    "createdAt",
    "mergedAt",
    "closedAt",
    "reviews",
    "comments",
    "commits",
    "additions",
    "deletions",
    "changedFiles",
    "statusCheckRollup",
    "isDraft",
)
_CHECK_FAILED = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
_CHECK_PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def parse_time(value: object) -> datetime | None:
    """GitHub's ISO times (`2026-10-12T15:30:12Z`); None for null, "" or garbage."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ci_status(rollup: object) -> str:
    """`statusCheckRollup` as `success`, `failure`, `pending` or `none` (no checks).

    Check runs carry `status` (COMPLETED, IN_PROGRESS, ...) and `conclusion`; commit statuses
    (`StatusContext`) carry `state` (SUCCESS, FAILURE, ERROR, PENDING, EXPECTED). Any failure
    wins, then anything unfinished, else success.
    """
    if not isinstance(rollup, list) or not rollup:
        return "none"
    pending = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        outcome = str(check.get("conclusion") or check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if outcome in _CHECK_FAILED:
            return "failure"
        if outcome not in _CHECK_PASSED or (status and status != "COMPLETED"):
            pending = True
    return "pending" if pending else "success"


def _count(value: object) -> int | None:
    return len(value) if isinstance(value, list) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class GithubKpis:
    state: str  # open | closed | merged
    draft: bool
    created_at: datetime | None
    merged_at: datetime | None
    closed_at: datetime | None
    reviews: int | None
    review_comments: int | None
    ci_status: str
    commits_after_open: int | None
    additions: int | None
    deletions: int | None
    changed_files: int | None


def github_kpis(view: Mapping[str, Any], pushed_commits: int | None) -> GithubKpis:
    """KPIs from one `gh pr view --json <PR_VIEW_FIELDS>` answer.

    `review_comments` counts conversation comments plus reviews with a non-empty body (inline
    review comments are not in `gh pr view`'s JSON). `commits_after_open` is the PR's commit
    count minus the commits jev pushed.
    """
    state = str(view.get("state") or "OPEN").lower()
    if state not in ("open", "closed", "merged"):
        state = "open"
    reviews = view.get("reviews")
    comments = view.get("comments")
    review_comments: int | None = None
    if isinstance(reviews, list) or isinstance(comments, list):
        bodies = [r for r in reviews or [] if isinstance(r, dict) and str(r.get("body") or "")]
        review_comments = len(comments or []) + len(bodies)
    commits = _count(view.get("commits"))
    after = None
    if commits is not None and pushed_commits is not None:
        after = max(commits - pushed_commits, 0)
    return GithubKpis(
        state=state,
        draft=bool(view.get("isDraft")),
        created_at=parse_time(view.get("createdAt")),
        merged_at=parse_time(view.get("mergedAt")),
        closed_at=parse_time(view.get("closedAt")),
        reviews=_count(reviews),
        review_comments=review_comments,
        ci_status=ci_status(view.get("statusCheckRollup")),
        commits_after_open=after,
        additions=_int(view.get("additions")),
        deletions=_int(view.get("deletions")),
        changed_files=_int(view.get("changedFiles")),
    )


def is_revert_of(candidate: Mapping[str, Any], repo: str, number: int, title: str) -> bool:
    """A PR reverts `repo#number`: GitHub's revert button titles it `Revert "<title>"` and
    writes `Reverts <repo>#<number>` in the body. Either is enough."""
    body = str(candidate.get("body") or "")
    other = str(candidate.get("title") or "")
    mention = re.search(rf"\bReverts {re.escape(repo)}#{number}(?!\d)", body)
    return mention is not None or (bool(title) and other == f'Revert "{title}"')


# ---- shipped PRs with their run's KPIs ---------------------------------------------------


@dataclass(frozen=True)
class PrKpis:
    pr: ShippedPrRecord
    run: RunKpis

    def value(self, key: str) -> float | None:
        """A KPI by name (`core.benchmarks.KPI_DEFS`): from the PR, else from the run."""
        if key == "cost_usd":
            return self.run.total_cost_usd
        if key == "wall_clock_h":
            return None if self.run.wall_clock_s is None else self.run.wall_clock_s / 3600
        if key == "time_to_merge_h":
            return self.pr.time_to_merge_h
        if key == "review_comments":
            return None if self.pr.review_comments is None else float(self.pr.review_comments)
        if key == "findings_total":
            return float(self.run.findings_total)
        value = getattr(self.run, key, None)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @property
    def label(self) -> str:
        return f"#{self.pr.number}" if self.pr.number is not None else self.pr.branch


def load_pr_kpis(store: Store, last: int | None = None) -> list[PrKpis]:
    """Every shipped PR (the last `last` ones), oldest first, with its run's KPIs."""
    prs = store.list_shipped_prs()
    if last is not None:
        prs = prs[-last:] if last > 0 else []
    cache: dict[str, RunKpis] = {}
    out: list[PrKpis] = []
    for pr in prs:
        if pr.run_id not in cache:
            cache[pr.run_id] = run_kpis(store, pr.run_id)
        out.append(PrKpis(pr, cache[pr.run_id]))
    return out
