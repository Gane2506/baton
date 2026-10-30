# Implementation readiness report — Stage 9, style lens

Target: `docs/designs/profile-workflows/design.md` (1,498 lines, branch
`design/profile-workflows`, Baton issue #368), sections 15–19
(lines 996–1498). Earlier sections were read only to check that the
plan matches them.

Standard applied: the implementation-readiness standard in
`elephant-goldfish-model/resources/document_standards/implementation-readiness-standard.md`
— a plan passes only if a builder unfamiliar with the codebase can
follow it on the first attempt without guessing, asking questions,
or making independent design choices (the zero-decision rule) — and
the style persona in `elephant-goldfish-model/agents/style.md`
(plain, direct, measurable wording; no filler, no subjective claims).

## Verdict

Not ready for implementation. The plan in section 15 does not meet
the zero-decision rule: phase boundaries, file lists, and exit
criteria leave choices to the builder; the option sets in section 16
cannot be picked cleanly because each pick invalidates a different
part of sections 6–15 and section 17; several acceptance criteria
cannot be measured as written; several named tests cannot be written
as written; and the rollback paragraph names no executable procedure.

Counts: **6 Blockers, 10 Majors, 8 Nits.**

Findings that were already raised in the stage 8 report
(`review/intermediate_zoom_out_report.md`) and are still present in
the same words are marked as such and cited, not presented as new.
The rest are new to this stage's scope (sections 15–19 read as a
plan, not as a feature description).

## Blockers

### B1 — No phase has a complete entry/exit rule; phase 0 has no exit rule at all

- Sections: §15 introduction (lines 996–1006), Phase 0 (lines 1007–1019).
- Problem: the introduction states a phase does not start until the
  previous phase's acceptance checks pass, then excepts phase 0,
  which "may overlap phase 1". Phase 0's own deliverable is the
  contract (floor table, knob list, stage/gate catalogue) that
  phase 1 measurement is built against. Building phase 1 while its
  contract is still being written means phase 1's exit check cannot
  be run against a fixed contract. Phase 0 lists no acceptance line
  at all: no named output beyond "documents today's behaviour", no
  format, no location, no check that would tell a builder phase 0
  is done. The remaining phases each carry an "Acceptance:"
  paragraph, but none states an entry precondition (what must be
  true before the phase starts, beyond the previous phase passing),
  a test command, or a pass/fail threshold. A builder cannot tell
  when a phase is finished except by judgement.
- Still present; see stage 8 N8, which found the same phase-0
  exception and the undefined skeleton output.
- Fix required: state phase 0's output files and its acceptance
  check; either remove the overlap or state exactly which phase-0
  items are frozen before phase 1 starts and which may still move.

### B2 — No phase builds the safety-floor label, but acceptance requires it

- Sections: §15 phases 1–6 (lines 1020–1137) against §10.3
  (lines 716–734) and §17 acceptance (lines 1382–1404).
- Problem: phase 1 adds run-record columns for assurance
  (`assurance`, `assurance_absent`) and states "no handler logic
  changes in this phase". No later phase names assurance
  computation, board-row rendering, run-page rendering, or PR-body
  writing. Phase 2 covers storage and the validator; phase 3 covers
  step lookup and gate policy; phase 4 covers editor routes and
  templates; phase 5 covers presets; phase 6 covers suggestions.
  The PR-body writer is named in no phase, and labelling of legacy
  runs (which §10.2 says the computation covers) is assigned to no
  phase at all. Section 17 then requires the label on the run page
  and in the PR body for shipped reduced runs, and live Mac
  verification of a reduced run's label rendering. That criterion
  has no owning phase, so a builder following section 15 in order
  reaches the end of phase 6 without ever having built the thing
  section 17 checks.
- Still present; see stage 8 N1.
- Fix required: add the assurance computation and each label
  rendering surface to a named phase, with file names, or remove
  the label criterion from section 17.

### B3 — Option picks are not independent, and section 17 is false under several approved picks

