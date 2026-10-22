"""The dashboard's Starlette app: routes, templates and actions (docs/dashboard.md).

Pages read through `views` (the CLI's own store and helpers); actions call the same functions
as the CLI commands (`core.queue.add_idea`, `core.learning.enable_learning`,
`core.pipeline.cancel_run`, `approve_run` / `resume_run` through `runner.RunLauncher`, ...).
Every action is a POST checked by `security.checked_form`, and redirects back (POST, redirect,
GET) with a one-time message.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from jinja2 import Environment, PackageLoader
from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from jev_agent import __version__
from jev_agent.app import chat, settings
from jev_agent.app import profiles as app_profiles
from jev_agent.app.board import DONE_LIMIT, state_legend
from jev_agent.app.errors import JevError
from jev_agent.app.format import format_age, format_answers, format_time
from jev_agent.app.learning import (
    create_profile as learning_create,
)
from jev_agent.app.learning import (
    delete as learning_delete_profile,
)
from jev_agent.app.learning import (
    disable as learning_disable,
)
from jev_agent.app.learning import (
    enable as learning_enable,
)
from jev_agent.app.learning import (
    flush as flush_lessons,
)
from jev_agent.app.learning import (
    forget as learning_forget,
)
from jev_agent.app.learning import (
    use_learning_profile,
)
from jev_agent.app.quality import review_text
from jev_agent.app.queue import QueueOptions
from jev_agent.app.runs import RunOptions, require_ship_enabled
from jev_agent.config import STATE_DIRNAME, ConfigError
from jev_agent.config.schema import BEGINNER_FIELDS, JevConfig, JevSettings, ShipMode
from jev_agent.config.servers_schema import ServerConfig
from jev_agent.core.benchmarks import DEFAULT_LAST, format_value
from jev_agent.core.budget import approval_budget_block
from jev_agent.core.chat import (
    BACKENDS,
    DEFAULT_BUDGET_USD,
    BackendName,
    ChatError,
    ChatLearning,
    ChatMeta,
    describe_design_system,
    list_design_library,
    parse_design_system,
)
from jev_agent.core.design_library import (
    delete_library_system,
    export_tokens_css,
    export_tokens_tailwind,
    export_tokens_w3c,
    library_system_path,
    list_library,
    load_library_system,
    save_library_system,
    validate_library_name,
)
from jev_agent.core.design_system import (
    META_JSON,
    design_dir,
    get_design_system,
    save_design_system,
)
from jev_agent.core.kpi import DiffStat, diff_stat
from jev_agent.core.learning import LearningError, learning_status
from jev_agent.core.lease import RunInUseError
from jev_agent.core.models import RunState, TaskState
from jev_agent.core.pipeline import STOPPED, PipelineError, cancel_run
from jev_agent.core.provider_auth import (
    CLAUDE_OAUTH_TOKEN_ENV,
    CodexDeviceFlow,
    check_claude_oauth_token,
)
from jev_agent.core.queue import Move, QueueError, add_idea, move_item, queue_counts, remove_item
from jev_agent.core.scheduler import SchedulerLocked, spent_today
from jev_agent.core.ship import (
    Gh,
    ShipError,
    ShipPlan,
    check_gh,
    prepare_ship,
    prs_to_sync,
    sync_pr,
)
from jev_agent.core.store import (
    QUEUE_PRIORITIES,
    AttemptRecord,
    DecisionRecord,
    NotFoundError,
    ShippedPrRecord,
    TaskRecord,
)
from jev_agent.core.workspace import (
    GIT_QUICK_TIMEOUT_S,
    result_branch,
    run_git,
    start_ref,
)
from jev_agent.dashboard import ui_styles, views
from jev_agent.dashboard.browser_install import BrowserInstallBusy, BrowserInstallRunner
from jev_agent.dashboard.preview import (
    PreviewBridge,
    PreviewError,
    PreviewInput,
    PreviewSession,
    read_capped_body,
)
from jev_agent.dashboard.render import (
    render_daily_spend,
    render_markdown,
    render_trend_chart,
)
from jev_agent.dashboard.runner import (
    ChatRunner,
    Executor,
    RunAction,
    RunLauncher,
    execute_run,
    prepare_run,
    request_changes,
)
from jev_agent.dashboard.scheduler_runner import SchedulerRunner
from jev_agent.dashboard.security import (
    CSRF_FIELD,
    DashboardSettings,
    SecurityMiddleware,
    checked_form,
    same_origin,
)
from jev_agent.demo import is_demo_repo
from jev_agent.quality.levels import LEVEL_SUMMARY, LEVELS
from jev_agent.servers.audit import AuditEntry, AuditLog, audit_path
from jev_agent.servers.profiles import ProfileError
from jev_agent.servers.ssh import (
    CONNECT_TIMEOUT_S,
    SshRunner,
    SshTransport,
    control_dir,
    run_process,
)

STATIC_DIR = Path(__file__).parent / "static"

NAV = (
    ("/", "Board"),
    ("/queue", "Queue"),
    ("/artifacts", "Artifacts"),
    ("/agents", "Agents"),
    ("/fleet", "Fleet"),
    ("/chats", "Chats"),
    ("/learning", "Learning"),
    ("/profiles", "Profiles"),
    ("/design-systems", "Design Systems"),
    ("/tools", "Tools"),
    ("/connections", "Connections"),
    ("/spend", "Spend"),
    ("/kpis", "KPIs"),
    ("/settings", "Settings"),
    ("/diagnostics", "Diagnostics"),
)


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def _time(value: datetime | str | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            return format_time(datetime.fromisoformat(value))
        except ValueError:
            return value
    return format_time(value)


def _age(value: datetime) -> str:
    return format_age(value, datetime.now(UTC))


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("jev_agent.dashboard", "templates"),
        autoescape=True,  # every value is escaped unless it is explicitly Markup
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["usd"] = _usd
    env.filters["time"] = _time
    env.filters["age"] = _age
    env.globals["nav"] = NAV
    env.globals["version"] = __version__
    env.globals["csrf_field"] = CSRF_FIELD
    env.globals["daily_bars_svg"] = render_daily_spend
    env.globals["trend_chart_svg"] = render_trend_chart
    env.globals["fmt"] = format_value
    return env


# ---- connection status ---------------------------------------------------------------------


ssh_runner: SshRunner = run_process  # faked in tests; `ssh HOST true` otherwise


@dataclass(frozen=True)
class _ConnServer:
    """One configured server and its last-known reachability. Built from the config plus
    the audit log's `connect` entries; building it never opens an SSH connection."""

    name: str
    host: str
    user: str | None
    access: str  # the configured access tier: basic, medium or unrestricted
    state: str  # "reachable" | "unreachable" | "unknown"
    last_check: datetime | None


@dataclass(frozen=True)
class _ConnKey:
    """An API key env var the dispatcher route needs: its name and whether it is set,
    never the value."""

    name: str
    is_set: bool


@dataclass(frozen=True)
class _ConnLeg:
    title: str
    keys: tuple[_ConnKey, ...]
    note: str


@dataclass(frozen=True)
class _ConnRoute:
    """The Jev decision-model route the repo's config builds (mirrors
    `app.context.build_dispatcher`): native TypeSafe, then Jev through OpenRouter,
    then the rules fallback."""

    primary: str
    legs: tuple[_ConnLeg, ...]
    final: str
    warning: str | None


_CONN_AUDIT_TAIL_BYTES = 64 * 1024  # one backward read of the audit tail per snapshot


def _parse_connect_line(raw: bytes) -> tuple[str | None, int | None, datetime | None]:
    """(server, exit_code, checked_at) from one audit line, or Nones when the line is not
    a `connect` entry. `connect` entries are written by `jev doctor --connect` and by the
    dashboard's explicit refresh action."""
    try:
        entry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None, None
    if not isinstance(entry, dict) or entry.get("action") != "connect":
        return None, None, None
    server = entry.get("server")
    if not isinstance(server, str) or not server:
        return None, None, None
    exit_code = entry.get("exit_code")
    checked_at: datetime | None = None
    when = entry.get("time")
    if isinstance(when, str):
        try:
            checked_at = datetime.fromisoformat(when)
        except ValueError:
            checked_at = None
    return server, exit_code if isinstance(exit_code, int) else None, checked_at


def _last_connect_checks(
    state_dir: Path, names: Collection[str]
) -> dict[str, tuple[int | None, datetime | None]]:
    """The last `connect` audit entry per server: exit code and check time.

    Reads the audit file backwards in 64KB chunks and stops once every configured
    server has an entry (or the file is exhausted), so a big audit log never slows the
    panel down. A GET never opens an SSH connection; this is the cached state.
    """
    want = set(names)
    if not want:
        return {}
    path = audit_path(state_dir)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {}
    found: dict[str, tuple[int | None, datetime | None]] = {}
    with open(path, "rb") as handle:
        offset = size
        head = b""
        while offset > 0 and len(found) < len(want):
            start = max(0, offset - _CONN_AUDIT_TAIL_BYTES)
            handle.seek(start)
            chunk = handle.read(offset - start) + head
            offset = start
            lines = chunk.split(b"\n")
            head = lines[0]  # a possibly partial first line, completed by the next chunk
            for raw in reversed(lines[1:]):
                server, exit_code, checked_at = _parse_connect_line(raw)
                if server is None or server not in want or server in found:
                    continue
                found[server] = (exit_code, checked_at)
        if head.strip() and len(found) < len(want):  # the file's very first line
            server, exit_code, checked_at = _parse_connect_line(head)
            if server is not None and server in want and server not in found:
                found[server] = (exit_code, checked_at)
    return found


def _connection_servers(config: JevConfig, state_dir: Path) -> list[_ConnServer]:
    """Every configured server with its cached reachability (audit log, never live SSH)."""
    last = _last_connect_checks(state_dir, list(config.servers))
    servers = []
    for name, server in config.servers.items():
        exit_code, checked_at = last.get(name, (None, None))
        state = (
            "unknown" if name not in last else ("reachable" if exit_code == 0 else "unreachable")
        )
        servers.append(
            _ConnServer(
                name=name,
                host=server.host,
                user=server.user,
                access=server.access,
                state=state,
                last_check=checked_at,
            )
        )
    return servers


def _conn_aggregate(servers: list[_ConnServer]) -> tuple[str, str]:
    """(slot state, label) for the sidebar: All connected / N unreachable / Not checked."""
    if not servers:
        return "ok", "Live"  # nothing to report; the slot keeps workstream D's default
    down = sum(1 for s in servers if s.state == "unreachable")
    if down:
        return "degraded", "1 unreachable" if down == 1 else f"{down} unreachable"
    unknown = sum(1 for s in servers if s.state == "unknown")
    if unknown:
        return "degraded", "1 not checked" if unknown == 1 else f"{unknown} not checked"
    return "ok", "All connected"


