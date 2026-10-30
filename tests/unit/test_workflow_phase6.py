"""Phase 6: usage-based suggestions (issue #368, design sections 12, 13.3, 15).

Threshold tests, approval tests (an unapproved suggestion changes no run
and appears in no picker; a discard changes only the record's status),
evidence tests (a reason line without evidence is refused, never
displayed), cross-repo tests, dedup tests, and staleness tests — plus the
dashboard request/result/approve/discard routes and the suggestion
history on the detail page. Safety floor (B1) and legacy path (G1) are
unchanged throughout: a tampered draft that drops a floor behaviour is
refused at approval, and no suggestion path touches the default flow.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from jev_agent.core.models import Idea
from jev_agent.core.store import Store
from jev_agent.core.suggestions import (
    Evidence,
    EvidenceRefusal,
    ReasoningLine,
    RunEvidence,
    StageRowEvidence,
    SuggestionStore,
    dedup_key,
    diff_changes,
    evidence_fingerprint,
    measure_volume,
    recheck_for_approval,
    request_suggestion,
    validate_reasoning_line,
)
from jev_agent.core.workflow_profiles import ProfileStore
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

FULL_STAGES = [
    "INTAKE",
    "PLANNING",
    "VALIDATING",
    "DESIGNING",
    "EXECUTING",
    "INTEGRATING",
    "SHIPPING",
]
FULL_GATES = {stage: "inherit" for stage in FULL_STAGES if stage != "INTAKE"}

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


def _profile(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "repo:fast",
        "display_name": "Fast",
        "scope": "repo",
        "stage_list": list(FULL_STAGES),
        "gate_policies": dict(FULL_GATES),
        "designing": "when_needed",
        "ship_enabled": True,
        "after_approval": "ask",
        "version": 1,
    }
    base.update(overrides)
    return base


def _row(stage: str, seconds: float | None = 120.0, outcome: str = "success") -> StageRowEvidence:
    return StageRowEvidence(
        stage=stage,
        outcome=outcome,
        work_seconds=seconds,
        completeness="full" if seconds is not None else "partial",
    )


def _run(
    run_id: str,
    goal: str | None,
    *,
    state: str = "DONE",
    days_ago: float = 1,
    stages: tuple[str, ...] = ("DESIGNING", "SHIPPING"),
    repo: str = "repo-a",
    seconds: float | None = 120.0,
) -> RunEvidence:
    return RunEvidence(
        run_id=run_id,
        repo_label=repo,
        state=state,
        goal=goal,
        ended_at=NOW - timedelta(days=days_ago),
        stage_rows=tuple(_row(stage, seconds) for stage in stages),
    )


def _evidence(runs: list[RunEvidence], **kwargs: Any) -> Evidence:
    return Evidence(runs=tuple(runs), scope="repo", **kwargs)


def _completed(goal: str, count: int, **kwargs: Any) -> list[RunEvidence]:
    return [_run(f"run-{i}", goal, **kwargs) for i in range(count)]


# ---- thresholds ---------------------------------------------------------------


def test_below_threshold_returns_preset_only_labelled() -> None:
    """Below 10 completed goal-labelled runs: preset-only mode, labelled
    as such, with the preset's documented behaviour as the reason."""
    outcome = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    assert outcome.mode == "preset-only"
    assert outcome.draft is not None
    assert outcome.draft["max_plan_rounds"] == 2
    assert outcome.draft["max_fix_rounds"] == 1
    assert outcome.thresholds_met["min_completed_runs_10"] is False
    assert "3 completed" in outcome.message and "at least 10" in outcome.message
    line = outcome.reasoning_lines[0]
    assert line.kind == "preset"
    assert line.window == "preset, not measured for your history"
    assert "plan rounds at most 2" in line.evidence


def test_below_threshold_custom_goal_proposes_no_preset() -> None:
    """A custom goal has no preset: below threshold the request says so
    plainly and proposes nothing, never a mismatched preset."""
    outcome = request_suggestion(_profile(), "custom", _evidence(_completed("custom", 2)), now=NOW)
    assert outcome.mode == "preset-only"
    assert outcome.draft is None
    assert "No preset matches a custom goal" in outcome.message


