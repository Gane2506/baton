"""`jev doctor`: check git state, config validity, the dispatcher, worker CLIs, API keys and the
agent tools (MCP servers, skills, plugins) each role is given."""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.cli.quality_cmds import missing_tools
from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.config import CONFIG_FILENAME, STATE_DIRNAME, ConfigError, JevConfig, load_config
from jev_agent.config.schema import role_can_use_free, role_prefers_free, role_uses_free
from jev_agent.core.learning import Learning, learning_status
from jev_agent.core.store import Store, StoreError
from jev_agent.roles import ROLE_SCHEMAS, PromptError, load_template
from jev_agent.workers.openrouter_models import ModelCatalog, cache_path
from jev_agent.workers.tools import tools_for_role

console = Console()

# "note" is informational, like "ok", but points at something worth knowing.
Status = Literal["ok", "note", "warn", "fail"]
_STYLE: dict[Status, str] = {"ok": "green", "note": "cyan", "warn": "yellow", "fail": "red"}

# Indirection so tests can fake PATH lookups without real CLIs installed.
which: Callable[[str], str | None] = shutil.which


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def doctor(
    repo: RepoOption = Path("."),
) -> None:
    """Check git state, config validity, worker CLIs and API keys."""
    checks = run_checks(repo)
    table = Table(title="jev doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for check in checks:
        style = _STYLE[check.status]
        table.add_row(
            escape(check.name), f"[{style}]{check.status}[/{style}]", escape(check.detail)
        )
    console.print(table)
    if any(check.status == "fail" for check in checks):
        raise typer.Exit(1)


def run_checks(repo: Path) -> list[Check]:
    checks: list[Check] = []
    root = find_git_root(repo)
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
        worker_checks = _worker_checks(config)
        checks.extend(worker_checks)
        checks.append(_learning_check(config, state_dir, worker_checks))
        checks.append(_quality_check(config, root, worker_checks))
        checks.extend(_free_model_checks(config, root))
        checks.extend(_prompt_checks(config, root))
        checks.extend(_tool_checks(config, root))
    return checks


def _tool_checks(config: JevConfig, root: Path) -> list[Check]:
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
        checks.append(_mcp_check(config, name, roles))
    return checks


def _mcp_check(config: JevConfig, name: str, roles: list[str]) -> Check:
    server = config.tools.mcp[name]
    label = f"tools.mcp.{name}"
    problems: list[str] = []
    if server.command is not None and which(server.command) is None:
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
    """Which dispatcher decides, and whether Jev's key env var is set (never its value)."""
    if config.dispatcher.primary == "rules":
        return Check("dispatcher", "ok", "rules")
    jev = config.dispatcher.jev
    where = f"jev ({jev.model} at {jev.base_url})"
    if os.environ.get(jev.api_key_env, "").strip():
        return Check("dispatcher", "ok", f"{where}; {jev.api_key_env} is set")
    return Check(
        "dispatcher",
        "warn",
        f"{where}; {jev.api_key_env} is not set, so decisions fall back to the rules dispatcher",
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


def _worker_checks(config: JevConfig) -> list[Check]:
    checks: list[Check] = []
    usable: set[str] = set()
    workers = config.workers
    for name, cli in (("claude_code", workers.claude_code), ("codex", workers.codex)):
        label = f"worker {name}"
        if not cli.enabled:
            checks.append(Check(label, "ok", "disabled"))
        elif (found := which(cli.command)) is None:
            checks.append(
                Check(label, "warn", f"`{cli.command}` not found on PATH; install or disable it")
            )
        else:
            usable.add(name)
            checks.append(Check(label, "ok", found))

    checks.extend(_permission_checks(config))

    api = workers.api
    if not api.enabled:
        checks.append(Check("worker api", "ok", "disabled"))
    elif os.environ.get(api.api_key_env):  # never print the value
        usable.add("api")
        model = api.default_model or "none; roles using api need `models`"
        checks.append(
            Check("worker api", "ok", f"{api.api_key_env} is set; default model: {model}")
        )
    else:
        checks.append(Check("worker api", "warn", f"{api.api_key_env} is not set"))

    for role_name, role in config.roles.items():
        candidates = [w for w in role.workers if w in usable]
        if candidates:
            checks.append(Check(f"role {role_name}", "ok", ", ".join(candidates)))
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
            checks.append(Check(f"role {role_name}", "fail", "no usable worker"))

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
