"""Assurance labels (phase 2, design sections 10.1-10.3): the single label
text on the run page, the board row, and the PR body — on both ship paths —
and dashboard export/import routes (repo scope only)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from jev_agent.core.ship.pr_body import render_pr_body
from jev_agent.core.store import Store
from jev_agent.core.workflow_profiles import ProfileStore
from jev_agent.core.workflow_profiles.assurance import assurance_label_text, compute_assurance
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings

PROFILE = {
    "id": "repo:fast",
    "display_name": "Fast",
    "scope": "repo",
    "stage_list": ["INTAKE", "PLANNING", "VALIDATING", "DESIGNING", "EXECUTING", "INTEGRATING"],
    "gate_policies": {
        "PLANNING": "inherit",
        "VALIDATING": "inherit",
        "DESIGNING": "inherit",
        "EXECUTING": "inherit",
        "INTEGRATING": "inherit",
    },
    "designing": "when_needed",
    "ship_enabled": False,
}


def _label(quality, score_skip=False, mode="standard", plan_gate=True):
    assurance, absent = compute_assurance(
        quality_level=quality,
        score_skip_human=score_skip,
        autonomy_mode=mode,
        plan_gate_present=plan_gate,
    )
    return assurance, absent, assurance_label_text(assurance, absent)


def test_full_label_text() -> None:
    assurance, absent, text = _label("medium")
    assert assurance == "full" and absent == []
    assert text == "Full checks"


def test_reduced_label_names_absent_behaviours() -> None:
    assurance, absent, text = _label("none", score_skip=True, mode="autonomous", plan_gate=False)
    assert assurance == "reduced"
    assert absent == [
        "quality gate off",
        "ship score gate skipped",
        "plan approval gate absent (autonomous mode)",
    ]
    assert text.startswith("Reduced checks — ")
    assert "quality gate off" in text and "ship score gate skipped" in text


def test_ship_score_gate_not_claimed_at_creation() -> None:
    """A missing ship score is only knowable at ship time, so creation
    never lists it as absent."""
    _, absent, _ = _label("medium", score_skip=False, plan_gate=True)
    assert "ship score gate skipped" not in absent


def _pr_body(fixture_repo: Path, assurance: str, absent: list[str]) -> str:
    from jev_agent.core.kpi import RunKpis
    from jev_agent.core.models import Idea

    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = store.create_run(
            Idea(text="add search", repo_path=fixture_repo),
            None,
            "none",
            assurance=assurance,
            assurance_absent=absent,
        )
        return render_pr_body(
            store,
            run.id,
            RunKpis(
                run_id=run.id,
                total_cost_usd=0.0,
                wall_clock_s=None,
                tasks=0,
                tasks_done=0,
                attempts_total=0,
                attempts_per_task=None,
                first_pass_rate=None,
                retries=0,
                escalations=0,
                reassigns=0,
                fix_rounds=0,
                quality_level="none",
            ),
        )


def test_pr_body_carries_identical_label_text(fixture_repo: Path) -> None:
    body = _pr_body(fixture_repo, "reduced", ["quality gate off"])
    assert "## Assurance" in body
    assert assurance_label_text("reduced", ["quality gate off"]) in body


def test_pr_body_full_label_also_present(fixture_repo: Path) -> None:
    body = _pr_body(fixture_repo, "full", [])
    assert "Full checks" in body


# ---- dashboard export/import routes ----------------------------------------------


@pytest.fixture
def client(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("HOME", str(fixture_repo / "home"))
    store = ProfileStore(fixture_repo, fixture_repo / "home" / ".jev")
    store.save(PROFILE)
    app = create_app(DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as test_client:
        yield test_client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/queue").text)
    assert match
    return match.group(1)


def test_export_downloads_profile_json(client: TestClient) -> None:
    response = client.get("/workflows/repo:fast/export")
    assert response.status_code == 200
    data = json.loads(response.text)
    assert data["id"] == "repo:fast" and data["version"] == 1


def test_export_user_scope_refused(client: TestClient) -> None:
    response = client.get("/workflows/user:fast/export", follow_redirects=False)
    assert response.status_code in (302, 303)


def test_import_round_trip(client: TestClient, fixture_repo: Path) -> None:
    exported = client.get("/workflows/repo:fast/export").text
    # Import into a second repo: the exported JSON imports cleanly.
    response = client.post(
        "/workflows/import",
        data={"csrf_token": _csrf(client), "profile_json": exported},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    store = ProfileStore(fixture_repo, fixture_repo / "home" / ".jev")
    assert store.get("repo:fast")["version"] == 2  # a new version, not a clobber


def test_import_invalid_json_refused_not_partially_applied(
    client: TestClient, fixture_repo: Path
) -> None:
    response = client.post(
        "/workflows/import",
        data={"csrf_token": _csrf(client), "profile_json": "{not json"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    store = ProfileStore(fixture_repo, fixture_repo / "home" / ".jev")
    assert store.get("repo:fast")["version"] == 1


def test_import_page_lists_repo_profiles(client: TestClient) -> None:
    response = client.get("/workflows/import")
    assert response.status_code == 200
    assert "repo:fast" in response.text
    assert "/workflows/repo:fast/export" in response.text