def test_volume_counts_only_goal_labelled_completed_in_window() -> None:
    """Null-goal runs, wrong-goal runs, failed runs, and runs older than
    90 days never count toward the 10-run threshold (section 12.1)."""
    runs = [
        *_completed("efficiency", 4),
        _run("null-goal", None),
        _run("acc-goal", "accuracy"),
        _run("failed", "efficiency", state="FAILED"),
        _run("old", "efficiency", days_ago=100),
        _run("no-rows", "efficiency", stages=()),
    ]
    assert measure_volume(_evidence(runs), "efficiency", NOW) == 4
    outcome = request_suggestion(_profile(), "efficiency", _evidence(runs), now=NOW)
    assert outcome.mode == "preset-only"


def test_accuracy_evidence_never_comes_from_efficiency_runs() -> None:
    """A suggestion never mixes goals (section 11.3): 12 efficiency runs
    do not make an accuracy request usage-based."""
    outcome = request_suggestion(
        _profile(), "accuracy", _evidence(_completed("efficiency", 12)), now=NOW
    )
    assert outcome.mode == "preset-only"
    assert outcome.draft is not None
    assert outcome.draft["quality_level"] == "production"


# ---- usage-based drops ----------------------------------------------------------


def test_usage_based_designing_drop_carries_median_and_no_defect_note() -> None:
    outcome = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 12)), now=NOW
    )
    assert outcome.mode == "usage-based"
    assert outcome.draft is not None
    assert "DESIGNING" not in outcome.draft["stage_list"]
    assert outcome.draft["designing"] == "never"
    assert "DESIGNING" not in outcome.draft["gate_policies"]
    line = next(line for line in outcome.reasoning_lines if "DESIGNING" in line.change)
    assert "median 120.0 seconds over 12 full runs" in line.evidence
    assert "no defect signal" in line.evidence
    assert line.sample_size == 12 and line.completeness == "full"
    assert line.falsifier


def test_usage_based_shipping_drop_turns_shipping_off() -> None:
    profile = _profile(
        stage_list=["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING", "SHIPPING"],
        gate_policies={
            stage: "inherit"
            for stage in ("PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING", "SHIPPING")
        },
        designing="never",
    )
    outcome = request_suggestion(
        profile,
        "efficiency",
        _evidence(_completed("efficiency", 12, stages=("SHIPPING",))),
        now=NOW,
    )
    assert outcome.mode == "usage-based"
    assert outcome.draft is not None
    assert "SHIPPING" not in outcome.draft["stage_list"]
    assert outcome.draft["ship_enabled"] is False
    assert any("SHIPPING" in line.change for line in outcome.reasoning_lines)


def test_no_change_when_no_drop_has_a_measured_saving() -> None:
    """Skipped DESIGNING rows are not runs of the stage, and partial-only
    SHIPPING rows have no full median: no change beats the current flow,
    and the engine says so with the numbers instead of inventing one."""
    runs = [
        RunEvidence(
            run_id=f"run-{i}",
            repo_label="repo-a",
            state="DONE",
            goal="efficiency",
            ended_at=NOW - timedelta(days=1),
            stage_rows=(
                _row("DESIGNING", outcome="skipped"),
                StageRowEvidence(
                    stage="SHIPPING", outcome="success", work_seconds=None, completeness="partial"
                ),
            ),
        )
        for i in range(12)
    ]
    outcome = request_suggestion(_profile(), "efficiency", _evidence(runs), now=NOW)
    assert outcome.mode == "no-change"
    assert outcome.draft is None
    assert "No change beats your current flow" in outcome.message
    assert "12 completed" in outcome.message


def test_no_round_shortening_and_no_cheaper_model_ever_proposed() -> None:
    """Round caps have no measured saving in stage rows, and no price map
    is configured: the engine proposes neither, even with defect signals
    present that would forbid shortening on cost grounds alone."""
    runs = [
        RunEvidence(
            run_id=f"run-{i}",
            repo_label="repo-a",
            state="DONE",
            goal="efficiency",
            ended_at=NOW - timedelta(days=1),
            stage_rows=(
                StageRowEvidence(
                    stage="VALIDATING",
                    outcome="success",
                    work_seconds=60.0,
                    completeness="full",
                    defects_caught=2,
                ),
            ),
        )
        for i in range(12)
    ]
    outcome = request_suggestion(_profile(), "efficiency", _evidence(runs), now=NOW)
    assert outcome.mode == "no-change"
    assert outcome.draft is None


