"""Quality use cases shared by the CLI and the web UI (issue #92).

Which checks apply and whether their tools are installed (`missing_tools`, what `jev doctor`
reports), and the run's review text (`review_text`, what `jev quality show` prints and the
dashboard renders). No printing, no markup. `cli.quality_cmds` keeps the commands and
re-exports these.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jev_agent.config import STATE_DIRNAME, JevConfig
from jev_agent.core.store import QualityReportRecord, RunRecord
from jev_agent.quality import tools
from jev_agent.quality.checks import CHECKS, QualityCheck
from jev_agent.quality.gate import RUN_REVIEW_FILENAME, quality_dir
from jev_agent.quality.levels import QualityLevel, at_least
from jev_agent.quality.report import render_run_review


def review_path(root: Path, run_id: str) -> Path:
    """`.jev/runs/<run_id>/quality/review.md`."""
    return quality_dir(root / STATE_DIRNAME, run_id) / RUN_REVIEW_FILENAME


def review_text(root: Path, run: RunRecord, reports: list[QualityReportRecord]) -> str:
    """The run's review.md (Markdown), rebuilt from the database if the file was removed."""
    path = review_path(root, run.id)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return render_run_review(run.id, run.quality_level, [r.result for r in reports])


@dataclass(frozen=True)
class CheckRow:
    level: QualityLevel  # the first level the check runs at
    name: str
    tool: str
    installed: str  # "yes" | "no" | "built in" | "fallback" | "varies"
    applies: bool
    blocks_from: str


def check_rows(
    root: Path, config: JevConfig, checks: list[QualityCheck] | None = None
) -> list[CheckRow]:
    """One row per registered check: where it starts, its tool and whether it is installed."""
    rows = []
    for check in CHECKS if checks is None else checks:
        tool = check.tool or "-"
        if check.tool is None:
            installed = "varies" if check.name == "tests" else "built in"
        elif check.tool == "pytest-cov":
            installed = "project dependency"
        elif tools.resolve_tool(check.tool, (root,), tools.which):
            installed = "yes"
        elif check.name == "secrets":
            installed = "no (built-in scan)"
        else:
            installed = "no"
        if check.tool and check.tool in config.quality.required_tools:
            installed += " (required)"
        rows.append(
            CheckRow(
                check.levels[0],
                check.name,
                tool,
                installed,
                check.applies(root),
                check.gate_from or "-",
            )
        )
    return rows


def missing_tools(root: Path, config: JevConfig, level: QualityLevel) -> list[str]:
    """Tools of checks that run at `level` and apply to the repo but are not installed."""
    return [
        row.tool
        for row in check_rows(root, config)
        if at_least(level, row.level)
        and row.applies
        and row.installed.startswith("no")
        and "built-in" not in row.installed
    ]
