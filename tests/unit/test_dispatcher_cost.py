"""Cost-aware routing and pre-run cost estimates (workstream L).

Routing: `MultiProviderJevDispatcher` prefers the cheaper leg when both are reachable,
without ever dropping a leg -- the fallback chain (other leg, then the harness guard)
stays intact, and the quality floor (confidence gate) applies whichever leg answers.
Every cost-influenced ordering is logged and recorded in `Decision.details`.

Estimates: `core.budget.estimate_run_cost` gives a range with a stated basis -- history
of ended runs where available, model pricing times expected tokens otherwise -- and
`Budget.estimate_verdict` wires it into the budget as an advisory warn/refuse.
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.config.schema import BudgetConfig, CostSettings, JevSettings
from jev_agent.core.budget import (
    ESTIMATE_STATES,
    Budget,
    CostEstimate,
    approval_budget_block,
    estimate_run_cost,
    format_estimate,
)
from jev_agent.core.models import (
    Decision,
    DecisionPoint,
    DecisionRequest,
    Idea,
    Plan,
    RunState,
    TaskCard,
    TaskState,
)
from jev_agent.core.store import Store
from jev_agent.dispatch.base import Dispatcher, DispatchError
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.jev import (
    OPENROUTER_PROVIDER,
    MultiProviderJevDispatcher,
    cost_from_usage,
)
from jev_agent.dispatch.rules import RulesDispatcher

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jev"
NATIVE_URL = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_URL = "https://openrouter.ai/api/v1/systemone"


def _d4() -> DecisionRequest:
    return DecisionRequest(
        point="D4",
        state={"task": {"id": "T3", "title": "Add rate limiting", "criteria_count": 3}},
        questions={
            "complexity": ["trivial", "standard", "hard"],
            "worker": ["claude_code", "codex", "api"],
            "model": ["anthropic/claude-sonnet-4.6", "openai/gpt-5.1-codex"],
            "effort": ["low", "medium", "high"],
        },
    )


class Recorder:
    """A MockTransport handler that replays responses and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def _ok(name: str = "d4_openrouter_success.json") -> httpx.Response:
    return httpx.Response(200, json=json.loads((FIXTURES / name).read_text()))


def _no_cost_ok() -> httpx.Response:
    """A success whose usage carries input tokens but no `cost` (native TypeSafe)."""
    body = json.loads((FIXTURES / "d4_openrouter_success.json").read_text())
    assert isinstance(body["usage"], dict)
    del body["usage"]["cost"]
    return httpx.Response(200, json=body)


def _dispatcher(
    recorder: Recorder,
    cost: CostSettings | None = None,
) -> MultiProviderJevDispatcher:
    async def fake_sleep(seconds: float) -> None:
        pass

    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return MultiProviderJevDispatcher(
        JevSettings.for_native(),
        JevSettings(),
        client=client,
        sleep=fake_sleep,
        cost=cost,
    )


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-native-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")


# ---- cost-aware leg ordering -------------------------------------------------


def test_implements_dispatcher_protocol_with_cost() -> None:
    dispatcher = MultiProviderJevDispatcher(cost=CostSettings())
    assert isinstance(dispatcher, Dispatcher)
    assert dispatcher.name == "jev"


async def test_cheaper_leg_first(caplog: pytest.LogCaptureFixture) -> None:
    rec = Recorder(_ok())
    dispatcher = _dispatcher(
        rec,
        CostSettings(native_price_per_million=0.05, openrouter_price_per_million=0.01),
    )
    with caplog.at_level(logging.INFO, logger="jev_agent.dispatch.jev"):
        decision = await dispatcher.decide(_d4())

    assert rec.urls() == [OPENROUTER_URL]
    assert decision.details["provider"] == OPENROUTER_PROVIDER
    reason = decision.details.get("routing_reason")
    assert isinstance(reason, str) and "openrouter" in reason
    assert any("cost-aware routing" in record.message for record in caplog.records)


async def test_equal_prices_keep_native_first() -> None:
    rec = Recorder(_ok())
    dispatcher = _dispatcher(rec, CostSettings())  # both legs $0.042/1M
    decision = await dispatcher.decide(_d4())

    assert rec.urls() == [NATIVE_URL]
    assert "routing_reason" not in decision.details


