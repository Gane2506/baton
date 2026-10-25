"""ChatRoutes: Chat pages, chat actions and the chat work pane."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    Response,
)

from jev_agent.app import chat, settings
from jev_agent.app.errors import JevError
from jev_agent.app.quality import review_text
from jev_agent.config import ConfigError
from jev_agent.config.loader import default_user_config_path
from jev_agent.core.chat import (
    DEFAULT_BUDGET_USD,
    ChatError,
    describe_design_system,
    list_design_library,
    parse_design_system,
)
from jev_agent.core.design_system import (
    get_design_system,
)
from jev_agent.core.learning import LearningError
from jev_agent.core.lease import RunInUseError
from jev_agent.core.models import RunState
from jev_agent.core.pipeline import STOPPED, PipelineError
from jev_agent.core.queue import QueueError
from jev_agent.core.store import (
    NotFoundError,
)
from jev_agent.dashboard import views
from jev_agent.dashboard.chat_helpers import (
    _CHAT_SUGGESTIONS,
    API_MODEL_OPTIONS,
    _approval_cards,
    _chat_backend_name,
    _chat_context_bar,
    _chat_greeting,
    _chat_learning,
    _chat_starters,
    _rail_groups,
    _step_blocks,
)
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.errors import friendly_error as _friendly_error
from jev_agent.dashboard.helpers import (
    _PLAN_ACTIVE_STATES,
    _WORK_TABS,
    _newest_attempt_log,
    _run_diff,
    _tail_lines,
    _text,
)
from jev_agent.dashboard.preview import (
    PreviewBridge,
)
from jev_agent.dashboard.render import (
    render_markdown,
)
from jev_agent.dashboard.routes_preview import _preview_bridge, _preview_view
from jev_agent.dashboard.security import (
    checked_form,
)


class ChatRoutes(DashboardCore):
    def chats_page(self, request: Request) -> Response:
        query = request.query_params.get("q", "")

        def build(repo: views.Repo) -> dict[str, Any]:
            rows = views.chats(repo, query)
            backends = chat.backend_readiness(repo.config)
            api_ok, _ = backends["api"]
            return {
                "chats": rows,
                "q": query.strip(),
                "busy_ids": {
                    meta.chat_id for meta, _ in rows if self.chat_runner.busy(meta.chat_id)
                },
                "profiles": repo.learning.profiles(),
                "backends": backends,
                # API model picker (issue #149): the API backend is usable but no
                # default model is set — show an inline picker instead of the raw
                # "no model for the api backend" error.
                "api_model_needed": api_ok and not repo.config.workers.api.default_model,
                "api_model_options": API_MODEL_OPTIONS,
                # The submenu shows the same filtered list as the page.
                "submenu_chats": rows[:12],
                # The New chat form's design-system picker, like `profiles` above.
                "design_systems": list_design_library(),
                "has_repo_design_system": get_design_system(repo.root) is not None,
            }

        return self.page(request, "chats.html", build)

    def _chat_context(self, chat_id: str) -> Callable[[views.Repo], dict[str, Any]]:
        def build(repo: views.Repo) -> dict[str, Any]:
            meta, entries = views.chat_transcript(repo, chat_id)
            if meta is None and not entries:
                raise NotFoundError(f"no chat {chat_id}")
            approval_cards = _approval_cards(repo, meta) if meta is not None else []
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
                # Smart rail: chats grouped by recency with a rich status per chat
                # (issue #156) — the flat rail_chats stays for other consumers.
                "rail_groups": _rail_groups(self.chat_runner, repo, chat_id, approval_cards),
                "approval_cards": approval_cards,
                "suggestions": list(_CHAT_SUGGESTIONS) if not entries else [],
                # Repo-aware starters + greeting for the serene empty state.
                "starters": _chat_starters(repo) if not entries else [],
                "greeting": _chat_greeting(repo.root.name) if not entries else "",
                # Persistent compact context: repo, branch, backend, model (issue #156).
                "context_bar": _chat_context_bar(repo, meta),
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

    async def chat_delete(self, request: Request) -> Response:
        """Two steps (Tier 2), like `design_system_delete`: the first POST shows a
        confirmation page; only `confirm=yes` deletes the chat. Deleting removes
        its transcript and brief. Runs it started are kept."""
        form = await checked_form(request)  # CSRF; the id comes from the path
        chat_id = request.path_params["chat_id"]
        if self.chat_runner.busy(chat_id):
            self.flash(request, "error", f"chat {chat_id} is replying right now; wait for it")
            return self.back("/chats")
        if form.get("confirm") != "yes":

            def describe() -> str:
                with views.open_repo(self.settings.root) as repo:
                    for meta, title in views.chats(repo):
                        if meta.chat_id == chat_id:
                            return title
                raise ChatError(f"no chat {chat_id!r}")

            try:
                title = await run_in_threadpool(describe)
            except (ChatError, ConfigError) as exc:
                self.flash(request, "error", _friendly_error(exc))
                return self.back("/chats")
            return self.render(request, "confirm_chat_delete.html", chat_id=chat_id, title=title)

        def remove() -> None:
            with views.open_repo(self.settings.root) as repo:
                chat.delete_chat(repo.root, chat_id)

        try:
            await run_in_threadpool(remove)
        except (ChatError, ConfigError) as exc:
            self.flash(request, "error", _friendly_error(exc))
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
            message = str(exc)
            # Never show the raw "no model" error (issue #149) — the picker on
            # /chats handles it; point there instead.
            if "no model for the api backend" in message:
                message = "pick an API model first — use the picker at the top of this page."
            else:
                hint = "check the Settings page" if isinstance(exc, ConfigError) else ""
                message = _friendly_error(exc, hint=hint)
            self.flash(request, "error", message)
            return self.back("/chats")
        # Minimal new-chat flow: if the user typed a first message, send it
        # right away — type + Enter just works, no second step.
        first_text = _text(form, "text") or ""

        def first_turn() -> str:
            return chat.turn(self.settings.root, chat_id, first_text)

        if first_text and self.chat_runner.start(chat_id, first_turn):
            self.flash(request, "ok", f"chat {chat_id} created; replying…")
        else:
            self.flash(request, "ok", f"chat {chat_id} created; say what you want to build")
        return self.back(f"/chats/{chat_id}")

    async def chats_api_model(self, request: Request) -> Response:
        """Save the API backend's default model (issue #149).

        The picker posts here; the model goes to the *user* config so it works
        across repos. After saving, the picker disappears (disappear-when-done).
        """
        form = await checked_form(request)
        to = _text(form, "next") or "/chats"
        model = (_text(form, "model") or "").strip()

        def save() -> str:
            if not model:
                raise ValueError("pick a model first")
            # A model id is `provider/name`; keep it simple and safe.
            if not all(c.isalnum() or c in "-_./:" for c in model):
                raise ValueError("that doesn't look like a model id")
            user_path = default_user_config_path()
            settings.write_value(user_path, "workers.api.default_model", model)
            return f"API model set to {model}; it applies immediately."

        try:
            message = await run_in_threadpool(save)
        except ValueError as exc:
            self.flash(request, "error", str(exc))
        except (OSError, ConfigError) as exc:
            self.flash(request, "error", _friendly_error(exc, hint="check the Settings page"))
        else:
            self.flash(request, "ok", message)
        return self.back(to)

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
            message = str(exc)
            # The UI disables these buttons without a brief, but a crafted POST
            # could still hit this — keep the message actionable, not raw.
            if "no brief yet" in message:
                message = "write a brief first (the Write brief button above), then plan or run."
            else:
                message = _friendly_error(exc)
            self.flash(request, "error", message)
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
            message = str(exc)
            if "no brief yet" in message:
                message = "write a brief first (the Write brief button above), then queue it."
            else:
                message = _friendly_error(exc)
            self.flash(request, "error", message)
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
            self.flash(request, "error", _friendly_error(exc))
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
                payload = self._work_payload(repo, chat_id, _preview_bridge(request))
        except (NotFoundError, ChatError) as exc:
            return JSONResponse({"run_id": None, "error": _friendly_error(exc)}, status_code=404)
        except (ConfigError, JevError) as exc:
            return JSONResponse({"run_id": None, "error": _friendly_error(exc)}, status_code=500)
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
        bridge = _preview_bridge(request)

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
            context["preview"] = _preview_view(bridge.session_for_run(run_id))
        elif tab == "terminal":  # tab is one of _WORK_TABS here
            log = _newest_attempt_log(repo, run_id, repo.store.list_attempts(run_id))
            context["terminal_log"] = _tail_lines(log) if log is not None else None
        return context
