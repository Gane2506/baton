"""End-to-end pipeline runs: FakeWorker + RulesDispatcher against a real local git repo."""

import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jev_agent.config import JevConfig
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.lease import RunInUseError, hold_run_lease
from jev_agent.core.models import (
    ChoiceChallenge,
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    RunState,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
    WorkerStatus,
)
from jev_agent.core.pipeline import Pipeline, PipelineError, build_pipeline, cancel_run
from jev_agent.core.store import RunRecord, Store
from jev_agent.dispatch.base import Dispatcher, DispatchError
from jev_agent.dispatch.consensus import ConsensusDispatcher, NoConsensus
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.roles import ReviewResult
from jev_agent.workers.base import WorkerJob, WorkerOutput
from jev_agent.workers.fake import FakeStep, FakeWorker, FakeWorkerCrash, ScriptValue
from jev_agent.workers.registry import WorkerRegistry


def _card(
    tid: str, deps: list[str] | None = None, files: int = 2, test: str | None = None
) -> TaskCard:
    return TaskCard(
        id=tid,
        title=f"task {tid}",
        description=f"do {tid}",
        files_hint=[f"src/{tid.lower()}_{i}.py" for i in range(files)],
        depends_on=deps or [],
        acceptance_criteria=["a", "b"],
        test_command=test,
    )


TWO_TASKS = Plan(
    summary="two tasks",
    tasks=[_card("T1", test="check T1"), _card("T2", deps=["T1"], test="check T2")],
)
ONE_TASK = Plan(summary="one task", tasks=[_card("T1")])


def _config(**overrides: Any) -> JevConfig:
    data: dict[str, Any] = {
        "approval": {"require_plan_approval": False},
        "roles": {
            "coder": {"workers": ["claude_code", "codex"], "models": ["coder-small", "coder-large"]}
        },
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return JevConfig.model_validate(data)


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

    def points(self, run_id: str) -> list[str]:
        return [d.decision.point for d in self.store.list_decisions(run_id)]


@pytest.fixture
def make_env(fixture_repo: Path) -> Iterator[Any]:
    stores: list[Store] = []

    def make(
        script: Mapping[str, ScriptValue],
        config: JevConfig | None = None,
        dispatcher: Dispatcher | None = None,
        exit_codes: dict[str, list[int]] | None = None,
    ) -> Env:
        store = Store.open(fixture_repo / ".jev" / "state.db")
        stores.append(store)
        worker = FakeWorker("fake", script)
        registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
        commands = FakeCommandRunner(exit_codes)
        pipeline = build_pipeline(
            fixture_repo,
            config or _config(),
            store,
            registry,
            dispatcher=dispatcher,
            commands=commands,
        )
        return Env(fixture_repo, store, pipeline, worker, commands)

    yield make
    for store in stores:
        store.close()


def _idea(repo: Path) -> Idea:
    return Idea(text="add two modules", repo_path=repo)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


async def test_full_pipeline_end_to_end(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=TWO_TASKS)})
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    assert run.reason == f"result on branch jev/{run.id}/result"
    assert [t.state for t in env.store.list_tasks(run.id)] == [TaskState.DONE, TaskState.DONE]
    assert env.points(run.id) == ["D1", "D2", "D3", "D4", "D5", "D4", "D5", "D6"]
    assert all(d.fallback_reason is None for d in env.store.list_decisions(run.id))
    # D2 picked a validator from a different vendor than the planner (claude_code).
    assert env.jobs("planner")[0].model is None
    validator = next(a for a in env.store.list_attempts(run.id) if a.role == "validator")
    assert validator.worker == "codex"
    # Tests ran per task in the task's worktree, then on the result branch.
    assert [c for c, _ in env.commands.calls] == ["check T1", "check T2", "check T1", "check T2"]
    # The result branch has both tasks' files; nothing was done on main.
    files = _git(env.repo, "ls-tree", "-r", "--name-only", f"jev/{run.id}/result").split()
    assert {"jev_fake_T1.txt", "jev_fake_T2.txt"} <= set(files)
    assert _git(env.repo, "branch", "--show-current").strip() == "main"
    assert not (env.repo / "jev_fake_T1.txt").exists()
    # Logs land in .jev/runs/<run_id>/<scope>/<attempt>.log.
    runs_dir = env.repo / ".jev" / "runs" / run.id
    assert (runs_dir / "run" / "1.log").exists()
    assert (runs_dir / "T1" / "1.log").exists()
    assert (runs_dir / "T1" / "1.test.log").exists()