- Sections: §16 introduction (lines 1138–1148) against §17
  (lines 1380–1436).
- Problem: section 16 says sections 6–15 are written for the
  design's proposed answers and that a different pick means
  re-audit before implementation, with "max 1 re-audit per stage".
  That sentence does not make the sets decidable, because the picks
  interact and each one breaks a different acceptance criterion:
  under C3 (no editor, no suggestions) the criteria requiring a
  custom flow "in both editor modes" and an approvable suggestion
  are false by design; under D2 or D3 the "both editor modes"
  criterion is false; under B2 (refuse everything) there is no
  reduced run, so the criterion requiring live verification of a
  reduced run's label rendering cannot be demonstrated; under H2
  (Accuracy optimizes speed) the Accuracy criterion as written
  (production quality, CDA grading on) describes the wrong preset.
  "Max 1 re-audit per stage" is also undefined: the document never
  says what a "stage" is here, who performs the re-audit, or what
  happens if the single re-audit still finds a conflict. A human
  picking options cannot see, from section 16 alone, which parts of
  the document each pick rewrites, and a builder cannot know which
  acceptance checklist applies after the gate.
- Still present in substance; see stage 8 P4 and N11.
- Fix required: either rewrite section 17 as one checklist per
  option branch, or hold section 17 until the gate picks and state
  that the re-audit rewrites it; define the re-audit (owner,
  scope, what happens on a second conflict).

### B4 — Rollback names no flag, no order, and no behaviour under the unpicked G option

- Sections: §17 rollback (lines 1429–1436) against §16 set G
  (lines 1315–1348).
- Problem: rollback is described as "disabling profile selection
  at run creation (a configuration flag, default on)". No flag name
  is given, no file or setting location, no statement of whether
  flipping it needs a restart, and "default on" is ambiguous: it can
  be read as selection enabled by default or as the disabling
  mechanism enabled by default. No order of operations is given:
  disable selection first, or drain the queue first, or stop the
  editor first? No statement covers runs already queued, recurring
  queue items that will create runs later, or chat-started runs.
  No statement covers suggestion records and approved-but-unrun
  draft versions: do they stay visible, stay pickable, get hidden,
  get deleted? The paragraph says in-flight snapshots "do not need
  the editor or the store to keep running", but it does not say
  whether the snapshot-aware lookup code stays live during
  rollback — if that lookup code is the defect being rolled back
  from, letting snapshots finish through it is not a rollback.
  Finally, the paragraph admits its own premise fails under G2
  ("option set G notwithstanding, until it is picked"): under G2
  every run executes through the snapshot-aware path and there is
  no untouched legacy path to return to, so disabling selection
  restores nothing. A rollback paragraph that works only under one
  branch of an unpicked option, and names no flag under that
  branch, is not an executable procedure.
- Still present; see stage 8 N5.
- Fix required: name the flag and its location; state the ordered
  steps; state what happens to queued runs, recurring items,
  drafts, and approved-but-unrun versions; state the rollback
  behaviour under each G branch, including what replaces the
  legacy path under G2, or state that G2 has no rollback and say
  what is done instead.

### B5 — The conformance test and the preset comparison cannot be run as written

- Sections: §15 Phase 3 (lines 1065–1084), §15 Phase 5
  (lines 1102–1118), §17 acceptance (lines 1382–1404),
  §17 test matrix (lines 1405–1428).
