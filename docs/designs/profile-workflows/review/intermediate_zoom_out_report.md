# Intermediate zoom-out report — Stage 8

Target: `docs/designs/profile-workflows/design.md` (1,498 lines, branch
`design/profile-workflows`, Baton issue #368), read in full.

Stage: STAGE 8 — INTERMEDIATE ZOOM OUT, against the zoom-out standard:
root problem focus, simplicity, YAGNI, reuse, no premature detail, no
repetition. This stage re-examines the full design after the
comprehension stage (stage 6) and the critic stages (stage 7), and
asks what those stages changed at altitude: what they added, what
they mitigated, and what still fails when the whole document is read
together.

Prior reports read first, and used as the baseline for this report:

- `review/zoom_out_report.md` (stage 5)
- `review/comprehension_report.md` (stage 6)
- `review/critic_design_report.md` (stage 7, design lens)
- `review/critic_structural_report.md` (stage 7, structural lens)
- `review/critic_style_report.md` (stage 7, style lens)

Counts: **8 Blockers, 17 Majors, 4 Nits.**

## First finding, and why it frames the rest

The design document is unchanged since stages 5–7. Its history is a
single commit (`c78e7c4`); no amendment answers any stage 6 or stage 7
finding. Stages 6 and 7 therefore added diagnosis and did not mitigate
anything in the text. Every Blocker below that cites a prior report is
still present in the same words, in the same sections. This report does
not repeat those findings as new work: each persisting finding names
the prior report it comes from and states what is still wrong. The new
material is in the second half: what appears only when the whole
document, including section 15 (phasing), section 16 (option sets),
and section 17 (acceptance and rollback), is read together.

At altitude the design is not coherent yet, for one reason that covers
most of the findings: it describes four large mechanisms — an editor,
a preset system, a safety floor, and a suggestion engine — before it
settles the three facts all four depend on: what a profile run
actually executes, what stops an unsafe run, and what data exists to
learn from. Section 16 leaves those facts to a later gate, and
sections 6–15 write detail that assumes answers to them. The phased
plan in section 15 does not repair that order; it builds the dependent
parts in the same order the document describes them, and it leaves the
safety floor itself without a building phase (see N1).

## Persisting findings: still present, still load-bearing

### P1 — Blocker: two different lists of stages that cannot be left out

- Sections: §7.3 (lines 465–492) against §10.2 (lines 693–715).
- Prior reports: stage 5 Major 1; stage 6 B4; stage 7 structural B4;
  stage 7 style B3.
- Still present: §7.3 forbids omitting five stages (`INTAKE`,
  `PLANNING`, `VALIDATING`, `EXECUTING`, `INTEGRATING`). §10.2
  refuses omitting only three (`VALIDATING`, `EXECUTING`,
  `INTEGRATING`). Nothing in the document says what happens to a
  profile that omits `INTAKE` or `PLANNING`: refused under §7.3,
  allowed under §10.2, or handled somewhere else. This is not a
  wording slip. The two sections are the two places an implementer
  would look for the refusal rule, and they give different answers
  for a safety rule.

### P2 — Blocker: the snapshot is defined two ways

- Sections: §5 (lines 309–344), §6.4 (lines 397–409), §8.1 (lines
  553–584), §9.2 (lines 646–668).
- Prior reports: stage 6 B2; stage 7 structural B2; stage 7 style B7.
- Still present: §5 defines the snapshot as the profile version
  frozen onto a run — a reference to a version. §6.4 defines it as
  the full parameter set resolved and stored on the run record — a
  copy of the parameters. §8.1 then adds a third behaviour: values
  are read from the snapshot, falling back to repo configuration
  for anything the profile does not set. A full copy has nothing
  left to fall back to; a partial copy is not the full set §6.4
  promises. §9.2 adds a fourth piece of data, the winning source
  per setting, which appears in no schema in the document. Until
  one definition is picked, the executor cannot be written to "read
  only the snapshot," because the document does not say what the
  snapshot contains.

### P3 — Blocker: the editor cannot rearrange anything, but the goals promise rearrangement

- Sections: §3 (lines 207–245), §7.1 (lines 433–451), §13.2 (lines
  933–959), §17 (lines 1380–1436).
- Prior reports: stage 5 Blocker 1; stage 7 design B8; stage 7 style
  N8.
- Still present: §7.1 refuses every reordering of stages, and §13.2
  admits that in practice no reordering passes. §3 still lists
  "create, rearrange, save, and version" as a goal, and §17 still
  requires a user to "rearrange" a flow in both editor modes. The
  only shape changes the editor can actually make are omitting
  `DESIGNING`, omitting `SHIPPING` by setting ship mode off, and
  editing parameters. That is a settings form with boxes drawn
  around it, not a flowchart editor, and the document's own §2.1
  explains why: the edges live inside handler code, not in a table
  a drawing could express. The goal and the mechanism still
  contradict each other.

### P4 — Blocker: eight unpicked option sets, with detail written as if they were picked

- Sections: §16 (lines 1138–1379) against §6–§15 (lines 345–1137).
- Prior reports: stage 5 Blocker 2; stage 6 B5; stage 7 structural
  M15.
- Still present: none of the eight sets (A naming, B enforcement, C
  phasing, D editor form, E scope, F precedence, G default
  representation, H what Accuracy optimizes) is ranked, marked
  preferred, or pre-picked. Sections 6–15 are written for the
  proposed answers, and the document itself says a different pick
  means re-audit. A pick in any set breaks a different part of the
  detail: G changes whether phase 3 touches every run or only
  profiled runs; H changes whether the §9.1 Accuracy table is the
  right table; B changes §10 enforcement and §17 acceptance; D
  changes phase 4; E changes §6 storage; F changes §9.2; C changes
  §15 outright. The document is still a conditional sketch with
  detailed appendages, not a plan an implementation team can follow
  without first running the gate.

### P5 — Blocker: the execution mechanism does not cover the paths that move a run

- Sections: §7.2 (lines 452–464), §8.1 (lines 553–584).
- Prior reports: stage 6 B1; stage 7 design B1 and M1; stage 7
  structural B6.
- Still present: §8.1 says the step lookup consults the snapshot
  stage list, and gate parking goes through `STEP_GATES` and
  `execution_gate_target` with a snapshot parameter. That covers
  only part of the surface. Handlers move the run themselves to
  hardcoded targets (D3 verdicts, D6 votes, score-gate rework,
  `_integrate` moving directly to `AWAITING_SHIP`), and
  `Pipeline.approve` maps gates back to steps with a static map
  that has no snapshot check. No snapshot check is specified at
  any of those call sites. A profile that omits a stage can still
  have a handler move the run into that stage, and the document
  still names no outcome for that case: error, reroute, or refusal
  at run start. The gate-policy table itself is still missing: no
  mapping of step × autonomy mode × policy to an actual gate
  state, no rule for `INTAKE` (which has no gate state), and no
  rule for whether `require` reimposes a gate on an `AUTONOMOUS`
  run that §10.1 item 5 exempts.

### P6 — Blocker: the suggestion engine is specified in detail before its data exists

- Sections: §11 (lines 735–805), §12 (lines 806–904), §19 (lines
  1469–1498).
- Prior reports: stage 5 Major 3; stage 6 B12; stage 7 design B6;
  stage 7 structural M5.
- Still present: §19 states most repos will see preset-only
  suggestions "for a long time," because learning is off by
  default, CDA grading is off by default and backend-less on the
  Mac, shadow data measures answer agreement rather than
  stage-skip safety, and no run carries a goal label today. §12
  still specifies the full engine on top of that absence:
  thresholds (10 runs, 90-day window, 5 runs per model/tier),
  reasoning records with falsification lines, cross-repo
  aggregation, and discarded-suggestion history. Three mechanisms
  still have no producer in §11: per-stage defect attribution
  (needed for "caught plan defects in 7 of your last 12 runs"),
  a price source (needed for "cheapest configured model"), and
  the effective model and level per stage (needed because per-run
  flags can override the snapshot). The engine's example reason
  lines still cannot be computed from the schema that is supposed
  to feed them.

### P7 — Major: presets are absolute values, and Efficiency cuts checks that are security checks

- Sections: §9.1 (lines 611–645), §9.2 (lines 646–668), §10.1 (lines
  671–692).
- Prior reports: stage 7 design B3; stage 7 structural B3 and M3.
- Still present: Accuracy sets quality `production` absolutely, so
  on a repo already at `scalable` Accuracy lowers strictness while
  being presented as the fullest checking. Efficiency sets quality
  `basic`, which drops secret scanning, SAST, and dependency audit
  (those sit at `medium` and above in the level definitions), while
  §10.1 item 1 still counts the floor as satisfied because "the
  level may vary." The round-cap cuts (plan rounds 3→2, fix rounds
  2→1, score retries 2→1) save nothing on a run that succeeds first
  time, because caps only bind when a run is already failing; on a
  failing run they save tokens by giving up earlier and stopping
  for a human, a cost the document still does not state. The table
  still never says "at most" or "at least," so the implementer
  cannot tell whether the preset value or the repo value wins when
  they conflict.

