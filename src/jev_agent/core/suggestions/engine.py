"""Usage-based suggestion engine (issue #368, phase 6, design section 12).

The engine runs only on request, produces drafts, and never executes: it
has no decision path during a run, writes no profile, changes no run, and
changes no default (section 12.5). It reads exactly the section 12.1
inputs — stage rows, lessons, CDA grades, cache rows, run outcomes — as
presented by the caller in an :class:`Evidence` object, and nothing else:
no model prices it was not configured with (unpriced models are excluded
from cheapest claims, so with no price map supplied no cheaper-model
recommendation is ever made), no other repo's runs for a repo-scope
suggestion, and no run whose user-stated goal is ``null`` as evidence for
a goal-conditioned recommendation (a run that picked the Efficiency preset
without stating a goal records ``goal`` ``null`` and counts as no goal
evidence, not efficiency evidence; ``custom``-goal runs are evidence only
for ``custom``-goal requests).

Thresholds are the section 12.3 design values, kept as module constants so
a design amendment can tune them in one place:

* 10 completed (``DONE``) runs with stage records in a 90-day window
  anchored at run end, for the scope the suggestion covers, before any
  usage-based recommendation. Below that the engine runs in preset-only
  mode: it may propose a preset, with the preset's documented behaviour
  as the reason, labelled "preset, not measured for your history".
* A stage-drop recommendation concerns only DESIGNING and SHIPPING, the
  only omittable stages, and requires at least 10 runs in which the stage
  ran, with a measured-median saving stated alongside it. DESIGNING and
  SHIPPING have no defect signal in the stage-row data, and their reason
  lines say so. Neither drop is a safety-floor behaviour: omitting design
  never reduces assurance (section 7.3), and omitting shipping with
  shipping off is the legitimate unshipped end state (section 10.1 item
  4) — so no reduced-assurance label travels with either, and the result
  page says so rather than implying one.
* A stage-shortening recommendation (fewer rounds, not removal) requires
  at least 10 runs, a measured-median saving, and the stage's defect
  signal stated; a stage whose defect signal is above zero in the window
  may not be recommended for shortening on cost grounds alone. The
  stage-row data carries no measured saving for a round-cap change (a
  cap constrains future runs; it does not refund measured seconds), so
  this engine proposes no round shortening: a change without a measured
  saving is not proposed, and the no-change outcome says so with the
  numbers instead of inventing one.
* A cheaper-model or cheaper-tier recommendation requires at least 5 runs
  with that model/tier at the run's quality level, no attributable
  quality-gate failure, and (for CDA tiers) the existing 3-graded-diff
  floor. With no configured price map this engine makes no such
  recommendation at all — a profile never invents a price (section 7.4).

Reasoning honesty is structural, not aspirational: a
:class:`ReasoningLine` that lacks its evidence (counts and medians with
sample sizes, the evidence window, and the completeness flag for a
measured line; the preset's documented behaviour for a preset line) is
refused by :func:`validate_reasoning_line` and never displayed, and a
change that cannot produce a valid line is not made (section 12.2). A
suggestion with no changes is not presented as a suggestion: the engine
returns the no-change outcome with the numbers.

Below-threshold requests say so plainly instead of inventing a
recommendation: the preset-only outcome names the counted volume, the
threshold, and the preset label, and the dashboard shows that text
verbatim.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jev_agent.core.workflow_profiles.presets import load_preset
from jev_agent.core.workflow_profiles.schema import GOALS

# Section 12.3 design values (tunable constants).
USAGE_MIN_COMPLETED_RUNS = 10
EVIDENCE_WINDOW_DAYS = 90
STAGE_DROP_MIN_RUNS = 10
CHEAPER_MIN_RUNS = 5
CDA_TIER_MIN_GRADED_DIFFS = 3

# The only stages a drop recommendation may concern (section 12.3, 7.3).
DROPPABLE_STAGES = ("DESIGNING", "SHIPPING")

# A preset-only proposal per stated goal (section 12.3). A custom goal has
# no preset: below threshold a custom-goal request gets the plain
# below-threshold statement and no draft, never a mismatched preset.
GOAL_PRESETS: dict[str, str | None] = {
    "efficiency": "efficiency",
    "accuracy": "accuracy",
    "custom": None,
}

PRESET_ONLY_LABEL = "preset, not measured for your history"


class EvidenceRefusal(ValueError):
    """A reasoning line without its evidence, refused — never displayed."""


class SuggestionRefusal(ValueError):
    """A suggestion request refused in plain words (bad goal, cross-repo
    for a repo-scope profile, an invalid draft, a stale approval)."""


@dataclass(frozen=True)
class StageRowEvidence:
    """One stage row, reduced to what section 12.1 lets the engine read."""

    stage: str
    outcome: str | None  # "success" | "halted" | "failed" | "skipped" | None
    work_seconds: float | None
    completeness: str | None  # "full" | "partial"
    defects_caught: int = 0

    @property
    def ran(self) -> bool:
        """The stage actually ran, as opposed to being skipped (a DESIGNING
        row under `when_needed` for a non-UI run) or left open."""
        return self.outcome == "success"


@dataclass(frozen=True)
class RunEvidence:
    """One run's section 12.1 evidence: its outcome, its user-stated goal
    (``None`` when none was stated — never goal evidence), and its stage
    rows. ``ended_at`` anchors the 90-day window at run end."""

    run_id: str
    repo_label: str
    state: str  # "DONE" | "NEEDS_HUMAN" | "FAILED" | ...
    goal: str | None
    ended_at: datetime
    stage_rows: tuple[StageRowEvidence, ...] = ()
    profile_id: str | None = None
    quality_level: str | None = None

    @property
    def completed(self) -> bool:
        """Reached DONE (section 12.3: stopped-for-human and failed runs
        count as evidence for failure rates but not toward the 10-run
        savings threshold)."""
        return self.state == "DONE"

    @property
    def has_stage_records(self) -> bool:
        return bool(self.stage_rows)


@dataclass(frozen=True)
class Evidence:
    """Everything one suggestion request may read (section 12.1), and
    nothing else. ``runs`` may span several repos only for a cross-repo
    request (``cross_repo`` true); a repo-scope request carries one repo.
    ``lesson_count`` is the lesson total for the profile's learning
    profile in the window — a new lesson arriving changes the evidence
    fingerprint, which is what the re-offer rule checks (section 12.4)."""

    runs: tuple[RunEvidence, ...] = ()
    scope: str = "repo"
    cross_repo: bool = False
    repos: tuple[str, ...] = ()
    lesson_count: int = 0

    @property
    def repo_labels(self) -> tuple[str, ...]:
        if self.repos:
            return self.repos
        labels: list[str] = []
        for run in self.runs:
            if run.repo_label not in labels:
                labels.append(run.repo_label)
        return tuple(labels)


@dataclass(frozen=True)
class ReasoningLine:
    """One change's reasoning record (section 12.2): what changes, the
    evidence (counts and medians with sample sizes, the evidence window,
    and the completeness flag), and what would prove it wrong. ``kind``
    is "measured" or "preset": a preset line's evidence is the preset's
    documented behaviour, labelled as such — never presented as measured
    (section 12.5)."""

    change: str
    evidence: str
    sample_size: int
    window: str
    completeness: str  # "full" | "partial" | "preset"
    falsifier: str
    kind: str = "measured"


def validate_reasoning_line(line: ReasoningLine) -> None:
    """Refuse a reason line without evidence — never display it. A
    measured line needs its evidence text, a positive sample, its window,
    and a completeness flag; a preset line needs the preset's documented
    behaviour as its evidence and the preset label in its window."""
    if not line.change.strip():
        raise EvidenceRefusal("a reasoning line without a change is refused")
    if not line.evidence.strip():
        raise EvidenceRefusal(f"a reasoning line for {line.change!r} without evidence is refused")
    if not line.window.strip():
        raise EvidenceRefusal(
            f"a reasoning line for {line.change!r} without its evidence window is refused"
        )
    if not line.falsifier.strip():
        raise EvidenceRefusal(
            f"a reasoning line for {line.change!r} without what would prove it wrong is refused"
        )
    if line.kind == "measured":
        if line.sample_size < 1:
            raise EvidenceRefusal(
                f"a measured reasoning line for {line.change!r} without a sample size is refused"
            )
        if line.completeness not in ("full", "partial"):
            raise EvidenceRefusal(
                f"a measured reasoning line for {line.change!r} without a completeness "
                "flag is refused"
            )
    elif line.kind == "preset":
        if line.completeness != "preset":
            raise EvidenceRefusal(
                f"a preset reasoning line for {line.change!r} must carry the preset "
                "completeness flag, not a measured one"
            )
    else:
        raise EvidenceRefusal(f"unknown reasoning line kind {line.kind!r}")


@dataclass(frozen=True)
class Change:
    """One draft change, in diffable form: the field, the old value, and
    the new value. The dedup key (section 6.6) is the from-profile version
    plus this change set."""

    field: str
    before: Any
    after: Any


@dataclass
class SuggestionOutcome:
    """What :func:`request_suggestion` returns. ``mode`` is "usage-based"
    (a measured draft), "preset-only" (below threshold, preset labelled
    as such), or "no-change" (at threshold, but no change beats the
    current flow on the stated goal with the data available — ``draft``
    is None and there is nothing to approve). ``message`` is the plain
    statement shown for preset-only and no-change outcomes."""

    mode: str
    from_profile_id: str
    from_profile_version: int | None
    goal: str
    draft: dict[str, Any] | None
    reasoning_lines: list[ReasoningLine] = field(default_factory=list)
    thresholds_met: dict[str, bool] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)
    message: str = ""
    cross_repo: bool = False
    repos: tuple[str, ...] = ()
    evidence_window: dict[str, Any] = field(default_factory=dict)
    evidence_fingerprint: str = ""


def _window_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now - timedelta(days=EVIDENCE_WINDOW_DAYS)
    return start, now


def _window_text(start: datetime, end: datetime) -> str:
    return (
        f"last {EVIDENCE_WINDOW_DAYS} days ({start.date().isoformat()} to {end.date().isoformat()})"
    )


def windowed_runs(evidence: Evidence, goal: str, now: datetime) -> list[RunEvidence]:
    """Completed runs with stage records in the window whose user-stated
    goal is the requested goal. A ``null`` goal never counts (section
    12.1); ``custom``-goal runs count only for ``custom`` requests."""
    start, end = _window_bounds(now)
    return [
        run
        for run in evidence.runs
        if run.completed
        and run.has_stage_records
        and run.goal == goal
        and start <= run.ended_at <= end
    ]


def measure_volume(evidence: Evidence, goal: str, now: datetime) -> int:
    """The entry-check count: goal-labelled completed runs with stage
    records in the window, for the scope this evidence covers."""
    return len(windowed_runs(evidence, goal, now))


def evidence_fingerprint(evidence: Evidence, goal: str, now: datetime) -> str:
    """The re-offer check (section 12.4): the windowed run set plus the
    lesson count. New runs in the window, a threshold crossed by new
    runs, or a new lesson arriving all change this fingerprint, so a
    discarded suggestion is re-offered only when the evidence moved.
    The window bounds are day-granular: the design's window is "the last
    90 days", so two requests on the same day with the same run set
    share a fingerprint — full timestamps would make every re-request
    look like new evidence and the re-offer rule could never fire."""
    start, _end = _window_bounds(now)
    windowed = windowed_runs(evidence, goal, now)
    payload = {
        "goal": goal,
        "scope": evidence.scope,
        "cross_repo": evidence.cross_repo,
        "repos": sorted(evidence.repo_labels),
        "window_start": start.date().isoformat(),
        "run_ids": sorted(run.run_id for run in windowed),
        "stage_row_counts": {
            run.run_id: len(run.stage_rows) for run in sorted(windowed, key=lambda r: r.run_id)
        },
        "lesson_count": evidence.lesson_count,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _evidence_window_dict(evidence: Evidence, goal: str, now: datetime) -> dict[str, Any]:
    start, end = _window_bounds(now)
    windowed = windowed_runs(evidence, goal, now)
    return {
        "goal": goal,
        "scope": evidence.scope,
        "cross_repo": evidence.cross_repo,
        "repos": list(evidence.repo_labels),
        "window_start": start.date().isoformat(),
        "window_end": end.date().isoformat(),
        "run_ids": sorted(run.run_id for run in windowed),
        "completed_runs": len(windowed),
        "lesson_count": evidence.lesson_count,
    }


def _median_full_seconds(rows: list[StageRowEvidence]) -> tuple[float | None, int, int]:
    """Median work seconds over full rows only (section 11.3: a median
    over a mixed set is quoted as "at least" with the partial count
    stated, never as an exact median over all rows). Returns
    (median, full_count, partial_count); median is None with no full row."""
    full = [r.work_seconds for r in rows if r.completeness == "full" and r.work_seconds is not None]
    partial = len([r for r in rows if r.completeness != "full"])
    if not full:
        return None, 0, partial
    return float(statistics.median(full)), len(full), partial


def _stage_runs(windowed: list[RunEvidence], stage: str) -> list[StageRowEvidence]:
    """Rows where the stage ran (outcome success) in the windowed set —
    skipped rows (DESIGNING under `when_needed` for a non-UI run) are not
    runs of the stage and never count toward the 10-run drop threshold."""
    rows: list[StageRowEvidence] = []
    for run in windowed:
        rows.extend(row for row in run.stage_rows if row.stage == stage and row.ran)
    return rows


def _preset_behaviour_text(preset: dict[str, Any]) -> str:
    """The preset's documented behaviour, derived from the preset file
    itself (never invented): its stage list and its round caps."""
    parts = [f"stage list {', '.join(preset.get('stage_list') or [])}"]
    for key, label in (
        ("max_plan_rounds", "plan rounds at most"),
        ("max_fix_rounds", "fix rounds at most"),
        ("score_max_retries", "score retries at most"),
    ):
        if preset.get(key) is not None:
            parts.append(f"{label} {preset[key]}")
    if preset.get("cda_grading") is not None:
        parts.append(f"CDA grading {preset['cda_grading']}")
    if preset.get("quality_level") is not None:
        parts.append(f"quality at least {preset['quality_level']}")
    return "; ".join(parts) + "."


def _apply_preset(from_profile: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    """The preset-only draft: the from-profile with the preset's settings
    overlaid, keeping the profile's own id, scope, display name, and
    version base. Fields the preset deliberately does not carry
    (quality on Efficiency, planner policy, cache mode, score threshold)
    stay at the profile's values — a preset never lowers or narrows
    (section 9.1)."""
    draft = dict(from_profile)
    draft.pop("version", None)
    draft.pop("release", None)
    for key in (
        "stage_list",
        "gate_policies",
        "designing",
        "max_plan_rounds",
        "max_fix_rounds",
        "score_max_retries",
        "cda_grading",
        "quality_level",
        "priority",
    ):
        if preset.get(key) is not None:
            draft[key] = preset[key]
    if preset.get("goal") is not None:
        draft["goal"] = preset["goal"]
    return draft


def diff_changes(before: dict[str, Any], after: dict[str, Any]) -> list[Change]:
    """The change set between two profile dicts, in canonical field
    order, ignoring version/release bookkeeping."""
    changes: list[Change] = []
    for key in sorted(set(before) | set(after)):
        if key in ("version", "release"):
            continue
        if before.get(key) != after.get(key):
            changes.append(Change(field=key, before=before.get(key), after=after.get(key)))
    return changes


def change_set_key(changes: list[Change]) -> str:
    """The dedup change-set rendering (section 6.6): the change set as
    canonical JSON, hashed by the caller together with the from-profile
    version."""
    return json.dumps(
        [{"field": c.field, "before": c.before, "after": c.after} for c in changes],
        sort_keys=True,
        default=str,
    )


def dedup_key(from_profile_id: str, from_version: int | None, changes: list[Change]) -> str:
    """Section 6.6: a hash of the from-profile version plus the change set."""
    payload = json.dumps(
        {
            "from": from_profile_id,
            "version": from_version,
            "changes": json.loads(change_set_key(changes)),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def request_suggestion(
    from_profile: dict[str, Any],
    goal: str,
    evidence: Evidence,
    *,
    now: datetime | None = None,
) -> SuggestionOutcome:
    """One suggestion request (section 12.4 step 2): a draft plus its
    reasoning record, or a plain below-threshold / no-change outcome.
    Never writes anything — the caller stores the record. A repo-scope
    request with ``cross_repo`` evidence is refused: another repo's runs
    are never read for a repo-scope suggestion (section 12.1)."""
    if goal not in GOALS:
        raise SuggestionRefusal(f"unknown goal {goal!r}: pick efficiency, accuracy, or custom")
    if evidence.scope == "repo" and evidence.cross_repo:
        raise SuggestionRefusal(
            "a repo-scope suggestion never reads another repo's runs: ask for a "
            "cross-repo suggestion from a user-scope profile instead"
        )
    moment = now or datetime.now(UTC)
    start, end = _window_bounds(moment)
    window = _window_text(start, end)
    windowed = windowed_runs(evidence, goal, moment)
    volume = len(windowed)
    fingerprint = evidence_fingerprint(evidence, goal, moment)
    window_dict = _evidence_window_dict(evidence, goal, moment)
    from_id = str(from_profile.get("id", ""))
    from_version = from_profile.get("version")
    if isinstance(from_version, str):
        from_version = None
    thresholds: dict[str, bool] = {
        "min_completed_runs_10": volume >= USAGE_MIN_COMPLETED_RUNS,
    }

    if volume < USAGE_MIN_COMPLETED_RUNS:
        # Preset-only mode (section 12.3): propose a preset, labelled.
        preset_name = GOAL_PRESETS[goal]
        message = (
            f"Below the usage threshold: {volume} completed {goal}-goal run(s) with "
            f"stage records in the {window}, but a usage-based suggestion needs at "
            f"least {USAGE_MIN_COMPLETED_RUNS}. No measured recommendation is made "
            f"from this history."
        )
        if preset_name is None:
            return SuggestionOutcome(
                mode="preset-only",
                from_profile_id=from_id,
                from_profile_version=from_version,
                goal=goal,
                draft=None,
                thresholds_met=thresholds,
                message=message
                + " No preset matches a custom goal, so no preset is proposed either.",
                cross_repo=evidence.cross_repo,
                repos=evidence.repo_labels,
                evidence_window=window_dict,
                evidence_fingerprint=fingerprint,
            )
        preset = load_preset(preset_name)
        draft = _apply_preset(from_profile, preset)
        changes = diff_changes(from_profile, draft)
        line = ReasoningLine(
            change=f"Adopt the {preset.get('display_name', preset_name)} preset's settings",
            evidence=_preset_behaviour_text(preset),
            sample_size=0,
            window=PRESET_ONLY_LABEL,
            completeness="preset",
            falsifier=(
                "this proposal is not measured: it would be proved wrong only by "
                "measured history, which is below threshold"
            ),
            kind="preset",
        )
        validate_reasoning_line(line)
        if not changes:
            # The profile already matches the preset: approving would
            # create no new version, and a preset-only suggestion with no
            # change is not presented as a suggestion (section 12.2).
            return SuggestionOutcome(
                mode="no-change",
                from_profile_id=from_id,
                from_profile_version=from_version,
                goal=goal,
                draft=None,
                thresholds_met=thresholds,
                message=message
                + f" Your flow already matches the {preset.get('display_name', preset_name)} "
                "preset, so there is no preset change to propose.",
                cross_repo=evidence.cross_repo,
                repos=evidence.repo_labels,
                evidence_window=window_dict,
                evidence_fingerprint=fingerprint,
            )
        return SuggestionOutcome(
            mode="preset-only",
            from_profile_id=from_id,
            from_profile_version=from_version,
            goal=goal,
            draft=draft,
            reasoning_lines=[line],
            thresholds_met=thresholds,
            changes=changes,
            message=message + f" The proposal below is a {PRESET_ONLY_LABEL}.",
            cross_repo=evidence.cross_repo,
            repos=evidence.repo_labels,
            evidence_window=window_dict,
            evidence_fingerprint=fingerprint,
        )

    # Usage-based: candidate drops, each needing 10 runs of the stage and
    # a measured-median saving, with its reason line carrying the numbers.
    stage_list = list(from_profile.get("stage_list") or [])
    draft = dict(from_profile)
    draft.pop("version", None)
    draft.pop("release", None)
    lines: list[ReasoningLine] = []
    thresholds["stage_drop_runs_10"] = True
    for stage in DROPPABLE_STAGES:
        if stage not in stage_list:
            continue
        rows = _stage_runs(windowed, stage)
        thresholds[f"stage_drop_{stage.lower()}_runs_10"] = len(rows) >= STAGE_DROP_MIN_RUNS
        if len(rows) < STAGE_DROP_MIN_RUNS:
            continue
        median, full_count, partial_count = _median_full_seconds(rows)
        if median is None or median <= 0:
            continue
        sample_text = f"median {median:.1f} seconds over {full_count} full runs"
        if partial_count:
            sample_text += (
                f" ({partial_count} partial runs not counted in the median, "
                "section 11.3: partial rows are never quoted as exact)"
            )
        if stage == "DESIGNING":
            change_text = "Drop DESIGNING from this flow"
            evidence_text = (
                f"DESIGNING ran in {len(rows)} of your last {volume} completed "
                f"{goal}-goal runs in the {window}; {sample_text}. DESIGNING has "
                "no defect signal in the data, so no defect count guards this "
                "change."
            )
            falsifier = (
                "a future run that needs UI design work this flow can no longer "
                "produce, or a DESIGNING median that rises above the saving claimed"
            )
            draft["stage_list"] = [s for s in draft["stage_list"] if s != "DESIGNING"]
            draft["designing"] = "never"
            draft["gate_policies"] = {
                k: v for k, v in (draft.get("gate_policies") or {}).items() if k != "DESIGNING"
            }
        else:
            change_text = "Drop SHIPPING from this flow"
            evidence_text = (
                f"SHIPPING ran in {len(rows)} of your last {volume} completed "
                f"{goal}-goal runs in the {window}; {sample_text}. SHIPPING has "
                "no defect signal in the data. The run would end at DONE "
                "unshipped instead of shipping."
            )
            falsifier = (
                "a future run whose result you need shipped, or a SHIPPING median "
                "that rises above the saving claimed"
            )
            draft["stage_list"] = [s for s in draft["stage_list"] if s != "SHIPPING"]
            draft["gate_policies"] = {
                k: v for k, v in (draft.get("gate_policies") or {}).items() if k != "SHIPPING"
            }
            draft["ship_enabled"] = False
        line = ReasoningLine(
            change=change_text,
            evidence=evidence_text,
            sample_size=len(rows),
            window=window,
            completeness="full" if partial_count == 0 else "partial",
            falsifier=falsifier,
            kind="measured",
        )
        validate_reasoning_line(line)
        lines.append(line)

    changes = diff_changes(from_profile, draft)
    if not lines or not changes:
        # No change beats the current flow with the data available
        # (section 12.2): say so with the numbers, propose nothing.
        return SuggestionOutcome(
            mode="no-change",
            from_profile_id=from_id,
            from_profile_version=from_version,
            goal=goal,
            draft=None,
            thresholds_met=thresholds,
            message=(
                f"No change beats your current flow on your stated goal with the data "
                f"available: {volume} completed {goal}-goal runs with stage records "
                f"in the {window} support no stage drop (a drop needs at least "
                f"{STAGE_DROP_MIN_RUNS} runs of the stage with a measured-median "
                f"saving) and no round-cap saving is measurable from stage rows."
            ),
            cross_repo=evidence.cross_repo,
            repos=evidence.repo_labels,
            evidence_window=window_dict,
            evidence_fingerprint=fingerprint,
        )
    return SuggestionOutcome(
        mode="usage-based",
        from_profile_id=from_id,
        from_profile_version=from_version,
        goal=goal,
        draft=draft,
        reasoning_lines=lines,
        thresholds_met=thresholds,
        changes=changes,
        cross_repo=evidence.cross_repo,
        repos=evidence.repo_labels,
        evidence_window=window_dict,
        evidence_fingerprint=fingerprint,
    )


def recheck_for_approval(
    record: dict[str, Any],
    fresh: SuggestionOutcome,
    current_profile: dict[str, Any],
) -> tuple[bool, str]:
    """Staleness re-check (section 12.4, phase-6 test): a draft approved
    after new runs arrive is re-checked against the current evidence
    window before saving. Returns (ok, message). A usage-based draft is
    stale when the fresh change set differs from the draft's; a
    preset-only draft is stale when the volume has crossed the usage
    threshold (a measured suggestion may now differ). The identical-draft
    case is not staleness: approval then records approval of the existing
    version instead of creating a new one (handled by the caller)."""
    draft = record.get("draft") or {}
    if not diff_changes(current_profile, draft):
        return True, "the draft matches the current version"
    mode = record.get("mode")
    if mode == "preset-only":
        if fresh.mode != "preset-only":
            return False, (
                "the evidence changed since this suggestion was made: usage history "
                "has now crossed the threshold, so this preset-only draft is stale. "
                "Ask for a fresh suggestion instead of approving it."
            )
        fresh_changes = diff_changes(current_profile, fresh.draft or {})
        old_changes = diff_changes(current_profile, draft)
        if change_set_key(fresh_changes) != change_set_key(old_changes):
            return False, (
                "the evidence changed since this suggestion was made: a fresh "
                "preset-only proposal differs from this draft. Ask for a fresh "
                "suggestion instead of approving it."
            )
        return True, "the evidence window is unchanged"
    # Usage-based (and any other mode with a draft): the change set must
    # still be what the current evidence supports.
    fresh_changes = diff_changes(current_profile, fresh.draft or {}) if fresh.draft else []
    old_changes = diff_changes(current_profile, draft)
    if fresh.mode == "no-change" or change_set_key(fresh_changes) != change_set_key(old_changes):
        return False, (
            "the evidence changed since this suggestion was made: the current "
            "evidence window no longer supports this draft's changes. Ask for a "
            "fresh suggestion instead of approving it."
        )
    return True, "the current evidence window still supports this draft"


__all__ = [
    "CDA_TIER_MIN_GRADED_DIFFS",
    "CHEAPER_MIN_RUNS",
    "DROPPABLE_STAGES",
    "EVIDENCE_WINDOW_DAYS",
    "GOAL_PRESETS",
    "PRESET_ONLY_LABEL",
    "STAGE_DROP_MIN_RUNS",
    "USAGE_MIN_COMPLETED_RUNS",
    "Change",
    "Evidence",
    "EvidenceRefusal",
    "ReasoningLine",
    "RunEvidence",
    "StageRowEvidence",
    "SuggestionOutcome",
    "SuggestionRefusal",
    "change_set_key",
    "dedup_key",
    "diff_changes",
    "evidence_fingerprint",
    "measure_volume",
    "recheck_for_approval",
    "request_suggestion",
    "validate_reasoning_line",
    "windowed_runs",
]
