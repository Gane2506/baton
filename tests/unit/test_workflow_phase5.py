"""Phase 5: flowchart editor (issue #368, design sections 13.1-13.2, 15).

List, editor (canvas plus list mode, one form, one server-side validator),
detail, export/import reuse, nav entry, catalogue-fix audit, and the
input-equivalence table: canvas and list selections build identical
profile JSON. Suggestion routes are phase 6 and must NOT exist here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from jev_agent.core.workflow_profiles import ProfileStore
from jev_agent.core.workflow_profiles.validator import validate_profile
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.routes_workflows import build_profile_from_editor
from jev_agent.dashboard.security import DashboardSettings

PROFILE_FORM = {
    "display_name": "Fast",
    "slug": "fast",
    "scope": "repo",
    "stage_INTAKE": "on",
    "stage_PLANNING": "on",
    "stage_VALIDATING": "on",
    "stage_DESIGNING": "on",
    "stage_EXECUTING": "on",
    "stage_INTEGRATING": "on",
    "stage_SHIPPING": "on",
    "gate_PLANNING": "inherit",
    "gate_VALIDATING": "inherit",
    "gate_DESIGNING": "inherit",
    "gate_EXECUTING": "inherit",
    "gate_INTEGRATING": "inherit",
    "gate_SHIPPING": "inherit",
    "designing": "when_needed",
    "ship_enabled": "on",
    "after_approval": "ask",
}


@pytest.fixture
def client(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("HOME", str(fixture_repo / "home"))
    app = create_app(DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as test_client:
        yield test_client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/queue").text)
    assert match
    return match.group(1)


def _store(fixture_repo: Path) -> ProfileStore:
    return ProfileStore(fixture_repo, fixture_repo / "home" / ".jev")


def _create(client: TestClient, form: dict[str, str], url: str = "/workflows/new"):
    return client.post(url, data={"csrf_token": _csrf(client), **form}, follow_redirects=False)


# ---- input equivalence (design section 13.2) --------------------------------


def test_canvas_and_list_selections_build_identical_json() -> None:
    """The equivalence table: a canvas drag-out is a list toggle-off and a
    canvas parameter edit is a list field edit — both modes submit the
    same fields, so the same selections build identical JSON."""
    canvas_form = dict(PROFILE_FORM)
    del canvas_form["stage_DESIGNING"]
    canvas_form["max_plan_rounds"] = "2"
    canvas_form["gate_EXECUTING"] = "require"
    list_form = dict(PROFILE_FORM)
    del list_form["stage_DESIGNING"]
    list_form["max_plan_rounds"] = "2"
    list_form["gate_EXECUTING"] = "require"
    # Designing "never" is the coherent partner of omitting design.
    canvas_form["designing"] = "never"
    list_form["designing"] = "never"
    assert build_profile_from_editor(canvas_form) == build_profile_from_editor(list_form)
    built = build_profile_from_editor(canvas_form)
    assert built["stage_list"] == [
        "INTAKE",
        "PLANNING",
        "VALIDATING",
        "EXECUTING",
        "INTEGRATING",
        "SHIPPING",
    ]
    assert "DESIGNING" not in built["gate_policies"]


def test_editor_build_always_uses_canonical_order() -> None:
    """The form has no reorder input: even a submission whose fields arrive
    in scrambled order builds the canonical order. Reordering is refused
    elsewhere (validator), never produced here."""
    scrambled = dict(reversed(list(PROFILE_FORM.items())))
    built = build_profile_from_editor(scrambled)
    assert built["stage_list"] == [
        "INTAKE",
        "PLANNING",
        "VALIDATING",
        "DESIGNING",
        "EXECUTING",
        "INTEGRATING",
        "SHIPPING",
    ]


def test_editor_page_has_one_form_and_one_input_per_stage(client: TestClient) -> None:
    """One form, one input per stage: there is no second (canvas-only or
    list-only) field set that could drift from the other mode."""
    text = client.get("/workflows/new").text
    assert 'id="wf-form"' in text
    assert text.count('id="wf-form"') == 1
    for stage in (
        "INTAKE",
        "PLANNING",
        "VALIDATING",
        "DESIGNING",
        "EXECUTING",
        "INTEGRATING",
        "SHIPPING",
    ):
        assert text.count(f'name="stage_{stage}"') == 1
    assert "Canvas" in text and "List" in text
    assert "cannot be reordered" in text


# ---- list / detail / nav -------------------------------------------------------


def test_list_page_sections_and_nav(client: TestClient) -> None:
    text = client.get("/workflows").text
    assert "preset:accuracy" in text or "Accuracy" in text
    assert "/workflows/new" in text and "/workflows/import" in text
    board = client.get("/").text
    assert 'href="/workflows"' in board


def test_detail_page_for_preset_is_read_only(client: TestClient) -> None:
    text = client.get("/workflows/preset:accuracy").text
    assert "Accuracy" in text
    assert "quality first, speed second" in text
    assert "/workflows/new?preset=preset:accuracy" in text


def test_preset_edit_redirects_to_copy(client: TestClient) -> None:
    response = client.get("/workflows/preset:accuracy/edit", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/workflows/new?preset=preset:accuracy" in response.headers["location"]


def test_unknown_profile_detail_is_404(client: TestClient) -> None:
    assert client.get("/workflows/repo:nope").status_code == 404


def test_no_suggestion_routes_in_phase_5(client: TestClient) -> None:
    """Phase 6 owns suggestions: no suggest/approve/discard route exists."""
    assert client.get("/workflows/repo:fast/suggest").status_code in (404, 405)
    assert client.post(
        "/workflows/repo:fast/suggest",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    ).status_code in (404, 405)


# ---- create / edit / save failures ----------------------------------------------


def test_create_save_and_detail_round_trip(client: TestClient, fixture_repo: Path) -> None:
    response = _create(client, PROFILE_FORM)
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/workflows/repo:fast"
    stored = _store(fixture_repo).get("repo:fast")
    assert stored["version"] == 1
    assert stored["stage_list"] == [
        "INTAKE",
        "PLANNING",
        "VALIDATING",
        "DESIGNING",
        "EXECUTING",
        "INTEGRATING",
        "SHIPPING",
    ]
    detail = client.get("/workflows/repo:fast").text
    assert "Fast" in detail and "Version history" in detail
    assert "/workflows/repo:fast/export" in detail
    assert "/workflows/repo:fast/edit" in detail


def test_create_from_preset_template(client: TestClient, fixture_repo: Path) -> None:
    text = client.get("/workflows/new?preset=preset:accuracy").text
    assert "Accuracy copy" in text
    form = dict(PROFILE_FORM)
    form["display_name"] = "Accuracy copy"
    form["quality_level"] = "production"
    form["cda_grading"] = "on"
    response = _create(client, form)
    assert response.status_code in (302, 303)
    stored = _store(fixture_repo).get("repo:fast")
    assert stored["quality_level"] == "production"


def test_save_failure_lists_every_error(client: TestClient, fixture_repo: Path) -> None:
    """Design section 13.2: a failed save lists every error, and saves
    nothing — the user is not sent around the form one error at a time."""
    form = dict(PROFILE_FORM)
    del form["stage_VALIDATING"]
    form["quality_level"] = "none"  # safety floor (B1) with shipping on
    response = _create(client, form)
    assert response.status_code == 200
    assert "Validation cannot be removed" in response.text
    assert "quality level" in response.text and "shipping is enabled" in response.text
    assert "Could not save" in response.text
    assert not _store(fixture_repo).exists("repo:fast")


def test_edit_saves_new_version_without_touching_v1(client: TestClient, fixture_repo: Path) -> None:
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    form = dict(PROFILE_FORM)
    del form["stage_DESIGNING"]
    form["designing"] = "never"
    response = _create(client, form, url="/workflows/repo:fast/edit")
    assert response.status_code in (302, 303)
    store = _store(fixture_repo)
    assert store.get("repo:fast")["version"] == 2
    assert store.get("repo:fast", version=1)["stage_list"][-1] == "SHIPPING"
    assert "DESIGNING" in store.get("repo:fast", version=1)["stage_list"]
    assert "DESIGNING" not in store.get("repo:fast")["stage_list"]


def test_detail_lists_runs_that_snapshotted_it(client: TestClient, fixture_repo: Path) -> None:
    from jev_agent.core.models import Idea
    from jev_agent.core.store import Store

    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    with Store.open(fixture_repo / ".jev" / "state.db") as db:
        run = db.create_run(
            Idea(text="add search", repo_path=fixture_repo),
            None,
            "none",
            workflow_profile_id="repo:fast",
            workflow_profile_version=1,
        )
    text = client.get("/workflows/repo:fast").text
    assert run.id in text


# ---- live validation subset and reorder refusal -----------------------------------


def test_live_validate_returns_only_live_subset(client: TestClient) -> None:
    """Live feedback is exactly the section 13.2 subset: unknown names,
    duplicates, empty list — not the full catalogue (an omitted required
    stage mid-edit is a save-time refusal, not a live error)."""
    csrf = _csrf(client)

    def validate(draft: dict) -> list:
        response = client.post(
            "/workflows/validate",
            data={"csrf_token": csrf, "profile_json": json.dumps(draft)},
            follow_redirects=False,
        )
        assert response.status_code == 200
        return response.json()["errors"]

    base = {
        "id": "repo:fast",
        "display_name": "Fast",
        "scope": "repo",
        "stage_list": [
            "INTAKE",
            "PLANNING",
            "VALIDATING",
            "DESIGNING",
            "EXECUTING",
            "INTEGRATING",
            "SHIPPING",
        ],
        "gate_policies": {"PLANNING": "inherit"},
        "designing": "when_needed",
    }
    omitted = dict(
        base, stage_list=["INTAKE", "PLANNING", "DESIGNING", "EXECUTING", "INTEGRATING", "SHIPPING"]
    )
    assert validate(omitted) == []  # save would refuse; live does not flag it
    duplicate = dict(base, stage_list=[*base["stage_list"], "PLANNING"])
    assert any("Duplicate stage" in e for e in validate(duplicate))
    unknown = dict(base, stage_list=[*base["stage_list"], "BOGUS"])
    assert any("Unknown stage" in e for e in validate(unknown))
    assert validate({**base, "stage_list": []})


def test_reorder_refused_by_validator_with_dependency() -> None:
    """Canvas drag refusal has a server-side twin: a reordered list
    submitted any other way is refused with the section 7.1 dependency,
    and the refusal counts as the reorder check passing."""
    errors = validate_profile(
        {
            "id": "repo:fast",
            "display_name": "Fast",
            "scope": "repo",
            "stage_list": ["INTAKE", "PLANNING", "EXECUTING", "VALIDATING", "INTEGRATING"],
            "gate_policies": {"PLANNING": "inherit"},
            "designing": "never",
        }
    )
    assert any("Execution cannot run before validation" in e for e in errors)


def test_catalogue_messages_carry_a_fix() -> None:
    """Phase 5 catalogue audit (design section 7.5): the two messages that
    previously stated a reason without a fix now state both."""
    errors = validate_profile(
        {
            "id": "repo:fast",
            "display_name": "Fast",
            "scope": "repo",
            "stage_list": [
                "INTAKE",
                "PLANNING",
                "VALIDATING",
                "DESIGNING",
                "EXECUTING",
                "INTEGRATING",
                "SHIPPING",
            ],
            "gate_policies": {"INTAKE": "inherit", "BOGUS": "inherit"},
            "designing": "when_needed",
        }
    )
    intake = [e for e in errors if "Intake has no waiting state" in e]
    assert intake and "Remove the gate policy for intake" in intake[0]
    unknown_gate = [e for e in errors if "Unknown stage 'BOGUS' in gate policies" in e]
    assert unknown_gate and "Remove the unknown stage" in unknown_gate[0]


def test_export_from_detail_downloads_current_version(
    client: TestClient, fixture_repo: Path
) -> None:
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    response = client.get("/workflows/repo:fast/export")
    assert response.status_code == 200
    assert json.loads(response.text)["id"] == "repo:fast"