### P8 — Major: the conformance test cannot pass as written with live model decisions

- Sections: §8.2 (lines 585–599), §15 phase 3 (lines 1065–1084).
- Prior reports: stage 7 design B4; stage 7 structural M8.
- Still present: §8.2 requires the legacy path and the `default`
  preset path to produce identical stage sequences, gate parkings,
  and outcomes, including Halt/resume and cancel cases. Pipeline
  transitions are driven by model decisions (D1 intake, D3 verdict,
  D6 vote) and worker output, which are not deterministic across
  two live runs. The document still never says the suite stubs,
  replays, or fixes those decisions. Without that sentence, the
  acceptance criterion is unimplementable as written: either the
  suite is flaky by construction, or the implementer must invent a
  stubbing harness the design never describes. See also N2, where
  phase 3 softens this criterion in a different direction.

### P9 — Major: storage claims that do not match how the files behave

- Sections: §6.2 (lines 361–378), §6.3 (lines 379–396).
- Prior reports: stage 7 design B5; stage 7 structural M2, M4, M11.
- Still present: §6.2 says repo-scope profiles "travel with the
  repo," but the files live under `<repo>/.jev/`, which the repo
  ignores, so they travel only if someone copies the directory by
  hand — a mechanism the design never names. Version creation is
  still two steps (write `v<n>.json`, then update `current.json`)
  with no locking or atomic rule, so concurrent edits and a crash
  between the steps are still unhandled. Preset versions are still
  said to accumulate "alongside the old one" across Baton upgrades,
  while presets ship as package data that an upgrade replaces; no
  release step is named that would keep old versions. Retention
  still defers to "the repo's existing run-retention behaviour"
  without naming one, so if runs are never deleted, no archived
  version can ever be permanently deleted.

