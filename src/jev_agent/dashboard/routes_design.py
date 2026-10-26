"""DesignRoutes: Design-system library pages and actions."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    Response,
)

from jev_agent.app.errors import JevError
from jev_agent.core import figma as figma_mod
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
    save_library_system,
    update_library_meta,
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
    _design_system_figma_view,
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


def _render_showcase_html(system_name: str) -> str:
    """Render a system's showcase on demand from its stored tokens + meta.

    The same render the showcase route serves when no saved file exists —
    and the same render the refresh action writes back over a stale saved
    file (issue #323), so a refreshed showcase is byte-identical to what
    the route would render on demand. Raises NotFoundError when the system
    does not exist.
    """
    system = load_library_system(system_name)
    if system is None:
        raise NotFoundError(f'no design system named "{system_name}"')
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
        if name == "delete" and load_library_system(name) is None:
            # Refreshing the delete-confirmation page (POST /design-systems/delete,
            # issue #316): that URL is shadowed by this route, and no system is
            # named "delete" — back to the library instead of a 404. A real
            # system named "delete" still renders normally above.
            self.flash(
                request, "error", "start again from the library — this page only follows its form"
            )
            return self.back("/design-systems")
        return self.page(
            request, "design_system.html", lambda repo: _design_system_view(repo, name)
        )

    async def design_showcase_page(self, request: Request) -> Response:
        """The system's showcase gallery: standalone stunning preview page.

        Serves the saved ``showcase.html``; older systems (saved before the
        showcase existed) get it rendered on demand from their stored tokens.
        A saved showcase that predates a generator fix stays stale until the
        system page's "Regenerate showcase" action re-renders it (issue #323).
        """
        name = request.path_params["name"]
        try:
            system_name = validate_library_name(name)
        except ValueError:
            raise HTTPException(404, "no such design system") from None

        def build() -> str:
            saved = library_system_path(system_name) / "showcase.html"
            if saved.is_file():
                return saved.read_text(encoding="utf-8")
            return _render_showcase_html(system_name)

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

    async def design_system_showcase_refresh(self, request: Request) -> Response:
        """Re-render a system's saved ``showcase.html`` (issue #323).

        The saved file embeds the generator's CSS at generation time, so a
        system saved before a showcase fix (e.g. #322's mobile overflow)
        keeps serving the stale file even though the on-demand render is
        fixed. This action re-renders from the stored tokens with the
        current generator and overwrites only ``showcase.html`` — tokens,
        markdown, meta and every other file are untouched, and the version
        is not bumped (the system itself did not change).
        """
        form = await checked_form(request)
        name = (_text(form, "name") or "").strip()
        to = f"/design-systems/{name}" if name else "/design-systems"
        try:
            system_name = validate_library_name(name)
        except ValueError as exc:
            self.flash(request, "error", str(exc))
            return self.back("/design-systems")

        def refresh() -> None:
            if load_library_system(system_name) is None:
                raise JevError(f'no design system named "{system_name}" in the library')
            html = _render_showcase_html(system_name)
            (library_system_path(system_name) / "showcase.html").write_bytes(html.encode("utf-8"))

        try:
            await run_in_threadpool(refresh)
        except (JevError, NotFoundError, ValueError) as exc:
            self.flash(request, "error", str(exc))
            # The system page itself 404s for an unknown name, so the error
            # is shown on the library — the delete action does the same.
            return self.back("/design-systems")
        self.flash(
            request,
            "ok",
            f'regenerated the showcase for "{system_name}" from its stored tokens',
        )
        return self.back(to)

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
            return bundle_zip(
                target, system_name, tokens, business_elements=business_elements, context=context
            )

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

    def design_system_figma_page(self, request: Request) -> Response:
        """One library system's Figma page: link, export, granular edits, sync.

        The two-way binding lives here, not on the system page itself: the
        link state, the generated `use_figma` export script, the one-change
        edit form, and the sync-back from the file (Tier 0 reads; the POST
        actions below do the writes).
        """
        name = request.path_params["name"]
        try:
            validate_library_name(name)
        except ValueError:
            raise HTTPException(404, "no such design system") from None
        return self.page(
            request,
            "design_system_figma.html",
            lambda repo: _design_system_figma_view(repo, name),
        )

    async def design_system_figma_action(self, request: Request) -> Response:
        """The Figma page's POST actions: link, unlink, export, edit, sync.

        Every action re-renders the Figma page (with its script, snippet or
        sync diff) instead of redirecting, so the result is visible where it
        was asked for. Sync is the only network call; it runs in a thread,
        saves a new library version on change, and never deletes tokens.
        """
        name = request.path_params["name"]
        try:
            system_name = validate_library_name(name)
        except ValueError:
            raise HTTPException(404, "no such design system") from None
        form = await checked_form(request)
        action = (_text(form, "action") or "").strip()

        def view(**extra: Any) -> dict[str, Any]:
            with views.open_repo(self.settings.root) as repo:
                return _design_system_figma_view(repo, system_name, **extra)

        async def show(**extra: Any) -> Response:
            context = await run_in_threadpool(lambda: view(**extra))
            return self.render(request, "design_system_figma.html", **context)

        if action == "link":
            url = (_text(form, "figma_url") or "").strip()
            try:
                ref = figma_mod.parse_figma_url(url)
                updated = await run_in_threadpool(
                    lambda: update_library_meta(
                        system_name,
                        {"figma_file_url": ref.url, "figma_file_key": ref.file_key},
                    )
                )
            except figma_mod.FigmaError as exc:
                self.flash(request, "error", str(exc))
                return await show()
            if updated is None:
                self.flash(request, "error", f'no design system named "{system_name}"')
                return await show()
            self.flash(request, "ok", f"linked {system_name} to Figma file {ref.file_key}")
            return await show()

        if action == "unlink":
            await run_in_threadpool(
                lambda: update_library_meta(
                    system_name, {"figma_file_url": None, "figma_file_key": None}
                )
            )
            self.flash(request, "ok", f"unlinked {system_name} from Figma")
            return await show()

        if action == "export":

            def export_job() -> str:
                system = load_library_system(system_name)
                if system is None:
                    raise JevError(f'no design system named "{system_name}" in the library')
                meta = _library_meta(system_name)
                file_key = meta.get("figma_file_key")
                tokens = system.tokens if isinstance(system.tokens, dict) else {}
                return figma_mod.render_use_figma_script(
                    system_name,
                    tokens,
                    file_key=file_key if isinstance(file_key, str) else None,
                )

            try:
                script = await run_in_threadpool(export_job)
            except JevError as exc:
                self.flash(request, "error", str(exc))
                return await show()
            return await show(script=script)

        if action == "edit":
            kind = (_text(form, "kind") or "").strip()
            values = {key: value for key, value in form.items() if isinstance(value, str)}

            def edit_job() -> tuple[str, str]:
                edit = figma_mod.edit_from_form(kind, values)
                return figma_mod.render_use_figma_edit(edit), kind

            try:
                snippet, edit_kind = await run_in_threadpool(edit_job)
            except (figma_mod.FigmaError, ValueError) as exc:
                self.flash(request, "error", str(exc))
                return await show()
            self.flash(
                request,
                "ok",
                "edit script ready — run it with the Figma MCP server's use_figma, "
                "then verify with get_screenshot",
            )
            return await show(snippet=snippet, edit_kind=edit_kind)

        if action == "sync":

            def sync_job() -> dict[str, object]:
                system = load_library_system(system_name)
                if system is None:
                    raise JevError(f'no design system named "{system_name}" in the library')
                meta = _library_meta(system_name)
                file_key = meta.get("figma_file_key")
                if not isinstance(file_key, str) or not file_key:
                    raise JevError("link a Figma file first — paste its URL above")
                token = os.environ.get(figma_mod.TOKEN_ENV, "").strip()
                if not token:
                    raise JevError("no Figma token saved yet — add it on Tools → APIs first")
                with figma_mod.FigmaClient(token) as client:
                    file_json = client.get_file(file_key)
                imported = figma_mod.file_to_tokens(file_json)
                tokens = system.tokens if isinstance(system.tokens, dict) else {}
                diff = figma_mod.reconcile_tokens(tokens, imported.tokens)
                changed_view = {
                    section: {name: [old, new] for name, (old, new) in names.items()}
                    for section, names in diff.changed.items()
                }
                if diff.empty():
                    return {
                        "summary": "already in sync",
                        "added": {},
                        "changed": {},
                        "removed": {},
                        "version": system.version,
                        "applied": False,
                    }
                merged = figma_mod.apply_sync(tokens, diff)
                extras = {
                    key: value
                    for key, value in meta.items()
                    if key not in ("version", "updated_at")
                }
                extras["figma_last_sync"] = datetime.now(UTC).isoformat(timespec="seconds")
                version = save_library_system(
                    system_name, system.markdown, merged, meta_extra=extras
                )
                return {
                    "summary": diff.summary(),
                    "added": diff.added,
                    "changed": changed_view,
                    "removed": diff.removed,
                    "version": version,
                    "applied": True,
                }

            try:
                sync = await run_in_threadpool(sync_job)
            except (figma_mod.FigmaError, JevError, ValueError) as exc:
                self.flash(request, "error", str(exc))
                return await show()
            if sync.get("applied"):
                self.flash(
                    request,
                    "ok",
                    f"synced from Figma: {sync.get('summary')} — saved as v{sync.get('version')}",
                )
            else:
                self.flash(request, "ok", "already in sync with Figma — nothing changed")
            return await show(sync=sync)

        self.flash(request, "error", f"unknown Figma action {action!r}")
        return await show()

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
        if action == "showcase-refresh":
            return await self.design_system_showcase_refresh(request)
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