# ---- evidence honesty ------------------------------------------------------------


def test_reason_line_without_evidence_is_refused() -> None:
    """A reason line without evidence is refused by the engine, never
    displayed (section 12.2, phase-6 evidence tests)."""
    for line in (
        ReasoningLine(
            change="Drop DESIGNING",
            evidence="",
            sample_size=12,
            window="last 90 days",
            completeness="full",
            falsifier="x",
        ),
        ReasoningLine(
            change="Drop DESIGNING",
            evidence="median 120 seconds",
            sample_size=0,
            window="last 90 days",
            completeness="full",
            falsifier="x",
        ),
        ReasoningLine(
            change="Drop DESIGNING",
            evidence="median 120 seconds",
            sample_size=12,
            window="",
            completeness="full",
            falsifier="x",
        ),
        ReasoningLine(
            change="Drop DESIGNING",
            evidence="median 120 seconds",
            sample_size=12,
            window="last 90 days",
            completeness="",
            falsifier="x",
        ),
    ):
        with pytest.raises(EvidenceRefusal):
            validate_reasoning_line(line)


def test_engine_never_emits_an_evidence_less_line() -> None:
    outcome = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 12)), now=NOW
    )
    assert outcome.reasoning_lines
    for line in outcome.reasoning_lines:
        validate_reasoning_line(line)


# ---- cross-repo -------------------------------------------------------------------


def test_cross_repo_aggregates_repos_and_labels() -> None:
    """Aggregation across repos with different configurations (quality
    levels here), labelled cross-repo (section 12.1)."""
    runs = _completed("efficiency", 6, repo="repo-a") + [
        RunEvidence(
            run_id=f"b-{i}",
            repo_label="repo-b",
            state="DONE",
            goal="efficiency",
            ended_at=NOW - timedelta(days=1),
            stage_rows=(_row("DESIGNING"),),
            quality_level="production",
        )
        for i in range(6)
    ]
    evidence = Evidence(runs=tuple(runs), scope="user", cross_repo=True)
    outcome = request_suggestion(_profile(scope="user"), "efficiency", evidence, now=NOW)
    assert outcome.mode == "usage-based"
    assert outcome.cross_repo is True
    assert set(outcome.repos) == {"repo-a", "repo-b"}


def test_repo_scope_cross_repo_is_refused() -> None:
    """Another repo's runs are never read for a repo-scope suggestion."""
    evidence = Evidence(runs=tuple(_completed("efficiency", 12)), scope="repo", cross_repo=True)
    with pytest.raises(ValueError, match="never reads another repo's runs"):
        request_suggestion(_profile(), "efficiency", evidence, now=NOW)


# ---- dedup and staleness --------------------------------------------------------------


def _record_for(outcome: Any, profile_dir: Path) -> dict[str, Any]:
    return SuggestionStore(profile_dir).create(outcome)


def test_dedup_key_covers_version_plus_change_set(tmp_path: Path) -> None:
    outcome = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    key = dedup_key("repo:fast", 1, outcome.changes)
    assert key == dedup_key("repo:fast", 1, outcome.changes)
    assert key != dedup_key("repo:fast", 2, outcome.changes)


def test_discarded_with_unchanged_evidence_is_not_reoffered(tmp_path: Path) -> None:
    """A discarded suggestion with unchanged evidence returns the
    discarded record, not a fresh suggestion (section 12.4)."""
    store = SuggestionStore(tmp_path)
    outcome = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    record = store.create(outcome)
    store.set_status(record["id"], "discarded")
    found = store.find_discarded_match(record["dedup_key"], record["evidence_fingerprint"])
    assert found is not None and found["id"] == record["id"]
    assert found["status"] == "discarded"


