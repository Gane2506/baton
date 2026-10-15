"""Free OpenRouter models: `"free"` expansion, ordering, dispatcher answers, the quota fallback
chain, ApiWorker classification, `jev models` and the doctor rows (docs/config.md
"Free models")."""

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from jev_agent.app import context
from jev_agent.cli import doctor_cmd, wiring
from jev_agent.cli.app import app
from jev_agent.config import JevConfig
from jev_agent.core.assign import Assignment, escalate, fit_free, quota_fallback, reassign
from jev_agent.core.models import Idea, Plan, RunState, TaskCard
from jev_agent.core.pipeline import build_pipeline
from jev_agent.core.store import Store
from jev_agent.dispatch.jev import (
    FREE_OPTION,
    FREE_UNKNOWN_OPTION,
    PREFER_FREE_INSTRUCTION,
    JevDispatcher,
)
from jev_agent.dispatch.requests import (
    DEFAULT_MODEL,
    RoleOptions,
    d1_intake,
    d2_validator,
    d5_after_attempt,
    role_options,
)
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.workers.api import ApiWorker, classify_api_error
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.openrouter_models import ModelCatalog, cache_path, parse_models
from jev_agent.workers.registry import WorkerRegistry

FIXTURES = Path(__file__).parent.parent / "fixtures" / "openrouter"
ALL = ["claude_code", "codex", "api"]
NEMOTRON = "nvidia/nemotron-3-super-120b-a12b:free"
LING = "inclusionai/ling-3.1-flash"
APODEX = "apodex/apodex-1.1-mini:free"
LFM = "liquid/lfm-2.5-2.6b:free"
MINI = "openai/gpt-5-mini"


def _models_body() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "models.json").read_text())
    return data


def _catalog() -> ModelCatalog:
    return ModelCatalog(parse_models(_models_body()), time.time())


def _config(models: dict[str, Any] | None = None, **roles: dict[str, Any]) -> JevConfig:
    return JevConfig.model_validate({"models": models or {}, "roles": roles})


# ---- config and role options ------------------------------------------------------------------


def test_defaults_are_off() -> None:
    cfg = JevConfig()
    assert cfg.models.prefer_free is False
    assert cfg.models.free_models_limit == 3
    assert cfg.roles.planner.prefer_free is None
    opts = role_options(cfg, "planner", ALL, _catalog())
    assert opts.models == [DEFAULT_MODEL] and opts.free == () and not opts.prefer_free
    assert opts.model_state() == {}


def test_free_token_expands_with_capability_filter_and_limit() -> None:
    cfg = _config(planner={"workers": ["claude_code", "api"], "models": [MINI, "free"]})
    opts = role_options(cfg, "planner", ALL, _catalog())
    # Structured role: laguna (no structured_outputs) and the router are left out; limit 3.
    assert opts.models == [MINI, NEMOTRON, LING, APODEX]
    assert opts.free == (NEMOTRON, LING, APODEX)
    assert not opts.prefer_free  # the token alone does not reorder

    wide = _config({"free_models_limit": 10}, planner={"workers": ["api"], "models": ["free"]})
    opts = role_options(wide, "planner", ALL, _catalog())
    assert opts.models[-1] == LFM and opts.free_unknown == (LFM,)  # unknown: kept and marked
    assert opts.model_state()["models"]["structured_output_unknown"] == [LFM]


def test_prefer_free_puts_free_first_and_adds_expansion() -> None:
    cfg = _config({"prefer_free": True}, reviewer={"models": [MINI]})
    opts = role_options(cfg, "reviewer", ALL, _catalog())
    assert opts.models == [NEMOTRON, LING, APODEX, MINI]
    assert opts.prefer_free
    state = opts.model_state()["models"]
    assert state == {"prefer_free": True, "free": [NEMOTRON, LING, APODEX], "free_worker": "api"}

    # A free model already in the pool is moved first; no expansion is added.
    listed = _config({"prefer_free": True}, reviewer={"models": [MINI, NEMOTRON]})
    assert role_options(listed, "reviewer", ALL, _catalog()).models == [NEMOTRON, MINI]


