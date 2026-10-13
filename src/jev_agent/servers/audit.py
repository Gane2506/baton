"""`.jev/audit/servers.log`: one JSON line per server command or file operation, allowed or not.

Fields: `time` (UTC, ISO 8601), `run_id`, `role`, `server`, `access`, `action` (`run_command`,
`read_file`, `write_file`, `upload`, `browser_install`, `connect`), `command` (or the path),
`allowed`, `reason`, `exit_code`, `duration_s`. Every line is redacted before it is written
(`jev_agent.runlog.redact`). File contents are never logged, only their size.
"""

from __future__ import annotations

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
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)

    def read(self) -> list[dict[str, object]]:
        """Every entry, oldest first (for tests and `jev doctor`)."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]