def test_discarded_with_new_evidence_may_be_reoffered(tmp_path: Path) -> None:
    """New evidence (a new lesson, or new runs) changes the fingerprint,
    so the re-offer rule no longer blocks a fresh suggestion."""
    store = SuggestionStore(tmp_path)
    outcome = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    record = store.create(outcome)
    store.set_status(record["id"], "discarded")
    moved = _evidence(_completed("efficiency", 3), lesson_count=1)
    assert evidence_fingerprint(moved, "efficiency", NOW) != record["evidence_fingerprint"]
    assert (
        store.find_discarded_match(
            record["dedup_key"], evidence_fingerprint(moved, "efficiency", NOW)
        )
        is None
    )


def test_discard_changes_only_the_record_status(tmp_path: Path) -> None:
    """Discard changes ONLY the record's status — the stored draft and
    every other field are byte-identical apart from status."""
    store = SuggestionStore(tmp_path)
    outcome = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    record = store.create(outcome)
    discarded = store.set_status(record["id"], "discarded")
    assert discarded["status"] == "discarded"
    for key, value in record.items():
        if key != "status":
            assert discarded[key] == value


def test_staleness_recheck_refuses_changed_evidence(tmp_path: Path) -> None:
    """A preset-only draft approved after new runs cross the threshold is
    stale: the current window no longer supports it as preset-only."""
    store = SuggestionStore(tmp_path)
    old = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    record = store.create(old)
    fresh = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 12)), now=NOW
    )
    ok, message = recheck_for_approval(record, fresh, _profile())
    assert ok is False and "evidence changed" in message


def test_staleness_recheck_passes_when_evidence_unchanged(tmp_path: Path) -> None:
    store = SuggestionStore(tmp_path)
    old = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    record = store.create(old)
    fresh = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    ok, _message = recheck_for_approval(record, fresh, _profile())
    assert ok is True


def test_staleness_recheck_passes_for_identical_draft(tmp_path: Path) -> None:
    """The identical-draft case is not staleness: approval records
    approval of the existing version (the caller creates no new one)."""
    store = SuggestionStore(tmp_path)
    outcome = request_suggestion(
        _profile(), "efficiency", _evidence(_completed("efficiency", 3)), now=NOW
    )
    record = store.create(outcome)
    assert outcome.draft is not None
    current = _profile()
    for key, value in outcome.draft.items():
        current[key] = value
    ok, _message = recheck_for_approval(record, outcome, current)
    assert ok is True
    assert diff_changes(current, outcome.draft) == []


# ---- dashboard -------------------------------------------------------------------


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


def _create(client: TestClient, form: dict[str, str], url: str = "/workflows/new") -> Any:
    return client.post(url, data={"csrf_token": _csrf(client), **form}, follow_redirects=False)