async def test_plan_revision_loop(make_env: Any) -> None:
    revise = FakeStep(parsed=ValidationResult(verdict="revise", issues=["split T1 by module"]))
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK), "validator": [revise, FakeStep()]})
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    assert run.plan_round == 2
    assert env.points(run.id)[:6] == ["D1", "D2", "D3", "D2", "D3", "D4"]
    second_prompt = env.jobs("planner")[1].prompt
    assert "split T1 by module" in second_prompt
    assert "Previous plan" in second_prompt


async def test_unsound_choice_sends_the_plan_back(make_env: Any) -> None:
    # The validator flags a choice but still approves: the plan goes back anyway (#67).
    flagged = ValidationResult(
        verdict="approve",
        challenges=[
            ChoiceChallenge(choice="one task", alternative="a task per module", holds=True),
            ChoiceChallenge(
                choice="a regex parser",
                alternative="the stdlib json module",
                reason="(not stated)",
                holds=False,
                note="json input needs a real parser",
            ),
        ],
    )
    env: Env = make_env(
        {"planner": FakeStep(parsed=ONE_TASK), "validator": [FakeStep(parsed=flagged), FakeStep()]}
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    assert run.plan_round == 2
    d3 = next(r for r in env.store.list_decisions(run.id) if r.request.point == "D3")
    assert d3.decision.answers == {"verdict": "revise"}
    card = d3.request.state["validation"]
    assert (card["challenge_count"], card["failed_challenge_count"]) == (2, 1)
    assert card["failed_challenges"][0]["choice"] == "a regex parser"
    second_prompt = env.jobs("planner")[1].prompt
    assert "a regex parser instead of the stdlib json module" in second_prompt
    assert "json input needs a real parser" in second_prompt


async def test_plan_rounds_exhausted_needs_human(make_env: Any) -> None:
    revise = FakeStep(parsed=ValidationResult(verdict="revise", issues=["no"]))
    env: Env = make_env(
        {"planner": FakeStep(parsed=ONE_TASK), "validator": revise},
        config=_config(budget={"max_plan_rounds": 2}),
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.NEEDS_HUMAN
    assert len(env.jobs("planner")) == 2
    assert env.store.list_decisions(run.id)[-1].decision.answers == {"verdict": "escalate_human"}


async def test_task_retry(make_env: Any) -> None:
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "coder:T1": [FakeStep(status="failure", summary="compile error"), FakeStep()],
        }
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    task = env.store.get_task(run.id, "T1")
    assert task.attempts == 2
    d5 = [
        d.decision.answers["action"]
        for d in env.store.list_decisions(run.id)
        if d.decision.point == "D5"
    ]
    assert d5 == ["retry", "done"]
    first, second = env.jobs("coder", "T1")
    assert (first.effort, first.model) == (second.effort, second.model)
    assert "compile error" in second.prompt  # feedback from the failed attempt


async def test_retry_after_failing_tests(make_env: Any) -> None:
    plan = Plan(summary="s", tasks=[_card("T1", test="pytest -q")])
    env: Env = make_env({"planner": FakeStep(parsed=plan)}, exit_codes={"pytest -q": [1, 0]})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE, run.reason
    assert env.store.get_task(run.id, "T1").attempts == 2
    assert "Tests failed" in env.jobs("coder", "T1")[1].prompt


async def test_escalation_bumps_effort_then_model(make_env: Any) -> None:
    fail = FakeStep(status="failure", summary="wrong")
    env: Env = make_env(
        {"planner": FakeStep(parsed=ONE_TASK), "coder:T1": [fail, fail, fail, FakeStep()]},
        config=_config(budget={"max_task_attempts": 5}),
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    jobs = env.jobs("coder", "T1")
    # T1 is "standard" (2 files, 2 criteria) -> medium effort, first model in the pool.
    assert [(j.effort, j.model) for j in jobs[:3]] == [
        ("medium", "coder-small"),
        ("medium", "coder-small"),  # retry
        ("high", "coder-small"),  # escalate: effort first
    ]
    actions = [
        d.decision.answers["action"]
        for d in env.store.list_decisions(run.id)
        if d.decision.point == "D5"
    ]
    assert actions == ["retry", "escalate", "reassign", "done"]
    assert jobs[3].effort == "high"
    assert env.store.list_attempts(run.id)[-1].worker == "codex"  # reassigned


async def test_escalation_at_top_effort_moves_to_next_model(make_env: Any) -> None:
    hard = Plan(summary="s", tasks=[_card("T1", files=5)])  # hard -> high effort
    fail = FakeStep(status="failure")
    env: Env = make_env({"planner": FakeStep(parsed=hard), "coder:T1": [fail, fail, FakeStep()]})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE, run.reason
    assert [(j.effort, j.model) for j in env.jobs("coder", "T1")] == [
        ("high", "coder-small"),
        ("high", "coder-small"),
        ("high", "coder-large"),
    ]


async def test_task_fails_after_max_attempts(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK), "coder": FakeStep(status="failure")})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.FAILED
    assert "gave up after 3 attempts" in (run.reason or "")
    assert env.store.get_task(run.id, "T1").state is TaskState.FAILED
    assert len(env.jobs("coder")) == 3


