"""Demo project support: `jev init --demo` seeding and the demo marker.

A demo repo is an ordinary repo whose `.jev/` holds illustrative, made-up state (sample
runs, queue items, lessons, spend data, shipped pull requests and a saved chat), seeded by
`jev_agent.demo.seeder` (also `examples/demo/seed_demo.py`). The marker file
`.jev/demo.json` says the state is demo data; `jev demo reset` removes it again.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jev_agent.config import STATE_DIRNAME

DEMO_MARKER_FILENAME = "demo.json"
# The state database file, as opened by `jev_agent.app.context` (repeated here so the CLI
# can check for existing state without importing the pipeline).
STATE_DB_FILENAME = "state.db"


def marker_path(root: Path) -> Path:
    """Path of the demo marker inside `root`'s state directory."""
    return root / STATE_DIRNAME / DEMO_MARKER_FILENAME


def is_demo_repo(root: Path) -> bool:
    """Whether `root` holds demo-seeded state (the marker file exists)."""
    return marker_path(root).is_file()


def read_demo_marker(root: Path) -> dict[str, Any] | None:
    """The parsed demo marker, or None when `root` is not a demo repo.

    A corrupt marker (hand-edited or truncated) is treated as absent, not fatal:
    `jev demo reset` should report cleanly instead of dying on a traceback.
    """
    path = marker_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_demo_marker(root: Path, *, seeded_by: str) -> dict[str, Any]:
    """Write `.jev/demo.json`, marking `root`'s state as demo data. Returns the marker."""
    marker: dict[str, Any] = {
        "demo": True,
        "seeded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seeded_by": seeded_by,
        "seeder": "jev_agent.demo.seeder.seed",
        "note": (
            "Illustrative demo data: the runs, queue items, lessons, spend figures, "
            "shipped pull requests and the saved chat are made up; no agent ran. "
            "Remove with `jev demo reset`."
        ),
    }
    marker_path(root).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return marker