def test_role_override_and_coder_unaffected() -> None:
    cfg = _config(
        {"prefer_free": True},
        reviewer={"prefer_free": False, "models": [MINI]},
        coder={"models": ["free", "claude-sonnet"]},
    )
    assert role_options(cfg, "reviewer", ALL, _catalog()).models == [MINI]
    coder = role_options(cfg, "coder", ALL, _catalog())  # claude_code/codex: no api worker
    assert coder.models == ["claude-sonnet"] and coder.free == () and not coder.prefer_free


def test_no_catalog_means_only_free_slugs_by_name() -> None:
    cfg = _config({"prefer_free": True}, planner={"models": [MINI, "x/y:free", "free"]})
    opts = role_options(cfg, "planner", ALL, None)
    assert opts.models == ["x/y:free", MINI]
    assert opts.free == ("x/y:free",)


def test_requests_carry_model_state() -> None:
    cfg = _config({"prefer_free": True}, planner={"models": [MINI]})
    opts = role_options(cfg, "planner", ALL, _catalog())
    req = d1_intake(Idea(text="x", repo_path=Path(".")), opts)
    assert req.questions["model"][:3] == [NEMOTRON, LING, APODEX]
    assert req.state["models"]["prefer_free"] is True


# ---- rules dispatcher -------------------------------------------------------------------------


async def test_rules_pick_first_free_model_on_api() -> None:
    cfg = _config({"prefer_free": True}, planner={"models": [MINI]})
    opts = role_options(cfg, "planner", ALL, _catalog())
    assert opts.workers == ["claude_code", "api"]
    decision = await RulesDispatcher().decide(d1_intake(Idea(text="x", repo_path=Path(".")), opts))
    assert decision.answers["worker"] == "api"
    assert decision.answers["model"] == NEMOTRON


async def test_rules_without_prefer_never_pair_free_model_with_cli() -> None:
    cfg = _config(planner={"models": [NEMOTRON, MINI]})
    opts = role_options(cfg, "planner", ALL, _catalog())
    decision = await RulesDispatcher().decide(d1_intake(Idea(text="x", repo_path=Path(".")), opts))
    assert (decision.answers["worker"], decision.answers["model"]) == ("api", NEMOTRON)


async def test_rules_d2_vendor_rule_keeps_free_on_api() -> None:
    cfg = _config({"prefer_free": True}, validator={"models": [MINI]})
    opts = role_options(cfg, "validator", ALL, _catalog())
    plan = Plan(summary="s", tasks=[TaskCard(id="T1", title="t", description="d")])
    req = d2_validator(plan, 1, opts, "planner", "api", NEMOTRON)  # avoid vendor nvidia
    decision = await RulesDispatcher().decide(req)
    assert decision.answers["worker"] == "api"
    assert decision.answers["model"] == LING  # the next free model from another vendor


def _d5(**kwargs: Any) -> Any:
    base: dict[str, Any] = {
        "attempt": 1,
        "max_attempts": 3,
        "assignment": {"worker": "api", "model": NEMOTRON, "effort": "medium"},
        "worker_status": "quota_exhausted",
        "summary": "free-model rate limit",
        "tests": "skipped",
        "review": "skipped",
        "can_reassign": True,
        "task_spend_usd": 0.0,
        "task_budget_usd": 1.0,
    }
    base.update(kwargs)
    card = TaskCard(id="T1", title="t", description="d")
    return d5_after_attempt(card, **base)


async def test_rules_d5_quota_on_free_model_retries_with_fallback() -> None:
    rules = RulesDispatcher()
    req = _d5(free_model=True, quota_fallback_model=LING)
    assert req.state["quota_fallback_model"] == LING
    assert (await rules.decide(req)).answers["action"] == "retry"
    exhausted = _d5(free_model=True, quota_fallback_model=None)
    assert (await rules.decide(exhausted)).answers["action"] == "reassign"
    paid = _d5()  # not a free model: unchanged behaviour
    assert "free_model" not in paid.state
    assert (await rules.decide(paid)).answers["action"] == "reassign"