async def test_quota_exhausted_reassigns(make_env: Any) -> None:
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "coder:T1": [FakeStep(status="quota_exhausted"), FakeStep()],
        }
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE, run.reason
    workers = [a.worker for a in env.store.list_attempts(run.id) if a.role == "coder"]
    assert workers == ["claude_code", "codex"]


async def test_run_budget_exceeded_stops(make_env: Any) -> None:
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK, cost_usd=3.0),
            "validator": FakeStep(cost_usd=2.5),
        }
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.BUDGET_EXCEEDED
    assert run.spend_usd == pytest.approx(5.5)
    assert "cap" in (run.reason or "")
    assert env.jobs("coder") == []  # no worker launched after the cap was reached


async def test_task_budget_exceeded_fails_task(make_env: Any) -> None:
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "coder:T1": FakeStep(status="failure", cost_usd=1.6),
        }
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.FAILED
    task = env.store.get_task(run.id, "T1")
    assert task.state is TaskState.FAILED
    assert task.spend_usd == pytest.approx(1.6)
    assert run.spend_usd == pytest.approx(1.6)  # task spend also counts towards the run
    assert len(env.jobs("coder")) == 1


async def test_resume_after_crash(make_env: Any) -> None:
    crashing: Env = make_env(
        {"planner": FakeStep(parsed=TWO_TASKS), "coder:T2:1": FakeStep(crash=True)}
    )
    with pytest.raises(FakeWorkerCrash):
        await crashing.pipeline.start(_idea(crashing.repo))
    run_id = crashing.store.list_runs()[0].id
    assert crashing.store.get_run(run_id).state is RunState.EXECUTING
    assert crashing.store.get_task(run_id, "T2").state is TaskState.RUNNING

    # A new process: fresh store, pipeline and workers.
    fresh: Env = make_env({"planner": FakeStep(parsed=TWO_TASKS)})
    run = await fresh.pipeline.resume(run_id)

    assert run.state is RunState.DONE, run.reason
    t1, t2 = fresh.store.list_tasks(run_id)
    assert (t1.attempts, t2.attempts) == (1, 2)  # the interrupted attempt counts
    assert fresh.jobs("planner") == [] and fresh.jobs("coder", "T1") == []  # not redone
    interrupted = next(a for a in fresh.store.list_attempts(run_id) if a.task_id == "T2")
    assert interrupted.result is not None and interrupted.result.status == "error"
    assert "interrupted" in interrupted.result.summary


async def test_approval_gate(make_env: Any) -> None:
    env: Env = make_env(
        {"planner": FakeStep(parsed=ONE_TASK)},
        config=_config(approval={"require_plan_approval": True}),
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.AWAITING_APPROVAL
    assert [t.state for t in env.store.list_tasks(run.id)] == [TaskState.PENDING]
    assert env.jobs("coder") == []
    with pytest.raises(PipelineError, match="approve"):
        await env.pipeline.resume(run.id)

    done = await env.pipeline.approve(run.id)
    assert done.state is RunState.DONE, done.reason
    with pytest.raises(PipelineError):
        await env.pipeline.approve(run.id)


async def test_plan_only_stops_at_gate(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)})  # approval not required
    run = await env.pipeline.start(_idea(env.repo), plan_only=True)
    assert run.state is RunState.AWAITING_APPROVAL


