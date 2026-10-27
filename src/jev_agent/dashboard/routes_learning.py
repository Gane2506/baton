"""LearningRoutes: Learning and artifacts pages and actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    Response,
)

from jev_agent.app.errors import JevError
from jev_agent.app.learning import (
    delete as learning_delete_profile,
)
from jev_agent.app.learning import (
    flush as flush_lessons,
)
from jev_agent.core.learning import LearningError, learning_status
from jev_agent.dashboard import views
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.errors import friendly_error as _friendly_error
from jev_agent.dashboard.helpers import (
    _text,
)
from jev_agent.dashboard.learning_helpers import (
    _learning_approve_skill,
    _learning_create,
    _learning_delete_lesson,
    _learning_delete_skill,
    _learning_disable,
    _learning_edit,
    _learning_enable,
    _learning_forget,
    _learning_graph_link,
    _learning_graph_unlink,
    _learning_use,
)
from jev_agent.dashboard.security import (
    checked_form,
)


class LearningRoutes(DashboardCore):
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
            # The Ratings tab is repo-wide (the ledger is per-repo, not
            # per-profile), so it loads without a selected profile.
            ratings = views.ratings_view(repo) if tab == "ratings" else None
            resolved = "list"
            if tab == "graph" and graph is not None:
                resolved = "graph"
            elif tab == "ratings":
                resolved = "ratings"
            return {
                "lv": lv,
                "include_retired": include_retired,
                "tab": resolved,
                "graph": graph,
                "ratings": ratings,
                "relation_facets": views.RELATION_FACETS,
            }

        return self.page(request, "learning.html", build)

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

    async def learning_action(self, request: Request) -> Response:
        action: str = request.path_params["action"]
        if request.method == "GET":
            # Refreshing a confirmation page (flush/delete, issue #316), or a
            # stray GET to any learning action: back to Learning, never a 405.
            self.flash(
                request, "error", "start again from Learning — this page only follows its form"
            )
            return self.back("/learning")
        if action == "flush":
            return await self.learning_flush(request)
        if action == "delete":
            return await self.learning_delete(request)
        if action == "graph-link":
            return await self.learning_graph_edge(request, _learning_graph_link)
        if action == "graph-unlink":
            return await self.learning_graph_edge(request, _learning_graph_unlink)
        handlers: dict[str, Callable[[views.Repo, FormData], str]] = {
            "create": _learning_create,
            "use": _learning_use,
            "enable": _learning_enable,
            "disable": _learning_disable,
            "forget": _learning_forget,
            "edit": _learning_edit,
            "delete-lesson": _learning_delete_lesson,
            "approve-skill": _learning_approve_skill,
            "delete-skill": _learning_delete_skill,
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
                self.flash(request, "error", _friendly_error(exc))
                return self.back("/learning")
            return self.render(request, "confirm_flush.html", profile=name, lessons=lessons)

        def flush() -> int:
            with views.open_repo(self.settings.root) as repo:
                return flush_lessons(repo.learning, name)

        try:
            deleted = await run_in_threadpool(flush)
        except (JevError, LearningError) as exc:
            self.flash(request, "error", _friendly_error(exc))
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
                self.flash(request, "error", _friendly_error(exc))
                return self.back("/learning")
            return self.render(request, "confirm_learning_delete.html", profile=name, **info)

        def delete() -> None:
            with views.open_repo(self.settings.root) as repo:
                learning_delete_profile(repo.store, repo.learning, name)

        try:
            await run_in_threadpool(delete)
        except (JevError, LearningError) as exc:
            self.flash(request, "error", _friendly_error(exc))
        else:
            self.flash(request, "ok", f"deleted profile {name}")
        return self.back("/learning")

    async def learning_graph_edge(
        self,
        request: Request,
        handler: Callable[[views.Repo, FormData], str],
    ) -> Response:
        """Run a graph link/unlink (issue #254), then send the user back to the Graph tab
        focused on the source lesson, so the recorded/removed edge is visible at once.
        Errors flash with `friendly_error`, never a bare 500 or traceback."""
        form = await checked_form(request)
        profile = _text(form, "profile") or ""
        src = _text(form, "src") or ""

        def work() -> str:
            with views.open_repo(self.settings.root) as repo:
                return handler(repo, form)

        try:
            message = await run_in_threadpool(work)
        except (JevError, LearningError) as exc:
            self.flash(request, "error", _friendly_error(exc))
        except Exception as exc:  # issue #200: an unexpected failure used to
            # escape to Starlette's plain-text 500; flash it and redirect instead.
            self.flash(request, "error", _friendly_error(exc))
        else:
            self.flash(request, "ok", message)
        target = "/learning"
        if profile:
            target = f"/learning?profile={quote(profile)}&tab=graph"
            if src:
                target += f"&lesson={quote(src)}"
        return self.back(target)
