"""`jev doctor`'s checks as a use case (issue #92).

`run_checks` inspects git state, config validity, the dispatcher, worker CLIs, API keys and
the agent tools each role is given. Nothing here prints or exits: problems are `Check`
rows, and a repo in the legacy (jao v0.1) layout is a `JevError`. The CLI (`cli.doctor_cmd`)
renders the table; the dashboard shows the rows on its doctor page.

The PATH/ssh/DNS indirections (`find`, `ssh`, `resolve`) exist so tests never run `ssh`
or touch the network; the CLI passes its own patchable ones through.
"""

from __future__ import annotations

import os
import queue
import shutil
import socket
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jev_agent.app.context import worker_readiness
from jev_agent.app.errors import JevError
from jev_agent.app.quality import missing_tools
from jev_agent.app.repo import LEGACY_MESSAGE, find_git_root, legacy_layout
from jev_agent.config import CONFIG_FILENAME, STATE_DIRNAME, ConfigError, JevConfig, load_config
from jev_agent.config.schema import (
    JevSettings,
    role_can_use_free,
    role_prefers_free,
    role_uses_free,
)
from jev_agent.core.learning import Learning, learning_status
from jev_agent.core.store import Store, StoreError
from jev_agent.roles import ROLE_SCHEMAS, PromptError, load_template
from jev_agent.runlog.redact import redact
from jev_agent.servers.access import WRITE_ROLE
from jev_agent.servers.audit import AuditEntry, AuditLog, audit_path
from jev_agent.servers.browser import PLAYWRIGHT_MCP_VERSION
from jev_agent.servers.profiles import repo_default
from jev_agent.servers.ssh import (
    SSH_COMMAND,
    SshRunner,
    SshTransport,
    control_dir,
    run_process,
    ssh_options,
)
from jev_agent.workers.openrouter_models import ModelCatalog, cache_path
from jev_agent.workers.tools import tools_for_role