# ---- Jev payload ------------------------------------------------------------------------------


def test_jev_payload_marks_free_options_and_prefers_them() -> None:
    cfg = _config(
        {"prefer_free": True, "free_models_limit": 10},
        planner={"workers": ["api"], "models": [MINI]},
    )
    opts = role_options(cfg, "planner", ALL, _catalog())
    req = d1_intake(Idea(text="x", repo_path=Path(".")), opts)
    payload = JevDispatcher().build_payload(req.point, req.state, req.questions)
    model_q = payload["questions"]["model"]
    assert list(model_q["criteria"])[:4] == [NEMOTRON, LING, APODEX, LFM]
    assert model_q["criteria"][NEMOTRON] == FREE_OPTION == "free (rate-limited)"
    assert model_q["criteria"][LFM] == FREE_UNKNOWN_OPTION
    assert model_q["criteria"][MINI] is None
    assert model_q["instructions"].endswith(PREFER_FREE_INSTRUCTION)
    assert "unless the task is hard or earlier attempts failed" in model_q["instructions"]
    assert payload["state"]["models"]["free"][0] == NEMOTRON


def test_jev_payload_unchanged_without_free_models() -> None:
    opts = role_options(_config(planner={"models": [MINI]}), "planner", ALL, _catalog())
    req = d1_intake(Idea(text="x", repo_path=Path(".")), opts)
    payload = JevDispatcher().build_payload(req.point, req.state, req.questions)
    assert "models" not in payload["state"]
    assert PREFER_FREE_INSTRUCTION not in payload["questions"]["model"]["instructions"]


# ---- assignment changes -------------------------------------------------------------------------

OPTS = RoleOptions(
    "planner",
    ["api", "claude_code"],
    [NEMOTRON, LING, MINI, "anthropic/claude-sonnet-5"],
    ["low", "medium", "high"],
    free=(NEMOTRON, LING),
    prefer_free=True,
)


def test_quota_fallback_chain() -> None:
    a = Assignment("api", NEMOTRON, "medium")
    b = quota_fallback(a, OPTS)
    assert b == Assignment("api", LING, "medium")
    assert b is not None
    c = quota_fallback(b, OPTS)
    assert c == Assignment("api", MINI, "medium")  # first paid model
    assert c is not None
    assert quota_fallback(c, OPTS) is None  # paid: reassign instead
    # claude_code cannot run an OpenRouter slug and the pool has no CLI model: its default.
    assert reassign(c, OPTS) == Assignment("claude_code", None, "medium")


def test_escalate_from_free_goes_to_paid() -> None:
    assert escalate(Assignment("api", NEMOTRON, "low"), OPTS) == Assignment("api", MINI, "low")


def test_fit_free_keeps_free_models_on_api() -> None:
    assert fit_free(Assignment("claude_code", NEMOTRON, None), OPTS).worker == "api"
    no_api = RoleOptions(
        "planner", ["claude_code"], [NEMOTRON, MINI, "sonnet"], ["low"], free=(NEMOTRON,)
    )
    assert fit_free(Assignment("claude_code", NEMOTRON, None), no_api).model == "sonnet"
    only_free = RoleOptions("planner", ["claude_code"], [NEMOTRON], ["low"], free=(NEMOTRON,))
    assert fit_free(Assignment("claude_code", NEMOTRON, None), only_free).model is None
    # Reassigning away from api swaps the free model for one claude_code can run (here none in
    # the pool: its default) instead of bouncing back.
    assert reassign(Assignment("api", NEMOTRON, None), OPTS) == Assignment(
        "claude_code", None, None
    )


# ---- ApiWorker classification -------------------------------------------------------------------

KEY_ENV = "JEV_TEST_OPENROUTER_KEY"


