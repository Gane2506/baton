"""The capability benchmark: fixed tasks run through Baton, published as a scoreboard.

Terminology (to avoid the collision): the repo already has "benchmarks" in
`jev_agent.core.benchmarks` - KPI regression detection over *shipped PRs*
(docs/ship.md). This package is about something different: a fixed suite of
small tasks run *through* Baton (plan → validate → build → review → quality
gate) to measure what the orchestrator-plus-workers can do, published in
`docs/benchmarks.md`. New code lives here, never in `core/benchmarks.py`.
"""

from jev_agent.capbench.grading import (
    diff_within_bands,
    read_ship_score,
    run_grade_script,
    run_hidden_pytest,
    stdlib_only_imports,
    tests_dir_untouched,
)
from jev_agent.capbench.models import (
    COST_FULL_USD,
    COST_ZERO_USD,
    DEFAULT_PER_RUN_USD,
    DEFAULT_PROFILE,
    DEFAULT_QUALITY_LEVEL,
    TIME_FULL_MIN,
    TIME_ZERO_MIN,
    W_CORRECTNESS,
    W_COST,
    W_SHIP,
    W_TIME,
    GradeReport,
    SuiteResult,
    TaskResult,
    TaskSpec,
    composite_score,
    correctness_score,
    cost_score,
    load_task,
    load_tasks,
    suite_score,
    time_score,
)
from jev_agent.capbench.report import badge_payload, render_scoreboard, write_scoreboard
from jev_agent.capbench.runner import (
    BENCHMARK_TOML_TEMPLATE,
    BenchmarkConfig,
    prepare_sandbox,
    run_suite,
    run_task,
)

__all__ = [
    "BENCHMARK_TOML_TEMPLATE",
    "COST_FULL_USD",
    "COST_ZERO_USD",
    "DEFAULT_PER_RUN_USD",
    "DEFAULT_PROFILE",
    "DEFAULT_QUALITY_LEVEL",
    "TIME_FULL_MIN",
    "TIME_ZERO_MIN",
    "W_CORRECTNESS",
    "W_COST",
    "W_SHIP",
    "W_TIME",
    "BenchmarkConfig",
    "GradeReport",
    "SuiteResult",
    "TaskResult",
    "TaskSpec",
    "badge_payload",
    "composite_score",
    "correctness_score",
    "cost_score",
    "diff_within_bands",
    "load_task",
    "load_tasks",
    "prepare_sandbox",
    "read_ship_score",
    "render_scoreboard",
    "run_grade_script",
    "run_hidden_pytest",
    "run_suite",
    "run_task",
    "stdlib_only_imports",
    "suite_score",
    "tests_dir_untouched",
    "time_score",
    "write_scoreboard",
]
