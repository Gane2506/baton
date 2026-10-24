"""Shared form, git and run-log helpers for the dashboard's view builders."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request

from jev_agent.core.kpi import DiffStat, diff_stat
from jev_agent.core.models import RunState
from jev_agent.core.store import AttemptRecord
from jev_agent.core.workspace import GIT_QUICK_TIMEOUT_S, result_branch, run_git, start_ref
from jev_agent.dashboard import views
from jev_agent.dashboard.security import CSRF_FIELD, same_origin


def _text(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else None


def _back_target(request: Request) -> str:
    """Where a sidebar-picker POST redirects: the same-origin referer, else `/`.

    Only the referer's path and query are kept, so a forged absolute URL can
    never turn the redirect into an open redirect.
    """
    referer = request.headers.get("referer")
    if referer and same_origin(request):
        target = urlsplit(referer)
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        if path.startswith("/"):
            return path
    return "/"


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise HTTPException(404, "no such item") from None


_WORK_TABS: tuple[str, ...] = ("plan", "diff", "tests", "preview", "terminal")


_PLAN_ACTIVE_STATES = frozenset(
    {
        RunState.INTAKE,
        RunState.PLANNING,
        RunState.VALIDATING,
        RunState.AWAITING_PLANNING,
        RunState.AWAITING_VALIDATING,
    }
)


_DIFF_CAP_BYTES = 200 * 1024  # the work pane's diff tab never renders more than this


_TERMINAL_TAIL_LINES = 200


_TERMINAL_TAIL_BYTES = 256 * 1024  # only the tail of a worker log is read


def _newest_attempt_log(
    repo: views.Repo, run_id: str, attempts: list[AttemptRecord]
) -> Path | None:
    """The newest attempt's log file strictly under the run's directory, or None. Worker logs
    may point anywhere, so paths outside the run dir are never served."""
    base = views.run_dir(repo, run_id).resolve()
    for attempt in sorted(attempts, key=lambda a: a.started_at, reverse=True):
        if attempt.log_path is None:
            continue
        try:
            path = attempt.log_path.resolve()
            path.relative_to(base)
        except (OSError, ValueError):
            continue
        if path.is_file():
            return path
    return None


def _tail_lines(path: Path, lines: int = _TERMINAL_TAIL_LINES) -> str:
    """The last `lines` lines of `path` (at most the last 256KB), decoded lossily."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - _TERMINAL_TAIL_BYTES))
        data = handle.read()
    return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", errors="replace")


def _run_diff(root: Path, run_id: str) -> str | None:
    """`git diff` of the run's result branch against its start ref, capped at 200KB; None
    when there is no diff yet or git cannot tell (the tab shows its empty state)."""
    try:
        proc = run_git(
            [
                "diff",
                "--no-color",
                "--no-ext-diff",
                f"{start_ref(run_id)}..{result_branch(run_id)}",
            ],
            root,
            GIT_QUICK_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout[:_DIFF_CAP_BYTES]


def _git_branch(root: Path) -> str:
    """Current git branch of the repo root; "" when git cannot tell."""
    try:
        proc = run_git(["rev-parse", "--abbrev-ref", "HEAD"], root, GIT_QUICK_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    branch = proc.stdout.strip()
    return branch if proc.returncode == 0 and branch else ""


def _safe_diff_stat(root: Path, base: str, head: str) -> DiffStat | None:
    """`git diff --shortstat base..head` as numbers; None when git cannot tell."""
    try:
        return diff_stat(root, base, head)
    except OSError:
        return None


def _format_diff_stat(stat: DiffStat | None) -> str:
    if stat is None:
        return "-"
    return f"{stat.files} files, +{stat.additions}/-{stat.deletions}"


def palette_registry(csrf_token: str) -> dict[str, Any]:
    """The Cmd+K palette's registry: navigation entries, whitelisted POST actions and
    CLI-only commands (greyed, with the pasteable command). Rendered as JSON into
    base.html; `static/palette.js` filters it and handles the keyboard. No backend route
    is needed: actions POST to the same routes their pages use, with the same confirm
    flows (Tier 2 entries open the confirm page, they do not fire directly)."""
    nav = [
        ("Board: runs and the new-run form", "/#new-run"),
        ("Run: approve, resume, cancel, ship (on the run page)", "/"),
        ("Queue: ideas and the scheduler", "/queue#scheduler"),
        ("Artifacts: work products", "/artifacts"),
        ("Agents: roles, workers and models", "/agents"),
        ("Fleet: the agent roster", "/fleet"),
        ("Chats", "/chats"),
        ("Learning: profiles, lessons", "/learning"),
        ("Profiles: workspace profiles", "/profiles"),
        ("Design systems: the design library", "/design-systems"),
        ("Tools: MCP, skills and plugins", "/tools"),
        ("Tools: model browser", "/tools#models"),
        ("Tools: quality checks", "/tools#quality-checks"),
        ("Connections: servers, keys, browser", "/connections#browser"),
        ("Spend", "/spend"),
        ("KPIs: shipped pull requests", "/kpis"),
        ("Diagnostics: doctor checks", "/diagnostics"),
        ("Settings: this dashboard", "/settings#s-readonly"),
    ]
    actions = [
        ("Queue: clear all items", "/queue/clear"),
        ("Scheduler: start", "/queue/scheduler/start"),
        ("Scheduler: stop", "/queue/scheduler/stop"),
        ("KPIs: sync from GitHub", "/kpis/sync"),
        ("Connections: check servers now", "/connections/refresh"),
        ("Connections: install the browser", "/connections/browser/install"),
    ]
    cli = [
        ("init: create jev.toml and .jev/", "jev init"),
        ("dashboard: serve the dashboard", "jev dashboard"),
        ("mcp servers: serve MCP tools over stdio", "jev mcp servers"),
        ("mcp runs: serve the pipeline tools over stdio", "jev mcp runs"),
        ("doctor: also check servers over SSH", "jev doctor --connect"),
        ("scheduler: run one item, then exit", "jev scheduler start --once"),
        ("ship: show the PR without opening it", "jev ship --dry-run"),
    ]
    return {
        "csrf_field": CSRF_FIELD,
        "csrf_token": csrf_token,
        "entries": [
            *({"kind": "nav", "label": label, "target": target} for label, target in nav),
            *({"kind": "action", "label": label, "target": target} for label, target in actions),
            *({"kind": "cli", "label": label, "command": command} for label, command in cli),
        ],
    }


def _record_ship_sync(state_dir: Path) -> None:
    """Stamp when `ship sync --all` last ran, so the KPIs page can show its freshness."""
    payload = json.dumps({"synced_at": datetime.now(UTC).isoformat()})
    (state_dir / views.SHIP_SYNC_FILENAME).write_text(payload)
