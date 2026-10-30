"""Workflow profile sharing routes (issue #368, phase 2 slice).

Minimal export/import only — no editor, no picker, no suggestion UI around
them (those are phases 4-6). Repo-scope profiles only: repo profiles do
not travel via git (`.jev/` is gitignored), so sharing is dashboard
export (download JSON) and import (upload JSON) here. Import validates
with the same validator before saving, and refuses with the setting named
on failure — never partially applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from jev_agent.app.errors import JevError
from jev_agent.core.workflow_profiles import ProfileStore, ProfileValidationError
from jev_agent.core.workflow_profiles.resolver import build_validation_context
from jev_agent.core.workflow_profiles.schema import parse_profile_id
from jev_agent.dashboard import views
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.helpers import _text
from jev_agent.dashboard.security import checked_form


class WorkflowRoutes(DashboardCore):
    def workflows_import_page(self, request: Request) -> Response:
        """The import form, plus the repo-scope profiles already stored (each
        with its export link). No list/editor page exists yet — phase 5."""

        def build(repo: views.Repo) -> dict[str, Any]:
            store = ProfileStore(repo.root, Path.home() / ".jev")
            return {"profiles": store.list_profiles(scope="repo")}

        return self.page(request, "workflows_import.html", build)

    async def workflows_export(self, request: Request) -> Response:
        """Download one repo-scope profile's current version as JSON."""
        profile_id: str = request.path_params["profile_id"]
        parsed = parse_profile_id(profile_id)
        if parsed is None or parsed[0] != "repo":
            self.flash(request, "error", f"only repo-scope profiles export here: {profile_id!r}")
            return self.back("/workflows/import")

        def build() -> dict[str, object]:
            with views.open_repo(self.settings.root) as repo:
                store = ProfileStore(repo.root, Path.home() / ".jev")
                try:
                    return store.get(profile_id)
                except ValueError as exc:
                    raise JevError(str(exc)) from exc

        try:
            data = await run_in_threadpool(build)
        except JevError as exc:
            self.flash(request, "error", str(exc))
            return self.back("/workflows/import")
        filename = f"{parsed[1]}.json"
        return Response(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def workflows_import(self, request: Request) -> Response:
        """Import an uploaded (or pasted) profile JSON as a new repo-scope
        version. The same validator runs before saving; a failure refuses
        with the setting named and saves nothing."""
        form: FormData = await checked_form(request)
        text = _text(form, "profile_json") or ""
        upload = form.get("profile_file")
        if isinstance(upload, UploadFile) and upload.filename:
            try:
                raw = await upload.read()
            except Exception as exc:
                self.flash(request, "error", f"could not read the uploaded file: {exc}")
                return self.back("/workflows/import")
            if raw:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    self.flash(request, "error", "the uploaded file is not UTF-8 text")
                    return self.back("/workflows/import")
        if not text.strip():
            self.flash(request, "error", "paste profile JSON or upload a profile file")
            return self.back("/workflows/import")

        def save() -> str:
            with views.open_repo(self.settings.root) as repo:
                try:
                    data = json.loads(text)
                except ValueError as exc:
                    raise JevError(f"the profile JSON does not parse: {exc}") from exc
                if not isinstance(data, dict):
                    raise JevError("the profile JSON must be one profile object")
                parsed = parse_profile_id(str(data.get("id", "")))
                if parsed is None:
                    raise JevError(
                        "the profile JSON needs an id like 'repo:<slug>' or 'user:<slug>'"
                    )
                # This route imports into repo scope: a user-scope file lands
                # as the same slug under repo scope, as a new version.
                data = dict(data)
                data["scope"] = "repo"
                data["id"] = f"repo:{parsed[1]}"
                data.pop("version", None)
                data.pop("release", None)
                context = build_validation_context(
                    repo.config,
                    learning_profiles={p.name for p in repo.learning.profiles()},
                )
                store = ProfileStore(repo.root, Path.home() / ".jev")
                try:
                    stored = store.save(data, context=context)
                except ProfileValidationError as exc:
                    raise JevError(str(exc)) from exc
                return f"imported workflow profile {stored['id']} version {stored['version']}"

        try:
            message = await run_in_threadpool(save)
        except (JevError, ValueError) as exc:
            self.flash(request, "error", str(exc))
            return self.back("/workflows/import")
        self.flash(request, "ok", message)
        return self.back("/workflows/import")