### P10 — Major: the default is both unchanged and newly labelled

- Sections: §3 (lines 207–245), §8.2 (lines 585–599), §10.2 (lines
  693–715), §10.3 (lines 716–734).
- Prior reports: stage 7 design B2 and M3; stage 7 structural B7
  and M14.
- Still present: §8.2 says runs without a profile execute exactly
  today's code path with no behaviour change, and §3 lists changing
  the meaning of any existing setting for runs that do not pick a
  profile as a non-goal. §10.2 says the assurance computation
  applies to every run, profiled or not, and §10.3 puts the label
  on the run page, the board row, and in the PR body for any
  shipped result. A PR-body change on a legacy run is a behaviour
  change on the legacy path. The PR-body writer is still unnamed,
  and the CLI ship path (which pushes a branch and opens a PR) is
  still outside the design's acceptance, so a reduced run shipped
  from the CLI still ships without the label. The label's coverage
  — the point of §4 item 3 — still has a hole on the path the
  design does not cover.

### P11 — Major: measurement picks a new table without the comparison the brief required

- Sections: §2.6 (lines 178–192), §11 (lines 735–805), §15 phase 1
  (lines 1020–1040).
- Prior reports: stage 5 Major 4; stage 7 design M5; stage 7
  structural M7.
- Still present: the brainstorm brief carried "new stage records
  vs. derivation from attempt records" as a decision for the draft.
  The design still picks new stage records (new table, migration,
  `_advance` wrapper, decision/attempt stage tagging) without
  showing why derivation fails, even though attempt records already
  carry start/finish timestamps and cost, decision and quality
  records already exist, and gate-wait time is in principle
  derivable from `AWAITING_*` transitions already recorded. The
  write path is still underspecified at every boundary: whether
  the wrapper covers one loop iteration, one handler call, or one
  state visit; what closes a row on cancel, crash, or
  approve-without-execute; which stage row owns ship-decision
  waiting time (`AWAITING_SHIP` is not a step gate); and how a
  resumed stage is numbered. The §11.1 schema still includes
  fields with no stated producer, such as per-stage
  `semantic_shadow_hits` (shadow observations carry a key hash and
  kind, not a stage).

### P12 — Major: autonomy modes, creation paths, and precedence sources are still incomplete

- Sections: §2.3 (lines 116–140), §9.1 (lines 611–645), §9.2 (lines
  646–668), §13.1 (lines 907–932), §15 phase 2 (lines 1041–1064).
- Prior reports: stage 6 M2, M9, M10; stage 7 structural M1, M9,
  M10.
- Still present: §2.3 lists three autonomy modes, but the code has
  five; `MANUAL` (waits in `INTAKE` for an instruction) and `MEDIUM`
  (folder-scoped) still have no profile behaviour defined, and the
  `AUTONOMOUS` opt-in flag still never appears. Precedence level 3
  still lumps "workspace profile settings" with `jev.toml` without
  defining what a workspace profile is. Phase 2 still requires the
  same resolver on five creation paths (dashboard, CLI, queue add,
  recurring queue items, chat-started runs) while §13.1 specifies
  a picker only for `/runs/new`, and nothing says whether a queued
  run picks a profile at queue time (snapshot can go stale before
  the run starts) or at execution time (the run executes a version
  the user never saw). `plan_only` still conflicts with the
  non-omittable stage rule without a stated resolution: a
  plan-only run never reaches `EXECUTING`/`INTEGRATING`, which
  §7.3 says cannot be omitted.

## Which stage 6/7 findings change the architecture if fixed, and which are wording fixes

Stages 6 and 7 produced two different kinds of finding, and treating
them as one list understates the work. The split below is the
intermediate zoom-out judgement on that question.

Architecture-changing — fixing these changes what is built, not how
it reads. Each one requires a decision plus a change to the mechanism,
and each one blocks implementation of the part that depends on it:

- P1 (one refusal list for non-omittable stages) and the floor's
  runtime effect: whether a reduced run can ship, and what, if
  anything, checks the label at ship time. This is the safety
  mechanism, not the safety wording.
- P2 (one snapshot definition plus a field-by-field map to the
  existing run-record fields) and P5 (snapshot checks at every
  handler move site, in `approve`, and in the ship move). Together
  these are the execution mechanism.
