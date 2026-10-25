"""Fill a repo's `.jev/` with illustrative demo state.

Everything written here is made up: the runs, plans, decisions, attempts, costs, quality
reviews, lessons, the saved chat and the shipped pull requests (with their GitHub state) only
look like what a few days of real use produce; nothing is pushed and GitHub is never called. No
agent, model or network is called.

Two entry points share this seeder:

- `examples/demo/seed_demo.py` (the screenshots pipeline, `docs/dashboard.md`): full demo
  configuration — `configure_demo` edits the demo's `jev.toml` (rules dispatcher, free models
  for the planner, example MCP servers and a skill given to roles, `[ship] enabled`; variable
  names only, no secrets), adds an example skill folder, and trusts the demo repo in a
  scratch user config kept inside the demo repo. It exists so the screenshots in
  `docs/images/` can be regenerated (`dashboard-*.png` with Chrome, `cli-*.svg` with
  `examples/demo/capture.py`).
- `jev init --demo`: state only (`demo_config=False`). The demo `jev.toml` dressing changes
  guarded keys, which would need the repo trusted in the user's own config; a demo must work
  immediately and must never touch the user's config, so the seeder skips those edits and
  only caches the offline model list.

Usage: `examples/demo/screenshots.py` sets up a scratch repo, runs this script and captures
every image (see docs/dashboard.md "Screenshots"). By hand, keep the user config inside the
demo repo, since the script adds the demo to its `[trust] repos`:

    DEMO=$(mktemp -d /tmp/baton-demo.XXXX)
    cp -R examples/sample-repo/. "$DEMO"/
    git -C "$DEMO" init -q && git -C "$DEMO" add -A
    git -C "$DEMO" -c user.name=demo -c user.email=demo@example.com commit -qm "sample"
    export XDG_DATA_HOME="$DEMO/.xdg/data" XDG_CONFIG_HOME="$DEMO/.xdg/config"
    (cd "$DEMO" && uv run --project "$OLDPWD" --all-extras jev init)
    uv run --all-extras python examples/demo/seed_demo.py "$DEMO"

Run it on a freshly initialised repo only: it adds runs, queue items and a learning profile
next to whatever is already there. Timestamps are placed in the past relative to now, so the
board ages and the spend chart look like a couple of weeks of use.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jev_agent.config import STATE_DIRNAME, load_config
from jev_agent.config.loader import default_user_config_path
from jev_agent.core.chat import (
    BRIEF_CLOSE,
    BRIEF_OPEN,
    BRIEF_REQUEST,
    BackendName,
    ChatLearning,
    ChatSession,
    Reply,
    Turn,
)
from jev_agent.core.kpi import DiffStat, compute_run_kpis
from jev_agent.core.learning import Learning, enable_learning, use_profile
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    Role,
    RunState,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
    WorkerStatus,
)
from jev_agent.core.queue import add_idea
from jev_agent.core.ship import pr_title
from jev_agent.core.store import Store
from jev_agent.quality.gate import RUN_REVIEW_FILENAME, quality_dir
from jev_agent.quality.models import CheckResult, GateResult
from jev_agent.quality.report import render_gate, render_run_review
from jev_agent.roles.schemas import ChecklistItem, FileNote, QualityReview, ReviewFinding
from jev_agent.runlog.writer import log_path
from jev_agent.workers.openrouter_models import ModelCatalog, cache_path, parse_models

JEV_MODEL = "~typesafe/jev-latest"
PROFILE = "textstats"
CHAT_MODEL = "openai/gpt-5-mini"
FIXTURE_MODELS = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "openrouter" / "models.json"
)


# ---- a controllable clock --------------------------------------------------------------------


class Clock:
    """A controllable clock, injected into `Store`, `Learning` and `ChatSession`.

    Each call returns the current time and moves it forward by `step`, so records created
    one after another get plausible, increasing timestamps.
    """

    def __init__(self) -> None:
        self.now = datetime.now(UTC)
        self.step = timedelta(seconds=40)

    def set(self, when: datetime, step_s: float = 40) -> None:
        self.now = when
        self.step = timedelta(seconds=step_s)

    def __call__(self) -> str:
        value = self.now
        self.now += self.step
        return value.isoformat(timespec="microseconds")


# ---- demo data description ---------------------------------------------------------------------


@dataclass
class Attempt:
    role: Role
    worker: str
    model: str | None
    effort: str | None
    cost: float | None
    summary: str
    task: str | None = None
    status: WorkerStatus = "success"
    files: list[str] = field(default_factory=list)
    lessons: Sequence[str] = ()
    minutes: float = 2.0  # how long it "ran"


@dataclass
class Point:
    point: str
    answers: dict[str, str]
    confidences: dict[str, float] = field(default_factory=dict)
    task: str | None = None
    fallback: str | None = None  # set: the rules dispatcher answered instead of Jev


def _questions(point: str, answers: dict[str, str]) -> dict[str, list[str]]:
    allowed = {
        "clear": ["plan", "ask_human"],
        "verdict": ["approve", "revise", "escalate_human"],
        "complexity": ["trivial", "standard", "hard"],
        "action": (
            ["ship", "fix_task", "human"]
            if point == "D6"
            else ["done", "retry", "escalate", "reassign", "human"]
        ),
        "learn": ["learn", "skip"],
        "worker": ["claude_code", "codex", "api"],
        "effort": ["low", "medium", "high"],
    }
    return {name: sorted({*allowed.get(name, []), value}) for name, value in answers.items()}


def record_point(store: Store, run_id: str, p: Point) -> None:
    request = DecisionRequest(
        point=p.point,  # type: ignore[arg-type]
        state={"demo": True},
        questions=_questions(p.point, p.answers),
    )
    if p.fallback is None:
        details: dict[str, Any] = {
            "direct": [],
            "answers": {
                name: {"choice": value, "confidence": p.confidences.get(name, 0.9)}
                for name, value in p.answers.items()
            },
            "usage": {"prompt_tokens": 610},
        }
        decision = Decision(
            point=p.point,  # type: ignore[arg-type]
            answers=p.answers,
            dispatcher="jev",
            model=JEV_MODEL,
            cost_usd=0.0002,
            latency_ms=380 + 37 * len(p.answers),
            details=details,
        )
    else:
        decision = Decision(
            point=p.point,  # type: ignore[arg-type]
            answers=p.answers,
            dispatcher="rules",
            latency_ms=1,
        )
    store.record_decision(run_id, p.task, request, decision, fallback_reason=p.fallback)


def record_attempt(store: Store, clock: Clock, root: Path, run_id: str, a: Attempt) -> None:
    state_dir = root / STATE_DIRNAME
    if a.task is None:
        number = 1 + sum(1 for x in store.list_attempts(run_id) if x.task_id is None)
        name: str = str(number)
    elif a.role == "coder":
        number = store.increment_task_attempts(run_id, a.task)
        name = str(number)
    else:  # the reviewer shares the coder attempt's number, like the real harness
        number = store.get_task(run_id, a.task).attempts
        name = f"{number}.review"
    path = log_path(state_dir, run_id, a.task, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[demo] role={a.role} task={a.task or 'run'} attempt={number} worker={a.worker} "
        f"model={a.model or 'default'} effort={a.effort or 'default'}\n"
        "--- note ---\nIllustrative demo log written by examples/demo/seed_demo.py; no agent ran.\n"
        f"--- result ---\nstatus={a.status}\n{a.summary}\n",
        encoding="utf-8",
    )
    attempt_id = store.start_attempt(
        run_id, a.task, number, a.role, a.worker, a.model, a.effort, path, a.lessons
    )
    clock.now += timedelta(minutes=a.minutes)
    store.finish_attempt(
        attempt_id,
        WorkerResult(
            status=a.status,
            summary=a.summary,
            files_changed=a.files,
            cost_usd=a.cost,
            raw_log_path=path,
        ),
    )
    if a.cost:
        store.add_spend(run_id, a.task, a.cost)


def record_running_attempt(
    store: Store, root: Path, run_id: str, task: str, worker: str, model: str | None
) -> None:
    """Start an attempt that never finishes: the dashboard's Fleet page lists it as an agent
    currently working (its `started_at` comes from the caller's clock, so set it recent).
    The log is marked as demo: no agent ran."""
    number = store.increment_task_attempts(run_id, task)
    path = log_path(root / STATE_DIRNAME, run_id, task, str(number))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[demo] role=coder task={task} attempt={number} worker={worker} "
        f"model={model or 'default'}\n"
        "--- note ---\nIllustrative demo log written by examples/demo/seed_demo.py; "
        "no agent ran.\n",
        encoding="utf-8",
    )
    store.start_attempt(run_id, task, number, "coder", worker, model, "medium", path, ())


def write_quality(store: Store, root: Path, run_id: str, gates: Sequence[GateResult]) -> None:
    qdir = quality_dir(root / STATE_DIRNAME, run_id)
    qdir.mkdir(parents=True, exist_ok=True)
    for gate in gates:
        report = qdir / f"{gate.label}.md"
        report.write_text(render_gate(gate), encoding="utf-8")
        store.add_quality_report(run_id, gate, report)
    run = store.get_run(run_id)
    (qdir / RUN_REVIEW_FILENAME).write_text(
        render_run_review(run_id, run.quality_level, list(gates)), encoding="utf-8"
    )


def run_id_at(when: datetime, suffix: str) -> str:
    return f"{when:%Y%m%d-%H%M%S}-{suffix}"


# ---- the runs ----------------------------------------------------------------------------------


def checks(*, coverage: str = "pass", detail: str = "") -> list[CheckResult]:
    return [
        CheckResult(name="ruff", tool="ruff", status="pass", required=True),
        CheckResult(name="tests", status="pass", detail="pytest: 14 passed", required=True),
        CheckResult(name="secrets", tool="gitleaks", status="pass"),
        CheckResult(name="bandit", tool="bandit", status="pass"),
        CheckResult(
            name="semgrep", tool="semgrep", status="skipped", detail="semgrep is not installed"
        ),
        CheckResult(name="pip-audit", tool="pip-audit", status="pass"),
        CheckResult(
            name="coverage",
            tool="pytest-cov",
            status=coverage,  # type: ignore[arg-type]
            detail=detail or "coverage 91% (minimum 60%)",
        ),
    ]


def seed_word_frequencies(store: Store, clock: Clock, root: Path, start: datetime) -> str:
    """The richest run: three tasks, a retry, a quality gate and learning."""
    run_id = run_id_at(start, "3f1a")
    clock.set(start)
    plan = Plan(
        summary=(
            "Add `word_frequencies(text)` to textstats: count words case-insensitively, "
            "ignore punctuation, return a dict sorted by count; cover it with tests and "
            "document it in the README."
        ),
        tasks=[
            TaskCard(
                id="T1",
                title="Implement word_frequencies() in textstats/core.py",
                description="Reuse the existing tokenizer; lower-case words; strip punctuation.",
                files_hint=["textstats/core.py", "textstats/__init__.py"],
                acceptance_criteria=[
                    "word_frequencies('A a b.') == {'a': 2, 'b': 1}",
                    "exported from textstats",
                ],
                test_command="pytest -q tests/test_core.py",
            ),
            TaskCard(
                id="T2",
                title="Tests for word_frequencies()",
                description="Empty text, punctuation, mixed case, ordering of ties.",
                files_hint=["tests/test_core.py"],
                depends_on=["T1"],
                acceptance_criteria=["at least 5 focused tests", "pytest passes"],
                test_command="pytest -q",
            ),
            TaskCard(
                id="T3",
                title="Document word_frequencies() in the README",
                description="One usage example under 'API'.",
                files_hint=["README.md"],
                depends_on=["T1"],
                acceptance_criteria=["README example matches the function's output"],
            ),
        ],
        risks=[
            "Tie ordering is unspecified today; tests must pin it (alphabetical).",
            "Apostrophes (don't) must stay inside words.",
        ],
    )
    store.create_run(
        Idea(text="Add word_frequencies() with tests", repo_path=root),
        learning_profile=PROFILE,
        quality_level="medium",
        run_id=run_id,
    )
    store.set_run_state(run_id, RunState.PLANNING)
    record_point(
        store,
        run_id,
        Point(
            "D1",
            {
                "clear": "plan",
                "worker": "api",
                "model": "qwen/qwen3-coder:free",
                "effort": "medium",
            },
            {"clear": 0.97, "worker": 0.88, "model": 0.81, "effort": 0.74},
        ),
    )
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "planner",
            "api",
            "qwen/qwen3-coder:free",
            "medium",
            0.0,
            "Plan with 3 tasks: implementation, tests, README.",
            lessons=["L1", "L2"],
            minutes=1.2,
        ),
    )
    store.set_run_plan(run_id, plan, 1)
    store.upsert_tasks(run_id, plan)
    store.set_run_state(run_id, RunState.VALIDATING)
    record_point(
        store,
        run_id,
        Point(
            "D2",
            {"worker": "codex", "model": "gpt-5-codex", "effort": "low"},
            {"worker": 0.79, "model": 0.83, "effort": 0.9},
        ),
    )
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "validator",
            "codex",
            "gpt-5-codex",
            "low",
            None,
            "Approve: tasks are small and testable; tie ordering called out.",
            minutes=0.8,
        ),
    )
    store.set_run_validation(run_id, ValidationResult(verdict="approve"))
    record_point(store, run_id, Point("D3", {"verdict": "approve"}, {"verdict": 0.95}))
    store.set_run_state(run_id, RunState.AWAITING_APPROVAL)
    clock.now += timedelta(minutes=6)  # a human reads the plan and approves
    store.set_run_state(run_id, RunState.EXECUTING)

    # T1: first attempt fails a test, the retry passes.
    record_point(
        store,
        run_id,
        Point(
            "D4",
            {
                "complexity": "standard",
                "worker": "claude_code",
                "model": "sonnet",
                "effort": "medium",
            },
            {"complexity": 0.86, "worker": 0.91, "model": 0.84, "effort": 0.77},
            task="T1",
        ),
    )
    store.set_task_assignment(run_id, "T1", TaskState.RUNNING, "claude_code", "sonnet", "medium")
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "coder",
            "claude_code",
            "sonnet",
            "medium",
            0.21,
            'Added word_frequencies(); test_apostrophes fails: "don\'t" split into two words.',
            task="T1",
            status="failure",
            files=["textstats/core.py", "textstats/__init__.py"],
            lessons=["L1"],
            minutes=3.5,
        ),
    )
    record_point(
        store,
        run_id,
        Point("D5", {"action": "retry"}, {"action": 0.82}, task="T1"),
    )
    store.set_task_assignment(
        run_id,
        "T1",
        TaskState.RETRY,
        "claude_code",
        "sonnet",
        "medium",
        note="Keep apostrophes inside words (don't, it's).",
    )
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "coder",
            "claude_code",
            "sonnet",
            "medium",
            0.17,
            "Tokenizer keeps inner apostrophes; all tests pass.",
            task="T1",
            files=["textstats/core.py", "textstats/__init__.py"],
            lessons=["L1", "L3"],
            minutes=2.6,
        ),
    )
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "reviewer",
            "codex",
            "gpt-5-codex",
            "low",
            None,
            "Approve: small, readable change.",
            task="T1",
            minutes=0.7,
        ),
    )
    record_point(store, run_id, Point("D5", {"action": "done"}, {"action": 0.96}, task="T1"))
    store.set_task_state(run_id, "T1", TaskState.DONE)

    # T2: tests, on Codex.
    record_point(
        store,
        run_id,
        Point(
            "D4",
            {"complexity": "trivial", "worker": "codex", "model": "gpt-5-codex", "effort": "low"},
            {"complexity": 0.9, "worker": 0.72, "model": 0.8, "effort": 0.88},
            task="T2",
        ),
    )
    store.set_task_assignment(run_id, "T2", TaskState.RUNNING, "codex", "gpt-5-codex", "low")
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "coder",
            "codex",
            "gpt-5-codex",
            "low",
            None,
            "Added 6 tests (empty text, punctuation, case, ties, apostrophes, unicode).",
            task="T2",
            files=["tests/test_core.py"],
            minutes=2.1,
        ),
    )
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "reviewer",
            "api",
            "deepseek/deepseek-chat-v3.1:free",
            "low",
            0.0,
            "Approve.",
            task="T2",
            minutes=0.5,
        ),
    )
    record_point(store, run_id, Point("D5", {"action": "done"}, {"action": 0.97}, task="T2"))
    store.set_task_state(run_id, "T2", TaskState.DONE)

    # T3: README; Jev timed out once, the rules dispatcher answered.
    record_point(
        store,
        run_id,
        Point(
            "D4",
            {"complexity": "trivial", "worker": "claude_code", "model": "haiku", "effort": "low"},
            task="T3",
            fallback="jev: request timed out after 10s",
        ),
    )
    store.set_task_assignment(run_id, "T3", TaskState.RUNNING, "claude_code", "haiku", "low")
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "coder",
            "claude_code",
            "haiku",
            "low",
            0.03,
            "README: added a word_frequencies() example under API.",
            task="T3",
            files=["README.md"],
            minutes=1.0,
        ),
    )
    record_point(store, run_id, Point("D5", {"action": "done"}, {"action": 0.98}, task="T3"))
    store.set_task_state(run_id, "T3", TaskState.DONE)

    # Integration: quality gate and AI review.
    store.set_run_state(run_id, RunState.INTEGRATING)
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "code_reviewer",
            "claude_code",
            "sonnet",
            "medium",
            0.09,
            "Review: no blocking problems; 1 major, 1 minor and 1 info finding.",
            minutes=1.4,
        ),
    )
    review = QualityReview(
        summary=(
            "The change adds a small, well-tested `word_frequencies()` and a README example. "
            "Tokenization is shared with `word_count()`, so both now keep inner apostrophes."
        ),
        walkthrough=[
            FileNote(
                file="textstats/core.py",
                change="New word_frequencies(); tokenizer keeps inner apostrophes.",
            ),
            FileNote(file="textstats/__init__.py", change="Exports word_frequencies."),
            FileNote(file="tests/test_core.py", change="6 new tests."),
            FileNote(file="README.md", change="Usage example."),
        ],
        findings=[
            ReviewFinding(
                severity="major",
                category="tests",
                title="No test covers non-ASCII words (café, naïve)",
                file="tests/test_core.py",
                suggestion="Add a case with accented words; the tokenizer uses \\w, which "
                "matches them, but nothing pins that.",
            ),
            ReviewFinding(
                severity="minor",
                category="performance",
                title="Sorting the whole dict on every call",
                file="textstats/core.py",
                line_start=41,
                line_end=44,
                suggestion="Return a Counter and sort only when the caller asks for top-N.",
            ),
            ReviewFinding(
                severity="info",
                category="docs",
                title="Docstring does not mention tie ordering",
                file="textstats/core.py",
                line_start=33,
                suggestion="State that ties are ordered alphabetically.",
            ),
        ],
        security_checklist=[
            ChecklistItem(item="Untrusted input handling", status="ok", note="Pure function."),
            ChecklistItem(
                item="Resource use on large input",
                status="concern",
                note="Builds a dict of every word; fine for files, not for unbounded streams.",
            ),
            ChecklistItem(item="Secrets in the diff", status="ok", note="None found."),
        ],
    )
    gate = GateResult(
        level="medium",
        scope="integration",
        number=1,
        checks=checks(),
        review=review,
        review_status="done",
    )
    write_quality(store, root, run_id, [gate])
    record_point(store, run_id, Point("D6", {"action": "ship"}, {"action": 0.93}))
    store.set_run_state(run_id, RunState.DONE)

    # Learning (D7).
    record_point(
        store,
        run_id,
        Point(
            "D7",
            {"learn": "learn", "worker": "api", "model": "qwen/qwen3-coder:free", "effort": "low"},
            {"learn": 0.89, "worker": 0.9, "model": 0.85, "effort": 0.93},
        ),
    )
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "learner",
            "api",
            "qwen/qwen3-coder:free",
            "low",
            0.0,
            "1 lesson added (apostrophes in tokens).",
            minutes=0.4,
        ),
    )
    store.set_run_learning_outcome(run_id, f"learned into {PROFILE}: 1 added, 1 updated, 0 retired")
    return run_id


@dataclass
class SimpleRun:
    """A shorter run: one or two tasks, straight through."""

    idea: str
    suffix: str
    ago: timedelta
    state: RunState
    tasks: list[tuple[str, str, str, str, float | None]]  # title, worker, model, file, cost
    quality: str = "none"
    reason: str | None = None
    planner_cost: float = 0.0


def seed_simple(store: Store, clock: Clock, root: Path, now: datetime, spec: SimpleRun) -> str:
    start = now - spec.ago
    run_id = run_id_at(start, spec.suffix)
    clock.set(start)
    cards = [
        TaskCard(
            id=f"T{i}",
            title=title,
            description=title + ".",
            files_hint=[file],
            depends_on=[f"T{i - 1}"] if i > 1 else [],
            acceptance_criteria=["pytest passes"],
            test_command="pytest -q",
        )
        for i, (title, _, _, file, _) in enumerate(spec.tasks, start=1)
    ]
    plan = Plan(summary=spec.idea + ".", tasks=cards)
    store.create_run(
        Idea(text=spec.idea, repo_path=root),
        learning_profile=PROFILE,
        quality_level=spec.quality,  # type: ignore[arg-type]
        run_id=run_id,
    )
    store.set_run_state(run_id, RunState.PLANNING)
    record_point(
        store,
        run_id,
        Point("D1", {"clear": "plan", "worker": "api", "effort": "medium"}, {"clear": 0.95}),
    )
    if spec.state is RunState.PLANNING:
        return run_id  # the planner is still writing the plan
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt(
            "planner",
            "api",
            "qwen/qwen3-coder:free",
            "medium",
            spec.planner_cost,
            f"Plan with {len(cards)} task(s).",
            minutes=1.0,
        ),
    )
    store.set_run_plan(run_id, plan, 1)
    store.upsert_tasks(run_id, plan)
    store.set_run_state(run_id, RunState.VALIDATING)
    record_point(store, run_id, Point("D2", {"worker": "codex", "effort": "low"}, {"worker": 0.8}))
    record_attempt(
        store,
        clock,
        root,
        run_id,
        Attempt("validator", "codex", "gpt-5-codex", "low", None, "Approve.", minutes=0.6),
    )
    store.set_run_validation(run_id, ValidationResult(verdict="approve"))
    record_point(store, run_id, Point("D3", {"verdict": "approve"}, {"verdict": 0.94}))
    if spec.state is RunState.AWAITING_APPROVAL:
        store.set_run_state(run_id, RunState.AWAITING_APPROVAL)
        return run_id
    store.set_run_state(run_id, RunState.EXECUTING)
    for i, (title, worker, model, file, cost) in enumerate(spec.tasks, start=1):
        task = f"T{i}"
        last = i == len(spec.tasks)
        record_point(
            store,
            run_id,
            Point(
                "D4",
                {"complexity": "standard", "worker": worker, "model": model, "effort": "medium"},
                {"complexity": 0.84, "worker": 0.87},
                task=task,
            ),
        )
        store.set_task_assignment(run_id, task, TaskState.RUNNING, worker, model, "medium")
        if last and spec.state is RunState.EXECUTING:
            # four minutes into the attempt: Fleet shows it as a working agent, not stale
            clock.set(now - timedelta(minutes=4))
            record_running_attempt(store, root, run_id, task, worker, model)
            return run_id  # still running
        failed = last and spec.state is RunState.NEEDS_HUMAN
        record_attempt(
            store,
            clock,
            root,
            run_id,
            Attempt(
                "coder",
                worker,
                model,
                "medium",
                cost,
                "Tests still fail on a 2 GB input (memory)." if failed else f"Done: {title}.",
                task=task,
                status="failure" if failed else "success",
                files=[file],
                minutes=2.5,
            ),
        )
        if failed:
            record_point(store, run_id, Point("D5", {"action": "human"}, {"action": 0.71}, task))
            store.set_task_state(run_id, task, TaskState.NEEDS_HUMAN, note=spec.reason)
            store.set_run_state(
                run_id, RunState.NEEDS_HUMAN, spec.reason, resume_state=RunState.EXECUTING
            )
            return run_id
        record_point(store, run_id, Point("D5", {"action": "done"}, {"action": 0.95}, task))
        store.set_task_state(run_id, task, TaskState.DONE)
    store.set_run_state(run_id, RunState.INTEGRATING)
    if spec.quality != "none":
        write_quality(
            store,
            root,
            run_id,
            [
                GateResult(
                    level=spec.quality,  # type: ignore[arg-type]
                    scope="integration",
                    checks=checks(),
                    review=QualityReview(summary="Small change; no problems found."),
                    review_status="done",
                )
            ],
        )
    record_point(store, run_id, Point("D6", {"action": "ship"}, {"action": 0.95}))
    store.set_run_state(run_id, RunState.DONE)
    store.set_run_learning_outcome(run_id, f"learned into {PROFILE}: 0 added, 1 updated, 0 retired")
    return run_id


# ---- shipped pull requests (`jev ship`, `jev kpi`, the KPIs page) ------------------------------

SHIP_REPO = "example/textstats"  # made up; nothing is pushed and GitHub is never called
# Per shipped PR, oldest first: (state, hours to merge or close, review comments, CI status,
# commits after open, reverted). Times and counts are illustrative.
SHIP_OUTCOMES: list[tuple[str, float | None, int, str, int, bool]] = [
    ("merged", 6.5, 2, "success", 0, False),
    ("merged", 3.0, 1, "success", 0, False),
    ("merged", 20.0, 4, "success", 1, False),
    ("closed", 30.0, 3, "failure", 0, False),
    ("merged", 5.0, 0, "success", 0, True),
    ("merged", 2.5, 1, "success", 0, False),
    ("merged", 9.0, 2, "success", 1, False),
    ("merged", 4.0, 1, "success", 0, False),
    ("open", None, 1, "pending", 0, False),
    ("open", None, 1, "success", 2, False),
]


def seed_shipped(store: Store, clock: Clock, now: datetime) -> None:
    """Ship the latest DONE runs as made-up PRs #12, #13, ...: a KPI snapshot per run (from
    the seeded data, as `jev ship` takes it) and GitHub data as `jev ship sync` would store."""
    done = sorted(
        (r for r in store.list_runs() if r.state is RunState.DONE), key=lambda r: r.created_at
    )[-len(SHIP_OUTCOMES) :]
    for n, (run, outcome) in enumerate(zip(done, SHIP_OUTCOMES, strict=False)):
        state, hours, comments, ci, after, reverted = outcome
        tasks = store.list_tasks(run.id)
        files = len({f for t in tasks for f in t.card.files_hint}) + 1
        diff = DiffStat(files=files, additions=24 + 37 * len(tasks) + 9 * n, deletions=3 + 2 * n)
        kpis = compute_run_kpis(store, run.id, diff)
        opened = run.updated_at + timedelta(minutes=12)
        clock.set(opened)
        number = 12 + n
        pr = store.add_shipped_pr(
            run.id,
            remote="origin",
            repo=SHIP_REPO,
            branch=f"jev/{run.id}",
            base="main",
            number=number,
            url=f"https://github.com/{SHIP_REPO}/pull/{number}",
            title=pr_title(run.idea.text),
            draft=True,
            head_oid=None,
            pushed_commits=len(tasks) + 1,
        )
        store.set_run_kpis(run.id, kpis.model_dump_json())
        ended = None if hours is None else min(opened + timedelta(hours=hours), now)
        clock.set(now - timedelta(minutes=5))
        store.update_shipped_pr(
            pr.id,
            state=state,  # type: ignore[arg-type]
            draft=state == "open" and n % 2 == 0,
            merged_at=ended if state == "merged" else None,
            closed_at=ended,
            review_comments=comments,
            reviews=1 + comments // 2,
            ci_status=ci,
            commits_after_open=after,
            additions=diff.additions,
            deletions=diff.deletions,
            changed_files=diff.files,
            reverted=reverted if state == "merged" else None,
            raw=None,
        )


# ---- learning and queue ------------------------------------------------------------------------

LESSONS: list[tuple[str, list[str], list[str], int, float]] = [
    (
        "Run `pytest -q` from the repo root; the tests import `textstats` as a package.",
        ["coder", "reviewer"],
        ["pytest", "textstats"],
        9,
        3.0,
    ),
    (
        "Plans for textstats should keep one function per task and name its test file.",
        ["planner"],
        ["plan", "textstats"],
        6,
        2.0,
    ),
    (
        "Keep inner apostrophes in tokens (don't, it's); split only on whitespace and "
        "punctuation around words.",
        ["coder"],
        ["tokenizer", "words", "apostrophes"],
        2,
        1.0,
    ),
    (
        "Pin tie ordering in tests (alphabetical) whenever a function returns counts.",
        ["coder", "validator"],
        ["tests", "ordering", "counts"],
        3,
        1.0,
    ),
    (
        "Date parsing tests must not depend on the local timezone; pass tzinfo=UTC.",
        ["coder"],
        ["dates", "timezone", "flaky"],
        1,
        1.0,
    ),
    (
        "Cache entries need an explicit TTL and a test that advances a fake clock.",
        ["planner", "coder"],
        ["cache", "ttl"],
        1,
        0.0,
    ),
]


def seed_learning(store: Store, clock: Clock, root: Path, start: datetime) -> None:
    config = load_config(root)
    learning = Learning.for_store(store, clock=clock)
    try:
        clock.set(start, step_s=3600 * 7)
        learning.create(PROFILE)
        db = learning.db_for(PROFILE)
        for text, roles, tags, uses, score in LESSONS:
            lesson = db.add_lesson(PROFILE, text, roles, tags)
            number = int(lesson.id[1:])
            for _ in range(uses):
                db.record_use(PROFILE, number, score / uses if uses else 0.0)
        learning.create("python-style")
        style = learning.db_for("python-style")
        style.add_lesson(
            "python-style",
            "Prefer pathlib over os.path in new code.",
            ["coder"],
            ["pathlib", "style"],
        )
        learning.set_enabled("python-style", False)
        use_profile(store, learning, PROFILE)
        enable_learning(store, config, learning)
    finally:
        learning.close()


QUEUE: list[tuple[str, str, bool, str | None, timedelta]] = [
    ("Add a top_n parameter to word_frequencies()", "high", False, "medium", timedelta(hours=1)),
    ("Support reading text from stdin in the CLI", "normal", False, None, timedelta(hours=5)),
    ("Document the textstats public API", "normal", True, None, timedelta(hours=20)),
    ("Benchmark the tokenizer on a 10 MB input", "low", False, None, timedelta(days=2)),
]


def seed_queue(store: Store, clock: Clock, now: datetime) -> None:
    learning = Learning.for_store(store, clock=clock)
    try:
        for idea, priority, plan_only, quality, ago in QUEUE:
            clock.set(now - ago)
            add_idea(store, learning, idea, priority, plan_only=plan_only, quality=quality)
    finally:
        learning.close()


# ---- config, tools and the model list ----------------------------------------------------------

DEMO_TOOLS = """
# Demo tools (examples/demo/seed_demo.py): variable NAMES only, never values.
[tools.mcp.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env_from = ["GITHUB_PERSONAL_ACCESS_TOKEN"]

[tools.mcp.docs]
url = "https://docs.example.com/mcp"
bearer_token_env = "DOCS_MCP_TOKEN"

[tools.skills]
house-style = ".claude/house-style"
"""

HOUSE_STYLE = """---
name: house-style
description: textstats house style for new code and tests (demo skill).
---

- Type-annotate public functions; keep them pure where possible.
- One behaviour per test; name tests after the behaviour.
"""


def _set_in_table(text: str, table: str, key: str, value: str) -> str:
    """Set `key = value` in `[table]` of a jev.toml written by `jev init` (adds the key if the
    table does not have it)."""
    head = f"[{table}]"
    start = text.index(head)
    end = text.find("\n[", start + len(head))
    end = len(text) if end == -1 else end + 1
    body = text[start:end]
    line = re.compile(rf"^{re.escape(key)} = .*$", re.MULTILINE)
    if line.search(body):
        body = line.sub(f"{key} = {value}", body, count=1)
    else:
        body = body.replace(head + "\n", f"{head}\n{key} = {value}\n", 1)
    return text[:start] + body + text[end:]


def cache_demo_models(root: Path) -> None:
    """Cache an OpenRouter model list built from the test fixture
    `tests/fixtures/openrouter/models.json`, so `jev models list --free` works offline in a
    demo repo. Unguarded: no repo trust needed."""
    body = json.loads(FIXTURE_MODELS.read_text(encoding="utf-8"))
    catalog = ModelCatalog(parse_models(body), time.time(), "https://openrouter.ai/api/v1/models")
    catalog.save(cache_path(root))


def seed_cache(store: Store, clock: Clock, now: datetime, run_id: str) -> None:
    """A few warm cache entries with hits, so the Tools page's Cache section (and
    the Spend page's "Saved by cache") show real numbers in the demo (issue #254).

    The clock is set to `now` first: entries created at the seed's older timestamps
    would already be past their TTL when the dashboard reads them with the real
    clock.
    """
    clock.set(now)
    entries = [
        ("decision", 0.42, 3),
        ("decision", 0.18, 1),
        ("decision", 0.09, 2),
        ("role", 0.31, 2),
    ]
    for i, (kind, cost, hits) in enumerate(entries):
        key_hash = hashlib.sha256(f"demo-cache-{kind}-{i}".encode()).hexdigest()
        key_json = json.dumps({"run_id": run_id, "demo": i})
        store.cache_put(key_hash, kind, key_json, "{}", cost, 168)
        for _ in range(hits):
            store.cache_get(key_hash)
    store.cache_shadow_log("demo-shadow-1", "decision", True, 0.99)
    store.cache_shadow_log("demo-shadow-2", "decision", True, 1.0)


def configure_demo(root: Path) -> None:
    """Demo settings in jev.toml, an example skill folder and an offline model list."""
    path = root / "jev.toml"
    text = path.read_text(encoding="utf-8")
    if not re.search(r"^\[tools\.mcp\.github\]", text, re.MULTILINE):
        edits = [
            ("dispatcher", "primary", '"rules"                  # demo: no key needed'),
            ("roles.planner", "workers", '["api", "claude_code"]'),
            ("roles.planner", "models", '["free", "openai/gpt-5-mini"]'),
            ("roles.planner", "mcp", '["github", "docs"]'),
            ("roles.coder", "skills", '["house-style"]'),
            ("roles.code_reviewer", "mcp", '["github"]'),
            ("ship", "enabled", "true                   # demo: PRs are made up, never opened"),
        ]
        for table, key, value in edits:
            text = _set_in_table(text, table, key, value)
        path.write_text(text.rstrip("\n") + "\n" + DEMO_TOOLS, encoding="utf-8")
    skill = root / ".claude" / "house-style"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(HOUSE_STYLE, encoding="utf-8")
    cache_demo_models(root)
    trust_demo(root)


def trust_demo(root: Path) -> None:
    """Trust the demo repo in the user config, so its `jev.toml` may name tools and skills. Only
    a user config inside the demo repo is written (set `XDG_CONFIG_HOME` under it, as
    docs/dashboard.md shows): never your own."""
    path = default_user_config_path()
    if not path.resolve().is_relative_to(root):
        raise SystemExit(
            f"refusing to edit {path}: set XDG_CONFIG_HOME to a directory inside {root} "
            "(see docs/dashboard.md, Screenshots)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if "[trust]" not in text:
        path.write_text(text + f'[trust]\nrepos = ["{root}"]\n', encoding="utf-8")


# ---- a saved chat ------------------------------------------------------------------------------

CHAT_SCRIPT: list[tuple[str, str]] = [
    (
        "Let callers ask word_frequencies() for just the most common words",
        "Two questions before I write the brief:\n\n"
        "1. Should it be a `top_n` argument on `word_frequencies()`, or a new function?\n"
        "2. Ties at the cut-off: keep today's alphabetical order, or return every tied word?\n\n"
        "I read `textstats/core.py`: results are already sorted by count, then word.",
    ),
    (
        "An optional top_n argument; keep alphabetical ties; top_n <= 0 raises ValueError",
        "Clear. `word_frequencies(text, top_n=None)` returns everything as today; with "
        "`top_n` it returns the first `top_n` items of the sorted result. Non-positive "
        "values raise `ValueError`. Tests: a cut-off inside a tie, `top_n` larger than the "
        "vocabulary, and the error. Type `/brief` when you are happy with that.",
    ),
]

CHAT_BRIEF = """# Add top_n to word_frequencies()

## Goal
Let callers ask `word_frequencies()` for only the most common words.

## Scope
- New optional argument `top_n: int | None = None` on `word_frequencies()`.
- `None` keeps today's behaviour; a positive `top_n` returns the first `top_n` items.
- `top_n <= 0` raises `ValueError`.

## Acceptance
- Ties at the cut-off follow the existing alphabetical order.
- Tests cover a cut-off inside a tie, `top_n` above the vocabulary size, and the error.
- README example updated.

## Assumptions
- No change to tokenization."""


class DemoChatBackend:
    """A canned chat model for the demo: replays `CHAT_SCRIPT` and `CHAT_BRIEF`. Never calls
    a model, a CLI or the network. `examples/demo/capture.py` uses it for `cli-chat.svg`."""

    keeps_session = False

    def __init__(self, name: BackendName = "api", model: str | None = CHAT_MODEL) -> None:
        self.name: BackendName = name
        self.model = model
        self.replies = {user: reply for user, reply in CHAT_SCRIPT}

    async def send(
        self,
        system: str,
        history: Sequence[Turn],
        message: str,
        *,
        session_id: str | None,
        budget_left_usd: float,
        log_path: Path,
    ) -> Reply:
        if message.endswith(BRIEF_REQUEST):
            return Reply(True, f"{BRIEF_OPEN}\n{CHAT_BRIEF}\n{BRIEF_CLOSE}", 0.0021)
        reply = self.replies.get(message, "Noted. Anything else, or shall I write the brief?")
        return Reply(True, reply, 0.0012)


def seed_chat(root: Path, clock: Clock, start: datetime) -> str:
    """One saved chat that ends with a brief."""
    clock.set(start, step_s=50)
    session = ChatSession.create(
        root,
        DemoChatBackend(),
        "(demo system prompt)",
        ChatLearning(profile=PROFILE),
        None,
        1.00,
        clock=clock,
        chat_id=f"c-{start:%Y%m%d-%H%M%S}-5e1d",
    )
    for user, _ in CHAT_SCRIPT:
        asyncio.run(session.say(user))
    asyncio.run(session.make_brief())
    return session.meta.chat_id


# ---- main --------------------------------------------------------------------------------------


def seed(root: Path, *, demo_config: bool = True) -> str:
    """Fill `root/.jev/` with illustrative demo state (runs, queue, learning, spend, shipped
    PRs, a saved chat and quality reports).

    `demo_config=True` (the screenshots pipeline) also applies the full demo configuration:
    `jev.toml` edits, an example skill folder and a trust entry in the user config (which must
    live inside the demo repo; `trust_demo` refuses anything else). `demo_config=False`
    (`jev init --demo`) seeds state only: the demo `jev.toml` dressing changes guarded keys,
    which would need the repo trusted in the user's own config, and a demo must work
    immediately without touching the user's config, so only the offline model list is cached.

    Returns the summary message (the caller prints it; library code stays quiet).
    """
    root = root.resolve()
    if not (root / "jev.toml").is_file() or not (root / STATE_DIRNAME).is_dir():
        raise SystemExit(f"{root} is not initialised: run `jev init` there first")
    if demo_config:
        configure_demo(root)
    else:
        cache_demo_models(root)
    now = datetime.now(UTC).replace(microsecond=0)
    clock = Clock()
    with Store.open(root / STATE_DIRNAME / "state.db", clock=clock) as store:
        seed_learning(store, clock, root, now - timedelta(days=16))
        older = [
            SimpleRun(
                "Set up pytest-cov and a coverage badge",
                "a1c4",
                timedelta(days=13, hours=3),
                RunState.DONE,
                [("Configure pytest-cov", "codex", "gpt-5-codex", "pyproject.toml", None)],
                planner_cost=0.04,
            ),
            SimpleRun(
                "Add type hints to textstats.core",
                "b27e",
                timedelta(days=11, hours=5),
                RunState.DONE,
                [("Annotate core functions", "claude_code", "sonnet", "textstats/core.py", 0.24)],
            ),
            SimpleRun(
                "Rename tokenize() to split_words() with a deprecation alias",
                "c90d",
                timedelta(days=9, hours=2),
                RunState.DONE,
                [
                    ("Rename and alias", "claude_code", "sonnet", "textstats/core.py", 0.31),
                    (
                        "Update callers and tests",
                        "codex",
                        "gpt-5-codex",
                        "tests/test_core.py",
                        None,
                    ),
                ],
                quality="basic",
            ),
            SimpleRun(
                "Add --json output to the CLI",
                "d4b2",
                timedelta(days=6, hours=4),
                RunState.DONE,
                [
                    ("JSON formatter", "claude_code", "sonnet", "textstats/cli.py", 0.42),
                    ("CLI tests", "claude_code", "haiku", "tests/test_cli.py", 0.06),
                ],
                quality="medium",
                planner_cost=0.02,
            ),
            SimpleRun(
                "Fix flaky date parsing test",
                "e815",
                timedelta(days=3, hours=6),
                RunState.DONE,
                [("Make the test timezone independent", "codex", "gpt-5-codex", "tests/", None)],
                quality="basic",
                planner_cost=0.03,
            ),
            SimpleRun(
                "Stream large files instead of reading them whole",
                "f3a0",
                timedelta(days=2, hours=1),
                RunState.NEEDS_HUMAN,
                [
                    ("Chunked reader", "claude_code", "sonnet", "textstats/io.py", 0.38),
                    (
                        "Wire the reader into the CLI",
                        "claude_code",
                        "opus",
                        "textstats/cli.py",
                        0.74,
                    ),
                ],
                reason="T2 needs a human: the 2 GB memory test still fails",
            ),
            SimpleRun(
                "Cache API responses for 10 minutes",
                "0b6c",
                timedelta(days=1, hours=2),
                RunState.DONE,
                [
                    ("TTL cache helper", "claude_code", "sonnet", "textstats/cache.py", 0.29),
                    (
                        "Use the cache in the client",
                        "codex",
                        "gpt-5-codex",
                        "textstats/api.py",
                        None,
                    ),
                ],
                quality="medium",
                planner_cost=0.01,
            ),
        ]
        early = [
            ("Add a CLI entry point: textstats FILE", 29, "cli.py", 0.46),
            ("Count characters with and without spaces", 27, "core.py", 0.18),
            ("Add average_word_length()", 24, "core.py", 0.22),
            ("Read files with an explicit encoding", 22, "io.py", 0.27),
            ("Add a GitHub Actions workflow for tests", 19, "ci.yml", 0.35),
            ("Handle empty input in every function", 16, "core.py", 0.15),
        ]
        for n, (idea, days, file, cost) in enumerate(early):
            older.insert(
                0,
                SimpleRun(
                    idea,
                    f"{0x5A10 + n * 0x111:04x}",
                    timedelta(days=days, hours=n + 2),
                    RunState.DONE,
                    [(idea, "claude_code", "sonnet", f"textstats/{file}", cost)],
                    planner_cost=0.01,
                ),
            )
        for spec in older:
            seed_simple(store, clock, root, now, spec)
        rich = seed_word_frequencies(store, clock, root, now - timedelta(hours=3, minutes=20))
        seed_simple(
            store,
            clock,
            root,
            now,
            SimpleRun(
                "Normalise unicode before counting words",
                "7d21",
                timedelta(minutes=35),
                RunState.EXECUTING,
                [
                    ("NFKC-normalise input", "claude_code", "sonnet", "textstats/core.py", 0.12),
                    (
                        "Tests with accented text",
                        "codex",
                        "gpt-5-codex",
                        "tests/test_core.py",
                        None,
                    ),
                ],
            ),
        )
        seed_simple(
            store,
            clock,
            root,
            now,
            SimpleRun(
                "Add sentence_count() that handles abbreviations",
                "92ee",
                timedelta(minutes=12),
                RunState.AWAITING_APPROVAL,
                [
                    ("sentence_count()", "claude_code", "sonnet", "textstats/core.py", None),
                    ("Abbreviation tests", "codex", "gpt-5-codex", "tests/test_core.py", None),
                ],
                planner_cost=0.05,
                quality="medium",
            ),
        )
        seed_simple(
            store,
            clock,
            root,
            now,
            SimpleRun(
                "Add a Flesch reading-ease score",
                "c3d8",
                timedelta(minutes=3),
                RunState.PLANNING,
                [("reading_ease()", "claude_code", "sonnet", "textstats/core.py", None)],
            ),
        )
        seed_queue(store, clock, now)
        seed_chat(root, clock, now - timedelta(hours=4, minutes=10))
        seed_shipped(store, clock, now)
        seed_cache(store, clock, now, rich)
    return f"Seeded demo state in {root / STATE_DIRNAME}; richest run: {rich}"


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit("usage: python examples/demo/seed_demo.py /path/to/initialised/repo")
    print(seed(Path(argv[1])))


if __name__ == "__main__":
    main(sys.argv)
