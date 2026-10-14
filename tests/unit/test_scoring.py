"""The 0-100 ship score: scoring math, gate boundaries, retries, human skip."""

from __future__ import annotations

import pytest

from jev_agent.config import JevConfig
from jev_agent.config.schema import DEFAULT_LAYA_WEIGHTS, LAYA_CRITERIA, QualityConfig
from jev_agent.quality.models import (
    CheckResult,
    CheckStatus,
    Finding,
    GateResult,
    GateScope,
    ScoreBreakdown,
)
from jev_agent.quality.report import render_gate
from jev_agent.quality.scoring import (
    SCORE_FIX_TITLE,
    diff_stats,
    score_fix_rounds,
    score_gate,
    ship_gate_outcome,
)
from jev_agent.roles.schemas import Category, Severity

SMALL_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,4 @@
 import os
+GREETING = "hello"
 print(os.name)
+print(GREETING)
"""


def _check(
    name: str = "tests",
    status: CheckStatus = "pass",
    required: bool = True,
    findings: list[Finding] | None = None,
) -> CheckResult:
    return CheckResult(
        status=status,
        name=name,
        required=required,
        findings=findings or [],
    )


def _finding(severity: Severity = "major", category: Category = "bug") -> Finding:
    return Finding(
        source="test",
        severity=severity,
        category=category,
        title="something is wrong",
    )


def _gate(checks: list[CheckResult] | None = None, scope: GateScope = "integration") -> GateResult:
    return GateResult(
        level="basic",
        scope=scope,
        checks=checks if checks is not None else [_check()],
    )


def _big_diff(added_lines: int = 3000) -> str:
    body = "".join(f"+line {n}\n" for n in range(added_lines))
    header = (
        f"diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -0,0 +1,{added_lines} @@\n"
    )
    return header + body


# ---- scoring math ----------------------------------------------------------------------------


def test_clean_small_diff_scores_96() -> None:
    """All 5s except user preference (neutral 3): 30+20+15+15+10+6."""
    score = score_gate(_gate(), diff=SMALL_DIFF)
    assert score.criteria == {
        "correctness": 5,
        "safety": 5,
        "simplicity": 5,
        "maintainability": 5,
        "tool_fit": 5,
        "user_preference": 3,
    }
    assert score.total == 96
    assert score.passed is True
    assert score.threshold == 85
    assert set(score.details) == set(LAYA_CRITERIA)


def test_laya_weights_are_reused_not_reinvented() -> None:
    weights = {"correctness": 1.0, **{c: 0.0 for c in LAYA_CRITERIA if c != "correctness"}}
    gate = _gate([_check("tests", "fail", required=True)])
    score = score_gate(gate, diff=SMALL_DIFF, weights=weights)
    assert score.criteria["correctness"] == 3
    assert score.total == 60  # correctness only, 3/5
    assert score.weights["correctness"] == pytest.approx(1.0)


def test_weights_must_name_all_six_criteria() -> None:
    with pytest.raises(ValueError, match="exactly the criteria"):
        score_gate(_gate(), diff=SMALL_DIFF, weights={"correctness": 1.0})
    with pytest.raises(ValueError, match="exactly the criteria"):
        score_gate(_gate(), diff=SMALL_DIFF, weights={**DEFAULT_LAYA_WEIGHTS, "bogus": 0.0})


def test_failed_required_check_costs_correctness() -> None:
    gate = _gate([_check("tests", "fail", required=True)])
    score = score_gate(gate, diff=SMALL_DIFF)
    assert score.criteria["correctness"] == 3  # 5 - 2
    assert score.total == 84  # 96 - 12
    assert score.passed is False
    assert "required check(s) failed" in score.details["correctness"]


def test_optional_failures_cost_less() -> None:
    gate = _gate([_check("tests", "pass"), _check("licenses", "fail", required=False)])
    score = score_gate(gate, diff=SMALL_DIFF)
    assert score.criteria["correctness"] == 4
    assert score.total == 90


def test_critical_finding_tanks_safety() -> None:
    gate = _gate([_check("secrets", "fail", findings=[_finding("critical", "security")])])
    score = score_gate(gate, diff=SMALL_DIFF)
    assert score.criteria["safety"] == 1
    # the failed required check also costs correctness: 18 + 4 + 15 + 15 + 10 + 6
    assert score.criteria["correctness"] == 3
    assert score.total == 68
    assert score.passed is False


def test_major_bug_findings_cost_correctness() -> None:
    findings = [_finding("major", "bug"), _finding("major", "bug")]
    gate = _gate([_check("tests", "pass", findings=findings)])
    score = score_gate(gate, diff=SMALL_DIFF)
    assert score.criteria["correctness"] == 3
    assert score.total == 84


def test_major_security_findings_cost_safety() -> None:
    findings = [_finding("major", "security")]
    gate = _gate([_check("bandit", "pass", findings=findings)])
    score = score_gate(gate, diff=SMALL_DIFF)
    assert score.criteria["safety"] == 3
    assert score.total == 88


def test_large_diff_costs_simplicity() -> None:
    score = score_gate(_gate(), diff=_big_diff())
    assert score.criteria["simplicity"] == 1
    assert score.total == 84
    assert score.passed is False


def test_failed_style_and_type_checks_cost_maintainability() -> None:
    gate = _gate([_check("ruff", "fail"), _check("mypy", "fail"), _check("tests", "pass")])
    score = score_gate(gate, diff=SMALL_DIFF)
    assert score.criteria["maintainability"] == 3  # 5 - 1 - 1
    # correctness also drops: ruff and mypy are required checks that failed
    assert score.criteria["correctness"] == 1  # 5 - 2 - 2, clamped


def test_missing_required_tools_cost_tool_fit() -> None:
    gate = _gate([_check("bandit", "skipped", required=True), _check("tests", "pass")])
    score = score_gate(gate, diff=SMALL_DIFF)
    assert score.criteria["tool_fit"] == 4
    assert score.total == 94


def test_user_preference_is_neutral_without_a_signal() -> None:
    score = score_gate(_gate(), diff=SMALL_DIFF)
    assert score.criteria["user_preference"] == 3
    assert "neutral" in score.details["user_preference"]


def test_score_survives_a_json_round_trip() -> None:
    """The breakdown is audit-logged in `quality_reports.result_json`."""
    gate = _gate()
    scored = gate.model_copy(update={"score": score_gate(gate, diff=SMALL_DIFF)})
    back = GateResult.model_validate_json(scored.model_dump_json())
    assert back.score is not None
    assert back.score.total == 96
    assert back.score.criteria["correctness"] == 5


# ---- the ship gate: threshold, retries, human skip --------------------------------------------


def test_score_at_the_threshold_passes() -> None:
    score = score_gate(_gate(), diff=SMALL_DIFF, threshold=96)
    assert score.total == 96
    assert score.passed is True
    assert score_gate(_gate(), diff=SMALL_DIFF, threshold=97).passed is False


def test_threshold_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="0-100"):
        score_gate(_gate(), diff=SMALL_DIFF, threshold=101)


def test_human_skip_scores_but_passes() -> None:
    gate = _gate([_check("tests", "fail", required=True)])
    score = score_gate(gate, diff=SMALL_DIFF, skipped=True, skip_reason="human-authored")
    assert score.total == 84  # the score is still recorded
    assert score.skipped is True
    assert score.passed is True
    assert ship_gate_outcome(score, max_retries=2, retry_round=99) == "ship"


def test_ship_gate_outcome_without_a_score_ships() -> None:
    assert ship_gate_outcome(None, max_retries=2, retry_round=0) == "ship"


def test_ship_gate_rework_is_bounded() -> None:
    failing: ScoreBreakdown = score_gate(
        _gate([_check("tests", "fail", required=True)]), diff=SMALL_DIFF
    )
    assert failing.passed is False
    assert ship_gate_outcome(failing, max_retries=2, retry_round=0) == "rework"
    assert ship_gate_outcome(failing, max_retries=2, retry_round=1) == "rework"
    assert ship_gate_outcome(failing, max_retries=2, retry_round=2) == "stop"
    assert ship_gate_outcome(failing, max_retries=0, retry_round=0) == "stop"


def test_score_fix_rounds_counts_only_score_fix_tasks() -> None:
    titles = [
        "Add search",
        f"{SCORE_FIX_TITLE} (round 1)",
        "Fix integration failures (round 1)",
        f"{SCORE_FIX_TITLE} (round 2)",
    ]
    assert score_fix_rounds(titles) == 2
    assert score_fix_rounds([]) == 0


def test_threshold_and_knobs_come_from_config() -> None:
    assert JevConfig().quality.score_threshold == 85
    assert JevConfig().quality.score_max_retries == 2
    assert JevConfig().quality.score_skip_human is False
    cfg = JevConfig(
        quality=QualityConfig(score_threshold=90, score_max_retries=1, score_skip_human=True)
    )
    assert cfg.quality.score_threshold == 90
    assert cfg.quality.score_max_retries == 1
    assert cfg.quality.score_skip_human is True


# ---- diff stats ------------------------------------------------------------------------------


def test_diff_stats_counts_files_and_lines() -> None:
    stats = diff_stats(SMALL_DIFF)
    assert stats.files_changed == 1
    assert stats.added == 2
    assert stats.removed == 0
    assert diff_stats("").files_changed == 0


# ---- surfacing -------------------------------------------------------------------------------


def test_gate_report_shows_the_score_breakdown() -> None:
    gate = _gate()
    scored = gate.model_copy(update={"score": score_gate(gate, diff=SMALL_DIFF)})
    text = render_gate(scored)
    assert "## Score" in text
    assert "**96/100** (threshold 85)" in text
    assert "| Correctness | 30% | 5 |" in text
    assert "passed the ship gate" in text


def test_gate_report_marks_a_failing_score() -> None:
    gate = _gate([_check("tests", "fail", required=True)])
    scored = gate.model_copy(update={"score": score_gate(gate, diff=SMALL_DIFF)})
    text = render_gate(scored)
    assert "**84/100** (threshold 85)" in text
    assert "below the ship threshold" in text


def test_gate_report_without_a_score_says_so() -> None:
    assert "Not scored" in render_gate(_gate())
