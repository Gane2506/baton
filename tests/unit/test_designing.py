"""The DESIGNING pipeline stage: after validation, the designer drafts a design doc for
UI work (`Plan.needs_design`), approved together with the plan at the approval gate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jev_agent.core.design_system import get_design_system, render_for_prompt
from jev_agent.core.harness import Halt
from jev_agent.core.models import (
    AutonomyMode,
    DesignDoc,
    Idea,
    Plan,
    RunState,
    ScreenSpec,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
)
from jev_agent.core.pipeline import (
    APPROVAL_GATES,
    STEP_GATES,
    Pipeline,
    step_gate_target,
)
from jev_agent.core.store import RunRecord, Store, TaskRecord
from jev_agent.core.tasks import TaskRunner, _is_ui_task
from jev_agent.dispatch.requests import RoleOptions
from jev_agent.roles.render import builtin_template, load_template
from jev_agent.workers.base import WorkerOutput


def _design_doc() -> DesignDoc:
    return DesignDoc(
        title="Settings page redesign",
        summary="Two-column settings page",
        screens=[
            ScreenSpec(
                name="Settings",
                description="Two-column layout, card list left",
                components=["Card", "Button"],
            )
        ],
    )


def _plan(*, needs_design: bool) -> Plan:
    """A plan with (or without) the design flag."""
    return Plan(
        summary="test plan",
        tasks=[
            TaskCard(
                id="T1",
                title="Redesign the settings screen",
                description="rebuild the settings page",
                files_hint=["src/jev_agent/dashboard/templates/settings.html"],
                acceptance_criteria=["the page renders"],
            )
        ],
        needs_design=needs_design,
    )


class _FakeHarness:
    """Just enough harness for `_validate` and `_design`: no workers, no network."""

    def __init__(self, store: Store, repo: Path) -> None:
        self.store = store
        self.repo = repo
        self.config = SimpleNamespace(
            budget=SimpleNamespace(max_plan_rounds=3),
            roles=SimpleNamespace(validator=SimpleNamespace(prefer_different_vendor_than=None)),
            plan_approval_required=False,
        )
        self.decide_calls = 0
        self.captured_prompt: tuple[str, dict[str, Any]] | None = None

    def require_options(self, role: str) -> RoleOptions:
        return RoleOptions(role=role, workers=["fake"], models=["m"], efforts=["e"])  # type: ignore[arg-type]

    def check_budget(self, run_id: str) -> None:
        pass

    def check_running(self, run_id: str) -> None:
        pass

    def check_prompts(self) -> None:
        pass

    def has_tests(self) -> bool:
        return False

    def lessons_for(self, run_id: str, role: str, *args: Any) -> list[Any]:
        return []

    def last_answers(self, run_id: str, point: str) -> dict[str, str]:
        return {}

    def last_attempt(self, run_id: str, role: str) -> Any:
        return None

    def assignment(self, role: str, answers: dict[str, str]) -> Any:
        return SimpleNamespace(role=role, answers=answers)

    def prompt(self, role: str, lessons: Any = (), **variables: Any) -> str:
        self.captured_prompt = (role, variables)
        return f"{role} prompt"

    async def decide(self, run_id: str, task_id: Any, req: Any) -> Any:
        self.decide_calls += 1
        if self.decide_calls == 1:  # D2: the validator's worker assignment
            return SimpleNamespace(answers={"worker": "fake", "model": "m", "effort": "e"})
        return SimpleNamespace(answers={"verdict": "approve"})  # D3


class _ScriptedPipeline(Pipeline):
    """A pipeline whose run-level worker returns a scripted parsed output per role."""

    def __init__(self, harness: _FakeHarness, parsed: Any | dict[str, Any]) -> None:
        super().__init__(harness)  # type: ignore[arg-type]
        self.by_role = parsed if isinstance(parsed, dict) else {}
        self.default_parsed = None if isinstance(parsed, dict) else parsed
        self.captured: tuple[str, Any, str] | None = None

    async def _run_level_worker(
        self,
        run: RunRecord,
        role: str,
        assignment: Any,
        prompt: str,
        lesson_ids: list[str],
    ) -> WorkerOutput:
        self.captured = (role, assignment, prompt)
        return WorkerOutput(
            result=WorkerResult(status="success", summary="ok", raw_log_path=Path("log")),
            parsed=self.by_role.get(role, self.default_parsed),
        )


def _validating_run(
    store: Store, repo: Path, *, needs_design: bool, autonomy_mode: str = "standard"
) -> RunRecord:
    run = store.create_run(
        Idea(text="redesign the settings page", repo_path=repo),
        None,
        "none",
        autonomy_mode=autonomy_mode,
        autonomous_opt_in=autonomy_mode == "autonomous",
    )
    store.set_run_plan(run.id, _plan(needs_design=needs_design), 1)
    store.set_run_state(run.id, RunState.VALIDATING)
    return store.get_run(run.id)


async def _validate_to(pipeline: Pipeline, run: RunRecord) -> RunState:
    await pipeline._validate(run)
    return pipeline.store.get_run(run.id).state


# ---- gate wiring ---------------------------------------------------------------------------


def test_step_gates_design() -> None:
    assert STEP_GATES[RunState.DESIGNING] is RunState.AWAITING_DESIGNING
    assert RunState.AWAITING_DESIGNING in APPROVAL_GATES


def test_step_gate_target_design() -> None:
    assert step_gate_target(AutonomyMode.ASK_EVERY_STEP, RunState.DESIGNING) is (
        RunState.AWAITING_DESIGNING
    )
    for mode in (
        AutonomyMode.STANDARD,
        AutonomyMode.AUTONOMOUS,
        AutonomyMode.MANUAL,
        AutonomyMode.MEDIUM,
    ):
        assert step_gate_target(mode, RunState.DESIGNING) is RunState.DESIGNING


async def test_approve_passes_awaiting_designing(fixture_repo: Path) -> None:
    """`approve` inverts STEP_GATES, so AWAITING_DESIGNING -> DESIGNING needs no special case."""
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        pipeline = Pipeline(harness)  # type: ignore[arg-type]
        run = store.create_run(Idea(text="x", repo_path=fixture_repo), None, "none")
        assert store.set_run_state(run.id, RunState.AWAITING_DESIGNING)
        record = await pipeline.approve(run.id, execute=False)
        assert record.state is RunState.DESIGNING


# ---- _validate branches to DESIGNING -------------------------------------------------------


async def test_validate_moves_to_designing_when_needs_design(fixture_repo: Path) -> None:
    """STANDARD, no approval required: VALIDATING -> DESIGNING (not EXECUTING)."""
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        pipeline = _ScriptedPipeline(
            harness, ValidationResult(verdict="approve", issues=[], challenges=[])
        )
        run = _validating_run(store, fixture_repo, needs_design=True)
        assert await _validate_to(pipeline, run) is RunState.DESIGNING


async def test_validate_skips_designing_when_no_design_needed(fixture_repo: Path) -> None:
    """Byte-identical existing behavior when the flag is false: straight to EXECUTING."""
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        pipeline = _ScriptedPipeline(
            harness, ValidationResult(verdict="approve", issues=[], challenges=[])
        )
        run = _validating_run(store, fixture_repo, needs_design=False)
        assert await _validate_to(pipeline, run) is RunState.EXECUTING


async def test_validate_design_gate_in_ask_every_step(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        pipeline = _ScriptedPipeline(
            harness, ValidationResult(verdict="approve", issues=[], challenges=[])
        )
        run = _validating_run(
            store, fixture_repo, needs_design=True, autonomy_mode="ask_every_step"
        )
        assert await _validate_to(pipeline, run) is RunState.AWAITING_DESIGNING


async def test_validate_design_in_autonomous_skips_all_gates(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        pipeline = _ScriptedPipeline(
            harness, ValidationResult(verdict="approve", issues=[], challenges=[])
        )
        run = _validating_run(store, fixture_repo, needs_design=True, autonomy_mode="autonomous")
        assert await _validate_to(pipeline, run) is RunState.DESIGNING


async def test_design_approved_with_plan_at_approval_gate(fixture_repo: Path) -> None:
    """STANDARD + plan_only/require_plan_approval: the design is produced first, then plan
    and design are approved together at AWAITING_APPROVAL (the gate `_validate` used)."""
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        harness.config.plan_approval_required = True
        pipeline = _ScriptedPipeline(
            harness,
            {
                "validator": ValidationResult(verdict="approve", issues=[], challenges=[]),
                "designer": _design_doc(),
            },
        )
        run = _validating_run(store, fixture_repo, needs_design=True)
        assert await _validate_to(pipeline, run) is RunState.DESIGNING
        await pipeline._design(store.get_run(run.id))
        assert store.get_run(run.id).state is RunState.AWAITING_APPROVAL


# ---- _design --------------------------------------------------------------------------------


async def test_design_stores_doc_and_moves_to_execution_gate(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        pipeline = _ScriptedPipeline(harness, _design_doc())
        run = _validating_run(store, fixture_repo, needs_design=True)
        store.set_run_state(run.id, RunState.DESIGNING)
        run = store.get_run(run.id)

        await pipeline._design(run)

        assert pipeline.captured is not None
        role, _assignment, _prompt = pipeline.captured
        assert role == "designer"
        stored = store.get_run(run.id)
        assert stored.design is not None
        assert stored.design.title == "Settings page redesign"
        assert stored.design.screens[0].name == "Settings"
        assert stored.design.screens[0].components == ["Card", "Button"]
        # STANDARD, no approval required: design approved with the plan -> EXECUTING.
        assert stored.state is RunState.EXECUTING
        # The prompt carried the idea, the plan JSON and the design-system text.
        assert harness.captured_prompt is not None
        prompt_role, variables = harness.captured_prompt
        assert prompt_role == "designer"
        assert "redesign the settings page" in variables["idea"]
        assert "T1" in variables["plan_json"]
        assert variables["design_system"] == render_for_prompt(get_design_system(fixture_repo))


async def test_design_moves_to_approval_gate_when_required(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        harness.config.plan_approval_required = True
        pipeline = _ScriptedPipeline(harness, _design_doc())
        run = _validating_run(store, fixture_repo, needs_design=True)
        store.set_run_state(run.id, RunState.DESIGNING)
        await pipeline._design(store.get_run(run.id))
        assert store.get_run(run.id).state is RunState.AWAITING_APPROVAL


async def test_design_without_design_doc_stops_for_human(fixture_repo: Path) -> None:
    """A designer that gives no design stops the run in NEEDS_HUMAN, like _validate."""
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        harness = _FakeHarness(store, fixture_repo)
        pipeline = _ScriptedPipeline(harness, None)
        run = _validating_run(store, fixture_repo, needs_design=True)
        store.set_run_state(run.id, RunState.DESIGNING)
        with pytest.raises(Halt) as excinfo:
            await pipeline._design(store.get_run(run.id))
        assert excinfo.value.state is RunState.NEEDS_HUMAN
        assert "no design" in excinfo.value.reason


# ---- store: set_run_design round-trip -------------------------------------------------------


def test_set_run_design_round_trip(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = store.create_run(Idea(text="x", repo_path=fixture_repo), None, "none")
        assert store.get_run(run.id).design is None
        store.set_run_design(run.id, _design_doc())
        stored = store.get_run(run.id)
        assert stored.design is not None
        assert stored.design.model_dump() == _design_doc().model_dump()


def test_migration_16_applies_on_v15_db(tmp_path: Path) -> None:
    """A database written at schema v15 gains `design_json` from migration 16; old runs
    read back with `design` None, and the new column round-trips."""
    from jev_agent.core.migrations import MIGRATIONS, apply_migrations, schema_version

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.isolation_level = None  # autocommit, as apply_migrations requires
    try:
        apply_migrations(conn, MIGRATIONS[:15])
        assert schema_version(conn) == 15
        columns = [row[1] for row in conn.execute("PRAGMA table_info(runs)")]
        assert "design_json" not in columns  # migration 1 has no such column
        idea_json = Idea(text="old idea", repo_path=tmp_path).model_dump_json()
        conn.execute(
            "INSERT INTO runs (id, idea_json, state, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                "old-run",
                idea_json,
                "AWAITING_APPROVAL",
                "2026-10-01T00:00:00",
                "2026-10-01T00:00:00",
            ),
        )
        apply_migrations(conn)  # to latest: runs migration 16
        assert schema_version(conn) == len(MIGRATIONS)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(runs)")]
        assert "design_json" in columns
    finally:
        conn.close()
    with Store.open(db) as store:
        old = store.get_run("old-run")
        assert old.state is RunState.AWAITING_APPROVAL
        assert old.design is None
        store.set_run_design("old-run", _design_doc())
        assert store.get_run("old-run").design is not None
        assert store.get_run("old-run").design.title == "Settings page redesign"


# ---- coder prompt design injection ------------------------------------------------------------


def _task_record(run_id: str, card: TaskCard) -> TaskRecord:
    return TaskRecord(
        run_id=run_id,
        id=card.id,
        card=card,
        state=TaskState.READY,
        attempts=0,
        spend_usd=0.0,
    )


def _ui_card() -> TaskCard:
    return TaskCard(
        id="T1",
        title="Redesign the settings screen",
        description="rebuild the settings page",
        files_hint=["src/jev_agent/dashboard/templates/settings.html"],
        acceptance_criteria=["the page renders"],
    )


def _backend_card() -> TaskCard:
    return TaskCard(
        id="T2",
        title="Add retry logic to the dispatcher",
        description="retry failed dispatches",
        files_hint=["src/jev_agent/dispatch/rules.py"],
        acceptance_criteria=["retries happen"],
    )


def _coder_prompt_text(store: Store, repo: Path, card: TaskCard, *, with_design: bool) -> str:
    run = store.create_run(Idea(text="x", repo_path=repo), None, "none")
    store.set_run_plan(run.id, Plan(summary="s", tasks=[card]), 1)
    if with_design:
        store.set_run_design(run.id, _design_doc())
    harness = _FakeHarness(store, repo)
    runner = TaskRunner(harness)  # type: ignore[arg-type]
    prompt, _lesson_ids = runner._coder_prompt(run.id, _task_record(run.id, card), 1)
    return prompt


def test_coder_prompt_includes_design_for_ui_task(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        prompt = _coder_prompt_text(store, fixture_repo, _ui_card(), with_design=True)
        assert prompt.startswith("## UI design (v1): Settings page redesign")
        assert "### Screen: Settings" in prompt
        assert "coder prompt" in prompt  # the base prompt still follows


def test_coder_prompt_unchanged_for_non_ui_task(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        prompt = _coder_prompt_text(store, fixture_repo, _backend_card(), with_design=True)
        assert prompt == "coder prompt"


def test_coder_prompt_unchanged_without_design(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        prompt = _coder_prompt_text(store, fixture_repo, _ui_card(), with_design=False)
        assert prompt == "coder prompt"


@pytest.mark.parametrize(
    ("title", "files_hint", "expected"),
    [
        ("Redesign the settings screen", [], True),  # title match
        ("Fix a backend bug", ["src/ui/theme.css"], True),  # files_hint match
        ("Add a frontend route", ["src/app.py"], True),  # frontend
        ("Update component styles", [], True),  # component/style
        ("Add retry logic", ["src/jev_agent/dispatch/rules.py"], False),
    ],
)
def test_is_ui_task_heuristic(title: str, files_hint: list[str], expected: bool) -> None:
    card = TaskCard(
        id="T1",
        title=title,
        description="d",
        files_hint=files_hint,
        acceptance_criteria=["a"],
    )
    assert _is_ui_task(card) is expected


# ---- prompts ----------------------------------------------------------------------------------


def test_planner_prompt_sets_needs_design() -> None:
    load_template("planner")  # still valid Jinja
    text = builtin_template("planner")
    assert "needs_design" in text
    assert '"needs_design": false' in text  # the output shape documents the flag


def test_validator_prompt_challenges_needs_design() -> None:
    load_template("validator")  # still valid Jinja
    text = builtin_template("validator")
    assert "needs_design" in text


# ---- board / badge coverage for the new states --------------------------------------------------


def test_board_covers_designing_states() -> None:
    from jev_agent.app.board import COLUMN_FOR_STATE, RUN_STATE_PHRASE

    assert set(COLUMN_FOR_STATE) == set(RunState)
    assert set(RUN_STATE_PHRASE) == set(RunState)
    assert COLUMN_FOR_STATE[RunState.DESIGNING] == "Planning"
    assert COLUMN_FOR_STATE[RunState.AWAITING_DESIGNING] == "Awaiting approval"