- Problem: the conformance test requires the legacy path and the
  `default` preset path to produce identical stage sequences, gate
  parkings, and outcomes, including Halt/resume and cancel cases.
  Pipeline transitions are driven by model decisions (D3 verdicts,
  D6 votes) and worker output, which vary between two live runs.
  The plan never says the suite stubs, replays, or fixes those
  decisions, so the test as written is either flaky by construction
  or requires the builder to invent a stubbing harness the design
  never describes. Phase 3 then softens the criterion to identical
  results "except where the profile deliberately differs, and
  every deliberate difference is named in the test" — but for the
  `default` preset nothing deliberately differs, and for any other
  profile "deliberately differs" has no definition and no named
  author of the naming. Separately, phase 5 and section 17 require
  Efficiency to use "fewer rounds and cheaper models than the
  default on the same scenario", but "the same scenario" is never
  defined: same prompt, same repo, same models, same seed, same
  starting state — none is stated, and model output variance means
  the comparison is not reproducible without fixing those inputs.
  A gate whose pass condition cannot be stated cannot guard a
  migration, and a comparison whose inputs are not fixed cannot be
  measured.
- Still present; see stage 8 P8 and N2.
- Fix required: name the determinism mechanism for the conformance
  suite (stubbed decisions, replayed transcripts, or fixed seeds)
  and define "deliberately differs" (who names a difference, where
  it is recorded); define "same scenario" as a fixed input set.

### B6 — The validator is built in two phases with no stated boundary between them

- Sections: §15 Phase 2 (lines 1041–1064), §15 Phase 3
  (lines 1065–1084), §7.5 (lines 521–550).
- Problem: phase 2 creates "the shared validator (section 7.5
  catalogue)" alongside storage and versioning, and phase 2
  acceptance covers create, version, archive, and delete — none of
  which tests validation strength. Phase 3 then "changes" the
  validator to "the full catalogue". The plan never says what the
  phase-2 validator validates: the schema only, a subset of the
  catalogue, or the full catalogue early. A profile that passes
  phase 2 can therefore fail phase 3, and no step re-checks
  phase-2 artefacts (stored profiles, archived versions) against
  the phase-3 catalogue. The editor in phase 4 is built against
  the phase-3 validator while the store it edits was accepted
  under the phase-2 validator. One validator, built in two phases
  with no boundary, is the same drift the design warns against
  between editor and executor, moved inside the plan itself.
- Still present; see stage 8 N9.
- Fix required: state the phase-2 validator's exact scope
  (enumerated catalogue entries) and add a re-check step for
  phase-2 artefacts under the full catalogue, or build the full
  validator once, in one phase.

## Majors

### M1 — File lists are incomplete, conditional, or missing

- Sections: §15 phases 1–6 (lines 1020–1137).
- Problem: several phase file lists cannot be followed literally.
  Phase 1 lists `src/jev_agent/dashboard/` with "(change, views
  only)" and no file names. Phase 4 lists `src/jev_agent/dashboard/static/`
  with "(new, scoped files only)" and no file names. Phase 2 says
  module names in the new `core/workflow_profiles/` package "follow
  the option-set-A name if it differs" — so the file list itself is
  conditional on an unpicked option. Phase 6 creates
  `core/suggestions/` but never names where suggestion records are
  stored (table, file, or other). Phase 5 lists a dashboard change
  with no file names. Phase 3 lists only `pipeline.py` and
  `validator.py`, although the gate-policy change touches
  `STEP_GATES` helpers and approval mapping elsewhere. Phase 2
  lists `cli/` with no file name, then states the CLI "is not
  required for acceptance". Under the zero-decision rule, a builder
  must invent the missing layout; two builders would invent
  different layouts.
- Fix required: name every file per phase, or mark each unnamed
  group with the rule that determines its names; move
  not-required-for-acceptance work out of a phase whose acceptance
  it does not serve.

### M2 — Several option sets smuggle in a second, undecided decision

- Sections: §16 sets A, B, C, E, F, G, H
  (lines 1149–1379).
