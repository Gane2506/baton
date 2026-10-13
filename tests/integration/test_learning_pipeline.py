"""Recursive learning end to end: FakeWorker + RulesDispatcher on a real git repo."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jev_agent.config import JevConfig
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.learning import Learning, resolve_learning
from jev_agent.core.models import Idea, Plan, RunState, TaskCard
from jev_agent.core.pipeline import Pipeline, build_pipeline
from jev_agent.core.store import Store
from jev_agent.roles import LearnerOutput, LessonDraft
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.fake import FakeStep, FakeWorker, FakeWorkerCrash, ScriptValue
from jev_agent.workers.registry import WorkerRegistry

LESSON = "Run the task's test command before finishing; T1 broke the build once."
PLAN = Plan(
    summary="one task",
    tasks=[
        TaskCard(
            id="T1",
            title="add search module",
            description="do it",
            files_hint=["src/search.py"],
            acceptance_criteria=["a"],
        )
    ],
)
LEARNED = FakeStep(
    parsed=LearnerOutput(
        add=[LessonDraft(text=LESSON, roles=["planner", "coder"], tags=["src/search.py"])]
    )
)


def _config(**overrides: Any) -> JevConfig:
    data: dict[str, Any] = {
        "approval": {"require_plan_approval": False},
        "learning": {"enabled": True},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return JevConfig.model_validate(data)


@dataclass
class Env:
    repo: Path
    store: Store
    learning: Learning
    pipeline: Pipeline
    worker: FakeWorker
    config: JevConfig

    def jobs(self, role: str) -> list[WorkerJob]:
        return [j for j in self.worker.jobs if j.role == role]

    def points(self, run_id: str) -> list[str]:
        return [d.decision.point for d in self.store.list_decisions(run_id)]

    async def start(self, **flags: Any) -> Any:
        profile = resolve_learning(self.store, self.learning, self.config, **flags)
        idea = Idea(text="add search", repo_path=self.repo)
        return await self.pipeline.start(idea, learning_profile=profile)


@pytest.fixture
def make_env(fixture_repo: Path) -> Iterator[Any]:
    opened: list[tuple[Store, Learning]] = []

    def make(script: Mapping[str, ScriptValue], config: JevConfig | None = None) -> Env:
        config = config or _config()
        store = Store.open(fixture_repo / ".jev" / "state.db")
        learning = Learning.for_store(store)
        opened.append((store, learning))
        worker = FakeWorker("fake", script)
        registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
        pipeline = build_pipeline(
            fixture_repo,
            config,
            store,
            registry,
            commands=FakeCommandRunner(),
            learning=learning,
        )
        return Env(fixture_repo, store, learning, pipeline, worker, config)

    yield make
    for store, learning in opened:
        learning.close()
        store.close()


async def test_run_learns_and_next_run_uses_the_lesson(make_env: Any) -> None:
    first: Env = make_env(
        {
            "planner": FakeStep(parsed=PLAN),
            "coder:T1": [FakeStep(status="failure", summary="build broke"), FakeStep()],
            "learner": [LEARNED, FakeStep()],
        }
    )
    run1 = await first.start()
    assert run1.state is RunState.DONE, run1.reason
    assert run1.learning_profile == "default"
    assert run1.learning_outcome == "learned into default: 1 added, 0 updated, 0 retired"
    assert first.points(run1.id)[-1] == "D7"
    d7 = first.store.list_decisions(run1.id)[-1]
    assert d7.decision.answers["learn"] == "learn"
    assert d7.request.state["signals"] == ["retry"]
    learner_job = first.jobs("learner")[0]
    assert '"retry"' in learner_job.prompt and "build broke" in learner_job.prompt
    assert "None yet." in learner_job.prompt
    assert all("Lessons from earlier runs" not in j.prompt for j in first.worker.jobs)
    [lesson] = first.learning.lessons("default")
    assert (lesson.text, lesson.roles, lesson.source_run) == (
        LESSON,
        ("planner", "coder"),
        run1.id,
    )

    second: Env = make_env({"planner": FakeStep(parsed=PLAN)})
    run2 = await second.start()
    assert run2.state is RunState.DONE, run2.reason
    for role in ("planner", "coder"):
        prompt = second.jobs(role)[0].prompt
        assert "## Lessons from earlier runs in this repo" in prompt and LESSON in prompt, role
    assert LESSON not in second.jobs("validator")[0].prompt  # not one of the lesson's roles
    attempts = {a.role: a.lesson_ids for a in second.store.list_attempts(run2.id)}
    assert attempts["planner"] == (lesson.id,) and attempts["coder"] == (lesson.id,)
    assert attempts["validator"] == ()
    assert attempts["learner"] == (lesson.id,)  # the learner sees the lesson to refine it
    assert f"`{lesson.id}`" in second.jobs("learner")[0].prompt
    # Run 2's task was done on the first attempt: the lesson is credited.
    scored = second.learning.lessons("default")[0]
    assert (scored.uses, scored.score) == (1, 1.0)
    # The profile has fewer than 3 lessons, so even a clean run learns.
    assert run2.learning_outcome is not None and run2.learning_outcome.startswith("learned")


async def test_no_learning_injects_and_learns_nothing(make_env: Any) -> None:
    env: Env = make_env({"planner": FakeStep(parsed=PLAN), "learner": LEARNED})
    env.learning.ensure("default")
    env.learning.db_for("default").add_lesson("default", LESSON, ["planner", "coder"], [])
    run = await env.start(no_learning=True)
    assert run.state is RunState.DONE, run.reason
    assert (run.learning_profile, run.learning_outcome) == (None, None)
    assert "D7" not in env.points(run.id)
    assert env.jobs("learner") == []
    assert all(LESSON not in j.prompt for j in env.worker.jobs)
    assert all(a.lesson_ids == () for a in env.store.list_attempts(run.id))
    assert len(env.learning.lessons("default")) == 1


async def test_learning_off_by_default(make_env: Any) -> None:
    env: Env = make_env(
        {"planner": FakeStep(parsed=PLAN)},
        config=JevConfig.model_validate({"approval": {"require_plan_approval": False}}),
    )
    run = await env.start()
    assert run.learning_profile is None and "D7" not in env.points(run.id)
    assert env.learning.profiles() == []


async def test_resume_keeps_the_runs_profile(make_env: Any) -> None:
    crashing: Env = make_env({"planner": FakeStep(parsed=PLAN), "coder:T1:1": FakeStep(crash=True)})
    crashing.learning.create("work")
    with pytest.raises(FakeWorkerCrash):
        await crashing.start(flag_profile="work")
    run_id = crashing.store.list_runs()[0].id

    # A new process; the repo default would be "default", but the run keeps "work".
    fresh: Env = make_env({"planner": FakeStep(parsed=PLAN), "learner": LEARNED})
    run = await fresh.pipeline.resume(run_id)
    assert run.state is RunState.DONE, run.reason
    assert run.learning_profile == "work"
    assert run.learning_outcome is not None and "learned into work" in run.learning_outcome
    assert [x.text for x in fresh.learning.lessons("work")] == [LESSON]
    assert fresh.learning.get("default") is None


async def test_learner_skipped_when_budget_is_spent(make_env: Any) -> None:
    env: Env = make_env(
        {"planner": FakeStep(parsed=PLAN, cost_usd=1.0), "coder": FakeStep(cost_usd=1.0)},
        config=_config(budget={"per_run_usd": 2.0, "per_task_usd": 1.5}),
    )
    run = await env.start()
    assert run.state is RunState.DONE, run.reason
    assert run.learning_outcome is not None and "budget is spent" in run.learning_outcome
    assert env.jobs("learner") == [] and "D7" not in env.points(run.id)


async def test_learner_skipped_without_worker_and_on_failed_runs(make_env: Any) -> None:
    config = _config(roles={"learner": {"workers": []}})
    env: Env = make_env(
        {"planner": FakeStep(parsed=PLAN), "coder": FakeStep(status="failure")}, config=config
    )
    run = await env.start()
    assert run.state is RunState.FAILED
    assert run.learning_outcome == "skipped: no available worker for role learner (`jev doctor`)"


async def test_failed_run_learns_and_bad_learner_output_is_skipped(make_env: Any) -> None:
    env: Env = make_env(
        {
            "planner": FakeStep(parsed=PLAN),
            "coder": FakeStep(status="failure", summary="cannot import"),
            "learner": FakeStep(status="error", summary="model refused"),
        }
    )
    run = await env.start()
    assert run.state is RunState.FAILED
    d7 = env.store.list_decisions(run.id)[-1]
    assert d7.request.point == "D7"
    assert d7.request.state["signals"] == ["retry", "escalate", "reassign", "failed"]
    assert run.learning_outcome == (
        "skipped: the learner gave no valid output (error: model refused)"
    )
