"""The quality gate in real pipeline runs: FakeWorker, a fake command runner, a real git repo."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jev_agent.cli.wiring import pipeline_settings
from jev_agent.config import JevConfig
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.models import Decision, DecisionRequest, Idea, Plan, RunState, TaskCard
from jev_agent.core.pipeline import Pipeline, build_pipeline
from jev_agent.core.store import Store
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.quality import tools
from jev_agent.roles.schemas import QualityReview, ReviewFinding
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.fake import FakeStep, FakeWorker, ScriptValue
from jev_agent.workers.registry import WorkerRegistry

ONE_TASK = Plan(
    summary="one task",
    tasks=[
        TaskCard(
            id="T1",
            title="add mod",
            description="add src/mod.py",
            files_hint=["src/mod.py"],
            acceptance_criteria=["exists"],
        )
    ],
)
CODER = FakeStep(files={"src/mod.py": "x = 1\n"})
RUFF = "ruff check --output-format=concise src/mod.py"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def _review(*findings: tuple[str, str, str]) -> FakeStep:
    return FakeStep(
        parsed=QualityReview(
            summary="review",
            findings=[
                ReviewFinding(severity=s, category=c, title=t, file="src/mod.py", line_start=1)  # type: ignore[arg-type]
                for s, c, t in findings
            ],
        )
    )


CLEAN = _review()
CRITICAL = _review(("critical", "bug", "divides by zero"))


@dataclass
class Env:
    repo: Path
    store: Store
    pipeline: Pipeline
    worker: FakeWorker
    commands: FakeCommandRunner

    def jobs(self, role: str, task_id: str | None = None) -> list[WorkerJob]:
        return [
            j
            for j in self.worker.jobs
            if j.role == role and (task_id is None or j.task_id == task_id)
        ]

    def decisions(self, run_id: str, point: str) -> list[tuple[dict[str, Any], str]]:
        return [
            (d.request.state, next(iter(d.decision.answers.values())))
            for d in self.store.list_decisions(run_id)
            if d.decision.point == point
        ]


class _AlwaysDone(RulesDispatcher):
    """Says `done` / `ship` no matter what: the harness must still respect the gate."""

    name = "always-done"

    async def decide(self, req: DecisionRequest) -> Decision:
        decision = await super().decide(req)
        if req.point == "D5":
            decision.answers["action"] = "done"
        if req.point == "D6" and decision.answers["action"] == "fix_task":
            decision.answers["action"] = "ship"
        return decision


@pytest.fixture
def python_repo(fixture_repo: Path) -> Path:
    (fixture_repo / "pyproject.toml").write_text('[project]\nname = "x"\n')
    subprocess.run(["git", "add", "-A"], cwd=fixture_repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "python"], cwd=fixture_repo, check=True)
    return fixture_repo


@pytest.fixture
def make_env(python_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    stores: list[Store] = []
    installed: set[str] = set()
    monkeypatch.setattr(tools, "which", lambda tool: f"/bin/{tool}" if tool in installed else None)

    def make(
        script: Mapping[str, ScriptValue],
        *,
        level: str = "basic",
        exit_codes: dict[str, list[int]] | None = None,
        tools_installed: tuple[str, ...] = (),
        max_fix_rounds: int = 2,
        dispatcher: Any = None,
        quality: dict[str, Any] | None = None,
    ) -> Env:
        installed.clear()
        installed.update(tools_installed)
        store = Store.open(python_repo / ".jev" / "state.db")
        stores.append(store)
        worker = FakeWorker(
            "fake", {"planner": FakeStep(parsed=ONE_TASK), "coder": CODER, **script}
        )
        registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
        commands = FakeCommandRunner(exit_codes)
        config = JevConfig.model_validate(
            {
                "approval": {"require_plan_approval": False},
                "quality": {"level": level, **(quality or {})},
                "integration": {"max_fix_rounds": max_fix_rounds},
            }
        )
        pipeline = build_pipeline(
            python_repo,
            config,
            store,
            registry,
            commands=commands,
            dispatcher=dispatcher,
            settings=pipeline_settings(config),
        )
        return Env(python_repo, store, pipeline, worker, commands)

    yield make
    for store in stores:
        store.close()


def _idea(repo: Path) -> Idea:
    return Idea(text="add a module", repo_path=repo)


async def test_level_none_runs_no_gate(make_env: Any) -> None:
    env: Env = make_env({}, level="none")
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE and run.quality_level == "none"
    assert env.jobs("code_reviewer") == []
    assert env.store.list_quality_reports(run.id) == []
    assert "quality" not in env.decisions(run.id, "D5")[0][0]


async def test_passing_gate_writes_reports(make_env: Any) -> None:
    env: Env = make_env({}, tools_installed=("ruff",))
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE and run.quality_level == "basic"
    reports = env.store.list_quality_reports(run.id)
    assert [(r.result.scope, r.result.label, r.result.blocked) for r in reports] == [
        ("task", "T1-1", False),
        ("integration", "integration-1", False),
    ]
    # ruff ran on the changed file in the task worktree and on the result branch
    ruff_calls = [cwd for cmd, cwd in env.commands.calls if cmd == RUFF]
    assert len(ruff_calls) == 2 and ruff_calls[0] != ruff_calls[1]
    quality = env.repo / ".jev" / "runs" / run.id / "quality"
    assert (quality / "T1-1.md").is_file() and (quality / "integration-1.md").is_file()
    review = (quality / "review.md").read_text()
    assert "# Quality review: run" in review and "every gate passed" in review
    assert [j.task_id for j in env.jobs("code_reviewer")] == ["T1", None]
    assert "src/mod.py" in env.jobs("code_reviewer")[0].prompt  # the diff
    d5 = env.decisions(run.id, "D5")[0][0]
    assert d5["quality"] == "passed" and "quality_failed" not in d5
    assert env.decisions(run.id, "D6")[0][0]["quality"] == "passed"


async def test_missing_tools_never_fail_the_run(make_env: Any) -> None:
    env: Env = make_env({}, level="scalable")
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE
    checks = env.store.list_quality_reports(run.id)[0].result.checks
    by_name = {c.name: c for c in checks}
    assert by_name["ruff"].status == "skipped" and "not installed" in by_name["ruff"].detail
    assert by_name["secrets"].status == "pass"  # the built-in scan needs no tool


async def test_required_missing_tool_blocks(make_env: Any) -> None:
    env: Env = make_env({}, quality={"required_tools": ["ruff"]})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.FAILED  # 3 attempts, each blocked
    reasons = env.store.list_quality_reports(run.id)[-1].result.reasons
    assert any("ruff failed" in r and "required_tools" in r for r in reasons)


async def test_blocked_task_retries_with_findings(make_env: Any) -> None:
    env: Env = make_env(
        {"code_reviewer:T1:1": CRITICAL, "code_reviewer": CLEAN},
        tools_installed=("ruff",),
        exit_codes={RUFF: [1, 0]},
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE
    (state1, action1), (state2, action2) = env.decisions(run.id, "D5")
    assert state1["quality_failed"] is True and action1 == "retry"
    assert any("divides by zero" in f for f in state1["quality_findings"])
    assert any("ruff failed" in f for f in state1["quality_findings"])
    assert state2["quality"] == "passed" and action2 == "done"
    first, second = env.jobs("coder", "T1")
    assert "Quality gate findings to fix" not in first.prompt
    assert "Quality gate findings to fix" in second.prompt
    assert "divides by zero" in second.prompt and "The quality gate blocked it" in second.prompt
    task = env.store.get_task(run.id, "T1")
    assert task.attempts == 2


async def test_rules_escalate_when_the_gate_keeps_blocking(make_env: Any) -> None:
    env: Env = make_env({"code_reviewer": CRITICAL})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.FAILED
    assert [a for _, a in env.decisions(run.id, "D5")] == ["retry", "escalate", "reassign"]


async def test_dispatcher_cannot_wave_a_blocked_task_through(make_env: Any) -> None:
    env: Env = make_env(
        {"code_reviewer:T1:1": CRITICAL, "code_reviewer": CLEAN}, dispatcher=_AlwaysDone()
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE
    assert env.store.get_task(run.id, "T1").attempts == 2  # "done" became a retry


async def test_policy_by_level_in_a_run(make_env: Any) -> None:
    major_security = _review(("major", "security", "path traversal"))
    env: Env = make_env({"code_reviewer": major_security}, level="medium")
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE  # reported only at medium
    env = make_env({"code_reviewer": major_security}, level="production")
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.FAILED  # blocks at production


async def test_integration_gate_adds_a_fix_task(make_env: Any) -> None:
    env: Env = make_env(
        {"code_reviewer:T1": CLEAN, "code_reviewer:None": [CRITICAL, CLEAN], "code_reviewer": CLEAN}
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE
    (state1, action1), (_, action2) = env.decisions(run.id, "D6")
    assert state1["quality_failed"] is True and action1 == "fix_task"
    assert action2 == "ship"
    fix = env.store.get_task(run.id, "T2").card
    assert "quality gate" in fix.title and "divides by zero" in fix.description
    labels = [r.result.label for r in env.store.list_quality_reports(run.id)]
    assert labels == ["T1-1", "integration-1", "T2-1", "integration-2"]


async def test_integration_gate_stops_after_max_fix_rounds(make_env: Any) -> None:
    env: Env = make_env(
        {"code_reviewer:None": CRITICAL, "code_reviewer": CLEAN},
        max_fix_rounds=0,
        dispatcher=_AlwaysDone(),
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.NEEDS_HUMAN
    assert run.reason is not None and "quality gate still blocks" in run.reason


async def test_run_override_beats_config(make_env: Any) -> None:
    env: Env = make_env({}, level="production")
    run = await env.pipeline.start(_idea(env.repo), quality="none")
    assert run.quality_level == "none" and env.jobs("code_reviewer") == []
