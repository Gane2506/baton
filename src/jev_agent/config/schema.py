"""Configuration schema (PLAN.md section 8). See docs/config.md for a reference."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jev_agent.config.servers_schema import (
    BrowserConfig,
    ServerConfig,
    WorkspaceProfileConfig,
    check_servers_config,
)
from jev_agent.quality.levels import QualityLevel
from jev_agent.runlog.redact import SECRET_PATTERNS

DispatcherName = Literal["jev", "rules"]
Selection = Literal["dispatcher", "fixed"]

# Key names that look like they hold a secret. `api_key_env` and `bearer_token_env` are
# allowed: they name an env var.
_SECRET_KEY = re.compile(r"(api_)?key$|token|secret|password", re.IGNORECASE)
_ALLOWED_SECRET_LOOKALIKES = frozenset({"api_key_env", "bearer_token_env"})
# Tables whose keys are names chosen by the user (MCP server, skill and plugin names, HTTP
# header names), not settings: a server called "secrets" or a header "X-Api-Key" is fine.
_NAME_TABLES = frozenset({"tools.mcp", "tools.skills", "tools.plugins", "servers", "profiles"})


def _keys_are_names(prefix: str) -> bool:
    return (
        prefix in _NAME_TABLES
        or prefix.endswith(".headers_from_env")
        or (prefix.startswith("profiles.") and prefix.endswith(".access_overrides"))
    )


def find_secret_keys(data: Any, prefix: str = "") -> list[str]:
    """Return the dotted path of every key in `data` whose name looks like it holds a secret."""
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if (
                _SECRET_KEY.search(str(key))
                and key not in _ALLOWED_SECRET_LOOKALIKES
                and not _keys_are_names(prefix)
            ):
                found.append(path)
            found.extend(find_secret_keys(value, path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(find_secret_keys(item, f"{prefix}.{index}"))
    return found


def secret_keys_message(paths: list[str]) -> str:
    return (
        f"secrets are not allowed in config files (found: {', '.join(paths)}); "
        "put the secret in an environment variable and name it with `api_key_env`"
    )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
NATIVE_JEV_BASE_URL = "https://api.typesafe.ai/v1"
_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"


class JevSettings(_Strict):
    """`[dispatcher.jev]`: connection and gating settings for the Jev dispatcher.

    Defaults call Jev through OpenRouter; `for_native()` gives the native TypeSafe values.
    """

    base_url: str = Field(default=OPENROUTER_BASE_URL, min_length=1)
    model: str = Field(default="~typesafe/jev-latest", min_length=1)
    api_key_env: str = Field(default="OPENROUTER_API_KEY", pattern=_ENV_NAME)
    timeout_s: float = Field(default=30.0, gt=0)
    min_confidence: float = Field(default=0.55, ge=0, le=1)
    # Instruction overrides, keyed "D4.effort" (one point) or "effort" (every point).
    instructions: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def for_native(cls, **overrides: Any) -> JevSettings:
        """Settings for calling TypeSafe directly instead of through OpenRouter."""
        values: dict[str, Any] = {
            "base_url": NATIVE_JEV_BASE_URL,
            "model": "jev-latest",
            "api_key_env": "TYPESAFE_API_KEY",
        }
        values.update(overrides)
        return cls(**values)


class DispatcherConfig(_Strict):
    primary: DispatcherName = "jev"
    # Only "rules" is used as the fallback for now: the rules dispatcher always backs up Jev.
    fallback: DispatcherName = "rules"
    jev: JevSettings = Field(default_factory=JevSettings)


class BudgetConfig(_Strict):
    per_run_usd: float = Field(default=5.0, gt=0)
    per_task_usd: float = Field(default=1.5, gt=0)
    max_plan_rounds: int = Field(default=3, gt=0)
    max_task_attempts: int = Field(default=3, gt=0)
    test_timeout_s: int = Field(default=1200, gt=0)  # per test command, task and integration

    @model_validator(mode="after")
    def _task_within_run(self) -> BudgetConfig:
        if self.per_task_usd > self.per_run_usd:
            raise ValueError(
                f"per_task_usd ({self.per_task_usd}) must not exceed "
                f"per_run_usd ({self.per_run_usd})"
            )
        return self


class IntegrationConfig(_Strict):
    """`[integration]`: the step that merges task branches and tests the result."""

    # Full test suite run on the result branch after each task's own test command.
    test_command: str | None = Field(default=None, min_length=1)
    # Fix tasks D6 may add after failing integration tests before a human is asked.
    max_fix_rounds: int = Field(default=2, ge=0)


class ApprovalConfig(_Strict):
    require_plan_approval: bool = True


# Learning profile names: letters, digits, `-` and `_`, starting with a letter or digit.
PROFILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"


class LearningConfig(_Strict):
    """`[learning]`: recursive learning (docs/learning.md). Off by default: when on, each run
    ends with one extra model call (the learner)."""

    enabled: bool = False
    profile: str = Field(default="default", pattern=PROFILE_NAME_PATTERN)
    max_lessons: int = Field(default=8, ge=0)  # lessons injected into one prompt
    max_chars: int = Field(default=2000, ge=0)  # characters of lesson text in one prompt


FREE_MODELS_TOKEN = "free"  # in a role's `models`: the cached free models (docs/config.md)


class ModelsConfig(_Strict):
    """`[models]`: free OpenRouter models (docs/config.md "Free models"). They only apply to
    roles that can use the `api` worker; Claude Code and Codex never get a free model."""

    prefer_free: bool = False  # put free models first for every role (roles may override)
    free_models_limit: int = Field(default=3, ge=0)  # free models one `"free"` expands to
    cache_ttl_hours: float = Field(default=24.0, gt=0)  # age before the model list is refetched


class ReviewConfig(_Strict):
    # Off by default in v0.1 (an open decision in PLAN.md section 17; see docs/decisions.md).
    enabled: bool = False


_TOOL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class QualityConfig(_Strict):
    """`[quality]`: the optional quality and security gate (docs/quality.md). Off by default
    ("none"): each level adds checks and, from `basic`, one AI code review per gate."""

    level: QualityLevel = "none"
    # Full test suite for the gate; unset: `[integration] test_command`, else auto-detected.
    test_command: str | None = Field(default=None, min_length=1)
    # Minimum line coverage in percent; unset: 60 at medium, 80 at production and scalable.
    min_coverage: float | None = Field(default=None, ge=0, le=100)
    # Tools that must be installed: a missing one fails its check instead of skipping it.
    required_tools: list[str] = Field(default_factory=list)
    timeout_s: int = Field(default=600, gt=0)  # per tool command

    @model_validator(mode="after")
    def _check_tools(self) -> QualityConfig:
        for tool in self.required_tools:
            if not re.match(_TOOL_NAME_PATTERN, tool):
                raise ValueError(f"required_tools: {tool!r} is not a tool name")
        return self


_BRANCH_PREFIX = r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
_REMOTE_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class ShipConfig(_Strict):
    """`[ship]`: `jev ship RUN` pushes a DONE run's result branch and opens a GitHub pull
    request with `gh` (docs/ship.md). Off by default; it never force-pushes and never merges."""

    enabled: bool = False
    remote: str = Field(default="origin", pattern=_REMOTE_NAME)
    # Base branch of the PR; empty: the repo's default branch (from the remote's HEAD or
    # `gh repo view`), else the branch checked out in the repo.
    base: str = ""
    branch_prefix: str = Field(default="jev/", pattern=_BRANCH_PREFIX)
    draft: bool = True
    labels: list[str] = Field(default_factory=list)
    require_quality_pass: bool = True

    @model_validator(mode="after")
    def _check(self) -> ShipConfig:
        if self.base and not re.match(_BRANCH_PREFIX, self.base):
            raise ValueError(f"base: {self.base!r} is not a branch name")
        if ".." in self.branch_prefix or ".." in self.base:
            raise ValueError("branch names cannot contain `..`")
        for label in self.labels:
            if not label.strip() or label.startswith("-"):
                raise ValueError(f"labels: {label!r} is not a label name")
        return self


# Claude Code `--permission-mode` values (docs/verification.md section 2), minus
# `bypassPermissions`: that one is only reachable through the explicit `skip_permissions`.
ClaudePermissionMode = Literal["acceptEdits", "auto", "default", "dontAsk", "plan"]
ClaudeReadOnlyPermissionMode = Literal["dontAsk", "plan", "default"]
# Codex `--sandbox` values (docs/verification.md section 3).
CodexSandbox = Literal["read-only", "workspace-write", "danger-full-access"]


class WorkerConfig(_Strict):
    """Settings shared by the workers that run a local CLI (Claude Code, Codex)."""

    enabled: bool = True
    command: str = Field(min_length=1)
    timeout_s: int = Field(default=1200, gt=0)
    # Appended to every invocation, e.g. ["--max-turns", "40"].
    extra_args: list[str] = Field(default_factory=list)


class ClaudeCodeConfig(WorkerConfig):
    command: str = Field(default="claude", min_length=1)
    permission_mode: ClaudePermissionMode = "acceptEdits"  # coder, in its own worktree
    read_only_permission_mode: ClaudeReadOnlyPermissionMode = "dontAsk"  # all other roles
    # Coder tools; unset keeps the built-in list (read/edit tools, `git status`/`git diff`).
    allowed_tools: list[str] | None = None
    # Claude Code's own per-attempt cap (`--max-budget-usd`); unset means budget.per_task_usd.
    max_budget_usd: float | None = Field(default=None, gt=0)
    bare: bool = False  # `--bare` mode; needs ANTHROPIC_API_KEY (see docs/security.md)
    # DANGEROUS: the coder runs with `--dangerously-skip-permissions` (any command, your
    # privileges). Only in a throwaway sandbox without secrets or network.
    skip_permissions: bool = False


class CodexConfig(WorkerConfig):
    command: str = Field(default="codex", min_length=1)
    sandbox: CodexSandbox = "workspace-write"  # coder only; other roles are always read-only


class ApiWorkerConfig(_Strict):
    """A worker that calls an OpenAI-compatible HTTP API. The key comes from `api_key_env`."""

    enabled: bool = True
    base_url: str = Field(default=OPENROUTER_BASE_URL, min_length=1)
    api_key_env: str = Field(default="OPENROUTER_API_KEY", pattern=_ENV_NAME)
    # Model used when the role's `models` pool is empty; unset means such roles fail on api.
    default_model: str | None = Field(default=None, min_length=1)
    timeout_s: int = Field(default=600, gt=0)


class WorkersConfig(_Strict):
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    api: ApiWorkerConfig = Field(default_factory=ApiWorkerConfig)

    def names(self) -> list[str]:
        return list(type(self).model_fields)

    def is_enabled(self, name: str) -> bool:
        worker: WorkerConfig | ApiWorkerConfig = getattr(self, name)
        return worker.enabled


# --- agent tools: MCP servers, skills, plugins (docs/config.md "Agent tools") ---------------

_TOOL_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
# Credentials in a URL's query string; the other literal credentials that must never sit in a
# config file (args, URLs) are the shapes every log redacts (`runlog.redact`).
_SECRET_QUERY = re.compile(r"[?&](?:api_?key|key|token|access_token|secret|password)=", re.I)


def _literal_secret(value: str) -> bool:
    return bool(_SECRET_QUERY.search(value)) or any(p.search(value) for p in SECRET_PATTERNS)


McpTransport = Literal["http", "sse"]


class McpServerConfig(_Strict):
    """`[tools.mcp.<name>]`: one MCP server, either local (`command`) or remote (`url`).

    Secrets never go here: `env_from` and `headers_from_env` name environment variables, and
    jev reads their values when it starts the agent.
    """

    command: str | None = Field(default=None, min_length=1)
    args: list[str] = Field(default_factory=list)
    env_from: list[str] = Field(default_factory=list)  # env var NAMES passed to the server
    url: str | None = Field(default=None, pattern=r"^https?://")
    transport: McpTransport = "http"  # remote servers only
    bearer_token_env: str | None = Field(default=None, pattern=_ENV_NAME)
    headers_from_env: dict[str, str] = Field(default_factory=dict)  # header -> env var NAME

    @model_validator(mode="before")
    @classmethod
    def _no_literal_values(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, use in (("env", "env_from"), ("headers", "headers_from_env")):
                if key in data:
                    raise ValueError(
                        f"`{key}` is not allowed: values in config files could leak secrets; "
                        f"name environment variables with `{use}` instead"
                    )
        return data

    @model_validator(mode="after")
    def _check(self) -> McpServerConfig:
        if (self.command is None) == (self.url is None):
            raise ValueError("set exactly one of `command` (local server) or `url` (remote)")
        if self.command is not None and (self.bearer_token_env or self.headers_from_env):
            raise ValueError("`bearer_token_env` and `headers_from_env` need a remote `url`")
        if self.url is None and "transport" in self.model_fields_set:
            raise ValueError("`transport` only applies to a remote `url`")
        if self.url is not None and (self.args or self.env_from):
            raise ValueError("`args` and `env_from` only apply to a local `command`")
        for name in [*self.env_from, *self.headers_from_env.values()]:
            if not re.match(_ENV_NAME, name):
                raise ValueError(f"{name!r} is not an environment variable name")
        for value in [*(self.args), self.url or "", self.command or ""]:
            if _literal_secret(value):
                raise ValueError(
                    "looks like a literal credential; pass it through an environment variable "
                    "(`env_from`, `bearer_token_env` or `headers_from_env`)"
                )
        return self

    def env_names(self) -> list[str]:
        """Every environment variable this server reads its secrets from."""
        names = [*self.env_from, *self.headers_from_env.values()]
        if self.bearer_token_env:
            names.append(self.bearer_token_env)
        return list(dict.fromkeys(names))


class PluginConfig(_Strict):
    """`[tools.plugins.<name>]`: a Claude Code plugin directory loaded with `--plugin-dir`."""

    path: Path
    # Permission rules the plugin's tools need in headless runs, e.g. "mcp__plugin_x_db__*".
    allowed_tools: list[str] = Field(default_factory=list)


class ToolsConfig(_Strict):
    """`[tools]`: tools defined once and given to roles by name (`[roles.<role>] mcp = [...]`)."""

    mcp: dict[str, McpServerConfig] = Field(default_factory=dict)
    skills: dict[str, Path] = Field(default_factory=dict)  # name -> folder holding SKILL.md
    plugins: dict[str, PluginConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_names(self) -> ToolsConfig:
        for kind, names in (("mcp", self.mcp), ("skills", self.skills), ("plugins", self.plugins)):
            for name in names:
                if not re.match(_TOOL_NAME, name):
                    raise ValueError(
                        f"tools.{kind}: {name!r} is not a valid name "
                        "(letters, digits, `-` and `_`, starting with a letter or digit)"
                    )
        return self


class RoleConfig(_Strict):
    workers: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)  # empty means "not configured"
    selection: Selection = "dispatcher"
    # Overrides `[models] prefer_free` for this role; unset follows it.
    prefer_free: bool | None = None
    efforts: list[str] = Field(default_factory=lambda: ["low", "medium", "high"])
    prefer_different_vendor_than: str | None = None
    # Your own Jinja2 prompt template; relative paths are resolved against the repo root.
    prompt_file: Path | None = None
    # Agent tools for this role, by name from `[tools]`.
    mcp: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    # Servers and the browser for this role (docs/servers.md); read-only roles get basic access.
    servers: list[str] = Field(default_factory=list)
    browser: bool = False


# Per-role defaults. A partial `[roles.<role>]` table is layered on top of these, so setting
# only `models` keeps the role's default `workers`.
_ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "planner": {"workers": ["claude_code", "api"]},
    "validator": {"workers": ["codex", "api"], "prefer_different_vendor_than": "planner"},
    "coder": {"workers": ["claude_code", "codex"]},
    "reviewer": {"workers": ["api", "codex"]},
    "code_reviewer": {"workers": ["api", "codex", "claude_code"]},
    "learner": {"workers": ["claude_code", "api", "codex"]},
}


class RolesConfig(_Strict):
    planner: RoleConfig = Field(default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["planner"]))
    validator: RoleConfig = Field(default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["validator"]))
    coder: RoleConfig = Field(default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["coder"]))
    reviewer: RoleConfig = Field(default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["reviewer"]))
    code_reviewer: RoleConfig = Field(
        default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["code_reviewer"])
    )
    learner: RoleConfig = Field(default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["learner"]))

    @model_validator(mode="before")
    @classmethod
    def _apply_role_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {
            name: {**_ROLE_DEFAULTS[name], **value}
            if name in _ROLE_DEFAULTS and isinstance(value, dict)
            else value
            for name, value in data.items()
        }

    def items(self) -> list[tuple[str, RoleConfig]]:
        return [(name, getattr(self, name)) for name in type(self).model_fields]


def role_prefers_free(config: JevConfig, role: str) -> bool:
    """`[roles.<role>] prefer_free` if set, else `[models] prefer_free`."""
    override: bool | None = getattr(config.roles, role).prefer_free
    return config.models.prefer_free if override is None else override


def role_uses_free(config: JevConfig, role: str) -> bool:
    """The role asks for free models: it prefers them or lists the `"free"` token."""
    return (
        role_prefers_free(config, role) or FREE_MODELS_TOKEN in getattr(config.roles, role).models
    )


def role_can_use_free(config: JevConfig, role: str) -> bool:
    """Free models run only on the api worker (OpenRouter); the role must allow it."""
    return "api" in getattr(config.roles, role).workers and config.workers.api.enabled


class TrustConfig(_Strict):
    """`[trust]` (user config only): repositories whose jev.toml may change the guarded keys
    (`config.loader.GUARDED_KEYS`): absolute paths, or `"*"` for every repository."""

    repos: list[str] = Field(default_factory=list)


class JevConfig(_Strict):
    dispatcher: DispatcherConfig = Field(default_factory=DispatcherConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    integration: IntegrationConfig = Field(default_factory=IntegrationConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    ship: ShipConfig = Field(default_factory=ShipConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    roles: RolesConfig = Field(default_factory=RolesConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    # Servers, browser and workspace profiles (docs/servers.md).
    servers: dict[str, ServerConfig] = Field(default_factory=dict)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    profiles: dict[str, WorkspaceProfileConfig] = Field(default_factory=dict)
    trust: TrustConfig = Field(default_factory=TrustConfig)

    @model_validator(mode="before")
    @classmethod
    def _reject_secrets(cls, data: Any) -> Any:
        secrets = find_secret_keys(data)
        if secrets:
            raise ValueError(secret_keys_message(secrets))
        return data

    @model_validator(mode="after")
    def _check_role_references(self) -> JevConfig:
        known_workers = self.workers.names()
        role_names = [name for name, _ in self.roles.items()]
        for name, role in self.roles.items():
            for worker in role.workers:
                if worker not in known_workers:
                    raise ValueError(
                        f"roles.{name}.workers: unknown worker {worker!r} "
                        f"(known: {', '.join(known_workers)})"
                    )
            for kind in ("mcp", "skills", "plugins"):
                defined: dict[str, Any] = getattr(self.tools, kind)
                for ref in getattr(role, kind):
                    if ref not in defined:
                        known = ", ".join(defined) or "none defined"
                        raise ValueError(
                            f"roles.{name}.{kind}: unknown {kind} entry {ref!r}: define it in "
                            f"[tools.{kind}] (known: {known})"
                        )
            other = role.prefer_different_vendor_than
            if other is not None and (other == name or other not in role_names):
                raise ValueError(
                    f"roles.{name}.prefer_different_vendor_than: must name another role "
                    f"(one of: {', '.join(r for r in role_names if r != name)})"
                )
        return self

    @model_validator(mode="after")
    def _check_servers(self) -> JevConfig:
        reserved = {"jev_servers", "jev_browser"} & set(self.tools.mcp)
        if reserved:
            raise ValueError(f"tools.mcp: {', '.join(sorted(reserved))} is reserved for jev")
        check_servers_config(
            self.servers,
            self.browser,
            self.profiles,
            {name: role.servers for name, role in self.roles.items()},
            PROFILE_NAME_PATTERN,
        )
        return self
