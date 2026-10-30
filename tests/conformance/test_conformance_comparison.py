"""Phase-3 conformance comparison (issue #368, design sections 8.2, 15, 17).

The same scenarios run through the legacy path AND through the `default`
preset path under the stub/replay harness — stubbed Jev decisions and worker
outputs from recorded fixtures (a scripted dispatcher plus FakeWorker), a
fixed clock, and a stubbed ship operation (the external push/PR side
effect, not pipeline logic) — comparing stage sequences, gate parkings,
outcomes, and label output. Differences are allowed only when named in the
deliberate-difference registry; for `default` the registry must be empty,
so any difference fails.

Phase 3 has no preset files (they land in phase 4), so the `default`
preset path is resolved from the design text itself: the profile saved
for the profile-path runs is the section 9.1 default parameter set — the
full canonical stage list, `designing` `when_needed`, `inherit` gate
policies — with every other setting left to resolve from the repo
configuration, exactly as `build_default_snapshot` resolves it. The two
paths therefore differ only in execution path, not in parameters.

Label comparison note: the phase-0 baselines record the label core
("Reduced checks — …" without the trailing review sentence), while
`assurance_label_text` renders the full text everywhere. A baseline label
is therefore accepted when it equals the rendered label or is its prefix;
the legacy-vs-profile comparison itself is exact.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from scenarios import SCENARIOS

from jev_agent.app.context import pipeline_settings
from jev_agent.config import JevConfig
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.models import (
    AutonomyMode,
    Decision,
    DecisionRequest,
    DesignDoc,
    Idea,
    Plan,
    RunState,
    TaskCard,
    ValidationResult,
)
from jev_agent.core.pipeline import APPROVAL_GATES, build_pipeline, cancel_run
from jev_agent.core.store import Store
from jev_agent.core.workflow_profiles import ProfileStore
from jev_agent.core.workflow_profiles.assurance import assurance_label_text
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.roles.schemas import QualityReview, ReviewFinding
from jev_agent.workers.base import WorkerJob, WorkerOutput
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry

BASELINE_DIR = Path(__file__).parent / "baseline"
REGISTRY_PATH = Path(__file__).parent / "deliberate_differences.json"
FIXED_CLOCK = "2026-10-30T12:00:00+00:00"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
CANONICAL = [
    "INTAKE",
    "PLANNING",
    "VALIDATING",
    "DESIGNING",
    "EXECUTING",
    "INTEGRATING",
    "SHIPPING",
]
GATES = {stage: "inherit" for stage in CANONICAL if stage != "INTAKE"}


def _card(tid: str = "T1") -> TaskCard:
    return TaskCard(
        id=tid,
        title=f"task {tid}",
        description=f"do {tid}",
        files_hint=[f"src/{tid.lower()}.py"],
        depends_on=[],
        acceptance_criteria=["a"],
    )


ONE_TASK = Plan(summary="one task", tasks=[_card()])
DESIGN_TASK = Plan(summary="ui task", tasks=[_card()], needs_design=True)


def _clean_review() -> FakeStep:
    return FakeStep(parsed=QualityReview(summary="review", findings=[]))


def _review(
    *findings: tuple[
        Literal["info", "minor", "major", "critical"],
        Literal["bug", "security", "performance", "maintainability", "tests", "docs"],
        str,
    ],
) -> FakeStep:
    return FakeStep(
        parsed=QualityReview(
            summary="review",
            findings=[
                ReviewFinding(severity=s, category=c, title=t, file="src/t1.py", line_start=1)
                for s, c, t in findings
            ],
        )
    )


class _ReviseOnce(RulesDispatcher):
    """D3 revises once, then approves."""

    name = "revise-once"

    def __init__(self) -> None:
        self.d3_calls = 0

    async def decide(self, req: DecisionRequest) -> Decision:
        decision = await super().decide(req)
        if req.point == "D3":
            self.d3_calls += 1
            if self.d3_calls == 1:
                decision.answers["verdict"] = "revise"
        return decision


class _CancelsOnCoder:
    """A worker wrapper that cancels the run from inside the first coder
    job, mirroring the existing cancel-while-a-worker-runs test: the
    pipeline must keep CANCELLED instead of overwriting it."""

    name = "fake"

    def __init__(self, store: Store, inner: FakeWorker) -> None:
        self.store, self.inner = store, inner

    async def run(self, job: WorkerJob) -> WorkerOutput:
        if job.role == "coder":
            cancel_run(self.store, self.store.list_runs()[0].id)
        return await self.inner.run(job)


async def _fake_ship(*args: Any, **kwargs: Any) -> Any:
    """The ship stub: the external push/PR side effect is stubbed, the
    pipeline's SHIPPING stage, approval mapping, and DONE move are real."""
    return SimpleNamespace(
        url="https://example.test/pr/1", repo="owner/repo", branch="jev/x/result", number=1
    )


