"""Model catalog listing as a use case (issue #92).

The list comes from `GET {[workers.api] base_url}/models` and is cached in
`.jev/cache/openrouter-models.json` for `[models] cache_ttl_hours`. `fetch_catalog` wires
the config, the key env var (never its value) and the test transport; `list_models` filters
it. Nothing here prints or exits; no catalog is a `JevError`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jev_agent.app import context as app_context
from jev_agent.app.errors import JevError
from jev_agent.app.repo import find_git_root
from jev_agent.config import ConfigError, load_config
from jev_agent.config.schema import JevConfig
from jev_agent.workers.openrouter_models import (
    PARAM_ALIASES,
    ModelCatalog,
    cache_path,
    load_catalog,
)


@dataclass(frozen=True)
class ModelRow:
    """One model, as the listing shows it."""

    id: str
    context_length: int | None
    prompt_price: float | None
    completion_price: float | None
    supports: dict[str, bool | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelListing:
    models: list[ModelRow]
    fetched_hours_ago: float
    title: str


def fetch_catalog(
    root: Path, config: JevConfig, *, refresh: bool
) -> tuple[ModelCatalog | None, str | None]:
    """The cached (or freshly fetched) model catalog and a warning, if any.

    `JevError` when the repo has no readable config. A missing catalog is not an error
    here: the caller decides (`jev models` fails, `jev doctor` warns).
    """
    api = config.workers.api
    return load_catalog(
        cache_path(root),
        api.base_url,
        ttl_s=config.models.cache_ttl_hours * 3600,
        refresh=refresh,
        api_key=os.environ.get(api.api_key_env, "").strip() or None,
        transport=app_context.catalog_transport,
    )


def list_models(catalog: ModelCatalog, *, free: bool, supports: Sequence[str]) -> ModelListing:
    """Filter the catalog: free models only, and/or models supporting each parameter."""
    wanted = [s.strip() for s in supports if s.strip()]
    models = [
        m
        for m in catalog.filtered(free=free)
        if all(m.supports(PARAM_ALIASES.get(p, p)) for p in wanted)
    ]
    rows = [
        ModelRow(
            id=m.id,
            context_length=m.context_length,
            prompt_price=m.prompt_price,
            completion_price=m.completion_price,
            # The table always shows these two, plus whatever was filtered on.
            supports={
                p: m.supports(PARAM_ALIASES.get(p, p))
                for p in dict.fromkeys([*wanted, "json_schema", "reasoning"])
            },
        )
        for m in models
    ]
    return ModelListing(
        models=rows,
        fetched_hours_ago=catalog.age_s() / 3600,
        title="free models" if free else "models",
    )


def load_listing(
    repo: Path, *, free: bool, supports: Sequence[str], refresh: bool
) -> tuple[ModelListing | None, str | None]:
    """The filtered listing for `repo`, plus a fetch warning, if any.

    `JevError` when the repo's config is unreadable. A missing catalog is not an error:
    the caller decides (`jev models` fails, `jev doctor` warns).
    """
    root = find_git_root(repo) or repo.resolve()
    try:
        config = load_config(root)
    except ConfigError as exc:
        raise JevError(str(exc)) from exc
    catalog, warning = fetch_catalog(root, config, refresh=refresh)
    if catalog is None:
        return None, warning
    return list_models(catalog, free=free, supports=supports), warning
