"""Quality checks: the `QualityCheck` protocol and every registered check (`CHECKS`).

To add a check, write a small class with the attributes and two methods below and append an
instance to `CHECKS`; the gate (`gate.py`) runs every registered check whose level the run
reaches. Nothing else changes.

- `name`: short id, shown in reports (`ruff`, `tests`, ...).
- `tool`: the executable it needs (None: built in); `[quality] required_tools` names these.
- `levels`: the levels it runs at (cumulative: `from_level("medium")`).
- `gate_from`: from this level, a `fail` status blocks the task (None: only its findings'
  severities count; see `policy.py`).
- `scopes`: `task` (each task's diff, in its worktree) and/or `integration` (the result branch).
- `needs`: what makes it apply, for the "not applicable" note; `hint`: how to install the tool.
- `whole_repo`: the tool scans the whole tree, so the gate ignores findings outside the files
  the task (or run) changed. Pre-existing problems never block new work.
- `applies(repo)`: whether the project uses what the check checks (cheap file tests only).
- `run(workdir, runner)`: run it and return a `CheckResult` with status `pass`, `fail` or
  `skipped` (and why). A missing tool is `runner.missing(tool)`: skipped, or failed when it is
  in `required_tools`. Never raise for tool problems; report them.

Tools only run on changed files where they can, so a task is judged on what it changed.
"""

from __future__ import annotations

from collections.abc import Iterable

from jev_agent.quality.base import BaseCheck, QualityCheck, from_level
from jev_agent.quality.checks_basic import (
    EslintCheck,
    PrettierCheck,
    RuffCheck,
    TestSuiteCheck,
    detect_test_command,
    pytest_command,
)
from jev_agent.quality.checks_medium import (
    BanditCheck,
    CoverageCheck,
    NpmAuditCheck,
    PipAuditCheck,
    SecretScanCheck,
    SemgrepCheck,
    scan_added_lines,
)
from jev_agent.quality.checks_production import (
    CheckovCheck,
    HadolintCheck,
    LicenseCheck,
    MypyCheck,
    PyrightCheck,
    TrivyCheck,
    TscCheck,
    new_python_dependencies,
)
from jev_agent.quality.levels import QualityLevel

# ---- the registry ----------------------------------------------------------------------------

CHECKS: list[QualityCheck] = [
    RuffCheck(),
    EslintCheck(),
    PrettierCheck(),
    TestSuiteCheck(),
    SecretScanCheck(),
    BanditCheck(),
    SemgrepCheck(),
    PipAuditCheck(),
    NpmAuditCheck(),
    CoverageCheck(),
    MypyCheck(),
    PyrightCheck(),
    TscCheck(),
    LicenseCheck(),
    TrivyCheck(),
    HadolintCheck(),
    CheckovCheck(),
]


def checks_for(
    level: QualityLevel, checks: Iterable[QualityCheck] | None = None
) -> list[QualityCheck]:
    """The registered checks that run at `level` (none at "none"), in registry order."""
    return [c for c in (CHECKS if checks is None else checks) if level in c.levels]


__all__ = [
    "CHECKS",
    "BanditCheck",
    "BaseCheck",
    "CheckovCheck",
    "CoverageCheck",
    "EslintCheck",
    "HadolintCheck",
    "LicenseCheck",
    "MypyCheck",
    "NpmAuditCheck",
    "PipAuditCheck",
    "PrettierCheck",
    "PyrightCheck",
    "QualityCheck",
    "RuffCheck",
    "SecretScanCheck",
    "SemgrepCheck",
    "TestSuiteCheck",
    "TrivyCheck",
    "TscCheck",
    "checks_for",
    "detect_test_command",
    "from_level",
    "new_python_dependencies",
    "pytest_command",
    "scan_added_lines",
]
