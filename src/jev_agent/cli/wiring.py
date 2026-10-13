"""Build a `Pipeline` for the CLI from the repo's config and state database.

- Dispatcher: `[dispatcher] primary = "jev"` gives a `JevDispatcher`; the pipeline wraps it in
  a `GuardedDispatcher` with the rules dispatcher as fallback. `"rules"` uses rules only.
- Workers: Claude Code, Codex and the API worker, each when enabled in `[workers.*]`. Worker
  timeouts come from the same tables (the harness reads `timeout_s`). The test hook
  `JEV_FAKE_WORKERS=1` replaces every enabled worker with a `FakeWorker`.
- Free models: when a role asks for them (`prefer_free` or `"free"` in `models`) and can use
  the api worker, the cached OpenRouter model list is loaded (and refreshed when older than
  `[models] cache_ttl_hours`; never with fake workers). See docs/config.md "Free models".
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import typer
from rich.console import Console

from jev_agent.cli.repo import find_git_root
from jev_agent.config import STATE_DIRNAME, ConfigError, JevConfig, load_config
from jev_agent.config.schema import role_can_use_free, role_uses_free
from jev_agent.core.learning import Learning
from jev_agent.core.pipeline import Pipeline, PipelineSettings, build_pipeline
from jev_agent.core.store import Store
from jev_agent.dispatch.base import Dispatcher
from jev_agent.dispatch.jev import JevDispatcher
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
    f"or `[workers.api]` in jev.toml, or set {FAKE_WORKERS_ENV}=1 to try the pipeline with fake "
    "workers."
)

console = Console()

# Test hook: the HTTP transport used to fetch the OpenRouter model list (None: the network).
catalog_transport: httpx.BaseTransport | None = None


@dataclass
class CliContext:
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

    Jev is built even when its key env var is unset: each decision then falls back to rules.
    """
    if config.dispatcher.primary == "rules":
        return RulesDispatcher(), []
    settings = config.dispatcher.jev
    warnings: list[str] = []
    if not os.environ.get(settings.api_key_env, "").strip():
        warnings.append(
            f"{settings.api_key_env} is not set, so the Jev dispatcher cannot be reached; "
            "every decision will use the rules dispatcher. Set it, or set "
            '`[dispatcher] primary = "rules"` to silence this warning.'
        )
    return JevDispatcher(settings), warnings


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


def build_registry(config: JevConfig, catalog: ModelCatalog | None = None) -> WorkerRegistry:
    if fake_workers_enabled():
        fakes: dict[str, Worker] = {name: FakeWorker(name) for name in config.workers.names()}
        return WorkerRegistry.from_config(config, fakes)
    workers = build_cli_workers(config)
    api = build_api_worker(config, catalog)
    if api is not None:
        workers["api"] = api
    return WorkerRegistry.from_config(config, workers)


def fail(message: str) -> typer.Exit:
    console.print(f"[red]error:[/red] {message}")
    return typer.Exit(1)


@contextmanager
def cli_context(repo: Path) -> Iterator[CliContext]:
    """Open config, store and pipeline for the repo containing `repo`; exit 1 on errors."""
    root = find_git_root(repo)
    if root is None:
        raise fail(f"{repo.resolve()} is not inside a git repository")
    try:
        config = load_config(root)
    except ConfigError as exc:
        raise fail(str(exc)) from exc
    catalog, free_warnings = load_free_catalog(config, root, allow_fetch=not fake_workers_enabled())
    registry = build_registry(config, catalog)
    dispatcher, warnings = build_dispatcher(config)
    warnings += free_warnings
    with Store.open(root / STATE_DIRNAME / STATE_DB_FILENAME) as store:
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
            yield CliContext(root, config, store, pipeline, bool(registry), warnings, learning)
        finally:
            learning.close()