class _BadDispatcher:
    """Answers outside the allowed values, or raises, to exercise the rules fallback."""

    name = "bad"

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, req: DecisionRequest) -> Decision:
        self.calls += 1
        if req.point == "D4":
            raise DispatchError("connection reset")
        answers = {name: "nonsense" for name in req.questions}
        return Decision(point=req.point, answers=answers, dispatcher=self.name, latency_ms=5)


async def test_invalid_dispatcher_answers_fall_back_to_rules(make_env: Any) -> None:
    bad = _BadDispatcher()
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, dispatcher=bad)
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    records = env.store.list_decisions(run.id)
    assert bad.calls == len(records)
    assert all(r.decision.dispatcher == "rules" for r in records)
    assert all(r.fallback_reason for r in records)
    d1 = records[0]
    assert d1.fallback_reason is not None and "'nonsense' not in" in d1.fallback_reason
    d4 = next(r for r in records if r.decision.point == "D4")
    assert d4.fallback_reason is not None and "connection reset" in d4.fallback_reason


class _AskHuman:
    name = "asker"

    async def decide(self, req: DecisionRequest) -> Decision:
        answers = {name: allowed[-1] for name, allowed in req.questions.items()}
        return Decision(point=req.point, answers=answers, dispatcher=self.name)


async def test_d1_ask_human(make_env: Any) -> None:
    env: Env = make_env({}, dispatcher=_AskHuman())
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.NEEDS_HUMAN
    assert env.worker.jobs == []


async def test_plan_with_cycle_fails(make_env: Any) -> None:
    cyclic = Plan(summary="s", tasks=[_card("T1", deps=["T2"]), _card("T2", deps=["T1"])])
    env: Env = make_env({"planner": FakeStep(parsed=cyclic)})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.FAILED
    assert "cycle" in (run.reason or "")


async def test_planner_without_plan_needs_human(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=ValidationResult(verdict="approve"))})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.NEEDS_HUMAN
    assert "no valid Plan output" in (run.reason or "")


async def test_review_stage_when_enabled(make_env: Any) -> None:
    revise = FakeStep(parsed=ReviewResult(verdict="revise", issues=["missing docstring"]))
    env: Env = make_env(
        {"planner": FakeStep(parsed=ONE_TASK), "reviewer": [revise, FakeStep()]},
        config=_config(review={"enabled": True}),
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE, run.reason
    reviews = env.jobs("reviewer", "T1")
    assert len(reviews) == 2
    assert "jev_fake_T1.txt" in reviews[0].prompt  # the reviewer saw the diff
    assert "missing docstring" in env.jobs("coder", "T1")[1].prompt


async def test_cancel(make_env: Any) -> None:
    env: Env = make_env(
        {"planner": FakeStep(parsed=ONE_TASK)},
        config=_config(approval={"require_plan_approval": True}),
    )
    run = await env.pipeline.start(_idea(env.repo))
    cancelled: RunRecord = env.pipeline.cancel(run.id)
    assert cancelled.state is RunState.CANCELLED
    with pytest.raises(PipelineError):
        env.pipeline.cancel(run.id)
    with pytest.raises(PipelineError):
        await env.pipeline.resume(run.id)


# ---- bad prompt templates ------------------------------------------------------------------


async def test_missing_prompt_file_needs_human_then_resumes(make_env: Any) -> None:
    config = _config(roles={"planner": {"prompt_file": "prompts/planner.md"}})
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, config=config)
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.NEEDS_HUMAN
    assert run.resume_state is RunState.INTAKE  # stopped before any step ran
    reason = run.reason or ""
    assert "planner prompt" in reason and "prompts/planner.md" in reason
    assert "file not found" in reason and f"jev resume {run.id}" in reason
    assert env.store.list_attempts(run.id) == []

    (env.repo / "prompts").mkdir()
    (env.repo / "prompts" / "planner.md").write_text("Plan: {{ idea }}\n")
    run = await env.pipeline.resume(run.id)
    assert run.state is RunState.DONE, run.reason
    assert run.resume_state is None
    assert env.jobs("planner")[0].prompt == "Plan: add two modules\n"


