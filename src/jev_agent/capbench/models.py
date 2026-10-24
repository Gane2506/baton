"""Capability benchmark models: task specs, grade reports, results, and the rubric.

The scoring rubric (design §3): per task, four dimensions normalized to 0-100 -
correctness (hidden-test fraction, 50%), ship score (quality gate 0-100 at the
`basic` level, 25%), cost (15%) and time (10%). The suite score is the mean of
the task composites. Phase 1 runs each task once and says so on the scoreboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

# --- rubric constants (design §3) ---------------------------------------------------
W_CORRECTNESS = 0.50
W_SHIP = 0.25
W_COST = 0.15
W_TIME = 0.10

COST_FULL_USD = 0.50  # 100 points at or below this spend
COST_ZERO_USD = 2.0  # 0 points at or above (the per-task cap)
TIME_FULL_MIN = 5.0  # 100 points at or below
TIME_ZERO_MIN = 30.0  # 0 points at or above

DEFAULT_PER_RUN_USD = 2.0  # the `[budget] per_run_usd` cap each benchmark run gets
DEFAULT_QUALITY_LEVEL = "basic"  # fewer checks, fewer model calls (design §2)
DEFAULT_PROFILE = "default"  # worker-profile label recorded with each result

SCHEMA_VERSION = 1


def _tasks_dir() -> Path:
    """`benchmarks/tasks/` at the repo root (this file is `src/jev_agent/capbench/`)."""
    return Path(__file__).resolve().parents[3] / "benchmarks" / "tasks"


@dataclass(frozen=True)
class TaskSpec:
    """One benchmark task: its directory, brief, and grading bands."""

    id: str
    title: str
    dir: Path
    brief: str
    max_added_lines: int = 150
    max_files: int = 6

    @property
    def fixture_dir(self) -> Path:
        return self.dir / "fixture"

    @property
    def grade_py(self) -> Path:
        return self.dir / "grade.py"


def load_task(task_id: str, tasks_dir: Path | None = None) -> TaskSpec:
    """Load one task spec; raises FileNotFoundError / ValueError for a bad task dir.

    The task dir must stay inside `tasks_dir`: without this, a `..` task id (or
    an absolute one, which would replace the root entirely) landing on a dir
    with brief.md/fixture/grade.py would load - and later execute - that
    grade.py. Resolving both sides also catches a symlink inside the tasks
    dir pointing out of it.
    """
    root = tasks_dir or _tasks_dir()
    root_resolved = root.resolve()
    task_dir = (root_resolved / task_id).resolve()
    if not task_dir.is_relative_to(root_resolved):
        raise ValueError(f"benchmark task {task_id!r}: escapes the tasks directory")
    brief_path = task_dir / "brief.md"
    if not brief_path.is_file():
        raise FileNotFoundError(f"benchmark task {task_id!r}: no brief.md in {task_dir}")
    brief = brief_path.read_text(encoding="utf-8").strip()
    if not brief:
        raise ValueError(f"benchmark task {task_id!r}: brief.md is empty")
    if not (task_dir / "fixture").is_dir():
        raise FileNotFoundError(f"benchmark task {task_id!r}: no fixture/ in {task_dir}")
    if not (task_dir / "grade.py").is_file():
        raise FileNotFoundError(f"benchmark task {task_id!r}: no grade.py in {task_dir}")
    title = brief.splitlines()[0].lstrip("# ").strip() or task_id
    return TaskSpec(id=task_id, title=title, dir=task_dir, brief=brief)


def load_tasks(tasks_dir: Path | None = None) -> list[TaskSpec]:
    """Every task spec under `benchmarks/tasks/`, sorted by id."""
    root = tasks_dir or _tasks_dir()
    if not root.is_dir():
        return []
    return [load_task(d.name, root) for d in sorted(root.iterdir()) if d.is_dir()]


class GradeReport(BaseModel):
    """What a task's `grade.py` reported (exit 0/1 plus this JSON on stdout)."""

    task_id: str = Field(description="the benchmark task graded")
    passed: bool = Field(description="the grader's verdict")
    tests_passed: int = Field(ge=0, description="hidden tests passed")
    tests_total: int = Field(ge=0, description="hidden tests run")
    diff_added: int = Field(ge=0, description="added lines in the result diff")
    diff_files: int = Field(ge=0, description="files changed in the result diff")
    diff_within_band: bool = Field(description="diff inside the task's size band")
    ship_score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="quality gate 0-100 ship score, None when no gate ran",
    )
    reasons: list[str] = Field(default_factory=list, description="human-readable failures")
    details: dict[str, str] = Field(
        default_factory=dict, description="per-check notes from the grader"
    )


class TaskResult(BaseModel):
    """One benchmark task run, with its four rubric dimensions and the composite."""

    task_id: str
    title: str
    run_id: str | None = Field(default=None, description="the jev run, None when never started")
    passed: bool = Field(description="the grader's verdict")
    correctness: float = Field(
        ge=0, le=100, description="hidden-test fraction x 100", allow_inf_nan=False
    )
    ship_score: int | None = Field(
        default=None, ge=0, le=100, description="gate ship score, None if no gate ran"
    )
    cost_usd: float = Field(
        ge=0, description="actual run spend from the run record", allow_inf_nan=False
    )
    duration_min: float = Field(
        ge=0, description="wall-clock minutes for the run", allow_inf_nan=False
    )
    profile: str = Field(description="worker-profile label the task ran under")
    run_date: str = Field(description="ISO date the task ran")
    repeats: int = Field(default=1, description="repeats per task; Phase 1 is always 1")
    grade: GradeReport | None = Field(default=None, description="the raw grader report")
    note: str = Field(default="", description="why the run did not complete, if it did not")

    @property
    def composite(self) -> float:
        """The 0-100 composite (design §3); a missing ship score counts as 0."""
        return composite_score(self.correctness, self.ship_score, self.cost_usd, self.duration_min)


