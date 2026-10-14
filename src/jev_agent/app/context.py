"""Build the app's contexts from a repo's config and state database (the shared seam).

The front ends (`cli/` and `dashboard/`) call in here; nothing in this module prints, prompts
or exits, and problems are `JevError` with user-facing messages (issue #92).

- Dispatcher: `[dispatcher] primary = "consensus"` (the default) gives a `ConsensusDispatcher`
  (Jev, then Laya); the pipeline gives it no fallback, so a decision without agreement stops
  the run for a human. `"jev"` gives a `JevDispatcher`, wrapped with the rules dispatcher as
  fallback. `"rules"` uses rules only.
- Workers: Claude Code, Codex and the API worker, each when enabled in `[workers.*]` and ready
  (`worker_readiness`, the same rule `jev doctor` reports): a CLI worker needs its command on
  PATH; the api worker needs its key env var and, per role, a model. Unready workers are left
  out with one warning each. Worker timeouts come from the same tables (the harness reads
  `timeout_s`). The test hook `JEV_FAKE_WORKERS=1` replaces every enabled worker with a
  `FakeWorker` and skips the readiness checks.
- Free models: when a role asks for them (`prefer_free` or `"free"` in `models`) and can use
  the api worker, the cached OpenRouter model list is loaded (and refreshed when older than
  `[models] cache_ttl_hours`; never with fake workers). See docs/config.md "Free models".
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx

from jev_agent.app.errors import JevError
from jev_agent.cli.repo import LEGACY_MESSAGE, find_git_root, legacy_layout
from jev_agent.config import STATE_DIRNAME, ConfigError, JevConfig, load_config
from jev_agent.config.schema import JevSettings, role_can_use_free, role_uses_free
from jev_agent.core.learning import Learning
from jev_agent.core.models import Role
from jev_agent.core.pipeline import Pipeline, PipelineSettings, build_pipeline
from jev_agent.core.store import SchemaVersionError, Store
from jev_agent.dispatch.base import Dispatcher
from jev_agent.dispatch.consensus import ConsensusDispatcher
from jev_agent.dispatch.jev import MultiProviderJevDispatcher
from jev_agent.dispatch.requests import API_WORKER, DEFAULT_MODEL, model_fits, role_options
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.workers.api import ApiWorker
from jev_agent.workers.base import Worker
from jev_agent.workers.claude_code import ClaudeCodeWorker
from jev_agent.workers.codex import CodexWorker
from jev_agent.workers.fake import FakeWorker
from jev_agent.workers.openrouter_models import ModelCatalog, cache_path, load_catalog
from jev_agent.workers.registry import WorkerRegistry

STATE_DB_FILENAME = "state.db"
FAKE_WORKERS_ENV = "JEV_FAKE_WORKERS"

NO_WORKERS_MESSAGE = (
    "No workers are available: enable at least one of `[workers.claude_code]`, `[workers.codex]` "
    "or `[workers.api]` in jev.toml and make sure it can run (`jev doctor` shows why a worker "
    f"is not ready), or set {FAKE_WORKERS_ENV}=1 to try the pipeline with fake workers."
)

# Test hook: the HTTP transport used to fetch the OpenRouter model list (None: the network).
catalog_transport: httpx.BaseTransport | None = None

# Indirection so tests can fake PATH lookups without real CLIs installed.
which: Callable[[str], str | None] = shutil.which


@dataclass
class AppContext:
    root: Path
    config: JevConfig
    store: Store
    pipeline: Pipeline
    has_workers: bool
    # Problems worth one line at the start of a run (never secret values).
    warnings: list[str] = field(default_factory=list)
    # Lesson profiles: the repo's (in state.db) and the shared ones (docs/learning.md).
    learning: Learning | None = None


def pipeline_settings(config: JevConfig) -> PipelineSettings:
    return PipelineSettings(
        test_timeout_s=config.budget.test_timeout_s,
        integration_test_command=config.integration.test_command,
        max_fix_rounds=config.integration.max_fix_rounds,
    )


def build_dispatcher(config: JevConfig) -> tuple[Dispatcher, list[str]]:
    """The primary dispatcher from `[dispatcher]`, plus warnings to show at run start.

    Jev is a `MultiProviderJevDispatcher`: the native TypeSafe API first, then Jev
    through OpenRouter. Built even when a key env var is unset: each decision then
    falls back to rules (`jev`), or stops the run for a human (`consensus`).
    """
    primary = config.dispatcher.primary
    if primary == "rules":
        return RulesDispatcher(), []
    # The user's knobs (timeout, confidence, instructions) apply to both legs; the
    # provider endpoints and key env vars keep their `for_native()`/OpenRouter defaults.
    s = config.dispatcher.jev
    native = JevSettings.for_native(
        timeout_s=s.timeout_s, min_confidence=s.min_confidence, instructions=s.instructions
    )
    openrouter = JevSettings(
        timeout_s=s.timeout_s, min_confidence=s.min_confidence, instructions=s.instructions
    )
    jev = MultiProviderJevDispatcher(native=native, openrouter=openrouter)
    warnings: list[str] = []
    native_key = bool(os.environ.get(native.api_key_env, "").strip())
    openrouter_key = bool(os.environ.get(openrouter.api_key_env, "").strip())
    if not native_key and not openrouter_key:
        outcome = (
            "the run will stop for a human at its first decision"
            if primary == "consensus"
            else "every decision will use the rules dispatcher"
        )
        warnings.append(
            f"{native.api_key_env} and {openrouter.api_key_env} are not set, so Jev cannot "
            f"be reached; {outcome}. Jev tries the native TypeSafe API "
            f"({native.api_key_env}) first, then Jev through OpenRouter; set either key, or "
            'set `[dispatcher] primary = "rules"` to silence this warning.'
        )
    elif not native_key:
        warnings.append(
            f"{native.api_key_env} is not set, so every Jev decision goes through OpenRouter "
            f"({openrouter.api_key_env}) without the native TypeSafe leg."
        )
    elif not openrouter_key:
        warnings.append(
            f"{openrouter.api_key_env} is not set, so there is no failover if the native "
            f"TypeSafe leg ({native.api_key_env}) cannot serve a decision."
        )
    if primary == "consensus":
        return ConsensusDispatcher(jev, config.dispatcher.laya), warnings
    return jev, warnings


def free_roles(config: JevConfig) -> list[str]:
    """Roles that would use free models: they ask for them and may use the api worker."""
    return [
        name
        for name, _ in config.roles.items()
        if role_uses_free(config, name) and role_can_use_free(config, name)
    ]


def load_free_catalog(
    config: JevConfig, root: Path, *, allow_fetch: bool = True
) -> tuple[ModelCatalog | None, list[str]]:
    """The model catalog when some role uses free models, plus warnings; (None, []) otherwise."""
    if not free_roles(config):
        return None, []
    api = config.workers.api
    catalog, warning = load_catalog(
        cache_path(root),
        api.base_url,
        ttl_s=config.models.cache_ttl_hours * 3600,
        api_key=os.environ.get(api.api_key_env, "").strip() or None,
        transport=catalog_transport,
        allow_fetch=allow_fetch,
    )
    warnings = [f"free models: {warning}"] if warning else []
    if catalog is None:
        warnings.append(
            'free models: no model list is cached, so `"free"` expands to nothing; run '
            "`jev models free --refresh`"
        )
    return catalog, warnings


@dataclass(frozen=True)
class WorkerReadiness:
    """Which enabled workers can run, and for which roles (issue #19).

    One rule for runs (`build_registry`) and `jev doctor`: a CLI worker needs its command on
    PATH; the api worker needs its key env var, and for each role a model it can run
    (`default_model`, or a `vendor/name` slug from the role's `models` or its free models), and
    never runs the coder role (it cannot edit files).
    """

    # Enabled worker -> why it cannot run at all.
    problems: dict[str, str] = field(default_factory=dict)
    # Role -> worker -> why that (otherwise ready) worker cannot run for the role.
    role_problems: dict[str, dict[str, str]] = field(default_factory=dict)
    # Ready CLI worker -> the command's path.
    paths: dict[str, str] = field(default_factory=dict)

    def ready(self, worker: str) -> bool:
        return worker not in self.problems

    def ready_for(self, role: str, worker: str) -> bool:
        return self.ready(worker) and worker not in self.role_problems.get(role, {})

    def not_for(self) -> dict[str, list[str]]:
        """Role -> ready workers it cannot use (for `WorkerRegistry`)."""
        return {role: list(workers) for role, workers in self.role_problems.items()}

    def warnings(self) -> list[str]:
        """One line per unready worker (per reason when it is unready for some roles only)."""
        lines = [f"worker {name} is not used: {why}" for name, why in self.problems.items()]
        by_worker: dict[str, dict[str, list[str]]] = {}
        for role, workers in self.role_problems.items():
            for worker, why in workers.items():
                by_worker.setdefault(worker, {}).setdefault(why, []).append(role)
        for worker, reasons in by_worker.items():
            for why, roles in reasons.items():
                lines.append(f"worker {worker} is not offered to {', '.join(roles)}: {why}")
        return lines


API_NO_MODEL = (
    "no model; set `[workers.api] default_model`, a `vendor/name` slug in the role's `models`, "
    "or free models (`prefer_free` with a cached list, `jev models free --refresh`)"
)
API_NO_CODER = "the api worker cannot edit files, so it cannot run the coder role"


def api_role_problem(config: JevConfig, role: str, catalog: ModelCatalog | None) -> str | None:
    """Why the api worker cannot run for `role`, or None (its key is checked separately)."""
    if role == "coder":
        return API_NO_CODER
    if config.workers.api.default_model:
        return None
    models = role_options(config, cast(Role, role), [API_WORKER], catalog).models
    if any(m != DEFAULT_MODEL and model_fits(API_WORKER, m) for m in models):
        return None
    return API_NO_MODEL


def worker_readiness(
    config: JevConfig,
    catalog: ModelCatalog | None = None,
    *,
    find: Callable[[str], str | None] | None = None,
) -> WorkerReadiness:
    """Readiness of each enabled worker; `find` looks a command up on PATH (default `which`)."""
    find = find or which
    readiness = WorkerReadiness()
    workers = config.workers
    for name, cli in (("claude_code", workers.claude_code), ("codex", workers.codex)):
        if not cli.enabled:
            continue
        if (path := find(cli.command)) is None:
            readiness.problems[name] = f"`{cli.command}` not found on PATH; install or disable it"
        else:
            readiness.paths[name] = path
    api = workers.api
    if not api.enabled:
        return readiness
    if not os.environ.get(api.api_key_env, "").strip():  # never the value
        readiness.problems[API_WORKER] = f"{api.api_key_env} is not set"
        return readiness
    roles = [name for name, rc in config.roles.items() if API_WORKER in rc.workers]
    found = {r: why for r in roles if (why := api_role_problem(config, r, catalog)) is not None}
    if roles and len(found) == len(roles):
        reasons: dict[str, list[str]] = {}
        for role, why in found.items():
            reasons.setdefault(why, []).append(role)
        readiness.problems[API_WORKER] = "; ".join(
            f"{', '.join(rs)}: {why}" for why, rs in reasons.items()
        )
        return readiness
    for role, why in found.items():
        readiness.role_problems.setdefault(role, {})[API_WORKER] = why
    return readiness


def build_api_worker(config: JevConfig, catalog: ModelCatalog | None = None) -> ApiWorker | None:
    api = config.workers.api
    if not api.enabled:
        return None
    free = [m.id for m in catalog.free()] if catalog is not None else []
    return ApiWorker(api.base_url, api.api_key_env, api.default_model, free_models=free)


def build_cli_workers(config: JevConfig) -> dict[str, Worker]:
    """Workers that run local agent CLIs (`claude_code`, `codex`), for each enabled one."""
    workers: dict[str, Worker] = {}
    claude = config.workers.claude_code
    if claude.enabled:
        workers["claude_code"] = ClaudeCodeWorker(
            claude.command,
            permission_mode=claude.permission_mode,
            read_only_permission_mode=claude.read_only_permission_mode,
            allowed_tools=claude.allowed_tools,
            skip_permissions=claude.skip_permissions,
            max_budget_usd=claude.max_budget_usd or config.budget.per_task_usd,
            bare=claude.bare,
            extra_args=claude.extra_args,
        )
    codex = config.workers.codex
    if codex.enabled:
        workers["codex"] = CodexWorker(
            codex.command, sandbox=codex.sandbox, extra_args=codex.extra_args
        )
    return workers


def fake_workers_enabled() -> bool:
    return os.environ.get(FAKE_WORKERS_ENV) == "1"


def build_registry(
    config: JevConfig, catalog: ModelCatalog | None = None
) -> tuple[WorkerRegistry, list[str]]:
    """The ready workers (see `WorkerReadiness`), plus one warning per unready one."""
    if fake_workers_enabled():
        fakes: dict[str, Worker] = {name: FakeWorker(name) for name in config.workers.names()}
        return WorkerRegistry.from_config(config, fakes), []
    readiness = worker_readiness(config, catalog)
    workers = build_cli_workers(config)
    api = build_api_worker(config, catalog)
    if api is not None:
        workers["api"] = api
    ready = {name: w for name, w in workers.items() if readiness.ready(name)}
    registry = WorkerRegistry.from_config(config, ready, not_for=readiness.not_for())
    return registry, readiness.warnings()


def repo_root(repo: Path) -> Path:
    """The git repo containing `repo`; `JevError` when there is none or it still has the jao
    (v0.1) layout."""
    root = find_git_root(repo, allow_legacy=True)
    if root is None:
        raise JevError(f"{repo.resolve()} is not inside a git repository")
    if legacy_layout(root):
        raise JevError(LEGACY_MESSAGE)
    return root


def repo_config(root: Path) -> JevConfig:
    try:
        return load_config(root)
    except ConfigError as exc:
        raise JevError(str(exc)) from exc


def state_db_path(root: Path) -> Path:
    return root / STATE_DIRNAME / STATE_DB_FILENAME


@contextmanager
def open_store(root: Path, *, create: bool = True) -> Iterator[Store]:
    """The repo's state database. With `create=False` (read-only commands) a missing database
    is not created: the store is an empty in-memory one with the current schema, so the
    command shows its usual "nothing yet" output and leaves no `.jev/` behind (issue #26).
    A database written by a newer jev is a `JevError`, not a traceback (issue #25)."""
    path = state_db_path(root)
    try:
        store = Store.open(path if create or path.exists() else Path(":memory:"))
    except SchemaVersionError as exc:
        raise JevError(str(exc)) from exc
    with store:
        yield store


@dataclass
class RepoContext:
    """Config, state database and lesson profiles: what use cases need that run no workers."""

    root: Path
    config: JevConfig
    store: Store
    learning: Learning


@contextmanager
def open_repo(repo: Path, *, create: bool = True) -> Iterator[RepoContext]:
    """A `RepoContext` for the repo containing `repo`; raises `JevError`. No workers,
    dispatcher or model list are built, and nothing is fetched."""
    root = repo_root(repo)
    config = repo_config(root)
    with open_store(root, create=create) as store:
        learning = Learning.for_store(store)
        try:
            yield RepoContext(root, config, store, learning)
        finally:
            learning.close()


@contextmanager
def open_context(repo: Path) -> Iterator[AppContext]:
    """Config, store and pipeline (workers, dispatcher, model list) for the repo containing
    `repo`; raises `JevError`. The dashboard and the chat use it directly; CLI commands use
    `cli.wiring.cli_context`, which wraps it with the CLI's error line and exit code."""
    root = repo_root(repo)
    config = repo_config(root)
    catalog, free_warnings = load_free_catalog(config, root, allow_fetch=not fake_workers_enabled())
    registry, worker_warnings = build_registry(config, catalog)
    dispatcher, warnings = build_dispatcher(config)
    warnings += worker_warnings + free_warnings
    with open_store(root) as store:
        learning = Learning.for_store(store)
        try:
            pipeline = build_pipeline(
                root,
                config,
                store,
                registry,
                dispatcher=dispatcher,
                settings=pipeline_settings(config),
                learning=learning,
                catalog=catalog,
            )
            yield AppContext(root, config, store, pipeline, bool(registry), warnings, learning)
        finally:
            learning.close()