async def test_syntax_error_in_any_template_stops_before_work(make_env: Any) -> None:
    config = _config(roles={"reviewer": {"prompt_file": "review.md"}})
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, config=config)
    (env.repo / "review.md").write_text("{% for x in task.files_hint %}\n")
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.NEEDS_HUMAN
    assert "reviewer prompt" in (run.reason or "") and "syntax error on line" in (run.reason or "")
    assert env.worker.jobs == []


async def test_undefined_variable_in_coder_template_keeps_the_attempt(make_env: Any) -> None:
    config = _config(roles={"coder": {"prompt_file": "coder.md"}})
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, config=config)
    (env.repo / "coder.md").write_text("Do {{ task.title }} in {{ nope }}\n")
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.NEEDS_HUMAN
    assert run.resume_state is RunState.EXECUTING
    assert "coder prompt" in (run.reason or "") and "'nope' is undefined" in (run.reason or "")
    (task,) = env.store.list_tasks(run.id)
    assert task.attempts == 0  # no attempt was used up
    assert env.jobs("coder") == []

    (env.repo / "coder.md").write_text("Do {{ task.title }}\n")
    run = await env.pipeline.resume(run.id)
    assert run.state is RunState.DONE, run.reason
    assert env.jobs("coder")[0].prompt == "Do task T1\n"
    assert env.store.list_tasks(run.id)[0].attempts == 1


async def test_other_needs_human_stops_are_not_resumable(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=ValidationResult(verdict="approve"))})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.NEEDS_HUMAN and run.resume_state is None
    with pytest.raises(PipelineError, match="cannot be resumed"):
        await env.pipeline.resume(run.id)


async def test_role_tools_reach_worker_jobs(make_env: Any) -> None:
    config = _config(
        tools={"mcp": {"github": {"command": "npx", "env_from": ["GH_TOKEN"]}},
               "skills": {"style": "skills/style"}},
        roles={"planner": {"mcp": ["github"]},
               "coder": {"workers": ["claude_code"], "skills": ["style"]}},
    )  # fmt: skip
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, config)
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE, run.reason
    planner, coder = env.jobs("planner")[0], env.jobs("coder")[0]
    assert list(planner.tools.mcp) == ["github"] and not planner.tools.skills
    assert [(s.name, s.path) for s in coder.tools.skills] == [("style", env.repo / "skills/style")]
    assert not coder.tools.mcp
    assert not env.jobs("validator")[0].tools


# ---- cancellation is never overwritten; plan-only survives resume (issues #14, #22) ----------


class _CancelsThenAnswers:
    """A worker during which the user runs `jev cancel` from another shell."""

    name = "fake"

    def __init__(self, store: Store, status: WorkerStatus) -> None:
        self.store, self.status = store, status

    async def run(self, job: WorkerJob) -> WorkerOutput:
        cancel_run(self.store, self.store.list_runs()[0].id)
        parsed = ONE_TASK if self.status == "success" else None
        result = WorkerResult(status=self.status, summary="done", raw_log_path=job.log_path)
        return WorkerOutput(result=result, parsed=parsed)


@pytest.mark.parametrize("status", ["error", "success"])
async def test_cancel_while_a_worker_runs_is_kept(fixture_repo: Path, status: WorkerStatus) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        worker = _CancelsThenAnswers(store, status)
        registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
        pipeline = build_pipeline(fixture_repo, _config(), store, registry)
        run = await pipeline.start(_idea(fixture_repo))
        assert run.state is RunState.CANCELLED, run.reason
        assert run.reason == "cancelled by user"
        assert run.learning_outcome is None  # the learning step never ran for it


async def test_cancel_does_not_replace_a_finished_run(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)})
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE, run.reason
    with pytest.raises(PipelineError, match="already DONE"):
        cancel_run(env.store, run.id)
    assert env.store.get_run(run.id).state is RunState.DONE


async def test_plan_only_is_kept_when_the_run_is_resumed(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)})  # approval not required
    # A plan-only run that stopped before planning (the process died): resume it.
    run = env.store.create_run(_idea(env.repo), plan_only=True)
    assert run.plan_only
    resumed = await env.pipeline.resume(run.id)
    assert resumed.state is RunState.AWAITING_APPROVAL, resumed.reason
    assert env.jobs("coder") == []


