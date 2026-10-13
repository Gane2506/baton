"""The OpenRouter model catalog: parsing, free detection, filters and the on-disk cache."""

import json
import time
from pathlib import Path

import httpx
import pytest

from jev_agent.workers.openrouter_models import (
    CatalogError,
    ModelCatalog,
    cache_path,
    fetch_catalog,
    free_for_role,
    is_free_slug,
    load_catalog,
    parse_models,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "openrouter"
BASE_URL = "https://openrouter.test/api/v1"


def _body() -> dict[str, object]:
    data: dict[str, object] = json.loads((FIXTURES / "models.json").read_text())
    return data


def _catalog(fetched_at: float | None = None) -> ModelCatalog:
    return ModelCatalog(parse_models(_body()), time.time() if fetched_at is None else fetched_at)


def _transport(calls: list[httpx.Request], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "down"}})
        return httpx.Response(200, json=_body())

    return httpx.MockTransport(handler)


def test_parse_models_fields_and_free_detection() -> None:
    catalog = _catalog()
    assert len(catalog.models) == 8  # the entry without an id is skipped
    mini = catalog.get("openai/gpt-5-mini")
    assert mini is not None
    assert mini.context_length == 400000
    assert mini.prompt_price == pytest.approx(0.00000025)
    assert not mini.free
    assert mini.supports("json_schema") is True  # alias for structured_outputs
    free_ids = [m.id for m in catalog.free()]
    assert free_ids == [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "poolside/laguna-s-2.1:free",
        "inclusionai/ling-3.1-flash",  # price 0 without the :free suffix
        "liquid/lfm-2.5-2.6b:free",
        "apodex/apodex-1.1-mini:free",
        "openrouter/free",
    ]


def test_capabilities_known_and_unknown() -> None:
    catalog = _catalog()
    laguna = catalog.get("poolside/laguna-s-2.1:free")
    lfm = catalog.get("liquid/lfm-2.5-2.6b:free")
    assert laguna is not None and lfm is not None
    assert laguna.supports_json_schema() is False
    assert laguna.supports("reasoning") is True
    assert lfm.supports_json_schema() is None  # no supported_parameters listed
    assert lfm.supports("tools") is None


def test_is_free_by_slug_and_catalog() -> None:
    assert is_free_slug("x/y:free") and is_free_slug("openrouter/free")
    assert not is_free_slug("x/y") and not is_free_slug(None)
    catalog = _catalog()
    assert catalog.is_free("inclusionai/ling-3.1-flash")
    assert catalog.is_free("unknown/model:free")  # not in the catalog: by name
    assert not catalog.is_free("anthropic/claude-sonnet-5")


def test_filtered_by_free_and_supports() -> None:
    catalog = _catalog()
    ids = [m.id for m in catalog.filtered(free=True, supports=["structured_outputs"])]
    # Unknown capability (lfm) is kept by `filtered`; known "no" (laguna) is dropped.
    assert "poolside/laguna-s-2.1:free" not in ids
    assert "liquid/lfm-2.5-2.6b:free" in ids
    assert "openai/gpt-5-mini" not in ids


def test_free_for_role_filters_structured_and_caps() -> None:
    catalog = _catalog()
    ids, unknown = free_for_role(catalog, structured=True, limit=10)
    assert ids == [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "inclusionai/ling-3.1-flash",
        "apodex/apodex-1.1-mini:free",
        "liquid/lfm-2.5-2.6b:free",  # unknown support: kept, last, and reported
    ]
    assert unknown == ["liquid/lfm-2.5-2.6b:free"]
    assert "openrouter/free" not in ids  # routers are never expanded
    capped, _ = free_for_role(catalog, structured=True, limit=2)
    assert capped == ids[:2]
    loose, _ = free_for_role(catalog, structured=False, limit=10)
    assert "poolside/laguna-s-2.1:free" in loose
    assert free_for_role(None, structured=True, limit=3) == ([], [])


def test_cache_roundtrip_and_ttl(tmp_path: Path) -> None:
    path = cache_path(tmp_path)
    assert path == tmp_path / ".jev" / "cache" / "openrouter-models.json"
    old = _catalog(fetched_at=time.time() - 25 * 3600)
    old.save(path)
    loaded = ModelCatalog.load(path)
    assert loaded is not None
    assert [m.id for m in loaded.models] == [m.id for m in old.models]
    assert loaded.get("liquid/lfm-2.5-2.6b:free") is not None
    assert loaded.get("liquid/lfm-2.5-2.6b:free").supported_parameters is None  # type: ignore[union-attr]
    assert loaded.is_stale(24 * 3600)
    assert not loaded.is_stale(48 * 3600)


def test_load_cache_tolerates_garbage(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    assert ModelCatalog.load(path) is None
    path.write_text("not json")
    assert ModelCatalog.load(path) is None
    path.write_text('{"fetched_at": "yesterday", "data": []}')
    assert ModelCatalog.load(path) is None


def test_load_catalog_uses_fresh_cache_without_fetching(tmp_path: Path) -> None:
    path = cache_path(tmp_path)
    _catalog().save(path)
    calls: list[httpx.Request] = []
    catalog, warning = load_catalog(path, BASE_URL, transport=_transport(calls))
    assert catalog is not None and warning is None
    assert calls == []


def test_load_catalog_refetches_stale_or_on_refresh(tmp_path: Path) -> None:
    path = cache_path(tmp_path)
    _catalog(fetched_at=time.time() - 30 * 3600).save(path)
    calls: list[httpx.Request] = []
    catalog, warning = load_catalog(path, BASE_URL, transport=_transport(calls), api_key="k")
    assert warning is None and catalog is not None
    assert [str(r.url) for r in calls] == [f"{BASE_URL}/models"]
    assert calls[0].headers["authorization"] == "Bearer k"
    assert not catalog.is_stale(3600)
    reloaded = ModelCatalog.load(path)
    assert reloaded is not None and not reloaded.is_stale(3600)  # the cache was rewritten
    load_catalog(path, BASE_URL, transport=_transport(calls), refresh=True)
    assert len(calls) == 2
    assert "authorization" not in calls[1].headers  # no key: the endpoint is public


def test_load_catalog_keeps_stale_cache_when_fetch_fails(tmp_path: Path) -> None:
    path = cache_path(tmp_path)
    _catalog(fetched_at=time.time() - 30 * 3600).save(path)
    catalog, warning = load_catalog(path, BASE_URL, transport=_transport([], status=500))
    assert catalog is not None
    assert warning is not None and "HTTP 500" in warning and "cached" in warning
    empty, warning = load_catalog(tmp_path / "none.json", BASE_URL, transport=_transport([], 500))
    assert empty is None and warning is not None and "no cached" in warning


def test_load_catalog_without_fetch(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    catalog, warning = load_catalog(
        tmp_path / "none.json", BASE_URL, transport=_transport(calls), allow_fetch=False
    )
    assert catalog is None and warning is None and calls == []


def test_fetch_catalog_errors() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(CatalogError, match="ConnectError"):
        fetch_catalog(BASE_URL, transport=httpx.MockTransport(broken))
    with pytest.raises(CatalogError, match="no `data`"):
        fetch_catalog(
            BASE_URL, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        )
