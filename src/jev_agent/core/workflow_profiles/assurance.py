"""Assurance computation and label text (issue #368, design section 10.3).

Assurance is computed at run creation from the run's effective settings
(the snapshot for a profiled run, the run's settings for a legacy run) and
stored on the run record. `full` when every floor behaviour in section 10.1
is present; `reduced` otherwise, plus the absent behaviours in plain words.
The label text is identical everywhere it renders — run page, board row, PR
body — because all three call `assurance_label_text` (or the views pass its
result through), never a local copy of the wording.
"""

from __future__ import annotations


def compute_assurance(
    *,
    quality_level: str | None,
    score_skip_human: bool,
    autonomy_mode: str,
    plan_gate_present: bool,
) -> tuple[str, list[str]]:
    """The (assurance, absent-behaviours) pair for effective settings.

    Labelled bypass classes (section 10.2): quality `none` in configuration,
    AUTONOMOUS plan-gate absence, `score_skip_human` in configuration, plan
    approval off. A missing ship score is an absent behaviour too, but it is
    only knowable at ship time, not at creation, so creation never claims it.
    """
    absent: list[str] = []
    if quality_level == "none":
        absent.append("quality gate off")
    if score_skip_human:
        absent.append("ship score gate skipped")
    if autonomy_mode == "autonomous":
        absent.append("plan approval gate absent (autonomous mode)")
    elif not plan_gate_present:
        absent.append("plan approval gate absent")
    return ("full", []) if not absent else ("reduced", absent)


def assurance_label_text(assurance: str | None, absent: list[str] | tuple[str, ...]) -> str:
    """The display term everywhere is "assurance label": the same text on the
    run page, the board row, and in the pull request body."""
    if assurance != "reduced" or not absent:
        return "Full checks"
    joined = "; ".join(absent)
    return f"Reduced checks — {joined}. Review the changes before merging."