class _AlwaysDone:
    """Answers `done` at D5 whatever happened (issue #21); the rules dispatcher elsewhere."""

    name = "always-done"

    def __init__(self) -> None:
        from jev_agent.dispatch.rules import RulesDispatcher

        self.rules = RulesDispatcher()

    async def decide(self, req: DecisionRequest) -> Decision:
        if req.point == "D5":
            return Decision(point="D5", answers={"action": "done"}, dispatcher=self.name)
        return await self.rules.decide(req)


def _d5_actions(env: Env, run_id: str) -> list[str]:
    return [
        d.decision.answers["action"]
        for d in env.store.list_decisions(run_id)
        if d.decision.point == "D5"
    ]


async def test_done_after_a_failed_worker_is_overridden_to_retry(make_env: Any) -> None:
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "coder:T1": [FakeStep(status="error", summary="worker crashed"), FakeStep()],
        },
        dispatcher=_AlwaysDone(),
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    task = env.store.get_task(run.id, "T1")
    assert task.attempts == 2  # the failed attempt was not accepted as done
    assert _d5_actions(env, run.id) == ["done", "done"]  # as the dispatcher answered
    second = env.jobs("coder", "T1")[1]
    assert "D5 answered done, overridden to retry: the worker reported error" in second.prompt


async def test_done_after_failed_tests_is_overridden_to_retry(make_env: Any) -> None:
    plan = Plan(summary="s", tasks=[_card("T1", test="pytest -q")])
    env: Env = make_env(
        {"planner": FakeStep(parsed=plan)},
        dispatcher=_AlwaysDone(),
        exit_codes={"pytest -q": [1, 0]},
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.DONE, run.reason
    assert env.store.get_task(run.id, "T1").attempts == 2
    second = env.jobs("coder", "T1")[1]
    assert "D5 answered done, overridden to retry: the tests failed" in second.prompt
    assert "Tests failed" in second.prompt


async def test_done_never_accepted_while_the_worker_keeps_failing(make_env: Any) -> None:
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "coder:T1": FakeStep(status="timeout", summary="took too long"),
        },
        dispatcher=_AlwaysDone(),
    )
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.FAILED, run.reason
    task = env.store.get_task(run.id, "T1")
    assert task.state is TaskState.FAILED and task.attempts == 3


# ---- failure, cleanup and cancellation paths (issue #54) -------------------------------------


class _Wrapped:
    """A FakeWorker with a hook that runs before (and may replace) each job."""

    name = "fake"

    def __init__(self, inner: FakeWorker, before: Any) -> None:
        self.inner, self.before = inner, before

    async def run(self, job: WorkerJob) -> WorkerOutput:
        self.before(job)
        return await self.inner.run(job)


def _wired(
    repo: Path,
    store: Store,
    worker: Any,
    *,
    workspace: Any = None,
    dispatcher: Dispatcher | None = None,
) -> Pipeline:
    registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
    return build_pipeline(
        repo,
        _config(),
        store,
        registry,
        dispatcher=dispatcher,
        workspace=workspace,
        commands=FakeCommandRunner(),
    )


async def test_a_raising_worker_adapter_becomes_an_error_attempt(fixture_repo: Path) -> None:
    inner = FakeWorker("fake", {"planner": FakeStep(parsed=ONE_TASK)})

    def boom_once(job: WorkerJob) -> None:
        if job.role == "coder" and job.attempt == 1:
            raise RuntimeError("adapter bug")

    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = await _wired(fixture_repo, store, _Wrapped(inner, boom_once)).start(
            _idea(fixture_repo)
        )
        assert run.state is RunState.DONE, run.reason
        first, second = [a for a in store.list_attempts(run.id) if a.role == "coder"]
        assert first.result is not None and first.result.status == "error"
        assert first.result.summary == "worker raised RuntimeError: adapter bug"
        assert second.result is not None and second.result.status == "success"
        assert store.get_task(run.id, "T1").attempts == 2


def _failing_workspace(repo: Path, method: str) -> Any:
    from jev_agent.core.workspace import GitWorkspace, WorkspaceError

    class Failing(GitWorkspace):
        pass

    def fail(self: GitWorkspace, run_id: str) -> Any:
        raise WorkspaceError(f"git {method} failed: disk full")

    setattr(Failing, method, fail)
    return Failing(repo, repo / ".jev")