def _suggest(client: TestClient, profile_id: str, goal: str = "efficiency") -> Any:
    response = client.post(
        f"/workflows/{profile_id}/suggest",
        data={"csrf_token": _csrf(client), "goal": goal},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text[:500]
    return response


def _suggestion_records(fixture_repo: Path) -> list[dict[str, Any]]:
    directory = fixture_repo / ".jev" / "workflow-profiles" / "fast" / "suggestions"
    if not directory.exists():
        return []
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def test_suggest_routes_exist_in_phase_6(client: TestClient) -> None:
    """Phase 6 supersedes the phase-5 no-suggestion-routes check: the
    request page exists and serves the goal form."""
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    text = client.get("/workflows/repo:fast/suggest").text
    assert "Suggest a flow" in text
    assert 'name="goal"' in text
    assert "Labelled-run volume" in text


def test_suggest_page_hides_cross_repo_for_repo_scope(client: TestClient) -> None:
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    text = client.get("/workflows/repo:fast/suggest").text
    assert "cross_repo" not in text
    assert "this repo's runs only" in text


def test_preset_suggest_redirects_to_copy(client: TestClient) -> None:
    response = client.get("/workflows/preset:efficiency/suggest", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/workflows/new?preset=preset:efficiency" in response.headers["location"]


def test_unknown_profile_suggest_is_404(client: TestClient) -> None:
    assert client.get("/workflows/repo:nope/suggest").status_code == 404


def test_below_threshold_result_says_so_plainly(client: TestClient, fixture_repo: Path) -> None:
    """Acceptance: a below-threshold request says so plainly instead of
    inventing a recommendation — with the counted volume on the page."""
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    location = _suggest(client, "repo:fast").headers["location"]
    text = client.get(location).text
    assert "preset, not measured for your history" in text
    assert "0 completed efficiency-goal runs" in text
    assert "needs at least 10" in text
    assert "Approve suggestion" in text and "Discard suggestion" in text
    records = _suggestion_records(fixture_repo)
    assert len(records) == 1 and records[0]["status"] == "pending"
    assert records[0]["mode"] == "preset-only"
    # Unapproved: no new version, no picker entry, no run change.
    assert _store(fixture_repo).versions("repo:fast") == [1]
    assert [p["id"] for p in _store(fixture_repo).list_profiles()] == ["repo:fast"]
    with Store.open(fixture_repo / ".jev" / "state.db") as db:
        assert db.list_runs() == []


def test_approve_saves_one_suggested_version(client: TestClient, fixture_repo: Path) -> None:
    """Acceptance: a suggestion with reasoning shown can be approved and
    then picked — exactly one new version, with origin suggested."""
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    location = _suggest(client, "repo:fast").headers["location"]
    record_id = location.rsplit("/", 1)[-1]
    response = client.post(
        f"/workflows/repo:fast/suggestions/{record_id}/approve",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/workflows/repo:fast"
    store = _store(fixture_repo)
    assert store.versions("repo:fast") == [1, 2]
    assert store.get("repo:fast")["origin"] == "suggested"
    assert store.get("repo:fast")["max_plan_rounds"] == 2
    records = _suggestion_records(fixture_repo)
    assert records[0]["status"] == "approved" and records[0]["approved_version"] == 2
    detail = client.get("/workflows/repo:fast").text
    assert "Suggestion history" in detail and "approved" in detail


def test_discard_changes_only_status_and_blocks_reoffer(
    client: TestClient, fixture_repo: Path
) -> None:
    """Discard changes ONLY the record's status — no new version, no
    picker entry, no run change — and a re-request with unchanged
    evidence returns the discarded record, not a fresh suggestion."""
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    location = _suggest(client, "repo:fast").headers["location"]
    record_id = location.rsplit("/", 1)[-1]
    response = client.post(
        f"/workflows/repo:fast/suggestions/{record_id}/discard",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert _store(fixture_repo).versions("repo:fast") == [1]
    records = _suggestion_records(fixture_repo)
    assert len(records) == 1 and records[0]["status"] == "discarded"
    again = _suggest(client, "repo:fast")
    assert again.headers["location"].endswith(f"/suggestions/{record_id}")
    assert len(_suggestion_records(fixture_repo)) == 1
    detail = client.get("/workflows/repo:fast").text
    assert "discarded" in detail and "not re-offered" in detail


def test_approve_after_new_runs_crossing_threshold_is_stale(
    client: TestClient, fixture_repo: Path
) -> None:
    """Staleness at the dashboard: a preset-only draft approved after new
    runs arrive is re-checked against the current evidence window
    before saving — and refused when the window crossed the threshold."""
    from jev_agent.core.models import RunState

    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    location = _suggest(client, "repo:fast").headers["location"]
    record_id = location.rsplit("/", 1)[-1]
    # Twelve completed efficiency-goal runs with stage records arrive
    # after the suggestion, moving the window across the threshold.
    with Store.open(fixture_repo / ".jev" / "state.db") as db:
        for i in range(12):
            run = db.create_run(
                Idea(text=f"task {i}", repo_path=fixture_repo),
                None,
                "none",
                goal="efficiency",
            )
            row_id = db.start_stage_row(run.id, "PLANNING")
            db.finish_stage_row(row_id, outcome="success")
            db.set_run_state(run.id, RunState.DONE)
    response = client.post(
        f"/workflows/repo:fast/suggestions/{record_id}/approve",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"].endswith(f"/suggestions/{record_id}")
    assert _store(fixture_repo).versions("repo:fast") == [1]
    assert _suggestion_records(fixture_repo)[0]["status"] == "pending"


def test_approve_identical_draft_creates_no_new_version(
    client: TestClient, fixture_repo: Path
) -> None:
    """Approval of a draft identical to the current version records
    approval of the existing version instead of creating a new one."""
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    location = _suggest(client, "repo:fast").headers["location"]
    record_id = location.rsplit("/", 1)[-1]
    record = _suggestion_records(fixture_repo)[0]
    store = _store(fixture_repo)
    draft = dict(record["draft"])
    draft["id"] = "repo:fast"
    store.save(draft)  # the profile is edited to match the draft: v2
    assert store.versions("repo:fast") == [1, 2]
    response = client.post(
        f"/workflows/repo:fast/suggestions/{record_id}/approve",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert store.versions("repo:fast") == [1, 2]
    assert _suggestion_records(fixture_repo)[0]["approved_version"] == 2


def test_approve_tampered_floor_drop_is_refused(client: TestClient, fixture_repo: Path) -> None:
    """Safety floor (B1) unchanged: a draft tampered to drop a floor
    behaviour (quality none while shipping) is refused at approval."""
    assert _create(client, PROFILE_FORM).status_code in (302, 303)
    location = _suggest(client, "repo:fast").headers["location"]
    record_id = location.rsplit("/", 1)[-1]
    path = (
        fixture_repo / ".jev" / "workflow-profiles" / "fast" / "suggestions" / f"{record_id}.json"
    )
    record = json.loads(path.read_text())
    record["draft"]["quality_level"] = "none"
    record["draft"]["ship_enabled"] = True
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    response = client.post(
        f"/workflows/repo:fast/suggestions/{record_id}/approve",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert _store(fixture_repo).versions("repo:fast") == [1]
    assert _suggestion_records(fixture_repo)[0]["status"] == "pending"


def test_cross_repo_label_displayed_for_user_scope(client: TestClient, fixture_repo: Path) -> None:
    """Cross-repo aggregation is labelled on the result page (and the
    request page offers it only for user-scope profiles)."""
    form = dict(PROFILE_FORM, scope="user", slug="mine")
    assert _create(client, form).status_code in (302, 303)
    text = client.get("/workflows/user:mine/suggest").text
    assert "cross_repo" in text
    response = client.post(
        "/workflows/user:mine/suggest",
        data={"csrf_token": _csrf(client), "goal": "efficiency", "cross_repo": "on"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    result = client.get(response.headers["location"]).text
    assert "cross-repo" in result


def test_usage_based_suggestion_approved_from_real_stage_rows(
    client: TestClient, fixture_repo: Path
) -> None:
    """Acceptance, measured path: with 12 completed efficiency runs whose
    DESIGNING rows ran 120 seconds each, the engine proposes the
    DESIGNING drop with its median — and approving it saves v2 without
    DESIGNING, which the run-creation picker then offers."""
    from jev_agent.core.models import RunState

    assert _create(client, PROFILE_FORM).status_code in (302, 303)

    class FakeClock:
        def __init__(self) -> None:
            self.now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

        def __call__(self) -> str:
            return self.now.isoformat()

        def advance(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    clock = FakeClock()
    with Store.open(fixture_repo / ".jev" / "state.db", clock=clock) as db:
        for i in range(12):
            run = db.create_run(
                Idea(text=f"task {i}", repo_path=fixture_repo),
                None,
                "none",
                goal="efficiency",
            )
            row_id = db.start_stage_row(run.id, "DESIGNING")
            clock.advance(120)
            db.finish_stage_row(row_id, outcome="success")
            db.set_run_state(run.id, RunState.DONE)
    location = _suggest(client, "repo:fast").headers["location"]
    text = client.get(location).text
    assert "usage-based" in text or "Drop DESIGNING" in text
    assert "median 120.0 seconds over 12 full runs" in text
    record_id = location.rsplit("/", 1)[-1]
    response = client.post(
        f"/workflows/repo:fast/suggestions/{record_id}/approve",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    store = _store(fixture_repo)
    assert "DESIGNING" not in store.get("repo:fast")["stage_list"]
    assert store.get("repo:fast")["origin"] == "suggested"
