"""ConnectionsRoutes: Connections, servers, providers, browser, fleet and profiles."""

from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    Response,
)

from jev_agent.app import profiles as app_profiles
from jev_agent.app import settings
from jev_agent.app.errors import JevError
from jev_agent.config import ConfigError
from jev_agent.config.loader import trusts
from jev_agent.core.provider_auth import (
    CLAUDE_OAUTH_TOKEN_ENV,
    check_claude_oauth_token,
)
from jev_agent.core.store import (
    NotFoundError,
)
from jev_agent.dashboard import views
from jev_agent.dashboard.browser_install import (
    BrowserInstallBusy,
    BrowserInstallError,
)
from jev_agent.dashboard.connection_status import (
    _conn_aggregate,
    _connection_servers,
    _dispatcher_route,
    _refresh_servers,
)
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.errors import friendly_error as _friendly_error
from jev_agent.dashboard.helpers import (
    _text,
)
from jev_agent.dashboard.security import (
    checked_form,
)
from jev_agent.servers.profiles import ProfileError


class ConnectionsRoutes(DashboardCore):
    def fleet_page(self, request: Request) -> Response:
        """The six-role roster plus every agent working right now (`templates/fleet.html`).

        The roster is static config (not part of the live poll), so the addendum
        textareas are never reset by the auto-refresh; only `_fleet.html` polls.
        """

        def build(repo: views.Repo) -> dict[str, Any]:
            agents = views.fleet(repo)
            return {"agents": agents, "roles": views.role_roster(repo.config, agents)}

        return self.page(request, "fleet.html", build)

    async def fleet_addendum(self, request: Request) -> Response:
        """Save one role's additional prompt instructions (POST), back to its roster card.

        Follows the settings editor's save path (`settings.save` on the repo's
        jev.toml): the merged config must validate, and an empty value removes the key
        so the role falls back to the default (no addendum).
        """
        form = await checked_form(request)
        role = _text(form, "role") or ""
        to = f"/fleet#role-{role}" if role in views.ROLE_NAMES else "/fleet"

        def job() -> str | None:
            if role not in views.ROLE_NAMES:
                return f"unknown role {role!r}"
            path = f"roles.{role}.prompt_addendum"
            value = str(form.get("value", "") or "")
            root = self.settings.root
            try:
                file = settings.target_file(root, "repo")
            except ValueError as exc:
                return str(exc)
            if not value.strip():
                return settings.save(
                    root,
                    "repo",
                    lambda layer: settings.remove_path(layer, path),
                    lambda: settings.unset_value(file, path),
                )
            return settings.save(
                root,
                "repo",
                lambda layer: settings.set_path(layer, path, value),
                lambda: settings.write_value(file, path, value),
            )

        error = await run_in_threadpool(job)
        # Trust gate blocked the save: offer one-click trust (issue #158).
        # prompt_addendum is a guarded key (issue #194), so an untrusted repo
        # hits this path; the trust flash renders the "Trust this repo" button.
        if error and settings.TRUST_GATE_MARKER in error:
            clean = error.replace(settings.TRUST_GATE_MARKER, "").strip()
            self.flash(request, "trust", clean)
        else:
            self.flash(
                request,
                "error" if error else "ok",
                error or f"saved the additional prompt for {role}",
            )
        return self.back(to)

    def profiles_page(self, request: Request) -> Response:
        """`jev profile list` / `show`: the workspace profiles, the repo default and what
        each role gets from each one (Tier 0)."""

        name = request.query_params.get("name")

        def build(repo: views.Repo) -> dict[str, Any]:
            detail = None
            sources = app_profiles.profile_sources(repo.root)
            if name is not None:
                try:
                    detail = app_profiles.describe_profile(
                        repo.config, repo.state_dir, name, sources
                    )
                except ProfileError as exc:
                    raise NotFoundError(str(exc)) from exc
            repo_raw, user_raw, _ = settings.read_layers(repo.root)
            profile_add = next(
                (
                    e
                    for e in settings.entity_sections(repo.config, repo_raw, user_raw)
                    if e.path == "profiles"
                ),
                None,
            )
            return {
                "profiles": app_profiles.list_profiles(repo.config, repo.state_dir, sources),
                "detail": detail,
                "profile_add": profile_add,
            }

        return self.page(request, "profiles.html", build)

    async def profiles_use(self, request: Request) -> Response:
        """`jev profile use` (Tier 1): make a profile the repo default, or clear it."""
        form = await checked_form(request)
        name = _text(form, "profile")

        def use() -> str:
            with views.open_repo(self.settings.root) as repo:
                if not name or name == "none":
                    app_profiles.set_default_profile(repo.config, repo.state_dir, None)
                    return "no default workspace profile: runs use none unless --profile is given"
                app_profiles.set_default_profile(repo.config, repo.state_dir, name)
                return f"default workspace profile: {name}"

        try:
            message = await run_in_threadpool(use)
        except (JevError, ConfigError, ProfileError) as exc:
            self.flash(request, "error", _friendly_error(exc))
        else:
            self.flash(request, "ok", message)
        return self.back("/profiles")

    def fleet_fragment(self, request: Request) -> Response:
        """The fleet table alone, for live.js polling and `JevShell.mountPane("terminal",
        "/fleet/live", ms)` (see the hook documented in `templates/fleet.html`)."""
        return self.page(request, "_fleet.html", lambda repo: {"agents": views.fleet(repo)})

    def connections_page(self, request: Request) -> Response:
        """The full panel: cached per-server reachability, the dispatcher route and key
        health, then the connections settings editor (as before).

        Reads the audit tail only; rendering the page never opens an SSH connection."""
        # Test seam: tests monkeypatch these on the app module; read at call time.
        from jev_agent.dashboard.app import ssh_runner

        def build(repo: views.Repo) -> dict[str, Any]:
            repo_raw, user_raw, _user_file = settings.read_layers(repo.root)
            _, _, secs, ents = settings.page_view(repo.config, repo_raw, user_raw, "connections")
            servers = _connection_servers(repo.config, repo.state_dir)
            state, label = _conn_aggregate(servers)
            # Server health stats: cached, never blocks rendering. Kick off a
            # background collection for servers that have never been sampled.
            server_stats: dict[str, dict[str, Any]] = {}
            for s in servers:
                stats, st = self.server_stats.get(repo.state_dir, s.name)
                if st == "never" and s.state != "unreachable":
                    cfg = repo.config.servers.get(s.name)
                    if cfg is not None:
                        self.server_stats.refresh(s.name, cfg, repo.state_dir, ssh_runner)
                        st = "collecting"
                server_stats[s.name] = {"stats": stats, "state": st}
            return {
                "servers": servers,
                "agg_state": state,
                "agg_label": label,
                "route": _dispatcher_route(repo.config),
                "providers": views.cli_providers(repo.config),
                "codex_flow": self.codex_flow.status(),
                "claude_flow": self.claude_flow.status(),
                "env_vars": views.env_vars(repo.config),
                "key_names": views.env_var_names(repo.config),
                "browser_check": views.browser_check(repo),
                "browser_install": self.browser_installer.status(),
                "server_stats": server_stats,
                # the settings editor, unchanged from the old connections page
                "sections": secs,
                "entities": ents,
                "user_only": settings.USER_ONLY,
                "settings": self.settings,
                "repo_trusted": trusts(repo.config, repo.root),
            }

        return self.page(request, "connections.html", build)

    def connections_live(self, request: Request) -> Response:
        """The sidebar slot's poll target: cached reachability as JSON.

        Key names and set/unset only, never values. The browser being offline is decided
        client-side in conn.js/app.js."""

        try:
            with views.open_repo(self.settings.root) as repo:
                servers = _connection_servers(repo.config, repo.state_dir)
        except (ConfigError, JevError) as exc:
            return JSONResponse({"ok": False, "error": _friendly_error(exc)}, status_code=500)
        state, label = _conn_aggregate(servers)
        flow = self.codex_flow.status()
        return JSONResponse(
            {
                "ok": True,
                "state": state,  # "ok" | "degraded"
                "label": label,
                "total": len(servers),
                "reachable": sum(s.state == "reachable" for s in servers),
                "unreachable": sum(s.state == "unreachable" for s in servers),
                "unknown": sum(s.state == "unknown" for s in servers),
                "providers": [
                    {
                        "provider": p.provider,
                        "auth": p.auth,
                        "label": p.label,
                        "installed": p.installed,
                    }
                    for p in views.cli_providers(repo.config)
                ],
                # The device flow's URL/code are single-use and already shown on
                # the page; only the state travels here so the card can update.
                "codex_flow": {"state": flow.state, "detail": flow.detail},
                "servers": [
                    {
                        "name": s.name,
                        "host": s.host,
                        "access": s.access,
                        "state": s.state,
                        "last_check": s.last_check.isoformat() if s.last_check else None,
                    }
                    for s in servers
                ],
            }
        )

    async def connections_refresh(self, request: Request) -> Response:
        """Explicit re-check (POST): one real `ssh HOST true` per server, then back to the
        panel. Each result lands in the audit log, which is what the panel reads."""
        # Test seam: tests monkeypatch these on the app module; read at call time.
        from jev_agent.dashboard.app import ssh_runner

        def refresh(repo: views.Repo, form: FormData) -> str:
            return _refresh_servers(repo.config, repo.state_dir, ssh_runner)

        return await self.act(request, "/connections", refresh)

    async def server_stats_refresh(self, request: Request) -> Response:
        """Re-collect one server's health stats in the background (POST)."""
        # Test seam: tests monkeypatch these on the app module; read at call time.
        from jev_agent.dashboard.app import ssh_runner

        def refresh(repo: views.Repo, form: FormData) -> str:
            name = request.path_params.get("name", "")
            cfg = repo.config.servers.get(name)
            if cfg is None:
                return f"unknown server {name!r}"
            if self.server_stats.refresh(name, cfg, repo.state_dir, ssh_runner):
                return f"collecting stats for {name}…"
            return f"already collecting stats for {name}"

        return await self.act(request, "/connections", refresh)

    async def connections_key_add(self, request: Request) -> Response:
        """Save a key to ~/.jev/secrets.env (POST): it applies to this process at
        once, so the page shows "Set" without a restart. The value travels only in
        the POST body and is never rendered, logged, or flashed back."""

        def add(repo: views.Repo, form: FormData) -> str:
            from jev_agent.core.secrets import write_secret

            name = str(form.get("name", "")).strip()
            value = str(form.get("value", ""))
            write_secret(name, value)
            return f"{name} saved to the secrets file; it is in effect now."

        return await self.act(request, "/connections", add)

    async def connections_key_remove(self, request: Request) -> Response:
        """Two steps (Tier 2), like `design_system_delete`: the first POST shows a
        confirmation page; only `confirm=yes` deletes the secrets-file key. Keys
        from the real environment cannot reach this path: the template only
        offers removal for file-backed keys. The value is never rendered."""

        form = await checked_form(request)
        name = str(form.get("name", "")).strip()
        if form.get("confirm") != "yes":

            def describe() -> str:
                from jev_agent.core.secrets import read_secrets

                if name not in read_secrets():
                    raise JevError(f"{name} is not in the secrets file")
                return name

            try:
                key_name = await run_in_threadpool(describe)
            except (JevError, ValueError) as exc:
                self.flash(request, "error", _friendly_error(exc))
                return self.back("/connections")
            return self.render(request, "confirm_key_remove.html", name=key_name)

        def remove() -> None:
            from jev_agent.core.secrets import delete_secret

            delete_secret(name)

        try:
            await run_in_threadpool(remove)
        except (JevError, ValueError) as exc:
            self.flash(request, "error", _friendly_error(exc))
            return self.back("/connections")
        self.flash(request, "ok", f"{name} removed from the secrets file.")
        return self.back("/connections")

    async def connections_trust_repo(self, request: Request) -> Response:
        """Trust the current repo for guarded config keys (POST, issue #158).

        One-click trust: adds the repo to `[trust] repos` in the user config.
        The `next` form field controls where to return (defaults to /connections).
        """

        def trust(repo: views.Repo, form: FormData) -> str:
            err = settings.trust_repo(self.settings.root)
            if err:
                # Strip the marker for display; the button already handled it.
                return err.replace(settings.TRUST_GATE_MARKER, "").strip()
            return f"Trusted {self.settings.root}; repo config changes now apply."

        back = str(request.query_params.get("next", "/connections"))
        # Only allow relative paths to avoid open redirects.
        if not back.startswith("/"):
            back = "/connections"
        return await self.act(request, back, trust)

    async def claude_oauth_token_save(self, request: Request) -> Response:
        """Save a pasted `claude setup-token` OAuth token to the secrets file (POST).

        The token travels only in the POST body and is never rendered, logged, or
        flashed back — same guarantees as the generic key form. It lands in this
        process's environment at once, so the provider card flips to "Subscription
        login (OAuth)" without a restart, and the next `claude` worker run sees it.
        """

        def save(repo: views.Repo, form: FormData) -> str:
            from jev_agent.core.secrets import write_secret

            value = str(form.get("value", "")).strip()
            check_claude_oauth_token(value)  # ValueError -> flashed, nothing saved
            write_secret(CLAUDE_OAUTH_TOKEN_ENV, value)
            return "Claude subscription token saved to the secrets file; it is in effect now."

        return await self.act(request, "/connections", save)

    async def claude_setup_token_start(self, request: Request) -> Response:
        """Start the 1-click Claude subscription sign-in (POST): spawns
        `claude setup-token` in the background. The card shows the authorize
        URL the CLI prints; the user approves in their browser, pastes the
        browser code into the card, and the captured token is saved to the
        secrets file automatically."""

        def start(repo: views.Repo, form: FormData) -> str:
            self.claude_flow.start(command=repo.config.workers.claude_code.command)
            return (
                "Claude sign-in started — open the link on the Claude card, "
                "approve in your browser, then paste the browser code."
            )

        return await self.act(request, "/connections", start)

    async def claude_setup_token_cancel(self, request: Request) -> Response:
        """Cancel a pending Claude setup-token sign-in (POST)."""

        def cancel(repo: views.Repo, form: FormData) -> str:
            if self.claude_flow.cancel():
                return "Claude sign-in cancelled; no credentials were stored."
            return "No Claude sign-in was in progress."

        return await self.act(request, "/connections", cancel)

    async def claude_setup_token_code(self, request: Request) -> Response:
        """Forward the browser code to the pending `claude setup-token` (POST)."""

        def submit(repo: views.Repo, form: FormData) -> str:
            code = str(form.get("code", "")).strip()
            if not code:
                raise ValueError("paste the code your browser showed")
            if self.claude_flow.submit_code(code):
                return "Code sent — the sign-in will complete automatically."
            raise ValueError("no Claude sign-in is waiting for a code right now")

        return await self.act(request, "/connections", submit)

    async def codex_device_start(self, request: Request) -> Response:
        """Start the ChatGPT device sign-in (POST): spawns `codex login
        --device-auth` in the background. The card shows the URL + one-time code
        the CLI prints; the user finishes in their browser while the dashboard
        polls for completion. Nothing secret is stored by jev itself — the CLI
        keeps its own `~/.codex/auth.json`."""

        def start(repo: views.Repo, form: FormData) -> str:
            self.codex_flow.start(command=repo.config.workers.codex.command)
            return (
                "Codex sign-in started — open the link on the Codex card "
                "and enter the code in your browser."
            )

        return await self.act(request, "/connections", start)

    async def codex_device_cancel(self, request: Request) -> Response:
        """Cancel a pending ChatGPT device sign-in (POST)."""

        def cancel(repo: views.Repo, form: FormData) -> str:
            if self.codex_flow.cancel():
                return "Codex sign-in cancelled; no credentials were stored."
            return "No Codex sign-in was in progress."

        return await self.act(request, "/connections", cancel)

    async def browser_install(self, request: Request) -> Response:
        """Two steps (Tier 2: it downloads and runs an installer): the first POST names
        the exact command on a confirmation page; only `confirm=yes` starts it, once."""
        form = await checked_form(request)
        to = "/connections"
        status = self.browser_installer.status()
        if form.get("confirm") != "yes":
            if status.state == "running":
                self.flash(request, "error", "a browser install is already running")
                return self.back(to)
            return self.render(request, "confirm_browser_install.html", install=status)
        try:
            await run_in_threadpool(self.browser_installer.start)
        except BrowserInstallBusy as exc:
            self.flash(request, "error", str(exc))
        except BrowserInstallError as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", "browser install started; its progress shows below")
        return self.back(to)

    async def browser_install_stop(self, request: Request) -> Response:
        """Stop the running browser install (Tier 1)."""
        await checked_form(request)
        if self.browser_installer.stop():
            self.flash(request, "ok", "stopping the browser install")
        else:
            self.flash(request, "error", "no browser install is running")
        return self.back("/connections")
