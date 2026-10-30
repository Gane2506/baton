"""Workflow profile routes (issue #368, phases 2 and 5).

Phase 2 slice: export/import for repo-scope sharing (repo profiles do not
travel via git — `.jev/` is gitignored — so sharing is dashboard export
(download JSON) and import (upload JSON) here). Phase 5 adds the list,
editor (canvas plus list mode, one form, one server-side validator) and
detail pages. Suggestion request/result and approve/discard routes are
phase 6 work and deliberately do NOT exist here yet.

Import validates with the same validator before saving, and refuses with
the setting named on failure — never partially applied. The editor does
the same: the browser never validates alone (design section 14); every
save runs the phase-3 full validator server-side and a failed save lists
every error, not just the first. The editor can omit stages where allowed
and edit parameters, but there is NO reorder control: canvas dragging a
node to a new position is refused in the browser (see
`static/workflows_editor.js`), and a reordered stage list submitted any
other way is refused by the validator with the section 7.1 dependency.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from jev_agent.app.errors import JevError
from jev_agent.core.store import NotFoundError
from jev_agent.core.workflow_profiles import ProfileStore, ProfileValidationError
from jev_agent.core.workflow_profiles.presets import (
    PRESET_NAMES,
    load_preset,
    parse_preset_ref,
)
from jev_agent.core.workflow_profiles.resolver import build_validation_context
from jev_agent.core.workflow_profiles.schema import (
    CANONICAL_ORDER,
    GATE_POLICIES,
    ROLES,
    parse_profile_id,
    pretty_stage,
)
from jev_agent.core.workflow_profiles.validator import (
    ValidationContext,
    validate_profile,
)
from jev_agent.dashboard import views
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.helpers import _text
from jev_agent.dashboard.security import checked_form

# Live-validation subset (design section 13.2): while editing, the browser
# shows exactly schema parse, unknown names against current configuration,
# duplicate stages, and an empty list — never the full catalogue (an
# omitted required stage mid-edit is not a live error; it refuses at save).
# The live endpoint runs the full validator and returns only messages in
# this subset, so the browser never carries its own validation logic.
_LIVE_MARKERS = (
    "Unknown stage ",
    "Duplicate stage ",
    "A flow needs at least ",
    "Unknown model ",
    "Unknown role ",
    "Unknown quality level ",
    "Unknown planner policy ",
    "Unknown cache mode ",
    "Unknown learning profile ",
    "Unknown gate policy ",
    "Unknown field ",
)


def _live_errors(messages: list[str]) -> list[str]:
    return [m for m in messages if m.startswith(_LIVE_MARKERS)]


def _form_text(form: Mapping[str, Any], name: str) -> str | None:
    value = form.get(name)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _form_int(form: Mapping[str, Any], name: str) -> int | str | None:
    """An editor integer field: a parsed int, the raw text (so the validator
    refuses it with the setting named), or None when left blank."""
    raw = _form_text(form, name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def build_profile_from_editor(form: Mapping[str, Any]) -> dict[str, Any]:
    """Build profile-format JSON from one editor submission, in canonical
    stage order. Canvas mode and list mode submit the same form with the
    same field names (the mode only changes the layout — see
    `workflows_editor.html`), so the same selections produce identical
    JSON whichever mode the user was in (design section 13.2
    input-equivalence table: canvas drag-out = list toggle-off, canvas
    parameter edit = list field edit). Stage order here is always the
    canonical order — the form has no reorder input at all — so a
    reordered list can only arrive outside the editor, where the
    validator refuses it."""
    scope = _form_text(form, "scope") or "repo"
    slug = _form_text(form, "slug") or ""
    profile: dict[str, Any] = {
        "id": f"{scope}:{slug}",
        "display_name": _form_text(form, "display_name") or "",
        "scope": scope,
        "stage_list": [
            stage for stage in CANONICAL_ORDER if form.get(f"stage_{stage}") is not None
        ],
        "gate_policies": {
            stage: _form_text(form, f"gate_{stage}") or "inherit"
            for stage in CANONICAL_ORDER
            if stage != "INTAKE" and form.get(f"stage_{stage}") is not None
        },
        "designing": _form_text(form, "designing") or "when_needed",
        "origin": "user",
    }
    for key in ("max_plan_rounds", "max_fix_rounds", "score_max_retries", "max_task_attempts"):
        value = _form_int(form, key)
        if value is not None:
            profile[key] = value
    threshold = _form_int(form, "score_threshold")
    if threshold is not None:
        profile["score_threshold"] = threshold
    for key in (
        "quality_level",
        "planner_policy",
        "cache_mode",
        "cda_grading",
        "after_approval",
        "learning_profile",
        "goal",
        "priority",
    ):
        value = _form_text(form, key)
        if value is not None:
            profile[key] = value
    if form.get("score_skip_human") is not None:
        profile["score_skip_human"] = True
    if form.get("ship_enabled") is not None:
        profile["ship_enabled"] = True
    role_models = {role: _form_text(form, f"role_{role}") for role in ROLES}
    role_models = {role: model for role, model in role_models.items() if model}
    if role_models:
        profile["role_models"] = role_models
    return profile


def _editor_stages(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per canonical stage for the editor template: whether it is
    included, and its gate policy (INTAKE has no gate state, design
    section 7.2, so it carries no policy control)."""
    included = set(profile.get("stage_list") or [])
    policies = profile.get("gate_policies") or {}
    return [
        {
            "code": stage,
            "name": pretty_stage(stage),
            "included": stage in included,
            "gate": policies.get(stage, "inherit"),
        }
        for stage in CANONICAL_ORDER
    ]


def _blank_profile() -> dict[str, Any]:
    return {
        "display_name": "",
        "scope": "repo",
        "stage_list": list(CANONICAL_ORDER),
        "gate_policies": {stage: "inherit" for stage in CANONICAL_ORDER if stage != "INTAKE"},
        "designing": "when_needed",
        "ship_enabled": True,
        "after_approval": "ask",
    }


def _store(repo: views.Repo) -> ProfileStore:
    return ProfileStore(repo.root, Path.home() / ".jev")


def _context(repo: views.Repo) -> ValidationContext:
    return build_validation_context(
        repo.config,
        learning_profiles={p.name for p in repo.learning.profiles()},
    )


def _load_profile_or_preset(
    repo: views.Repo, profile_id: str
) -> tuple[dict[str, Any], bool, list[int]]:
    """A stored profile (current version, plus its version list) or a
    built-in preset (read-only: never edited in place, design section 6.2).
    Raises NotFoundError when neither resolves."""
    name = parse_preset_ref(profile_id)
    if name is not None and name in PRESET_NAMES:
        return load_preset(name), True, []
    store = _store(repo)
    try:
        return store.get(profile_id), False, store.versions(profile_id)
    except ValueError as exc:
        raise NotFoundError(f"no workflow profile {profile_id!r}") from exc


class WorkflowRoutes(DashboardCore):
    # ---- list ---------------------------------------------------------------

    def workflows_list_page(self, request: Request) -> Response:
        """List page (design section 13.1): built-in presets, repo-scope
        profiles, user-scope profiles, each with scope badge, current
        version, goal label, and stage chain."""

        def build(repo: views.Repo) -> dict[str, Any]:
            store = _store(repo)
            return {
                "presets": [load_preset(name) for name in PRESET_NAMES],
                "repo_profiles": store.list_profiles(scope="repo"),
                "user_profiles": store.list_profiles(scope="user"),
            }

        return self.page(request, "workflows_list.html", build)

    # ---- editor ---------------------------------------------------------------

    def workflows_new_page(self, request: Request) -> Response:
        """Editor for a new profile. `?preset=preset:<slug>` prefills the
        form from that preset (a copy under a new id — presets are never
        edited in place); otherwise the form starts from the full canonical
        stage list."""

        def build(repo: views.Repo) -> dict[str, Any]:
            profile = _blank_profile()
            preset_ref = request.query_params.get("preset")
            preset_name = parse_preset_ref(preset_ref) if preset_ref else None
            if preset_name is not None and preset_name in PRESET_NAMES:
                preset = load_preset(preset_name)
                profile = {k: v for k, v in preset.items() if k not in ("id", "scope", "release")}
                profile["display_name"] = f"{preset.get('display_name', preset_name)} copy"
                profile["scope"] = "repo"
            return {
                "profile": profile,
                "stages": _editor_stages(profile),
                "gate_policies": GATE_POLICIES,
                "roles": ROLES,
                "editing": False,
                "errors": [],
                "is_preset": False,
            }

        return self.page(request, "workflows_editor.html", build)

    async def workflows_create(self, request: Request) -> Response:
        """Save a new profile. The full validator runs server-side; a
        failure re-renders the editor with EVERY error listed (design
        section 13.2) and the submitted values kept — never a bare
        redirect that drops the form."""
        form: FormData = await checked_form(request)
        profile = build_profile_from_editor(form)
        return await self._save_profile(request, form, profile, editing=False)

    async def _save_profile(
        self,
        request: Request,
        form: FormData,
        profile: dict[str, Any],
        *,
        editing: bool,
    ) -> Response:
        """Shared create/edit save: validate-and-store via the repo, then
        redirect to the detail page, or re-render the editor with every
        error and the submitted values on failure."""
        errors: list[str] = []
        stored: dict[str, Any] | None = None

        def work() -> dict[str, Any]:
            with views.open_repo(self.settings.root) as repo:
                try:
                    return _store(repo).save(profile, context=_context(repo))
                except ProfileValidationError as exc:
                    raise JevError("\n".join(exc.messages)) from exc

        try:
            stored = await run_in_threadpool(work)
        except JevError as exc:
            errors = str(exc).split("\n") if str(exc) else ["could not save the profile"]
        if stored is not None:
            self.flash(
                request,
                "ok",
                f"saved workflow profile {stored['id']} version {stored['version']}",
            )
            return self.back(f"/workflows/{stored['id']}")
        return self.render(
            request,
            "workflows_editor.html",
            profile=profile,
            stages=_editor_stages(profile),
            gate_policies=GATE_POLICIES,
            roles=ROLES,
            editing=editing,
            errors=errors,
            is_preset=False,
        )

    def workflows_edit_page(self, request: Request) -> Response:
        """Editor for an existing stored profile, prefilled from its
        current version. A preset id lands here only by a stale link: it
        redirects to a preset-prefilled new profile instead of editing
        the preset in place."""
        profile_id: str = request.path_params["profile_id"]
        with views.open_repo(self.settings.root) as repo:
            _profile, is_preset, _versions = _load_profile_or_preset(repo, profile_id)
        if is_preset:
            self.flash(
                request,
                "error",
                f"preset {profile_id!r} cannot be edited in place — save it as a copy instead",
            )
            return self.back(f"/workflows/new?preset={profile_id}")

        def build(repo: views.Repo) -> dict[str, Any]:
            profile, _is_preset, _versions = _load_profile_or_preset(repo, profile_id)
            return {
                "profile": profile,
                "stages": _editor_stages(profile),
                "gate_policies": GATE_POLICIES,
                "roles": ROLES,
                "editing": True,
                "errors": [],
                "is_preset": False,
            }

        return self.page(request, "workflows_editor.html", build)

    async def workflows_save_edit(self, request: Request) -> Response:
        """Save an edit as a new version (design section 6.3: version n is
        never modified afterwards). The id and scope come from the URL's
        profile, not the form, so an edit cannot silently rename a profile
        into another scope."""
        profile_id: str = request.path_params["profile_id"]
        form: FormData = await checked_form(request)
        profile = build_profile_from_editor(form)
        parsed = parse_profile_id(profile_id)
        if parsed is not None:
            profile["id"] = profile_id
            profile["scope"] = parsed[0]
        return await self._save_profile(request, form, profile, editing=True)

    async def workflows_validate(self, request: Request) -> Response:
        """Live validation for the editor (design section 13.2): the
        browser posts a draft profile JSON and gets back ONLY the live
        subset of catalogue errors. The full catalogue still runs at
        save; this endpoint never replaces it, and no validation logic
        lives in the browser."""
        form: FormData = await checked_form(request)
        text = _text(form, "profile_json") or ""
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            return JSONResponse({"errors": [f"the profile JSON does not parse: {exc}"]})

        def check() -> list[str]:
            with views.open_repo(self.settings.root) as repo:
                return validate_profile(data, context=_context(repo))

        try:
            messages = await run_in_threadpool(check)
        except Exception:
            messages = validate_profile(data)
        return JSONResponse({"errors": _live_errors(messages)})

    # ---- detail ---------------------------------------------------------------

    def workflows_detail_page(self, request: Request) -> Response:
        """Detail page (design section 13.1): stage list as a chain,
        parameters, version history, and runs that snapshotted each
        version. Suggestion records land here in phase 6."""

        def build(repo: views.Repo) -> dict[str, Any]:
            profile_id: str = request.path_params["profile_id"]
            profile, is_preset, versions = _load_profile_or_preset(repo, profile_id)
            runs = [run for run in repo.store.list_runs() if run.workflow_profile_id == profile_id]
            return {
                "profile": profile,
                "is_preset": is_preset,
                "versions": versions,
                "runs": runs,
            }

        return self.page(request, "workflows_detail.html", build)

    # ---- import / export (phase 2, reused by the editor) ------------------------

    def workflows_import_page(self, request: Request) -> Response:
        """The import form, plus the repo-scope profiles already stored (each
        with its export link). The list page links here too."""

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