# "note" is informational, like "ok", but points at something worth knowing.
Status = Literal["ok", "note", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def run_checks(
    repo: Path,
    *,
    connect: bool = False,
    find: Callable[[str], str | None] | None = None,
    ssh: SshRunner | None = None,
    resolve: Callable[[str], bool] | None = None,
) -> list[Check]:
    """Check git state, config, workers, learning, quality, models, prompts, tools, servers."""
    find = find or shutil.which
    ssh = ssh or run_process
    resolve = resolve or _resolve_host
    checks: list[Check] = []
    root = find_git_root(repo)
    if root is not None and legacy_layout(root):
        raise JevError(LEGACY_MESSAGE)
    if root is None:
        root = repo.resolve()
        checks.append(Check("git repo", "fail", f"{root} is not inside a git repository"))
    else:
        checks.append(Check("git repo", "ok", str(root)))

    try:
        config = load_config(root)
    except ConfigError as exc:
        checks.append(Check("config", "fail", str(exc)))
        config = None
    else:
        source = root / CONFIG_FILENAME
        detail = "valid" if source.exists() else f"valid (no {CONFIG_FILENAME}; using defaults)"
        checks.append(Check("config", "ok", detail))

    state_dir = root / STATE_DIRNAME
    if state_dir.is_dir():
        checks.append(Check(f"{STATE_DIRNAME}/", "ok", str(state_dir)))
    else:
        checks.append(Check(f"{STATE_DIRNAME}/", "warn", "missing; run `jev init`"))

    if config is not None:
        checks.append(_dispatcher_check(config))
        worker_checks = _worker_checks(config, ModelCatalog.load(cache_path(root)), find)
        checks.extend(worker_checks)
        checks.append(_learning_check(config, state_dir, worker_checks))
        checks.append(_quality_check(config, root, worker_checks))
        checks.extend(_free_model_checks(config, root))
        checks.extend(_prompt_checks(config, root))
        checks.extend(_tool_checks(config, root, find))
        checks.extend(_server_checks(config, state_dir, connect, find, ssh, resolve))
    return checks


# ---- servers, browser, profiles (docs/servers.md) -----------------------------------------


# The glibc resolver has no timeout of its own; on a dead DNS it can block for tens of
# seconds per host, so the lookup runs on a thread with a bounded wait (audit 2026-10-16).
_DNS_TIMEOUT_S = 5.0


def _resolve_host(host: str) -> bool:
    """Whether `host` resolves, with a bounded wait (the resolver itself can block)."""
    outcome: queue.Queue[bool] = queue.Queue(maxsize=1)

    def _lookup() -> None:
        try:
            socket.getaddrinfo(host, None)
        except (OSError, UnicodeError):
            outcome.put(False)
        else:
            outcome.put(True)

    thread = threading.Thread(target=_lookup, daemon=True, name="jev-doctor-dns")
    thread.start()
    try:
        return outcome.get(timeout=_DNS_TIMEOUT_S)
    except queue.Empty:
        return False


def _ssh_hostname(config: JevConfig, name: str, ssh: SshRunner) -> str | None:
    """What `ssh -G HOST` (config evaluation only, no connection) says the host name is."""
    server = config.servers[name]
    argv = [SSH_COMMAND, "-G", *ssh_options(server, None), "--", server.host]
    result = ssh(argv, stdin=None, timeout_s=10)
    if not result.ok:
        return None
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        key, _, value = line.partition(" ")
        if key == "hostname":
            return value.strip() or None
    return None


def _server_checks(
    config: JevConfig,
    state_dir: Path,
    connect: bool,
    find: Callable[[str], str | None],
    ssh: SshRunner,
    resolve: Callable[[str], bool],
) -> list[Check]:
    checks: list[Check] = []
    if config.servers:
        ssh_path = find(SSH_COMMAND)
        if ssh_path is None:
            checks.append(Check("ssh", "fail", "`ssh` not found on PATH; servers cannot be used"))
        else:
            checks.append(Check("ssh", "ok", ssh_path))
        for name in config.servers:
            checks.append(
                _server_check(
                    config, name, state_dir, connect and ssh_path is not None, find, ssh, resolve
                )
            )
    for role in ROLE_SCHEMAS:
        names = getattr(config.roles, role).servers
        if names and role != WRITE_ROLE:
            checks.append(
                Check(
                    f"role {role} servers",
                    "note",
                    f"{', '.join(names)}: capped at basic (read-only role)",
                )
            )
    checks.extend(_browser_checks(config, find))
    checks.append(_profiles_check(config, state_dir))
    return checks


def _server_check(
    config: JevConfig,
    name: str,
    state_dir: Path,
    connect: bool,
    find: Callable[[str], str | None],
    ssh: SshRunner,
    resolve: Callable[[str], bool],
) -> Check:
    server = config.servers[name]
    label = f"server {name}"
    about = f"access {server.access}"
    roots = server.write_roots()
    if server.access == "medium":
        about += f", writes in {', '.join(roots)}" if roots else ", no allowed_paths: no writes"
    if find(SSH_COMMAND) is None:
        return Check(label, "warn", f"{server.host}: {about}; ssh missing")
    hostname = _ssh_hostname(config, name, ssh)
    if hostname is None:
        return Check(label, "warn", f"{server.host}: `ssh -G` failed; {about}")
    via = f"{server.host} -> {hostname}" if hostname != server.host else server.host
    if not resolve(hostname):
        return Check(label, "warn", f"{via}: does not resolve (DNS); {about}")
    if not connect:
        return Check(label, "ok", f"{via}: resolves; {about} (`jev doctor --connect` logs in)")
    transport = SshTransport(server, sockets=control_dir(state_dir), runner=ssh)
    result = transport.check()
    AuditLog(audit_path(state_dir)).write(
        AuditEntry(
            run_id=None, role="doctor", server=name, access=server.access, action="connect",
            command="true", allowed=True, exit_code=result.exit_code,
            duration_s=result.duration_s,
        )
    )  # fmt: skip
    if result.ok:
        return Check(label, "ok", f"{via}: login works (BatchMode); {about}")
    lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
    err = redact(lines[-1] if lines else f"exit {result.exit_code}")
    hint = (
        " (host key unknown: connect once by hand to check and accept it)"
        if "host key" in err.lower()
        else ""
    )
    return Check(label, "fail", f"{via}: cannot log in: {err}{hint}")


def _browser_checks(config: JevConfig, find: Callable[[str], str | None]) -> list[Check]:
    roles = [r for r in ROLE_SCHEMAS if getattr(config.roles, r).browser]
    profiles = [n for n, p in config.profiles.items() if p.browser]
    if not (config.browser.enabled or roles or profiles):
        return []
    users = []
    if roles:
        users.append(f"roles: {', '.join(roles)}")
    if profiles:
        users.append(f"profiles (coder): {', '.join(profiles)}")
    used_by = "; ".join(users) or "no role has browser = true"
    if roles and not config.browser.enabled:
        return [Check("browser", "warn", f"{used_by}, but [browser] enabled = false")]
    npx = find("npx")
    if npx is None:
        return [Check("browser", "warn", f"`npx` not found on PATH (install Node.js); {used_by}")]
    mode = "headless" if config.browser.headless else "headed (needs a display)"
    return [
        Check(
            "browser",
            "ok",
            f"Playwright MCP {PLAYWRIGHT_MCP_VERSION}, {mode}; {used_by}; "
            "install Chromium with `jev browser install`",
        )
    ]


def _profiles_check(config: JevConfig, state_dir: Path) -> Check:
    default = repo_default(state_dir)
    if not config.profiles:
        if default:
            return Check("profiles", "warn", f".jev/profile names {default!r}, but none defined")
        return Check("profiles", "ok", "none defined")
    names = ", ".join(config.profiles)
    if default and default not in config.profiles:
        return Check("profiles", "warn", f"{names}; default {default!r} is not defined")
    return Check("profiles", "ok", f"{names}; default: {default or 'none'}")


def _tool_checks(config: JevConfig, root: Path, find: Callable[[str], str | None]) -> list[Check]:
    """Per role: its tools, and what its workers cannot use. Per tool in use: env vars set
    (names only, never values), `command` on PATH, skill and plugin paths present."""
    checks: list[Check] = []
    used_mcp: dict[str, list[str]] = {}
    for role in ROLE_SCHEMAS:
        role_config = getattr(config.roles, role)
        tools = tools_for_role(config, role, root)
        if not tools:
            continue
        for name in tools.mcp:
            used_mcp.setdefault(name, []).append(role)
        checks.append(Check(f"role {role} tools", "ok", tools.describe()))
        unusable: list[str] = []
        workers = [w for w in role_config.workers if config.workers.is_enabled(w)]
        if "codex" in workers:
            missing = [f"skill {s.name}" for s in tools.skills]
            missing += [f"plugin {p.name}" for p in tools.plugins]
            missing += [
                f"mcp {n} (SSE)" for n, m in tools.mcp.items() if m.url and m.transport != "http"
            ]
            if missing:
                unusable.append(f"codex cannot use {', '.join(missing)}")
        if "api" in workers:
            unusable.append("the api worker gets no tools")
        if unusable:
            checks.append(Check(f"role {role} tools", "note", "; ".join(unusable)))
        if role != "coder" and tools.mcp:
            checks.append(
                Check(
                    f"role {role} tools",
                    "note",
                    f"read-only role with MCP servers ({', '.join(tools.mcp)}): it can do "
                    "whatever those servers allow, writes included; see docs/security.md",
                )
            )
        for skill in tools.skills:
            if not (skill.path / "SKILL.md").is_file():
                checks.append(
                    Check(f"tools.skills.{skill.name}", "fail", f"no SKILL.md in {skill.path}")
                )
        for plugin in tools.plugins:
            if not plugin.path.exists():
                checks.append(
                    Check(f"tools.plugins.{plugin.name}", "fail", f"{plugin.path} does not exist")
                )
    for name, roles in used_mcp.items():
        checks.append(_mcp_check(config, name, roles, find))
    return checks


def _mcp_check(
    config: JevConfig, name: str, roles: list[str], find: Callable[[str], str | None]
) -> Check:
    server = config.tools.mcp[name]
    label = f"tools.mcp.{name}"
    problems: list[str] = []
    if server.command is not None and find(server.command) is None:
        problems.append(f"`{server.command}` not found on PATH")
    unset = [var for var in server.env_names() if not os.environ.get(var, "").strip()]
    if unset:
        problems.append(f"{', '.join(unset)} not set")
    if problems:
        return Check(label, "warn", "; ".join(problems))
    where = server.command or server.url or ""
    env = f"; {', '.join(server.env_names())} set" if server.env_names() else ""
    return Check(label, "ok", f"{where} (for {', '.join(roles)}){env}")


FREE_PRIVACY_NOTE = (
    "free models: providers serving them may log prompts or train on them; your OpenRouter "
    "privacy settings (https://openrouter.ai/settings/privacy) decide which providers may be "
    "used, separately for free and paid models. Prompts include your idea, plan and diffs."
)


def _free_model_checks(config: JevConfig, root: Path) -> list[Check]:
    """`[models] prefer_free`, the cached model list (no network: doctor only reads the cache),
    which roles would use free models, and the privacy note when any does."""
    roles = [name for name, _ in config.roles.items()]
    asking = [r for r in roles if role_uses_free(config, r)]
    using = [r for r in asking if role_can_use_free(config, r)]
    overrides = [
        f"{r}={'on' if role_prefers_free(config, r) else 'off'}"
        for r, rc in config.roles.items()
        if rc.prefer_free is not None
    ]
    catalog = ModelCatalog.load(cache_path(root))
    ttl_s = config.models.cache_ttl_hours * 3600
    if catalog is None:
        cache = "no model list cached (`jev models free --refresh`)"
    else:
        stale = ", stale" if catalog.is_stale(ttl_s) else ""
        cache = (
            f"{len(catalog.free())} free model(s) cached, "
            f"fetched {catalog.age_s() / 3600:.1f}h ago{stale}"
        )
    prefer = "on" if config.models.prefer_free else "off"
    detail = f"prefer_free {prefer}"
    if overrides:
        detail += f" (role overrides: {', '.join(overrides)})"
    detail += f"; {cache}; roles using free models: {', '.join(using) or 'none'}"
    status: Status = "ok"
    if using and catalog is None:
        status = "warn"
        detail += '; `"free"` expands to nothing until the list is cached'
    checks = [Check("free models", status, detail)]
    unable = [r for r in asking if r not in using]
    if unable:
        checks.append(
            Check(
                "free models",
                "note",
                f"{', '.join(unable)}: free models run only on the api worker, which "
                "these roles do not use; Claude Code and Codex keep their own models",
            )
        )
    if using:
        checks.append(Check("free models privacy", "note", FREE_PRIVACY_NOTE))
    return checks


def _prompt_checks(config: JevConfig, root: Path) -> list[Check]:
    """Each custom `prompt_file` must exist and parse as a Jinja2 template (else fail).

    Undefined variables only show when a run renders the template.
    """
    checks: list[Check] = []
    for role in ROLE_SCHEMAS:
        prompt_file = getattr(config.roles, role).prompt_file
        if prompt_file is None:
            continue
        path = prompt_file if prompt_file.is_absolute() else root / prompt_file
        try:
            load_template(role, path)
        except PromptError as exc:
            checks.append(Check(f"role {role} prompt", "fail", exc.detail + f": {path}"))
        else:
            checks.append(Check(f"role {role} prompt", "ok", str(path)))
    return checks


def _dispatcher_check(config: JevConfig) -> Check:
    """Which dispatcher decides, and whether either Jev key env var is set (never its value).

    Mirrors `context.build_dispatcher`'s two legs: the native TypeSafe API and Jev through
    OpenRouter. Only a missing key on *both* legs fails (consensus) or warns (jev): with
    one leg set the run decides fine (audit 2026-10-16: no more false "fail" on
    native-key-only setups)."""
    primary = config.dispatcher.primary
    if primary == "rules":
        return Check("dispatcher", "ok", "rules")
    jev = config.dispatcher.jev
    who = "consensus: Jev, then Laya" if primary == "consensus" else "jev"
    where = f"{who} ({jev.model} at {jev.base_url})"
    native_env = JevSettings.for_native().api_key_env  # "TYPESAFE_API_KEY"
    openrouter_env = jev.api_key_env
    native_set = bool(os.environ.get(native_env, "").strip())
    openrouter_set = bool(os.environ.get(openrouter_env, "").strip())
    if native_set and openrouter_set and native_env != openrouter_env:
        return Check("dispatcher", "ok", f"{where}; {native_env} and {openrouter_env} are set")
    if native_set:
        detail = f"{where}; {native_env} is set (native TypeSafe leg)"
        if not openrouter_set:
            detail += f"; no {openrouter_env} failover"
        return Check("dispatcher", "ok", detail)
    if openrouter_set:
        detail = f"{where}; {openrouter_env} is set (OpenRouter leg)"
        if not native_set:
            detail += f"; no {native_env} native leg"
        return Check("dispatcher", "ok", detail)
    legs = (
        f"neither {native_env} nor {openrouter_env}" if native_env != openrouter_env else native_env
    )
    if primary == "consensus":
        return Check(
            "dispatcher",
            "fail",
            f"{where}; {legs} is not set, so every run stops for a human at its "
            "first decision (set it via the dashboard Connections page or export it, or set "
            '`[dispatcher] primary = "rules"`)',
        )
    return Check(
        "dispatcher",
        "warn",
        f"{where}; {legs} is not set, so decisions fall back to the rules dispatcher "
        "(set it via the dashboard Connections page or export it)",
    )


def _learning_check(config: JevConfig, state_dir: Path, worker_checks: list[Check]) -> Check:
    """On/off and where that comes from, the active profile and its lesson count. Reads the
    repo settings only if the state database already exists (doctor never creates it)."""
    db_path = state_dir / "state.db"
    try:
        if db_path.is_file():
            with Store.open(db_path) as store:
                status = learning_status(store, config)
                learning = Learning.for_store(store)
                try:
                    profile = learning.get(status.profile)
                finally:
                    learning.close()
        else:
            status, profile = learning_status(None, config), None
    except (StoreError, sqlite3.Error) as exc:
        return Check("learning", "warn", f"cannot read learning settings: {exc}")
    if profile is None:
        about = f"profile {status.profile} (not created yet)"
    else:
        flag = "" if profile.enabled else ", disabled"
        about = f"profile {profile.name} ({profile.scope}{flag}, {profile.lessons} lesson(s))"
    if not status.enabled:
        return Check("learning", "ok", f"off ({status.enabled_source}); {about}")
    detail = f"on ({status.enabled_source}); {about}; each run ends with one learner call"
    learner_ok = any(c.name == "role learner" and c.status == "ok" for c in worker_checks)
    if not learner_ok:
        return Check("learning", "warn", f"{detail}, but the learner role has no usable worker")
    if profile is not None and not profile.enabled:
        return Check("learning", "warn", f"{detail}, but the profile is disabled")
    return Check("learning", "ok", detail)


def _quality_check(config: JevConfig, root: Path, worker_checks: list[Check]) -> Check:
    """The quality level and, for that level, which tools that apply here are missing."""
    level = config.quality.level
    if level == "none":
        return Check("quality", "ok", "level none (task test commands only)")
    detail = f"level {level}"
    reviewer = any(c.name == "role code_reviewer" and c.status == "ok" for c in worker_checks)
    if not reviewer:
        detail += "; no worker for the AI review"
    missing = list(dict.fromkeys(missing_tools(root, config, level)))
    required = [t for t in missing if t in config.quality.required_tools]
    if required:
        return Check("quality", "fail", f"{detail}; required tools missing: {', '.join(required)}")
    if missing:
        return Check(
            "quality",
            "warn",
            f"{detail}; not installed (their checks are skipped): {', '.join(missing)}; "
            "see `jev quality checks`",
        )
    if not reviewer:
        return Check("quality", "warn", detail)
    return Check("quality", "ok", f"{detail}; every tool for this level is installed")


def _permission_checks(config: JevConfig) -> list[Check]:
    """Warn about settings that let an agent act outside its worktree's guard rails."""
    checks: list[Check] = []
    claude, codex = config.workers.claude_code, config.workers.codex
    if claude.enabled and claude.skip_permissions:
        checks.append(
            Check(
                "claude_code permissions",
                "warn",
                "skip_permissions = true: the coder runs with --dangerously-skip-permissions "
                "and can run any command as you; use only in a throwaway sandbox",
            )
        )
    if codex.enabled and codex.sandbox == "danger-full-access":
        checks.append(
            Check(
                "codex sandbox",
                "warn",
                'sandbox = "danger-full-access": the coder runs without a sandbox; '
                "use only in a throwaway sandbox",
            )
        )
    for name, cli in (("claude_code", claude), ("codex", codex)):
        risky = [arg for arg in cli.extra_args if "dangerous" in arg.lower()]
        if cli.enabled and risky:
            checks.append(
                Check(
                    f"{name} extra_args", "warn", f"passes {', '.join(risky)}; see docs/security.md"
                )
            )
    return checks


def _worker_checks(
    config: JevConfig, catalog: ModelCatalog | None, find: Callable[[str], str | None]
) -> list[Check]:
    """Each worker and role, by the rule runs use (`app.context.worker_readiness`, issue #19)."""
    checks: list[Check] = []
    readiness = worker_readiness(config, catalog, find=find)
    workers = config.workers
    for name, cli in (("claude_code", workers.claude_code), ("codex", workers.codex)):
        label = f"worker {name}"
        if not cli.enabled:
            checks.append(Check(label, "ok", "disabled"))
        elif name in readiness.problems:
            checks.append(Check(label, "warn", readiness.problems[name]))
        else:
            checks.append(Check(label, "ok", readiness.paths[name]))

    checks.extend(_permission_checks(config))

    api = workers.api
    if not api.enabled:
        checks.append(Check("worker api", "ok", "disabled"))
    elif "api" in readiness.problems:
        checks.append(Check("worker api", "warn", readiness.problems["api"]))
    else:
        model = api.default_model or "none; roles use their `models`"
        checks.append(
            Check("worker api", "ok", f"{api.api_key_env} is set; default model: {model}")
        )
    usable = {n for n in workers.names() if workers.is_enabled(n) and readiness.ready(n)}

    for role_name, role in config.roles.items():
        candidates = [w for w in role.workers if w in usable and readiness.ready_for(role_name, w)]
        skipped = readiness.role_problems.get(role_name, {})
        if candidates:
            detail = ", ".join(candidates)
            detail += "".join(f"; {w} skipped: {why}" for w, why in skipped.items())
            checks.append(Check(f"role {role_name}", "ok", detail))
        elif role_name == "learner":  # only needed when learning is on
            checks.append(
                Check("role learner", "warn", "no usable worker; learning cannot add lessons")
            )
        elif role_name == "code_reviewer":  # only needed when the quality gate is on
            status: Status = "ok" if config.quality.level == "none" else "warn"
            checks.append(
                Check(
                    "role code_reviewer",
                    status,
                    "no usable worker; the quality gate's AI review will be skipped",
                )
            )
        else:
            detail = "no usable worker"
            detail += "".join(f"; {w} skipped: {why}" for w, why in skipped.items())
            checks.append(Check(f"role {role_name}", "fail", detail))

    if "codex" in usable and any("codex" in role.workers for _, role in config.roles.items()):
        checks.append(
            Check(
                "codex cost",
                "note",
                "Codex reports no cost, so its attempts count $0 toward the [budget] caps; "
                "they are bounded only by max_task_attempts and timeout_s. Set a spend limit "
                "with your OpenAI account too.",
            )
        )
    return checks
