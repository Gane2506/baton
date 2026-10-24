"""Regression tests for issue #201 (core-main swarm review gaps).

1. D6 "ship" never ships a conflicted result or one with failed integration tests.
2. The quota ladder terminates: free -> paid -> exactly one other-worker try
   (issue #201's literal fix is corrected here — see the section comment).
3. Role cache entries are never served or stored when the repo fingerprint is
   "unknown".
4. `commit_task` never commits secret-looking files (`.env`, private keys, ...).
5. Invariant failures in `pipeline.py`/`tasks.py` raise `Halt` (FAILED), never
   `AssertionError` (cf. issue #174).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from jev_agent.config.schema import JevConfig
from jev_agent.core.assign import Assignment
from jev_agent.core.cache import (
    UNKNOWN_REPO_FINGERPRINT,
    Cache,
    role_cache_key,
)
from jev_agent.core.commands import CommandResult
from jev_agent.core.harness import Halt, Harness
from jev_agent.core.integration import FailedCommand, IntegrationOutcome
from jev_agent.core.models import Idea, Plan, RunState, TaskCard, TaskState
from jev_agent.core.pipeline import Pipeline
from jev_agent.core.store import Store
from jev_agent.core.tasks import TaskRunner
from jev_agent.core.workspace import GitWorkspace, IntegrationResult
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "state.db")
    yield s
    s.close()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _plan_with_task() -> Plan:
    return Plan(
        summary="s",
        tasks=[
            TaskCard(id="T1", title="t", description="d", acceptance_criteria=["done"]),
        ],
    )


def _integrating_run(store: Store, *, with_plan: bool = True) -> str:
    """A run in INTEGRATING with T1 DONE (and a plan, unless asked otherwise)."""
    run = store.create_run(Idea(text="add search", repo_path=Path(".")))
    plan = _plan_with_task()
    if with_plan:
        store.set_run_plan(run.id, plan, 1)
    store.upsert_tasks(run.id, plan)
    store.set_task_state(run.id, "T1", TaskState.DONE)
    store.set_run_state(run.id, RunState.INTEGRATING)
    return run.id


def _ship_pipeline(store: Store) -> Pipeline:
    """A pipeline whose D6 always votes "ship" — the vote the hard limits must
    override. Only `decide`/`check_running`/`config` are touched on these paths."""
    harness = SimpleNamespace(
        store=store,
        config=JevConfig(),
        decide=_ship_decide,
        check_running=lambda run_id: None,
        workspace=Mock(),
    )
    return Pipeline(harness, None)  # type: ignore[arg-type]


async def _ship_decide(run_id: str, task_id: str | None, req: Any) -> Any:
    return SimpleNamespace(answers={"action": "ship"})


def _result(*, conflicts: list[str] | None = None, workdir: Path) -> IntegrationResult:
    return IntegrationResult(
        branch="jev/r/result",
        workdir=workdir,
        merged=[] if conflicts else ["T1"],
        conflicts=conflicts or [],
    )


# ---- 1. pipeline never ships conflicts / failed tests ------------------------------


async def test_d6_ship_vote_with_conflicts_stops_for_a_human(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _integrating_run(store)
    pipeline = _ship_pipeline(store)

    async def _conflicted(*args: Any, **kwargs: Any) -> IntegrationOutcome:
        return IntegrationOutcome(_result(conflicts=["T1"], workdir=tmp_path), "skipped")

    monkeypatch.setattr("jev_agent.core.pipeline.run_integration", _conflicted)
    with pytest.raises(Halt) as excinfo:
        await pipeline._integrate(store.get_run(run_id))
    assert excinfo.value.state is RunState.NEEDS_HUMAN
    assert "T1" in excinfo.value.reason and "merge cleanly" in excinfo.value.reason
    # No fix task was invented for an unfixable conflict.
    assert [t.card.id for t in store.list_tasks(run_id)] == ["T1"]


async def test_d6_ship_vote_with_failed_tests_gets_a_fix_task(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _integrating_run(store)
    pipeline = _ship_pipeline(store)

    async def _failed_tests(*args: Any, **kwargs: Any) -> IntegrationOutcome:
        return IntegrationOutcome(
            _result(workdir=tmp_path),
            "failed",
            FailedCommand("pytest -q", CommandResult(exit_code=1, output="FAILED")),
        )

    monkeypatch.setattr("jev_agent.core.pipeline.run_integration", _failed_tests)
    await pipeline._integrate(store.get_run(run_id))
    assert store.get_run(run_id).state is RunState.EXECUTING
    cards = [t.card for t in store.list_tasks(run_id)]
    assert [c.id for c in cards] == ["T1", "T2"]
    assert cards[1].kind == "fix"


async def test_d6_ship_vote_with_clean_passing_result_still_ships(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _integrating_run(store)
    pipeline = _ship_pipeline(store)

    async def _passing(*args: Any, **kwargs: Any) -> IntegrationOutcome:
        return IntegrationOutcome(_result(workdir=tmp_path), "passed")

    monkeypatch.setattr("jev_agent.core.pipeline.run_integration", _passing)
    await pipeline._integrate(store.get_run(run_id))
    # SHIPPING, AWAITING_SHIP or DONE depending on ship config — but never a
    # NEEDS_HUMAN stop and never a fix task.
    assert store.get_run(run_id).state in {
        RunState.SHIPPING,
        RunState.AWAITING_SHIP,
        RunState.DONE,
    }
    assert [t.card.id for t in store.list_tasks(run_id)] == ["T1"]


# ---- 2. the quota ladder is bounded (issue #201's item 2, corrected) ---------------
#
# Issue #201 proposed testing `current.model` in the fallback loop, which would
# make a paid fallback's exhaustion terminal at once. That contradicts the
# documented ladder (docs/config.md "Free models": next free model, then the
# first paid model, then one other worker) and the pinned
# `test_planner_falls_back_free_then_paid_then_other_worker` sequence — and the
# reassigned worker runs a different vendor's model, not the exhausted paid key.
# What the loop must guarantee instead, and what this pins, is that the ladder
# TERMINATES: free -> paid -> exactly one other-worker try, then the exhausted
# output returns even if every step reports quota_exhausted. (An originally-paid
# assignment never enters the ladder at all; see
# `test_planner_paid_quota_is_not_retried` in test_free_models.py.)


async def test_quota_ladder_stops_after_the_one_other_worker_try(
    store: Store, fixture_repo: Path
) -> None:
    config = JevConfig.model_validate(
        {
            "roles": {
                "planner": {
                    "workers": ["api", "claude_code"],
                    "models": ["vendor/free-model:free", "vendor/paid-model"],
                }
            }
        }
    )
    quota = FakeStep(status="quota_exhausted", summary="out of quota")
    worker = FakeWorker("fake", {"planner": [quota, quota, quota, quota]})
    registry = WorkerRegistry({"api": worker, "claude_code": worker})
    harness = Harness(
        store=store,
        config=config,
        repo=fixture_repo,
        state_dir=fixture_repo / ".jev",
        dispatcher=GuardedDispatcher(RulesDispatcher(), None),
        workers=registry,
        workspace=Mock(),
        commands=Mock(),
    )
    run = store.create_run(Idea(text="add search", repo_path=fixture_repo))
    output = await harness.run_worker_with_fallback(
        run_id=run.id,
        task_id=None,
        role="planner",
        assignment=Assignment("api", "vendor/free-model:free", None),
        prompt="do",
        workdir=fixture_repo,
    )
    assert output.result.status == "quota_exhausted"
    # Free try, paid try, one other-worker try — and no fourth call, even though
    # the script still has quota steps left.
    assert len(worker.jobs) == 3
    assert [job.model for job in worker.jobs] == [
        "vendor/free-model:free",
        "vendor/paid-model",
        None,  # the reassigned worker's default model (its first runnable one)
    ]


# ---- 3. unknown repo fingerprint disables the role cache ----------------------------


def test_role_cache_refuses_unknown_fingerprint(store: Store, tmp_path: Path) -> None:
    config = JevConfig()
    cache = Cache(store, config.cache, tmp_path)
    key = role_cache_key(
        role="planner",
        worker="api",
        model=None,
        effort=None,
        prompt="do x",
        workdir=tmp_path,  # not a git repo: fingerprint is "unknown"
        repo=tmp_path,
        template=None,
        lesson_texts=[],
        role_config_json="{}",
        config=config,
        run_id="r",
    )
    assert json.loads(key.key_json)["repo_fp"] == UNKNOWN_REPO_FINGERPRINT
    cache.put(key, "{}", 0.1, run_id="r")
    assert store.cache_stats().entries == 0  # nothing stored
    # Even an entry written before the refusal (same hash) is never served.
    store.cache_put(key.key_hash, "role", key.key_json, "{}", 0.1, 168)
    assert cache.get(key) is None


def test_role_cache_still_works_with_a_real_fingerprint(store: Store, fixture_repo: Path) -> None:
    config = JevConfig()
    cache = Cache(store, config.cache, fixture_repo)
    key = role_cache_key(
        role="planner",
        worker="api",
        model=None,
        effort=None,
        prompt="do x",
        workdir=fixture_repo,
        repo=fixture_repo,
        template=None,
        lesson_texts=[],
        role_config_json="{}",
        config=config,
        run_id="r",
    )
    assert json.loads(key.key_json)["repo_fp"] != UNKNOWN_REPO_FINGERPRINT
    cache.put(key, "{}", 0.1, run_id="r")
    assert cache.get(key) is not None


# ---- 4. commit_task never commits secret-looking files ------------------------------


def test_commit_task_skips_secret_files_but_commits_the_rest(fixture_repo: Path) -> None:
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev")
    workdir = ws.task_workdir("r1", "T1")
    (workdir / "app.py").write_text("x = 1\n")
    (workdir / ".env").write_text("API_KEY=fake-secret\n")
    (workdir / ".env.local").write_text("API_KEY=fake-secret\n")
    (workdir / "id_rsa").write_text("fake-private-key\n")
    (workdir / "key.pem").write_text("fake-private-key\n")
    assert ws.commit_task(workdir, "jev: T1") is True
    committed = _git(workdir, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert committed == ["app.py"]
    # Skipped files are still in the worktree, untracked — nothing was deleted.
    status = _git(workdir, "status", "--porcelain")
    for name in (".env", ".env.local", "id_rsa", "key.pem"):
        assert f"?? {name}" in status


def test_commit_task_with_only_secret_files_commits_nothing(fixture_repo: Path) -> None:
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev")
    workdir = ws.task_workdir("r1", "T1")
    (workdir / ".env").write_text("API_KEY=fake-secret\n")
    before = _git(workdir, "rev-list", "--count", "HEAD")
    assert ws.commit_task(workdir, "jev: T1") is False
    assert _git(workdir, "rev-list", "--count", "HEAD") == before


def test_commit_task_skips_modification_of_a_tracked_secret_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / ".gitignore").write_text(".jev/\n")
    (repo / "app.py").write_text("x = 1\n")
    (repo / ".env").write_text("API_KEY=old-fake\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )
    ws = GitWorkspace(repo, repo / ".jev")
    workdir = ws.task_workdir("r1", "T1")
    (workdir / "app.py").write_text("x = 2\n")
    (workdir / ".env").write_text("API_KEY=new-fake\n")
    assert ws.commit_task(workdir, "jev: T1") is True
    committed = _git(workdir, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert committed == ["app.py"]
    # The secret modification stays in the worktree, uncommitted (unstaged " M";
    # `_git` strips the leading space, so both spellings are accepted).
    status_lines = _git(workdir, "status", "--porcelain").splitlines()
    assert any(line in (" M .env", "M .env") for line in status_lines)


# ---- 5. invariant failures are Halt, never AssertionError ---------------------------


async def test_validate_without_a_plan_halts_failed(store: Store) -> None:
    run = store.create_run(Idea(text="add search", repo_path=Path(".")))
    pipeline = Pipeline(SimpleNamespace(store=store, config=JevConfig()), None)  # type: ignore[arg-type]
    with pytest.raises(Halt) as excinfo:
        await pipeline._validate(store.get_run(run.id))
    assert excinfo.value.state is RunState.FAILED


async def test_design_without_a_plan_halts_failed(store: Store) -> None:
    run = store.create_run(Idea(text="add search", repo_path=Path(".")))
    pipeline = Pipeline(SimpleNamespace(store=store, config=JevConfig()), None)  # type: ignore[arg-type]
    with pytest.raises(Halt) as excinfo:
        await pipeline._design(store.get_run(run.id))
    assert excinfo.value.state is RunState.FAILED


async def test_attempt_without_an_assignment_halts_failed(store: Store) -> None:
    run = store.create_run(Idea(text="add search", repo_path=Path(".")))
    plan = _plan_with_task()
    store.set_run_plan(run.id, plan, 1)
    store.upsert_tasks(run.id, plan)
    store.set_task_state(run.id, "T1", TaskState.DISPATCHED)  # no assignment recorded
    harness = SimpleNamespace(store=store, config=JevConfig(), check_budget=lambda *a: None)
    runner = TaskRunner(harness, 60)  # type: ignore[arg-type]
    with pytest.raises(Halt) as excinfo:
        await runner._attempt(run.id, store.get_task(run.id, "T1"))
    assert excinfo.value.state is RunState.FAILED
    assert store.get_task(run.id, "T1").state is TaskState.FAILED


def test_coder_prompt_without_a_plan_halts_failed(store: Store) -> None:
    run = store.create_run(Idea(text="add search", repo_path=Path(".")))
    plan = _plan_with_task()
    store.upsert_tasks(run.id, plan)
    harness = SimpleNamespace(store=store, config=JevConfig())
    runner = TaskRunner(harness, 60)  # type: ignore[arg-type]
    with pytest.raises(Halt) as excinfo:
        runner._coder_prompt(run.id, store.get_task(run.id, "T1"), 1)
    assert excinfo.value.state is RunState.FAILED


async def test_score_gate_without_a_score_halts_failed(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's rework/stop branch with no score at all: defensive, but it
    must still fail as FAILED, never as AssertionError."""
    run_id = _integrating_run(store)
    pipeline = Pipeline(SimpleNamespace(store=store, config=JevConfig()), None)  # type: ignore[arg-type]
    monkeypatch.setattr("jev_agent.core.pipeline.ship_gate_outcome", lambda *a, **k: "rework")
    outcome = IntegrationOutcome(_result(workdir=tmp_path), "passed")
    with pytest.raises(Halt) as excinfo:
        await pipeline._ship_after_score_gate(
            store.get_run(run_id), outcome, [_plan_with_task().tasks[0]]
        )
    assert excinfo.value.state is RunState.FAILED


def test_fix_task_without_anything_fixable_halts_failed(store: Store, tmp_path: Path) -> None:
    run_id = _integrating_run(store)
    pipeline = Pipeline(SimpleNamespace(store=store, config=JevConfig()), None)  # type: ignore[arg-type]
    outcome = IntegrationOutcome(_result(workdir=tmp_path), "passed")
    with pytest.raises(Halt) as excinfo:
        pipeline._add_fix_task(store.get_run(run_id), outcome)
    assert excinfo.value.state is RunState.FAILED


def test_append_task_without_a_plan_halts_failed(store: Store) -> None:
    run = store.create_run(Idea(text="add search", repo_path=Path(".")))
    pipeline = Pipeline(SimpleNamespace(store=store, config=JevConfig()), None)  # type: ignore[arg-type]
    with pytest.raises(Halt) as excinfo:
        pipeline._append_task(store.get_run(run.id), _plan_with_task().tasks[0])
    assert excinfo.value.state is RunState.FAILED
