"""Regression tests for the 2026-10-16 core-correctness audit fixes (Worker D).

Each test pins the behavior the audit found missing: the decision cache key keeping
`max_attempts`, the `max_mb` eviction actually shrinking the table, the learner's retire
cap and lesson origins, gate-aware queue statuses and recover notes, `create_run`
failing fast on real constraint violations, the semantic scan surviving a broken
embedder entry, role keys carrying the jev version, cache-restore degrading to a review
error, paid quota exhaustion staying terminal, and the dispatch guards.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from jev_agent import __version__
from jev_agent.config.schema import LAYA_CRITERIA, JevConfig
from jev_agent.core.cache import (
    Cache,
    SimHashEmbedder,
    decision_cache_key,
    decision_normalized_text,
    role_cache_key,
    strip_volatile,
)
from jev_agent.core.chat_backends import ClaudeChatBackend
from jev_agent.core.harness import _role_cache_output
from jev_agent.core.learner import MAX_RETIRES, apply_learner_output
from jev_agent.core.learning import LearningError, LessonDB
from jev_agent.core.models import DecisionRequest, Idea, RunState
from jev_agent.core.pipeline import build_pipeline
from jev_agent.core.queue import recover_note, status_for_run
from jev_agent.core.store import Store, StoreError
from jev_agent.core.tuning import _normalised
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.consensus import ConsensusDispatcher, Score, evaluate
from jev_agent.dispatch.jev import JevDispatcher, JevSettings, check_size, retry_delay
from jev_agent.roles.schemas import LearnerOutput, LessonDraft
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "<redacted>")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "state.db")
    yield s
    s.close()


def _req(point: str = "D5", state: dict[str, Any] | None = None) -> DecisionRequest:
    return DecisionRequest(
        point=point,  # type: ignore[arg-type]
        state={"idea": "add search", **(state or {})},
        questions={"action": ["retry", "escalate"]},
    )


def _cache_config(**overrides: Any) -> JevConfig:
    return JevConfig.model_validate({"cache": overrides})


# ---- 1. max_attempts is decision-relevant, not volatile ------------------------------


def test_strip_volatile_keeps_max_attempts() -> None:
    stripped = strip_volatile(
        {
            "attempt": 2,
            "max_attempts": 3,
            "task_spend_usd": 1.5,
            "round": 1,
            "max_rounds": 5,
        }
    )
    assert stripped == {"attempt": 2, "max_attempts": 3, "round": 1, "max_rounds": 5}


def test_decision_key_differs_across_max_attempts() -> None:
    """A decision cached with max_attempts=3 must never serve a run with max_attempts=10."""
    config = _cache_config()
    low = decision_cache_key(
        _req(state={"attempt": 2, "max_attempts": 3}),
        dispatcher_name="s",
        config=config,
        run_id="r",
    )
    high = decision_cache_key(
        _req(state={"attempt": 2, "max_attempts": 10}),
        dispatcher_name="s",
        config=config,
        run_id="r",
    )
    assert low.key_hash != high.key_hash


def test_decision_key_still_strips_spend() -> None:
    """Spend/budget stripping is a documented freshness tradeoff — it must not change."""
    config = _cache_config()
    cheap = decision_cache_key(
        _req(state={"attempt": 2, "max_attempts": 3, "task_spend_usd": 0.01}),
        dispatcher_name="s",
        config=config,
        run_id="r",
    )
    pricey = decision_cache_key(
        _req(state={"attempt": 2, "max_attempts": 3, "task_spend_usd": 4.99}),
        dispatcher_name="s",
        config=config,
        run_id="r",
    )
    assert cheap.key_hash == pricey.key_hash


# ---- 2. max_mb eviction is no longer dead code -----------------------------------------


def test_cache_prune_evicts_down_to_max_mb(store: Store, tmp_path: Path) -> None:
    """An over-budget table shrinks (the old inverted formula was always a no-op)."""
    config = _cache_config(max_entries=1000, max_mb=0.05)
    cache = Cache(store, config.cache, tmp_path)
    for i in range(150):
        key = decision_cache_key(
            _req(state={"n": i}), dispatcher_name="s", config=config, run_id="r"
        )
        store.cache_put(key.key_hash, "decision", key.key_json, "x" * 10_000, 0.1, 168)
    assert store.cache_size_bytes() / (1024 * 1024) > 0.05
    cache.prune()
    entries = store.cache_stats().entries
    assert entries < 150
    assert entries <= 100  # the floor keeps at least 100


# ---- 3. learner retire cap + lesson origins -------------------------------------------


def test_learner_retire_list_is_capped(tmp_path: Path) -> None:
    db = LessonDB.open_shared(tmp_path / "learning.db")
    try:
        profile = db.create_profile("p").name
        for i in range(MAX_RETIRES + 2):
            db.add_lesson(profile, f"lesson {i}", ["coder"], [])
        shown = db.list_lessons(profile)
        output = LearnerOutput(retire=[lesson.id for lesson in shown])
        applied = apply_learner_output(db, profile, output, shown)
        assert applied.retired == [lesson.id for lesson in shown[:MAX_RETIRES]]
        assert len(db.list_lessons(profile)) == 2  # the overflow stays active
    finally:
        db.conn.close()


def test_learner_adds_record_learner_origin(tmp_path: Path) -> None:
    db = LessonDB.open_shared(tmp_path / "learning.db")
    try:
        profile = db.create_profile("p").name
        draft = LessonDraft(
            text="rerun flaky tests before blaming the code", roles=["coder"], tags=["src/flaky.py"]
        )
        applied = apply_learner_output(db, profile, LearnerOutput(add=[draft]), [])
        assert len(applied.added) == 1
        origins = {(e.dst, e.origin) for e in db.lesson_edges(profile)}
        assert origins == {("src/flaky.py", "learner")}
    finally:
        db.conn.close()


def test_add_lesson_defaults_to_manual_origin(tmp_path: Path) -> None:
    db = LessonDB.open_shared(tmp_path / "learning.db")
    try:
        profile = db.create_profile("p").name
        db.add_lesson(profile, "manual lesson", ["coder"], ["src/manual.py"])
        db.add_lesson(profile, "learner lesson", ["coder"], ["src/learned.py"], origin="learner")
        origins = {e.dst: e.origin for e in db.lesson_edges(profile)}
        assert origins == {"src/manual.py": "manual", "src/learned.py": "learner"}
    finally:
        db.conn.close()


def test_add_lesson_rejects_unknown_origin(tmp_path: Path) -> None:
    db = LessonDB.open_shared(tmp_path / "learning.db")
    try:
        profile = db.create_profile("p").name
        with pytest.raises(LearningError, match="origin"):
            db.add_lesson(profile, "x", ["coder"], [], origin="martian")
    finally:
        db.conn.close()


# ---- 4. ASK_EVERY_STEP gates are "waiting", with gate-aware recover notes -------------


def test_step_gates_map_to_waiting() -> None:
    gates = (
        RunState.AWAITING_PLANNING,
        RunState.AWAITING_VALIDATING,
        RunState.AWAITING_DESIGNING,
        RunState.AWAITING_INTEGRATING,
        RunState.AWAITING_SHIPPING,
    )
    for state in gates:
        assert status_for_run(state) == "waiting", state


def test_recover_note_sends_gates_to_approve() -> None:
    assert recover_note(RunState.AWAITING_PLANNING, "r1") == (
        "the scheduler stopped; continue with `jev approve r1`"
    )
    assert recover_note(RunState.AWAITING_SHIP, "r1") == (
        "the scheduler stopped; continue with `jev approve r1`"
    )


def test_recover_note_sends_in_progress_runs_to_resume() -> None:
    assert recover_note(RunState.EXECUTING, "r1") == (
        "the scheduler stopped; continue with `jev resume r1`"
    )


# ---- 5. create_run fails fast on real constraint violations ---------------------------


def test_create_run_check_violation_fails_fast_with_clear_error(
    store: Store, tmp_path: Path
) -> None:
    with pytest.raises(StoreError) as exc_info:
        store.create_run(Idea(text="x", repo_path=tmp_path), autonomy_mode="bogus")
    message = str(exc_info.value)
    assert "could not create run" in message
    assert "could not generate a unique run id" not in message


def test_create_run_explicit_id_collision_is_reported(store: Store, tmp_path: Path) -> None:
    store.create_run(Idea(text="x", repo_path=tmp_path), run_id="audit-fixed-id")
    with pytest.raises(StoreError, match="already exists"):
        store.create_run(Idea(text="y", repo_path=tmp_path), run_id="audit-fixed-id")


def test_create_run_retries_collisions_but_not_explicit_ids(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a PK/UNIQUE collision on runs.id retries (with a fresh id each time); an
    explicitly given id is tried exactly once."""
    calls: list[str] = []

    def fake_insert(self: Store, run_id: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(run_id)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: runs.id")

    monkeypatch.setattr(Store, "_insert_run", fake_insert)
    with pytest.raises(StoreError, match="could not generate a unique run id"):
        store.create_run(Idea(text="x", repo_path=tmp_path))
    assert len(calls) == 5
    assert len(set(calls)) == 5  # every retry mints a fresh id
    calls.clear()
    with pytest.raises(StoreError, match="already exists"):
        store.create_run(Idea(text="x", repo_path=tmp_path), run_id="given-id")
    assert calls == ["given-id"]  # no retry loop for an explicit id


def test_create_run_non_collision_integrity_error_does_not_retry(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_insert(self: Store, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise sqlite3.IntegrityError("CHECK constraint failed: autonomy_mode")

    monkeypatch.setattr(Store, "_insert_run", fake_insert)
    with pytest.raises(StoreError, match="could not create run"):
        store.create_run(Idea(text="x", repo_path=tmp_path))
    assert calls == 1


# ---- 6. similar() survives a broken embedder entry ------------------------------------


class _FragileEmbedder(SimHashEmbedder):
    """SimHash that blows up on one entry, like an embedder switched mid-cache."""

    def similarity(self, a: str, b: str) -> float:
        if b == "not-a-simhash":
            raise ValueError("simhash hex expected")
        return super().similarity(a, b)


def test_similar_skips_entries_that_break_the_embedder(store: Store, tmp_path: Path) -> None:
    config = _cache_config(semantic={"enabled": True})
    cache = Cache(store, config.cache, tmp_path)
    cache._embedder = _FragileEmbedder()
    text = decision_normalized_text(_req(state={"n": 1}))
    good = decision_cache_key(_req(state={"n": 1}), dispatcher_name="s", config=config, run_id="r")
    store.cache_put(
        "bad-entry", "decision", json.dumps({"simhash": "not-a-simhash"}), "{}", 0.1, 168
    )
    store.cache_put(good.key_hash, "decision", good.key_json, "{}", 0.1, 168)
    hits = cache.similar("decision", text, 0.5)
    assert [entry.key_hash for entry, _ in hits] == [good.key_hash]


# ---- 7. role cache keys carry the jev version -----------------------------------------


def _role_kwargs(tmp_path: Path, config: JevConfig) -> dict[str, Any]:
    return {
        "role": "planner",
        "worker": "api",
        "model": None,
        "effort": None,
        "prompt": "do x",
        "workdir": tmp_path,
        "repo": tmp_path,
        "template": None,
        "lesson_texts": [],
        "role_config_json": "{}",
        "config": config,
        "run_id": "r",
    }


def test_role_cache_key_carries_jev_version(tmp_path: Path) -> None:
    key = role_cache_key(**_role_kwargs(tmp_path, _cache_config()))
    assert json.loads(key.key_json)["jev_version"] == __version__


def test_role_cache_key_invalidates_on_version_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _role_kwargs(tmp_path, _cache_config())
    before = role_cache_key(**kwargs)
    monkeypatch.setattr("jev_agent.core.cache.__version__", "0.0.0-audit")
    after = role_cache_key(**kwargs)
    assert before.key_hash != after.key_hash


# ---- 8. cache-restore degrades to a review error, not a crash ------------------------


def test_role_cache_output_degrades_double_validation_failure_to_review_error() -> None:
    """A cached entry whose `parsed` fails its schema AND whose result fails the legacy
    validation must not crash the gate: it becomes a recorded review `error`."""
    value_json = json.dumps(
        {
            "result": {"status": "success", "summary": "cached", "raw_log_path": ""},
            "parsed": {"not": "a QualityReview"},
        }
    )
    output = _role_cache_output("code_reviewer", value_json)
    assert output.result.status == "error"
    assert output.parsed is None
    assert "QualityReview" in output.result.summary


# ---- 9. a paid model's quota exhaustion is terminal -----------------------------------


async def test_paid_model_quota_exhaustion_is_terminal(fixture_repo: Path) -> None:
    """`run_worker_with_fallback`'s quota ladder is free-models only: a paid model's
    `quota_exhausted` is returned as-is — one attempt, no fallback try."""
    config = JevConfig.model_validate(
        {"roles": {"planner": {"workers": ["api"], "models": ["openai/gpt-5-mini"]}}}
    )
    worker = FakeWorker(
        "fake", {"planner": FakeStep(status="quota_exhausted", summary="paid quota out")}
    )
    registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        pipeline = build_pipeline(fixture_repo, config, store, registry)
        run = await pipeline.start(Idea(text="add a module", repo_path=fixture_repo))
        planner = [a for a in store.list_attempts(run.id) if a.role == "planner"]
        assert len(planner) == 1  # no fallback attempt was made
        assert run.state is RunState.NEEDS_HUMAN


# ---- 10. tuning._normalised guards ----------------------------------------------------


def test_normalised_rejects_empty_weight_map() -> None:
    with pytest.raises(ValueError, match="empty"):
        _normalised({})


def test_normalised_rejects_all_zero_weights() -> None:
    with pytest.raises(ValueError, match="zero"):
        _normalised({"a": 0.0, "b": 0.0})


# ---- 12. chat backends never pass a "0.00" budget -------------------------------------


def test_claude_backend_never_passes_zero_budget(tmp_path: Path) -> None:
    backend = ClaudeChatBackend(root=tmp_path)
    argv = backend.build_argv(None, 0.004)
    assert argv[argv.index("--max-budget-usd") + 1] == "0.01"
    argv = backend.build_argv(None, 2.5)
    assert argv[argv.index("--max-budget-usd") + 1] == "2.50"
    assert "--max-budget-usd" not in backend.build_argv(None, 0.0)


# ---- 13. dispatch minors -------------------------------------------------------------


def _weights() -> dict[str, float]:
    return {c: 1.0 / len(LAYA_CRITERIA) for c in LAYA_CRITERIA}


def test_evaluate_rejects_fewer_than_two_options() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        evaluate(
            ["only"],
            {"choice": "only", "confidence": 0.9},
            {},
            _weights(),
            min_confidence=0.5,
            min_margin=0.1,
        )


def test_evaluate_reports_missing_cells() -> None:
    first = next(iter(LAYA_CRITERIA))
    scores = {("a", first): Score(4.0, 0.9)}
    with pytest.raises(KeyError, match="missing"):
        evaluate(
            ["a", "b"],
            {"choice": "a", "confidence": 0.9},
            scores,
            _weights(),
            min_confidence=0.5,
            min_margin=0.1,
        )


def test_check_size_rejects_empty_questions() -> None:
    with pytest.raises(DispatchError, match="no questions"):
        check_size({"state": {}, "questions": {}})


def test_retry_delay_parses_http_date() -> None:
    future = datetime.now(UTC) + timedelta(seconds=5)
    resp = httpx.Response(429, headers={"retry-after": format_datetime(future, usegmt=True)})
    delay = retry_delay(resp)
    assert 4.0 < delay <= 5.0
    # A date in the past means no wait; a far-future date is capped.
    past = datetime.now(UTC) - timedelta(seconds=60)
    assert retry_delay(httpx.Response(429, headers={"retry-after": format_datetime(past)})) == 0.0
    far = datetime.now(UTC) + timedelta(hours=1)
    assert retry_delay(httpx.Response(429, headers={"retry-after": format_datetime(far)})) == 10.0
    # Numeric headers still work.
    assert retry_delay(httpx.Response(429, headers={"retry-after": "3"})) == 3.0
    assert retry_delay(httpx.Response(429)) == 1.0  # DEFAULT_BACKOFF_S


class _ProvenanceServer:
    """System One fake: Jev's vote calls report one model, Laya's scoring calls another."""

    def __init__(self) -> None:
        self.vote_model = "jev-vote-model"
        self.score_model = "laya-score-model"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        kinds = {q["type"] for q in payload["questions"].values()}
        assert kinds == {"choice"} or kinds == {"score"}, kinds
        if "choice" in kinds:
            answers = {
                name: {
                    "type": "choice",
                    "choice": "retry",
                    "confidence": 0.99,
                    "probabilities": {},
                }
                for name in payload["questions"]
            }
            return httpx.Response(200, json={"model": self.vote_model, "answers": answers})
        answers = {}
        for qid, question in payload["questions"].items():
            match = re.search(r"Candidate answer to `(\w+)`: (\S+)", question["instructions"])
            assert match is not None, question["instructions"]
            answers[qid] = {
                "type": "score",
                "score": 4 if match.group(2) == "retry" else 0,
                "confidence": 0.99,
            }
        return httpx.Response(200, json={"model": self.score_model, "answers": answers})


async def test_consensus_decision_model_is_jevs_vote_model() -> None:
    """`Decision.model` is the model that made the decision (Jev's vote), not the model
    that ran Laya's scoring pass."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ProvenanceServer()))
    dispatcher = ConsensusDispatcher(JevDispatcher(JevSettings(), client=client))
    req = DecisionRequest(
        point="D4",
        state={"task": {"id": "T1"}},
        questions={"action": ["retry", "escalate"]},
    )
    decision = await dispatcher.decide(req)
    assert decision.answers == {"action": "retry"}
    assert decision.model == "jev-vote-model"
