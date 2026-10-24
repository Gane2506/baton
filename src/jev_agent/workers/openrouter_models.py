"""The OpenRouter model catalog: `GET {base_url}/models`, parsed and cached on disk.

Used for free models (docs/config.md "Free models", docs/verification.md section 7):

- A model is **free** when its prompt and completion prices are both `"0"`, or its id ends
  with `:free` (OpenRouter's free variant suffix).
- Capabilities come from each model's `supported_parameters`; json_schema structured output is
  listed as `structured_outputs`. A model without that list has *unknown* capabilities.
- The catalog is cached as JSON at `.jev/cache/openrouter-models.json` and refreshed when older
  than the TTL (24h by default). The HTTP client is injectable, so tests never hit the network.

Nothing here is secret: the endpoint is public, and an API key, when sent, is never stored.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from jev_agent.config.defaults import STATE_DIRNAME

CACHE_FILENAME = "openrouter-models.json"
DEFAULT_TTL_S = 24 * 3600
FETCH_TIMEOUT_S = 15.0
FREE_SUFFIX = ":free"
# `supported_parameters` entries meaning json_schema structured output. `structured_outputs` is
# the name OpenRouter uses (docs/verification.md section 4); `--supports json_schema` maps to it.
JSON_SCHEMA_PARAMS = frozenset({"structured_outputs"})
PARAM_ALIASES = {"json_schema": "structured_outputs"}
# Router slugs pick a model at random per request; jev keeps them out of `"free"` expansion
# (an explicit `models = ["openrouter/free"]` still works).
ROUTER_PREFIX = "openrouter/"


class CatalogError(Exception):
    """The catalog could not be fetched or read. The message is user-facing."""


def is_free_slug(model: str | None) -> bool:
    """True for ids that are free by name alone: the `:free` variant or the free router."""
    return bool(model) and (str(model).endswith(FREE_SUFFIX) or model == "openrouter/free")


def _price(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str = ""
    context_length: int | None = None
    prompt_price: float | None = None  # USD per token
    completion_price: float | None = None
    supported_parameters: tuple[str, ...] | None = None  # None: not listed (unknown)

    @property
    def free(self) -> bool:
        priced_free = self.prompt_price == 0 and self.completion_price == 0
        return priced_free or self.id.endswith(FREE_SUFFIX)

    @property
    def is_router(self) -> bool:
        return self.id.startswith(ROUTER_PREFIX)

    def supports(self, param: str) -> bool | None:
        """Whether the model lists `param` (aliases such as json_schema resolved); None when the
        model lists no parameters at all."""
        if not self.supported_parameters:
            return None
        param = PARAM_ALIASES.get(param, param)
        return param in self.supported_parameters

    def supports_json_schema(self) -> bool | None:
        if not self.supported_parameters:
            return None
        return any(p in self.supported_parameters for p in JSON_SCHEMA_PARAMS)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        if self.supported_parameters is not None:
            data["supported_parameters"] = list(self.supported_parameters)
        return data


def parse_model(raw: Mapping[str, Any]) -> ModelInfo | None:
    """One model object from `GET /models`; None when it has no usable id."""
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    pricing = raw_pricing if isinstance(raw_pricing := raw.get("pricing"), dict) else {}
    params = raw.get("supported_parameters")
    supported = tuple(p for p in params if isinstance(p, str)) if isinstance(params, list) else None
    context = raw.get("context_length")
    name = raw.get("name")
    return ModelInfo(
        id=model_id,
        name=name if isinstance(name, str) else "",
        context_length=context
        if isinstance(context, int) and not isinstance(context, bool)
        else None,
        prompt_price=_price(pricing.get("prompt")),
        completion_price=_price(pricing.get("completion")),
        supported_parameters=supported,
    )


def parse_models(body: Any) -> list[ModelInfo]:
    """The `data` array of a `GET /models` response (or of a cache file)."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise CatalogError("model list response has no `data` array")
    return [m for raw in data if isinstance(raw, dict) and (m := parse_model(raw)) is not None]