def _key_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _dispatcher_route(config: JevConfig) -> _ConnRoute:
    """The effective decision-model route, mirroring `app.context.build_dispatcher`.

    `consensus` is Jev (native TypeSafe, then OpenRouter) plus Laya's second vote with
    no rules fallback; `jev` adds the rules fallback; `rules` is rules alone. Key env
    var names are shown with set/unset; values never leave the process.
    """
    primary = config.dispatcher.primary
    native_env = JevSettings.for_native().api_key_env  # "TYPESAFE_API_KEY"
    openrouter_env = config.dispatcher.jev.api_key_env  # default "OPENROUTER_API_KEY"
    native = _ConnKey(native_env, _key_set(native_env))
    openrouter = _ConnKey(openrouter_env, _key_set(openrouter_env))
    if primary == "rules":
        return _ConnRoute(
            primary="rules",
            legs=(
                _ConnLeg(
                    "Rules dispatcher",
                    (),
                    "Deterministic answers from simple rules: no network calls, no keys.",
                ),
            ),
            final="Rules decide every decision.",
            warning=None,
        )
    jev_legs = (
        _ConnLeg("Jev \u00b7 native TypeSafe", (native,), "TypeSafe's Jev API answers first."),
        _ConnLeg(
            "Jev \u00b7 OpenRouter",
            (openrouter,),
            "When the native leg cannot serve a request, the same Jev model answers "
            "through OpenRouter.",
        ),
    )
    if not native.is_set and not openrouter.is_set:
        outcome = (
            "the run stops for a human at its first decision."
            if primary == "consensus"
            else "every decision uses the rules dispatcher."
        )
        warning: str | None = (
            f"{native_env} and {openrouter_env} are not set, so Jev cannot be reached; {outcome}"
        )
    elif not native.is_set:
        warning = (
            f"{native_env} is not set, so every Jev decision goes through OpenRouter "
            f"({openrouter_env}) without the native TypeSafe leg."
        )
    elif not openrouter.is_set:
        warning = (
            f"{openrouter_env} is not set, so there is no failover if the native TypeSafe "
            f"leg ({native_env}) cannot serve a decision."
        )
    else:
        warning = None
    if primary == "consensus":
        return _ConnRoute(
            primary="consensus",
            legs=(
                *jev_legs,
                _ConnLeg(
                    "Laya \u00b7 second vote",
                    (),
                    "The Laya decision matrix scores the same decision question independently.",
                ),
            ),
            final="No rules fallback: when Jev and Laya disagree, the run stops for a human.",
            warning=warning,
        )
    return _ConnRoute(
        primary="jev",
        legs=jev_legs,
        final="Rules dispatcher: the deterministic fallback when Jev cannot answer.",
        warning=warning,
    )


def _check_one_server(name: str, server: ServerConfig, state_dir: Path) -> str:
    """One `ssh HOST true`, like `jev doctor --connect`. The audit line it writes is the
    cached state the panel and `/connections/live` read back.

    `ssh_runner` is a module global (not a default argument) so tests can fake it.
    """
    transport = SshTransport(server, sockets=control_dir(state_dir), runner=ssh_runner)
    result = transport.check(timeout_s=CONNECT_TIMEOUT_S + 5)
    entry = AuditEntry(
        run_id=None,
        role="dashboard",
        server=name,
        access=server.access,
        action="connect",
        command="true",
        allowed=True,
        exit_code=result.exit_code,
        duration_s=result.duration_s,
    )
    try:
        AuditLog(audit_path(state_dir)).write(entry)
    except OSError:
        return "unknown"  # the check ran, but its result could not be recorded
    return "reachable" if result.ok else "unreachable"


def _refresh_servers(config: JevConfig, state_dir: Path) -> str:
    """Check every server. Only called from the explicit refresh POST: a GET never runs
    this, so reloading the page cannot repeat the SSH connections."""
    names = list(config.servers)
    if not names:
        return "no servers configured"
    if shutil.which("ssh") is None:
        return "`ssh` not found on PATH; servers cannot be checked"
    states: dict[str, list[str]] = {"reachable": [], "unreachable": [], "unknown": []}
    for name in names:
        states[_check_one_server(name, config.servers[name], state_dir)].append(name)
    parts = []
    if states["reachable"]:
        parts.append("reachable: " + ", ".join(states["reachable"]))
    if states["unreachable"]:
        parts.append("unreachable: " + ", ".join(states["unreachable"]))
    if states["unknown"]:
        parts.append("not checked: " + ", ".join(states["unknown"]))
    return "; ".join(parts) + " (recorded in the audit log)"