def _make_repo(base: Path, name: str) -> Path:
    repo = base / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / ".gitignore").write_text(".jev/\n")
    (repo / "README.md").write_text("fixture\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _config(scenario_id: str, **extra: Any) -> JevConfig:
    data: dict[str, Any] = {
        "approval": {"require_plan_approval": True},
        "review": {"enabled": False},
        "quality": {"level": "medium"},
        "roles": {
            "coder": {"workers": ["claude_code", "codex", "api"], "models": ["coder-model"]},
        },
        "ship": {"enabled": False},
    }
    if scenario_id == "ship-merge":
        data["ship"] = {"enabled": True, "after_approval": "merge"}
    elif scenario_id == "ship-ask":
        data["ship"] = {"enabled": True, "after_approval": "ask"}
    elif scenario_id == "fix-task-loop":
        data["integration"] = {"test_command": "pytest -q"}
    elif scenario_id == "halt-resume":
        data["roles"]["coder"] = {
            "workers": ["claude_code", "codex", "api"],
            "models": ["coder-model"],
            "prompt_file": "coder.md",
        }
    for section, values in extra.items():
        data.setdefault(section, {}).update(values)
    return JevConfig.model_validate(data)


def _script(scenario_id: str) -> dict[str, Any]:
    plan = DESIGN_TASK if scenario_id == "designing-on" else ONE_TASK
    script: dict[str, Any] = {
        "planner": FakeStep(parsed=plan),
        "validator": FakeStep(parsed=ValidationResult(verdict="approve", issues=[])),
        "code_reviewer:T1": _clean_review(),
        "code_reviewer:None": _clean_review(),
    }
    if scenario_id == "designing-on":
        script["designer"] = FakeStep(parsed=DesignDoc(title="Design", summary="ui"))
    if scenario_id == "score-rework-loop":
        # Two major bugs drop the integration score to 84 (below 85)
        # without blocking the gate; the clean second round scores 96.
        script["code_reviewer:None"] = [
            _review(("major", "bug", "f1"), ("major", "bug", "f2")),
            _clean_review(),
        ]
    return script


def _dispatcher(scenario_id: str) -> Any:
    if scenario_id == "plan-revise-loop":
        return _ReviseOnce()
    return None


def _make_env(repo: Path, scenario_id: str, path: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    config = _config(scenario_id)
    store = Store.open(repo / ".jev" / "state.db", clock=lambda: FIXED_CLOCK)
    worker = FakeWorker("fake", _script(scenario_id))
    effective: Any = worker
    if scenario_id == "cancel-mid-run":
        effective = _CancelsOnCoder(store, worker)
    registry = WorkerRegistry({n: effective for n in ("claude_code", "codex", "api")})
    # Consumption order for the fix-task loop: the task-scope gate runs the
    # integration command in the task worktree, then integration itself,
    # then the fix task's gate, then integration again — so the failing
    # code must sit second, not first.
    exit_codes = {"pytest -q": [0, 1, 0]} if scenario_id == "fix-task-loop" else None
    commands = FakeCommandRunner(exit_codes)
    pipeline = build_pipeline(
        repo,
        config,
        store,
        registry,
        dispatcher=_dispatcher(scenario_id),
        commands=commands,
        settings=pipeline_settings(config),
    )
    if scenario_id in ("ship-merge", "ship-ask"):
        monkeypatch.setattr("jev_agent.core.pipeline.ship_and_merge", _fake_ship)
    if scenario_id == "halt-resume":
        (repo / "coder.md").write_text("Do {{ task.title }} in {{ nope }}\n")
    if path == "profile":
        ProfileStore(repo, repo / "user-home").save(
            {
                "id": "repo:default",
                "display_name": "Default",
                "scope": "repo",
                "stage_list": list(CANONICAL),
                "gate_policies": dict(GATES),
                "designing": "when_needed",
                "ship_enabled": scenario_id in ("ship-merge", "ship-ask"),
            }
        )
    return SimpleNamespace(
        repo=repo, store=store, pipeline=pipeline, worker=worker, config=config, commands=commands
    )


def _result(env: Any, run: Any, parkings: list[str], scenario_id: str) -> dict[str, Any]:
    rows = env.store.list_stage_rows(run.id)
    sequence = [row.stage for row in rows if row.outcome != "skipped"]
    if scenario_id == "manual" and not sequence:
        # No handler ran: the run waits in INTAKE, which is its sequence.
        sequence = ["INTAKE"]
    reread = env.store.get_run(run.id)
    outcome = "waiting" if scenario_id == "manual" else reread.state.name
    return {
        "stage_sequence": sequence,
        "gate_parkings": parkings,
        "outcome": outcome,
        "label_output": assurance_label_text(reread.assurance, reread.assurance_absent),
    }


async def _drive(env: Any, scenario_id: str, path: str) -> dict[str, Any]:
    idea = Idea(text="add a module", repo_path=env.repo)
    kwargs: dict[str, Any] = {}
    if path == "profile":
        kwargs["workflow_profile"] = "repo:default@v1"
    if scenario_id == "autonomous":
        kwargs["autonomy_mode"] = AutonomyMode.AUTONOMOUS
        kwargs["autonomous_opt_in"] = True
    elif scenario_id == "ask-every-step":
        kwargs["autonomy_mode"] = AutonomyMode.ASK_EVERY_STEP
    elif scenario_id == "medium":
        kwargs["autonomy_mode"] = AutonomyMode.MEDIUM
        kwargs["medium_folder"] = "src"
    elif scenario_id == "manual":
        kwargs["autonomy_mode"] = AutonomyMode.MANUAL
    run = await env.pipeline.start(idea, **kwargs)
    parkings: list[str] = []
    if scenario_id == "manual":
        return _result(env, run, parkings, scenario_id)
    if scenario_id == "halt-resume":
        assert run.state is RunState.AWAITING_APPROVAL, run.reason
        parkings.append("AWAITING_APPROVAL")
        run = await env.pipeline.approve(run.id)
        assert run.state is RunState.NEEDS_HUMAN, run.reason
        assert run.resume_state is RunState.EXECUTING
        (env.repo / "coder.md").write_text("Do {{ task.title }}\n")
        run = await env.pipeline.resume(run.id)
        return _result(env, run, parkings, scenario_id)
    gates = set(APPROVAL_GATES) | {RunState.AWAITING_SHIP}
    while run.state in gates:
        parkings.append(run.state.name)
        run = await env.pipeline.approve(run.id)
    return _result(env, run, parkings, scenario_id)


def _label_matches_baseline(label: str, baseline_label: str) -> bool:
    return label == baseline_label or label.startswith(baseline_label)


def test_deliberate_difference_registry_empty_for_default() -> None:
    """Section 8.2: for the `default` preset the registry must be empty —
    the comparison below therefore requires exact agreement."""
    registry = json.loads(REGISTRY_PATH.read_text())
    assert registry == {"default": []}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
async def test_conformance_legacy_matches_profile_and_baseline(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every section-17 scenario, legacy path vs. `default` preset path vs.
    the phase-0 baseline: identical stage sequences, gate parkings,
    outcomes, and label output, with no deliberate difference allowed."""
    scenario_id = scenario["id"]
    baseline = json.loads((BASELINE_DIR / f"{scenario_id}.json").read_text())
    registry = json.loads(REGISTRY_PATH.read_text())
    allowed = {entry["field"] for entry in registry["default"]}
    results: dict[str, dict[str, Any]] = {}
    for path in ("legacy", "profile"):
        repo = _make_repo(tmp_path, f"{scenario_id}-{path}")
        env = _make_env(repo, scenario_id, path, monkeypatch)
        try:
            results[path] = await _drive(env, scenario_id, path)
        finally:
            env.store.close()
    for field in ("stage_sequence", "gate_parkings", "outcome", "label_output"):
        if results["legacy"][field] != results["profile"][field] and field not in allowed:
            raise AssertionError(
                f"{scenario_id}: legacy vs. profile differ on {field}: "
                f"{results['legacy'][field]!r} != {results['profile'][field]!r} "
                "(not in the deliberate-difference registry, which must stay empty "
                "for `default`)"
            )
    for field in ("stage_sequence", "gate_parkings", "outcome"):
        assert results["legacy"][field] == baseline[field], (
            f"{scenario_id}: legacy {field} {results['legacy'][field]!r} != "
            f"baseline {baseline[field]!r}"
        )
    assert _label_matches_baseline(results["legacy"]["label_output"], baseline["label_output"]), (
        f"{scenario_id}: legacy label {results['legacy']['label_output']!r} != "
        f"baseline {baseline['label_output']!r}"
    )
    assert _label_matches_baseline(results["profile"]["label_output"], baseline["label_output"])
