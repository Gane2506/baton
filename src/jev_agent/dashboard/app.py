"""The dashboard's Starlette app: routes, templates and actions (docs/dashboard.md).

Pages read through `views` (the CLI's own store and helpers); actions call the same functions
as the CLI commands (`core.queue.add_idea`, `core.learning.enable_learning`,
`core.pipeline.cancel_run`, `approve_run` / `resume_run` through `runner.RunLauncher`, ...).
Every action is a POST checked by `security.checked_form`, and redirects back (POST, redirect,
GET) with a one-time message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    PlainTextResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from jev_agent.core.ship import check_gh, prepare_ship, sync_pr
from jev_agent.dashboard.connection_status import (
    ssh_runner,
)
from jev_agent.dashboard.design_reverse import (
    ReverseExecutor,
)
from jev_agent.dashboard.nav import NAV, NavChild, NavItem
from jev_agent.dashboard.preview import (
    PreviewBridge,
)
from jev_agent.dashboard.routes_board import BoardRoutes
from jev_agent.dashboard.routes_chat import ChatRoutes
from jev_agent.dashboard.routes_connections import ConnectionsRoutes
from jev_agent.dashboard.routes_design import DesignRoutes
from jev_agent.dashboard.routes_learning import LearningRoutes
from jev_agent.dashboard.routes_preview import PreviewRoutes
from jev_agent.dashboard.routes_queue import QueueRoutes
from jev_agent.dashboard.routes_settings import SettingsRoutes
from jev_agent.dashboard.routes_studio import StudioRoutes
from jev_agent.dashboard.runner import (
    Executor,
    RunLauncher,
    execute_run,
)
from jev_agent.dashboard.security import (
    DashboardSettings,
    SecurityMiddleware,
)

# Names other modules and tests import from this module: the nav items and the
# `ssh_runner` / ship seams live in their own modules now but stay importable
# here — the route mixins read them from here at call time so tests can fake them.
__all__ = [
    "NAV",
    "Dashboard",
    "NavChild",
    "NavItem",
    "check_gh",
    "create_app",
    "prepare_ship",
    "ssh_runner",
    "sync_pr",
]

STATIC_DIR = Path(__file__).parent / "static"


class Dashboard(
    BoardRoutes,
    QueueRoutes,
    ChatRoutes,
    LearningRoutes,
    DesignRoutes,
    StudioRoutes,
    ConnectionsRoutes,
    SettingsRoutes,
    PreviewRoutes,
):
    """The dashboard app: route handlers grouped by area (routes_*.py mixins),
    over DashboardCore (state, render/page/back/act)."""


JOB_SHUTDOWN_TIMEOUT_S = 30.0  # per job, for blocking steps that ignore cancellation


def create_app(
    settings: DashboardSettings,
    *,
    executor: Executor = execute_run,
    reverse_executor: ReverseExecutor | None = None,
) -> Starlette:
    """Build the app. Raises `DashboardConfigError` for a non-loopback host without a token.
    `executor` runs approve/resume jobs (tests pass a fake one); `reverse_executor`
    runs design-studio reverse jobs (tests pass a fake one)."""
    settings.check()
    from jev_agent.core.secrets import load_secrets

    load_secrets()  # ~/.jev/secrets.env: real environment always wins
    dash = Dashboard(settings, RunLauncher(settings.root, executor), reverse_executor)
    routes = [
        Route("/", dash.board_page),
        Route("/fragments/board", dash.board_fragment),
        Route("/runs/new", dash.runs_new, methods=["POST"]),
        Route("/runs/{run_id}", dash.run_page),
        Route("/runs/{run_id}/live", dash.run_fragment),
        Route("/runs/{run_id}/files/{path:path}", dash.run_file),
        Route("/runs/{run_id}/events/backfill", dash.events_backfill, methods=["POST"]),
        Route("/runs/{run_id}/{action}", dash.run_action, methods=["POST"]),
        Route("/queue", dash.queue_page),
        Route("/queue/add", dash.queue_add, methods=["POST"]),
        Route("/queue/{item_id}/move", dash.queue_move, methods=["POST"]),
        Route("/queue/{item_id}/remove", dash.queue_remove, methods=["POST"]),
        Route("/queue/recurring/add", dash.queue_recurring_add, methods=["POST"]),
        Route("/queue/recurring/{id}/pause", dash.queue_recurring_pause, methods=["POST"]),
        Route("/queue/recurring/{id}/resume", dash.queue_recurring_resume, methods=["POST"]),
        Route("/queue/recurring/{id}/remove", dash.queue_recurring_remove, methods=["POST"]),
        Route("/queue/scheduler/start", dash.scheduler_start, methods=["POST"]),
        Route("/queue/scheduler/stop", dash.scheduler_stop, methods=["POST"]),
        Route("/queue/clear", dash.queue_clear, methods=["POST"]),
        Route("/artifacts", dash.artifacts_page),
        Route("/learning", dash.learning_page),
        Route("/learning/{action}", dash.learning_action, methods=["POST"]),
        Route("/chats", dash.chats_page),
        Route("/chats/new", dash.chats_new, methods=["POST"]),
        Route("/chats/api-model", dash.chats_api_model, methods=["POST"]),
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
        Route("/design-systems/{name}/showcase", dash.design_showcase_page),
        Route("/design-systems/{name}/code", dash.design_code_page),
        Route("/design-systems/{name}/code/download", dash.design_code_download),
        Route("/design-systems/{action}", dash.design_systems_action, methods=["POST"]),
        Route("/design-studio", dash.design_studio_page),
        Route("/design-studio/new", dash.design_studio_start, methods=["GET", "POST"]),
        Route("/design-studio/new/{step}", dash.design_studio_step_page),
        # The generate POST is registered before the generic step POST so it wins.
        Route("/design-studio/new/generate", dash.design_studio_generate, methods=["POST"]),
        Route("/design-studio/new/{step}", dash.design_studio_step, methods=["POST"]),
        Route("/design-studio/reverse", dash.design_reverse_page),
        Route("/design-studio/reverse", dash.design_reverse_start, methods=["POST"]),
        Route("/design-studio/reverse/status", dash.design_reverse_status),
        Route("/design-studio/reverse/live", dash.design_reverse_live),
        Route("/tools", dash.tools_page),
        Route("/tools/models/refresh", dash.models_refresh, methods=["POST"]),
        Route("/connections", dash.connections_page),
        Route("/connections/live", dash.connections_live),
        Route("/connections/refresh", dash.connections_refresh, methods=["POST"]),
        Route(
            "/connections/servers/{name}/stats/refresh",
            dash.server_stats_refresh,
            methods=["POST"],
        ),
        Route("/connections/keys/add", dash.connections_key_add, methods=["POST"]),
        Route("/connections/keys/remove", dash.connections_key_remove, methods=["POST"]),
        Route("/connections/trust", dash.connections_trust_repo, methods=["POST"]),
        Route(
            "/connections/providers/claude/oauth-token",
            dash.claude_oauth_token_save,
            methods=["POST"],
        ),
        Route(
            "/connections/providers/claude/setup-token/start",
            dash.claude_setup_token_start,
            methods=["POST"],
        ),
        Route(
            "/connections/providers/claude/setup-token/cancel",
            dash.claude_setup_token_cancel,
            methods=["POST"],
        ),
        Route(
            "/connections/providers/claude/setup-token/code",
            dash.claude_setup_token_code,
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
        if not isinstance(exc, HTTPException):
            # The handler is registered for HTTPException only, so this is
            # defensive — but a non-HTTP error must be a 500, not a crash.
            return PlainTextResponse("internal server error", status_code=500)
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