- P6 (evidence producers for the suggestion engine, or cutting the
  engine to preset-only mode) and P11 (the stage-record write path
  and the derivation comparison). Together these are the
  measurement mechanism.
- P7 (presets as floors, caps, or deltas, with the checks each
  preset drops named) and P9 (storage location, atomic versioning,
  preset survival across upgrades). These change preset behaviour
  and storage behaviour.
- P8 (a deterministic conformance harness) and the G decision
  (P4): whether there are one or two execution paths. These change
  the test strategy and the migration risk.
- P3 (drop "rearrange" from the goals, or pick an editor form that
  can actually rearrange) and the D decision (P4): these change
  the scope of the editor build.
- N1, N2, N6 below: phase ownership for assurance, the phase-3 gate,
  and the three overlapping sources of truth.

Wording fixes — fixing these changes how the document reads, and an
implementer could proceed either way, though a dashboard user would
be misled until they are fixed. They do not change the mechanism:

- Term stability: one user-facing name across routes, messages,
  labels, and directory names (stage 7 style B1, M5, M14); one
  meaning each for stage/step/node, gate, flow, and default (style
  B2, M8, M11, M14); one version scheme for user profiles versus
  presets (style B4).
- Plain error and label text: every catalogue message with problem,
  reason, and fix (style M2); a reduced-assurance label a
  non-programmer can act on (style B5); plain headings for §4
  (style N4).
- Single normative statement per rule instead of restatements that
  drift (stage 5 Major 8; stage 7 style M3), including the absolute
  claim in §7.5 that a clean save "cannot fail differently at run
  time" (stage 7 style B6; stage 7 structural M13).
- Small consistencies: the 64 KB cap's basis (stage 7 design N1),
  colon handling in `/workflows/{id}` routes (stage 7 structural
  N1), whether `exact_off` also stops shadow logging (structural
  N2), and the live-vs-save validation split (stage 6 B11).

The wording fixes matter, but they are not a substitute for the
architecture list. A re-audit that fixes only the wording list would
leave every Blocker in place.

## New altitude-level problems: what appears only when the whole document is read together

### N1 — Blocker: section 15 has no phase that builds the safety floor's label

- Sections: §10.3 (lines 716–734) against §15 (lines 996–1137),
  especially phase 1 (lines 1020–1040).
- Evidence: phase 1 adds run-record columns for assurance
  (`profile_id`, `profile_version`, `goal`, `assurance`,
  `assurance_absent`) and a stage-record wrapper, and states "no
  handler logic changes in this phase." No later phase names
  assurance computation: not phase 2 (storage and validator), not
  phase 3 (step lookup and gate policy), not phase 4 (editor
  routes and templates), not phase 5 (presets as data), not phase
  6 (suggestions). Label rendering on the board row and in the PR
  body is likewise assigned to no phase, and legacy-run labelling
  (P10) has no phase at all. The safety floor is the design's
  protection against its own §4 item 3 failure mode, and the plan
  that is supposed to build the feature never builds it. This is
  new at altitude: stages 6 and 7 each examined §10 in isolation
  and found its rules contradictory; reading §15 shows the rules
  are also unowned work — there is no step in the plan where they
  become code.

### N2 — Blocker: the phase-3 gate contradicts the phase-5 acceptance, and the pass criterion changes between §8.2 and §15

- Sections: §8.2 (lines 585–599), §15 phase 3 (lines 1065–1084),
  §15 phase 5 (lines 1102–1118).
- Evidence: phase 3 states the conformance suite "must pass before
  any profiled run beyond tests is allowed." Phase 5 acceptance
  requires both presets to "run end to end on the Mac dashboard."
  Presets are profiled runs. The document never says whether phase
  5 is gated on phase 3, or whether preset runs are exempt from the
  phase-3 gate — the same ambiguity stage 6 B9 found between §8.2
  and phase 5, now visible as a phase-ordering contradiction rather
  than a wording gap. Separately, §8.2 requires "identical stage
  sequences, gate parkings, and outcomes," while phase 3 acceptance
  requires identical results "except where the profile
  deliberately differs, and every deliberate difference is named
  in the test." For the `default` preset, which deliberately differs
  in nothing, the two statements agree; for any other profiled run
  compared against a legacy run, "deliberately differs" has no
  definition, so the suite's pass criterion is undefined. A gate
  that cannot state its own pass condition cannot guard a migration.

### N3 — Major: the measurement-first dependency is really a measurement-plus-labelling dependency, and the labelling clock starts at phase 5, not phase 1

- Sections: §11 (lines 735–805), §12.1 (lines 808–832), §12.3 (lines
  855–875), §15 phases 1, 2, and 5 (lines 1020–1118).
- Evidence: §11 is correctly called a prerequisite phase, and §15
  correctly puts it first. But §12.1 excludes every run whose goal
  label is `null` from goal-conditioned evidence, and §12.3
  requires at least 10 runs for a usage-based suggestion. Goal
  labels arrive with profiles and presets: phase 1 rows for legacy
  runs carry goal `null` by construction, and a preset-assigned
  goal label (see N7) only exists once presets land in phase 5.
  Phase 1 therefore starts no goal-conditioned evidence clock at
  all; the clock for the suggestions phase 6 is supposed to serve
  starts four phases later. The phased plan looks like it resolves
  the measurement-first dependency because stage records come
  first, but the dependency that actually gates suggestions —
  labelled, comparable runs — is not satisfied by stage records
  alone. The plan's phase order hides a four-phase gap between
  "measurement exists" and "measured suggestions are possible."

