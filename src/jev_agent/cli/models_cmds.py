"""`jev models ...`: the OpenRouter model catalog, for choosing models and free models.

The list comes from `GET {[workers.api] base_url}/models` and is cached in
`.jev/cache/openrouter-models.json` for `[models] cache_ttl_hours` (docs/config.md "Free
models"). Nothing here needs an API key; the key is sent when it is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.cli import wiring
from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.config import ConfigError, load_config
from jev_agent.workers.openrouter_models import (
    PARAM_ALIASES,
    ModelCatalog,
    ModelInfo,
    cache_path,
    load_catalog,
)

console = Console()

models_app = typer.Typer(
    name="models",
    help="List OpenRouter models (for the api worker), including free ones.",
    no_args_is_help=True,
)

FreeOption = Annotated[bool, typer.Option("--free", help="Only free models.")]
SupportsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--supports",
        help="Only models listing this parameter (repeatable), e.g. json_schema, tools, reasoning.",
    ),
]
RefreshOption = Annotated[
    bool, typer.Option("--refresh", help="Fetch the list now instead of using the cache.")
]


def _catalog(repo: Path, refresh: bool) -> ModelCatalog:
    root = find_git_root(repo) or repo.resolve()
    try:
        config = load_config(root)
    except ConfigError as exc:
        raise wiring.fail(str(exc)) from exc
    api = config.workers.api
    catalog, warning = load_catalog(
        cache_path(root),
        api.base_url,
        ttl_s=config.models.cache_ttl_hours * 3600,
        refresh=refresh,
        api_key=os.environ.get(api.api_key_env, "").strip() or None,
        transport=wiring.catalog_transport,
    )
    if warning:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
    if catalog is None:
        raise wiring.fail("no model list available")
    return catalog


def _price(value: float | None) -> str:
    if value is None:
        return "?"
    if value == 0:
        return "free"
    return f"${value * 1_000_000:.2f}"


def _support(model: ModelInfo, param: str) -> str:
    supported = model.supports(param)
    return "?" if supported is None else ("yes" if supported else "no")


def _print(models: list[ModelInfo], catalog: ModelCatalog, title: str) -> None:
    hours = catalog.age_s() / 3600
    if not models:
        console.print(f"No matching models (list fetched {hours:.1f}h ago).")
        return
    table = Table(title=f"{title} ({len(models)}; fetched {hours:.1f}h ago)")
    table.add_column("model", overflow="fold")
    table.add_column("context", justify="right")
    table.add_column("in $/M", justify="right")
    table.add_column("out $/M", justify="right")
    table.add_column("json_schema")
    table.add_column("reasoning")
    for m in models:
        table.add_row(
            escape(m.id),
            f"{m.context_length:,}" if m.context_length else "?",
            _price(m.prompt_price),
            _price(m.completion_price),
            _support(m, "json_schema"),
            _support(m, "reasoning"),
        )
    console.print(table)


def _list(repo: Path, free: bool, supports: list[str] | None, refresh: bool) -> None:
    catalog = _catalog(repo, refresh)
    wanted = [s.strip() for s in supports or [] if s.strip()]
    models = [
        m
        for m in catalog.filtered(free=free)
        if all(m.supports(PARAM_ALIASES.get(p, p)) for p in wanted)
    ]
    title = "free models" if free else "models"
    _print(models, catalog, title)
    if free and models:
        console.print(
            "Free models are rate-limited and run on the api worker only; providers may log or "
            "train on prompts (see https://openrouter.ai/settings/privacy). Use them with "
            '`\\[models] prefer_free = true` or `models = ["free"]` in a role.'
        )


@models_app.command("list")
def list_models(
    free: FreeOption = False,
    supports: SupportsOption = None,
    refresh: RefreshOption = False,
    repo: RepoOption = Path("."),
) -> None:
    """List models with context length, prices and json_schema/reasoning support."""
    _list(repo, free, supports, refresh)


@models_app.command("free")
def free_models(
    supports: SupportsOption = None,
    refresh: RefreshOption = False,
    repo: RepoOption = Path("."),
) -> None:
    """List free models (same as `jev models list --free`)."""
    _list(repo, True, supports, refresh)