async def test_prefer_cheaper_leg_off_keeps_native_first() -> None:
    rec = Recorder(_ok())
    dispatcher = _dispatcher(
        rec,
        CostSettings(
            prefer_cheaper_leg=False,
            native_price_per_million=0.05,
            openrouter_price_per_million=0.01,
        ),
    )
    decision = await dispatcher.decide(_d4())

    assert rec.urls() == [NATIVE_URL]
    assert "routing_reason" not in decision.details


async def test_keyless_cheaper_leg_sorts_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cheaper leg without a key does not get tried first and fail noisily."""
    monkeypatch.delenv("OPENROUTER_API_KEY")
    rec = Recorder(_ok())
    dispatcher = _dispatcher(
        rec,
        CostSettings(native_price_per_million=0.05, openrouter_price_per_million=0.01),
    )
    await dispatcher.decide(_d4())

    assert rec.urls() == [NATIVE_URL]


async def test_fallback_chain_intact_when_first_leg_fails() -> None:
    """Cheaper leg fails -> the other leg still serves; nothing is dropped."""
    rec = Recorder(
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(500, json={"error": "boom"}),
        _ok(),
    )
    dispatcher = _dispatcher(
        rec,
        CostSettings(native_price_per_million=0.05, openrouter_price_per_million=0.01),
    )
    decision = await dispatcher.decide(_d4())

    assert OPENROUTER_URL in rec.urls()  # the cheaper leg was tried first...
    assert NATIVE_URL in rec.urls()  # ...and the other leg still served on failure
    assert decision.details["provider"] == "native"


async def test_both_legs_failing_raises_for_the_guard() -> None:
    rec = Recorder(*[httpx.Response(500, json={"error": "boom"})] * 4)
    dispatcher = _dispatcher(rec, CostSettings())
    with pytest.raises(DispatchError):
        await dispatcher.decide(_d4())


async def test_request_problem_still_raises_at_once() -> None:
    """A 422 would fail on both legs identically: no retry on the other leg."""
    body = json.loads((FIXTURES / "error_422.json").read_text())
    rec = Recorder(httpx.Response(422, json=body))
    dispatcher = _dispatcher(
        rec,
        CostSettings(native_price_per_million=0.05, openrouter_price_per_million=0.01),
    )
    with pytest.raises(DispatchError, match="422"):
        await dispatcher.decide(_d4())
    assert len(rec.requests) == 1


async def test_quality_floor_applies_to_the_cheaper_leg() -> None:
    """Cost never lowers the bar: low confidence on the cheaper leg still raises."""
    rec = Recorder(_ok("d4_low_confidence.json"))
    dispatcher = _dispatcher(
        rec,
        CostSettings(native_price_per_million=0.05, openrouter_price_per_million=0.01),
    )
    with pytest.raises(DispatchError, match="low confidence"):
        await dispatcher.decide(_d4())
    assert rec.urls() == [OPENROUTER_URL]  # it did try the cheaper leg first


# ---- config-driven decision pricing ------------------------------------------


async def test_configured_price_prices_decision_cost() -> None:
    rec = Recorder(_no_cost_ok())  # 612 input tokens, no usage.cost
    dispatcher = _dispatcher(
        rec,
        CostSettings(native_price_per_million=0.10, openrouter_price_per_million=0.20),
    )
    decision = await dispatcher.decide(_d4())

    assert decision.details["provider"] == "native"
    assert decision.cost_usd == pytest.approx(612 * 0.10 / 1_000_000)


async def test_usage_cost_still_wins_over_configured_price() -> None:
    rec = Recorder(_ok())  # usage.cost = 2.57e-05
    dispatcher = _dispatcher(rec, CostSettings(native_price_per_million=10.0))
    decision = await dispatcher.decide(_d4())

    assert decision.cost_usd == pytest.approx(2.57e-05)


def test_cost_from_usage_default_keeps_old_price() -> None:
    assert cost_from_usage({"input_tokens": 1_000_000}) == pytest.approx(0.042)
    assert cost_from_usage({"input_tokens": 1_000_000}, 0.10 / 1_000_000) == pytest.approx(0.10)


def test_guard_still_falls_back_to_rules() -> None:
    """The chain Jev -> OpenRouter -> rules is untouched by cost-awareness."""
    guarded = GuardedDispatcher(MultiProviderJevDispatcher(cost=CostSettings()), RulesDispatcher())
    assert guarded.fallback is not None and guarded.fallback.name == "rules"


# ---- config schema -----------------------------------------------------------


def test_cost_settings_defaults() -> None:
    cost = CostSettings()
    assert cost.prefer_cheaper_leg is True
    assert cost.native_price_per_million == pytest.approx(0.042)
    assert cost.min_history_runs == 3
    assert cost.warn_fraction < cost.refuse_fraction


def test_cost_settings_rejects_bad_fractions() -> None:
    with pytest.raises(ValueError, match="warn_fraction"):
        CostSettings(warn_fraction=1.5, refuse_fraction=1.0)
    with pytest.raises(ValueError, match="history_runs"):
        CostSettings(min_history_runs=5, history_runs=2)


def test_dispatcher_config_carries_cost() -> None:
    from jev_agent.config.schema import DispatcherConfig

    assert DispatcherConfig().cost.prefer_cheaper_leg is True


# ---- pre-run estimates -------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / ".jev" / "state.db") as s:
        yield s


def _idea(tmp_path: Path) -> Idea:
    return Idea(text="add search", repo_path=tmp_path / "repo", context_files=[])


def _plan(*ids: str) -> Plan:
    return Plan(
        summary="s",
        tasks=[
            TaskCard(
                id=tid,
                title=f"title {tid}",
                description="d",
                files_hint=["src/"],
                acceptance_criteria=["works"],
                test_command="pytest",
            )
            for tid in ids
        ],
    )


def _req(point: DecisionPoint) -> DecisionRequest:
    return DecisionRequest(point=point, state={}, questions={"q": ["a", "b"]})


def _dec(point: DecisionPoint, cost_usd: float) -> Decision:
    return Decision(point=point, answers={"q": "a"}, dispatcher="rules", cost_usd=cost_usd)


def _ended_run(
    store: Store,
    tmp_path: Path,
    *,
    planning_cost: float,
    task_costs: list[float],
    integrating_extra: float,
    learning_cost: float,
) -> None:
    """One DONE run: planning decisions, per-task spend, learning, integration residual."""
    run = store.create_run(_idea(tmp_path))
    plan = _plan(*[f"T{i + 1}" for i in range(len(task_costs))])
    store.set_run_plan(run.id, plan, 1)
    store.upsert_tasks(run.id, plan)
    store.record_decision(run.id, None, _req("D1"), _dec("D1", planning_cost / 2), None)
    store.record_decision(run.id, None, _req("D2"), _dec("D2", planning_cost / 2), None)
    # The harness adds every decision's cost to the run's spend; mirror that here.
    store.add_spend(run.id, None, planning_cost)
    for task, amount in zip(plan.tasks, task_costs, strict=True):
        store.add_spend(run.id, task.id, amount)
    if learning_cost:
        store.record_decision(run.id, None, _req("D7"), _dec("D7", learning_cost), None)
        store.add_spend(run.id, None, learning_cost)
    if integrating_extra:
        store.add_spend(run.id, None, integrating_extra)  # run-level: the residual
    store.set_run_state(run.id, RunState.DONE)


def _awaiting_run(store: Store, tmp_path: Path, *task_ids: str) -> Any:
    run = store.create_run(_idea(tmp_path))
    store.set_run_plan(run.id, _plan(*task_ids), 1)
    store.set_run_state(run.id, RunState.AWAITING_APPROVAL)
    return store.get_run(run.id)


def test_history_estimate_uses_per_phase_percentiles(store: Store, tmp_path: Path) -> None:
    for integrating in (0.1, 0.2, 0.3, 0.4):
        _ended_run(
            store,
            tmp_path,
            planning_cost=1.0,
            task_costs=[0.5, 0.5],
            integrating_extra=integrating,
            learning_cost=0.1,
        )
    run = _awaiting_run(store, tmp_path, "T1", "T2", "T3")
    estimate = estimate_run_cost(store, run, CostSettings())

    assert estimate.basis == "history of 4 runs"
    assert estimate.task_count == 3
    # executing: p25/p75 of eight 0.5 task spends, times 3 tasks
    # integrating: p25/p75 of [0.1, 0.2, 0.3, 0.4]; learning: 0.1
    assert estimate.low_usd == pytest.approx(1.5 + 0.175 + 0.1)
    assert estimate.high_usd == pytest.approx(1.5 + 0.325 + 0.1)
    assert estimate.spent_usd == pytest.approx(0.0)


def test_estimate_covers_remaining_phases_by_state(store: Store, tmp_path: Path) -> None:
    for planning in (1.0, 2.0, 3.0, 4.0):
        _ended_run(
            store,
            tmp_path,
            planning_cost=planning,
            task_costs=[0.5, 0.5],
            integrating_extra=0.2,
            learning_cost=0.1,
        )
    run = store.create_run(_idea(tmp_path))
    store.set_run_plan(run.id, _plan("T1", "T2"), 1)
    store.set_run_state(run.id, RunState.PLANNING)  # planning not done yet
    run = store.get_run(run.id)
    estimate = estimate_run_cost(store, run, CostSettings())

    # planning p25/p75 of [1,2,3,4] = 1.75/3.25; the rest as in the previous test
    assert estimate.low_usd == pytest.approx(1.75 + 1.0 + 0.2 + 0.1)
    assert estimate.high_usd == pytest.approx(3.25 + 1.0 + 0.2 + 0.1)


def test_estimate_adds_already_spent(store: Store, tmp_path: Path) -> None:
    for _ in range(3):
        _ended_run(
            store,
            tmp_path,
            planning_cost=1.0,
            task_costs=[0.5],
            integrating_extra=0.2,
            learning_cost=0.1,
        )
    run = _awaiting_run(store, tmp_path, "T1")
    store.add_spend(run.id, None, 0.4)  # planning already cost real money
    run = store.get_run(run.id)
    estimate = estimate_run_cost(store, run, CostSettings())

    assert estimate.spent_usd == pytest.approx(0.4)
    assert estimate.low_usd == pytest.approx(0.4 + 0.5 + 0.2 + 0.1)
    assert estimate.high_usd == pytest.approx(0.4 + 0.5 + 0.2 + 0.1)


def test_pricing_fallback_without_history(store: Store, tmp_path: Path) -> None:
    run = _awaiting_run(store, tmp_path, "T1", "T2")
    cost = CostSettings()
    estimate = estimate_run_cost(store, run, cost)

    assert estimate.basis == "model pricing (no run history yet)"
    leg = min(cost.native_price_per_million, cost.openrouter_price_per_million)
    decision = cost.tokens_per_decision / 1_000_000 * leg
    task = cost.tokens_per_task / 1_000_000 * cost.worker_price_per_million
    mid = 2 * task + 2 * decision + decision  # executing + integrating + learning
    assert estimate.low_usd == pytest.approx(mid / 2)
    assert estimate.high_usd == pytest.approx(mid * 2)
    assert estimate.low_usd < estimate.high_usd  # a range, never a single number


def test_partial_history_says_so(store: Store, tmp_path: Path) -> None:
    # Two runs: enough task samples for history, too few runs for the run phases.
    for _ in range(2):
        _ended_run(
            store,
            tmp_path,
            planning_cost=1.0,
            task_costs=[0.5, 0.5],
            integrating_extra=0.2,
            learning_cost=0.1,
        )
    run = _awaiting_run(store, tmp_path, "T1")
    estimate = estimate_run_cost(store, run, CostSettings())

    assert estimate.basis == "history of 2 runs + model pricing (partial)"
    assert estimate.low_usd < estimate.high_usd


def test_estimate_counts_unfinished_stored_tasks(store: Store, tmp_path: Path) -> None:
    for _ in range(3):
        _ended_run(
            store,
            tmp_path,
            planning_cost=1.0,
            task_costs=[0.5],
            integrating_extra=0.2,
            learning_cost=0.1,
        )
    run = store.create_run(_idea(tmp_path))
    plan = _plan("T1", "T2", "T3")
    store.set_run_plan(run.id, plan, 1)
    store.upsert_tasks(run.id, plan)
    store.set_task_state(run.id, "T1", TaskState.DONE)
    store.set_run_state(run.id, RunState.EXECUTING)
    run = store.get_run(run.id)
    estimate = estimate_run_cost(store, run, CostSettings())

    assert estimate.task_count == 2  # T1 is done; only T2 and T3 remain


def test_format_estimate_shows_range_and_basis() -> None:
    text = format_estimate(
        CostEstimate(
            spent_usd=0.4,
            low_usd=1.2,
            high_usd=3.4,
            task_count=2,
            basis="history of 4 runs",
        )
    )
    assert "$1.20" in text and "$3.40" in text
    assert "history of 4 runs" in text


def test_estimate_states_are_the_approval_gates() -> None:
    assert RunState.AWAITING_APPROVAL in ESTIMATE_STATES
    assert RunState.DONE not in ESTIMATE_STATES
    assert RunState.EXECUTING not in ESTIMATE_STATES


# ---- budget wiring -----------------------------------------------------------


def _verdict_for(budget: Budget, high: float) -> Any:
    estimate = CostEstimate(spent_usd=0.0, low_usd=0.5, high_usd=high, task_count=1, basis="b")
    return budget.estimate_verdict(estimate, warn_fraction=0.8, refuse_fraction=1.0)


def test_estimate_verdict_ok_warn_refuse() -> None:
    budget = Budget.from_config(BudgetConfig(per_run_usd=5.0, per_task_usd=1.5))

    ok = _verdict_for(budget, 3.0)
    assert ok.level == "ok"
    assert ok.message is None

    warn = _verdict_for(budget, 4.5)
    assert warn.level == "warn"
    assert warn.message is not None and "$5.00" in warn.message

    refused = _verdict_for(budget, 5.0)
    assert refused.level == "refuse"
    assert refused.message is not None and "--force" in refused.message


def test_verdict_uses_the_high_end() -> None:
    """A range whose pessimistic end reaches the cap refuses, even if the low end is fine."""
    budget = Budget.from_config(BudgetConfig(per_run_usd=5.0, per_task_usd=1.5))
    assert _verdict_for(budget, 6.0).level == "refuse"


# ---- CLI: estimate display and the approval check --------------------------------


def _jev(repo: Path, *args: str) -> tuple[int, str]:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, [*args, "--repo", str(repo)])
    # Rich wraps long lines to the terminal width (narrow in CI); compare unwrapped.
    return result.exit_code, " ".join(result.output.split())


def _cli_run_id(repo: Path) -> str:
    with Store.open(repo / ".jev" / "state.db") as store:
        return store.list_runs()[0].id


def _cli_run_state(repo: Path, run_id: str) -> RunState:
    with Store.open(repo / ".jev" / "state.db") as store:
        return store.get_run(run_id).state


def test_run_shows_estimate_before_approval(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 0, output
    assert "AWAITING_APPROVAL" in output
    assert "estimated total cost" in output
    assert "model pricing" in output  # no history on a fresh repo


def test_status_shows_estimate_before_approval(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    assert _jev(fixture_repo, "run", "add search")[0] == 0
    code, output = _jev(fixture_repo, "status", _cli_run_id(fixture_repo))
    assert code == 0, output
    assert "estimated total" in output


def _tiny_budget_run(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A run awaiting approval whose pricing-fallback estimate exceeds its budget."""
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    (fixture_repo / "jev.toml").write_text("[budget]\nper_run_usd = 0.5\nper_task_usd = 0.4\n")
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 0, output
    run_id = _cli_run_id(fixture_repo)
    assert _cli_run_state(fixture_repo, run_id) is RunState.AWAITING_APPROVAL
    return run_id


