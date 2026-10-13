"""The dashboard's Starlette app: routes, templates and actions (docs/dashboard.md).

Pages read through `views` (the CLI's own store and helpers); actions call the same functions
as the CLI commands (`core.queue.add_idea`, `core.learning.enable_learning`,
`core.pipeline.cancel_run`, `approve_run` / `resume_run` through `runner.RunLauncher`, ...).
Every action is a POST checked by `security.checked_form`, and redirects back (POST, redirect,
GET) with a one-time message.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from jev_agent import __version__
from jev_agent.cli.board_cmds import format_age
from jev_agent.cli.status_cmds import format_time
from jev_agent.config import ConfigError
from jev_agent.core.chat import ChatError
from jev_agent.core.learning import (
    LearningError,
    disable_learning,
    enable_learning,
    use_profile,
)
from jev_agent.core.models import RunState
from jev_agent.core.pipeline import PipelineError, cancel_run
from jev_agent.core.queue import Move, QueueError, add_idea, move_item, remove_item
from jev_agent.core.store import QUEUE_PRIORITIES, NotFoundError
from jev_agent.dashboard import views
from jev_agent.dashboard.render import render_bar_chart
from jev_agent.dashboard.runner import Executor, RunAction, RunLauncher, execute_run
from jev_agent.dashboard.security import (
    CSRF_FIELD,
    DashboardSettings,
    SecurityMiddleware,
    checked_form,
)
from jev_agent.quality.levels import LEVELS

STATIC_DIR = Path(__file__).parent / "static"

NAV = (
    ("/", "Board"),
    ("/queue", "Queue"),
    ("/learning", "Learning"),
    ("/chats", "Chats"),
    ("/spend", "Spend"),
    ("/settings", "Settings"),
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
    env.globals["bar_chart_svg"] = render_bar_chart
    return env


class Dashboard:
    """The app's state: settings, templates, background runs and one-time messages."""

    def __init__(self, settings: DashboardSettings, launcher: RunLauncher) -> None:
        self.settings = settings
        self.launcher = launcher
        self.env = _environment()
        self._flash_lock = threading.Lock()
        self._flashes: dict[str, list[tuple[str, str]]] = {}

    # ---- helpers ---------------------------------------------------------------------------

    def flash(self, request: Request, kind: str, text: str) -> None:
        with self._flash_lock:
            self._flashes.setdefault(request.state.session, []).append((kind, text))

    def take_flashes(self, request: Request) -> list[tuple[str, str]]:
        with self._flash_lock:
            return self._flashes.pop(request.state.session, [])

    def render(
        self, request: Request, template: str, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        page = self.env.get_template(template).render(
            request=request,
            path=request.url.path,
            csrf=request.state.csrf,
            refresh_ms=int(self.settings.refresh_s * 1000),
            root=self.settings.root,
            flashes=self.take_flashes(request) if not template.startswith("_") else [],
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
            ConfigError,
            NotFoundError,
            PipelineError,
            QueueError,
            LearningError,
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
            lambda repo: {"columns": views.board(repo, show_all), "show_all": show_all},
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

    def run_fragment(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        return self.page(request, "_run_live.html", lambda repo: self._run_context(repo, run_id))

    def run_file(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        relative = request.path_params["path"]
        with views.open_repo(self.settings.root) as repo:
            path = views.safe_run_file(repo, run_id, relative)
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
                "show_all": show_all,
                "priorities": QUEUE_PRIORITIES,
                "levels": LEVELS,
            },
        )

    def learning_page(self, request: Request) -> Response:
        profile = request.query_params.get("profile")
        include_retired = request.query_params.get("all") == "1"
        return self.page(
            request,
            "learning.html",
            lambda repo: {
                "lv": views.learning_view(repo, profile, include_retired),
                "include_retired": include_retired,
            },
        )

    def chats_page(self, request: Request) -> Response:
        return self.page(request, "chats.html", lambda repo: {"chats": views.chats(repo)})

    def chat_page(self, request: Request) -> Response:
        chat_id = request.path_params["chat_id"]

        def build(repo: views.Repo) -> dict[str, Any]:
            meta, entries = views.chat_transcript(repo, chat_id)
            if meta is None and not entries:
                raise NotFoundError(f"no chat {chat_id}")
            return {"chat_id": chat_id, "meta": meta, "entries": entries}

        return self.page(request, "chat.html", build)

    def spend_page(self, request: Request) -> Response:
        return self.page(request, "spend.html", lambda repo: {"s": views.spend_view(repo)})

    def settings_page(self, request: Request) -> Response:
        return self.page(
            request,
            "settings.html",
            lambda repo: {
                "summary": views.config_summary(repo),
                "env_vars": views.env_vars(repo.config),
                "checks": views.doctor_checks(repo.root),
                "settings": self.settings,
            },
        )

    # ---- actions: runs ---------------------------------------------------------------------

    async def run_action(self, request: Request) -> Response:
        run_id: str = request.path_params["run_id"]
        action: str = request.path_params["action"]
        to = f"/runs/{run_id}"
        if not views.valid_run_id(run_id) or action not in ("approve", "resume", "cancel"):
            raise HTTPException(404, "no such action")
        if action == "cancel":

            def cancel(repo: views.Repo, form: FormData) -> str:
                record = cancel_run(repo.store, run_id)
                return f"run {record.id} {record.state}; its branches and worktrees are kept"

            return await self.act(request, to, cancel)

        await checked_form(request)
        kind: RunAction = "approve" if action == "approve" else "resume"

        def check() -> str | None:
            with views.open_repo(self.settings.root) as repo:
                run = repo.store.get_run(run_id)
            if kind == "approve" and run.state is not RunState.AWAITING_APPROVAL:
                return f"run {run_id} is {run.state}; only a run awaiting approval can be approved"
            if kind == "resume" and not views.can_resume(run):
                return f"run {run_id} is {run.state}; it cannot be resumed"
            return None

        try:
            problem = await run_in_threadpool(check)
        except (NotFoundError, ConfigError) as exc:
            problem = str(exc)
        if problem is not None:
            self.flash(request, "error", problem)
        elif self.launcher.start(run_id, kind):
            self.flash(request, "ok", f"{kind} started in the background; this page updates")
        else:
            self.flash(request, "error", f"run {run_id} is already being worked on here")
        return self.back(to)

    # ---- actions: queue --------------------------------------------------------------------

    async def queue_add(self, request: Request) -> Response:
        def add(repo: views.Repo, form: FormData) -> str:
            learning_choice = _text(form, "learning")
            item = add_idea(
                repo.store,
                repo.learning,
                _text(form, "idea") or "",
                _text(form, "priority") or "normal",
                plan_only=form.get("plan_only") == "on",
                learning_profile=learning_choice
                if learning_choice not in (None, "", "none")
                else None,
                no_learning=learning_choice == "none",
                profile=_text(form, "profile") or None,
                quality=_text(form, "quality") or None,
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

    # ---- actions: learning -----------------------------------------------------------------

    async def learning_action(self, request: Request) -> Response:
        action: str = request.path_params["action"]
        if action == "flush":
            return await self.learning_flush(request)
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
            except LearningError as exc:
                self.flash(request, "error", str(exc))
                return self.back("/learning")
            return self.render(request, "confirm_flush.html", profile=name, lessons=lessons)

        def flush() -> int:
            with views.open_repo(self.settings.root) as repo:
                return repo.learning.flush(name)

        try:
            deleted = await run_in_threadpool(flush)
        except LearningError as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", f"deleted {deleted} lesson(s) from {name}")
        return self.back(f"/learning?profile={name}")


def _text(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else None


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise HTTPException(404, "no such item") from None


def _learning_create(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "name") or ""
    profile = repo.learning.create(name, shared=form.get("shared") == "on")
    return f"created profile {profile.name} ({profile.scope})"


def _learning_use(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or ""
    use_profile(repo.store, repo.learning, name)
    return f"active profile: {name}"


def _learning_enable(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or None
    active = enable_learning(repo.store, repo.config, repo.learning, name)
    if name is None:
        return f"learning is on for this repo, with profile {active}"
    return f"profile {name} is enabled"


def _learning_disable(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or None
    disable_learning(repo.store, repo.learning, name)
    if name is None:
        return "learning is off for this repo; lessons are kept"
    return f"profile {name} is disabled"


def _learning_forget(repo: views.Repo, form: FormData) -> str:
    lesson = repo.learning.forget(_text(form, "lesson") or "")
    return f"retired {lesson.id} from {lesson.profile}"


def create_app(
    settings: DashboardSettings,
    *,
    executor: Executor = execute_run,
) -> Starlette:
    """Build the app. Raises `DashboardConfigError` for a non-loopback host without a token.
    `executor` runs approve/resume jobs (tests pass a fake one)."""
    settings.check()
    dash = Dashboard(settings, RunLauncher(settings.root, executor))
    routes = [
        Route("/", dash.board_page),
        Route("/fragments/board", dash.board_fragment),
        Route("/runs/{run_id}", dash.run_page),
        Route("/runs/{run_id}/live", dash.run_fragment),
        Route("/runs/{run_id}/files/{path:path}", dash.run_file),
        Route("/runs/{run_id}/{action}", dash.run_action, methods=["POST"]),
        Route("/queue", dash.queue_page),
        Route("/queue/add", dash.queue_add, methods=["POST"]),
        Route("/queue/{item_id}/move", dash.queue_move, methods=["POST"]),
        Route("/queue/{item_id}/remove", dash.queue_remove, methods=["POST"]),
        Route("/learning", dash.learning_page),
        Route("/learning/{action}", dash.learning_action, methods=["POST"]),
        Route("/chats", dash.chats_page),
        Route("/chats/{chat_id}", dash.chat_page),
        Route("/spend", dash.spend_page),
        Route("/settings", dash.settings_page),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
    ]

    async def http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)
        if not hasattr(request.state, "csrf"):  # rejected before the session was set up
            return PlainTextResponse(exc.detail, status_code=exc.status_code)
        return dash.render(
            request, "error.html", exc.status_code, title=str(exc.status_code), message=exc.detail
        )

    app = Starlette(
        routes=routes,
        middleware=[Middleware(SecurityMiddleware, settings=settings)],
        exception_handlers={HTTPException: http_error},
    )
    app.state.dashboard = dash
    return app