async def test_cleanup_failure_after_done_keeps_done(fixture_repo: Path) -> None:
    worker = FakeWorker("fake", {"planner": FakeStep(parsed=ONE_TASK)})
    workspace = _failing_workspace(fixture_repo, "cleanup")
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = await _wired(fixture_repo, store, worker, workspace=workspace).start(
            _idea(fixture_repo)
        )
        assert run.state is RunState.DONE, run.reason
        assert run.reason == (
            f"result on branch jev/{run.id}/result; "
            "worktree cleanup failed: git cleanup failed: disk full"
        )
        assert store.get_run(run.id).state is RunState.DONE


async def test_start_commit_failure_halts_failed(fixture_repo: Path) -> None:
    worker = FakeWorker("fake", {"planner": FakeStep(parsed=ONE_TASK)})
    workspace = _failing_workspace(fixture_repo, "start_commit")
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = await _wired(fixture_repo, store, worker, workspace=workspace).start(
            _idea(fixture_repo)
        )
        assert run.state is RunState.FAILED
        assert run.reason == "cannot record the start commit: git start_commit failed: disk full"
        assert [j for j in worker.jobs if j.role == "coder"] == []


async def test_validator_without_a_verdict_needs_human(make_env: Any) -> None:
    env: Env = make_env(
        {"planner": FakeStep(parsed=ONE_TASK), "validator": FakeStep(parsed=ONE_TASK)}
    )
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.NEEDS_HUMAN
    reason = run.reason or ""
    assert reason.startswith("validator gave no verdict") and "no valid ValidationResult" in reason
    assert env.jobs("coder") == [] and env.store.list_tasks(run.id) == []


class _EscalatesAtD3:
    """Answers escalate_human at D3; the rules dispatcher elsewhere."""

    name = "escalates"

    def __init__(self) -> None:
        from jev_agent.dispatch.rules import RulesDispatcher

        self.rules = RulesDispatcher()

    async def decide(self, req: DecisionRequest) -> Decision:
        if req.point == "D3":
            answers = {"verdict": "escalate_human"}
            return Decision(point="D3", answers=answers, dispatcher=self.name)
        return await self.rules.decide(req)


async def test_d3_escalate_human_needs_human(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, dispatcher=_EscalatesAtD3())
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.NEEDS_HUMAN
    assert run.reason == "D3: plan escalated to a human"
    d3 = env.store.list_decisions(run.id)[-1]
    assert d3.decision.point == "D3" and d3.fallback_reason is None
    assert env.jobs("coder") == []


async def test_cancel_between_tasks_starts_no_further_coder(fixture_repo: Path) -> None:
    inner = FakeWorker("fake", {"planner": FakeStep(parsed=TWO_TASKS)})
    with Store.open(fixture_repo / ".jev" / "state.db") as store:

        class CancelsAfterT1:
            """`jev cancel` from another shell just as T1's coder returns."""

            name = "fake"

            async def run(self, job: WorkerJob) -> WorkerOutput:
                output = await inner.run(job)
                if job.role == "coder" and job.task_id == "T1":
                    cancel_run(store, store.list_runs()[0].id)
                return output

        run = await _wired(fixture_repo, store, CancelsAfterT1()).start(_idea(fixture_repo))
        assert run.state is RunState.CANCELLED, run.reason
        assert run.reason == "cancelled by user"
        assert [j.task_id for j in inner.jobs if j.role == "coder"] == ["T1"]
        assert store.get_task(run.id, "T2").state is TaskState.PENDING


class _CancelsAtSecondD4:
    """`jev cancel` lands while T2 is being assigned (after T1 is done, before its coder)."""

    name = "cancels"

    def __init__(self, store: Store) -> None:
        from jev_agent.dispatch.rules import RulesDispatcher

        self.store, self.rules, self.d4 = store, RulesDispatcher(), 0

    async def decide(self, req: DecisionRequest) -> Decision:
        if req.point == "D4":
            self.d4 += 1
            if self.d4 == 2:
                cancel_run(self.store, self.store.list_runs()[0].id)
        return await self.rules.decide(req)


