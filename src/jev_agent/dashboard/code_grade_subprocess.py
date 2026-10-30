"""Grade/route worker run under a different interpreter.

The dashboard grades in its own process by default. When the Coding
Decision Agent backend lives in the dedicated install venv (design
§5.4, branch 2) instead of the dashboard's own environment, the grade
runner spawns this module under that venv's interpreter so the grade
can see the venv-only install. The argv is a server-side constant
built from the discovered venv interpreter only — never from a
request — and the module prints exactly one JSON line to stdout:

    {"ok": true, "result": {...}}                      on a grade/route
    {"ok": false, "unavailable": true, "message": ...} when no backend
    {"ok": false, "error": "..."}                      on any other failure

Tracebacks go to stderr (the parent discards them); the stdout
protocol never carries one, so a child crash degrades to a failed
grade with a plain message, never a traceback on the page.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from jev_agent.code_orchestrator import CodeOrchestratorUnavailable
from jev_agent.config.schema import CodeOrchestratorSettings
from jev_agent.dashboard.code_grade_runner import grade, route
from jev_agent.dashboard.errors import friendly_error


def _settings(request: dict[str, Any]) -> CodeOrchestratorSettings:
    return CodeOrchestratorSettings(
        model_id=str(request["model_id"]),
        max_diff_chars=int(request["max_diff_chars"]),
        confidence_threshold=float(request["confidence_threshold"]),
    )


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    """Run one grade/route request; returns the stdout JSON payload."""
    settings = _settings(request)
    kind = str(request["kind"])
    try:
        if kind == "grade":
            result = grade(
                settings,
                str(request["task"]),
                str(request["diff"]),
                str(request.get("tests", "")),
            )
        elif kind == "route":
            result = route(
                settings,
                str(request["route_request"]),
            )
        else:
            return {"ok": False, "error": f"unknown grade kind: {kind!r}"}
    except CodeOrchestratorUnavailable as exc:
        return {
            "ok": False,
            "unavailable": True,
            "message": friendly_error(exc),
        }
    except Exception as exc:  # the parent needs a plain message, never a traceback
        return {"ok": False, "error": friendly_error(exc)}
    return {"ok": True, "result": result}


def main() -> int:
    request = json.loads(sys.stdin.read())
    json.dump(run_request(request), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
