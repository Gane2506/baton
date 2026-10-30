# Resolution report — Baton issue #368 design (profile-based process flows)

Design doc: `docs/designs/profile-workflows/design.md` (amended by
this resolution; 2,337 lines after amendment).
Review reports answered here (9 total, all on disk):

| Stage | Report | Blockers | Majors | Nits |
|---|---|---|---|---|
| 5 zoom-out | `zoom_out_report.md` | 2 | 9 | 5 |
| 6 comprehension | `comprehension_report.md` | 12 | 18 | 7 |
| 7 design critic | `critic_design_report.md` | 8 | 12 | 6 |
| 7 structural critic | `critic_structural_report.md` | 7 | 15 | 5 |
| 7 style critic | `critic_style_report.md` | 7 | 14 | 12 |
| 7 consolidated | `critic_report.md` | (merge of the three stage-7 lenses; 24 themes) | | |
| 8 intermediate zoom-out | `intermediate_zoom_out_report.md` | 8 | 17 | 4 |
| 9 readiness | `implementation_readiness_report.md` | 8 | 12 | 6 |
| 9 readiness, style lens | `implementation_style_report.md` | 6 | 10 | 8 |
| 9 readiness, codebase lens | `implementation_codebase_report.md` | 12 | 16 | 5 |
| **Totals across lenses** | | **70** | **123** | **58** |

The per-lens totals double-count: the lenses independently found
the same underlying issues. The R-groups below deduplicate them:
each group names the underlying issue once, cites every finding
that raised it, and gives one classification. No severity was
changed; a finding whose severity is disputed is contested, not
downgraded.

