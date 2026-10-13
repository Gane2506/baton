"""Dependent task bases, integration, fix tasks and worktree cleanup, end to end.

FakeWorker + RulesDispatcher + scripted test commands against the real fixture git repo.
"""

import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jev_agent.config import JevConfig
from jev_agent.core.commands import CommandResult, FakeCommandRunner
from jev_agent.core.integration import FIX_TITLE, FailedCommand, fix_task_card
from jev_agent.core.models import Idea, Plan, RunState, TaskCard, TaskState
from jev_agent.core.pipeline import Pipeline, PipelineSettings, build_pipeline
from jev_agent.core.store import Store
from jev_agent.core.workspace import GitWorkspace, WorkspaceError
from jev_agent.workers.fake import FakeStep, FakeWorker, FakeWorkerCrash, ScriptValue
from jev_agent.workers.registry import WorkerRegistry

SECRET = "sk-abcdefghijklmnop1234"


def _card(tid: str, deps: list[str] | None = None, test: str | None = None) -> TaskCard:
    return TaskCard(
        id=tid,
        title=f"task {tid}",
        description=f"do {tid}",
        files_hint=[f"src/{tid.lower()}.py", f"tests/test_{tid.lower()}.py"],
        depends_on=deps or [],
        acceptance_criteria=["a", "b"],
        test_command=test,
    )


def _plan(*cards: TaskCard) -> Plan:
    return Plan(summary="m6 plan", tasks=list(cards))


class _Commands(FakeCommandRunner):
    """Scripted exit codes; output mentions a secret; can crash on a chosen call."""

    def __init__(self, exit_codes: dict[str, list[int]], crash_on_call: int | None = None):
        super().__init__(exit_codes)
        self.crash_on_call = crash_on_call

    async def run(self, command: str, cwd: Path, log_path: Path, timeout_s: int) -> CommandResult:
        if self.crash_on_call is not None and len(self.calls) + 1 == self.crash_on_call:
            raise FakeWorkerCrash(f"crashed while running {command}")
        result = await super().run(command, cwd, log_path, timeout_s)
        return CommandResult(result.exit_code, f"{result.output}\nAuthorization: {SECRET}")


@dataclass
class Env:
    repo: Path
    store: Store
    pipeline: Pipeline
    worker: FakeWorker
    commands: _Commands

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=True
        ).stdout

    def files(self, ref: str) -> set[str]:
        return set(self.git("ls-tree", "-r", "--name-only", ref).split())

    def branches(self, run_id: str) -> set[str]:
        out = self.git("for-each-ref", "--format=%(refname:short)", f"refs/heads/jev/{run_id}")
        return {b.removeprefix(f"jev/{run_id}/") for b in out.split()}

    def worktrees(self, run_id: str) -> Path:
        return self.repo / ".jev" / "worktrees" / run_id

    def actions(self, run_id: str, point: str) -> list[str]:
        return [
            next(iter(d.decision.answers.values()))
            for d in self.store.list_decisions(run_id)
            if d.decision.point == point
        ]


@pytest.fixture
def make_env(fixture_repo: Path) -> Iterator[Any]:
    stores: list[Store] = []

    def make(
        script: Mapping[str, ScriptValue],
        exit_codes: dict[str, list[int]] | None = None,
        settings: PipelineSettings | None = None,
        crash_on_call: int | None = None,
    ) -> Env:
        store = Store.open(fixture_repo / ".jev" / "state.db")
        stores.append(store)
        worker = FakeWorker("fake", script)
        registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
        commands = _Commands(exit_codes or {}, crash_on_call)
        config = JevConfig.model_validate({"approval": {"require_plan_approval": False}})
        pipeline = build_pipeline(
            fixture_repo, config, store, registry, commands=commands, settings=settings
        )
        return Env(fixture_repo, store, pipeline, worker, commands)

    yield make
    for store in stores:
        store.close()


def _idea(repo: Path) -> Idea:
    return Idea(text="build things", repo_path=repo)


SUITE = PipelineSettings(integration_test_command="pytest -q", test_timeout_s=30)


async def test_dependent_task_branches_from_its_dependencies(make_env: Any) -> None:
    plan = _plan(_card("T1"), _card("T2"), _card("T3", deps=["T2", "T1"]))
    env: Env = make_env({"planner": FakeStep(parsed=plan)})
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    # T3 started from T1 and T2 merged; T1 and T2 did not see each other.
    assert {"jev_fake_T1.txt", "jev_fake_T2.txt", "jev_fake_T3.txt"} <= env.files(
        f"jev/{run.id}/T3"
    )
    assert "jev_fake_T1.txt" not in env.files(f"jev/{run.id}/T2")
    t3_job = next(j for j in env.worker.jobs if j.task_id == "T3")
    assert t3_job.workdir == env.worktrees(run.id) / "T3"


