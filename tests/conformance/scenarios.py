"""Phase-0 conformance skeleton (Baton issue #368, design section 8.2 / 15 phase 0).

The scenario list for the section 17 conformance row. Each scenario names the
legacy-path stage sequence, gate parkings, outcome, and label output that the
phase-3 comparison suite will require the `default` preset path to reproduce
exactly (empty deliberate-difference registry). Phase 0 records these as
baselines under ``tests/conformance/baseline/``; it does not yet run the
comparison — that is phase 3's work, behind the stub/replay harness.

Baseline fixture configuration (every baseline is recorded against this, so a
later change cannot quietly redefine "unchanged"): quality level ``medium``,
``[approval] require_plan_approval`` on, shipping off, standard autonomy mode,
unless the scenario itself varies one of those. Under that fixture the
assurance computation (design section 10.3) is ``full``: the quality gate
runs, the plan gate applies, and no bypass class from section 10.2 is in play.
Scenarios that vary the fixture record their own reduced label instead.
"""

from __future__ import annotations

from typing import Any

# Canonical work-state order (design section 7.1). Every stage sequence below
# is a subsequence of this order; SHIPPING appears only when shipping is on.
CANONICAL_ORDER = (
    "INTAKE",
    "PLANNING",
    "VALIDATING",
    "DESIGNING",
    "EXECUTING",
    "INTEGRATING",
    "SHIPPING",
)

# One entry per section 17 conformance row. ``stage_sequence`` is the work
# states visited on the legacy path, in order, with repeats for loop-backs
# (a fix-task loop re-enters EXECUTING, a plan revise re-enters PLANNING).
# ``gate_parkings`` names the waiting states the run parks in, in order.
# ``label_output`` is the assurance label text the run page, board row, and
# PR body must all show for the scenario (design section 10.3).
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "standard",
        "description": "Standard run, no UI work, shipping off: straight through to DONE.",
        "stage_sequence": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "plan-revise-loop",
        "description": "D3 revises the plan once before approving it.",
        "stage_sequence": [
            "INTAKE",
            "PLANNING",
            "VALIDATING",
            "PLANNING",
            "VALIDATING",
            "EXECUTING",
            "INTEGRATING",
        ],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "fix-task-loop",
        "description": "D6 adds one fix task for failing integration tests, then ships.",
        "stage_sequence": [
            "INTAKE",
            "PLANNING",
            "VALIDATING",
            "EXECUTING",
            "INTEGRATING",
            "EXECUTING",
            "INTEGRATING",
        ],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "score-rework-loop",
        "description": "Ship score below threshold once, one score fix task, then ship vote.",
        "stage_sequence": [
            "INTAKE",
            "PLANNING",
            "VALIDATING",
            "EXECUTING",
            "INTEGRATING",
            "EXECUTING",
            "INTEGRATING",
        ],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "designing-on",
        "description": "Plan needs a design: DESIGNING runs after validation.",
        "stage_sequence": [
            "INTAKE",
            "PLANNING",
            "VALIDATING",
            "DESIGNING",
            "EXECUTING",
            "INTEGRATING",
        ],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "designing-off",
        "description": "Plan needs no design: DESIGNING is skipped, never entered.",
        "stage_sequence": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "ship-merge",
        "description": "Shipping on, after_approval merge: SHIPPING runs, then DONE.",
        "stage_sequence": [
            "INTAKE",
            "PLANNING",
            "VALIDATING",
            "EXECUTING",
            "INTEGRATING",
            "SHIPPING",
        ],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "ship-ask",
        "description": "Shipping on, after_approval ask: parks in AWAITING_SHIP, then SHIPPING.",
        "stage_sequence": [
            "INTAKE",
            "PLANNING",
            "VALIDATING",
            "EXECUTING",
            "INTEGRATING",
            "SHIPPING",
        ],
        "gate_parkings": ["AWAITING_APPROVAL", "AWAITING_SHIP"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "ship-off",
        "description": "Shipping off: a ship vote ends the run at DONE without SHIPPING.",
        "stage_sequence": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "autonomous",
        "description": "AUTONOMOUS opt-in: the plan gate is skipped; assurance is reduced.",
        "stage_sequence": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
        "gate_parkings": [],
        "outcome": "DONE",
        "label_output": "Reduced checks — plan approval gate absent (autonomous mode)",
    },
    {
        "id": "ask-every-step",
        "description": "ASK_EVERY_STEP: a gate parks before every step.",
        "stage_sequence": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
        "gate_parkings": [
            "AWAITING_PLANNING",
            "AWAITING_VALIDATING",
            "AWAITING_APPROVAL",
            "AWAITING_INTEGRATING",
        ],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "manual",
        "description": "MANUAL: the run waits in INTAKE for an explicit instruction.",
        "stage_sequence": ["INTAKE"],
        "gate_parkings": [],
        "outcome": "waiting",
        "label_output": "Full checks",
    },
    {
        "id": "medium",
        "description": "MEDIUM: the standard flow scoped to a folder.",
        "stage_sequence": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "halt-resume",
        "description": "Halt mid-run (NEEDS_HUMAN with resume_state), then resume to DONE.",
        "stage_sequence": [
            "INTAKE",
            "PLANNING",
            "VALIDATING",
            "EXECUTING",
            "EXECUTING",
            "INTEGRATING",
        ],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "DONE",
        "label_output": "Full checks",
    },
    {
        "id": "cancel-mid-run",
        "description": "Cancelled mid-run: the run stops CANCELLED, open stage rows close.",
        "stage_sequence": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING"],
        "gate_parkings": ["AWAITING_APPROVAL"],
        "outcome": "CANCELLED",
        "label_output": "Full checks",
    },
]