### N4 — Major: phase-5 acceptance cannot be met by phase-1 data

- Sections: §9.1 (lines 611–645), §11 (lines 735–805), §12.1 (lines
  808–832), §15 phase 5 (lines 1102–1118).
- Evidence: phase 5 acceptance requires Efficiency to "measurably
  use fewer rounds and cheaper models than the default on the same
  scenario (measured by stage rows, not estimated)." Stage rows
  record tokens, time, and cache behaviour. They record no prices.
  §12.1 forbids the engine from reading prices it was not
  configured with, and §9.1 never names a price source for
  "cheapest configured model per role" (P6). Fewer rounds is
  measurable from stage rows; cheaper models is not, because token
  counts do not prove price, and Codex workers report no cost at
  all (§2.6), so their stage rows carry `null` costs. The phase
  that is supposed to prove the preset's value contains an
  acceptance its own measurement phase cannot supply. This is a
  phasing failure, not just a preset-table gap: the plan orders
  measurement before presets as if measurement validated presets,
  but for the model-cost claim it does not.

### N5 — Major: the rollback plan assumes the unpicked G option

- Sections: §16 set G (lines 1315–1348), §17 rollback (lines
  1380–1436).
- Evidence: §17 says rollback is "disabling profile selection at
  run creation (a configuration flag, default on) and letting
  in-flight snapshots finish," and adds "the default path is
  untouched (option set G notwithstanding, until it is picked)."
  Under G2 (default re-expressed as a profile, every run through
  the snapshot-aware path) there is no untouched path to roll back
  to; disabling selection at creation does not restore the legacy
  path, because the legacy path no longer exists. Under G1 the
  rollback flag itself is still undefined (stage 6 M13: no flag
  name, no location, and "default on" is ambiguous between
  selection enabled and disabling enabled). A rollback paragraph
  that is correct only under one branch of an unpicked option, and
  names no flag under that branch, is not a rollback plan. It is a
  sentence that depends on the gate it is supposed to survive.

### N6 — Major: three overlapping sources of truth, built in three phases, with no integration phase

- Sections: §6.4 (lines 397–409), §8.1 (lines 553–584), §9.2 (lines
  646–668), §15 phases 1–3 (lines 1020–1084).
- Evidence: today, handlers read run-record fields directly
  (`_integrate` reads `run.quality_level`, `_finish` reads
  `run.learning_profile`, ship handling reads `run.ship_mode`).
  The design adds a snapshot (phase 2), a snapshot-aware lookup
  (phase 3), and run-record snapshot columns (phase 1), while §8.1
  keeps a fallback to repo configuration for unset parameters and
  §9.2 keeps per-run flags above the snapshot. That is four
  sources — run-record fields, snapshot, repo configuration,
  per-run flags — and no phase reconciles them into one read path.
  Phase 1 says "no handler logic changes," phase 2 says run
  creation resolves a snapshot, phase 3 says the lookup consults
  the snapshot. None says whether a handler that today reads
  `run.quality_level` keeps reading the field, reads the snapshot,
  or reads whichever the precedence table names, and none says
  what happens when the field and the snapshot disagree (stage 7
  structural B2, at plan level). The plan builds each copy in turn
  and never builds the merge rule. At altitude this is the same
  shape as P2, but the phasing makes it worse: the copies land in
  different phases, so the disagreement will be discovered late,
  after the storage and lookup are already built.

### N7 — Major: the goal label is both the preset's assignment and the engine's training signal, which makes the evidence circular

- Sections: §5 (lines 309–344), §9.1 (lines 611–645), §12.1 (lines
  808–832).
- Evidence: §9.1 sets the goal label from the preset
  (`efficiency` for Efficiency, `accuracy` for Accuracy). §5 and
  §12.1 treat the goal label as the user's stated goal and the
  suggestion engine's training signal. A run that picks Efficiency
  is therefore labelled `efficiency` because of the preset, and
  that labelled run is then used as evidence that
  efficiency-goal users benefit from Efficiency-like flows. The
  label records which preset was picked, not what the user wanted;
  a user who picks Efficiency for a deadline, or because it was
  the only preset offered, is counted as an efficiency-goal user.
  Stage 6 M16 found the missing conflict rule (preset label versus
  user-stated goal); the altitude consequence is a feedback loop:
  goal-conditioned suggestions will confirm the presets that
  assigned the labels, and the engine cannot distinguish a user
  goal from a preset default. No threshold in §12.3 corrects for
  this, because the thresholds count labelled runs without asking
  how they were labelled.

### N8 — Major: phase 0 breaks the phase rule it states, and the two conformance suites are still one suite or two

- Sections: §15 introduction and phase 0 (lines 996–1020), §15
  phase 3 (lines 1065–1084).
