"""The semantic/exact cache (issue #10): keys, store, harness hooks, tune dedup."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from jev_agent.config.schema import LAYA_CRITERIA, JevConfig
from jev_agent.core.assign import Assignment
from jev_agent.core.cache import (
    Cache,
    SimHashEmbedder,
    calibrate_thresholds,
    decision_cache_key,
    normalize_prompt,
    repo_fingerprint,
    role_cache_key,
    strip_volatile,
)
from jev_agent.core.harness import Harness
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    TaskCard,
    WorkerResult,
)
from jev_agent.core.store import Store
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.workers.base import WorkerOutput

# ---- fixtures ------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def run_id(store: Store, tmp_path: Path) -> str:
    return store.create_run(Idea(text="add search", repo_path=tmp_path)).id


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "one"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _req(point: str = "D1", state: dict[str, Any] | None = None) -> DecisionRequest:
    return DecisionRequest(
        point=point,  # type: ignore[arg-type]
        state={"idea": "add search", "round": 1, **(state or {})},
        questions={"action": ["plan", "ask"]},
    )


def _decision(point: str = "D1", dispatcher: str = "stub") -> Decision:
    return Decision(
        point=point,  # type: ignore[arg-type]
        answers={"action": "plan"},
        dispatcher=dispatcher,
        model="m",
        cost_usd=0.0003,
        latency_ms=400,
    )


class _StubDispatcher:
    name = "stub"

    def __init__(self, decision: Decision) -> None:
        self._decision = decision
        self.calls = 0

    async def decide(self, req: DecisionRequest) -> Decision:
        self.calls += 1
        return self._decision


def _harness(
    store: Store,
    repo: Path,
    dispatcher: Any,
    config: JevConfig | None = None,
    workers: Any = None,
) -> Harness:
    return Harness(
        store=store,
        config=config or JevConfig(),
        repo=repo,
        state_dir=repo / ".jev",
        dispatcher=dispatcher,
        workers=workers or Mock(),
        workspace=Mock(),
        commands=Mock(),
    )


def _plan_parsed() -> Plan:
    return Plan(
        summary="s",
        tasks=[TaskCard(id="T1", title="t", description="d")],
    )


class _FakeWorker:
    def __init__(self, result: WorkerResult, parsed: Any = None) -> None:
        self._output = WorkerOutput(result=result, parsed=parsed)
        self.calls = 0

    async def run(self, job: Any) -> WorkerOutput:
        self.calls += 1
        return self._output


class _FakeRegistry:
    def __init__(self, worker: _FakeWorker) -> None:
        self._worker = worker

    def get(self, name: str) -> _FakeWorker:
        return self._worker


def _success_result(tmp_path: Path, cost: float = 0.30) -> WorkerResult:
    return WorkerResult(
        status="success",
        summary="the plan looks good and covers all acceptance criteria",
        files_changed=[],
        cost_usd=cost,
        raw_log_path=tmp_path / "log.txt",
    )


# ---- key building / normalization ----------------------------------------------------


def test_strip_volatile_removes_rounds_and_counters() -> None:
    state = {
        "idea": "add search",
        "previous_rounds": [{"round": 1}],
        "spend_usd": 1.5,
        "budget_usd": 10.0,
        "attempt_no": 3,
        "created_at": "2026-10-15T10:00:00Z",
        "latency_ms": 400,
        "nested": {"tokens": 8000, "keep": "yes"},
    }
    stripped = strip_volatile(state)
    assert stripped == {"idea": "add search", "nested": {"keep": "yes"}}


def test_strip_volatile_keeps_bare_attempt_number() -> None:
    """The D5/D8 `attempt` number drives the retry/escalate ladder: it is
    decision-relevant, so two D5 requests differing only by attempt must key
    differently (regression: the volatile regex used to strip it, replaying the
    first D5 answer for every later attempt)."""
    assert strip_volatile({"attempt": 1, "tests": "failed"}) == {
        "attempt": 1,
        "tests": "failed",
    }
    assert strip_volatile({"attempt": 2, "tests": "failed"}) == {
        "attempt": 2,
        "tests": "failed",
    }
    # ...but the counter variants stay volatile.
    assert strip_volatile({"attempt_no": 3, "attempts": 3}) == {}


def test_normalize_prompt_replaces_volatile_bits(tmp_path: Path) -> None:
    prompt = (
        f"work in {tmp_path}/jev/abc123/task-x on run 9f2e4d6a8b1c2d3e\n"
        "at 2026-10-15T11:05:13Z id 550e8400-e29b-41d4-a716-446655440000\n"
        "keep the plan text identical"
    )
    normalized = normalize_prompt(prompt, tmp_path)
    assert str(tmp_path) not in normalized
    assert "550e8400" not in normalized
    assert "2026-10-15" not in normalized
    assert "9f2e4d6a8b1c2d3e" not in normalized
    assert "keep the plan text identical" in normalized
    assert "<repo>" in normalized and "<uuid>" in normalized and "<ts>" in normalized


def test_decision_key_stable_for_same_request(tmp_path: Path) -> None:
    config = JevConfig()
    key1 = decision_cache_key(_req(), dispatcher_name="stub", config=config, run_id="r1")
    key2 = decision_cache_key(_req(), dispatcher_name="stub", config=config, run_id="r2")
    assert key1.key_hash == key2.key_hash  # run id is provenance, not key material


def test_decision_key_changes_with_min_confidence(tmp_path: Path) -> None:
    key1 = decision_cache_key(_req(), dispatcher_name="stub", config=JevConfig(), run_id="r")
    changed = JevConfig.model_validate({"dispatcher": {"jev": {"min_confidence": 0.9}}})
    key2 = decision_cache_key(_req(), dispatcher_name="stub", config=changed, run_id="r")
    assert key1.key_hash != key2.key_hash


def test_repo_fingerprint_changes_on_repo_change(git_repo: Path) -> None:
    before = repo_fingerprint(git_repo)
    (git_repo / "f.txt").write_text("v2\n", encoding="utf-8")
    assert repo_fingerprint(git_repo) != before
    (git_repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    assert repo_fingerprint(git_repo) != before


def test_repo_fingerprint_fail_soft_on_non_repo(tmp_path: Path) -> None:
    assert repo_fingerprint(tmp_path) == "unknown"


def test_simhash_near_duplicates_score_high() -> None:
    embedder = SimHashEmbedder()
    base = "the plan looks good and covers all acceptance criteria in detail " * 20
    variant = base.replace("detail", "depth", 1)
    other = "completely unrelated text about cooking recipes and spices " * 20
    assert embedder.similarity(embedder.fingerprint(base), embedder.fingerprint(variant)) >= 0.90
    assert embedder.similarity(embedder.fingerprint(base), embedder.fingerprint(other)) < 0.75


# ---- store ---------------------------------------------------------------------------


def test_cache_put_get_counts_hits(store: Store) -> None:
    store.cache_put("h1", "decision", "{}", '{"a": 1}', 0.0003, 168)
    entry = store.cache_get("h1")
    assert entry is not None and entry.hit_count == 1
    entry = store.cache_get("h1")
    assert entry is not None and entry.hit_count == 2
    stats = store.cache_stats()
    assert stats.entries == 1 and stats.hits == 2
    assert stats.saved_usd == pytest.approx(0.0006)


def test_cache_get_expired_returns_none_and_deletes(store: Store) -> None:
    store.cache_put("h2", "decision", "{}", '{"a": 1}', 0.1, -1)  # already expired
    assert store.cache_get("h2") is None
    assert store.cache_stats().entries == 0


def test_cache_clear_filters(store: Store) -> None:
    store.cache_put("d1", "decision", "{}", "{}", 0.1, 168)
    store.cache_put("r1", "role", "{}", "{}", 0.2, 168)
    assert store.cache_clear(kind="decision") == 1
    assert store.cache_stats().entries == 1
    assert store.cache_clear() == 1


def test_cache_saved_by_run(store: Store) -> None:
    store.cache_put("k1", "decision", '{"run_id": "r1"}', "{}", 0.5, 168)
    store.cache_get("k1")
    store.cache_put("k2", "role", '{"run_id": "r2"}', "{}", 0.25, 168)
    assert store.cache_saved_by_run() == {"r1": pytest.approx(0.5)}


# ---- Harness.decide ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_caches_and_serves(store: Store, run_id: str, tmp_path: Path) -> None:
    stub = _StubDispatcher(_decision())
    harness = _harness(store, tmp_path, GuardedDispatcher(stub))
    req = _req()
    first = await harness.decide(run_id, None, req)
    assert stub.calls == 1
    assert first.details.get("cache_hit") is not True
    spend_after_first = store.get_run(run_id).spend_usd

    second = await harness.decide(run_id, None, req)
    assert stub.calls == 1  # served from cache
    assert second.details["cache_hit"] is True
    assert second.details["cached_from"] is not None
    assert second.cost_usd == 0
    assert second.answers == first.answers
    assert store.get_run(run_id).spend_usd == spend_after_first  # no extra spend
    assert len(store.list_decisions(run_id)) == 2  # the hit is a first-class row


@pytest.mark.asyncio
async def test_decide_volatile_state_still_hits(store: Store, run_id: str, tmp_path: Path) -> None:
    stub = _StubDispatcher(_decision())
    harness = _harness(store, tmp_path, GuardedDispatcher(stub))
    await harness.decide(run_id, None, _req())
    resumed = _req(state={"previous_rounds": [{"round": 1}], "spend_usd": 2.0, "attempt_no": 2})
    await harness.decide(run_id, None, resumed)
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_decide_config_change_invalidates(store: Store, run_id: str, tmp_path: Path) -> None:
    stub1 = _StubDispatcher(_decision())
    await _harness(store, tmp_path, GuardedDispatcher(stub1)).decide(run_id, None, _req())
    assert stub1.calls == 1
    changed = JevConfig.model_validate({"dispatcher": {"jev": {"min_confidence": 0.9}}})
    stub2 = _StubDispatcher(_decision())
    await _harness(store, tmp_path, GuardedDispatcher(stub2), config=changed).decide(
        run_id, None, _req()
    )
    assert stub2.calls == 1  # miss: different config fingerprint


def _d6_decision() -> Decision:
    return Decision(
        point="D6",
        answers={"verdict": "ship"},
        dispatcher="stub",
        model="m",
        cost_usd=0.0003,
    )


def _d6_req() -> DecisionRequest:
    return DecisionRequest(
        point="D6",
        state={"ship": "ready"},
        questions={"verdict": ["ship", "fix"]},
    )


@pytest.mark.asyncio
async def test_decide_d6_not_cached_by_default(store: Store, run_id: str, tmp_path: Path) -> None:
    stub = _StubDispatcher(_d6_decision())
    harness = _harness(store, tmp_path, GuardedDispatcher(stub))
    req = _d6_req()
    await harness.decide(run_id, None, req)
    await harness.decide(run_id, None, req)
    assert stub.calls == 2


@pytest.mark.asyncio
async def test_decide_d6_cached_when_enabled(store: Store, run_id: str, tmp_path: Path) -> None:
    stub = _StubDispatcher(_d6_decision())
    config = JevConfig.model_validate({"cache": {"d6_decisions": True}})
    harness = _harness(store, tmp_path, GuardedDispatcher(stub), config=config)
    req = _d6_req()
    await harness.decide(run_id, None, req)
    await harness.decide(run_id, None, req)
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_decide_rules_dispatcher_skips_cache(
    store: Store, run_id: str, tmp_path: Path
) -> None:
    harness = _harness(store, tmp_path, GuardedDispatcher(RulesDispatcher()))
    assert harness._decisions_cached(harness.dispatcher) is False


@pytest.mark.asyncio
async def test_decide_rules_fallback_answer_not_stored(
    store: Store, run_id: str, tmp_path: Path
) -> None:
    stub = _StubDispatcher(_decision(dispatcher="rules"))
    harness = _harness(store, tmp_path, GuardedDispatcher(stub))
    await harness.decide(run_id, None, _req())
    await harness.decide(run_id, None, _req())
    assert stub.calls == 2  # rules answers are never stored
    assert store.cache_stats().entries == 0


@pytest.mark.asyncio
async def test_decide_no_cache_disables(store: Store, run_id: str, tmp_path: Path) -> None:
    stub = _StubDispatcher(_decision())
    config = JevConfig.model_validate({"cache": {"enabled": False}})
    harness = _harness(store, tmp_path, GuardedDispatcher(stub), config=config)
    await harness.decide(run_id, None, _req())
    await harness.decide(run_id, None, _req())
    assert stub.calls == 2
    assert store.cache_stats().entries == 0


@pytest.mark.asyncio
async def test_decide_shadow_mode_measures_without_serving(
    store: Store, run_id: str, tmp_path: Path
) -> None:
    config = JevConfig.model_validate(
        {"cache": {"semantic": {"enabled": True, "mode": "shadow", "decision_threshold": 0.90}}}
    )
    notes = "the quick brown fox jumps over the lazy dog " * 10
    live_req = _req(state={"notes": notes})
    # A near-duplicate request already in the cache (different exact key).
    variant = _req(state={"notes": notes.replace("lazy", "sleepy", 1)})
    key = decision_cache_key(variant, dispatcher_name="stub", config=config, run_id=run_id)
    store.cache_put(
        key.key_hash, "decision", key.key_json, _decision().model_dump_json(), 0.0003, 168
    )
    stub = _StubDispatcher(_decision())
    harness = _harness(store, tmp_path, GuardedDispatcher(stub), config=config)
    decision = await harness.decide(run_id, None, live_req)
    assert stub.calls == 1  # the live call still happened
    assert decision.details.get("cache_hit") is not True  # nothing served in shadow mode
    rows = store._conn.execute("SELECT agreed FROM cache_shadow").fetchall()
    assert len(rows) == 1 and rows[0][0] == 1  # same answers: agreement logged


@pytest.mark.asyncio
async def test_decide_semantic_serve_counts_hit_in_stats(
    store: Store, run_id: str, tmp_path: Path
) -> None:
    """A semantically served decision must account exactly like an exact hit:
    hit_count, saved dollars, and LRU recency (regression: serve-mode hits came
    from `Cache.similar()`'s scan, which never counted them, so `jev cache stats`
    and the dashboard underreported every semantic hit)."""
    config = JevConfig.model_validate(
        {"cache": {"semantic": {"enabled": True, "mode": "serve", "decision_threshold": 0.90}}}
    )
    notes = "the quick brown fox jumps over the lazy dog " * 10
    variant = _req(state={"notes": notes.replace("lazy", "sleepy", 1)})
    live_req = _req(state={"notes": notes})
    stub = _StubDispatcher(_decision())
    harness = _harness(store, tmp_path, GuardedDispatcher(stub), config=config)
    await harness.decide(run_id, None, variant)  # stored, with run_id provenance
    assert stub.calls == 1
    decision = await harness.decide(run_id, None, live_req)  # near-dup: semantic serve
    assert stub.calls == 1  # no live call for the near-duplicate
    assert decision.details.get("semantic_match") is True
    stats = store.cache_stats()
    assert stats.hits == 1
    assert stats.saved_usd == pytest.approx(0.0003)
    assert store.cache_saved_by_run() == {run_id: pytest.approx(0.0003)}


# ---- Harness.run_worker ---------------------------------------------------------------


def _run_kwargs(run_id: str, git_repo: Path, role: str = "planner") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_id": None,
        "attempt_no": 1,
        "role": role,
        "assignment": Assignment(worker="api", model="m", effort="medium"),
        "prompt": "review this plan carefully and completely " * 10,
        "workdir": git_repo,
        "log_name": "planner",
        "lesson_ids": [],
    }


@pytest.mark.asyncio
async def test_run_worker_caches_success(store: Store, run_id: str, git_repo: Path) -> None:
    worker = _FakeWorker(_success_result(git_repo), parsed=_plan_parsed())
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker))
    kwargs = _run_kwargs(run_id, git_repo)
    first = await harness.run_worker(**kwargs)
    assert worker.calls == 1
    assert "[served from cache]" not in first.result.summary
    spend_after_first = store.get_run(run_id).spend_usd

    second = await harness.run_worker(**kwargs)
    assert worker.calls == 1  # served from cache
    assert second.result.summary.endswith("[served from cache]")
    assert second.result.cost_usd == 0
    assert store.get_run(run_id).spend_usd == spend_after_first
    assert len(store.list_attempts(run_id)) == 2  # the hit is still recorded


@pytest.mark.asyncio
async def test_run_worker_cache_hit_restores_parsed_output(
    store: Store, run_id: str, git_repo: Path
) -> None:
    """A cached role hit must behave like the live call: consumers such as the quality
    gate read `output.parsed`, so the structured output has to survive the round-trip
    (regression: the hit path dropped it, making the gate pass on a cached review)."""
    worker = _FakeWorker(_success_result(git_repo), parsed=_plan_parsed())
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker))
    kwargs = _run_kwargs(run_id, git_repo)
    first = await harness.run_worker(**kwargs)
    assert isinstance(first.parsed, Plan)

    second = await harness.run_worker(**kwargs)
    assert worker.calls == 1  # served from cache
    assert isinstance(second.parsed, Plan)
    assert second.parsed == first.parsed


@pytest.mark.asyncio
async def test_run_worker_repo_change_invalidates(
    store: Store, run_id: str, git_repo: Path
) -> None:
    worker = _FakeWorker(_success_result(git_repo), parsed=_plan_parsed())
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker))
    kwargs = _run_kwargs(run_id, git_repo)
    await harness.run_worker(**kwargs)
    (git_repo / "f.txt").write_text("v2\n", encoding="utf-8")
    await harness.run_worker(**kwargs)
    assert worker.calls == 2


@pytest.mark.asyncio
async def test_run_worker_coder_never_cached(store: Store, run_id: str, git_repo: Path) -> None:
    worker = _FakeWorker(_success_result(git_repo))
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker))
    kwargs = _run_kwargs(run_id, git_repo, role="coder")
    await harness.run_worker(**kwargs)
    await harness.run_worker(**kwargs)
    assert worker.calls == 2
    assert store.cache_stats().entries == 0


@pytest.mark.asyncio
async def test_run_worker_learner_never_cached(store: Store, run_id: str, git_repo: Path) -> None:
    worker = _FakeWorker(_success_result(git_repo))
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker))
    kwargs = _run_kwargs(run_id, git_repo, role="learner")
    await harness.run_worker(**kwargs)
    await harness.run_worker(**kwargs)
    assert worker.calls == 2
    assert store.cache_stats().entries == 0


@pytest.mark.asyncio
async def test_run_worker_failure_never_cached(store: Store, run_id: str, git_repo: Path) -> None:
    failed = WorkerResult(status="failure", summary="boom", raw_log_path=git_repo / "log.txt")
    worker = _FakeWorker(failed, parsed=_plan_parsed())
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker))
    kwargs = _run_kwargs(run_id, git_repo)
    await harness.run_worker(**kwargs)
    await harness.run_worker(**kwargs)
    assert worker.calls == 2
    assert store.cache_stats().entries == 0


@pytest.mark.asyncio
async def test_run_worker_cli_worker_excluded_by_default(
    store: Store, run_id: str, git_repo: Path
) -> None:
    worker = _FakeWorker(_success_result(git_repo), parsed=_plan_parsed())
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker))
    kwargs = _run_kwargs(run_id, git_repo)
    kwargs["assignment"] = Assignment(worker="claude_code", model="m", effort="medium")
    await harness.run_worker(**kwargs)
    await harness.run_worker(**kwargs)
    assert worker.calls == 2
    assert store.cache_stats().entries == 0


@pytest.mark.asyncio
async def test_run_worker_cli_worker_cached_when_enabled(
    store: Store, run_id: str, git_repo: Path
) -> None:
    worker = _FakeWorker(_success_result(git_repo), parsed=_plan_parsed())
    config = JevConfig.model_validate({"cache": {"cli_workers": True}})
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker), config=config)
    kwargs = _run_kwargs(run_id, git_repo)
    kwargs["assignment"] = Assignment(worker="codex", model="m", effort="medium")
    await harness.run_worker(**kwargs)
    await harness.run_worker(**kwargs)
    assert worker.calls == 1


@pytest.mark.asyncio
async def test_run_worker_no_cache_disables(store: Store, run_id: str, git_repo: Path) -> None:
    worker = _FakeWorker(_success_result(git_repo), parsed=_plan_parsed())
    config = JevConfig.model_validate({"cache": {"enabled": False}})
    harness = _harness(store, git_repo, Mock(), workers=_FakeRegistry(worker), config=config)
    kwargs = _run_kwargs(run_id, git_repo)
    await harness.run_worker(**kwargs)
    await harness.run_worker(**kwargs)
    assert worker.calls == 2


# ---- role keys -----------------------------------------------------------------------


def test_role_key_changes_with_repo_and_template(git_repo: Path) -> None:
    config = JevConfig()
    base: dict[str, Any] = {
        "role": "reviewer",
        "worker": "api",
        "model": "m",
        "effort": "medium",
        "prompt": "review the diff",
        "workdir": git_repo,
        "repo": git_repo,
        "template": None,
        "lesson_texts": [],
        "role_config_json": "{}",
        "config": config,
        "run_id": "r",
    }
    key1 = role_cache_key(**base)
    (git_repo / "f.txt").write_text("v2\n", encoding="utf-8")
    assert role_cache_key(**base).key_hash != key1.key_hash  # repo change invalidates
    template = git_repo / "tpl.md"
    template.write_text("hello", encoding="utf-8")
    key3 = role_cache_key(**{**base, "template": template})
    template.write_text("hello changed", encoding="utf-8")
    assert role_cache_key(**{**base, "template": template}).key_hash != key3.key_hash


def test_role_key_stable_for_same_inputs(git_repo: Path) -> None:
    config = JevConfig()
    kwargs: dict[str, Any] = {
        "role": "planner",
        "worker": "api",
        "model": "m",
        "effort": "medium",
        "prompt": f"plan in {git_repo}/jev/abc/task at 2026-10-15T11:00:00Z",
        "workdir": git_repo,
        "repo": git_repo,
        "template": None,
        "lesson_texts": ["L1"],
        "role_config_json": "{}",
        "config": config,
        "run_id": "r",
    }
    assert role_cache_key(**kwargs).key_hash == role_cache_key(**kwargs).key_hash


# ---- tune dedup ----------------------------------------------------------------------


def _consensus_decision(
    store: Store,
    run_id: str,
    point: str,
    answer: str,
    *,
    cached_from: int | None = None,
) -> None:
    req = DecisionRequest(
        point=point,  # type: ignore[arg-type]
        state={},
        questions={"action": ["plan", "ask"]},
    )
    details: dict[str, Any] = {
        "laya": {
            "action": {
                "choice": answer,
                "scores": {answer: {criterion: 4.0 for criterion in LAYA_CRITERIA}},
            }
        }
    }
    if cached_from is not None:
        details["cached_from"] = cached_from
        details["cache_hit"] = True
    store.record_decision(
        run_id,
        None,
        req,
        Decision(
            point=point,  # type: ignore[arg-type]
            answers={"action": answer},
            dispatcher="consensus",
            details=details,
        ),
    )


# ---- cache policy object ---------------------------------------------------------------


def test_cache_disabled_short_circuits(store: Store, tmp_path: Path) -> None:
    config = JevConfig.model_validate({"cache": {"enabled": False}})
    cache = Cache(store, config.cache, tmp_path)
    assert cache.usable is False
    key = decision_cache_key(_req(), dispatcher_name="s", config=config, run_id="r")
    store.cache_put(key.key_hash, "decision", key.key_json, "{}", 0.1, 168)
    assert cache.get(key) is None  # disabled: never serve


def test_cache_prune_evicts_lru(store: Store, tmp_path: Path) -> None:
    config = JevConfig.model_validate({"cache": {"max_entries": 3}})
    cache = Cache(store, config.cache, tmp_path)
    for i in range(5):
        key = decision_cache_key(
            _req(state={"n": i}), dispatcher_name="s", config=config, run_id="r"
        )
        store.cache_put(key.key_hash, "decision", key.key_json, "{}", 0.1, 168)
    cache.prune()
    assert store.cache_stats().entries == 3


# --- P7: blind handlers record the failure ------------------------------------


def test_repo_fingerprint_records_failure_and_stays_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import jev_agent.core.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_git", Mock(side_effect=RuntimeError("git exploded")))
    with caplog.at_level("DEBUG", logger="jev_agent.core.cache"):
        assert repo_fingerprint(tmp_path) == "unknown"
    assert any(
        "RuntimeError" in rec.getMessage() and "git exploded" in rec.getMessage()
        for rec in caplog.records
    )


class _ExplodingEmbedder:
    """An embedder from another generation: similarity always blows up."""

    def fingerprint(self, text: str) -> str:
        return "00" * 8

    def similarity(self, a: str, b: str) -> float:
        raise RuntimeError("cross-embedder mismatch")


def test_similar_skips_incompatible_entry_and_records(
    store: Store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A stale entry written under another embedder must not crash the scan;
    the skip records the failure's type + message (P7 trust-boundary catch)."""
    config = JevConfig.model_validate({"cache": {"semantic": {"enabled": True}}})
    cache = Cache(store, config.cache, tmp_path)
    cache._embedder = _ExplodingEmbedder()
    store.cache_put("deadbeef", "role", '{"simhash": "0011223344556677"}', "{}", 0.1, 168)
    with caplog.at_level("DEBUG", logger="jev_agent.core.cache"):
        assert cache.similar("role", "some prompt", 0.5) == []
    assert any(
        "RuntimeError" in rec.getMessage() and "cross-embedder mismatch" in rec.getMessage()
        for rec in caplog.records
    )