class SuiteResult(BaseModel):
    """One benchmark suite run: the tasks, the profile, and whether it is real data."""

    schema_version: int = SCHEMA_VERSION
    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="when this result was written",
    )
    evaluated: bool = Field(description="False for the template: no benchmark run has happened yet")
    profile: str = DEFAULT_PROFILE
    commit: str | None = Field(default=None, description="baton commit the suite ran against")
    per_run_usd: float | None = Field(
        default=None,
        description="the [budget] per_run_usd cap the suite ran under; None for older JSON",
        allow_inf_nan=False,
    )
    repeats_per_task: int = Field(default=1, description="Phase 1 runs each task once")
    tasks: list[TaskResult] = Field(default_factory=list)
    note: str = Field(default="", description="human note, e.g. why unevaluated")

    @property
    def score(self) -> float | None:
        """The suite score: mean of the task composites (None when unevaluated/empty)."""
        if not self.evaluated:
            return None
        return suite_score(self.tasks)


def unevaluated_suite(tasks: list[TaskSpec] | None = None) -> SuiteResult:
    """The template suite: tasks listed, nothing run, no scores - never fabricated."""
    specs = tasks if tasks is not None else load_tasks()
    return SuiteResult(
        evaluated=False,
        tasks=[
            TaskResult(
                task_id=spec.id,
                title=spec.title,
                passed=False,
                correctness=0.0,
                cost_usd=0.0,
                duration_min=0.0,
                profile=DEFAULT_PROFILE,
                run_date="",
                note="not run yet",
            )
            for spec in specs
        ],
        note=(
            "Template: no benchmark run has been performed. Run "
            "`jev capbench run --all` to produce real scores; nothing here is measured."
        ),
    )


def correctness_score(tests_passed: int, tests_total: int) -> float:
    """Hidden-test fraction x 100 (design §3); no tests run means no evidence: 0.

    Clamped to 0-100: a grader reporting more passes than tests run (5/3)
    must not put 166.7 on the public scoreboard.
    """
    if tests_total <= 0:
        return 0.0
    return min(100.0, max(0.0, 100.0 * tests_passed / tests_total))


def _linear_falling(value: float, full_at: float, zero_at: float) -> float:
    """100 at or below `full_at`, 0 at or above `zero_at`, linear between."""
    if value <= full_at:
        return 100.0
    if value >= zero_at:
        return 0.0
    return 100.0 * (zero_at - value) / (zero_at - full_at)


def cost_score(cost_usd: float) -> float:
    """100 at ≤ $0.50, 0 at ≥ the $2 cap, linear between (design §3)."""
    return _linear_falling(cost_usd, COST_FULL_USD, COST_ZERO_USD)


def time_score(duration_min: float) -> float:
    """100 at ≤ 5 min, 0 at ≥ 30 min, linear between (design §3)."""
    return _linear_falling(duration_min, TIME_FULL_MIN, TIME_ZERO_MIN)


def composite_score(
    correctness: float,
    ship_score: int | None,
    cost_usd: float,
    duration_min: float,
) -> float:
    """The 0-100 composite: 50% correctness, 25% ship, 15% cost, 10% time.

    A missing ship score (no gate ran) counts as 0 - the dimensions are
    published raw on the scoreboard, so this is visible, not silent.
    """
    ship = float(ship_score) if ship_score is not None else 0.0
    return (
        W_CORRECTNESS * correctness
        + W_SHIP * ship
        + W_COST * cost_score(cost_usd)
        + W_TIME * time_score(duration_min)
    )


def suite_score(tasks: list[TaskResult]) -> float | None:
    """Mean of the task composites; None when there is nothing to average."""
    if not tasks:
        return None
    return sum(t.composite for t in tasks) / len(tasks)


def task_result_from_grade(
    spec: TaskSpec,
    grade: GradeReport,
    *,
    run_id: str | None,
    cost_usd: float,
    duration_min: float,
    profile: str,
    run_date: str,
) -> TaskResult:
    """Assemble a TaskResult from a grader report and the run's bookkeeping."""
    return TaskResult(
        task_id=spec.id,
        title=spec.title,
        run_id=run_id,
        passed=grade.passed,
        correctness=correctness_score(grade.tests_passed, grade.tests_total),
        ship_score=grade.ship_score,
        cost_usd=cost_usd,
        duration_min=duration_min,
        profile=profile,
        run_date=run_date,
        grade=grade,
    )


# --- task-specific knobs graders may consult ------------------------------------------
# (kept here so `grade.py` files and the runner agree on the protocol)
GRADE_ARGV = ("--result", "--run-json", "--diff")
"""The argv protocol every `benchmarks/tasks/<id>/grade.py` speaks."""