- Evidence: §15 states "a phase does not start until the previous
  phase's acceptance checks pass, except phase 0's contract work,
  which is documentation plus the conformance suite skeleton and
  may overlap phase 1." A rule stated with its exception in the
  same paragraph is not a rule implementers can schedule against
  (stage 5 Nit 2, still present). The substance matters more than
  the wording: phase 0's contract (floor table, knob list,
  stage/gate catalogue) is the contract phase 1 measurement is
  built against, so the overlap means measurement is built while
  its contract is still being written. Separately, phase 0 creates
  a "conformance suite skeleton" that "documents today's
  behaviour," and phase 3 creates the conformance suite that
  compares legacy against `default` preset paths. The document
  still never says whether these are one suite grown in two
  phases or two suites, what the skeleton contains, or what phase
  0 acceptance requires beyond documenting behaviour (stage 6
  M12). The plan's first step is therefore both exempt from its
  own gate and undefined in its output.

### N9 — Major: the validator is built twice, with the first build's scope undefined

- Sections: §7.5 (lines 521–550), §15 phase 2 (lines 1041–1064),
  §15 phase 3 (lines 1065–1084).
- Evidence: phase 2 creates the shared validator alongside storage
  and versioning, and phase 2 acceptance includes create, version,
  archive, and delete. Phase 3 "changes" the validator to the full
  catalogue. The document never says what the phase-2 validator
  validates — a subset of the catalogue, the schema only, or the
  full catalogue early — so phase-2 acceptance runs against an
  unknown validation strength. Nothing in §15 names a re-check of
  phase-2 artefacts (stored profiles, archived versions) against
  the phase-3 full catalogue, so a profile that passed phase 2
  could fail phase 3 with no named step that catches it. The
  editor in phase 4 is then built against the phase-3 validator,
  while the store it edits was accepted under the phase-2
  validator. One validator, built in two phases with no stated
  boundary between them, is how the two modes the document fears
  (editor versus executor drift, §4 item 2) reappear inside the
  plan itself.

### N10 — Major: at altitude, the plan still builds the speculative parts before the value parts

- Sections: §1 (lines 13–30), §15 phases 1–6 (lines 1020–1137),
  §16 set C (lines 1206–1235), §18 (lines 1437–1468).
