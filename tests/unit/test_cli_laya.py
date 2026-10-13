"""`jev laya tune` / `jev laya reset`, and tuned weights stored on learning profiles (#69)."""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.config.schema import DEFAULT_LAYA_WEIGHTS, LAYA_CRITERIA
from jev_agent.core.learning import Learning, LearningError
from jev_agent.core.models import Decision, DecisionRequest, Idea, RunState
from jev_agent.core.store import Store

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n'
TUNED = DEFAULT_LAYA_WEIGHTS | {"safety": 0.25, "simplicity": 0.10}


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(CONFIG)
    return fixture_repo


def _jev(repo: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)])
    return result.exit_code, " ".join(re.sub(r"[─-╿]", " ", result.output).split())


def _store(repo: Path) -> Store:
    (repo / ".jev").mkdir(exist_ok=True)
    return Store.open(repo / ".jev" / "state.db")


def _seed(repo: Path, passed: int, failed: int) -> None:
    """Ended runs with one consensus D1 each: safety scored 5 when it passed, 2 when not."""
    req = DecisionRequest(point="D1", state={}, questions={"clear": ["plan", "ask_human"]})
    with _store(repo) as store:
        for state, count, safety in ((RunState.DONE, passed, 5.0), (RunState.FAILED, failed, 2.0)):
            for _ in range(count):
                run = store.create_run(Idea(text="idea", repo_path=repo))
                store.set_run_state(run.id, state)
                scores = dict.fromkeys(LAYA_CRITERIA, 3.0) | {"safety": safety}
                decision = Decision(
                    point="D1",
                    answers={"clear": "plan"},
                    dispatcher="consensus",
                    details={"laya": {"clear": {"scores": {"plan": scores}}}},
                )
                store.record_decision(run.id, None, req, decision)


def _weights(repo: Path, profile: str = "default") -> dict[str, float] | None:
    with _store(repo) as store:
        learning = Learning.for_store(store)
        try:
            return learning.laya_weights(profile)
        finally:
            learning.close()


def test_tune_shows_then_applies_and_reset_clears(repo: Path) -> None:
    assert _jev(repo, "learning", "enable")[0] == 0
    _seed(repo, passed=5, failed=5)

    code, out = _jev(repo, "laya", "tune")
    assert code == 0, out
    assert "current weights: defaults; outcomes: 5 passed, 5 failed" in out
    assert "safety 5.00 2.00 0.200" in out and "Not applied" in out
    assert _weights(repo) is None

    code, out = _jev(repo, "laya", "tune", "--apply")
    assert code == 0 and "Saved on profile default" in out, out
    tuned = _weights(repo)
    assert tuned is not None and tuned["safety"] > 0.2

    code, out = _jev(repo, "laya", "tune")
    assert code == 0 and "current weights: profile default" in out

    code, out = _jev(repo, "laya", "reset")
    assert code == 0 and "Profile default has no tuned weights" in out
    assert _weights(repo) is None


def test_tune_without_enough_outcomes(repo: Path) -> None:
    assert _jev(repo, "learning", "enable")[0] == 0
    _seed(repo, passed=2, failed=1)
    code, out = _jev(repo, "laya", "tune")
    assert code == 0 and "No suggestion: need at least 5 passed and 5 failed" in out
    assert _jev(repo, "laya", "tune", "--min-outcomes", "1")[0] == 0
    code, out = _jev(repo, "laya", "tune", "--apply")
    assert code == 1 and "nothing to apply" in out


def test_apply_refuses_when_jev_toml_sets_the_weights(repo: Path) -> None:
    weights = "\n".join(f"{c} = {w}" for c, w in DEFAULT_LAYA_WEIGHTS.items())
    (repo / "jev.toml").write_text(f"{CONFIG}[dispatcher.laya.weights]\n{weights}\n")
    assert _jev(repo, "learning", "enable")[0] == 0
    _seed(repo, passed=5, failed=5)
    code, out = _jev(repo, "laya", "tune", "--apply")
    assert code == 1 and "jev.toml sets [dispatcher.laya] weights" in out, out
    assert "current weights: jev.toml" in out
    assert _weights(repo) is None


def test_unknown_profile(repo: Path) -> None:
    _seed(repo, passed=0, failed=0)
    code, out = _jev(repo, "laya", "tune", "--profile", "nope")
    assert code == 1 and "no learning profile named 'nope'" in out


def test_profile_weights_in_repo_and_shared_scope(repo: Path) -> None:
    with _store(repo) as store:
        learning = Learning.for_store(store)
        try:
            learning.create("work")
            learning.create("team", shared=True)
            for name in ("work", "team"):
                assert learning.laya_weights(name) is None
                learning.set_laya_weights(name, TUNED)
                assert learning.laya_weights(name) == TUNED
            with pytest.raises(LearningError, match="add up to 1"):
                learning.set_laya_weights("work", dict.fromkeys(LAYA_CRITERIA, 0.5))
            assert learning.laya_weights("work") == TUNED
            learning.set_laya_weights("team", None)
            assert learning.laya_weights("team") is None
        finally:
            learning.close()


def test_stored_weights_that_no_longer_validate_can_be_reset(repo: Path) -> None:
    assert _jev(repo, "learning", "enable")[0] == 0
    with _store(repo) as store:
        renamed = {"accuracy" if c == "correctness" else c: w for c, w in TUNED.items()}
        store.connection.execute(
            "UPDATE learning_profiles SET laya_weights_json = ?", (json.dumps(renamed),)
        )
    with pytest.raises(LearningError, match="invalid Laya weights"):
        _weights(repo)
    code, out = _jev(repo, "laya", "tune")
    assert code == 1 and "invalid Laya weights" in out
    assert _jev(repo, "laya", "reset")[0] == 0
    assert _weights(repo) is None