@dataclass
class ModelCatalog:
    models: list[ModelInfo]
    fetched_at: float  # unix seconds
    source: str = ""  # the URL it came from
    _by_id: dict[str, ModelInfo] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_id = {m.id: m for m in self.models}

    def get(self, model_id: str) -> ModelInfo | None:
        return self._by_id.get(model_id)

    def free(self) -> list[ModelInfo]:
        return [m for m in self.models if m.free]

    def is_free(self, model_id: str | None) -> bool:
        if not model_id:
            return False
        info = self._by_id.get(model_id)
        return info.free if info is not None else is_free_slug(model_id)

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.fetched_at)

    def is_stale(self, ttl_s: float, now: float | None = None) -> bool:
        return self.age_s(now) > ttl_s

    def filtered(self, *, free: bool = False, supports: Iterable[str] = ()) -> list[ModelInfo]:
        wanted = list(supports)
        return [
            m
            for m in self.models
            if (not free or m.free) and all(m.supports(p) is not False for p in wanted)
        ]

    # ---- cache -------------------------------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "fetched_at": self.fetched_at,
            "source": self.source,
            "data": [m.to_json() for m in self.models],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> ModelCatalog | None:
        """The cached catalog, or None when there is no readable cache file."""
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(body, dict) or not isinstance(body.get("fetched_at"), int | float):
            return None
        if not isinstance(body.get("data"), list):
            return None
        # One corrupt entry must not discard the whole cache (issue #204):
        # skip entries that fail validation and keep the rest. A cache with no
        # usable entries at all is still unusable, so it returns None and the
        # caller refetches instead of trusting an empty catalog.
        models = []
        for raw in body["data"]:
            try:
                models.append(_from_cache(raw))
            except (KeyError, TypeError, ValueError):
                continue
        if not models:
            return None
        source = body.get("source")
        return cls(models, float(body["fetched_at"]), source if isinstance(source, str) else "")


def _from_cache(raw: dict[str, Any]) -> ModelInfo:
    # The cache stores `to_json()` output (flat `prompt_price`/`completion_price`); adapt it
    # to the `GET /models` shape so the cache path gets exactly `parse_model`'s validation
    # (a corrupt entry degrades to "no usable id" instead of a half-validated ModelInfo).
    if not isinstance(raw, dict):
        raise ValueError(f"cached model entry is not an object: {raw!r}")
    adapted = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "context_length": raw.get("context_length"),
        "pricing": {
            "prompt": raw.get("prompt_price"),
            "completion": raw.get("completion_price"),
        },
        "supported_parameters": raw.get("supported_parameters"),
    }
    model = parse_model(adapted)
    if model is None:
        raise ValueError(f"cached model has no usable id: {raw.get('id')!r}")
    return model


def cache_path(root: Path) -> Path:
    return root / STATE_DIRNAME / "cache" / CACHE_FILENAME


def fetch_catalog(
    base_url: str,
    *,
    api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = FETCH_TIMEOUT_S,
    now: float | None = None,
) -> ModelCatalog:
    """`GET {base_url}/models`. The endpoint answers without a key; one is sent if given."""
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(transport=transport, timeout=timeout_s) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise CatalogError(f"cannot fetch {url}: {type(exc).__name__}") from exc
    if not response.is_success:
        raise CatalogError(f"cannot fetch {url}: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise CatalogError(f"{url} did not return JSON") from exc
    return ModelCatalog(parse_models(body), time.time() if now is None else now, url)


def load_catalog(
    path: Path,
    base_url: str,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    refresh: bool = False,
    api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    allow_fetch: bool = True,
) -> tuple[ModelCatalog | None, str | None]:
    """The catalog from the cache, refreshed when stale, missing or `refresh` is set.

    Returns (catalog, warning). A failed refresh keeps a stale cache and says so; with no cache
    at all the catalog is None.
    """
    cached = ModelCatalog.load(path)
    if not refresh and cached is not None and not cached.is_stale(ttl_s):
        return cached, None
    if not allow_fetch:
        return cached, None
    try:
        fresh = fetch_catalog(base_url, api_key=api_key, transport=transport)
    except CatalogError as exc:
        if cached is None:
            return None, f"{exc}; no cached model list"
        hours = cached.age_s() / 3600
        return cached, f"{exc}; using the cached model list ({hours:.0f}h old)"
    try:
        fresh.save(path)
    except OSError as exc:
        return fresh, f"cannot write the model cache {path}: {exc}"
    return fresh, None


def free_for_role(
    catalog: ModelCatalog | None, *, structured: bool, limit: int
) -> tuple[list[str], list[str]]:
    """Free model ids for a role, at most `limit`: (ids, ids whose json_schema support is
    unknown). Structured roles drop models known not to support json_schema; models with
    unknown support are kept (and reported). Router slugs are left out."""
    if catalog is None or limit <= 0:
        return [], []
    known: list[str] = []
    unknown: list[str] = []
    for model in catalog.free():
        if model.is_router:
            continue
        support = model.supports_json_schema() if structured else True
        if support is False:
            continue
        (known if support else unknown).append(model.id)
    chosen = (known + unknown)[:limit]  # known-good first
    return chosen, [m for m in chosen if m in unknown]