class Dashboard:
    """The app's state: settings, templates, background runs and one-time messages."""

    # Flash store bounds: entries per session, and how long a session that never
    # renders again may keep its entries (its cookie is attacker-influenceable).
    _MAX_FLASHES_PER_SESSION = 30
    _FLASH_SESSION_TTL_S = 6 * 3600

    def __init__(self, settings: DashboardSettings, launcher: RunLauncher) -> None:
        self.settings = settings
        self.launcher = launcher
        self.chat_runner = ChatRunner()
        self.scheduler_runner = SchedulerRunner(settings.root, launcher)
        self.browser_installer = BrowserInstallRunner(settings.root)
        self.codex_flow = CodexDeviceFlow()
        self.started_at = datetime.now(UTC)  # the About panel's uptime
        self.env = _environment()
        self._flash_lock = threading.Lock()
        self._flashes: dict[str, list[tuple[str, str]]] = {}
        self._flash_seen: dict[str, float] = {}  # session -> last flash (monotonic)

    # ---- helpers ---------------------------------------------------------------------------

    def flash(self, request: Request, kind: str, text: str) -> None:
        with self._flash_lock:
            self._prune_flashes()
            session = request.state.session
            flashes = self._flashes.setdefault(session, [])
            flashes.append((kind, text))
            # A session that POSTs forever but never renders again must not grow the
            # store without bound: keep the newest entries.
            del flashes[: -self._MAX_FLASHES_PER_SESSION]
            self._flash_seen[session] = time.monotonic()

    def take_flashes(self, request: Request) -> list[tuple[str, str]]:
        with self._flash_lock:
            self._flash_seen.pop(request.state.session, None)
            return self._flashes.pop(request.state.session, [])

    def _prune_flashes(self) -> None:
        """Drop sessions older than the TTL (called with the flash lock held)."""
        cutoff = time.monotonic() - self._FLASH_SESSION_TTL_S
        for session in [s for s, seen in self._flash_seen.items() if seen < cutoff]:
            del self._flash_seen[session]
            self._flashes.pop(session, None)

    def shutdown_jobs(self, timeout: float) -> list[str]:
        """Cancel and join every background job (runs, chat turns and the dashboard's
        scheduler), for the lifespan."""
        # Stop the scheduler first so it starts no new items; the launcher then cancels
        # the in-flight item's run, which lets the scheduler thread exit promptly.
        self.scheduler_runner.stop()
        self.browser_installer.shutdown()
        self.codex_flow.shutdown()
        return [
            *self.launcher.shutdown(timeout),
            *self.chat_runner.shutdown(timeout),
            *self.scheduler_runner.shutdown(timeout),
        ]

    def render(
        self, request: Request, template: str, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        # Flashes are shown once, on the next successful full-page render. A failed
        # request (e.g. a live poll hitting error.html) must not eat them (#52).
        show_flashes = 200 <= status_code < 300 and not template.startswith("_")
        page = self.env.get_template(template).render(
            request=request,
            path=request.url.path,
            csrf=request.state.csrf,
            # The UI style (workstream UI-02): every page shares base.html, so one
            # server-rendered `data-ui-style` attribute plus ui-styles.css covers
            # the whole dashboard. read_ui_style() never raises (falls back to
            # the default), so a broken preference cannot break page rendering.
            ui_style=ui_styles.read_ui_style(),
            ui_style_options=ui_styles.UI_STYLES,
            palette_registry=palette_registry(request.state.csrf),
            refresh_ms=int(self.settings.refresh_s * 1000),
            root=self.settings.root,
            demo=is_demo_repo(self.settings.root),
            flashes=self.take_flashes(request) if show_flashes else [],
            **context,
        )
        return HTMLResponse(page, status_code=status_code)

    def page(
        self, request: Request, template: str, build: Callable[[views.Repo], dict[str, Any]]
    ) -> HTMLResponse:
        """Render `template` with what `build` reads from the repo (sync; runs in a thread)."""
        try:
            with views.open_repo(self.settings.root) as repo:
                context = build(repo)
        except ConfigError as exc:
            return self.render(request, "error.html", 500, title="Config error", message=str(exc))
        except JevError as exc:
            return self.render(request, "error.html", 500, title="State error", message=str(exc))
        except (NotFoundError, ChatError) as exc:
            return self.render(request, "error.html", 404, title="Not found", message=str(exc))
        return self.render(request, template, **context)

    def back(self, to: str) -> RedirectResponse:
        return RedirectResponse(to, status_code=303)

    async def act(
        self,
        request: Request,
        to: str,
        action: Callable[[views.Repo, FormData], str],
    ) -> Response:
        """Check the form (CSRF), run `action` against the repo in a thread, flash its message
        (or the error), and redirect to `to`."""
        form = await checked_form(request)

        def work() -> str:
            with views.open_repo(self.settings.root) as repo:
                return action(repo, form)

        try:
            message = await run_in_threadpool(work)
        except (
            JevError,
            ConfigError,
            NotFoundError,
            PipelineError,
            QueueError,
            LearningError,
            ShipError,
            ValueError,
        ) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", message)
        return self.back(to)

    # ---- pages -----------------------------------------------------------------------------

    def board_page(self, request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        return self.page(
            request,
            "board.html",
            lambda repo: {
                "columns": views.board(repo, show_all),
                "show_all": show_all,
                "done_limit": DONE_LIMIT,
                "legend": state_legend(),
                "priorities": QUEUE_PRIORITIES,
                "levels": LEVELS,
                "profiles": repo.learning.profiles(),
            },
        )

    def board_fragment(self, request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        return self.page(
            request,
            "_board.html",
            lambda repo: {"columns": views.board(repo, show_all), "show_all": show_all},
        )

    def _run_context(self, repo: views.Repo, run_id: str) -> dict[str, Any]:
        if not views.valid_run_id(run_id):
            raise NotFoundError(f"run {run_id} not found")
        view = views.run_view(repo, run_id)
        job = self.launcher.job(run_id)
        busy = self.launcher.busy(run_id)
        return {"v": view, "job": job, "busy": busy}

    def run_page(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        return self.page(request, "run.html", lambda repo: self._run_context(repo, run_id))

    def _run_live_context(self, repo: views.Repo, run_id: str) -> dict[str, Any]:
        """The 3-second live poll's context: the lightweight view, no log-dir walk."""
        if not views.valid_run_id(run_id):
            raise NotFoundError(f"run {run_id} not found")
        view = views.run_live_view(repo, run_id)
        job = self.launcher.job(run_id)
        busy = self.launcher.busy(run_id)
        return {"v": view, "job": job, "busy": busy}

    def run_fragment(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        return self.page(
            request, "_run_live.html", lambda repo: self._run_live_context(repo, run_id)
        )

    def run_file(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        relative = request.path_params["path"]
        try:
            with views.open_repo(self.settings.root) as repo:
                path = views.safe_run_file(repo, run_id, relative)
        except JevError as exc:
            raise HTTPException(500, str(exc)) from exc
        if path is None:
            raise HTTPException(404, "no such file")
        # Served as plain text whatever the extension, so nothing in a log can run as a page.
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"},
        )

    def queue_page(self, request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        return self.page(
            request,
            "queue.html",
            lambda repo: {
                "q": views.queue_view(repo, show_all),
                "sched": self.scheduler_runner.status(),
                "show_all": show_all,
                "priorities": QUEUE_PRIORITIES,
                "levels": LEVELS,
            },
        )

    def learning_page(self, request: Request) -> Response:
        profile = request.query_params.get("profile")
        include_retired = request.query_params.get("all") == "1"
        tab = request.query_params.get("tab", "list")

        def build(repo: views.Repo) -> dict[str, Any]:
            lv = views.learning_view(repo, profile, include_retired)
            graph = (
                views.learning_graph_view(
                    repo,
                    lv.selected or "",
                    q=request.query_params.get("q", ""),
                    module=request.query_params.get("module", ""),
                    relation=request.query_params.get("rel", ""),
                    focus_id=request.query_params.get("lesson", ""),
                )
                if tab == "graph" and lv.selected
                else None
            )
            return {
                "lv": lv,
                "include_retired": include_retired,
                "tab": tab if tab == "graph" and graph is not None else "list",
                "graph": graph,
                "relation_facets": views.RELATION_FACETS,
                "laya": views.laya_view(
                    repo, request.query_params.get("laya_profile") or lv.selected
                ),
            }

        return self.page(request, "learning.html", build)

    def chats_page(self, request: Request) -> Response:
        query = request.query_params.get("q", "")

        def build(repo: views.Repo) -> dict[str, Any]:
            rows = views.chats(repo, query)
            return {
                "chats": rows,
                "q": query.strip(),
                "busy_ids": {
                    meta.chat_id for meta, _ in rows if self.chat_runner.busy(meta.chat_id)
                },
                "profiles": repo.learning.profiles(),
                "backends": chat.backend_readiness(repo.config),
                # The New chat form's design-system picker, like `profiles` above.
                "design_systems": list_design_library(),
                "has_repo_design_system": get_design_system(repo.root) is not None,
            }

        return self.page(request, "chats.html", build)

    def artifacts_page(self, request: Request) -> Response:
        kind = request.query_params.get("kind", "")
        chat_id = request.query_params.get("chat", "")
        query = request.query_params.get("q", "")

        def build(repo: views.Repo) -> dict[str, Any]:
            rows, chat_options = views.artifacts(repo, kind, chat_id, query)
            return {
                "artifacts": rows,
                "chat_options": chat_options,
                "kind": kind,
                "chat_id": chat_id,
                "q": query.strip(),
                "kinds": views.ARTIFACT_KINDS,
                "kind_labels": views.ARTIFACT_LABELS,
            }

        return self.page(request, "artifacts.html", build)

    def _chat_context(self, chat_id: str) -> Callable[[views.Repo], dict[str, Any]]:
        def build(repo: views.Repo) -> dict[str, Any]:
            meta, entries = views.chat_transcript(repo, chat_id)
            if meta is None and not entries:
                raise NotFoundError(f"no chat {chat_id}")
            return {
                "chat_id": chat_id,
                "meta": meta,
                "entries": entries,
                "job": self.chat_runner.job(chat_id),
                "busy": self.chat_runner.busy(chat_id),
                # The chat's design-system choice, read-only (set at creation).
                "design_system_label": describe_design_system(
                    meta.design_system if meta is not None else None
                ),
                # the redesigned chat page: the conversation rail, pending approvals, the
                # empty-state suggestions, the run step timeline, and the composer picker.
                "rail_chats": views.chats(repo, "")[:30],
                "approval_cards": _approval_cards(repo, meta) if meta is not None else [],
                "suggestions": list(_CHAT_SUGGESTIONS) if not entries else [],
                "step_blocks": _step_blocks(repo, meta) if meta is not None else [],
                "backends": chat.backend_readiness(repo.config),
            }

        return build

    def chat_page(self, request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        return self.page(request, "chat.html", self._chat_context(chat_id))

    def chat_fragment(self, request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        return self.page(request, "_chat_live.html", self._chat_context(chat_id))

    def spend_page(self, request: Request) -> Response:
        return self.page(request, "spend.html", lambda repo: {"s": views.spend_view(repo)})

    def kpis_page(self, request: Request) -> Response:
        try:
            last = min(max(int(request.query_params.get("last", DEFAULT_LAST)), 1), 200)
        except ValueError:
            last = DEFAULT_LAST
        return self.page(
            request,
            "kpis.html",
            lambda repo: {"k": views.kpi_view(repo, last), "rk": views.product_kpis(repo)},
        )

    async def kpis_sync(self, request: Request) -> Response:
        """`jev ship sync --all` behind a button (Tier 1: re-running it is the undo).

        Refreshes every shipped PR's state, reviews and CI from GitHub and records when
        the sync ran, so the page can say how fresh its numbers are.
        """

        def sync(repo: views.Repo, form: FormData) -> str:
            prs = prs_to_sync(repo.store, None)
            if not prs:
                return "nothing shipped yet"
            gh = Gh(repo.root)
            problem = check_gh(gh)
            if problem is not None:
                raise ShipError(problem)
            failed = 0
            for pr in prs:
                try:
                    sync_pr(repo.store, gh, pr)
                except ShipError:
                    failed += 1
            _record_ship_sync(repo.state_dir)
            synced = len(prs) - failed
            note = f"synced {synced} PR(s)"
            return note if not failed else f"{note}, {failed} failed"

        return await self.act(request, "/kpis", sync)

    def settings_page(self, request: Request) -> Response:
        return self._settings_page(request, "settings")

    def diagnostics_page(self, request: Request) -> Response:
        """`jev doctor` (local checks only) as a read-only page (Tier 0)."""

        def build(repo: views.Repo) -> dict[str, Any]:
            checks = views.doctor_checks(repo.root)
            failed = sum(1 for c in checks if c.status == "fail")
            return {"checks": checks, "failed": failed}

        return self.page(request, "diagnostics.html", build)

    def agents_page(self, request: Request) -> Response:
        return self._settings_page(request, "agents")

    def fleet_page(self, request: Request) -> Response:
        """The six-role roster plus every agent working right now (`templates/fleet.html`).

        The roster is static config (not part of the live poll), so the addendum
        textareas are never reset by the auto-refresh; only `_fleet.html` polls.
        """

        def build(repo: views.Repo) -> dict[str, Any]:
            agents = views.fleet(repo)
            return {"agents": agents, "roles": views.role_roster(repo.config, agents)}

        return self.page(request, "fleet.html", build)

    async def fleet_addendum(self, request: Request) -> Response:
        """Save one role's additional prompt instructions (POST), back to its roster card.

        Follows the settings editor's save path (`settings.save` on the repo's
        jev.toml): the merged config must validate, and an empty value removes the key
        so the role falls back to the default (no addendum).
        """
        form = await checked_form(request)
        role = _text(form, "role") or ""
        to = f"/fleet#role-{role}" if role in views.ROLE_NAMES else "/fleet"

        def job() -> str | None:
            if role not in views.ROLE_NAMES:
                return f"unknown role {role!r}"
            path = f"roles.{role}.prompt_addendum"
            value = str(form.get("value", "") or "")
            root = self.settings.root
            try:
                file = settings.target_file(root, "repo")
            except ValueError as exc:
                return str(exc)
            if not value.strip():
                return settings.save(
                    root,
                    "repo",
                    lambda layer: settings.remove_path(layer, path),
                    lambda: settings.unset_value(file, path),
                )
            return settings.save(
                root,
                "repo",
                lambda layer: settings.set_path(layer, path, value),
                lambda: settings.write_value(file, path, value),
            )

        error = await run_in_threadpool(job)
        self.flash(
            request, "error" if error else "ok", error or f"saved the additional prompt for {role}"
        )
        return self.back(to)

    def profiles_page(self, request: Request) -> Response:
        """`jev profile list` / `show`: the workspace profiles, the repo default and what
        each role gets from each one (Tier 0)."""

        name = request.query_params.get("name")

        def build(repo: views.Repo) -> dict[str, Any]:
            detail = None
            sources = app_profiles.profile_sources(repo.root)
            if name is not None:
                try:
                    detail = app_profiles.describe_profile(
                        repo.config, repo.state_dir, name, sources
                    )
                except ProfileError as exc:
                    raise NotFoundError(str(exc)) from exc
            return {
                "profiles": app_profiles.list_profiles(repo.config, repo.state_dir, sources),
                "detail": detail,
            }

        return self.page(request, "profiles.html", build)

    async def profiles_use(self, request: Request) -> Response:
        """`jev profile use` (Tier 1): make a profile the repo default, or clear it."""
        form = await checked_form(request)
        name = _text(form, "profile")

        def use() -> str:
            with views.open_repo(self.settings.root) as repo:
                if not name or name == "none":
                    app_profiles.set_default_profile(repo.config, repo.state_dir, None)
                    return "no default workspace profile: runs use none unless --profile is given"
                app_profiles.set_default_profile(repo.config, repo.state_dir, name)
                return f"default workspace profile: {name}"

        try:
            message = await run_in_threadpool(use)
        except (JevError, ConfigError, ProfileError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", message)
        return self.back("/profiles")

    # ---- pages/actions: design systems (the user-level library) ----------------------------

    def design_systems_page(self, request: Request) -> Response:
        """The design library: the saved systems, their sources and versions (Tier 0)."""
        return self.page(request, "design_systems.html", _design_systems_view)

    def design_system_page(self, request: Request) -> Response:
        """One library system: its tokens and system markdown, read-only (Tier 0)."""
        name = request.path_params["name"]
        try:
            validate_library_name(name)
        except ValueError:
            raise HTTPException(404, "no such design system") from None
        return self.page(
            request, "design_system.html", lambda repo: _design_system_view(repo, name)
        )

    async def design_systems_action(self, request: Request) -> Response:
        """Dispatch the library's POST actions: use, clone, export, delete."""
        action: str = request.path_params["action"]
        if action == "use":
            return await self.act(request, "/design-systems", _design_system_use)
        if action == "clone":
            return await self.act(request, "/design-systems", _design_system_clone)
        if action == "export":
            return await self.design_system_export(request)
        if action == "delete":
            return await self.design_system_delete(request)
        raise HTTPException(404, "no such action")

    async def design_system_export(self, request: Request) -> Response:
        """`jev design export` (Tier 0: read-only): the system's tokens as a download."""
        form = await checked_form(request)
        name = (_text(form, "name") or "").strip()
        fmt = (_text(form, "format") or "w3c").strip().lower()
        to = f"/design-systems/{name}" if name else "/design-systems"
        try:
            system_name = validate_library_name(name)
        except ValueError as exc:
            self.flash(request, "error", str(exc))
            return self.back("/design-systems")

        def build() -> tuple[str, str, str]:
            system = load_library_system(system_name)
            if system is None:
                raise JevError(f'no design system named "{system_name}" in the library')
            if fmt == "w3c":
                text = json.dumps(export_tokens_w3c(system.tokens), indent=2) + "\n"
                return f"{system_name}.tokens.json", "application/json", text
            if fmt == "tailwind":
                return (
                    f"{system_name}.tailwind.js",
                    "text/javascript",
                    export_tokens_tailwind(system.tokens),
                )
            if fmt == "css":
                return f"{system_name}.css", "text/css", export_tokens_css(system.tokens)
            raise JevError(f'unknown export format "{fmt}": choose w3c, tailwind or css')

        try:
            filename, media_type, text = await run_in_threadpool(build)
        except (JevError, ValueError) as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        return Response(
            text,
            media_type=f"{media_type}; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def design_system_delete(self, request: Request) -> Response:
        """Two steps (Tier 2), like `learning_delete`: the first POST shows a confirmation
        page; only `confirm=yes` deletes the library system."""
        form = await checked_form(request)
        name = (_text(form, "name") or "").strip()
        to = "/design-systems"
        try:
            system_name = validate_library_name(name)
        except ValueError as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        if form.get("confirm") != "yes":

            def describe() -> dict[str, Any]:
                system = load_library_system(system_name)
                if system is None:
                    raise JevError(f'no design system named "{system_name}" in the library')
                return {
                    "name": system_name,
                    "version": system.version,
                    "source": _meta_text(_library_meta(system_name), "source"),
                    "active": _repo_design_source(self.settings.root) == f"library:{system_name}",
                }

            try:
                info = await run_in_threadpool(describe)
            except (JevError, ValueError) as exc:
                self.flash(request, "error", str(exc))
                return self.back(to)
            return self.render(request, "confirm_design_system_delete.html", **info)

        def delete() -> None:
            if not delete_library_system(system_name):
                raise JevError(f'could not delete "{system_name}" from the library')

        try:
            await run_in_threadpool(delete)
        except (JevError, ValueError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", f'deleted "{system_name}" from the design library')
        return self.back(to)

    def fleet_fragment(self, request: Request) -> Response:
        """The fleet table alone, for live.js polling and `JevShell.mountPane("terminal",
        "/fleet/live", ms)` (see the hook documented in `templates/fleet.html`)."""
        return self.page(request, "_fleet.html", lambda repo: {"agents": views.fleet(repo)})

    def tools_page(self, request: Request) -> Response:
        def extra(repo: views.Repo) -> dict[str, Any]:
            listing, warning = views.model_listing(repo)
            return {
                "models_listing": listing,
                "models_warning": warning,
                "quality_rows": views.quality_checks(repo),
                "level_summaries": LEVEL_SUMMARY,
            }

        return self._settings_page(request, "tools", "tools.html", extra)

    def connections_page(self, request: Request) -> Response:
        """The full panel: cached per-server reachability, the dispatcher route and key
        health, then the connections settings editor (as before).

        Reads the audit tail only; rendering the page never opens an SSH connection."""

        def build(repo: views.Repo) -> dict[str, Any]:
            repo_raw, user_raw, _user_file = settings.read_layers(repo.root)
            _, _, secs, ents = settings.page_view(repo.config, repo_raw, user_raw, "connections")
            servers = _connection_servers(repo.config, repo.state_dir)
            state, label = _conn_aggregate(servers)
            return {
                "servers": servers,
                "agg_state": state,
                "agg_label": label,
                "route": _dispatcher_route(repo.config),
                "providers": views.cli_providers(repo.config),
                "codex_flow": self.codex_flow.status(),
                "env_vars": views.env_vars(repo.config),
                "key_names": views.env_var_names(repo.config),
                "browser_check": views.browser_check(repo),
                "browser_install": self.browser_installer.status(),
                # the settings editor, unchanged from the old connections page
                "sections": secs,
                "entities": ents,
                "user_only": settings.USER_ONLY,
                "settings": self.settings,
            }

        return self.page(request, "connections.html", build)

    def connections_live(self, request: Request) -> Response:
        """The sidebar slot's poll target: cached reachability as JSON.

        Key names and set/unset only, never values. The browser being offline is decided
        client-side in conn.js/app.js."""

        try:
            with views.open_repo(self.settings.root) as repo:
                servers = _connection_servers(repo.config, repo.state_dir)
        except (ConfigError, JevError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        state, label = _conn_aggregate(servers)
        flow = self.codex_flow.status()
        return JSONResponse(
            {
                "ok": True,
                "state": state,  # "ok" | "degraded"
                "label": label,
                "total": len(servers),
                "reachable": sum(s.state == "reachable" for s in servers),
                "unreachable": sum(s.state == "unreachable" for s in servers),
                "unknown": sum(s.state == "unknown" for s in servers),
                "providers": [
                    {
                        "provider": p.provider,
                        "auth": p.auth,
                        "label": p.label,
                        "installed": p.installed,
                    }
                    for p in views.cli_providers(repo.config)
                ],
                # The device flow's URL/code are single-use and already shown on
                # the page; only the state travels here so the card can update.
                "codex_flow": {"state": flow.state, "detail": flow.detail},
                "servers": [
                    {
                        "name": s.name,
                        "host": s.host,
                        "access": s.access,
                        "state": s.state,
                        "last_check": s.last_check.isoformat() if s.last_check else None,
                    }
                    for s in servers
                ],
            }
        )

    async def connections_refresh(self, request: Request) -> Response:
        """Explicit re-check (POST): one real `ssh HOST true` per server, then back to the
        panel. Each result lands in the audit log, which is what the panel reads."""

        def refresh(repo: views.Repo, form: FormData) -> str:
            return _refresh_servers(repo.config, repo.state_dir)

        return await self.act(request, "/connections", refresh)

    async def connections_key_add(self, request: Request) -> Response:
        """Save a key to ~/.jev/secrets.env (POST): it applies to this process at
        once, so the page shows "Set" without a restart. The value travels only in
        the POST body and is never rendered, logged, or flashed back."""

        def add(repo: views.Repo, form: FormData) -> str:
            from jev_agent.core.secrets import write_secret

            name = str(form.get("name", "")).strip()
            value = str(form.get("value", ""))
            write_secret(name, value)
            return f"{name} saved to the secrets file; it is in effect now."

        return await self.act(request, "/connections", add)

    async def connections_key_remove(self, request: Request) -> Response:
        """Delete a secrets-file key (POST). Keys from the real environment cannot
        reach this path: the template only offers removal for file-backed keys."""

        def remove(repo: views.Repo, form: FormData) -> str:
            from jev_agent.core.secrets import delete_secret

            name = str(form.get("name", "")).strip()
            delete_secret(name)
            return f"{name} removed from the secrets file."

        return await self.act(request, "/connections", remove)

    async def claude_oauth_token_save(self, request: Request) -> Response:
        """Save a pasted `claude setup-token` OAuth token to the secrets file (POST).

        The token travels only in the POST body and is never rendered, logged, or
        flashed back — same guarantees as the generic key form. It lands in this
        process's environment at once, so the provider card flips to "Subscription
        login (OAuth)" without a restart, and the next `claude` worker run sees it.
        """

        def save(repo: views.Repo, form: FormData) -> str:
            from jev_agent.core.secrets import write_secret

            value = str(form.get("value", "")).strip()
            check_claude_oauth_token(value)  # ValueError -> flashed, nothing saved
            write_secret(CLAUDE_OAUTH_TOKEN_ENV, value)
            return "Claude subscription token saved to the secrets file; it is in effect now."

        return await self.act(request, "/connections", save)

    async def codex_device_start(self, request: Request) -> Response:
        """Start the ChatGPT device sign-in (POST): spawns `codex login
        --device-auth` in the background. The card shows the URL + one-time code
        the CLI prints; the user finishes in their browser while the dashboard
        polls for completion. Nothing secret is stored by jev itself — the CLI
        keeps its own `~/.codex/auth.json`."""

        def start(repo: views.Repo, form: FormData) -> str:
            self.codex_flow.start(command=repo.config.workers.codex.command)
            return (
                "Codex sign-in started — open the link on the Codex card "
                "and enter the code in your browser."
            )

        return await self.act(request, "/connections", start)

    async def codex_device_cancel(self, request: Request) -> Response:
        """Cancel a pending ChatGPT device sign-in (POST)."""

        def cancel(repo: views.Repo, form: FormData) -> str:
            if self.codex_flow.cancel():
                return "Codex sign-in cancelled; no credentials were stored."
            return "No Codex sign-in was in progress."

        return await self.act(request, "/connections", cancel)

    async def browser_install(self, request: Request) -> Response:
        """Two steps (Tier 2: it downloads and runs an installer): the first POST names
        the exact command on a confirmation page; only `confirm=yes` starts it, once."""
        form = await checked_form(request)
        to = "/connections"
        status = self.browser_installer.status()
        if form.get("confirm") != "yes":
            if status.state == "running":
                self.flash(request, "error", "a browser install is already running")
                return self.back(to)
            return self.render(request, "confirm_browser_install.html", install=status)
        try:
            await run_in_threadpool(self.browser_installer.start)
        except BrowserInstallBusy as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", "browser install started; its progress shows below")
        return self.back(to)

    async def browser_install_stop(self, request: Request) -> Response:
        """Stop the running browser install (Tier 1)."""
        await checked_form(request)
        if self.browser_installer.stop():
            self.flash(request, "ok", "stopping the browser install")
        else:
            self.flash(request, "error", "no browser install is running")
        return self.back("/connections")

    # ---- agent browser preview (workstream F) ----------------------------------

    def _preview_bridge(self, request: Request) -> PreviewBridge:
        bridge: PreviewBridge = request.app.state.preview
        return bridge

    def _preview_view(self, session: PreviewSession | None) -> dict[str, Any] | None:
        if session is None:
            return None
        age = (
            int((datetime.now(UTC) - session.frame_at).total_seconds())
            if session.frame_at is not None
            else None
        )
        return {
            "session_id": session.session_id,
            "driver": session.driver.value,
            "label": session.label,
            "run_id": session.run_id,
            "page_url": session.page_url,
            "frame_age_s": age,
            "has_frame": session.frame is not None,
        }

    def preview_live(self, request: Request) -> Response:
        """The Preview pane's poll target for `JevShell.mountPane("preview", "/preview/live", ms)`.

        Renders `_preview_live.html`: the "No live preview" empty state when no
        agent browser session is live, otherwise the latest screenshot with the
        takeover controls (bound by `static/preview.js`).
        """
        bridge = self._preview_bridge(request)
        return self.render(
            request, "_preview_live.html", preview=self._preview_view(bridge.active())
        )

    def preview_frame(self, request: Request) -> Response:
        """The latest screenshot of the live browser session (PNG, never cached)."""
        session = self._preview_bridge(request).active()
        if session is None or session.frame is None:
            raise HTTPException(404, "no live preview")
        return Response(session.frame, media_type="image/png")

    def _audit_preview(self, session: PreviewSession, action: str) -> None:
        entry = AuditEntry(
            run_id=session.run_id,
            role="dashboard",
            server="preview",
            access="dashboard",
            action=action,
            command=session.session_id,
            allowed=True,
            reason=f"driver -> {session.driver.value}",
        )
        AuditLog(audit_path(self.settings.root / STATE_DIRNAME)).write(entry)

    async def preview_takeover(self, request: Request) -> Response:
        """Explicit user action: the user drives the agent's browser until hand-back."""
        await checked_form(request)
        bridge = self._preview_bridge(request)
        try:
            session = bridge.take_over()
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        self._audit_preview(session, "preview_takeover")
        return JSONResponse({"driver": session.driver.value, "session_id": session.session_id})

    async def preview_handback(self, request: Request) -> Response:
        """Explicit user action: the wheel goes back to the agent."""
        await checked_form(request)
        bridge = self._preview_bridge(request)
        try:
            session = bridge.hand_back()
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        self._audit_preview(session, "preview_handback")
        return JSONResponse({"driver": session.driver.value, "session_id": session.session_id})

    async def preview_input(self, request: Request) -> Response:
        """One user input event for the agent's browser (typed text, click, key).

        Accepted only while the user drives (409 otherwise); the agent side drains
        the queue through the agent seam's poll endpoint.
        """
        form = await checked_form(request)
        bridge = self._preview_bridge(request)
        try:
            event = PreviewInput.from_form(form)
            bridge.queue_input(event)
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return JSONResponse({"ok": True, "kind": event.kind})

    def _check_agent(self, request: Request) -> None:
        # The agent side is a local process, not a browser: no CSRF token, but the
        # same-origin check still keeps cross-site pages from driving these.
        if not same_origin(request):
            raise HTTPException(403, "cross-origin request refused")

    async def preview_agent_register(self, request: Request) -> Response:
        """Agent seam: register a browser session when the agent starts browsing."""
        self._check_agent(request)
        form = await request.form()
        run_id = str(form.get("run_id") or "")
        role = str(form.get("role") or "")
        if not run_id or not role:
            raise HTTPException(400, "run_id and role are required")
        session = self._preview_bridge(request).register(
            run_id=run_id, role=role, label=str(form.get("label") or "")
        )
        return JSONResponse({"session_id": session.session_id})

    async def preview_agent_frame(self, request: Request) -> Response:
        """Agent seam: push the latest screenshot (PNG body); also a heartbeat."""
        self._check_agent(request)
        session_id = request.query_params.get("session", "")
        page_url = request.query_params.get("url", "")
        # Read in chunks with a bail-out: a malicious local caller must not make the
        # server buffer an unbounded body before the size check runs.
        try:
            body = await read_capped_body(request)
            self._preview_bridge(request).post_frame(session_id, body, page_url=page_url)
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return JSONResponse({"ok": True})

    async def preview_agent_poll(self, request: Request) -> Response:
        """Agent seam: who drives now, and the queued user input (each delivered once).

        A POST (never GET) so a stray navigation cannot drain the queue.
        """
        self._check_agent(request)
        form = await request.form()
        try:
            driver, events = self._preview_bridge(request).poll(str(form.get("session_id") or ""))
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return JSONResponse(
            {"driver": driver.value, "inputs": [event.as_dict() for event in events]}
        )

    def _settings_page(
        self,
        request: Request,
        page: str,
        template: str = "settings.html",
        extra: Callable[[views.Repo], dict[str, Any]] | None = None,
    ) -> Response:
        def build(repo: views.Repo) -> dict[str, Any]:
            repo_raw, user_raw, user_file = settings.read_layers(repo.root)
            title, lede, secs, ents = settings.page_view(repo.config, repo_raw, user_raw, page)
            context = {
                "page": page,
                "page_title": title,
                "page_lede": lede,
                "sections": secs,
                "entities": ents,
                "beginner_fields": BEGINNER_FIELDS,
                "user_only": settings.USER_ONLY,
                "user_file": user_file,
                "summary": views.config_summary(repo),
                "env_vars": views.env_vars(repo.config),
                "checks": views.doctor_checks(repo.root),
                "settings": self.settings,
                "dashboard_started": _time(self.started_at),
                "dashboard_uptime": _age(self.started_at),
            }
            if extra is not None:
                context.update(extra(repo))
            return context

        return self.page(request, template, build)

    async def settings_set(self, request: Request) -> Response:
        return await self._settings_write(request, unset=False)

    async def settings_unset(self, request: Request) -> Response:
        return await self._settings_write(request, unset=True)

    async def ui_style_set(self, request: Request) -> Response:
        """The sidebar style picker: persist the chosen UI style (workstream UI-02)."""
        form = await checked_form(request)
        slug = _text(form, "style")
        if not slug:
            self.flash(request, "error", "no style given")
        else:
            try:
                ui_styles.write_ui_style(slug)
            except (ValueError, ConfigError) as exc:
                self.flash(request, "error", str(exc))
            else:
                self.flash(request, "ok", f"UI style: {ui_styles.label_for(slug)}")
        return self.back(_back_target(request))

    async def _settings_write(self, request: Request, *, unset: bool) -> Response:
        """One field of the settings editor: parse it, validate the config, then write."""
        form = await checked_form(request)
        path = _text(form, "path") or ""
        target = _text(form, "target") or "repo"
        kind = _text(form, "kind") or "str"
        if not path:
            self.flash(request, "error", "no setting given")
            return self.back("/settings")
        values = {key: value for key, value in form.items() if isinstance(value, str)}
        to = "/" + settings.page_of(path) + "#" + settings.anchor_of(path)

        def job() -> tuple[str | None, str]:
            root = self.settings.root
            try:
                file = settings.target_file(root, target)
            except ValueError as exc:
                return str(exc), ""

            def remove() -> str | None:
                return settings.save(
                    root,
                    target,
                    lambda layer: settings.remove_path(layer, path),
                    lambda: settings.unset_value(file, path),
                )

            if unset:
                return remove(), f"reset {path}"
            try:
                value = settings.parse_value(kind, values.get("value", ""), values)
            except ValueError as exc:
                return str(exc), ""
            if value is settings.UNSET:
                return remove(), f"reset {path}"
            return (
                settings.save(
                    root,
                    target,
                    lambda layer: settings.set_path(layer, path, value),
                    lambda: settings.write_value(file, path, value),
                ),
                f"saved {path}",
            )

        error, message = await run_in_threadpool(job)
        self.flash(request, "error" if error else "ok", error or message)
        return self.back(to)

    async def settings_entity(self, request: Request) -> Response:
        """Add or remove a table entry: an MCP server, a skill, a server, a workspace profile."""
        form = await checked_form(request)
        section = _text(form, "section") or ""
        action = _text(form, "action") or ""
        target = _text(form, "target") or "repo"
        name = _text(form, "name") or ""
        to = "/" + settings.page_of(section) + "#" + settings.anchor_of(section)
        if not section or not name:
            self.flash(request, "error", "a table and a name are required")
            return self.back("/settings")
        if (bad := settings.check_entity_name(name)) is not None:
            self.flash(request, "error", f"{name!r}: {bad}")
            return self.back(to)
        values = {key: value for key, value in form.items() if isinstance(value, str)}

        def job() -> str | None:
            root = self.settings.root
            try:
                file = settings.target_file(root, target)
            except ValueError as exc:
                return str(exc)
            if action == "remove":
                return settings.save(
                    root,
                    target,
                    lambda layer: settings.remove_path(layer, f"{section}.{name}"),
                    lambda: settings.remove_entity(file, section, name),
                )
            if action != "add":
                return "unknown action"
            keys = [key for key in (_text(form, "keys") or "").split(",") if key]
            kinds = (_text(form, "kinds") or "").split(",")
            entry: dict[str, Any] = {}
            for key, entry_kind in zip(keys, kinds, strict=False):
                try:
                    value = settings.parse_value(entry_kind, values.get(f"f_{key}", ""), values)
                except ValueError as exc:
                    return f"{key}: {exc}"
                if value is not settings.UNSET:
                    entry[key] = value
            return settings.save(
                root,
                target,
                lambda layer: settings.set_path(layer, f"{section}.{name}", entry),
                lambda: settings.add_entity(file, section, name, entry),
            )

        error = await run_in_threadpool(job)
        self.flash(request, "error" if error else "ok", error or f"saved {section}.{name}")
        return self.back(to)

    # ---- actions: runs ---------------------------------------------------------------------

    async def run_action(self, request: Request) -> Response:
        run_id: str = request.path_params["run_id"]
        action: str = request.path_params["action"]
        to = f"/runs/{run_id}"
        actions = ("approve", "resume", "cancel", "ship", "modify", "stop")
        if not views.valid_run_id(run_id) or action not in actions:
            raise HTTPException(404, "no such action")
        if action == "ship":
            return await self.ship(request, run_id)
        if action == "modify":
            return await self.modify(request, run_id)
        if action == "cancel":

            def cancel(repo: views.Repo, form: FormData) -> str:
                record = cancel_run(repo.store, run_id)
                return f"run {record.id} {record.state}; its branches and worktrees are kept"

            return await self.act(request, to, cancel)

        form = await checked_form(request)
        kinds: dict[str, RunAction] = {"approve": "approve", "stop": "stop"}
        kind = kinds.get(action, "resume")
        force = form.get("force") == "1"

        def check() -> str | None:
            with views.open_repo(self.settings.root) as repo:
                run = repo.store.get_run(run_id)
                gates = (RunState.AWAITING_APPROVAL, RunState.AWAITING_SHIP)
                if kind == "approve" and run.state not in gates:
                    return (
                        f"run {run_id} is {run.state}; only a run awaiting approval can be approved"
                    )
                if kind == "approve" and run.state is RunState.AWAITING_APPROVAL:
                    # The same budget gate as `jev approve`: refuse an over-budget
                    # estimate unless the "Approve anyway" form sent force=1.
                    _, _, block = approval_budget_block(
                        repo.store,
                        repo.config.budget,
                        repo.config.dispatcher.cost,
                        run,
                        force=force,
                    )
                    if block is not None:
                        return block
                if kind == "stop" and run.state is not RunState.AWAITING_SHIP:
                    return f"run {run_id} is {run.state}; only a run waiting to ship can be stopped"
                if kind == "resume" and not views.can_resume(run):
                    return f"run {run_id} is {run.state}; it cannot be resumed"
                risk = "" if kind != "resume" or force else views.resume_risk(repo, run)
            if risk:
                return (
                    f"not resumed: another jev process may be running this run ({risk}); "
                    "use Force resume if it is not"
                )
            return None

        try:
            problem = await run_in_threadpool(check)
        except (JevError, NotFoundError, ConfigError) as exc:
            problem = str(exc)
        if problem is not None:
            self.flash(request, "error", problem)
        elif self.launcher.start(run_id, kind):
            self.flash(request, "ok", f"{kind} started in the background; this page updates")
        else:
            self.flash(request, "error", f"run {run_id} is already being worked on here")
        return self.back(to)

    async def modify(self, request: Request, run_id: str) -> Response:
        """A run waiting to ship: add a task with the person's notes (`jev ship modify`), then
        execute it in the background like Resume."""
        form = await checked_form(request)
        to = f"/runs/{run_id}"
        notes = _text(form, "notes") or ""
        if self.launcher.busy(run_id):
            self.flash(request, "error", f"run {run_id} is already being worked on here")
            return self.back(to)
        try:
            has_workers = await run_in_threadpool(
                request_changes, self.settings.root, run_id, notes
            )
        except (JevError, ConfigError, NotFoundError, PipelineError, RunInUseError) as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        if not has_workers:
            self.flash(request, "ok", "changes recorded; set up a worker, then resume the run")
        elif self.launcher.start(run_id, "resume"):
            self.flash(request, "ok", "changes recorded; the run executes them in the background")
        else:
            self.flash(request, "error", f"run {run_id} is already being worked on here")
        return self.back(to)

    async def ship(self, request: Request, run_id: str) -> Response:
        """Two steps, like `jev ship`'s prompt: the first POST checks everything and shows the
        PR (remote, branch, base, title, body) on a confirmation page; only the confirmed POST
        starts `jev ship` in the background."""
        form = await checked_form(request)
        to = f"/runs/{run_id}"

        def plan() -> ShipPlan:
            with views.open_repo(self.settings.root) as repo:
                return prepare_ship(repo.root, repo.config, repo.store, run_id, gh=Gh(repo.root))

        try:
            ready = await run_in_threadpool(plan)
        except (JevError, ShipError, NotFoundError, ConfigError) as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        if form.get("confirm") != "yes":
            return self.render(
                request,
                "confirm_ship.html",
                plan=ready,
                body_html=render_markdown(ready.body),
            )
        if self.launcher.start(run_id, "ship"):
            self.flash(request, "ok", "ship started in the background; the PR link shows here")
        else:
            self.flash(request, "error", f"run {run_id} is already being worked on here")
        return self.back(to)

    # ---- actions: chat and runs --------------------------------------------------------------

    async def chat_delete(self, request: Request) -> Response:
        """Delete a saved chat: its transcript and brief. Runs it started are kept."""
        await checked_form(request)  # CSRF, like every other POST; the id comes from the path
        chat_id = request.path_params["chat_id"]
        if self.chat_runner.busy(chat_id):
            self.flash(request, "error", f"chat {chat_id} is replying right now; wait for it")
            return self.back("/chats")

        def remove() -> None:
            with views.open_repo(self.settings.root) as repo:
                chat.delete_chat(repo.root, chat_id)

        try:
            await run_in_threadpool(remove)
        except (ChatError, ConfigError) as exc:
            self.flash(request, "error", str(exc))
            return self.back("/chats")
        self.flash(request, "ok", f"deleted chat {chat_id}")
        return self.back("/chats")

    async def chats_new(self, request: Request) -> Response:
        """A new browser chat: the same `core.chat` session `jev chat` starts."""
        form = await checked_form(request)

        def create() -> str:
            with views.open_repo(self.settings.root) as repo:
                worker = _chat_backend_name(_text(form, "worker"))
                budget_text = _text(form, "budget_usd")
                try:
                    budget = DEFAULT_BUDGET_USD if not budget_text else float(budget_text)
                except ValueError:
                    raise ValueError("the budget must be a number of USD") from None
                if not math.isfinite(budget) or budget <= 0:
                    raise ValueError("the budget must be a finite number of USD above zero")
                return chat.create_chat(
                    repo.root,
                    repo,
                    worker,
                    _text(form, "model") or None,
                    _chat_learning(_text(form, "learning")),
                    _text(form, "profile") or None,
                    budget,
                    parse_design_system(_text(form, "design_system")),
                )

        try:
            chat_id = await run_in_threadpool(create)
        except (ChatError, ConfigError, LearningError, ValueError) as exc:
            self.flash(request, "error", str(exc))
            return self.back("/chats")
        self.flash(request, "ok", f"chat {chat_id} created; say what you want to build")
        return self.back(f"/chats/{chat_id}")

    async def chat_send(self, request: Request) -> Response:
        chat_id: str = request.path_params["chat_id"]
        form = await checked_form(request)
        to = f"/chats/{chat_id}"
        text = _text(form, "text") or ""
        if not text:
            self.flash(request, "error", "say something first")
            return self.back(to)

        def work() -> str:
            return chat.turn(self.settings.root, chat_id, text)

        if not self.chat_runner.start(chat_id, work):
            self.flash(request, "error", f"chat {chat_id} is already waiting for a reply")
            return self.back(to)
        self.flash(request, "ok", "sent; the reply appears here in a moment")
        return self.back(to)

    async def chat_brief(self, request: Request) -> Response:
        chat_id: str = request.path_params["chat_id"]
        await checked_form(request)
        to = f"/chats/{chat_id}"

        def work() -> str:
            return chat.brief(self.settings.root, chat_id)

        if not self.chat_runner.start(chat_id, work):
            self.flash(request, "error", f"chat {chat_id} is already waiting for a reply")
            return self.back(to)
        self.flash(request, "ok", "writing the brief...")
        return self.back(to)

    async def chat_start(self, request: Request) -> Response:
        """`/plan` and `/run` from a chat: a real run, created from the chat's brief."""
        chat_id: str = request.path_params["chat_id"]
        form = await checked_form(request)
        to = f"/chats/{chat_id}"
        plan_only = (_text(form, "action") or "run") == "plan"

        def start() -> str:
            run_id = chat.start_run_from_chat(self.settings.root, chat_id, plan_only=plan_only)
            if not self.launcher.start(run_id, "start"):
                raise PipelineError(f"run {run_id} is already being worked on here")
            return run_id

        try:
            run_id = await run_in_threadpool(start)
        except (
            ChatError,
            JevError,
            ConfigError,
            LearningError,
            NotFoundError,
            PipelineError,
            RunInUseError,
            ValueError,
        ) as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        verb = "plan-only run" if plan_only else "run"
        self.flash(request, "ok", f"{verb} {run_id} started from the chat's brief")
        return self.back(f"/runs/{run_id}")

    async def chat_queue(self, request: Request) -> Response:
        chat_id: str = request.path_params["chat_id"]
        form = await checked_form(request)
        to = f"/chats/{chat_id}"
        priority = _text(form, "priority") or "normal"

        def queue() -> int:
            return chat.queue_from_chat(self.settings.root, chat_id, priority)

        try:
            item_id = await run_in_threadpool(queue)
        except (
            ChatError,
            ConfigError,
            LearningError,
            NotFoundError,
            QueueError,
            ValueError,
        ) as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        self.flash(request, "ok", f"queued item {item_id} from the chat's brief")
        return self.back("/queue")

    async def chat_stop(self, request: Request) -> Response:
        """Stop the running turn: cancel the model call, like the lifespan shutdown does."""
        await checked_form(request)  # CSRF, like every other POST; the id comes from the path
        chat_id: str = request.path_params["chat_id"]
        to = f"/chats/{chat_id}"
        if self.chat_runner.stop(chat_id):
            self.flash(request, "ok", f"stopping the reply in chat {chat_id}")
        else:
            self.flash(request, "error", f"chat {chat_id} is not waiting for a reply")
        return self.back(to)

    async def chat_retry(self, request: Request) -> Response:
        """Ask the model again about the chat's last user message (the `chat_send` path)."""
        chat_id: str = request.path_params["chat_id"]
        await checked_form(request)
        to = f"/chats/{chat_id}"

        def last_user_text() -> str | None:
            with views.open_repo(self.settings.root) as repo:
                _, entries = views.chat_transcript(repo, chat_id)
                for entry in reversed(entries):
                    if entry.get("kind") == "user":
                        text = entry.get("text")
                        if isinstance(text, str) and text.strip():
                            return text
            return None

        try:
            text = await run_in_threadpool(last_user_text)
        except (ChatError, ConfigError, JevError) as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        if text is None:
            self.flash(request, "error", "nothing to retry yet: no user message in this chat")
            return self.back(to)

        def work() -> str:
            return chat.turn(self.settings.root, chat_id, text)

        if not self.chat_runner.start(chat_id, work):
            self.flash(request, "error", f"chat {chat_id} is already waiting for a reply")
            return self.back(to)
        self.flash(request, "ok", "retrying the last message; the reply appears here in a moment")
        return self.back(to)

    def chat_work(self, request: Request) -> Response:
        """The chat page's Work pane poll target: the newest run's tab availability as JSON.

        Cheap by design (at most three store queries: the run, its quality reports, its
        attempts); the per-tab content loads separately through `chat_work_tab`.
        """
        chat_id: str = request.path_params["chat_id"]
        try:
            with views.open_repo(self.settings.root) as repo:
                payload = self._work_payload(repo, chat_id, self._preview_bridge(request))
        except (NotFoundError, ChatError) as exc:
            return JSONResponse({"run_id": None, "error": str(exc)}, status_code=404)
        except (ConfigError, JevError) as exc:
            return JSONResponse({"run_id": None, "error": str(exc)}, status_code=500)
        return JSONResponse(payload)

    def _work_payload(
        self, repo: views.Repo, chat_id: str, bridge: PreviewBridge
    ) -> dict[str, Any]:
        meta, _ = views.chat_transcript(repo, chat_id)
        if meta is None:
            raise NotFoundError(f"no chat {chat_id}")
        tabs = {name: {"available": False, "active": False} for name in _WORK_TABS}
        run_id = meta.runs[-1].get("run_id") if meta.runs else None
        if not run_id:
            return {"run_id": None, "tabs": tabs}
        try:
            run = repo.store.get_run(run_id)
        except NotFoundError:
            return {"run_id": None, "tabs": tabs}
        reports = repo.store.list_quality_reports(run_id)
        attempts = repo.store.list_attempts(run_id)
        busy = run.state not in STOPPED and self.launcher.busy(run_id)
        session = bridge.session_for_run(run_id)
        has_preview = session is not None and bool(session.page_url)
        tabs["plan"] = {
            "available": run.plan is not None,
            "active": busy and run.state in _PLAN_ACTIVE_STATES,
        }
        tabs["diff"] = {
            "available": run.state
            in (
                RunState.INTEGRATING,
                RunState.SHIPPING,
                RunState.AWAITING_SHIP,
                RunState.DONE,
            ),
            "active": busy
            and run.state in (RunState.EXECUTING, RunState.INTEGRATING, RunState.SHIPPING),
        }
        tabs["tests"] = {
            "available": bool(reports),
            "active": busy
            and not reports
            and run.state in (RunState.EXECUTING, RunState.INTEGRATING),
        }
        tabs["preview"] = {"available": has_preview, "active": has_preview}
        tabs["terminal"] = {
            "available": busy or _newest_attempt_log(repo, run_id, attempts) is not None,
            "active": busy,
        }
        return {"run_id": run_id, "tabs": tabs}

    def chat_work_tab(self, request: Request) -> Response:
        """One Work pane tab as an HTML fragment (`_work_tab.html`); 404 for unknown tabs."""
        chat_id: str = request.path_params["chat_id"]
        tab: str = request.path_params["tab"]
        if tab not in _WORK_TABS:
            raise HTTPException(404, f"no work tab {tab!r}")
        bridge = self._preview_bridge(request)

        def build(repo: views.Repo) -> dict[str, Any]:
            meta, _ = views.chat_transcript(repo, chat_id)
            if meta is None:
                raise NotFoundError(f"no chat {chat_id}")
            run_id = meta.runs[-1].get("run_id") if meta.runs else None
            if not run_id:
                raise NotFoundError(f"chat {chat_id} has no runs yet")
            return self._work_tab_context(repo, run_id, tab, bridge)

        return self.page(request, "_work_tab.html", build)

    def _work_tab_context(
        self, repo: views.Repo, run_id: str, tab: str, bridge: PreviewBridge
    ) -> dict[str, Any]:
        context: dict[str, Any] = {"tab": tab, "run_id": run_id}
        if tab == "plan":
            context["run"] = repo.store.get_run(run_id)
        elif tab == "diff":
            context["diff"] = _run_diff(repo.root, run_id)
        elif tab == "tests":
            run = repo.store.get_run(run_id)
            reports = repo.store.list_quality_reports(run_id)
            context["review_html"] = (
                render_markdown(review_text(repo.root, run, reports)) if reports else None
            )
        elif tab == "preview":
            context["preview"] = self._preview_view(bridge.session_for_run(run_id))
        elif tab == "terminal":  # tab is one of _WORK_TABS here
            log = _newest_attempt_log(repo, run_id, repo.store.list_attempts(run_id))
            context["terminal_log"] = _tail_lines(log) if log is not None else None
        return context

    async def runs_new(self, request: Request) -> Response:
        """`jev run` from the web: create the run, advance it in the background."""
        form = await checked_form(request)
        idea = _text(form, "idea") or ""
        if not idea:
            self.flash(request, "error", "an idea is required")
            return self.back("/")
        learning = _text(form, "learning")
        plan_only = form.get("plan_only") == "on"
        quality = _text(form, "quality") or None
        profile = _text(form, "profile") or None
        ship_choice = _text(form, "ship_mode")
        try:
            options = RunOptions(
                plan_only=plan_only,
                learning=None if learning in (None, "", "none") else learning,
                no_learning=learning == "none",
                quality=quality,
                profile=profile,
            )
        except JevError as exc:
            self.flash(request, "error", str(exc))
            return self.back("/")

        def start() -> str:
            with views.open_repo(self.settings.root) as repo:
                mode = require_ship_enabled(repo.config, cast(ShipMode | None, ship_choice or None))
            run_id = prepare_run(
                self.settings.root,
                idea,
                plan_only=options.plan_only,
                learning=options.learning,
                no_learning=options.no_learning,
                quality=options.quality,
                profile=options.profile,
                ship_mode=mode,
            )
            if not self.launcher.start(run_id, "start"):
                raise PipelineError(f"run {run_id} is already being worked on here")
            return run_id

        try:
            run_id = await run_in_threadpool(start)
        except (
            JevError,
            ConfigError,
            LearningError,
            PipelineError,
            QueueError,
            RunInUseError,
            ValueError,
        ) as exc:
            self.flash(request, "error", str(exc))
            return self.back("/")
        self.flash(request, "ok", f"run {run_id} started; it shows on the board")
        return self.back(f"/runs/{run_id}")

    # ---- actions: queue --------------------------------------------------------------------

    async def queue_add(self, request: Request) -> Response:
        def add(repo: views.Repo, form: FormData) -> str:
            learning_choice = _text(form, "learning")
            options = QueueOptions(
                priority=_text(form, "priority") or "normal",
                plan_only=form.get("plan_only") == "on",
                learning=learning_choice if learning_choice not in (None, "", "none") else None,
                no_learning=learning_choice == "none",
                profile=_text(form, "profile") or None,
                quality=_text(form, "quality") or None,
                ship_mode=require_ship_enabled(
                    repo.config, cast(ShipMode | None, _text(form, "ship_mode") or None)
                ),
            )
            item = add_idea(
                repo.store,
                repo.learning,
                _text(form, "idea") or "",
                options.priority,
                plan_only=options.plan_only,
                learning_profile=options.learning,
                no_learning=options.no_learning,
                profile=options.profile,
                quality=options.quality,
                ship_mode=options.ship_mode,
            )
            return f"queued item {item.id} ({item.priority})"

        return await self.act(request, "/queue", add)

    async def queue_move(self, request: Request) -> Response:
        item_id = _int(request.path_params["item_id"])

        def move(repo: views.Repo, form: FormData) -> str:
            moves: dict[str | None, Move] = {"up": "up", "down": "down", "top": "top"}
            direction = moves.get(_text(form, "direction"))
            if direction is None:
                raise QueueError("move needs a direction: up, down or top")
            before = repo.store.get_queue_item(item_id)
            after = move_item(repo.store, item_id, direction)
            note = (
                f" (priority {before.priority} -> {after.priority})"
                if after.priority != before.priority
                else ""
            )
            return f"moved item {item_id} {direction}{note}"

        return await self.act(request, "/queue", move)

    async def queue_remove(self, request: Request) -> Response:
        item_id = _int(request.path_params["item_id"])

        def remove(repo: views.Repo, form: FormData) -> str:
            remove_item(repo.store, item_id)
            return f"removed item {item_id}"

        return await self.act(request, "/queue", remove)

    async def queue_recurring_pause(self, request: Request) -> Response:
        task_id = _int(request.path_params["id"])

        def pause(repo: views.Repo, form: FormData) -> str:
            repo.store.set_recurring_enabled(task_id, False)
            return f"paused recurring task {task_id}"

        return await self.act(request, "/queue", pause)

    async def queue_recurring_resume(self, request: Request) -> Response:
        task_id = _int(request.path_params["id"])

        def resume(repo: views.Repo, form: FormData) -> str:
            repo.store.set_recurring_enabled(task_id, True)
            return f"resumed recurring task {task_id}"

        return await self.act(request, "/queue", resume)

    async def queue_recurring_remove(self, request: Request) -> Response:
        task_id = _int(request.path_params["id"])

        def remove(repo: views.Repo, form: FormData) -> str:
            repo.store.remove_recurring_task(task_id)
            return f"removed recurring task {task_id}"

        return await self.act(request, "/queue", remove)

    async def scheduler_start(self, request: Request) -> Response:
        """Two steps (Tier 2: a scheduler spends real money): the first POST names the
        spend implication on a confirmation page; only `confirm=yes` starts the
        dashboard's own scheduler, which dies with the dashboard."""
        form = await checked_form(request)
        to = "/queue"
        if form.get("confirm") != "yes":

            def preview() -> dict[str, Any]:
                with views.open_repo(self.settings.root) as repo:
                    return {
                        "queued": queue_counts(repo.store).get("queued", 0),
                        "spent_today": spent_today(repo.store, datetime.now(UTC)),
                    }

            try:
                info = await run_in_threadpool(preview)
            except (JevError, ConfigError) as exc:
                self.flash(request, "error", str(exc))
                return self.back(to)
            if self.scheduler_runner.status().owner is not None:
                self.flash(request, "error", "a scheduler is already running")
                return self.back(to)
            return self.render(request, "confirm_scheduler.html", **info)

        try:
            await run_in_threadpool(self.scheduler_runner.start)
        except (SchedulerLocked, PipelineError, JevError, ConfigError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", "scheduler started; it stops when the dashboard stops")
        return self.back(to)

    async def scheduler_stop(self, request: Request) -> Response:
        """Stop the dashboard's scheduler after the current item (Tier 1: graceful, and
        only ever our own scheduler — a terminal's keeps running)."""
        await checked_form(request)
        if self.scheduler_runner.stop():
            self.flash(
                request, "ok", "scheduler stopping: it finishes the current item, then stops"
            )
        else:
            self.flash(request, "error", "the dashboard's scheduler is not running")
        return self.back("/queue")

    async def queue_clear(self, request: Request) -> Response:
        """Two steps (Tier 2), like `learning_flush`: the first POST shows how many queued
        ideas would go; only `confirm=yes` deletes them (items with a run are kept)."""
        form = await checked_form(request)
        to = "/queue"
        if form.get("confirm") != "yes":

            def count() -> int:
                with views.open_repo(self.settings.root) as repo:
                    return queue_counts(repo.store).get("queued", 0)

            try:
                queued = await run_in_threadpool(count)
            except (JevError, ConfigError) as exc:
                self.flash(request, "error", str(exc))
                return self.back(to)
            if queued == 0:
                self.flash(request, "ok", "the queue is already empty")
                return self.back(to)
            return self.render(request, "confirm_queue_clear.html", queued=queued)

        def clear() -> int:
            with views.open_repo(self.settings.root) as repo:
                return repo.store.clear_queued()

        try:
            deleted = await run_in_threadpool(clear)
        except (JevError, ConfigError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", f"removed {deleted} queued item(s)")
        return self.back(to)

    # ---- actions: learning -----------------------------------------------------------------

    async def learning_action(self, request: Request) -> Response:
        action: str = request.path_params["action"]
        if action == "flush":
            return await self.learning_flush(request)
        if action == "delete":
            return await self.learning_delete(request)
        handlers: dict[str, Callable[[views.Repo, FormData], str]] = {
            "create": _learning_create,
            "use": _learning_use,
            "enable": _learning_enable,
            "disable": _learning_disable,
            "forget": _learning_forget,
        }
        handler = handlers.get(action)
        if handler is None:
            raise HTTPException(404, "no such action")
        return await self.act(request, "/learning", handler)

    async def learning_flush(self, request: Request) -> Response:
        """Two steps, like `jev learning flush`'s prompt: without `confirm=yes` the form shows
        a confirmation page; only the confirmed POST deletes."""
        form = await checked_form(request)
        name = _text(form, "profile") or ""
        if form.get("confirm") != "yes":

            def count() -> int:
                with views.open_repo(self.settings.root) as repo:
                    return len(repo.learning.lessons(name))

            try:
                lessons = await run_in_threadpool(count)
            except (JevError, LearningError) as exc:
                self.flash(request, "error", str(exc))
                return self.back("/learning")
            return self.render(request, "confirm_flush.html", profile=name, lessons=lessons)

        def flush() -> int:
            with views.open_repo(self.settings.root) as repo:
                return flush_lessons(repo.learning, name)

        try:
            deleted = await run_in_threadpool(flush)
        except (JevError, LearningError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", f"deleted {deleted} lesson(s) from {name}")
        return self.back(f"/learning?profile={name}")

    async def learning_delete(self, request: Request) -> Response:
        """Two steps (Tier 2), like `learning_flush`: the first POST names the profile and
        its lesson count; only `confirm=yes` deletes the profile and all its lessons."""
        form = await checked_form(request)
        name = _text(form, "profile") or ""
        if form.get("confirm") != "yes":

            def describe() -> dict[str, Any]:
                with views.open_repo(self.settings.root) as repo:
                    repo.learning.db_for(name)  # unknown profile: error before asking
                    status = learning_status(repo.store, repo.config)
                    return {
                        "lessons": len(repo.learning.lessons(name)),
                        "active": status.profile == name,
                    }

            try:
                info = await run_in_threadpool(describe)
            except (JevError, LearningError) as exc:
                self.flash(request, "error", str(exc))
                return self.back("/learning")
            return self.render(request, "confirm_learning_delete.html", profile=name, **info)

        def delete() -> None:
            with views.open_repo(self.settings.root) as repo:
                learning_delete_profile(repo.store, repo.learning, name)

        try:
            await run_in_threadpool(delete)
        except (JevError, LearningError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", f"deleted profile {name}")
        return self.back("/learning")

    async def laya_apply(self, request: Request) -> Response:
        """Apply the selected `laya tune` suggestions to a profile (Tier 1: re-running
        the tune or `laya reset` undoes it). Refuses when jev.toml pins the weights."""
        form = await checked_form(request)
        name = _text(form, "profile") or ""
        accepted = [key for key in form if key.startswith("accept_")]

        def apply() -> str:
            with views.open_repo(self.settings.root) as repo:
                view = views.laya_view(repo, name)
                if view.profile is None:
                    raise LearningError(f"unknown profile {name}")
                if view.configured:
                    raise LearningError(
                        "jev.toml sets [dispatcher.laya] weights, which win over a "
                        "profile's; edit them there instead"
                    )
                suggestion = view.suggestion
                if suggestion is None or suggestion.weights is None:
                    raise LearningError(f"nothing to apply: {view.reason}")
                criteria = [key[7:] for key in accepted if key[7:] in suggestion.weights]
                if not criteria:
                    raise LearningError("select at least one suggestion")
                merged = views.apply_laya_selection(
                    suggestion.current, suggestion.weights, criteria
                )
                repo.learning.set_laya_weights(view.profile, merged)
                return f"applied {len(criteria)} weight(s) on profile {view.profile}"

        try:
            message = await run_in_threadpool(apply)
        except (JevError, LearningError, ConfigError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", message)
        return self.back(f"/learning?profile={name}")

    async def laya_reset(self, request: Request) -> Response:
        """Remove a profile's tuned weights (Tier 1; the button confirms first)."""
        form = await checked_form(request)
        name = _text(form, "profile") or ""

        def reset() -> str:
            with views.open_repo(self.settings.root) as repo:
                repo.learning.db_for(name)  # unknown profile: error first
                repo.learning.set_laya_weights(name, None)
                return f"profile {name} uses the configured weights again"

        try:
            message = await run_in_threadpool(reset)
        except (JevError, LearningError, ConfigError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", message)
        return self.back(f"/learning?profile={name}")


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


def _chat_backend_name(value: str | None) -> BackendName:
    """A form's worker value, or a `ChatError` (the form lists both backends)."""
    if value in BACKENDS:
        return value
    raise ChatError("pick a chat backend: api or claude_code")


def _chat_learning(value: str | None) -> ChatLearning:
    """The New chat form's learning choice: empty is the repo default, "none" is off."""
    if not value:
        return ChatLearning()
    if value == "none":
        return ChatLearning(off=True)
    return ChatLearning(profile=value)


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise HTTPException(404, "no such item") from None


# ---- chat work pane ----------------------------------------------------------------------

_WORK_TABS: tuple[str, ...] = ("plan", "diff", "tests", "preview", "terminal")

# Run states whose stream produces the plan tab while the dashboard drives the run.
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


# ---- chat context: approvals and step timeline ---------------------------------------------

_STEP_BLOCK_RUNS = 10  # newest runs considered, so one chat page stays cheap
_STEP_BLOCK_ENTRIES = 20

# Shown when the transcript is empty; the composer offers them as starting points.
_CHAT_SUGGESTIONS: tuple[str, ...] = (
    "Add pagination to the users API",
    "Fix the flaky date-parsing test",
    "Explain what run 20261014-3f1a changed",
    "Review the open pull requests",
)


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


def _approval_cards(repo: views.Repo, meta: ChatMeta) -> list[dict[str, Any]]:
    """The chat's runs waiting on a person (plan or ship approval), for the approval rail."""
    cards: list[dict[str, Any]] = []
    for ref in meta.runs:
        run_id = ref.get("run_id")
        if not run_id:
            continue
        try:
            run = repo.store.get_run(run_id)
        except NotFoundError:
            continue
        if run.state not in (RunState.AWAITING_APPROVAL, RunState.AWAITING_SHIP):
            continue
        cards.append(
            {
                "run_id": run_id,
                "kind": ref.get("kind", ""),
                "plan_summary": run.plan.summary[:300] if run.plan else "",
                "cost_estimate": f"${run.spend_usd:.2f}" if run.spend_usd > 0 else "unknown",
                "diff_stat": _format_diff_stat(
                    _safe_diff_stat(repo.root, start_ref(run_id), result_branch(run_id))
                ),
            }
        )
    return cards


def _decision_verdict(record: DecisionRecord) -> str:
    """The decision's outcome in a few words (the step timeline's title tail)."""
    answers = record.decision.answers
    point = record.decision.point
    if point == "D3":
        return answers.get("verdict", "")
    if point in ("D5", "D6"):
        return answers.get("action", "")
    return ", ".join(f"{name}={value}" for name, value in answers.items())[:120]


def _decision_block(record: DecisionRecord) -> dict[str, Any]:
    """One D1-D8 decision card as a collapsed step entry."""
    decision = record.decision
    worker = decision.answers.get("worker", decision.dispatcher)
    who = f"{worker}/{decision.model}" if decision.model else worker
    scope = record.task_id or "run"
    verdict = _decision_verdict(record)
    lines = [format_answers(decision.answers, decision.details)]
    if record.fallback_reason:
        lines.append(f"rules fallback: {record.fallback_reason}")
    if decision.cost_usd is not None:
        lines.append(f"cost {_usd(decision.cost_usd)}")
    return {
        "title": f"{scope} · {who}" + (f" — {verdict}" if verdict else ""),
        "summary": f"{decision.point} decided by {decision.dispatcher}",
        "detail_html": Markup("<br>").join(escape(line) for line in lines),
    }


def _task_block(task: TaskRecord) -> dict[str, Any]:
    """A finished task as a step entry (a run milestone)."""
    return {
        "title": f"{task.id} · {task.card.title} — done",
        "summary": f"{task.attempts} attempt(s), {_usd(task.spend_usd)}",
        "detail_html": Markup(escape(task.note)) if task.note else Markup(""),
    }


def _shipped_block(pr: ShippedPrRecord) -> dict[str, Any]:
    """A shipped PR as a step entry (a run milestone)."""
    return {
        "title": f"shipped: {pr.title or pr.url or pr.branch}",
        "summary": f"{pr.repo} · {pr.base} ← {pr.branch}",
        "detail_html": Markup(""),
    }


def _step_blocks(repo: views.Repo, meta: ChatMeta) -> list[dict[str, Any]]:
    """Collapsed step entries for the chat's runs: the D1-D8 decision cards plus run
    milestones (finished tasks, shipped PRs). Newest last, capped at 20 entries."""
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for ref in meta.runs[-_STEP_BLOCK_RUNS:]:
        run_id = ref.get("run_id")
        if not run_id:
            continue
        try:
            run = repo.store.get_run(run_id)
        except NotFoundError:
            continue
        for record in repo.store.list_decisions(run_id):
            dated.append((record.created_at, _decision_block(record)))
        for task in repo.store.list_tasks(run_id):
            if task.state is TaskState.DONE:
                dated.append((run.updated_at, _task_block(task)))
        for pr in repo.store.list_shipped_prs(run_id):
            dated.append((pr.created_at, _shipped_block(pr)))
    dated.sort(key=lambda item: item[0])
    return [block for _, block in dated[-_STEP_BLOCK_ENTRIES:]]


def _learning_create(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "name") or ""
    profile = learning_create(repo.learning, name, shared=form.get("shared") == "on")
    return f"created profile {profile.name} ({profile.scope})"


def _learning_use(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or ""
    use_learning_profile(repo.store, repo.learning, name)
    return f"active profile: {name}"


def _learning_enable(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or None
    active = learning_enable(repo.store, repo.config, repo.learning, name)
    if name is None:
        return f"learning is on for this repo, with profile {active}"
    return f"profile {name} is enabled"


def _learning_disable(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or None
    learning_disable(repo.store, repo.learning, name)
    if name is None:
        return "learning is off for this repo; lessons are kept"
    return f"profile {name} is disabled"


def _learning_forget(repo: views.Repo, form: FormData) -> str:
    lesson = learning_forget(repo.learning, _text(form, "lesson") or "")
    return f"retired {lesson.id} from {lesson.profile}"


# ---- design systems (the user-level library, ~/.jev/design-systems/) ------------------------


def _library_meta(name: str) -> dict[str, object]:
    """A library system's meta.json extras; {} when it cannot be read.

    Corrupt files are the library's own concern (`load_library_system` warns); the
    dashboard stays readable even when one system is broken, like `design list`.
    """
    try:
        raw = json.loads((library_system_path(name) / META_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _meta_text(meta: dict[str, object], key: str) -> str:
    value = meta.get(key)
    return str(value) if isinstance(value, str) and value else "-"


def _repo_design_source(root: Path) -> str | None:
    """The repo's installed design system's `source` (`library:<name>` after `use`)."""
    try:
        raw = json.loads((design_dir(root) / META_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    source = raw.get("source") if isinstance(raw, dict) else None
    return str(source) if isinstance(source, str) else None


def _design_systems_view(repo: views.Repo) -> dict[str, Any]:
    """The library's systems for the list page, with the repo's active one marked."""
    active = _repo_design_source(repo.root)
    systems = []
    for name in list_library():
        meta = _library_meta(name)
        systems.append(
            {
                "name": name,
                "source": _meta_text(meta, "source"),
                "app_type": _meta_text(meta, "app_type"),
                "stack": _meta_text(meta, "stack"),
                "version": meta.get("version", "-"),
                "updated": meta.get("updated_at", "-"),
                "active": active == f"library:{name}",
            }
        )
    return {"systems": systems}


def _design_system_view(repo: views.Repo, name: str) -> dict[str, Any]:
    """One library system for the detail page: tokens and the system markdown."""
    system = load_library_system(name)
    if system is None:
        raise NotFoundError(f'no design system named "{name}" in the library')
    meta = _library_meta(name)
    tokens = system.tokens if isinstance(system.tokens, dict) else {}
    sections = (
        ("Colors", tokens.get("colors")),
        ("Type scale", tokens.get("type_scale")),
        ("Spacing", tokens.get("spacing")),
        ("Radii", tokens.get("radii")),
    )
    token_sections = [
        {
            "title": title,
            "items": sorted(section.items()) if isinstance(section, dict) else [],
        }
        for title, section in sections
    ]
    return {
        "name": name,
        "source": _meta_text(meta, "source"),
        "app_type": _meta_text(meta, "app_type"),
        "stack": _meta_text(meta, "stack"),
        "version": system.version,
        "updated": system.updated_at,
        "token_sections": token_sections,
        "markdown_html": render_markdown(system.markdown),
        "active": _repo_design_source(repo.root) == f"library:{name}",
    }


def _design_system_extras(name: str) -> dict[str, object]:
    """The library meta extras a `use`/`clone` carries over (mirrors `design_cmds`)."""
    meta = _library_meta(name)
    return {key: meta[key] for key in ("app_type", "stack") if isinstance(meta.get(key), str)}


def _design_system_use(repo: views.Repo, form: FormData) -> str:
    """`jev design use <name>` (Tier 1): install the library system into the repo."""
    name = validate_library_name((_text(form, "name") or "").strip())
    system = load_library_system(name)
    if system is None:
        raise JevError(f'no design system named "{name}" in the library')
    version = save_design_system(
        repo.root,
        system.markdown,
        dict(system.tokens),
        meta_extra={"source": f"library:{name}", **_design_system_extras(name)},
    )
    return f'installed "{name}" as the repo design system v{version}'


def _design_system_clone(repo: views.Repo, form: FormData) -> str:
    """`jev design clone <name> <new_name>` (Tier 1): copy a library system."""
    name = validate_library_name((_text(form, "name") or "").strip())
    new_name = validate_library_name((_text(form, "new_name") or "").strip())
    system = load_library_system(name)
    if system is None:
        raise JevError(f'no design system named "{name}" in the library')
    if library_system_path(new_name).exists():
        raise JevError(f'a design system named "{new_name}" already exists in the library')
    version = save_library_system(
        new_name,
        system.markdown,
        dict(system.tokens),
        meta_extra={
            "name": new_name,
            "source": f"clone:{name}",
            **_design_system_extras(name),
        },
    )
    return f'cloned "{name}" to "{new_name}" (library v{version})'


# ---- command palette (issue #88) ------------------------------------------------------------


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
        ("Learning: profiles, lessons, Laya tuning", "/learning#laya"),
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


JOB_SHUTDOWN_TIMEOUT_S = 30.0  # per job, for blocking steps that ignore cancellation


def create_app(
    settings: DashboardSettings,
    *,
    executor: Executor = execute_run,
) -> Starlette:
    """Build the app. Raises `DashboardConfigError` for a non-loopback host without a token.
    `executor` runs approve/resume jobs (tests pass a fake one)."""
    settings.check()
    from jev_agent.core.secrets import load_secrets

    load_secrets()  # ~/.jev/secrets.env: real environment always wins
    dash = Dashboard(settings, RunLauncher(settings.root, executor))
    routes = [
        Route("/", dash.board_page),
        Route("/fragments/board", dash.board_fragment),
        Route("/runs/new", dash.runs_new, methods=["POST"]),
        Route("/runs/{run_id}", dash.run_page),
        Route("/runs/{run_id}/live", dash.run_fragment),
        Route("/runs/{run_id}/files/{path:path}", dash.run_file),
        Route("/runs/{run_id}/{action}", dash.run_action, methods=["POST"]),
        Route("/queue", dash.queue_page),
        Route("/queue/add", dash.queue_add, methods=["POST"]),
        Route("/queue/{item_id}/move", dash.queue_move, methods=["POST"]),
        Route("/queue/{item_id}/remove", dash.queue_remove, methods=["POST"]),
        Route("/queue/recurring/{id}/pause", dash.queue_recurring_pause, methods=["POST"]),
        Route("/queue/recurring/{id}/resume", dash.queue_recurring_resume, methods=["POST"]),
        Route("/queue/recurring/{id}/remove", dash.queue_recurring_remove, methods=["POST"]),
        Route("/queue/scheduler/start", dash.scheduler_start, methods=["POST"]),
        Route("/queue/scheduler/stop", dash.scheduler_stop, methods=["POST"]),
        Route("/queue/clear", dash.queue_clear, methods=["POST"]),
        Route("/artifacts", dash.artifacts_page),
        Route("/learning", dash.learning_page),
        Route("/learning/laya/apply", dash.laya_apply, methods=["POST"]),
        Route("/learning/laya/reset", dash.laya_reset, methods=["POST"]),
        Route("/learning/{action}", dash.learning_action, methods=["POST"]),
        Route("/chats", dash.chats_page),
        Route("/chats/new", dash.chats_new, methods=["POST"]),
        Route("/chats/{chat_id}", dash.chat_page),
        Route("/chats/{chat_id}/live", dash.chat_fragment),
        Route("/chats/{chat_id}/send", dash.chat_send, methods=["POST"]),
        Route("/chats/{chat_id}/brief", dash.chat_brief, methods=["POST"]),
        Route("/chats/{chat_id}/start", dash.chat_start, methods=["POST"]),
        Route("/chats/{chat_id}/queue", dash.chat_queue, methods=["POST"]),
        Route("/chats/{chat_id}/delete", dash.chat_delete, methods=["POST"]),
        Route("/chats/{chat_id}/stop", dash.chat_stop, methods=["POST"]),
        Route("/chats/{chat_id}/retry", dash.chat_retry, methods=["POST"]),
        Route("/chats/{chat_id}/work", dash.chat_work),
        Route("/chats/{chat_id}/work/{tab}", dash.chat_work_tab),
        Route("/spend", dash.spend_page),
        Route("/kpis", dash.kpis_page),
        Route("/kpis/sync", dash.kpis_sync, methods=["POST"]),
        Route("/agents", dash.agents_page),
        Route("/fleet", dash.fleet_page),
        Route("/fleet/live", dash.fleet_fragment),
        Route("/fleet/addendum", dash.fleet_addendum, methods=["POST"]),
        Route("/profiles", dash.profiles_page),
        Route("/profiles/use", dash.profiles_use, methods=["POST"]),
        Route("/design-systems", dash.design_systems_page),
        Route("/design-systems/{name}", dash.design_system_page),
        Route("/design-systems/{action}", dash.design_systems_action, methods=["POST"]),
        Route("/tools", dash.tools_page),
        Route("/connections", dash.connections_page),
        Route("/connections/live", dash.connections_live),
        Route("/connections/refresh", dash.connections_refresh, methods=["POST"]),
        Route("/connections/keys/add", dash.connections_key_add, methods=["POST"]),
        Route("/connections/keys/remove", dash.connections_key_remove, methods=["POST"]),
        Route(
            "/connections/providers/claude/oauth-token",
            dash.claude_oauth_token_save,
            methods=["POST"],
        ),
        Route(
            "/connections/providers/codex/device/start",
            dash.codex_device_start,
            methods=["POST"],
        ),
        Route(
            "/connections/providers/codex/device/cancel",
            dash.codex_device_cancel,
            methods=["POST"],
        ),
        Route("/connections/browser/install", dash.browser_install, methods=["POST"]),
        Route("/connections/browser/install/stop", dash.browser_install_stop, methods=["POST"]),
        Route("/preview/live", dash.preview_live),
        Route("/preview/frame", dash.preview_frame),
        Route("/preview/takeover", dash.preview_takeover, methods=["POST"]),
        Route("/preview/handback", dash.preview_handback, methods=["POST"]),
        Route("/preview/input", dash.preview_input, methods=["POST"]),
        Route("/preview/agent/register", dash.preview_agent_register, methods=["POST"]),
        Route("/preview/agent/frame", dash.preview_agent_frame, methods=["POST"]),
        Route("/preview/agent/poll", dash.preview_agent_poll, methods=["POST"]),
        Route("/settings", dash.settings_page),
        Route("/diagnostics", dash.diagnostics_page),
        Route("/settings/set", dash.settings_set, methods=["POST"]),
        Route("/settings/unset", dash.settings_unset, methods=["POST"]),
        Route("/ui-style", dash.ui_style_set, methods=["POST"]),
        Route("/settings/entity", dash.settings_entity, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
    ]

    async def http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)
        if not hasattr(request.state, "csrf"):  # rejected before the session was set up
            return PlainTextResponse(exc.detail, status_code=exc.status_code)
        return dash.render(
            request, "error.html", exc.status_code, title=str(exc.status_code), message=exc.detail
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        # The server is stopping (Ctrl-C, systemd): stop the jobs it started, so their agent
        # processes do not keep running and spending without anyone watching.
        await run_in_threadpool(dash.shutdown_jobs, JOB_SHUTDOWN_TIMEOUT_S)

    app = Starlette(
        routes=routes,
        middleware=[Middleware(SecurityMiddleware, settings=settings)],
        exception_handlers={HTTPException: http_error},
        lifespan=lifespan,
    )
    app.state.preview = PreviewBridge()
    app.state.dashboard = dash
    return app
