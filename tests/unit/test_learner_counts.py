"""The learning step records its changes as structured counts on the run (issue #42), which
the KPIs read instead of parsing the outcome line."""

from __future__ import annotations

from pathlib import Path

from jev_agent.config import JevConfig
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.learning import Learning, resolve_learning
from jev_agent.core.models import Idea, Plan, RunState, TaskCard
from jev_agent.core.pipeline import build_pipeline
from jev_agent.core.store import LearningCounts, Store
from jev_agent.roles import LearnerOutput, LessonDraft
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry

PLAN = Plan(
    summary="one task",
    tasks=[
        TaskCard(
            id="T1",
            title="add search",
            description="do it",
            files_hint=["src/search.py"],
            acceptance_criteria=["a"],
        )
    ],
)


async def test_learning_step_stores_counts_on_the_run(fixture_repo: Path) -> None:
    config = JevConfig.model_validate(
        {"approval": {"require_plan_approval": False}, "learning": {"enabled": True}}
    )
    learned = LearnerOutput(
        add=[
            LessonDraft(text="Run the tests before finishing.", roles=["coder"], tags=[]),
            LessonDraft(text="Keep modules small.", roles=["coder"], tags=[]),
        ]
    )
    worker = FakeWorker(
        "fake",
        {
            "planner": FakeStep(parsed=PLAN),
            "coder:T1": [FakeStep(status="failure", summary="broke"), FakeStep()],
            "learner": FakeStep(parsed=learned),
        },
    )
    registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
    store = Store.open(fixture_repo / ".jev" / "state.db")
    learning = Learning.for_store(store)
    try:
        pipeline = build_pipeline(
            fixture_repo, config, store, registry, commands=FakeCommandRunner(), learning=learning
        )
        profile = resolve_learning(store, learning, config)
        run = await pipeline.start(
            Idea(text="add search", repo_path=fixture_repo), learning_profile=profile
        )
        assert run.state is RunState.DONE, run.reason
        assert run.learning_counts == LearningCounts(added=2, updated=0, retired=0)
    finally:
        learning.close()
        store.close()