def test_classify_free_tier_429() -> None:
    body = (FIXTURES / "error_429_free_limit.json").read_text()
    assert classify_api_error(429, body, free_model=True) == "quota_exhausted"
    assert classify_api_error(429, body) == "failure"  # a paid model: transient, retried once


async def test_api_worker_free_429_is_quota_exhausted_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, "sk-or-v1-fake-key-for-tests")
    calls: list[httpx.Request] = []
    body = (FIXTURES / "error_429_free_limit.json").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, content=body, headers={"X-RateLimit-Limit": "50"})

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    worker = ApiWorker(
        "https://openrouter.test/api/v1",
        KEY_ENV,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        free_models=[LING],
    )
    for model in (NEMOTRON, LING):
        job = WorkerJob(
            role="planner",
            prompt="p",
            workdir=tmp_path,
            log_path=tmp_path / f"{model.replace('/', '_')}.log",
            model=model,
            output_schema=Plan,
        )
        output = await worker.run(job)
        assert output.result.status == "quota_exhausted"
        assert output.result.summary.startswith("free-model rate limit: HTTP 429")
    assert len(calls) == 2 and sleeps == []  # no retry: the fallback is another model


# ---- harness fallback in a run ---------------------------------------------------------------


async def test_planner_falls_back_free_then_paid_then_other_worker(fixture_repo: Path) -> None:
    config = JevConfig.model_validate(
        {
            "approval": {"require_plan_approval": False},
            "models": {"prefer_free": True},
            "roles": {
                "planner": {"workers": ["api", "claude_code"], "models": ["free", MINI, "sonnet"]},
                "coder": {"workers": ["claude_code"]},
            },
        }
    )
    quota = FakeStep(status="quota_exhausted", summary="free-model rate limit: HTTP 429")
    worker = FakeWorker("fake", {"planner": [quota, quota, quota, quota, FakeStep()]})
    registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        pipeline = build_pipeline(fixture_repo, config, store, registry, catalog=_catalog())
        run = await pipeline.start(Idea(text="add a module", repo_path=fixture_repo))
        assert run.state is RunState.DONE, run.reason
        planner = [a for a in store.list_attempts(run.id) if a.role == "planner"]
        assert [(a.worker, a.model) for a in planner] == [
            ("api", NEMOTRON),
            ("api", LING),
            ("api", APODEX),
            ("api", MINI),
            ("claude_code", "sonnet"),  # the first model claude_code can run
        ]
        assert [a.attempt_no for a in planner] == [1, 2, 3, 4, 5]


async def test_planner_paid_quota_is_not_retried(fixture_repo: Path) -> None:
    config = JevConfig.model_validate(
        {"roles": {"planner": {"workers": ["api"], "models": [MINI]}}}
    )
    worker = FakeWorker("fake", {"planner": FakeStep(status="quota_exhausted")})
    registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        pipeline = build_pipeline(fixture_repo, config, store, registry, catalog=_catalog())
        run = await pipeline.start(Idea(text="add a module", repo_path=fixture_repo))
        assert run.state is RunState.NEEDS_HUMAN
        assert len([a for a in store.list_attempts(run.id) if a.role == "planner"]) == 1


# ---- CLI and doctor -----------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def mocked_models(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_models_body())

    monkeypatch.setattr(context, "catalog_transport", httpx.MockTransport(handler))
    return calls


