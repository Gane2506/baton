"""Issue #205 (swarm review: config remaining gaps): NaN weights, secret scan,
cache limits, autonomy project folder, and non-finite numerics generally."""

from __future__ import annotations

import math

import pytest
from pydantic import BaseModel, ValidationError

from jev_agent.config.schema import (
    AutonomyConfig,
    BudgetConfig,
    CacheSettings,
    CacheTtlOverrides,
    ClaudeCodeConfig,
    CostSettings,
    JevConfig,
    JevSettings,
    ModelsConfig,
    QualityConfig,
    find_secret_keys,
)
from jev_agent.core.learning import LearningError, _checked_weights

NAN = float("nan")
INF = float("inf")

GOOD_WEIGHTS = {
    "correctness": 0.30,
    "safety": 0.20,
    "simplicity": 0.15,
    "maintainability": 0.15,
    "tool_fit": 0.10,
    "user_preference": 0.10,
}


def _weights(**overrides: float) -> dict[str, float]:
    weights = dict(GOOD_WEIGHTS)
    weights.update(overrides)
    return weights


# ---- 1. NaN / infinite Laya weights ----------------------------------------------------
# LayaSettings is gone (issue #193); stored learning-profile weights are validated by
# `_checked_weights` in core/learning.py, which keeps the same finite/sum rules.


@pytest.mark.parametrize(
    "bad",
    [
        {name: NAN for name in GOOD_WEIGHTS},  # all-NaN used to pass the sum check
        _weights(correctness=NAN),
        _weights(safety=INF),
        _weights(tool_fit=-INF),
    ],
)
def test_non_finite_weights_are_rejected(bad: dict[str, float]) -> None:
    with pytest.raises(LearningError, match="finite"):
        _checked_weights("test", bad)


def test_good_weights_still_pass() -> None:
    assert _checked_weights("test", GOOD_WEIGHTS) == GOOD_WEIGHTS


def test_weights_must_still_add_up_to_one() -> None:
    with pytest.raises(LearningError, match="add up to 1"):
        _checked_weights("test", _weights(correctness=0.31))


# ---- systemic: every numeric gate rejects non-finite inputs ----------------------------


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (JevSettings, {"timeout_s": INF}),
        (JevSettings, {"timeout_s": NAN}),
        (JevSettings, {"min_confidence": NAN}),
        (CostSettings, {"native_price_per_million": INF}),
        (CostSettings, {"openrouter_price_per_million": INF}),
        (CostSettings, {"worker_price_per_million": NAN}),
        (CostSettings, {"warn_fraction": INF}),
        (CostSettings, {"refuse_fraction": NAN}),
        (BudgetConfig, {"per_run_usd": INF}),
        (BudgetConfig, {"per_task_usd": NAN}),
        (ModelsConfig, {"cache_ttl_hours": INF}),
        (QualityConfig, {"min_coverage": NAN}),
        (ClaudeCodeConfig, {"max_budget_usd": INF}),
        (CacheSettings, {"ttl_hours": INF}),
        (CacheSettings, {"ttl_hours": NAN}),
        (CacheSettings, {"max_mb": INF}),
        (CacheTtlOverrides, {"roles": NAN}),
        (CacheTtlOverrides, {"chat": INF}),
    ],
)
def test_non_finite_numbers_are_rejected(model: type[BaseModel], kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(kwargs)


def test_finite_numbers_still_pass() -> None:
    assert JevConfig.model_validate({"budget": {"per_run_usd": 5.0}}).budget.per_run_usd == 5.0
    assert CacheSettings.model_validate({"ttl_hours": 24}).ttl_hours == 24
    assert math.isfinite(CostSettings().native_price_per_million)


# ---- 2. secret-key scan -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["credentials", "credential", "passwd", "user_passwd"])
def test_credential_and_passwd_keys_are_flagged(name: str) -> None:
    assert find_secret_keys({"extra": {name: "hunter2"}}) == [f"extra.{name}"]


@pytest.mark.parametrize("name", ["tokenizer", "tokenizer_model", "mytokenizer"])
def test_tokenizer_is_not_a_secret(name: str) -> None:
    assert find_secret_keys({"extra": {name: "x"}}) == []


@pytest.mark.parametrize(
    "name", ["api_key", "token", "tokens", "password", "client_secret", "auth"]
)
def test_known_secret_keys_still_flagged(name: str) -> None:
    assert find_secret_keys({"extra": {name: "hunter2"}}) == [f"extra.{name}"]


def test_secret_scan_reaches_nested_tables() -> None:
    assert find_secret_keys({"a": {"b": [{"credentials": "x"}]}}) == ["a.b.0.credentials"]


# ---- 6. cache limits and autonomy project folder -----------------------------------------


@pytest.mark.parametrize("bad", [-5, -1])
def test_negative_cache_max_entries_rejected(bad: int) -> None:
    with pytest.raises(ValidationError):
        CacheSettings.model_validate({"max_entries": bad})


def test_zero_cache_max_entries_means_keep_nothing() -> None:
    assert CacheSettings.model_validate({"max_entries": 0}).max_entries == 0


@pytest.mark.parametrize(
    ("field", "bad"),
    [("roles", -1), ("roles", 0), ("roles", NAN), ("chat", -1), ("chat", 0), ("chat", INF)],
)
def test_cache_ttl_overrides_must_be_positive(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        CacheTtlOverrides.model_validate({field: bad})


def test_filesystem_root_is_not_a_project_folder() -> None:
    with pytest.raises(ValidationError, match="too broad"):
        AutonomyConfig.model_validate({"mode": "medium", "project_folder": "/"})


def test_filesystem_root_rejected_whatever_the_tier() -> None:
    # Otherwise flipping the tier to medium later would silently activate it.
    with pytest.raises(ValidationError, match="too broad"):
        AutonomyConfig.model_validate({"mode": "standard", "project_folder": "/"})


def test_scoped_project_folder_still_accepted() -> None:
    cfg = AutonomyConfig.model_validate(
        {"mode": "medium", "project_folder": "/home/user/projects/app"}
    )
    assert str(cfg.project_folder) == "/home/user/projects/app"


def test_empty_project_folder_still_means_unset() -> None:
    assert AutonomyConfig.model_validate({"project_folder": ""}).project_folder is None