async def test_cancel_while_the_next_task_is_assigned(fixture_repo: Path) -> None:
    worker = FakeWorker("fake", {"planner": FakeStep(parsed=TWO_TASKS)})
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        pipeline = _wired(fixture_repo, store, worker, dispatcher=_CancelsAtSecondD4(store))
        run = await pipeline.start(_idea(fixture_repo))
        assert run.state is RunState.CANCELLED, run.reason
        assert [j.task_id for j in worker.jobs if j.role == "coder"] == ["T1"]
        assert store.get_task(run.id, "T2").attempts == 0


async def test_another_process_driving_the_run_blocks_resume_and_approve(make_env: Any) -> None:
    """Issue #61: a second process (a fresh store on the same DB) cannot resume or approve a
    run while another one holds its lease, and it touches nothing; once that one is gone, it
    can."""
    crashing: Env = make_env(
        {"planner": FakeStep(parsed=TWO_TASKS), "coder:T2:1": FakeStep(crash=True)}
    )
    with pytest.raises(FakeWorkerCrash):
        await crashing.pipeline.start(_idea(crashing.repo))
    run_id = crashing.store.list_runs()[0].id
    assert crashing.store.get_run_lease(run_id) is None  # released on the way out

    other: Env = make_env({"planner": FakeStep(parsed=TWO_TASKS)})
    async with hold_run_lease(crashing.store, run_id):  # "the first process is still running"
        with pytest.raises(RunInUseError, match="in use by another jev process"):
            await other.pipeline.resume(run_id)
        with pytest.raises(RunInUseError):
            await other.pipeline.approve(run_id)
        with pytest.raises(RunInUseError):
            await other.pipeline.advance(run_id)
        open_attempts = [a for a in other.store.list_attempts(run_id) if a.result is None]
        assert len(open_attempts) == 1  # the live process's attempt was not closed
        assert other.worker.jobs == []

    run = await other.pipeline.resume(run_id)
    assert run.state is RunState.DONE, run.reason
    assert other.store.get_run_lease(run_id) is None


class _Consensus:
    """Stands in for the consensus dispatcher (same name, so no rules fallback): answers like
    the rules dispatcher, except that Jev and Laya disagree the first time at `split_at`."""

    name = ConsensusDispatcher.name

    def __init__(self, split_at: str) -> None:
        self.split_at = split_at
        self.splits = 0

    async def decide(self, req: DecisionRequest) -> Decision:
        if req.point == self.split_at and not self.splits:
            self.splits += 1
            raise NoConsensus("Jev and Laya did not agree after 3 round(s): action: ...")
        decision = await RulesDispatcher().decide(req)
        return decision.model_copy(update={"dispatcher": self.name})


async def test_consensus_without_agreement_stops_for_a_human_and_resumes(make_env: Any) -> None:
    consensus = _Consensus(split_at="D5")
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, dispatcher=consensus)
    run = await env.pipeline.start(_idea(env.repo))

    assert run.state is RunState.NEEDS_HUMAN
    assert run.reason is not None and "D5: no valid decision" in run.reason
    assert "Jev and Laya did not agree" in run.reason
    assert run.resume_state is RunState.EXECUTING
    assert "D5" not in env.points(run.id)  # nothing is recorded for the failed vote

    run = await env.pipeline.resume(run.id)
    assert run.state is RunState.DONE, run.reason
    assert all(r.fallback_reason is None for r in env.store.list_decisions(run.id))


async def test_consensus_approval_replaces_the_human_plan_gate(make_env: Any) -> None:
    config = JevConfig.model_validate({"approval": {}})  # require_plan_approval unset
    assert not config.plan_approval_required
    env: Env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, config, _Consensus("none"))
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.DONE, run.reason

    explicit = JevConfig.model_validate({"approval": {"require_plan_approval": True}})
    env = make_env({"planner": FakeStep(parsed=ONE_TASK)}, explicit, _Consensus("none"))
    run = await env.pipeline.start(_idea(env.repo))
    assert run.state is RunState.AWAITING_APPROVAL


def test_plan_approval_default_depends_on_the_dispatcher() -> None:
    assert JevConfig().plan_approval_required is False  # consensus is the default
    for primary in ("jev", "rules"):
        config = JevConfig.model_validate({"dispatcher": {"primary": primary}})
        assert config.plan_approval_required is True