async def test_base_merge_conflict_needs_human(make_env: Any) -> None:
    plan = _plan(_card("T1"), _card("T2"), _card("T3", deps=["T1", "T2"]))
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=plan),
            "coder:T1": FakeStep(files={"shared.txt": "from T1\n"}),
            "coder:T2": FakeStep(files={"shared.txt": "from T2\n"}),
        }
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.NEEDS_HUMAN
    assert "T3" in (run.reason or "") and "shared.txt" in (run.reason or "")
    t3 = env.store.get_task(run.id, "T3")
    assert t3.state is TaskState.NEEDS_HUMAN
    assert t3.attempts == 0  # no attempt spent
    assert t3.note is not None and "shared.txt" in t3.note
    assert "base-T3" not in env.branches(run.id)  # the half-built base was removed
    assert (env.worktrees(run.id) / "T1").is_dir()  # kept for inspection


async def test_result_branch_has_every_task_and_grows_from_the_start_commit(
    make_env: Any,
) -> None:
    plan = _plan(_card("T1"), _card("T2", deps=["T1"]), _card("T3", deps=["T1"]))
    crashing: Env = make_env({"planner": FakeStep(parsed=plan), "coder:T3:1": FakeStep(crash=True)})
    with pytest.raises(FakeWorkerCrash):
        await crashing.pipeline.start(_idea(crashing.repo))
    run_id = crashing.store.list_runs()[0].id

    # The user commits on main while the run is interrupted.
    (crashing.repo / "later.txt").write_text("later\n")
    crashing.git("add", "later.txt")
    crashing.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "later")

    fresh: Env = make_env({"planner": FakeStep(parsed=plan)})
    run = await fresh.pipeline.resume(run_id)

    assert run.state is RunState.DONE, run.reason
    result = fresh.files(f"jev/{run_id}/result")
    assert {"jev_fake_T1.txt", "jev_fake_T2.txt", "jev_fake_T3.txt", "README.md"} <= result
    assert "later.txt" not in result  # every branch grows from the recorded start commit


async def test_integration_conflict_needs_human(make_env: Any) -> None:
    plan = _plan(_card("T1"), _card("T2"))
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=plan),
            "coder:T1": FakeStep(files={"shared.txt": "from T1\n"}),
            "coder:T2": FakeStep(files={"shared.txt": "from T2\n"}),
        },
        settings=SUITE,
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.NEEDS_HUMAN
    assert "T2 do not merge cleanly" in (run.reason or "")
    d6 = [d for d in env.store.list_decisions(run.id) if d.decision.point == "D6"]
    assert d6[-1].request.state["conflicts"] == ["T2"]
    assert d6[-1].decision.answers == {"action": "human"}
    assert env.commands.calls == []  # no tests on a half-merged result