def test_approve_refuses_when_estimate_exceeds_budget(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _tiny_budget_run(fixture_repo, monkeypatch)
    code, output = _jev(fixture_repo, "approve", run_id)
    assert code == 1, output
    assert "not approving" in output and "--force" in output
    # The run is untouched: the refusal is advisory, the gate still stands.
    assert _cli_run_state(fixture_repo, run_id) is RunState.AWAITING_APPROVAL


def test_approve_force_overrides_the_estimate(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _tiny_budget_run(fixture_repo, monkeypatch)
    code, output = _jev(fixture_repo, "approve", "--force", run_id)
    assert code == 0, output
    assert _cli_run_state(fixture_repo, run_id) is not RunState.AWAITING_APPROVAL


def test_approve_warns_but_proceeds_below_refusal(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """High end between warn and refuse fractions: a warning, then approval."""
    import re

    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 0, output
    run_id = _cli_run_id(fixture_repo)
    match = re.search(r"estimated total cost: \$([0-9.]+)[^$]*\$([0-9.]+)", output)
    assert match is not None, output
    high = float(match.group(2))
    # Cap just above the high end: warn fires (0.8 * cap < high), refuse does not.
    cap = high * 1.1
    (fixture_repo / "jev.toml").write_text(
        f"[budget]\nper_run_usd = {cap:.4f}\nper_task_usd = {cap:.4f}\n"
    )
    code, output = _jev(fixture_repo, "approve", run_id)
    assert code == 0, output
    assert "warning" in output
    assert _cli_run_state(fixture_repo, run_id) is not RunState.AWAITING_APPROVAL


# ---- the shared approve-time budget gate --------------------------------------


def _gate_config(cap: float) -> tuple[BudgetConfig, CostSettings]:
    return BudgetConfig(per_run_usd=cap, per_task_usd=cap), CostSettings()


def test_approval_budget_block_ok(store: Store, tmp_path: Path) -> None:
    run = _awaiting_run(store, tmp_path, "T1")
    estimate, verdict, block = approval_budget_block(store, *_gate_config(1000.0), run)
    assert block is None
    assert estimate is not None
    assert verdict is not None and verdict.level == "ok" and verdict.message is None


def test_approval_budget_block_warn(store: Store, tmp_path: Path) -> None:
    run = _awaiting_run(store, tmp_path, "T1")
    estimate, _, _ = approval_budget_block(store, *_gate_config(1000.0), run)
    assert estimate is not None
    # Cap just above the high end: warn fires (0.8 * cap < high), refuse does not.
    cap = estimate.high_usd / 0.9
    _, verdict, block = approval_budget_block(store, *_gate_config(cap), run)
    assert block is None
    assert verdict is not None and verdict.level == "warn"
    assert verdict.message is not None


def test_approval_budget_block_refuse(store: Store, tmp_path: Path) -> None:
    run = _awaiting_run(store, tmp_path, "T1")
    _, verdict, block = approval_budget_block(store, *_gate_config(0.0001), run)
    assert verdict is not None and verdict.level == "refuse"
    assert block is not None and block.startswith("not approving:")


def test_approval_budget_block_force_overrides_refuse(store: Store, tmp_path: Path) -> None:
    run = _awaiting_run(store, tmp_path, "T1")
    _, verdict, block = approval_budget_block(store, *_gate_config(0.0001), run, force=True)
    assert block is None
    assert verdict is not None and verdict.level == "refuse"


def test_approval_budget_block_ignores_other_states(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.set_run_plan(run.id, _plan("T1"), 1)
    store.set_run_state(run.id, RunState.EXECUTING)
    run = store.get_run(run.id)
    assert approval_budget_block(store, *_gate_config(0.0001), run) == (None, None, None)


def test_approval_budget_block_defers_to_the_hard_cap(store: Store, tmp_path: Path) -> None:
    """When the cap is already hit, the hard check owns the outcome, not the estimate."""
    run = _awaiting_run(store, tmp_path, "T1")
    store.add_spend(run.id, None, 5.0)
    run = store.get_run(run.id)
    assert approval_budget_block(store, *_gate_config(5.0), run) == (None, None, None)
