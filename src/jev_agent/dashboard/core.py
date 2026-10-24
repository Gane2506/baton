"""DashboardCore: the dashboard's shared state and page/action plumbing.

The route handlers (routes_*.py) are mixins over this class: it owns the
settings, template environment, background runners, flash messages and the
`render` / `page` / `act` helpers every handler builds on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from jev_agent.app import settings
from jev_agent.app.errors import JevError
from jev_agent.config import ConfigError
from jev_agent.core.chat import ChatError
from jev_agent.core.learning import LearningError
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.provider_auth import ClaudeSetupTokenFlow, CodexDeviceFlow
from jev_agent.core.queue import QueueError
from jev_agent.core.ship import ShipError
from jev_agent.core.store import NotFoundError
from jev_agent.dashboard import ui_styles, views
from jev_agent.dashboard.browser_install import BrowserInstallRunner
from jev_agent.dashboard.design_reverse import ReverseExecutor, ReverseRunner
from jev_agent.dashboard.design_studio import StudioWizard
from jev_agent.dashboard.errors import friendly_error as _friendly_error
from jev_agent.dashboard.helpers import palette_registry
from jev_agent.dashboard.nav import NAV, _environment
from jev_agent.dashboard.runner import ChatRunner, RunLauncher
from jev_agent.dashboard.scheduler_runner import SchedulerRunner
from jev_agent.dashboard.security import DashboardSettings, checked_form
from jev_agent.demo import is_demo_repo
from jev_agent.servers.stats import ServerStatsCache


class DashboardCore:
    """The app's state: settings, templates, background runs and one-time messages."""

    _MAX_FLASHES_PER_SESSION = 30

    _FLASH_SESSION_TTL_S = 6 * 3600

    def __init__(
        self,
        settings: DashboardSettings,
        launcher: RunLauncher,
        reverse_executor: ReverseExecutor | None = None,
    ) -> None:
        self.settings = settings
        self.launcher = launcher
        self.chat_runner = ChatRunner()
        self.scheduler_runner = SchedulerRunner(settings.root, launcher)
        self.browser_installer = BrowserInstallRunner(settings.root)
        self.reverse_runner = (
            ReverseRunner(settings.root, executor=reverse_executor)
            if reverse_executor is not None
            else ReverseRunner(settings.root)
        )
        self.codex_flow = CodexDeviceFlow()
        self.claude_flow = ClaudeSetupTokenFlow()
        self.server_stats = ServerStatsCache()
        self.started_at = datetime.now(UTC)  # the About panel's uptime
        self.env = _environment()
        self._flash_lock = threading.Lock()
        self._flashes: dict[str, list[tuple[str, str]]] = {}
        self._flash_seen: dict[str, float] = {}  # session -> last flash (monotonic)
        self.studio_wizard = StudioWizard()  # the Design Studio's wizard answers

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
        self.reverse_runner.shutdown()
        self.codex_flow.shutdown()
        self.claude_flow.shutdown()
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
            # Two-level nav: when a submenu is open for the current path, the
            # sidebar renders icon-collapsed server-side (base.html body class).
            nav_collapsed=any(item.submenu_open(request.url.path) for item in NAV),
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
                # Chats submenu (issue #149): the nav panel lists recent chats, so
                # every page carries them. Capped for the narrow submenu width.
                if "submenu_chats" not in context:
                    context["submenu_chats"] = views.chats(repo, "")[:12]
        except ConfigError as exc:
            # A broken config is the user's to fix, not a server failure: its own page
            # (400, with where to look) instead of the generic 500 error page.
            return self.render(
                request, "config_error.html", 400, title="Config error", message=str(exc)
            )
        except JevError as exc:
            return self.render(request, "error.html", 500, title="State error", message=str(exc))
        except (NotFoundError, ChatError) as exc:
            return self.render(request, "error.html", 404, title="Not found", message=str(exc))
        except Exception as exc:  # issue #200: anything else used to escape to
            # Starlette's plain-text 500. Render the friendly error page instead;
            # the message is the friendly rewrite, never a traceback.
            return self.render(
                request,
                "error.html",
                500,
                title="Something went wrong",
                message=_friendly_error(exc),
            )
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
            self.flash(request, "error", _friendly_error(exc))
        except Exception as exc:  # issue #200: an unexpected failure used to
            # escape to Starlette's plain-text 500; flash it and redirect instead.
            self.flash(request, "error", _friendly_error(exc))
        else:
            # Trust gate blocked the save: offer one-click trust (issue #158).
            if settings.TRUST_GATE_MARKER in message:
                clean = message.replace(settings.TRUST_GATE_MARKER, "").strip()
                self.flash(request, "trust", clean)
            else:
                self.flash(request, "ok", message)
        return self.back(to)
