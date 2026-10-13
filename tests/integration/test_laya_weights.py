"""Tuned Laya weights end to end (#69): a run that learns with a profile votes with the
profile's weights, unless jev.toml sets them; its decisions then become tuning samples."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jev_agent.config import JevConfig
from jev_agent.config.schema import DEFAULT_LAYA_WEIGHTS, LAYA_CRITERIA
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.learning import Learning, resolve_learning
from jev_agent.core.models import Decision, DecisionRequest, Idea, Plan, RunState, TaskCard
from jev_agent.core.pipeline import build_pipeline
from jev_agent.core.store import RunRecord, Store
from jev_agent.core.tuning import collect_samples
from jev_agent.dispatch.consensus import ConsensusDispatcher
from jev_agent.dispatch.jev import JevDispatcher, JevSettings
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry

PLAN = Plan(summary="one task", tasks=[TaskCard(id="T1", title="t", description="d")])
TUNED = DEFAULT_LAYA_WEIGHTS | {"safety": 0.25, "simplicity": 0.10}
SEEN: list[dict[str, float]] = []  # the weights of every vote, newest last


class _Consensus(ConsensusDispatcher):
    """Answers like the rules dispatcher, with full Laya scores, and records the weights it was
    asked to vote with (never reaches Jev)."""

    async def decide(self, req: DecisionRequest) -> Decision:
        SEEN.append(dict(self.laya.weights))
        decision = await RulesDispatcher().decide(req)
        scores = dict.fromkeys(LAYA_CRITERIA, 4.0)
        laya = {q: {"scores": {a: scores}} for q, a in decision.answers.items()}
        return decision.model_copy(update={"dispatcher": self.name, "details": {"laya": laya}})


@pytest.fixture
def run(fixture_repo: Path) -> Iterator[Any]:
    opened: list[tuple[Store, Learning]] = []

    async def start(
        config_data: dict[str, Any], stored: str | None = None
    ) -> tuple[Store, RunRecord]:
        config = JevConfig.model_validate(
            {
                "approval": {"require_plan_approval": False},
                "review": {"enabled": False},
                "learning": {"enabled": True},
                **config_data,
            }
        )
        store = Store.open(fixture_repo / ".jev" / "state.db")
        learning = Learning.for_store(store)
        opened.append((store, learning))
        learning.ensure("default")
        learning.set_laya_weights("default", TUNED)
        if stored is not None:  # bypasses the validation of set_laya_weights
            store.connection.execute(
                "UPDATE learning_profiles SET laya_weights_json = ?", (stored,)
            )
        worker = FakeWorker("fake", {"planner": FakeStep(parsed=PLAN)})
        pipeline = build_pipeline(
            fixture_repo,
            config,
            store,
            WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")}),
            dispatcher=_Consensus(JevDispatcher(JevSettings())),
            commands=FakeCommandRunner(),
            learning=learning,
        )
        SEEN.clear()
        profile = resolve_learning(store, learning, config)
        record = await pipeline.start(
            Idea(text="add it", repo_path=fixture_repo), learning_profile=profile
        )
        return store, record

    yield start
    for store, learning in opened:
        learning.close()
        store.close()


async def test_the_profiles_weights_drive_the_vote_and_outcomes_become_samples(run: Any) -> None:
    store, record = await run({})
    assert record.state is RunState.DONE, record.reason
    assert SEEN and all(w == TUNED for w in SEEN)
    samples = collect_samples(store)
    assert {s.point for s in samples} >= {"D1", "D4", "D5"}
    assert "D7" not in {s.point for s in samples}
    assert {s.outcome for s in samples} == {"passed"} and {s.run_id for s in samples} == {record.id}


async def test_weights_in_jev_toml_win_over_the_profile(run: Any) -> None:
    _, record = await run({"dispatcher": {"laya": {"weights": DEFAULT_LAYA_WEIGHTS}}})
    assert record.state is RunState.DONE, record.reason
    assert SEEN and all(w == DEFAULT_LAYA_WEIGHTS for w in SEEN)


async def test_stored_weights_that_no_longer_validate_stop_the_run(run: Any) -> None:
    _, record = await run({}, stored='{"accuracy": 1.0}')
    assert record.state is RunState.NEEDS_HUMAN
    assert record.reason is not None and "invalid Laya weights" in record.reason
    assert "`jev laya reset` clears them" in record.reason
    assert SEEN == []