- Problems, one per set:
  - A3 (UI-only relabelling, lines 1168–1175): relabelling learning
    profiles as "lesson profiles" and server profiles as "workspace
    settings" in UI text only requires an inventory of every UI
    string, error message, and log line that names the old concepts.
    No inventory is given, and the option's own failure condition
    (an error naming `learning_profiles` that the user cannot map)
    has no stated fix: which errors are renamed, which are mapped
    in display, and who owns the list.
  - B1 (mix, lines 1176–1192): §10.2 names three refusals (quality
    `none` with shipping on, `score_skip_human` with shipping on,
    omitted required stages). Section 10.1 lists six floor
    behaviours. The mapping from the remaining behaviours to
    "refuse" or "label" is not enumerated, so a builder cannot tell
    for each behaviour which enforcement shape applies.
  - C1 (phased, lines 1206–1222): the mechanism list reads
    "measurement, then storage/versioning, then engine/validator,
    then editor, then presets, then suggestions" — it omits phase 0
    contract work entirely, so the option text and section 15's
    0–6 numbering disagree about what the phased plan contains.
  - E1 (both scopes, lines 1259–1273): cross-scope use is
    "re-validated at run start", but the plan never says against
    what (the target repo's full configuration? only models and
    quality levels?) or at what point relative to tokens being
    spent — a refusal after a run has started spending is a
    different behaviour from a refusal before creation.
  - F1 (explicit order table, lines 1284–1300): the option admits
    "the autonomy-mode exception adds a rule that breaks the
    table's simplicity", but the exception rule itself is
    incomplete: it does not cover the `MANUAL` and `MEDIUM`
    autonomy modes that exist in code, and it does not state what
    happens when a profile's gate policy and an explicit human gate
    conflict in each mode.
  - G3 (two-step migration, lines 1339–1348): "after one release in
    which profiled defaults ran cleanly in tests and on the Mac
    dashboard" — "cleanly" has no metric, no soak duration, no
    decision owner, and no phase in section 15 performs the legacy
    path removal. A pick of G3 therefore leaves the removal step
    unplanned.
  - H3 (quality first, speed second, lines 1369–1379): the option
    requires the priority order to be "visible in the preset's
    table", but the §9.1 table has no tie-break column and no row
    for conflicting choices. The builder must invent where the
    tie-break lives.
- Fix required: for each set, either enumerate the sub-decision
  (mapping table, inventory, metric, owner) or state that the pick
  is deferred to the re-audit with a named owner.

### M3 — Acceptance criteria that cannot be measured as written

- Section: §17 acceptance criteria (lines 1382–1404).
- Problems, criterion by criterion, each with the measurement that
  would fix it:
  - "Creates, rearranges, saves, and runs a custom flow in both
    editor modes": "rearranges" cannot be demonstrated, because
    §7.1 refuses every reordering and §13.2 states that in
    practice no reordering passes. The only shape changes the
    editor can make are omitting stages where allowed and editing
    parameters. Fix: define "rearrange" as omit/include plus
    parameter edits, or delete the word from the criterion.
  - "Efficiency measurably fewer rounds and cheaper models":
    fewer rounds is measurable from stage rows; cheaper models is
    not. Stage rows carry tokens and `cost_usd` that is `null`
    when the worker reports no cost (Codex reports none), and no
    price source is named anywhere in the plan. Fix: define the
    metric (for example, summed `cost_usd` over measured-full
    stages only, with sample size stated and null-cost stages
    excluded by rule) and name the price source, or change the
    criterion to a static check (the preset's model names match
    the §9.1 table) and stop calling it measured.
  - "Accuracy full flow with grading on and production quality
    level": "full flow" is undefined. Fix: list the check (stage
    list equals the canonical list, quality level equals
    `production`, CDA grading on, every §10.1 item present).
  - "Runs without a profile produce identical stage sequences and
    outcomes to before the feature": no baseline artefact is named.
    Phase 0 "documents today's behaviour" but gives no format,
    location, retention, or comparison procedure. Fix: name the
    baseline recording and the comparison step, with the tolerance
    for model variance from B5.
  - "A flow that drops a safety behaviour is refused or labelled
    per the approved option set B": the disjunction means the
    verification differs by branch, but no per-branch checklist is
    given. Under B2 there is no reduced run, so the label-rendering
    check cannot run; the criterion never says what is verified
    instead. Fix: write one checklist per B branch.
  - "Verified live on the Mac dashboard, including a reduced-
    assurance run's label rendering and a snapshot surviving a
    profile edit mid-flight": no procedure is given — which
    scenario, which run state at edit time (queued, awaiting a
    gate, executing), what counts as passing for label rendering,
    and what artefact proves it (screenshot, log, test output).
    Fix: give the step list and the pass condition for each live
    check.