Classification rules used: PROPOSE FIX = the design was wrong or
incomplete and the amendment fixes it (the fix is described and
was applied to `design.md`). CONTEST = the critique is inaccurate
or rests on a misunderstanding, with checkable evidence (a file
and symbol, or a decision recorded in the document — "this was
intentional" is not evidence). DECLINE = a nit or suggestion
recorded but not fixed, with a reason. ESCALATE = the fix needs
evidence outside every file the reviewers could read, or a
judgement only the human gate can make; the resolution states
the chosen default and the question. A comprehension gap is never
contested.

## R-groups

### R1. One non-omittable list — PROPOSE FIX (applied)

Findings: stage 5 Major 1; stage 6 B4; stage 7 design B2 (in
part), structural B4 (in part), style B3; stage 8 P1; stage 9
codebase B12 (in part).
Fix: section 7.3 is the single normative list (INTAKE, PLANNING,
VALIDATING, EXECUTING, INTEGRATING non-omittable). Section 10.2
now points at section 7.3 instead of restating a shorter list.
Under B3 (label-only), the stage core stays refused; B3 covers
behaviour classes only (section 7.3, section 16 B3). The
"decided vs. open" list in the header names section 7.3 as
B-dependent so the re-audit revisits it first under a B3 pick.

### R2. One snapshot definition, and a merge rule — PROPOSE FIX (applied)

Findings: stage 6 B2; stage 7 structural B2, style B7; stage 8
P2, N6; stage 9 codebase B11; stage 5 Major 8 (in part).
Fix: "snapshot" now means exactly one thing — the stored full
copy of the resolved parameter set (section 5, section 6.4). The
other sense is renamed "profile reference" (id plus version, no
parameters). Resolution happens once at creation in section 9.2
order; the snapshot is complete, so there is no run-time fallback
(section 8.1 fallback deleted). The per-setting winning source
lives in the snapshot JSON as a `resolved_from` map (section
6.6), not as separate columns. Merge rule (section 6.4): for a
profiled run, handlers read snapshot fields only; the run
record's `quality_level`, `ship_mode`, and `learning_profile`
fields keep their meaning for legacy runs only. Storage: one
`flow_snapshot` JSON column plus scalar columns for querying
(`workflow_profile_id`, `workflow_profile_version` /
`workflow_profile_release`, `goal`, `preset_goal`, `assurance`,
`assurance_absent`).

### R3. Snapshot checks at every handler move site — PROPOSE FIX (applied)

Findings: stage 7 design B1, structural B6; stage 6 B1 (in part);
stage 8 P5; stage 9 codebase B6 (in part), readiness M9 (in part).
Fix: section 8.1 now carries a normative move-site table
(`_apply_d3_verdict` → DESIGNING, `_integrate` loop-backs,
`_ship_after_score_gate` rework, `_after_ship_vote`,
`_integrate` → AWAITING_SHIP, `Pipeline.approve` gate-to-step
map, `_advance` loop breakout). Rule: a handler target not in
the snapshot stage list halts the run with a named error —
never a silent reroute, never a stall, never an early `_finish`.
The EXECUTING loop-backs need no additional check because
EXECUTING is non-omittable, stated in the table so the site is
not missed.

### R4. Gate-policy table and the autonomy contradictions — PROPOSE FIX (applied)

Findings: stage 6 B1, M2; stage 7 design M1, structural B6 (in
part), style M8; stage 8 P5 (in part); stage 9 codebase B8, M13
(in part).
Fix: section 7.2 now carries the normative step × policy →
waiting-state table. INTAKE policies refuse at save (no gate
state exists). The two AWAITING_APPROVAL parkings are
distinguished by a `parked_step` field on gate records (plan
gate → VALIDATING; ASK_EVERY_STEP executing gate →
EXECUTING). SHIPPING policy governs AWAITING_SHIPPING only;
AWAITING_SHIP is governed by `after_approval` alone. `require`
under AUTONOMOUS refuses at creation with a named message
("Autonomous runs do not park for approval. Pick a gated mode
or drop the required gate."). The gate-policy value `default`
is renamed `inherit` (it meant "existing behaviour under the
mode", not the `default` preset). All five autonomy modes plus
the `AUTONOMOUS` opt-in flag are covered (MANUAL: snapshot
applies on advance; MEDIUM: snapshot applies within the folder
scope, which the profile does not change).

### R5. Ship-mode translation — PROPOSE FIX (applied)

Findings: stage 6 B3 (in part); stage 7 design (in B3 context);
stage 9 codebase B3.
Fix verified against code (`config/schema.py:760`: `ShipMode =
Literal["merge", "ask"]`; `[ship] enabled` boolean, off by
default; `after_approval` unset means `ask`): "ship mode off"
now always means `ship_enabled = false` (sections 2.3, 7.3,
7.4, 10.1 item 4). The section 7.5 incoherence message was
rewritten accordingly ("Shipping is omitted but shipping is on.
Turn shipping off, or include shipping.").

### R6. DESIGNING omission vs. `when_needed` — PROPOSE FIX (applied)

Findings: stage 6 B3 (in part); stage 7 design M11.
Fix: section 7.3 now states the rule exactly. Under
`when_needed` (the default), DESIGNING is in the stage list and
the executor enters it only when the run has UI work
(`needs_design`); non-UI runs record outcome `skipped`. Under
`never`, DESIGNING is omitted from the list, and a handler move
into it halts with a named error (R3). The executed stage list
therefore never contradicts the snapshot stage list: the list
contains DESIGNING unless `designing` is `never`.

### R7. Safety floor vs. the bypasses it names — PROPOSE FIX (applied)

Findings: stage 7 design B2; stage 8 P10 (in part); stage 6 M15.
Fix: section 10.1 is rewritten as a behaviour table with the
bypass rows closed explicitly. Missing ship score: fail-closed
for profiled runs (a ship vote with no score parks in
AWAITING_SHIP instead of shipping); legacy runs keep existing
behaviour and record the missing score as an absent behaviour.
`score_skip_human` in configuration: reduced label, not a
silent rewrite. Plan gate: item 5 now states exactly when it
applies (`require_plan_approval`, `ASK_EVERY_STEP`, VALIDATING
`require`) and that a standard-mode run with plan approval off
does not satisfy it (recorded as absent). Different-vendor
validation: applies only when the repo configures a distinct
validator model, checked against role configuration; otherwise
D3 validation alone satisfies the item, recorded as such.
Item 6: branch = the run's result branch, test record = the
integration-test result stored on the run, quality record = the
latest quality-gate report — all already run-record data, and a
run that cannot produce them does not ship; `[ship]
require_quality_pass` and `[ship] enabled` are named as part of
the mechanism. Manual ship of a `DONE` result: the label travels
through the same writer (R17); whether it should also require
confirmation is ESCALATED (see below).

### R8. Preset table: quality, planner policy, cheapest, autonomy — PROPOSE FIX (applied)

Findings: stage 7 design B3, M9, structural B3, M3, M4, style M7;
stage 6 B8, M3; stage 8 P7; stage 9 codebase M15 (in part).
Fix (section 9.1): Efficiency quality level is now "repo
configuration, never lowered" — the `basic` value is removed,
because `basic` drops secret scanning, SAST, and dependency
audit (those sit at `medium` and above), which is a security cut
presented as efficiency. Accuracy quality is a floor
(`production`, repo level kept when stricter), so Accuracy no
longer lowers a `scalable` repo. Efficiency planner policy is
"repo configuration, never narrowed" — the unconditional
`free_training_ok` is removed, because a preset must not override
a repo's privacy-relevant policy; free-first ordering applies
within the repo's allowed policy. "Cheapest" now has a price
source: `[dispatcher.cost]` configured prices; models with no
configured price are excluded from cheapest claims and listed as
unpriced; a role with no priced model stays at repo
configuration and the picker says so; token counts alone never
prove cheapness, and a Codex `null` cost never counts as zero.
Preset semantics stated once: round caps are maximums, quality
is a floor, planner policy is never narrowed, presets never set
autonomy mode (the table's `standard` rows are replaced by
"creation setting"). The Efficiency stage-list cell now reads
"Canonical list; DESIGNING `when_needed`; no stage omitted".
Preset assurance is stated: both presets `full`. The human gate
is asked to confirm the Efficiency quality change (section 19).

### R9. Round-cap ceilings and precedence — PROPOSE FIX (applied)

Findings: stage 6 M3; stage 7 structural (in B3 context).
Fix: section 7.3 now carries the normative ceiling mapping
(`max_plan_rounds` → `[budget] max_plan_rounds`;
`max_fix_rounds` → `[integration] max_fix_rounds`;
`score_max_retries` → `[quality] score_max_retries`;
`max_task_attempts` has no separate ceiling — it is the
`[budget]` setting itself). A profile cap above the target
repo's ceiling refuses at save (repo scope) or at creation
re-validation (user scope), naming the ceiling — never silently
clamped. This resolves the ceiling-vs-snapshot conflict in
favour of the ceiling, stated explicitly.

### R10. Validator specification — PROPOSE FIX (applied)

Findings: stage 7 structural B5; stage 6 B11; stage 8 N9; stage
9 readiness B6, M4, style B6.
Fix: section 7.5 names the module
(`src/jev_agent/core/workflow_profiles/validator.py`, name
follows option set A) and the single function validating
profile-format JSON. The editor validates drafts; the executor
validates the stored version before resolving, then checks the
resolved snapshot fields against the same schema — one function,
two call sites, not two validators. The live-vs-save split is
enumerated exactly: live = schema parse, unknown names against
current configuration, duplicate stages, empty list; save = the
full catalogue. Phase 2 builds schema plus the live subset;
phase 3 builds the full catalogue and re-checks every phase-2
artefact against it (section 15). Phase 3 is renamed
"Snapshot-aware lookup and shared validator" (the old title
reintroduced the rejected graph interpreter).

### R11. Schemas — PROPOSE FIX (applied)

Findings: stage 6 B6; stage 7 design (in B6 context).
Fix: new section 6.6 gives normative field lists for profile
JSON (required fields marked, including `gate_policies` with
the INTAKE exclusion, `ship_enabled`/`after_approval`,
`learning_profile`, `goal`, `origin`), snapshot JSON
(`resolved_from` map, profile reference, `assurance_absent` in
plain words), and suggestion-record JSON (`dedup_key`,
`evidence_window`, `thresholds_met`, `status`). Preset
versioning contradiction resolved: user/repo versions are
integers from 1; presets are identified by `release` (a string);
a user copy of a preset restarts numbering at 1 under its new
id. The 64 KB cap is kept but restated as a starting value the
implementation may tune with a reason recorded (section 14).

### R12. Storage: travel, atomicity, retention, re-validation — PROPOSE FIX (applied)

Findings: stage 7 design B5, M4, structural M4, M11; stage 6
M10; stage 8 P9; stage 9 codebase M12.
Fix: repo-scope travel corrected (section 6.2) — `.jev/` is
ignored by the repo (`.gitignore` line 13), so repo-scope
profiles do not travel via git by default; sharing is dashboard
export (download JSON) and import (upload JSON), added to phase
5. Files-vs-table justified (reviewable with the checkout,
exportable as one file, readable without the database). Version
creation is atomic per step (temp file + rename for both
`v<n>.json` and `current.json`; crash between steps leaves the
old current version; a version-number collision retries as
n+1). Preset survival corrected (section 6.3): a newer release
ships a newer preset release; old preset files are not retained
as runnable presets — only snapshots on runs keep the old
meaning. Retention: referenced versions are never auto-deleted;
when the last referencing run is deleted, the version becomes
eligible for permanent deletion, and no background cleanup is
specified. Cross-scope re-validation list is now exhaustive
(section 6.5): every section 7.4 parameter, including planner
policy, ship settings, learning-profile existence, and CDA
availability (grading `on` with no backend refuses at creation).
"Archived" vs. "deleted" distinguished (section 6.1): archived
= hidden from creation lists, snapshots still run; deleted =
permanently removed, which a run can never reference because
deletion is refused while referenced.

### R13. Measurement write path and derivation comparison — PROPOSE FIX (applied)

Findings: stage 5 Major 4; stage 6 B7, M18; stage 7 design M5,
structural M7; stage 8 P11.
Fix: section 2.6 now carries the derivation comparison (why a
new table: no stage attribution on attempts/decisions, shared
gates unresolvable from transitions alone, cache/shadow events
carry key hash and kind not stage, ship-decision timing belongs
to no existing record). Section 11.2 now states: the wrapper
wraps one handler call (one state visit); attempt numbers
continue across Halt/resume; halt closes the row (`halted` +
reason); cancel closes rows (`halted`, reason `cancelled`).
Gate attribution by `parked_step`: plan gate → VALIDATING row,
`ASK_EVERY_STEP` executing gate → EXECUTING row, `AWAITING_SHIP`
→ SHIPPING row, score-gate time → INTEGRATING row. No gate
timeout exists today — the clock stops on human action or run
end, stated plainly. Tagging points named (decision write,
attempt write; spanning items carry the starting stage).
Shadow observations tagged with the current stage at observation
time (`log_shadow` gains a stage parameter); unstageable
observations are unattributed. Completeness is a computed flag
(`full` iff no attempt reports `null`); medians run over `full`
rows only, with the full-row count as the sample size; mixed-set
medians are quoted as "at least" with the partial count stated
(section 11.3). New schema fields: `defects_caught` (per-stage
signal: VALIDATING = plan revise rounds, INTEGRATING = fix
tasks, DESIGNING = none — reason lines must say so),
`effective_model`, `effective_level`, `preset_goal`.

### R14. Suggestion engine producers, lifecycle, goal semantics — PROPOSE FIX (applied)

Findings: stage 5 Major 3; stage 6 B12, M7, M16; stage 7 design
B6, M7, M8, structural M5, M6; stage 8 P6, N3, N7, N13; stage 9
readiness M5 (in part).
Fix: evidence producers added (R13 fields). Goal semantics
fixed (sections 5, 12.1): the evidence goal is always the
user-stated goal; a preset's goal label is display-only
(`preset_goal` column) and never counts as stated goal — this
removes the feedback loop in stage 8 N7 (a run that picks
Efficiency without stating a goal records `goal` `null` and
counts as no goal evidence). `custom`-goal runs are evidence
only for `custom` requests. "Completed" defined (`DONE`;
stopped-for-human/failed runs count for failure rates only);
freshness anchored at run end. Stage-drop restricted to
DESIGNING and SHIPPING (the only omittable stages); shortening
(rounds) gets its own threshold with defect signals, and a stage
with a defect signal above zero in the window may not be
recommended for shortening on cost grounds alone. Defect-escape
count defined (gate failures + fix tasks whose origin stage is
the candidate). Attribution rule for cheaper-model/tier: a
gate failure is attributed to a model when the failing code's
producing attempt used that model; CDA tier attribution via the
ledger. Example reason lines rewritten to section 11.3 rules
(window + completeness flag on every line; the "keep
VALIDATING" line now explains why the stage stays, since
dropping it is refused). Approval lifecycle fixed (section
12.4): an identical draft creates no new version (approval of
the existing version is recorded); `origin: suggested` is the
provenance field; discard changes only the record's status (no
new version, no picker entry, no run change); re-offer rule
defined (new evidence = a threshold crossed or a new lesson in
the window, checked at request time; dedup key = from-profile
version plus change set). Phase 6 entry check added: labelled
volume is measured and reported; below-threshold, phase 6 ships
preset-only mode verified plus the usage-based path verified on
fixtures — preset-only alone on empty data does not satisfy
phase 6. Goal-label clock stated (section 19): labelled
evidence starts at phase 4 (presets), not phase 1.

### R15. Creation paths, distinct field, snapshot timing — PROPOSE FIX (applied)

Findings: stage 6 M9; stage 7 structural M1; stage 8 N12; stage
9 codebase B1, B2; readiness M6.
Fix verified against code: production creation funnels through
`app/runs.py create_run` (called by the dashboard runner,
scheduler runner, chat path, MCP path, and CLI run creation);
`Pipeline.start` is the chat-driver/test path. Section 15 phase
2 now hooks the single resolver (`core/workflow_profiles/
resolver.py`) into both entry points — not `pipeline.py` alone.
The distinct field is named: creation parameter
`workflow_profile`, run-record columns
`workflow_profile_id`/`workflow_profile_version`, queue-item
field `workflow_profile` (distinct from the existing `profile`
workspace-profile field and from the run's workspace-profile
snapshot precedent, section 2.4). Snapshot timing rule:
snapshot at creation — queue time for queued runs (the queued
run executes the version it was queued with), per scheduled
creation for recurring items, start time for direct runs.
Chat-started runs resolve `default` (no picker in this design).
`jev workflow` CLI commands are removed from phase 2 into a
later phase with their own acceptance (the CLI is not required
for acceptance). Queue-item staleness is answered: a queue-time
snapshot that predates repo-config changes still runs (it is
immutable); an execution-time existence failure (model removed
after creation) halts the run in `NEEDS_HUMAN` with the setting
named.

### R16. Autonomy inventory — PROPOSE FIX (applied)

Findings: stage 5 Major 5a; stage 6 M2; stage 7 design M2,
structural M9; stage 9 codebase M13.
Fix: section 2.3 now lists all five `AutonomyMode` values
(verified: `core/models.py:68` — STANDARD, AUTONOMOUS,
ASK_EVERY_STEP, MANUAL, MEDIUM) plus the `AUTONOMOUS` opt-in
flag, MANUAL's INTAKE-wait behaviour, and MEDIUM's folder scope.
Conformance matrix adds MANUAL/MEDIUM cases. The section 16 F1
option now states its mode coverage explicitly.

### R17. Assurance computation, label rendering, legacy exception — PROPOSE FIX (applied)

Findings: stage 7 structural B7, M14; stage 8 P10, N1; stage 9
readiness B2; style B5, M12 (in part).
Fix: assurance computation and every label surface are now
owned by phase 2 (resolver computes at creation; run-page and
board-row templates render; `core/ship/pr_body.py` writes the
pull request body on both the dashboard and CLI ship paths —
both paths verified to exist as modules). The legacy
contradiction is resolved as a stated exception, not hidden:
section 8.2 and section 3 now say execution behaviour for runs
without a profile is unchanged while display surfaces gain the
assurance label, and the conformance suite compares label output
as well as stage sequences. Assurance is defined from effective
settings (section 5), covering legacy runs. The label text is
rewritten in plain form ("Reduced checks — the quality gate was
off for this run. Review the changes before merging."), one
display term ("assurance label") everywhere, and the same text
on the run page, board row, and pull request body.

### R18. Conformance executability and the phase-3/phase-5 gate — PROPOSE FIX (applied)

Findings: stage 7 design B4, structural M8; stage 8 P8, N2;
stage 9 readiness B5, M5; style (in B5 context).
Fix: section 8.2 now names the determinism mechanism
(stub/replay harness: Jev decisions and worker outputs stubbed
from recorded fixtures, fixed clock) and the
deliberate-difference registry (a file listing each allowed
difference with its reason; empty for `default`). The single
criterion is identical-under-replay except registry entries —
resolving the section 8.2 vs. phase-3 softening. Phase 3's gate
explicitly covers phase 5: no preset run beyond tests happens
before the conformance suite passes. G1's migration trigger is
named: `[workflows] default_via_profile` (default false), set
only while the suite stays green. "Same scenario" for the
Efficiency comparison is defined (same prompt, repo state,
starting branch, configured models, under the replay harness);
the metric is summed `cost_usd` over measured-`full` stages
plus round counts, with `null`-cost stages excluded by rule and
sample sizes stated, falling back to a static check (resolved
snapshot matches the section 9.1 table) with the reason stated
when no measured-`full` comparison is possible.

### R19. Phase plan: order, entry/exit, files, rollback — PROPOSE FIX (applied)

Findings: stage 5 Major 7, Major 9, Nits 2, 5; stage 8 N3, N4,
N5, N8, N9, N10, N11; stage 9 readiness B1, B3, B4, M1, M2, M3,
M5, M6, M8, M9, M10, style B1, B3, B4, B5, M1, M5, M6, M8, M9,
M10, N1–N8; codebase B4, B5, B7, B10, M1, M2, M3, M4, M5, M6,
M7, M8, M9, M10, M11, M14, M16, N1–N5.
Fix: phase 0 completes before phase 1 (overlap exception
deleted; contract frozen at phase-0 exit); one conformance
suite grown in two phases (skeleton with recorded baselines in
phase 0 under `tests/conformance/baseline/`, comparison in
phase 3); phase-0 deliverables and acceptance stated (contract
document amended by re-audit + skeleton green on the legacy
path). File lists corrected: migration mechanism is
`core/migrations.py` append-only (`PRAGMA user_version`) — the
`core/store/migrations/` directory never existed; preset
package is `src/jev_agent/workflow_profiles/presets/` with an
`importlib.resources` loader (no `data/` directory exists; the
roles package's designer-file pattern is the precedent);
dashboard files follow the existing `routes_<area>.py` and flat
`templates/` patterns, with the pattern named where an exact
filename cannot be fixed before option set A. Phase order
changed: presets + picker move ahead of the editor (new phase
4 = presets, phase 5 = editor), because presets are data that do
not depend on the editor and the picker is the first
user-visible flow choice; section 16 C1's mechanism text was
updated to the new order. Suggestion record storage named
(profile directory `suggestions/`). Every phase now states an
entry rule and an exit check with a threshold or artefact.
"Fails loudly" is defined once (plain message naming the
setting plus a log line; never silent, never a fallback) and
the single failure point for a stale preset is startup
validation (re-checked at creation). Rollback rewritten
(section 17): flag `[workflows] enabled` in `jev.toml`
(default true, meaning selection enabled), ordered steps,
per-path behaviour (queued runs finish as snapshotted;
recurring items create default runs while disabled; drafts stay
visible but not pickable; approved-but-unrun versions stay
stored but unoffered; editor edits disabled with a message,
detail pages readable), per-G-branch behaviour (G1: legacy path
restored for new runs; G2: no legacy path — rollback is
disabling selection plus in-flight snapshots finishing through
the same path, defects fixed forward, stated plainly; G3: as G1
during soak, as G2 after removal), the stated limit that
rollback does not fix a defect in the snapshot-aware lookup code
itself, and rollback tests added to the matrix. Phase-5 Mac
scope fixed: "end to end" defined (to `DONE`, with a shipped
pull request when shipping is on), and Mac verification stated
once as applying to preset acceptance and the section 17 live
checks. G3 gains a removal phase (phase 7, added by re-audit)
with a measured "cleanly" (suite green for the release, zero
profile-path regressions on Mac dashboard runs during soak,
owner: release review). The section 16 C1 mechanism list now
includes phase 0. Option-set sub-decisions completed: A3
(UI-string inventory + error-display mapping, phase-5 work); B1
(refusal/label mapping enumerated, with "Refused:" /
"Reduced checks" distinguishing prefixes); E1 (re-validation at
creation, before spending); F1 (mode coverage, `resolved_from`
map); H3 (tie-break lives as a "priority" row on the preset
table, shown on the picker entry).

### R20. Section 17 conditioned on the gate — PROPOSE FIX (applied)

Findings: stage 6 M14; stage 8 N11; stage 9 readiness B3, M3,
M4, style B3, M3.
Fix: section 17 now carries a per-branch checklist table (C3
replaces editor/suggestion criteria; D2/D3 narrow to the picked
mode, with D3's mouse-free waiver recorded; B2 replaces
label-rendering with refusal verification; H2 replaces the
Accuracy criterion with re-audit-specified speed criteria; G2
replaces the legacy-path criterion with a migration criterion;
G3 adds the phase-7 removal criterion). Every criterion names
its demonstration artefact (screenshots, suite output, stored
records, session log). The live Mac procedure is specified
(start a run, edit its profile while it waits in a gate, resume,
confirm the run page still shows the original version and the
run finishes under it). The test matrix is fixed entry by entry:
validator cases counted separately; conformance fixtures, stub
rule, and expected-output form named; snapshot run-state ×
operation matrix with named resume entry points (approve action,
resume command); preset fixture named (repo with a stated set of
configured models); measurement tolerances stated (exact on
full rows, "at least" on partial rows; known-gate-wait test
procedure); discard rule corrected (only the record's status
changes); editor input-equivalence table defined (drag-out ↔
toggle-off, parameter edit ↔ field edit, identical JSON).

### R21. Wording, terms, and plain language — PROPOSE FIX (applied)

Findings: stage 7 style B1, B2, M1–M14, N1–N12; stage 6 M1, M17,
N1–N7; stage 5 Nits 1, 3, 4; stage 8 (wording half of the
architecture/wording split).
Fix: section 5 now defines the vocabulary once (stage/step/
node/state/gate/flow mapping; gate's three senses; `default`'s
senses distinguished, with the gate-policy value renamed
`inherit`; workspace profile defined; D3/D4/D5/D6 defined with
one line each, D1/D2/D7/D8 declared out of scope rather than
invented; CDA/tier/threshold, lease, checked form, pull request
body, JSON defined in one line each; SimHash/repo fingerprint
and calibration floors explained at first use). The catalogue
(section 7.5) is rewritten in problem/reason/fix form with plain
stage names, and every entry now states its fix (the two
previously fix-less messages now carry one). The label text is
rewritten in plain form (R17). Section 4 headings are
descriptions, not metaphors. "Honest/honesty" as
self-certification is removed everywhere (replaced by the rule
that does the work: sample sizes, completeness flags, "at
least" quoting). "Never silently" instances are replaced by
observable-behaviour statements (what the user sees, not what
the system refrains from doing quietly). The section 7.5
absolute claim is replaced (R21, T24): the editor and executor
run the same validator, so save-time and creation-time passes
use the same rules, and a creation-time failure names the
changed setting. Normative markers: sections 5, 6.4, 7.2, 7.3,
8.1's move table, and 10.1 are marked normative, and the header
states that a restatement never wins over the normative
statement. Semantic-cache self-contradiction fixed (section
2.5): serving exists when explicitly enabled; profiles never
enable it; `exact_off` stops exact serves only and shadow
logging continues. Integration test command fixed: not a profile
parameter anywhere (sections 2.3, 7.4, 14 agree). Learning
profile fixed (section 7.4): naming does not enable learning;
unknown names refuse regardless of the enabled flag. Goal values
fixed (section 5): four values listed, `custom` defined,
preset-vs-stated conflict rule stated (stated goal wins;
preset label is display-only). "A tax only new flows pay"
deleted (the label applies to legacy runs too, R17). "Flow" is
reserved for the stored definition; the drawing and the run use
their own words (section 5). Capitalisation of defined terms
unified. The re-audit rule is defined (section 16 intro: one
pass, fresh reviewers, changed sections only, owner the
Elephant; a second conflict returns to the human gate as an
added option). "Proposed" marking is made consistent by the
header's decided-vs-open list, which names every conditional
section and its option set.

### R22. Contested findings — CONTEST (with evidence)

* "The editor is gold-plating; ship C3 only" (stage 5 Major 2,
  Major 7, Major 9; stage 8 N10). CONTESTED as a unilateral fix:
  the issue explicitly asks for a flowchart editor and
  suggestions, and option set C3 already preserves the
  picker-only build as a human choice. Evidence: the issue scope
  (editor + suggestions are scope items 2 and 4, not optional
  extras) and section 16 C3. The resolution instead corrects
  what the editor can do (the section 3, section 13.2, and
  section 17 wording fixes) and moves presets ahead of the editor
  in the
  plan (R19), so the smallest value lands first without deleting
  the issue's editor from the design.
* "The suggestion engine is a second orchestrator" (stage 7
  design, in B6 context). CONTESTED: the engine runs only on
  request, produces drafts, and never executes; it has no
  decision path during a run and therefore no conflict path with
  a Jev decision. Evidence: section 12.5 as amended ("no
  decision path during a run"). The wording was nevertheless
  strengthened (PROPOSE FIX for the wording half) so the claim
  is checkable.
* "B3 label-only cannot coexist with section 7.3, so B3 must be
  removed" (stage 5 Major 1, stage 7 style B5 in part).
  CONTESTED as a removal demand: B3 is a human option, and the
  coexistence is now defined rather than contradictory — B3
  covers behaviour classes, the five-stage core stays refused
  (R1). The gate decides whether that shape is acceptable; the
  design no longer presents it as a contradiction.

### R23. Declined suggestions — DECLINE (recorded, not fixed)

* "Cut section 12 to preset-only mode and defer the reasoning
  format, cross-repo aggregation, and discard history" (stage 5
  Major 3 fix alternative). DECLINED as the primary fix: the
  thresholds plus preset-only mode already handle thin data, and
  the full engine specification is what the human gate needs to
  judge the engine. The fix instead adds the missing producers
  (R13, R14) and the phase-6 entry check, which answer the
  finding's substance without deleting the specification.
* "Remove per-setting winning-source recording as non-required
  work" (stage 5 Major 7). DECLINED: the run record must show
  the winner, or section 4 item 7 (invisible precedence) is
  unanswered. The fix instead defines its storage cheaply (a map
  inside the snapshot JSON, R2) rather than as separate columns.
* "Drop the canvas mode" (stage 5 Blocker 1 fix alternative).
  DECLINED as a unilateral fix: option set D preserves the
  choice for the human, and the design now states plainly what
  the canvas can and cannot do (the section 3, section 13.2, and
  section 17 wording fixes). Deleting the
  canvas before the gate would pre-pick D2.

### R24. Escalated to the human gate — ESCALATE

* Manual ship of a reduced run: the resolution chose
  label-travels with no additional block (consistent with the
  B1 mix). If the human wants a confirmation gate on hand ships
  of reduced runs, the re-audit adds it to section 10.2. The
  question is recorded in section 19.
* Efficiency's quality promise: the resolution changed the
  Efficiency preset from quality `basic` to "repo configuration,
  never lowered" (R8). The human gate confirms this change at
  approval: Efficiency saves tokens through rounds and priced
  models, not through a lower quality level. Recorded in
  section 19.
* No other finding required evidence outside the files the
  reviewers could read. Every code-grounded claim in the
  codebase-lens report was verified against the worktree before
  fixing (AutonomyMode five values, `ShipMode` merge/ask,
  `app/runs.py create_run` funnel, `core/migrations.py`
  append-only mechanism, `core/ship/pr_body.py` /
  `prepare.py` existence, `.jev/` ignored, no `data/`
  directory, `importlib.resources` precedent in
  `jev_agent.roles`, `QueueItem.profile` field).

## What was not changed

* No option set was picked, ranked, or pre-picked. Every section
  16 set still awaits the human gate, and section 17's
  per-branch checklists state what each pick rewrites.
* No production code, no workflow file, no lint/type/test
  configuration was touched. The only files changed are under
  `docs/designs/profile-workflows/` (design doc, brainstorm
  reports, review reports, this report, SITEMAP).
* No re-audit was used. The reserved re-audit (max 1 per stage)
  still stands for after the human picks the section 16 option
  sets.
* No Mac or live-dashboard change was made. Mac verification
  belongs to the post-approval implementation phase.

## Verification of this resolution

* `design.md` after amendment: 2,337 lines, 19 sections, option
  sets A–H unchanged in number and unpicked.
* Grep checks on the amended doc: zero remaining uses of
  `core/store/migrations/` as a mechanism (one intentional mention
  remains, in the sentence stating that directory does not exist),
  zero of `jev_agent/data/workflow-presets/`, zero of "Graph engine"
  as a phase title, zero of "cannot fail differently", zero of
  "honest" as self-certification; zero remaining bare
  `profile_id` column references (all renamed
  `workflow_profile_id`); zero remaining "ship mode `off`" as a
  value (all translated to `ship_enabled = false`).
* Review reports on disk: 9 reports plus this report plus the
  consolidated stage-7 critic report (`critic_report.md`).
