"""DesignRoutes: Design-system library pages and actions."""

from __future__ import annotations

import json
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    Response,
)

from jev_agent.app.errors import JevError
from jev_agent.core.design_business import (
    BusinessContext,
    elements_for_keys,
    enabled_element_keys,
)
from jev_agent.core.design_codegen import (
    bundle_zip,
    target_for_stack,
    validate_target,
)
from jev_agent.core.design_library import (
    delete_library_system,
    export_tokens_css,
    export_tokens_tailwind,
    export_tokens_w3c,
    library_system_path,
    load_library_system,
    validate_library_name,
)
from jev_agent.core.design_showcase import VIBE_PROFILES, rich_tokens, vibe_for_keywords
from jev_agent.core.design_showcase_html import render_showcase
from jev_agent.core.store import (
    NotFoundError,
)
from jev_agent.dashboard import views
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.design_helpers import (
    _design_system_business_context,
    _design_system_business_elements,
    _design_system_clone,
    _design_system_code_view,
    _design_system_use,
    _design_system_view,
    _design_systems_view,
    _library_meta,
    _meta_text,
    _repo_design_source,
)
from jev_agent.dashboard.helpers import (
    _text,
)
from jev_agent.dashboard.security import (
    SHOWCASE_CONTENT_SECURITY_POLICY,
    checked_form,
)


class DesignRoutes(DashboardCore):
    def design_systems_page(self, request: Request) -> Response:
        """The design library: the saved systems, their sources and versions (Tier 0)."""
        query = request.query_params.get("q", "")

        def build(repo: views.Repo) -> dict[str, Any]:
            view = _design_systems_view(repo, query)
            view["q"] = query.strip()
            return view

        return self.page(request, "design_systems.html", build)

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

    async def design_showcase_page(self, request: Request) -> Response:
        """The system's showcase gallery: standalone stunning preview page.

        Serves the saved ``showcase.html``; older systems (saved before the
        showcase existed) get it rendered on demand from their stored tokens.
        """
        name = request.path_params["name"]
        try:
            system_name = validate_library_name(name)
        except ValueError:
            raise HTTPException(404, "no such design system") from None

        def build() -> str:
            system = load_library_system(system_name)
            if system is None:
                raise NotFoundError(f'no design system named "{system_name}"')
            saved = library_system_path(system_name) / "showcase.html"
            if saved.is_file():
                return saved.read_text(encoding="utf-8")
            meta = _library_meta(system_name)
            tokens = system.tokens if isinstance(system.tokens, dict) else {}
            rich = tokens.get("design_tokens")
            profile_key = _meta_text(meta, "vibe_profile")
            profile = VIBE_PROFILES.get(profile_key) or vibe_for_keywords(
                _meta_text(meta, "vibe") or _meta_text(meta, "product_name")
            )
            theme = _meta_text(meta, "theme") or "both"
            if not isinstance(rich, dict):
                rich = rich_tokens(profile, theme)
            return render_showcase(
                system_name, profile, rich, theme, business=BusinessContext.from_meta(meta)
            )

        try:
            html = await run_in_threadpool(build)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        return Response(
            html,
            media_type="text/html; charset=utf-8",
            headers={
                "X-Content-Type-Options": "nosniff",
                # Issue #200: the showcase's own inline <style>/<script> would
                # be blocked by the dashboard's strict CSP (the middleware
                # only setdefaults, so this explicit header wins, on this
                # route alone).
                "Content-Security-Policy": SHOWCASE_CONTENT_SECURITY_POLICY,
            },
        )

    def design_code_page(self, request: Request) -> Response:
        """Get reusable elements: per-component code with copy buttons (Tier 0)."""
        name = request.path_params["name"]
        try:
            validate_library_name(name)
        except ValueError:
            raise HTTPException(404, "no such design system") from None
        target = validate_target(request.query_params.get("target", ""))
        if not target or "target" not in request.query_params:
            meta = _library_meta(name)
            target = target_for_stack(_meta_text(meta, "stack"))
        return self.page(
            request,
            "design_system_code.html",
            lambda repo: _design_system_code_view(repo, name, target),
        )

    async def design_code_download(self, request: Request) -> Response:
        """Download all generated component files as a zip (Tier 0: read-only)."""
        name = request.path_params["name"]
        target = validate_target(request.query_params.get("target", "html-css"))
        try:
            system_name = validate_library_name(name)
        except ValueError as exc:
            self.flash(request, "error", str(exc))
            return self.back("/design-systems")

        def build() -> bytes:
            system = load_library_system(system_name)
            if system is None:
                raise JevError(f'no design system named "{system_name}" in the library')
            tokens = system.tokens if isinstance(system.tokens, dict) else {}
            meta = _library_meta(system_name)
            context = BusinessContext.from_meta(meta)
            business_elements = (
                elements_for_keys(enabled_element_keys(meta, context))
                if context is not None and not context.is_empty()
                else []
            )
            return bundle_zip(target, system_name, tokens, business_elements=business_elements)

        try:
            data = await run_in_threadpool(build)
        except JevError as exc:
            self.flash(request, "error", str(exc))
            return self.back(f"/design-systems/{system_name}")
        return Response(
            data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{system_name}-{target}.zip"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def design_systems_action(self, request: Request) -> Response:
        """Dispatch the library's POST actions: use, clone, export, delete, business."""
        action: str = request.path_params["action"]
        if action == "use":
            return await self.act(request, "/design-systems", _design_system_use)
        if action == "clone":
            return await self.act(request, "/design-systems", _design_system_clone)
        if action == "business":
            # The edit form posts from the detail page, so the redirect returns
            # there — `act` takes a fixed `to`, and the name only exists in the
            # form. `checked_form` is safe to call twice: Starlette caches the
            # parsed form on the request, so `act` re-reads the same data.
            form = await checked_form(request)
            name = (_text(form, "name") or "").strip()
            try:
                system_name = validate_library_name(name)
            except ValueError as exc:
                self.flash(request, "error", str(exc))
                return self.back("/design-systems")
            return await self.act(
                request, f"/design-systems/{system_name}", _design_system_business_context
            )
        if action == "business-elements":
            # Same fixed-`to` constraint as above: read the form once for the
            # name and the target to return to, then let `act` re-read it.
            form = await checked_form(request)
            name = (_text(form, "name") or "").strip()
            raw_target = (_text(form, "target") or "").strip()
            raw_return = (_text(form, "return_to") or "").strip()
            try:
                system_name = validate_library_name(name)
            except ValueError as exc:
                self.flash(request, "error", str(exc))
                return self.back("/design-systems")
            to = f"/design-systems/{system_name}/code"
            if raw_target:
                to += f"?target={validate_target(raw_target)}"
            if raw_return.startswith(f"/design-systems/{system_name}"):
                # Toggles posted from the redesigned system page (issue #285)
                # return there instead of the code page.
                to = raw_return
            return await self.act(request, to, _design_system_business_elements)
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