async def test_failing_integration_tests_add_a_fix_task(make_env: Any) -> None:
    plan = _plan(_card("T1", test="check T1"))
    env: Env = make_env(
        {"planner": FakeStep(parsed=plan)}, exit_codes={"pytest -q": [1, 0]}, settings=SUITE
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    assert env.actions(run.id, "D6") == ["fix_task", "ship"]
    _, fix = env.store.list_tasks(run.id)
    assert fix.id == "T2" and fix.state is TaskState.DONE
    assert fix.card.title.startswith(FIX_TITLE)
    assert fix.card.depends_on == ["T1"]
    assert fix.card.test_command == "pytest -q"
    assert "pytest -q" in fix.card.description and "exit code 1" in fix.card.description
    assert SECRET not in fix.card.description and "[REDACTED]" in fix.card.description
    assert run.plan is not None and [t.id for t in run.plan.tasks] == ["T1", "T2"]
    # The fix task's worktree started from T1's work; the result has both.
    assert "jev_fake_T1.txt" in env.files(f"jev/{run.id}/T2")
    assert {"jev_fake_T1.txt", "jev_fake_T2.txt"} <= env.files(f"jev/{run.id}/result")
    assert [c for c, _ in env.commands.calls] == [
        "check T1",  # T1's own test
        "check T1",  # integration 1
        "pytest -q",  # integration 1: fails
        "pytest -q",  # the fix task's own test
        "check T1",  # integration 2
        "pytest -q",
    ]
    logs = env.repo / ".jev" / "runs" / run.id / "run"
    assert (logs / "integration-1-test-2.log").exists()
    assert (logs / "integration-2-test-2.log").exists()


async def test_fix_rounds_are_bounded(make_env: Any) -> None:
    env: Env = make_env(
        {"planner": FakeStep(parsed=_plan(_card("T1")))},
        # integration fails, fix task passes its own test, integration fails, ...
        exit_codes={"pytest -q": [1, 0, 1, 0, 1]},
        settings=PipelineSettings(integration_test_command="pytest -q", max_fix_rounds=2),
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.NEEDS_HUMAN
    assert "after 2 fix round(s)" in (run.reason or "")
    assert [t.id for t in env.store.list_tasks(run.id)] == ["T1", "T2", "T3"]
    assert env.store.get_task(run.id, "T3").card.depends_on == ["T1", "T2"]
    assert env.actions(run.id, "D6") == ["fix_task", "fix_task", "human"]
    assert (env.worktrees(run.id) / "_result").is_dir()  # kept for inspection


async def test_cleanup_on_done_keeps_branches(make_env: Any) -> None:
    plan = _plan(_card("T1"), _card("T2", deps=["T1"]))
    env: Env = make_env({"planner": FakeStep(parsed=plan)})
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    assert not env.worktrees(run.id).exists()
    assert env.branches(run.id) == {"T1", "T2", "result"}  # base-T2 is gone
    assert env.git("worktree", "list", "--porcelain").count("worktree ") == 1
    workspace = GitWorkspace(env.repo, env.repo / ".jev")
    workspace.cleanup(run.id)  # idempotent
    assert env.branches(run.id) == {"T1", "T2", "result"}


async def test_worktrees_kept_on_failure(make_env: Any) -> None:
    env: Env = make_env(
        {"planner": FakeStep(parsed=_plan(_card("T1"))), "coder": FakeStep(status="failure")}
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.FAILED
    assert (env.worktrees(run.id) / "T1").is_dir()


async def test_resume_after_crash_in_fix_task(make_env: Any) -> None:
    script: dict[str, ScriptValue] = {"planner": FakeStep(parsed=_plan(_card("T1")))}
    codes = {"pytest -q": [1, 0]}
    crashing: Env = make_env({**script, "coder:T2:1": FakeStep(crash=True)}, codes, SUITE)
    with pytest.raises(FakeWorkerCrash):
        await crashing.pipeline.start(_idea(crashing.repo))
    run_id = crashing.store.list_runs()[0].id
    assert crashing.store.get_run(run_id).state is RunState.EXECUTING
    assert crashing.store.get_task(run_id, "T2").state is TaskState.RUNNING

    fresh: Env = make_env(script, {"pytest -q": [0]}, SUITE)
    run = await fresh.pipeline.resume(run_id)
    assert run.state is RunState.DONE, run.reason
    assert fresh.actions(run_id, "D6") == ["fix_task", "ship"]


async def test_resume_with_fix_task_added_but_not_started(make_env: Any) -> None:
    script: dict[str, ScriptValue] = {"planner": FakeStep(parsed=_plan(_card("T1")))}
    crashing: Env = make_env(
        {**script, "coder:T2:1": FakeStep(crash=True)}, {"pytest -q": [1, 0]}, SUITE
    )
    with pytest.raises(FakeWorkerCrash):
        await crashing.pipeline.start(_idea(crashing.repo))
    run_id = crashing.store.list_runs()[0].id
    # As if jev died after adding the fix task but before leaving INTEGRATING.
    crashing.store.set_task_state(run_id, "T2", TaskState.PENDING)
    crashing.store.set_run_state(run_id, RunState.INTEGRATING)

    fresh: Env = make_env(script, {"pytest -q": [0]}, SUITE)
    run = await fresh.pipeline.resume(run_id)
    assert run.state is RunState.DONE, run.reason
    assert [t.id for t in fresh.store.list_tasks(run_id)] == ["T1", "T2"]  # no second fix task


async def test_resume_after_crash_during_integration_tests(make_env: Any) -> None:
    script: dict[str, ScriptValue] = {"planner": FakeStep(parsed=_plan(_card("T1", test="check")))}
    # Calls: T1's own test (1), then integration "check" (2) crashes.
    crashing: Env = make_env(script, settings=SUITE, crash_on_call=2)
    with pytest.raises(FakeWorkerCrash):
        await crashing.pipeline.start(_idea(crashing.repo))
    run_id = crashing.store.list_runs()[0].id
    assert crashing.store.get_run(run_id).state is RunState.INTEGRATING

    fresh: Env = make_env(script, settings=SUITE)
    run = await fresh.pipeline.resume(run_id)
    assert run.state is RunState.DONE, run.reason
    assert [c for c, _ in fresh.commands.calls] == ["check", "pytest -q"]
    assert not fresh.worktrees(run_id).exists()


def test_fix_task_card_numbers_after_the_highest_id() -> None:
    cards = [_card("T1"), _card("T7", deps=["T1"])]
    failure = FailedCommand("make test", CommandResult(2, "x" * 5000 + "boom"))
    card = fix_task_card(cards, failure)
    assert card.id == "T8"
    assert card.depends_on == ["T1", "T7"]
    assert card.description.endswith("boom")
    assert len(card.description) < 2_500  # only the output's tail


def test_cleanup_refuses_unsafe_run_ids(fixture_repo: Path) -> None:
    workspace = GitWorkspace(fixture_repo, fixture_repo / ".jev")
    for bad in ("../repo", "a/b", ""):
        with pytest.raises(WorkspaceError, match="unsafe run id"):
            workspace.cleanup(bad)