def test_cli_models_list_free(fixture_repo: Path, mocked_models: list[httpx.Request]) -> None:
    result = runner.invoke(app, ["models", "list", "--free", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    out = "".join(result.output.split())
    assert "nemotron" in out and "lfm-2.5" in out
    assert "gpt-5-mini" not in out and "claude-sonnet" not in out
    assert "freemodels(6;" in out
    assert "`[models]prefer_free=true`" in out  # not eaten as Rich markup
    assert len(mocked_models) == 1
    assert cache_path(fixture_repo).is_file()
    # A second call uses the cache.
    again = runner.invoke(
        app, ["models", "free", "--supports", "json_schema", "--repo", str(fixture_repo)]
    )
    assert again.exit_code == 0, again.output
    assert len(mocked_models) == 1
    out = "".join(again.output.split())
    assert "laguna" not in out and "nemotron" in out and "lfm-2.5" not in out


def test_cli_models_refresh_and_failure(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        context, "catalog_transport", httpx.MockTransport(lambda r: httpx.Response(503))
    )
    result = runner.invoke(app, ["models", "list", "--refresh", "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert "no model list" in result.output


def test_doctor_free_model_rows(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/fake/bin/{cmd}")
    checks = [c for c in doctor_cmd.run_checks(fixture_repo) if c.name.startswith("free models")]
    assert [(c.status, c.detail.split(";")[0]) for c in checks] == [("ok", "prefer_free off")]
    assert "roles using free models: none" in checks[0].detail

    (fixture_repo / "jev.toml").write_text(
        "[models]\nprefer_free = true\n[roles.reviewer]\nprefer_free = false\n"
    )
    checks = [c for c in doctor_cmd.run_checks(fixture_repo) if c.name.startswith("free models")]
    main = checks[0]
    assert main.status == "warn"  # roles want free models but nothing is cached
    assert "role overrides: reviewer=off" in main.detail
    assert "roles using free models: planner, validator, code_reviewer, learner, designer" in (
        main.detail
    )
    notes = {c.name: c for c in checks[1:]}
    assert "coder" in notes["free models"].detail and notes["free models"].status == "note"
    assert "train" in notes["free models privacy"].detail

    _catalog().save(cache_path(fixture_repo))
    main = next(c for c in doctor_cmd.run_checks(fixture_repo) if c.name == "free models")
    assert main.status == "ok"
    assert "6 free model(s) cached, fetched 0.0h ago" in main.detail


def test_cli_run_loads_catalog_only_when_needed(
    fixture_repo: Path, mocked_models: list[httpx.Request]
) -> None:
    catalog, warnings = wiring.load_free_catalog(JevConfig(), fixture_repo)
    assert catalog is None and warnings == [] and mocked_models == []
    cfg = _config({"prefer_free": True})
    catalog, warnings = wiring.load_free_catalog(cfg, fixture_repo)
    assert catalog is not None and warnings == [] and len(mocked_models) == 1
    assert wiring.free_roles(cfg) == [
        "planner",
        "validator",
        "reviewer",
        "code_reviewer",
        "learner",
        "designer",
    ]
    offline, warnings = wiring.load_free_catalog(cfg, fixture_repo / "elsewhere", allow_fetch=False)
    assert offline is None and "no model list is cached" in warnings[0]


async def test_reviewer_falls_back_within_the_task_attempt(fixture_repo: Path) -> None:
    config = JevConfig.model_validate(
        {
            "approval": {"require_plan_approval": False},
            "review": {"enabled": True},
            "models": {"prefer_free": True},
            "roles": {
                "planner": {"workers": ["claude_code"]},
                "validator": {"workers": ["codex"]},
                "coder": {"workers": ["claude_code"]},
                "reviewer": {"workers": ["api"], "models": [MINI]},
            },
        }
    )
    quota = FakeStep(status="quota_exhausted", summary="free-model rate limit: HTTP 429")
    worker = FakeWorker("fake", {"reviewer": [quota, FakeStep()]})
    registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        pipeline = build_pipeline(fixture_repo, config, store, registry, catalog=_catalog())
        run = await pipeline.start(Idea(text="add a module", repo_path=fixture_repo))
        assert run.state is RunState.DONE, run.reason
        reviews = [a for a in store.list_attempts(run.id) if a.role == "reviewer"]
        assert [(a.model, a.attempt_no) for a in reviews] == [(NEMOTRON, 1), (LING, 1)]
        assert [a.log_path.name for a in reviews if a.log_path] == [
            "1.review.log",
            "1.review.2.log",
        ]
        coder = [j for j in worker.jobs if j.role == "coder"]
        assert len(coder) == 1 and coder[0].model is None  # the coder is unaffected
