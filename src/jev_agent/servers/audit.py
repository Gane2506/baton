"""`.jev/audit/servers.log`: one JSON line per server command or file operation, allowed or not.

Fields: `time` (UTC, ISO 8601), `run_id`, `role`, `server`, `access`, `action` (`run_command`,
`read_file`, `write_file`, `upload`, `connect`), `command` (or the path),
`allowed`, `reason`, `exit_code`, `duration_s`, `phase`. An allowed operation gets two lines:
`phase` "started" before ssh runs and "finished" (with the exit code) after it returns, so a
command whose process was killed meanwhile is still on record; refused operations and other
writers (`jev doctor --connect`) write one line with an empty `phase`. Every line is redacted
before it is written (`jev_agent.runlog.redact`). File contents are never logged, only their
size.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from jev_agent.runlog.redact import redact

AUDIT_DIRNAME = "audit"
AUDIT_FILENAME = "servers.log"


def audit_path(state_dir: Path) -> Path:
    return state_dir / AUDIT_DIRNAME / AUDIT_FILENAME


@dataclass(frozen=True)
class AuditEntry:
    run_id: str | None
    role: str | None
    server: str
    access: str
    action: str
    command: str
    allowed: bool
    reason: str = ""
    exit_code: int | None = None
    duration_s: float | None = None
    phase: str = ""  # "started" / "finished" for allowed operations, "" otherwise
    time: str = ""


class AuditLog:
    """Appends entries to the audit file (created with mode 0600, its folder 0700)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, entry: AuditEntry) -> None:
        data = asdict(entry)
        data["time"] = entry.time or datetime.now(UTC).isoformat(timespec="seconds")
        if entry.duration_s is not None:
            data["duration_s"] = round(entry.duration_s, 3)
        line = redact(json.dumps(data, ensure_ascii=False)) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            # `mkdir`'s mode applies only on creation; a pre-existing audit dir
            # keeps whatever it had (like `ssh._prepare`).
            self.path.parent.chmod(0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)

    def read(self) -> list[dict[str, object]]:
        """Every entry, oldest first (for tests and `jev doctor`).

        Best-effort: a torn line (a write killed mid-line) is skipped rather than
        failing the whole read.
        """
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        entries: list[dict[str, object]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries
