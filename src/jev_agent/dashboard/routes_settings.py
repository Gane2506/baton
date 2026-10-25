"""SettingsRoutes: Settings, diagnostics, agents, tools, spend and KPIs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import (
    Response,
)

from jev_agent.app import settings
from jev_agent.app.errors import JevError
from jev_agent.app.models import fetch_catalog
from jev_agent.config import ConfigError
from jev_agent.config.schema import BEGINNER_FIELDS, CacheSemanticSettings
from jev_agent.core.benchmarks import DEFAULT_LAST
from jev_agent.core.cache import CalibrationResult, calibrate_thresholds
from jev_agent.core.ship import (
    Gh,
    ShipError,
    prs_to_sync,
)
from jev_agent.dashboard import ui_styles, views
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.errors import friendly_error as _friendly_error
from jev_agent.dashboard.helpers import (
    _back_target,
    _record_ship_sync,
    _text,
)
from jev_agent.dashboard.nav import _age, _time
from jev_agent.dashboard.security import (
    checked_form,
)
from jev_agent.quality.levels import LEVEL_SUMMARY


def _cache_clear_filters(form: FormData) -> tuple[str | None, float | None, str]:
    """The clear form's filters, validated like the CLI's (issue #223): a bad kind
    is an error, never a silent clear of everything. Returns the store kind
    (`"decision"`/`"role"`/None), the age filter in hours, and the form kind
    (`"decisions"`/`"roles"`/`""`) for the confirmation page."""
    kind = _text(form, "kind") or ""
    if kind not in ("", "decisions", "roles"):
        raise ValueError('--type must be "decisions" or "roles"')
    raw = _text(form, "older_than") or ""
    older_than: float | None = None
    if raw:
        try:
            older_than = float(raw)
        except ValueError:
            raise ValueError(f"older-than hours must be a number, got {raw!r}") from None
        if older_than < 0:
            raise ValueError("older-than hours must be 0 or more")
    store_kind = {"decisions": "decision", "roles": "role"}.get(kind) if kind else None
    return store_kind, older_than, kind


class SettingsRoutes(DashboardCore):
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
        # Test seam: tests monkeypatch these on the app module; read at call time.
        from jev_agent.dashboard.app import check_gh, sync_pr

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

    def tools_page(self, request: Request) -> Response:
        def extra(repo: views.Repo) -> dict[str, Any]:
            listing, warning = views.model_listing(repo)
            return {
                "models_listing": listing,
                "models_warning": warning,
                "cache_view": views.cache_view(repo),
                "quality_rows": views.quality_checks(repo),
                "level_summaries": LEVEL_SUMMARY,
            }

        return self._settings_page(request, "tools", "tools.html", extra)

    async def cache_clear(self, request: Request) -> Response:
        """Two steps (Tier 2), like `queue_clear`: the first POST counts what the
        filters would delete and shows it on a confirmation page; only
        `confirm=yes` clears (clearing is destructive and cannot be undone)."""
        form = await checked_form(request)
        to = "/tools#cache"
        try:
            store_kind, older_than, kind = _cache_clear_filters(form)
        except ValueError as exc:
            self.flash(request, "error", _friendly_error(exc))
            return self.back(to)
        if form.get("confirm") != "yes":

            def count() -> int:
                with views.open_repo(self.settings.root) as repo:
                    return repo.store.cache_count(kind=store_kind, older_than_hours=older_than)

            try:
                matching = await run_in_threadpool(count)
            except (JevError, ConfigError) as exc:
                self.flash(request, "error", _friendly_error(exc))
                return self.back(to)
            if matching == 0:
                self.flash(request, "ok", "the cache is already empty for those filters")
                return self.back(to)
            return self.render(
                request,
                "confirm_cache_clear.html",
                matching=matching,
                kind=kind,
                older_than=older_than,
            )

        def clear() -> int:
            with views.open_repo(self.settings.root) as repo:
                return repo.store.cache_clear(kind=store_kind, older_than_hours=older_than)

        try:
            deleted = await run_in_threadpool(clear)
        except (JevError, ConfigError) as exc:
            self.flash(request, "error", _friendly_error(exc))
        else:
            noun = "entry" if deleted == 1 else "entries"
            self.flash(request, "ok", f"cleared {deleted} cache {noun}")
        return self.back(to)

    async def cache_calibrate(self, request: Request) -> Response:
        """`jev cache calibrate` behind a button: run the pairwise-SimHash curve over
        stored decision history and show the table, the candidate threshold (if any)
        and the one-click apply for it. Read-only: nothing is served or configured
        until the user applies a threshold and turns serving on."""
        await checked_form(request)
        to = "/tools#cache"

        def job() -> tuple[CalibrationResult, CacheSemanticSettings]:
            with views.open_repo(self.settings.root) as repo:
                return calibrate_thresholds(repo.store), repo.config.cache.semantic

        try:
            result, semantic = await run_in_threadpool(job)
        except (JevError, ConfigError) as exc:
            self.flash(request, "error", _friendly_error(exc))
            return self.back(to)
        except Exception as exc:  # issue #200: a failed calibration flashes, never 500s
            self.flash(request, "error", _friendly_error(exc))
            return self.back(to)
        return self.render(
            request,
            "cache_calibrate.html",
            calibration=result,
            semantic=semantic,
        )

    async def cache_apply_threshold(self, request: Request) -> Response:
        """Apply a calibration candidate: set `[cache.semantic] decision_threshold`
        in the repo (or user) config, through the same validated `settings.save`
        the settings editor uses — including its trust gate. Serving still needs
        `mode = "serve"` under Settings; this only moves the floor."""

        def apply(repo: views.Repo, form: FormData) -> str:
            raw = _text(form, "threshold") or ""
            try:
                threshold = float(raw)
            except ValueError:
                raise ValueError(
                    f"threshold must be a number between 0 and 1, got {raw!r}"
                ) from None
            if not 0.0 <= threshold <= 1.0:
                raise ValueError("threshold must be between 0 and 1")
            target = _text(form, "target") or "repo"
            error = settings.save(
                self.settings.root,
                target,
                lambda layer: settings.set_path(
                    layer, "cache.semantic.decision_threshold", threshold
                ),
            )
            if error:
                if settings.TRUST_GATE_MARKER in error:
                    return error  # act() flashes it as a one-click trust offer
                raise JevError(error)
            return f"set cache.semantic.decision_threshold to {threshold:.2f} ({target})"

        return await self.act(request, "/tools#cache", apply)

    async def models_refresh(self, request: Request) -> Response:
        """Refresh the OpenRouter model catalog from the browser (issue #251):
        the dashboard never fetches on render, but this user-initiated POST may.
        A failed fetch keeps the stale cache and reports its warning; with no
        cache at all the warning is the error.
        """

        def refresh(repo: views.Repo, form: FormData) -> str:
            catalog, warning = fetch_catalog(repo.root, repo.config, refresh=True)
            if catalog is None:
                raise JevError(warning or "could not fetch the model catalog")
            note = f"refreshed the model catalog ({len(catalog.models)} models)"
            return note if not warning else f"{note} — {warning}"

        return await self.act(request, "/tools", refresh)

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
        # Trust gate blocked the save: offer one-click trust (issue #158).
        if error and settings.TRUST_GATE_MARKER in error:
            clean = error.replace(settings.TRUST_GATE_MARKER, "").strip()
            self.flash(request, "trust", clean)
        else:
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
        return_to = _text(form, "return_to") or ""
        if return_to.startswith("/") and not return_to.startswith("//") and "\\" not in return_to:
            # Same-origin path the posting page asked to land on (e.g. /profiles);
            # never an absolute or protocol-relative URL.
            to = return_to
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
            if len(keys) != len(kinds):
                # An empty kinds field is one empty string, not zero kinds: only a
                # key-less entry may have it. Anything else is a mismatched pair of
                # lists, and silently truncating (zip strict=False) would drop fields.
                if keys or kinds != [""]:
                    return (
                        f"keys and kinds do not match ({len(keys)} keys, {len(kinds)} kinds); "
                        "give one kind per key"
                    )
                kinds = []
            entry: dict[str, Any] = {}
            for key, entry_kind in zip(keys, kinds, strict=True):
                try:
                    value = settings.parse_value(entry_kind, values.get(f"f_{key}", ""), values)
                except ValueError as exc:
                    return f"{key}: {exc}"
                if value is not settings.UNSET:
                    entry[key] = value

            def add(layer: dict[str, Any]) -> None:
                # The duplicate check lives here, not in the write closure:
                # save() validates from `mutate` and ignores the closure, so
                # a check there never ran and a duplicate silently overwrote
                # the existing entry (issue #197). A ValueError from mutate
                # becomes save()'s error string.
                if settings.entity_exists(layer, section, name):
                    raise ValueError(f"{section}.{name} already exists")
                settings.set_path(layer, f"{section}.{name}", entry)

            return settings.save(
                root,
                target,
                add,
                lambda: settings.add_entity(file, section, name, entry),
            )

        error = await run_in_threadpool(job)
        # Trust gate blocked the save: offer one-click trust (issue #158).
        if error and settings.TRUST_GATE_MARKER in error:
            clean = error.replace(settings.TRUST_GATE_MARKER, "").strip()
            self.flash(request, "trust", clean)
        else:
            self.flash(request, "error" if error else "ok", error or f"saved {section}.{name}")
        return self.back(to)