# ---- calibration (issue #254: shared by `jev cache calibrate` and the dashboard) ----


def _record(
    store: Store, run_id: str, point: str, answer: str, dispatcher: str = "consensus"
) -> None:
    store.record_decision(
        run_id,
        None,
        DecisionRequest(
            point=point,  # type: ignore[arg-type]
            state={},
            questions={"action": ["plan", "ask"]},
        ),
        Decision(
            point=point,  # type: ignore[arg-type]
            answers={"action": answer},
            dispatcher=dispatcher,
        ),
    )


def test_calibrate_empty_history(store: Store) -> None:
    result = calibrate_thresholds(store)
    assert result.pairs == 0
    assert result.points == 0
    assert result.best is None
    assert all(row.pairs == 0 and row.probability is None for row in result.rows)


def test_calibrate_identical_decisions_agree(store: Store, run_id: str) -> None:
    _record(store, run_id, "D1", "plan")
    _record(store, run_id, "D1", "plan")
    result = calibrate_thresholds(store)
    assert result.pairs == 1
    assert result.points == 1
    assert result.best == 0.99
    for row in result.rows:
        assert row.pairs == 1
        assert row.probability == 1.0


def test_calibrate_disagreement_blocks_candidate(store: Store, run_id: str) -> None:
    _record(store, run_id, "D1", "plan")
    _record(store, run_id, "D1", "plan")
    _record(store, run_id, "D1", "ask")
    result = calibrate_thresholds(store)
    assert result.pairs == 3
    assert result.best is None
    for row in result.rows:
        assert row.pairs == 3
        assert row.probability == pytest.approx(1 / 3)


def test_calibrate_ignores_rules_fallback(store: Store, run_id: str) -> None:
    _record(store, run_id, "D1", "plan")
    _record(store, run_id, "D1", "plan")
    _record(store, run_id, "D1", "ask", dispatcher="rules")
    result = calibrate_thresholds(store)
    assert result.pairs == 1
    assert result.points == 1
    assert result.best == 0.99


# ---- cache_count (issue #254: the clear confirmation counts what it clears) --------


def test_cache_count_matches_clear_filters(store: Store) -> None:
    store.cache_put("a1", "decision", "{}", "{}", 0.1, 168)
    store.cache_put("a2", "decision", "{}", "{}", 0.1, 168)
    store.cache_put("b1", "role", "{}", "{}", 0.1, 168)
    assert store.cache_count() == 3
    assert store.cache_count(kind="decision") == 2
    assert store.cache_count(kind="role") == 1
    assert store.cache_count(older_than_hours=0) == 3
    assert store.cache_count(older_than_hours=10000) == 0
    assert store.cache_count(kind="decision", older_than_hours=10000) == 0
    assert store.cache_clear(kind="decision") == 2
    assert store.cache_count() == 1
