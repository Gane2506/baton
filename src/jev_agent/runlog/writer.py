"""Run log files: where they live and how they are written (always redacted).

Layout (PLAN.md section 9): `.jev/runs/<run_id>/<scope>/<name>.log`, where `scope` is a task id
or `run` for run-level work (planning, validation, integration).
"""

from __future__ import annotations

from pathlib import Path

from jev_agent.runlog.redact import redact

RUN_SCOPE = "run"


def _segment(label: str, value: str | int) -> str:
    """One path segment of a log path, validated: log paths are a write target, so `/`,
    `..`, empty segments and NULs are rejected (callers pass app-generated ids; this
    is defense in depth)."""
    text = str(value)
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        raise ValueError(f"unsafe log path segment {label}={text!r}")
    return text


def log_path(state_dir: Path, run_id: str, scope: str | None, name: str | int) -> Path:
    """Path of a log file; `scope` None means run-level. Does not create anything."""
    return (
        state_dir
        / "runs"
        / _segment("run_id", run_id)
        / _segment("scope", scope or RUN_SCOPE)
        / f"{_segment('name', name)}.log"
    )


def write_log(path: Path, text: str) -> None:
    """Write `text` to `path` (replacing it) after redacting secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(text), encoding="utf-8")


def append_log(path: Path, text: str) -> None:
    """Append `text` to `path` after redacting secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(redact(text))