- Still present in part; see stage 8 N11 (acceptance unconditioned
  on gate picks).

### M4 — Test-matrix entries that cannot be written as written

- Section: §17 test matrix (lines 1405–1428).
- Problems, entry by entry:
  - Validator tests ("every catalogue entry, including each invalid
    shape and its exact message"): §7.5 lists about eight bullets,
    but several bullets bundle multiple shapes (unknown model,
    quality level, cache mode, or learning profile is four shapes;
    empty stage list, duplicate stage, or missing snapshot fields
    is three). The case count is not enumerated, so coverage cannot
    be checked off.
  - Conformance tests: see B5. The scenario list (plan revise
    loops, fix-task loops, score rework loops, `DESIGNING` on and
    off, ship modes, autonomy modes, Halt and resume, cancel
    mid-run) names loop and mode coverage but no input fixtures,
    no stubbing rule, and no expected-output form.
  - Snapshot tests ("edit, archive, and delete a profile while
    runs reference it; resumed runs keep their snapshot"): no
    matrix of run states (queued, awaiting a gate, executing,
    `NEEDS_HUMAN`) against operations, and no named resume path
    (approval action, resume command, or other). "Resumed runs
    keep their snapshot" is untestable until the resume entry
    point is named.
  - Preset tests ("each preset validates against live
    configuration; a preset referencing a removed model fails
    loudly at validation"): "live configuration" of which repo,
    with which models configured, is unstated, so the test has no
    fixture. "Fails loudly" is also used in phase 5 as "fails
    loudly at startup checks" — two different failure points
    (validation time vs. startup) with no statement of which one
    the test checks, or what "loudly" means (refusal message, log
    line, banner, exit code).
  - Measurement tests ("stage rows sum to recorded totals within
    completeness rules"): no tolerance is stated (exact equality,
    rounding, null handling). "Gate-wait seconds never enter work
    seconds" needs a test with a known gate wait; no procedure for
    producing one is given.
  - Suggestion tests ("discard changes nothing"): false as
    written, because §12.4 keeps the discarded suggestion record
    marked discarded for audit. Fix: enumerate what must not
    change (no new profile version, no picker entry, no run
    change) and state what does change (the record's discarded
    mark).
  - Editor tests ("list mode and canvas mode produce identical
    profiles for the same inputs"): "same inputs" across two
    modalities is undefined — a drag-out in canvas mode and a
    toggle-off in list mode are not the same input event. Fix: an
    input-equivalence table mapping each canvas action to its
    list-mode action.
- Fix required: enumerate cases, name fixtures and entry points,
  define "loudly" once and use it consistently.

### M5 — Phase entry rules do not resolve the phase-3 / phase-5 ordering conflict

- Sections: §15 Phase 3 (lines 1065–1084), §15 Phase 5
  (lines 1102–1118).
- Problem: phase 3 states the conformance suite "must pass before
  any profiled run beyond tests is allowed". Phase 5 acceptance
  requires both presets to "run end to end on the Mac dashboard".
  Presets are profiled runs. The plan never says whether phase 5
  is gated on phase 3 or whether preset runs are exempt from the
  phase-3 gate. Under the section 15 phase rule (a phase does not
  start until the previous phase's acceptance passes), phase 5
  follows phase 4, not phase 3's gate — so the gate's scope is
  ambiguous in exactly the place where it is supposed to protect
  the default flow. A second entry gap: phase 6 acceptance can
  pass on empty data, because below-threshold requests return
  preset-only mode labelled as such. Nothing in section 15
  produces the 10 labelled runs with stage records that §12.3
  requires for a usage-based suggestion, and no phase-6 entry
  check measures whether that volume exists. Phase 6 can therefore
  "pass" without ever demonstrating a usage-based suggestion.
- Still present; see stage 8 N2 and N3.
- Fix required: state whether the phase-3 gate covers phase-5
  preset runs; state phase 6's entry check (minimum labelled-run
  volume, or an explicit statement that preset-only mode alone
  satisfies phase 6).

### M6 — Phase 4 acceptance demands more than the validator catalogue provides

- Sections: §15 Phase 4 (lines 1085–1101), §7.5 (lines 521–550),
  §13.2 (lines 933–959).
- Problem: phase 4 acceptance requires "every catalogue error
  displays with the stage named and the fix stated" and a
  "rearrange" demonstrated in both editor modes. Two catalogue
  messages do not meet that bar: "Gate without a stage is not a
  flow" names no stage and states no fix, and "Unknown stage
  <name>" names the stage but states no fix. The builder must
  invent fix text the catalogue does not contain. And "rearrange"
  in canvas mode, per §13.2, is in practice always a refusal with
  an explanation — the plan never says whether demonstrating that
  refusal counts as passing "rearrange". A test that can pass by
  either succeeding or being refused is not a test.
- Fix required: complete every catalogue message with stage-plus-
  fix text, or narrow phase 4 acceptance to the messages as
  written; state whether a refused reorder satisfies the
  rearrange check.

### M7 — Several option failure conditions are subjective, not checkable

- Section: §16 (lines 1138–1379).
- Problem: each option states a "Fails when" condition, but several
  describe user confusion rather than a checkable failure. A1
  fails "when a user opens `/profiles` expecting workflow profiles
  and picks the wrong kind" — no measurement is given for whether
  that happened (support reports? misclicks? a test?). F1 fails
  "when the table's winner surprises a user" — surprise is not a
  test condition. B1 fails "when a user cannot tell why" one
  behaviour is refused and a similar one is labelled — no
  criterion for "can tell". Options also lack magnitude: C1 vs. C2
  compare "longer to arrive" against "large", with no phase count,
  work-item count, or effort range, so the human picks a phasing
  strategy without any size information the document could have
  supplied from its own phase list.
- Fix required: restate each failure condition as a check (a test,
  a UI rule such as a disambiguation label, or a measured event);
  give each phasing option a size in phases or work items.

### M8 — Rollback leaves queued, recurring, draft, and editor state undefined

- Sections: §17 rollback (lines 1429–1436), §15 Phase 2
  (lines 1041–1064).
- Problem: beyond B4's missing flag, the paragraph never addresses
  state the feature creates. Phase 2 requires the same resolver on
  five creation paths, including queue add and recurring queue
  items. Rollback never says whether a queued run with a profile
  falls back to the default, fails, or runs as snapshotted; whether
  a recurring item keeps creating profiled runs after selection is
  disabled; whether the editor pages stay reachable and editable
  after rollback; or whether suggestion drafts are purged, hidden,
  or left convertible. The test matrix names no rollback test at
  all — no test that flag-off blocks creation, that in-flight
  snapshots finish, or that queued runs behave as stated. A
  rollback with untested, unstated behaviour for four of the five
  creation paths is a paragraph, not a procedure.
- Fix required: state the behaviour per creation path and per
  artefact type (snapshot, draft, approved version, queued run,
  recurring item), and add a rollback test entry to the matrix.

### M9 — Phase steps point at sections that leave the work itself undefined

- Sections: §15 phases 1–3 (lines 1020–1084) against §6.3
  (lines 379–396), §8.1 (lines 553–584), §11.2 (lines 769–785).
- Problems:
  - Phase 2 defers versioning rules to §6.3, but §6.3 defers
    retention to "the repo's existing run-retention behaviour"
    without naming it. If runs are never deleted, no archived
    version can ever be permanently deleted — the builder must
    discover or invent the retention rule.
  - Phase 1 requires "decision/attempt stage tagging at the points
    where decisions and attempts are recorded" without naming
    those points (file or function).
  - Phase 3 requires "the snapshot-aware step lookup and gate-
    policy application from section 8.1", but §8.1 covers only
    the step lookup and `STEP_GATES` helpers. Handler-chosen moves
    (D3 verdicts, D6 votes, score-gate rework, `_integrate`
    moving directly to `AWAITING_SHIP`) and the `approve` gate-to-
    step map have no snapshot check specified, so phase 3's file
    list (`pipeline.py`, `validator.py`) is incomplete for the
    work its own acceptance requires.
  - Phase 4 requires routes "following existing route patterns"
    without naming which existing routes file is the pattern.
- Still present in part; see stage 8 P5 (execution mechanism) and
  P9 (storage claims).
- Fix required: name the retention rule, the tagging points, every
  handler-move call site that needs a snapshot check, and the
  pattern routes file.

### M10 — The re-audit rule is undefined, and "proposed" marking is inconsistent

- Sections: §16 introduction (lines 1138–1148), §6.5 (line 410),
  §9.2 (line 646), §13.1 (line 907).
- Problem: the introduction says the document is amended by
  re-audit when a set picks otherwise, "max 1 re-audit per stage".
  No definition of "stage" is given in this context (EGM review
  stage, implementation phase, or something else), no owner, no
  timebox, and no rule for a second conflict found by the single
  re-audit. Separately, conditional sections are marked
  inconsistently: §6.5 and §9.2 carry "(proposed; option set X may
  narrow/reorder it)" and §13.1 carries "(proposed; final names
  follow option set A)", but §8.2 — which depends entirely on the
  unpicked G set — carries no such marking, nor does §7.3 (which
  §7.3 itself says re-audit revisits first under a label-only B
  pick) or §10.2. A builder cannot tell from the text which
  sentences are fixed and which are conditional, so the scope of
  the re-audit cannot be determined from the document.
- Fix required: define the re-audit (term, owner, scope, failure
  rule) and mark every G-, B-, and F-dependent section as
  proposed, or remove the marking convention and list conditional
  sections in one place.

## Nits

### N1 — Phase 3 title reintroduces a rejected concept

- Sections: §15 Phase 3 (lines 1065–1084) against §8
  (lines 551–599) and §18 (lines 1437–1468).
- Problem: §8 and §18 reject a graph interpreter replacing
  `_advance`. Phase 3 is titled "Graph engine and shared
  validator". The step's content is the snapshot-aware lookup, not
  a graph engine. A future reader will take the phase title as the
  scope.
- Still present; see stage 8 Nit 2.
- Fix: rename the phase to "Snapshot-aware lookup and shared
  validator".

### N2 — Phase 4 hardcodes names that option set A has not picked

- Sections: §15 Phase 4 (lines 1085–1101), §13.1 (lines 907–932).
- Problem: §13.1 states route names follow option set A, but phase
  4 already names `routes_workflows.py` and `templates/workflows/`.
  If A2 ("Flow", route `/flows`) wins, those names are wrong on
  arrival. Phase 2 carries a note that module names follow option
  A; phases 4 and 5 carry no such note.
- Still present; see stage 8 Nit 1.
- Fix: add the same conditional note to phases 4 and 5, or use a
  placeholder name until the gate picks.

### N3 — Phase 0 lists the design document itself as its own deliverable

- Section: §15 Phase 0 (lines 1007–1019).
- Problem: the first bullet names "this document, amended by
  re-audit after the section 16 gate" as phase-0 work. A plan whose
  first step is amending the plan, with no statement of which
  sections the amendment touches, is circular as a work item. It
  also duplicates §19's statement that no implementation work was
  started.
- Fix: list the contract artefacts (floor table, knob list,
  stage/gate catalogue) as separate deliverables with locations,
  and drop the self-reference.

### N4 — Rollback hedges instead of stating a procedure

- Section: §17 rollback (lines 1429–1436).
- Problem: "(option set G notwithstanding, until it is picked)"
  is a hedge inside a procedure. A rollback step that begins by
  conceding its premise may not hold cannot be followed.
- Fix: delete the hedge and write the per-branch rollback (see B4).

### N5 — "Fails loudly" is used with two meanings and no definition

- Sections: §15 Phase 5 (lines 1102–1118), §17 test matrix
  (lines 1405–1428).
- Problem: phase 5 says a stale preset "fails validation loudly at
  startup checks, not at run time", while the test matrix says a
  preset referencing a removed model "fails loudly at validation".
  Validation time and startup time are different points, and
  "loudly" is never defined (refusal message, log line, banner,
  non-zero exit, or all of these).
- Fix: define "fails loudly" once, and state the single point at
  which a stale preset fails.

### N6 — "Demonstrated, not asserted" names no demonstration artefact

- Section: §17 acceptance (lines 1382–1404).
- Problem: the introduction states each criterion "must be
  demonstrated, not asserted", but no criterion names its
  demonstration artefact — test output, screenshot, session log,
  or other. Without that, "demonstrated" is an aspiration, not a
  check.
- Fix: name the artefact per criterion.

### N7 — Option C1's mechanism list omits phase 0

- Section: §16 set C (lines 1206–1235).
- Problem: C1 describes the phased plan as "measurement, then
  storage/versioning, then engine/validator, then editor, then
  presets, then suggestions" — six steps against section 15's
  seven phases (0–6). The contract phase is missing from the
  option the human is asked to pick, so the option text and the
  plan disagree about what "phased" contains.
- Fix: add contract work to C1's list.

### N8 — "End to end" is undefined, and its Mac scope differs between sections

- Sections: §15 Phase 5 (lines 1102–1118), §17 acceptance
  (lines 1382–1404).
- Problem: phase 5 requires both presets to "run end to end on the
  Mac dashboard". "End to end" is never defined: to `DONE`, to a
  shipped PR, to a merge? Section 17's preset criterion does not
  mention the Mac, while its final criterion mentions Mac
  verification generally. A builder cannot tell whether preset
  acceptance requires a Mac run, a shipped result, or both.
- Fix: define the end state and state once where Mac verification
  applies.

## What the plan gets right, stated briefly and without offsetting the above

- Section 15 orders measurement before suggestions, which is the
  correct order for the dependency in §11: no usage-based
  suggestion can ship before stage records exist.
- Phase 2's rule that a creation path that cannot resolve a profile
  refuses with the setting named, and never silently falls back to
  the default, is stated as a checkable rule.
- Section 16 states, for every set, a mechanism, a trade-off, and a
  failure condition, and states plainly that no option is ranked or
  pre-picked. The failures listed above concern completeness and
  interaction of those statements, not their absence.

## What must change before implementation, in order

1. Run the section 16 gate, and rewrite section 17 as one checklist
   per option branch (B3, M3). Until then, no phase acceptance can
   be checked against a stable list.
2. Give every phase an entry rule, an exit check with a threshold
   or artefact, and a complete file list (B1, M1, M9); state the
   phase-2 validator's scope and the re-check of phase-2 artefacts
   (B6); resolve the phase-3/phase-5 gate conflict (M5).
3. Assign the assurance computation and every label surface to a
   named phase with file names (B2).
4. Replace the rollback paragraph with a named flag, ordered steps,
   per-path behaviour, and per-G-branch behaviour, plus a rollback
   test in the matrix (B4, M8).
5. Make the conformance suite deterministic and define "same
   scenario", "deliberately differs", and the Efficiency cost
   metric (B5, M3).
6. Complete the option sets: enumerate the B behaviour mapping,
   the A3 string inventory, the F mode coverage, the G3 removal
   step and its metric, and the H3 tie-break location (M2, M7,
   M10); restate subjective failure conditions as checks.
7. Fix the nits (N1–N8), which are wording fixes and do not
   substitute for items 1–6.
