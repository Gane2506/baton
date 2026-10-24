"""Round-2 re-grade residuals (issue #232) — the part NOT already merged by R3-02 (#238).

#238 merged first and covers the queue allowlist, lesson/profile no-ops and
validation, the dead Laya-weights API in profiles/registry, the `credentials.txt`
policy gap, and the ship merge/sync asserts. What is left for this issue:

- cache/harness: the dead `laya_weights_fp` fingerprint plumbing
  (`tuned_laya_fingerprint`, `decision_cache_key(..., laya_weights_fp=...)`)
- learning/weights: the dead `_stored_weights` helper (its only consumer was the
  deleted `get_laya_weights`) and the `assert isinstance(weights, dict)` in
  `_checked_weights`
- dispatch/jev.py, dashboard/app.py: the last production `assert`s in the files
  issue #232 names (`last_error`, the HTTPException handler)
- store/base.py: the abstract `_update_queue_item` declaration, matching
  `_update_run` / `_update_task`
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from jev_agent.config import JevConfig
from jev_agent.core.cache import decision_cache_key
from jev_agent.core.models import DecisionRequest, Idea
from jev_agent.core.store import Store
from jev_agent.core.store.base import StoreBase


def _assert_nodes(module: object) -> list[ast.Assert]:
    return [n for n in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(n, ast.Assert)]


# ---- 1. cache/harness: the Laya-weights fingerprint plumbing is gone ------------------


def test_laya_weights_fingerprint_plumbing_is_gone() -> None:
    import jev_agent.core.cache as cache_module

    assert not hasattr(cache_module, "tuned_laya_fingerprint")
    assert "laya_weights_fp" not in inspect.signature(decision_cache_key).parameters
    req = DecisionRequest(
        point="D1",  # type: ignore[arg-type]
        state={"idea": "add search", "round": 1},
        questions={"action": ["plan", "ask"]},
    )
    key = decision_cache_key(req, dispatcher_name="stub", config=JevConfig(), run_id="r")
    assert key.key_hash
    with pytest.raises(TypeError):
        decision_cache_key(  # type: ignore[call-arg]
            req, dispatcher_name="stub", config=JevConfig(), run_id="r", laya_weights_fp="x"
        )


def test_harness_no_longer_threads_a_laya_weights_fingerprint() -> None:
    import jev_agent.core.harness as harness_module

    assert "laya_weights_fp" not in inspect.getsource(harness_module.Harness)


# ---- 2. learning/weights: `_stored_weights` is gone -----------------------------------


def test_stored_weights_helper_is_gone() -> None:
    import jev_agent.core.learning as learning_pkg
    import jev_agent.core.learning.weights as weights_module

    assert not hasattr(weights_module, "_stored_weights")
    assert "_stored_weights" not in weights_module.__all__
    assert "_stored_weights" not in learning_pkg.__all__
    assert not hasattr(learning_pkg, "_stored_weights")


# ---- 3. production asserts replaced in the remaining #232 files ------------------------


@pytest.mark.parametrize(
    "module_name",
    ["jev_agent.dispatch.jev", "jev_agent.dashboard.app", "jev_agent.core.learning.weights"],
)
def test_no_production_asserts_in_residual_modules(module_name: str) -> None:
    import importlib

    module = importlib.import_module(module_name)
    assert _assert_nodes(module) == []


# ---- 4. store/base.py: `_update_queue_item` is declared like its siblings -------------


def test_store_base_declares_update_queue_item() -> None:
    assert "_update_queue_item" in StoreBase.__abstractmethods__
    # The concrete implementation lives on the queue mixin, not the base.
    assert Store._update_queue_item is not StoreBase._update_queue_item  # type: ignore[attr-defined]


def test_queue_update_uses_the_allowlisted_columns(tmp_path: Path) -> None:
    """The concrete `_update_queue_item` still goes through the #224 allowlist
    (declared in base.py, enforced in queue.py — #238 added the enforcement)."""
    from jev_agent.core.store.base import _QUEUE_UPDATE_COLUMNS, _check_update_assignments

    with Store.open(tmp_path / "repo" / ".jev" / "state.db") as store:
        item = store.add_queue_item("an idea")
        run = store.create_run(Idea(text="a", repo_path=tmp_path))
        store.set_queue_status(item.id, "waiting", run_id=run.id, note="n")
        assert store.get_queue_item(item.id).status == "waiting"
        with pytest.raises(ValueError, match="allowlist"):
            store._update_queue_item(item.id, "position = 5", ())
    for fragment in ("status = ?", "run_id = ?", "note = ?", "status = ?, run_id = ?, note = ?"):
        _check_update_assignments(fragment, _QUEUE_UPDATE_COLUMNS)
