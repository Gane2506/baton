"""Config keys that used to be accepted but never used or checked (issue #30)."""

from pathlib import Path

import pytest

from jev_agent.config import ConfigError, JevConfig, load_config
from jev_agent.config.schema import DECISION_QUESTIONS, WORKER_EFFORTS
from jev_agent.core.models import Idea, Plan, TaskCard, ValidationResult
from jev_agent.dispatch import requests as r
from jev_agent.workers import api, claude_code, codex


def _load(tmp_path: Path, text: str) -> JevConfig:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "jev.toml").write_text(text, encoding="utf-8")
    return load_config(repo, tmp_path / "missing-user-config.toml")


def _error(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, text)
    return str(exc.value)


# ---- [dispatcher] fallback -------------------------------------------------------------------


def test_fallback_rules_is_still_accepted_for_old_files(tmp_path: Path) -> None:
    cfg = _load(tmp_path, '[dispatcher]\nfallback = "rules"\n')
    assert cfg == JevConfig()
    assert "fallback" not in type(cfg.dispatcher).model_fields


@pytest.mark.parametrize("value", ["jev", "rule"])
def test_fallback_other_than_rules_is_rejected(tmp_path: Path, value: str) -> None:
    message = _error(tmp_path, f'[dispatcher]\nfallback = "{value}"\n')
    assert "dispatcher: fallback" in message
    assert 'only "rules"' in message and "remove the key" in message


# ---- [roles.<role>] efforts ------------------------------------------------------------------


def test_effort_typo_is_rejected_with_accepted_levels(tmp_path: Path) -> None:
    message = _error(tmp_path, '[roles.coder]\nefforts = ["low", "hgih"]\n')
    assert "roles.coder.efforts" in message and "'hgih'" in message
    assert "claude_code, codex" in message and "xhigh" in message


def test_effort_none_of_the_role_workers_accepts_is_rejected(tmp_path: Path) -> None:
    text = '[roles.reviewer]\nworkers = ["api"]\nefforts = ["xhigh"]\n'
    message = _error(tmp_path, text)
    assert "roles.reviewer.efforts" in message and "'xhigh'" in message
    assert "api accepts: low, medium, high" in message


def test_effort_accepted_by_one_of_the_role_workers_is_fine(tmp_path: Path) -> None:
    cfg = _load(tmp_path, '[roles.planner]\nefforts = ["high", "xhigh", "max"]\n')
    assert cfg.roles.planner.efforts == ["high", "xhigh", "max"]
    assert _load(tmp_path / "b", "[roles.coder]\nefforts = []\n").roles.coder.efforts == []


def test_workers_use_the_config_effort_levels() -> None:
    assert claude_code.EFFORTS is WORKER_EFFORTS["claude_code"]
    assert codex.EFFORTS is WORKER_EFFORTS["codex"]
    assert api.EFFORTS is WORKER_EFFORTS["api"]
    assert set(WORKER_EFFORTS) == set(JevConfig().workers.names())


# ---- [dispatcher.jev.instructions] -----------------------------------------------------------


@pytest.mark.parametrize("key", ["D4.efort", "D9.effort", "D3.worker", "efort", "d4.effort"])
def test_unknown_instruction_key_is_rejected(tmp_path: Path, key: str) -> None:
    message = _error(tmp_path, f'[dispatcher.jev.instructions]\n"{key}" = "x"\n')
    assert "dispatcher.jev.instructions" in message and repr(key) in message
    assert "<point>.<question>" in message


def test_instruction_key_message_lists_the_point_questions(tmp_path: Path) -> None:
    message = _error(tmp_path, '[dispatcher.jev.instructions]\n"D4.efort" = "x"\n')
    assert "D4 asks: complexity, worker, model, effort" in message


def test_valid_instruction_keys_are_accepted(tmp_path: Path) -> None:
    text = '[dispatcher.jev.instructions]\n"D4.effort" = "a"\nmodel = "b"\n"D5.action" = "c"\n'
    cfg = _load(tmp_path, text)
    assert cfg.dispatcher.jev.instructions == {"D4.effort": "a", "model": "b", "D5.action": "c"}


def test_decision_questions_match_the_requests(tmp_path: Path) -> None:
    opts = r.role_options(JevConfig(), "coder", ["claude_code", "codex", "api"])
    card = TaskCard(id="T1", title="t", description="d")
    requests = [
        r.d1_intake(Idea(text="x", repo_path=tmp_path), opts),
        r.d2_validator(Plan(summary="s", tasks=[card]), 1, opts, "planner", "claude_code", None),
        r.d3_after_validation(ValidationResult(verdict="approve"), 1, 3),
        r.d4_task(card, opts, 0.5, 5.0),
        r.d5_after_attempt(
            card,
            attempt=1,
            max_attempts=3,
            assignment={"worker": "codex", "model": None, "effort": "low"},
            worker_status="failure",
            summary="s",
            tests="skipped",
            review="skipped",
            can_reassign=True,
            task_spend_usd=0,
            task_budget_usd=1.5,
        ),
        r.d6_integration(1, True, [], "passed", fix_rounds=0, max_fix_rounds=2),
        r.d7_learn("done", [], 1, 0, opts, 0.1, 5.0),
        r.d8_reviewer(
            card,
            attempt=1,
            coder={"worker": "codex", "model": None, "effort": "low"},
            tests="passed",
            quality="skipped",
            diff={"files": 1, "lines_changed": 2},
            reviewer=opts,
        ),
    ]
    assert {req.point: tuple(req.questions) for req in requests} == DECISION_QUESTIONS
