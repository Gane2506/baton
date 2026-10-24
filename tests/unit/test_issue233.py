"""R3-02 (issue #233): one test per small-3 wart fixed.

Each test pins the fixed behaviour of one file that moved 3 -> 4. The
`prepare_ship` dedupe test lives in tests/unit/test_ship.py, next to the
`done_run` fixture it needs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from jev_agent.app.context import FAKE_WORKERS_ENV
from jev_agent.config import load_config
from jev_agent.core.kpi import RunKpis
from jev_agent.core.learning import Learning, SkillProposal
from jev_agent.core.learning.models import MAX_LESSON_CHARS, LearningError
from jev_agent.core.ship import Gh, ShipError
from jev_agent.core.ship.gh import _timeout_s
from jev_agent.core.ship.merge import _view_for_merge, merge_command
from jev_agent.core.ship.pr_body import kpi_rows
from jev_agent.core.ship.pr_lookup import parse_pr_list
from jev_agent.core.ship.sync import find_revert
from jev_agent.core.store import ShippedPrRecord, Store
from jev_agent.core.store.rows import _aware_time, _time
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.servers.policy.paths import sensitive


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


# ---- ship/pr_lookup.py: a missing headRefName is no match -------------------------------


def test_pr_lookup_missing_head_ref_name_is_no_match() -> None:
    text = (
        '[{"number": 7, "url": "https://github.com/o/r/pull/7", "state": "OPEN"},'
        ' {"number": 8, "url": "https://github.com/o/r/pull/8", "state": "OPEN",'
        ' "headRefName": "jev/r1"}]'
    )
    assert parse_pr_list(text, "jev/r1") is not None
    # Only the headless item matches nothing: the first item must not be returned.
    assert (
        parse_pr_list(
            '[{"number": 7, "url": "https://github.com/o/r/pull/7", "state": "OPEN"}]',
            "jev/r1",
        )
        is None
    )
    assert parse_pr_list('[{"number": 7, "state": "OPEN", "headRefName": 42}]', "jev/r1") is None
    assert (
        parse_pr_list('[{"number": 7, "state": "OPEN", "headRefName": "other"}]', "jev/r1") is None
    )


# ---- ship/pr_body.py: explicit tests_passed mapping --------------------------------------


def _kpis(**overrides: Any) -> RunKpis:
    base: dict[str, Any] = {
        "run_id": "r1",
        "total_cost_usd": 0.0,
        "wall_clock_s": None,
        "tasks": 0,
        "tasks_done": 0,
        "attempts_total": 0,
        "attempts_per_task": None,
        "first_pass_rate": None,
        "retries": 0,
        "escalations": 0,
        "reassigns": 0,
        "fix_rounds": 0,
        "quality_level": "none",
        "lessons_used": 0,
    }
    base.update(overrides)
    return RunKpis.model_validate(base)


def test_kpi_rows_names_all_three_test_states() -> None:
    rows = dict(kpi_rows(_kpis(tests_passed=True)))
    assert rows["Integration tests"] == "passed"
    rows = dict(kpi_rows(_kpis(tests_passed=False)))
    assert rows["Integration tests"] == "failed"
    rows = dict(kpi_rows(_kpis(tests_passed=None)))
    assert rows["Integration tests"] == "not run"


# ---- ship/merge.py, ship/sync.py: no asserts, explicit ShipError --------------------------


def _pr(**overrides: Any) -> ShippedPrRecord:
    base: dict[str, Any] = {
        "id": 1,
        "run_id": "r1",
        "remote": "origin",
        "repo": "o/r",
        "branch": "jev/r1",
        "base": "main",
        "number": 7,
        "url": None,
        "title": "t",
        "draft": False,
        "head_oid": "abc",
        "pushed_commits": 1,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "state": "open",
        "merged_at": None,
        "closed_at": None,
        "review_comments": 0,
        "reviews": 0,
        "ci_status": None,
        "commits_after_open": None,
        "additions": None,
        "deletions": None,
        "changed_files": None,
        "reverted": None,
        "last_synced_at": None,
        "raw": None,
    }
    base.update(overrides)
    return ShippedPrRecord(**base)


def test_merge_command_refuses_a_pr_without_number_or_head() -> None:
    with pytest.raises(ShipError, match="no number"):
        merge_command(_pr(number=None), "squash")
    with pytest.raises(ShipError, match="no number"):
        merge_command(_pr(head_oid=None), "squash")
    assert merge_command(_pr(), "squash")[:3] == ["gh", "pr", "merge"]


def test_view_for_merge_refuses_a_pr_without_number() -> None:
    with pytest.raises(ShipError, match="no number"):
        _view_for_merge(cast(Gh, None), _pr(number=None))


def test_find_revert_refuses_a_pr_without_number() -> None:
    with pytest.raises(ShipError, match="no number"):
        find_revert(cast(Gh, None), _pr(number=None))


# ---- ship/gh.py: _timeout_s reads the workspace constants ---------------------------------


def test_timeout_s_follows_workspace_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    import jev_agent.core.workspace as workspace

    monkeypatch.setattr(workspace, "GIT_QUICK_TIMEOUT_S", 3.0)
    monkeypatch.setattr(workspace, "GIT_SLOW_TIMEOUT_S", 9.0)
    assert _timeout_s(False) == 3.0
    assert _timeout_s(True) == 9.0


# ---- store/rows.py: one aware-UTC converter -------------------------------------------------


def test_time_treats_naive_stored_values_as_utc() -> None:
    naive = _time("2026-01-01T12:00:00")
    assert naive is not None and naive.tzinfo is not None
    assert naive == _aware_time("2026-01-01T12:00:00")
    assert _time("2026-01-01T12:00:00+02:00") == _aware_time("2026-01-01T12:00:00+02:00")
    assert _time(None) is None


# ---- store/queue.py: _update_queue_item allowlist -------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "repo" / ".jev" / "state.db") as s:
        yield s


def test_update_queue_item_rejects_columns_outside_the_allowlist(store: Store) -> None:
    item = store.add_queue_item("an idea")
    with pytest.raises(ValueError, match="outside the allowlist"):
        store._update_queue_item(item.id, "priority = ?", ("high",))
    with pytest.raises(ValueError, match="outside the allowlist"):
        store._update_queue_item(item.id, "status = ?; DELETE FROM queue", ("done",))
    store.set_queue_status(item.id, "done")
    assert store.get_queue_item(item.id).status == "done"


# ---- servers/policy/paths.py: credential stems ----------------------------------------------


@pytest.mark.parametrize(
    "name", ["credentials.txt", "secrets.env", "secret.yaml", "credentials", "secret"]
)
def test_sensitive_covers_credential_stems(name: str) -> None:
    assert sensitive(f"/tmp/{name}") is not None


@pytest.mark.parametrize("name", ["my-credentials.txt", "notes.txt", "secretary.md"])
def test_sensitive_does_not_match_words_containing_the_stem(name: str) -> None:
    assert sensitive(f"/tmp/{name}") is None


# ---- learning/lessons.py: validation, no silent no-ops ---------------------------------------


@pytest.fixture
def learning(store: Store) -> Iterator[Learning]:
    lr = Learning.for_store(store)
    yield lr
    lr.close()


def test_add_lesson_validates_text_like_update_lesson(learning: Learning) -> None:
    learning.create("p")
    db = learning.db_for("p")
    with pytest.raises(LearningError, match="cannot be empty"):
        db.add_lesson("p", "   ", ["coder"], [])
    with pytest.raises(LearningError, match="too long"):
        db.add_lesson("p", "x" * (MAX_LESSON_CHARS + 1), ["coder"], [])
    lesson = db.add_lesson("p", "  padded lesson  ", ["coder"], [])
    assert lesson.text == "padded lesson"


def test_update_text_and_retire_raise_on_a_missing_lesson(learning: Learning) -> None:
    learning.create("p")
    db = learning.db_for("p")
    with pytest.raises(LearningError, match="no lesson L9"):
        db.update_text("p", 9, "new text")
    with pytest.raises(LearningError, match="no lesson L9"):
        db.retire("p", 9)
    with pytest.raises(LearningError, match="cannot be empty"):
        db.update_text("p", 9, "  ")


# ---- learning/profiles.py, learning/registry.py: the dead Laya API is gone -------------------


def test_laya_weights_api_is_removed(learning: Learning) -> None:
    learning.create("p")
    db = learning.db_for("p")
    assert not hasattr(db, "get_laya_weights")
    assert not hasattr(db, "set_laya_weights")
    assert not hasattr(learning, "laya_weights")
    assert not hasattr(learning, "set_laya_weights")


def test_set_enabled_raises_for_an_unknown_profile(learning: Learning) -> None:
    with pytest.raises(LearningError, match="no learning profile named 'ghost'"):
        learning.repo.set_enabled("ghost", True)


# ---- learning/skills.py: typed proposals ------------------------------------------------------


def test_propose_skills_returns_typed_proposals(learning: Learning) -> None:
    learning.create("default")
    db = learning.db_for("default")
    db.add_lesson("default", "use dark backgrounds", ["coder"], ["ui"])
    db.add_lesson("default", "dark mode for dashboards", ["coder"], ["ui"])
    proposals = db.propose_skills("default")
    assert len(proposals) == 1
    (proposal,) = proposals
    assert isinstance(proposal, SkillProposal)
    assert proposal.tag == "ui"
    assert proposal.name == "Ui"
    assert len(proposal.lesson_ids) == 2


# ---- dashboard/core.py: a proper config-error page --------------------------------------------


@contextmanager
def _dashboard_client(repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def test_config_error_gets_its_own_page_not_a_500(fixture_repo: Path) -> None:
    (fixture_repo / "jev.toml").write_text("[broken\n")
    with _dashboard_client(fixture_repo) as client:
        response = client.get("/")
    assert response.status_code == 400
    assert "Config error" in response.text
    assert "/diagnostics" in response.text
    assert "/settings" in response.text
    assert "jev.toml" in response.text


# ---- dashboard/routes_settings.py: keys/kinds mismatch fails loudly ---------------------------


def _csrf(client: TestClient, page: str = "/") -> str:
    response = client.get(page)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', response.text)
    if match:
        return match.group(1)
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/queue").text)
    assert match
    return match.group(1)


def test_settings_entity_rejects_mismatched_keys_and_kinds(fixture_repo: Path) -> None:
    with _dashboard_client(fixture_repo) as client:
        token = _csrf(client, "/settings")
        response = client.post(
            "/settings/entity",
            data={
                "csrf_token": token,
                "section": "servers",
                "action": "add",
                "target": "repo",
                "name": "srv1",
                "keys": "host,port",
                "kinds": "str",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "keys and kinds do not match (2 keys, 1 kinds)" in page.text


def _trust(repo: Path) -> None:
    """Trust the fixture repo in the isolated test user config (see test_ship.py).

    `[servers.*]` is a guarded key: an untrusted repo's jev.toml may not set it.
    """
    user_config = Path(os.environ["XDG_CONFIG_HOME"]) / "jev" / "config.toml"
    if "[trust]" not in user_config.read_text():
        with user_config.open("a") as fh:
            fh.write(f'\n[trust]\nrepos = ["{repo}"]\n')


def test_settings_entity_accepts_matching_keys_and_kinds(fixture_repo: Path) -> None:
    _trust(fixture_repo)
    with _dashboard_client(fixture_repo) as client:
        token = _csrf(client, "/settings")
        response = client.post(
            "/settings/entity",
            data={
                "csrf_token": token,
                "section": "servers",
                "action": "add",
                "target": "repo",
                "name": "srv1",
                "keys": "host",
                "kinds": "str",
                "f_host": "example.com",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "saved servers.srv1" in page.text
    assert load_config(fixture_repo).servers["srv1"].host == "example.com"