- Evidence: the root problem in §1 is that a user cannot ask for a
  cheaper or stricter run. The smallest build that solves it is
  option set C3 (presets plus a picker plus per-run overrides,
  with measurement for reporting), which the document itself calls
  "the smallest build that delivers goal-conditioned flows and
  real cost data." The C1 plan in §15 still orders the work the
  other way: measurement, storage, engine/validator, editor, then
  presets as data in phase 5, then suggestions in phase 6. Presets
  — the part that delivers the §1 value — arrive only after the
  editor is built, even though presets do not depend on the editor
  (they are data in the profile format, §9.1). The plan also still
  carries work the document admits is not required for acceptance:
  `jev workflow` CLI commands in phase 2 ("the CLI is not required
  for acceptance"), five creation paths when the root problem
  needs the run-creation picker, cross-repo suggestions (§12.1),
  discarded-suggestion history (§13.3), and per-setting
  winning-source recording (§9.2). Each is a separate build with
  its own tests, none of which is needed for "pick a cheaper or
  stricter flow and run it." After stages 6 and 7, the case for
  C3-first is stronger, not weaker: every mechanism the full build
  needs (gate table, snapshot map, floor enforcement) is still
  unbuilt, while C3 needs only knobs that already exist (§2.3)
  plus a picker. The plan's order is still backwards against its
  own stated value.

### N11 — Major: §17 acceptance is still unconditioned on the gate picks

- Sections: §16 (lines 1138–1379), §17 (lines 1380–1436).
- Evidence: §17 lists acceptance criteria unconditionally: browser
  creation "in both editor modes," both presets behaving per
  table, suggestions approvable and runnable, reduced-label
  rendering. Under C3 (no editor, no suggestions) the first and
  fourth criteria are false by design; under D2/D3 the "both
  editor modes" criterion is false; under B2 the "refused or
  labelled" criterion collapses to refused-only, so there is no
  reduced run whose label rendering can be verified live; under
  H2 the Accuracy criterion (production quality, CDA on) describes
  the wrong preset. Stage 6 M14 found this; at altitude the point
  is sharper, because §17 is the section the gate is supposed to
  check against. An acceptance section that is false under
  approved options cannot serve as the gate's checklist. It needs
  to be rewritten as one checklist per option branch, or held
  until the gate picks and then rewritten — and §15 gives that
  rewrite no phase.

### N12 — Major: queued and chat-started runs still have no picker, no snapshot timing, and no acceptance test

- Sections: §2.7 (lines 193–206), §13.1 (lines 907–932), §15 phase 2
  (lines 1041–1064), §17 (lines 1380–1436).
- Evidence: phase 2 lists queue add, recurring queue items, and
  chat-started runs as creation paths reusing the same resolver,
  but §13.1 specifies a picker only for `/runs/new`, and §17
  acceptance tests no queued or chat-started path. The snapshot
  timing question from stage 7 structural M1 is therefore still
  open and still untested: a queue-time snapshot can go stale
  against repo config before the run starts and then fail the
  §7.5 run-start re-check after the user believed the run was set;
  an execution-time snapshot means the queued run executes a
  profile version the user never saw. A creation path that can
  silently fall back or silently go stale is exactly the §4 item 7
  failure mode (invisible precedence), and the plan gives it no
  acceptance criterion of its own.

### N13 — Major: the suggestion engine's scope still exceeds what any single phase can verify

- Sections: §12.1 (lines 808–832), §12.3 (lines 855–875), §13.3
  (lines 960–971), §15 phase 6 (lines 1119–1137).
- Evidence: §12.1 allows user-scope suggestions to aggregate across
  the user's repos when asked, §12.3 sets thresholds per scope,
  and §13.3 requires discarded suggestions to stay visible so the
  same suggestion "is not re-offered without new evidence." Phase
  6 tests cover threshold behaviour, approval, and reason-line
  honesty, but no test covers cross-repo aggregation (normalization
  across repos with different configurations and models, or the
  "say so" safeguard's actual display), no test covers the dedup
  rule (what counts as new evidence — one more run, a crossed
  threshold, a new lesson?), and no test covers evidence freshness
  at approval time (a draft approved after new runs arrive is
  approved on stale reason lines; stage 7 design M7). The engine
  is specified as one phase-6 build, but its hardest parts —
  cross-repo comparability, dedup, and staleness — have no stated
  rule and no test. At altitude this is the same pattern as P6,
  extended to the plan: the plan tests the easy thresholds and
  leaves the hard aggregation rules untested.

## Nits

### Nit 1 — Phase 4 hardcodes names that option set A has not picked

- Sections: §13.1 (lines 907–932), §15 phase 4 (lines 1085–1101).
- Prior report: stage 5 Nit 5, still present.
- Evidence: §13.1 says route names follow option set A, but phase
  4 already names `routes_workflows.py` and `templates/workflows/`.
  If A2 ("Flow", route `/flows`) wins, those names are wrong on
  arrival. Phase 2 carries a note that module names follow option
  A; phases 4 and 5 carry no such note.

### Nit 2 — Phase 3 is titled "Graph engine" in a document that rejects a graph interpreter

- Sections: §8 (lines 551–599), §15 phase 3 (lines 1065–1084),
  §18 (lines 1437–1468).
- Evidence: §8 and §18 reject a graph interpreter replacing
  `_advance`. Phase 3 is titled "Graph engine and shared validator."
  The title reintroduces the rejected concept as the name of the
  build step. The step's content is the snapshot-aware lookup, not
  a graph engine; the title should say so, because a future reader
  will take the phase title as the scope.

### Nit 3 — The phase rule's exception is stated in the same breath as the rule

- Sections: §15 introduction (lines 996–1006).
- Prior report: stage 5 Nit 2, still present.
- Evidence: "A phase does not start until the previous phase's
  acceptance checks pass, except phase 0's contract work, which is
  documentation plus the conformance suite skeleton and may overlap
  phase 1." State the overlap as the rule (contract work runs
  alongside phase 1) and drop the absolute form. See N8 for the
  substantive consequence.

### Nit 4 — CLI commands are listed as phase work the document admits are not required

- Sections: §3 (lines 207–245), §15 phase 2 (lines 1041–1064).
- Prior report: stage 5 Major 7, in miniature.
- Evidence: phase 2 includes `jev workflow` commands (list, show,
  validate) and then states "the CLI is not required for
  acceptance." Work that is not required for acceptance should not
  sit in a phase whose acceptance it does not serve; it should be
  a later phase with its own acceptance, or be removed from phase
  2.

## Does the phased plan in section 15 resolve the measurement-first dependency?

Partly, and the part it resolves is not the part that gates value.

What it does resolve: suggestions that change a run come last
(phase 6), after stage records (phase 1). If the plan were followed
literally, no usage-based suggestion could ship before stage records
exist, which is what §11 requires. That ordering is correct as far
as it goes.

What it does not resolve:

1. Presets are not gated on measurement at all in practice. Phase 5
   presets carry the "cheaper models" claim, but phase-1 stage rows
   cannot verify that claim (N4). Measurement before presets does
   not validate presets; it validates only round counts and token
   totals.
2. Goal-conditioned suggestions are gated on labelling, not just on
   stage records, and labelling arrives at phase 5 (N3). The plan
   therefore lets phases 2–4 start — and, under the phase rule,
   complete — on the unmeasured claim that goal-labelled history
   will exist in sufficient volume by phase 6. Nothing in §15
   measures that volume or sets a check for it before phase 6
   starts.
3. The safety floor, which §11 does not cover and §15 does not
   phase (N1), is not gated on anything. It can therefore be
   neither measured nor verified by the plan's own gates.
4. The phase-3 gate that is supposed to protect the default is
   itself ambiguous (N2), so the plan's strongest gate does not
   state its pass condition.

The plan does not let later phases start on unmeasured claims in the
narrow sense — phase 6 waits for phase 1. It lets them start on
unsettled claims in the wider sense that matters: unsettled option
picks (P4), an unsettled snapshot definition (P2), an unsettled
execution mechanism (P5), and an unsettled floor (P1, N1). Phase
gates check the previous phase's acceptance, but no phase's
acceptance checks the option picks, so the gates cannot catch the
dependencies that actually block the feature.

## Zoom-out standard check

- Context sufficiency: passes. §1 states the objective and §2
  grounds the current state in code that matches what was read.
  The document does not fail on context.
- Root problem focus: fails in ordering, not in statement. §1
  states the root problem (a user cannot ask for a cheaper or
  stricter run). The plan answers it last (presets in phase 5)
  and spends phases 2–4 on an editor and a validator whose core
  interaction (reordering) the mechanism forbids.
- Simplicity and YAGNI: fails. The full build (editor with two
  modes, suggestion engine with cross-repo aggregation and discard
  history, five creation paths, CLI commands, per-setting source
  recording) is specified before any evidence that users want more
  than presets plus a picker. Option set C3 is the smaller build
  that answers §1, and the document keeps it as a phasing option
  rather than the first shipped slice.
- Anti-weeds: mixed. The feature range (sections 1–14) already
  contains the full mechanism, and the detailed range (sections
  15–19) repeats it as plan, acceptance, and alternatives. The
  repetition already produces drift (P1's two refusal lists,
  §8.2 versus phase 3's pass criterion in N2). Exact refusal
  strings, a 64 KB cap, and threshold numbers are pinned before
  any data exists to justify them.
- Leverage existing: fails in two places. The profile store uses
  one JSON file per version plus a pointer, while learning profiles
  — the same-shaped concept (named profiles with versions and
  deletion rules) — live in a database table with create, list,
  enable, flush, and delete; the document never justifies the
  different mechanism. The run record already carries quality level,
  ship mode, learning profile, autonomy mode, and plan-only fields
  that handlers read directly; the snapshot duplicates them without
  a merge rule (P2, N6).

## What still stands (short, and stated without resolving the findings above)

- §2 states the pipeline shape, the bypasses, the knob list, and
  the absence of per-stage measurement from code, with file
  references a reader can check. The bypass list in §2.2 is the
  reason P10 can be stated precisely.
- §6.4's snapshot idea — a run executes the version it was created
  with, and later edits cannot change it — answers the §4 item 6
  failure mode (a profile edit corrupting a running run) as far as
  the idea goes. P2, P5, and N6 concern what the snapshot contains
  and how handlers read it, not the idea.
- §11.3's partial-versus-full rule and "at least N tokens" quoting
  rule, and §12.5's prohibitions (no silent application, no shadow
  data presented as serving data, no token number without sample
  size), state constraints the suggestion engine would need. P6,
  P11, and N13 concern the missing fields, thresholds, and tests
  underneath those constraints.
- §13.2's list mode as a first-class path with the same validator
  answers the §4 item 8 failure mode (a drag-only canvas excluding
  users) at the level of mechanism. P3 concerns what that editor
  can actually change, not the list-mode idea.

These do not offset the Blockers: the parts that are stated are the
parts that do not decide safety, execution, or measurement, and the
parts that decide safety, execution, and measurement are the parts
that are missing, contradictory, or — for the safety floor's label —
absent from the plan entirely.

## What must change before implementation, in order

1. Run the §16 gate first, and pick all eight sets or explicitly
   scope implementation to the subset that does not depend on the
   unpicked sets (P4, N11).
2. Fix the architecture list: one refusal list (P1); one snapshot
   definition with a field-by-field map to run-record fields (P2,
   N6); snapshot checks at every handler move site, in `approve`,
   and in the ship move, plus the gate-policy table (P5); the
   runtime effect, if any, of `reduced` (P1, P10); and a phase
   that builds assurance computation and label rendering (N1).
3. Fix the measurement list: the derivation-versus-new-table
   comparison (P11); the stage-record write path for halt, resume,
   cancel, and shared gates (P11); the evidence producers the
   suggestion engine consumes, or cut the engine to preset-only
   mode (P6); and a rule plus a test for cross-repo aggregation,
   dedup, and approval-time staleness (N13).
4. Fix the preset list: presets as floors, caps, or deltas with the
   checks each preset drops named (P7); a price source for
   "cheapest," or drop that word (P6, N4); and preset behaviour
   under `MANUAL`/`MEDIUM` and `plan_only` (P12).
5. Fix the plan list: state the phase-3 gate's pass condition and
   whether it gates phase 5 (N2); state the phase-2 validator's
   scope and the re-check of phase-2 artefacts under the full
   catalogue (N9); give queued and chat-started runs a snapshot
   timing rule and an acceptance test (N12); define the rollback
   flag and its behaviour under each G branch (N5); and move
   presets and the picker ahead of the editor, or adopt C3 as the
   first shipped slice (N10).
6. Re-audit after those changes, since the document itself requires
   re-audit when any option set picks otherwise, and since this
   report's new findings (N1–N13) concern sections 15–17, which
   stages 6 and 7 (in their feature-range scopes) did not cover in
   full.
