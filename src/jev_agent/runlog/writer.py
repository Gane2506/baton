"""Run log files: where they live and how they are written (always redacted).

Layout (PLAN.md section 9): `.jev/runs/<run_id>/<scope>/<name>.log`, where `scope` is a task id
or `run` for run-level work (planning, validation, integration).
"""

from __future__ import annotations

from pathlib import Path

from jev_agent.runlog.redact import redact

RUN_SCOPE = "run"


def log_path(state_dir: Path, run_id: str, scope: str | None, name: str | int) -> Path:
    """Path of a log file; `scope` None means run-level. Does not create anything."""
    return state_dir / "runs" / run_id / (scope or RUN_SCOPE) / f"{name}.log"


def write_log(path: Path, text: str) -> None:
    """Write `text` to `path` (replacing it) after redacting secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(text), encoding="utf-8")


def append_log(path: Path, text: str) -> None:
    """Append `text` to `path` after redacting secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(redact(text))
