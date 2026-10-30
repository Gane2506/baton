"""Phase-0 conformance skeleton (design section 15, phase 0; issue #368).

Acceptance: the skeleton runs green on the legacy path and the baselines are
recorded, so a later change cannot quietly redefine "unchanged". Phase 3 grows
this skeleton into the legacy-vs-`default`-preset comparison suite under the
stub/replay harness; these tests only pin the skeleton itself: every scenario
in the section 17 row has a baseline, every baseline is a valid canonical
subsequence with its gate parkings and label output, and a legacy run (no
workflow profile) reads back with a null profile id and no snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scenarios import CANONICAL_ORDER, SCENARIOS

from jev_agent.core.models import Idea
from jev_agent.core.store import Store

BASELINE_DIR = Path(__file__).parent / "baseline"


def _canonical_index(stage: str) -> int:
    return CANONICAL_ORDER.index(stage)


def test_every_scenario_has_a_baseline() -> None:
    recorded = {p.stem for p in BASELINE_DIR.glob("*.json")}
    missing = [s["id"] for s in SCENARIOS if s["id"] not in recorded]
    assert not missing, f"scenarios without a recorded baseline: {missing}"


def test_baselines_match_the_scenario_list() -> None:
    for scenario in SCENARIOS:
        baseline = json.loads((BASELINE_DIR / f"{scenario['id']}.json").read_text())
        assert baseline["path"] == "legacy"
        assert baseline["stage_sequence"] == scenario["stage_sequence"]
        assert baseline["gate_parkings"] == scenario["gate_parkings"]
        assert baseline["outcome"] == scenario["outcome"]
        assert baseline["label_output"] == scenario["label_output"]


def test_baseline_stage_sequences_are_canonical_subsequences() -> None:
    """A stage sequence may repeat a stage (loop-backs) but never runs one
    before an earlier canonical stage it depends on (design section 7.1)."""
    for scenario in SCENARIOS:
        sequence = scenario["stage_sequence"]
        assert sequence, f"{scenario['id']}: empty stage sequence"
        # First visits must follow canonical order; repeats are loop-backs.
        first_visit = []
        for stage in sequence:
            if stage not in first_visit:
                first_visit.append(stage)
        assert first_visit == sorted(first_visit, key=_canonical_index), (
            f"{scenario['id']}: first visits out of canonical order: {first_visit}"
        )
        for stage in sequence:
            assert stage in CANONICAL_ORDER, f"{scenario['id']}: unknown stage {stage!r}"


def test_baseline_label_output_is_present() -> None:
    for scenario in SCENARIOS:
        assert scenario["label_output"], f"{scenario['id']}: no label output recorded"


def test_legacy_run_reads_back_without_profile_or_snapshot(tmp_path: Path) -> None:
    """Design section 11/phase 1: legacy runs read back with a null profile id
    and no stage rows; nothing backfills invented history."""
    with Store.open(tmp_path / "state.db") as store:
        run = store.create_run(Idea(text="legacy skeleton run", repo_path=tmp_path), None, "none")
        reread = store.get_run(run.id)
        assert reread.workflow_profile_id is None
        assert reread.workflow_profile_version is None
        assert reread.flow_snapshot is None
        assert store.list_stage_rows(run.id) == []


def test_conformance_skeleton_covers_the_section_17_row() -> None:
    ids = {s["id"] for s in SCENARIOS}
    required = {
        "plan-revise-loop",
        "fix-task-loop",
        "score-rework-loop",
        "designing-on",
        "designing-off",
        "ship-merge",
        "ship-ask",
        "ship-off",
        "autonomous",
        "ask-every-step",
        "manual",
        "medium",
        "halt-resume",
        "cancel-mid-run",
    }
    assert required <= ids, f"missing section 17 scenarios: {sorted(required - ids)}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario_baseline_is_valid_json_with_required_keys(scenario: dict) -> None:
    baseline = json.loads((BASELINE_DIR / f"{scenario['id']}.json").read_text())
    for key in ("scenario", "path", "stage_sequence", "gate_parkings", "outcome", "label_output"):
        assert key in baseline, f"{scenario['id']}: baseline missing {key!r}"
