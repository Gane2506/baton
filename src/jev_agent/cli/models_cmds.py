"""`jev models ...`: the OpenRouter model catalog, for choosing models and free models.

The list comes from `GET {[workers.api] base_url}/models` and is cached in
`.jev/cache/openrouter-models.json` for `[models] cache_ttl_hours` (docs/config.md "Free
models"). Nothing here needs an API key; the key is sent when it is set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.app.errors import JevError
from jev_agent.app.models import ModelListing, ModelRow, load_listing
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import fail

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


def _price(value: float | None) -> str:
    if value is None:
        return "?"
    if value == 0:
        return "free"
    return f"${value * 1_000_000:.2f}"


def _support(row: ModelRow, param: str) -> str:
    supported = row.supports.get(param)
    return "?" if supported is None else ("yes" if supported else "no")


def _print(listing: ModelListing) -> None:
    if not listing.models:
        console.print(f"No matching models (list fetched {listing.fetched_hours_ago:.1f}h ago).")
        return
    table = Table(
        title=f"{listing.title} ({len(listing.models)}; "
        f"fetched {listing.fetched_hours_ago:.1f}h ago)"
    )
    table.add_column("model", overflow="fold")
    table.add_column("context", justify="right")
    table.add_column("in $/M", justify="right")
    table.add_column("out $/M", justify="right")
    table.add_column("json_schema")
    table.add_column("reasoning")
    for m in listing.models:
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
    try:
        listing, warning = load_listing(repo, free=free, supports=supports or [], refresh=refresh)
    except JevError as exc:
        raise fail(str(exc)) from exc
    if warning:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
    if listing is None:
        raise fail("no model list available")
    _print(listing)
    if free and listing.models:
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
