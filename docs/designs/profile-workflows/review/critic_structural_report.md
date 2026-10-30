# Structural critic report — Profile-based process flows (Baton issue #368)

- Stage: STAGE 7 — DESIGN CRITIC, structural lens
- Target: `docs/designs/profile-workflows/design.md`, lines 1–995 (sections 1–14)
- Branch: `design/profile-workflows`
- Persona: `elephant-goldfish-model/agents/structural.md`
- Standard: `elephant-goldfish-model/resources/document_standards/feature-spec-standard.md`
- Code checked against: `core/models.py`, `core/pipeline.py`, `core/store/records.py`,
  `core/learning/profiles.py`, `core/cache.py`, `code_orchestrator/ledger.py`,
  `quality/gate.py`, `quality/levels.py`, `config/defaults.py`, `dashboard/app.py`,
  plus the concepts brief. Nothing was modified.

## Verdict first

This range is not buildable as written. It contains 7 blockers, 15 majors, and
5 nits. The blockers are not wording problems. Each one is a place where two
parts of the design say different things about what the code will do, or where
a part the design depends on has no definition at all. The most serious
pattern: the design promises that the executor reads only the snapshot, that
the drawing is the stage list, and that the default path does not change —
and then, in other sections, breaks each of those three promises.

Counts: **Blocker: 7. Major: 15. Nit: 5.**

No merits section follows. The task for this stage is defects, and the
document's own section 4 already lists its failure modes; this report tests
whether sections 5–14 actually close them. In several cases they do not.

## Document-structure check (persona requirement)

Heading hierarchy in lines 1–995: one H1, then H2 sections 1–14 in order,
then H3 subsections in order under their H2. No skipped heading levels were
found. The preset table in section 9.1 has complete rows and columns. These
checks pass; every finding below is about the structure of the proposed
system, not the layout of the document.

Note on scope: section 15 starts at line 996, one line past this review
range, so no finding here covers sections 15–19. Findings below cite only
deferral sentences that appear inside lines 1–995.

---

## Blockers

### B1. Omitting DESIGNING does not omit it — the stage list is not the executed list

- Severity: Blocker
- Sections/lines: section 7.3 (lines 465–492) against section 8.1 (lines 553–584)
- Evidence: Section 7.3 says a profile that omits DESIGNING still runs it
  when the run has UI work, unless the `designing` parameter says `never`
  (the default is `when_needed`). Section 8.1 says "a step not in the
  snapshot is never entered." Both cannot hold. The code makes it worse:
  `_validate` moves the run into DESIGNING when the plan needs one
  (`pipeline.py`, the `_move_to_step(run, RunState.DESIGNING)` call after
  the `plan.needs_design` check). If the snapshot lookup then refuses to
  enter DESIGNING, `_advance` finds no handler for that state and breaks
  out of its loop, leaving the run parked in a work state that is not a
  gate, not a stopped state, and not an error. The run stalls with no
  message, no failure, and no path forward.
- What must change: pick one rule. Either omission removes the step and
  every handler move into it is re-routed, or `designing` is the only
  control and the stage-list omit toggle for DESIGNING is removed. The
  editor must not offer two controls that disagree. The stalled-state case
  needs a named outcome (refusal at run start, not a silent stall).

### B2. The snapshot duplicates fields that already exist on the run record, with no merge rule

- Severity: Blocker
- Sections/lines: section 6.4 (lines 397–409) against `core/store/records.py`
- Evidence: `RunRecord` already carries `quality_level`, `ship_mode`,
  `learning_profile`, `autonomy_mode`, and `plan_only` as fields, and the
  pipeline reads them directly (`_integrate` reads `run.quality_level`;
  `_finish` reads `run.learning_profile`; ship handling reads
  `run.ship_mode`). Section 6.4 adds a second copy — a "resolved
  parameter set" stored on the run record — and section 8.1 says handlers
  read the snapshot, while section 9.2 says the run record "names the
  winning source per setting," which is itself a third piece of data with
  no field defined for it. Nothing says whether the snapshot overwrites
  the existing fields at creation, whether handlers read the fields or
  the snapshot, or what happens when the two disagree (for example, a
  snapshot learning profile that differs from `run.learning_profile`
  when `_finish` runs).
- What must change: a field-by-field map from each snapshot parameter to
  either an existing `RunRecord` field or a new snapshot field, plus the
  rule for which copy wins on read. Until that map exists, "the executor
  reads only the snapshot" is unenforceable, because the executor code
  already reads the fields.

### B3. The preset table contradicts the precedence rule and overrides a privacy setting

- Severity: Blocker
- Sections/lines: section 9.1 table (lines 611–645) against section 9.2
  (lines 646–668)
- Evidence: Section 9.2 says "a profile never turns an `AUTONOMOUS` run
  into a gated run or vice versa." Section 9.1 sets autonomy mode to
  `standard` for both Efficiency and Accuracy. A repo configured
  `AUTONOMOUS` that picks either preset gets gated, which is exactly
  what section 9.2 forbids. Separately, section 9.1 sets Efficiency's
  planner policy to `free_training_ok`, while section 9.1 itself says
  Accuracy "never narrowed" its planner policy from the repo setting.
  A repo that set `mixed_no_training` for privacy reasons gets
  training-allowed runs through the Efficiency preset, with no warning
  named anywhere. The table treats a privacy setting as a cost lever.
- What must change: presets must not set autonomy mode or planner policy
  at all, or the design must state plainly that picking a preset
  overrides both, with the override shown on the picker before the run
  starts. The current text does both and therefore means neither.

### B4. The safety floor contradicts itself in three places

- Severity: Blocker
- Sections/lines: section 7.2 (lines 452–464), section 7.3 (lines 465–492),
  section 10.1 (lines 671–692), section 10.2 (lines 693–715)
- Evidence, three parts:
  1. Section 7.3 forbids omitting `INTAKE`, `PLANNING`, `VALIDATING`,
     `EXECUTING`, `INTEGRATING`. Section 10.2's refusal list covers only
     `VALIDATING`, `EXECUTING`, `INTEGRATING` (plus quality `none` and
     `score_skip_human` with shipping on). Nothing says whether omitting
     `PLANNING` or `INTAKE` is refused, labelled, or something else.
  2. Section 10.1 item 5 requires the plan approval gate "except under
     `AUTONOMOUS` opt-in." Section 7.2 lets a gate policy of `require`
     park a step "even when the mode would not." Nothing says whether
     `require` parks an `AUTONOMOUS` run. If it does, item 5's exception
     is false. If it does not, the `require` policy is false for the
     one mode where it matters most.
  3. Section 7.3 admits that under a label-only floor choice, "this rule
     is the first thing re-audit revisits, because a label-only floor
     with a non-omittable core is the same as the refuse floor." The
     floor's core therefore depends on an option the reviewed range does
     not decide, while sections 7.5 and 10.2 already write refusal
     messages for it. The floor cannot be implemented from this range.
- What must change: one refusal list, used identically in sections 7.3,
  7.5, and 10.2; and one sentence stating what `require` does under
  `AUTONOMOUS`. The "re-audit revisits" escape clause must be replaced
  with the actual rule before implementation.

### B5. The shared validator has no location, no owner, and no file format

- Severity: Blocker
- Sections/lines: section 6.2 (lines 361–378), section 7.5 (lines 521–550),
  section 14 (lines 972–995)
- Evidence: Section 7.5 says "validation runs in one place, shared by the
  editor and the executor." No module path is named. The editor lives in
  the dashboard layer; the executor lives in `core/pipeline.py`. If the
  validator lives in the dashboard, the executor cannot call it without
  importing dashboard code. If it lives in core, the design never says
  so, and never says who owns it when the stage list or the catalogue
  changes. Section 6.2 defines storage as one JSON file per version plus
  a `current.json` pointer, but never defines the JSON itself: no field
  list, no required-versus-optional split, no schema-version field
  inside the file, no statement of what "resolved" means field by field.
  Section 14 adds a 64 KB size cap but no schema, so a file written by
  one Baton release cannot be told apart from a file written by another
  after the format changes. "Validated on read" without a schema version
  is a sentence, not a check.
- What must change: a module path and dependency direction for the
  validator; a complete JSON schema for a profile version file,
  including its own schema version; and the rule for what happens when
  a file's schema version is newer or older than the running Baton.

### B6. Gate parking bypasses the snapshot in two code paths the design never mentions

- Severity: Blocker
- Sections/lines: section 8.1 (lines 553–584) against `core/pipeline.py`
- Evidence: Section 8.1 says gate parking goes "through the existing
  `STEP_GATES` and `execution_gate_target` helpers — the helpers gain a
  snapshot parameter." Two parking paths use neither helper:
  1. `_integrate` moves the run to `AWAITING_SHIP` directly (the ship
     gate), not through `STEP_GATES` (which has no `AWAITING_SHIP`
     entry) and not through `execution_gate_target` (which is
     plan-specific).
  2. `Pipeline.approve` maps gates back to steps with a static dict
     built from `STEP_GATES` plus `AWAITING_SHIP → SHIPPING`, with no
     snapshot check. Approving the gate of an omitted step resumes the
     run into that step, which section 8.1 says can never be entered.
  Section 7.2's per-step gate model also does not cover the real gate
  inventory: 7 work steps, but 8 gate states (`STEP_GATES` covers 6
  steps; `AWAITING_APPROVAL` doubles as the plan gate and the
  `ASK_EVERY_STEP` EXECUTING gate; `AWAITING_SHIP` stands alone).
  `INTAKE` has no gate state at all, so a gate policy on `INTAKE` is
  meaningless, and a gate policy on `EXECUTING` conflates the plan gate
  with a step gate.
- What must change: a gate-by-gate map (all 8 gate states, not "per
  step") stating which snapshot policy controls each, plus snapshot
  checks in the `_integrate` ship move and in `approve`. The helpers
  named in section 8.1 cover only part of the parking surface.

### B7. The legacy path both does and does not change

- Severity: Blocker
- Sections/lines: section 3 (lines 207–245), section 8.2 (lines 585–599),
  section 10.2 (lines 693–715), section 10.3 (lines 716–734)
- Evidence: Section 8.2 says runs without a profile execute "exactly
  today's code path: no snapshot, no new lookup, no behaviour change."
  Section 10.2 says the assurance computation "applies to every run,
  profiled or not," and section 10.3 says the label renders on the run
  page, the board row, and "in the PR body for any shipped result."
  A PR body change on a legacy run is a behaviour change on the legacy
  path, and section 3 lists "the meaning of any existing setting for
  runs that do not pick a profile" as a non-goal change. The design
  also promises in section 3 that the default flow is "unchanged for
  existing runs, proven by a conformance test," while section 10.3
  changes what those runs display and ship. One of these sections is
  wrong.
- What must change: decide whether assurance labelling applies to
  legacy runs. If yes, section 8.2 and section 3 must say so and the
  conformance suite must include label and PR-body output. If no,
  section 10.2 must limit assurance to profiled runs, and the claim
  that the label "is therefore not a tax only new flows pay" must be
  removed.

---

## Majors

### M1. Queued runs have no profile path and no snapshot timing

- Severity: Major
- Sections/lines: section 2.7 (lines 193–206), section 13.1 (lines 907–932)
- Evidence: Section 2.7 notes run creation flows through `Pipeline.start`
  and the queue routes (`/queue`, `/queue/add`) exist in `dashboard/app.py`,
  and there is a chat-queue path (`/chats/{chat_id}/queue`). Section 13.1
  adds a profile picker only to `/runs/new`. Nothing says whether a
  queued run picks a profile at queue time or at execution time. That
  choice decides everything about snapshots: a queue-time snapshot can
  go stale against repo config before the run starts (and then fails
  the section 7.5 run-start re-check after the user believed the run
  was set); an execution-time snapshot means the queued run executes a
  profile version the user never saw. The design is silent on both.
- What must change: a picker and a snapshot-timing rule for queued runs,
  or an explicit statement that queued runs cannot pick profiles.

### M2. Old preset versions cannot survive a Baton upgrade the way section 6.3 requires

- Severity: Major
- Sections/lines: section 5 (lines 309–344), section 6.3 (lines 379–396)
- Evidence: Section 6.3 says upgrading Baton "never changes what a stored
  preset version means; a newer release adds a newer preset version
  alongside the old one." Section 6.2 ships presets as package data
  (`jev_agent/data/workflow-presets/`). A package upgrade replaces
  package data; old preset version files do not accumulate in an
  installed package unless the release process explicitly ships every
  past version forever, which the design never requires. The snapshot
  saves execution (section 6.4 stores the resolved set), but the run
  page display (section 6.4: id, version) and the run-start re-check
  (section 7.5) still need the definition. A run created under preset
  version N, displayed after an upgrade to N+1, has no defined source
  for its definition.
- What must change: either presets are copied into user/repo storage at
  first use so old versions persist, or the design states that only the
  snapshot survives and display/re-check degrade to snapshot-only after
  an upgrade. The "alongside the old one" sentence must match the
  shipping mechanism.

### M3. Efficiency's "cheapest model" is computed at run time, not a fixed preset value

- Severity: Major
- Sections/lines: section 9.1 (lines 611–645) against section 12.1
  (lines 808–832)
- Evidence: Section 9.1 says "the preset itself carries fixed values"
  and defines Efficiency role models as "cheapest configured model per
  role where the repo has one configured." That is not a fixed value;
  it is a computation against live repo config at run-creation time.
  If the cheapest configured model changes between preset definition
  and run creation, the preset's behaviour changes without a version
  change, against section 5's "a preset's meaning never changes
  silently." No price source is named either: section 12.1 says the
  suggestion engine "never reads model prices it was not configured
  with," and `config/defaults.py` holds only dispatcher-leg price
  guesses, not per-role model prices. "Cheapest" by what table is
  undefined, and a repo with no configured model per role falls back
  to an unstated default.
- What must change: define the price source and the resolution time
  (preset-ship time with pinned names, or run-creation time with the
  resolved names recorded in the snapshot and shown on the picker).

### M4. Accuracy may be identical to the default, and the table does not define it

- Severity: Major
- Sections/lines: section 9.1 (lines 611–645)
- Evidence: Most Accuracy rows read "Repo configuration (never narrowed
  by the preset)." The only rows that differ from the default column
  are quality level (`production`) and CDA grading (`on`). If a repo
  already configures `production` and CDA on, Accuracy and default are
  the same flow under two names, and section 9.1 admits this ("if that
  closeness means Accuracy 'just renames the default'"). Section 3
  promises presets that "behave as their tables say"; for Accuracy the
  table mostly says "whatever the repo says," which is not a behaviour.
- What must change: either Accuracy pins values that differ from the
  default regardless of repo config, or the design states that Accuracy
  is a no-op on repos already at that level and the picker says so.

### M5. The suggestion engine's example reason lines cannot be computed from the schema

- Severity: Major
- Sections/lines: section 11.1 (lines 744–768), section 12.2 (lines 833–854),
  section 12.3 (lines 855–875)
- Evidence: Section 12.2's example lines claim "it caught plan defects
  in 7 of your last 12 runs" and "3 of your last 10 runs needed a fix
  task from failed integration tests," and section 12.3 requires a
  "defect-escape count" beside any stage-drop recommendation. The
  section 11.1 stage-row schema has no defect field, no defect-escape
  field, and no routing-change field: it has outcome (`success`,
  `halted`, `failed`, `skipped`), tokens, cache counters, and gate
  waits. A failed integration test produces a fix task inside
  `_integrate`, but nothing in the schema records "this stage caught a
  defect" versus "this stage failed." The engine's core evidence lines
  therefore have no source columns.
- What must change: add the missing fields to the schema (defects
  caught per stage, fix tasks raised per stage, routing changes per
  grade) or rewrite the examples and thresholds to use only fields the
  schema collects.

### M6. Suggestion approval has no re-validation, no draft storage, and no dedup key

- Severity: Major
- Sections/lines: section 12.4 (lines 876–891), section 13.3 (lines 960–971)
- Evidence: A draft is "stored as a suggestion record, not as an active
  profile version," but no storage location, format, or retention rule
  is given for suggestion records. Approval "saves it as a new profile
  version, like any other edit," but nothing re-validates the draft
  against repo config at approval time: a model named in the draft can
  be removed between suggestion and approval, and the approval step
  would either save an invalid version or fail with no named path.
  Section 13.3 says a discarded suggestion "is not re-offered without
  new evidence," but no dedup key is defined (same changes? same
  evidence set? same thresholds?), so "new evidence" cannot be tested.
- What must change: draft storage and format; a re-validation step at
  approval with a named failure path; and a dedup rule for discarded
  suggestions.

### M7. Stage-record mechanics are missing at every recording point

- Severity: Major
- Sections/lines: section 11.2 (lines 769–785), section 11.3 (lines 786–805)
- Evidence, four gaps:
  1. Section 11.2 says "a wrapper around each `_advance` step call" is
     the only writer, but gate waits happen outside step calls: the
     run parks in an `AWAITING_*` state when `_advance` breaks, and
     resumes through `approve`. The wrapper cannot time a wait it
     never sees; the gate clock needs hooks in the state-move and
     approve paths, which are never named.
  2. A stage that parks in a gate and resumes: one row or two? Attempt
     numbering is per stage execution, but a resumed stage is neither
     a new attempt nor a continuation under the stated rules.
  3. Section 11.2 records the ship decision as `SHIPPING` rows, but
     the ship decision (D6 vote, score gate, move to `AWAITING_SHIP`)
     happens inside the `_integrate` handler. Attributing it to
     `SHIPPING` misattributes the work and leaves `_integrate` rows
     understating their own handler's cost.
  4. "Decisions and attempts made during a stage carry the stage
     name" — but Jev decisions and worker attempts are written by the
     dispatcher and workers, which do not receive the current stage.
     No mechanism (parameter, context value, or post-hoc join) is
     named. Section 11.3's median rule is also incomplete: a median
     across runs mixing `measured: full` and `measured: partial` rows
     has no stated rule (exclude partial rows? include as at-least?).
- What must change: a recording-point map covering gate waits, resume,
  ship decision, and decision tagging, plus the median rule for mixed
  completeness.

### M8. The conformance suite cannot pass literally with live models

- Severity: Major
- Sections/lines: section 3 (lines 207–245), section 8.2 (lines 585–599)
- Evidence: Section 8.2 requires "identical stage sequences, gate
  parkings, and outcomes" between the legacy path and the `default`
  preset path, including Halt/resume and cancel cases. Model calls
  are not deterministic: two runs of the same case through the same
  path can produce different plans, scores, and verdicts. The design
  never says the conformance cases use stubbed decisions, recorded
  fixtures, or any other deterministic harness. As written, the suite
  compares two non-deterministic executions and demands identity.
  There is a second problem: after section 8.1's changes, the "legacy
  path" and the "preset path" share the same handlers and differ only
  in the snapshot lookup branch, so the suite compares a branch, not
  two implementations — and if the default is later re-expressed as a
  profile, the guard it was guarding disappears.
- What must change: name the deterministic harness (stubs or fixtures)
  and state what the suite compares after the default is re-expressed.

### M9. Two autonomy modes are missing from the design's mode list

- Severity: Major
- Sections/lines: section 2.3 (lines 116–140) against `core/models.py`
- Evidence: Section 2.3 lists autonomy modes as `standard`,
  `autonomous`, `ask_every_step`. `AutonomyMode` in code has five
  values: those three plus `manual` (only creates the run; waits in
  `INTAKE` for an explicit instruction) and `medium` (folder-scoped
  standard flow). Nothing says what a profile does under `manual`
  (does picking a profile start execution, or does the run still wait
  for instruction?) or under `medium` (does the folder boundary still
  apply, and does the profile's stage list interact with it?). A
  profile picker offered on a `manual` or `medium` run has undefined
  behaviour.
- What must change: profile behaviour under `manual` and `medium`,
  or a rule that profiles cannot be picked for those modes.

### M10. A precedence source is undefined and provenance has no storage

- Severity: Major
- Sections/lines: section 6.4 (lines 397–409), section 9.2 (lines 646–668)
- Evidence: Precedence level 3 is "Repo `jev.toml` and workspace
  profile settings." No section defines "workspace profile settings":
  section 1 notes the word "profile" collides with "workspace/server
  profiles" but never defines what a workspace profile is, where it
  lives, or which keys it holds. Separately, section 9.2 says "the run
  record names the winning source per setting," but `RunRecord` has no
  per-setting provenance field, and section 6.4's snapshot definition
  does not include one. The precedence table is therefore unrecordable
  as specified.
- What must change: define the workspace-profile source or remove it
  from the table, and define the provenance storage (field, format,
  and whether it is part of the snapshot).

### M11. Version creation is not atomic, and retention has no index

- Severity: Major
- Sections/lines: section 6.2 (lines 361–378), section 6.3 (lines 379–396)
- Evidence: Creating version n+1 means writing `v<n+1>.json` and then
  updating `current.json`. A crash between the two leaves the pointer
  stale; two editors saving at once can both write version n+1, and
  the loser overwrites or is lost. No lock, no atomic rename, no
  conflict rule is named. Section 6.3 refuses permanent deletion
  "while any run references any version" and says the refusal "names
  the referencing run count" — but no index from profile version to
  referencing runs is defined, so producing that count means scanning
  every run record, which is never stated. Section 6.3 also defers
  retention to "the repo's existing run-retention behaviour" without
  citing one; if runs are never deleted, archived versions are never
  deletable either, and version files accumulate without bound.
- What must change: an atomic version-creation rule, the version-to-run
  index, and either a cited retention rule or a new one.

### M12. Preset meaning drifts when a level or check changes meaning

- Severity: Major
- Sections/lines: section 5 (lines 309–344), section 6.3 (lines 379–396),
  section 7.4 (lines 493–520), section 9.1 (lines 611–645)
- Evidence: Presets name quality levels (`basic`, `production`), and
  levels are cumulative sets of checks (`quality/levels.py`). When a
  release changes what `production` runs, every stored preset version
  naming `production` changes meaning, against section 6.3's "never
  changes what a stored preset version means." Level names are not
  pinned to check sets anywhere. The same silent drift applies to CDA:
  section 7.4 sets Accuracy's CDA grading `on`, but CDA is advisory
  and records `unavailable` when its backend is missing — so an
  Accuracy run on a machine without the backend is indistinguishable
  from the default, with no warning specified. Section 9's "validated
  at save and at run start" does not catch either case, because the
  references are still valid names with changed or absent effects.
- What must change: pin preset level references to a check-set version
  or state that level-meaning drift is accepted; and name the warning
  when CDA grading is requested but unavailable.

### M13. "Saves cleanly cannot fail differently at run time" is false by the design's own mechanism

- Severity: Major
- Sections/lines: section 7.5 (lines 521–550)
- Evidence: Section 7.5 claims "a flow that saves cleanly cannot fail
  differently at run time for a validation reason." The same section
  requires re-checking the snapshot at run start precisely because "a
  profile edited on disk between save and run start is caught there."
  Repo config can also change between save and run start (a model
  removed, a quality level renamed). Save-time validation uses
  save-time config; run-start validation uses run-time config. They
  can disagree, and the design's own re-check exists because of it.
  The sentence overclaims what the mechanism provides.
- What must change: replace the sentence with the true guarantee:
  save-time validation plus run-start re-validation, with run-start
  failure as a normal, named outcome — which section 13.2 already
  describes.

### M14. The PR-body label has no writer on the CLI ship path

- Severity: Major
- Sections/lines: section 10.3 (lines 716–734)
- Evidence: Section 10.3 says the label appears "in the PR body for any
  shipped result." Shipping happens through `jev ship` (which pushes
  the result branch and runs `gh pr create`) as well as through the
  dashboard. The design names no component that writes the label into
  the PR body, and section 3 excludes CLI work from this design's
  acceptance. A reduced run shipped from the CLI therefore ships
  without the label, and the label's coverage — the whole point of
  section 4 item 3 ("a badge that appears on the editor page but not
  on the shipped PR changes nothing") — has a hole on the path the
  design does not cover.
- What must change: name the PR-body writer for both dashboard and CLI
  ship paths, or limit the section 10.3 claim to dashboard-shipped
  results.

### M15. The reviewed range defers its own decisions to a section outside the range

- Severity: Major
- Sections/lines: section 1 (lines 1–12), section 6.5 (lines 410–422),
  section 9.2 (lines 646–668), section 10.2 (lines 693–715),
  section 13.1 (lines 907–932)
- Evidence: Within lines 1–995, the design defers naming and route
  (option set A), enforcement strictness (B), scope (E), precedence
  order (F), and the default's representation (G), each with "proposed;
  option set X may narrow/reorder it" or equivalent. Those are not
  details; they are the answers to scope, precedence, and enforcement
  — the three mechanisms sections 6, 9, and 10 are built on. The
  document itself gates implementation on the human approval gate, so
  this is not a surprise, but it means sections 1–14 cannot be handed
  to an implementer as a buildable spec: every one of those sections
  needs re-audit once the options are picked, and the design says so
  in at least three places. That re-audit cost should be planned, not
  discovered.
- What must change: nothing in this range alone fixes this; the
  approval gate must pick the options first, and sections 6, 9, 10,
  and 13 must then be re-checked against the picks before any phase
  starts.

---

## Nits

### N1. Profile ids contain a colon, but routes use the id in the path

- Severity: Nit
- Sections/lines: section 6.1 (lines 347–360), section 13.1 (lines 907–932)
- Evidence: Id format is `<scope>:<slug>` (for example `repo:fast`), and
  the detail route is `/workflows/{id}`. A colon in a path segment needs
  an encoding rule, or ids in URLs need a different form. The design
  never states one.
- Fix: state the URL encoding or use a path-safe id form in routes.

### N2. `exact_off` cache mode leaves shadow logging undefined

- Severity: Nit
- Sections/lines: section 7.4 (lines 493–520), section 11.1 (lines 744–768)
- Evidence: Cache mode is `exact` or `exact_off`, but nothing says
  whether `exact_off` also stops shadow observation. A stage row with
  zero cache hits under `exact_off` is indistinguishable from a stage
  row with zero hits under `exact`, which weakens the suggestion
  engine's cache evidence.
- Fix: one sentence stating whether shadow logging runs under
  `exact_off`.

### N3. The invalid-graph catalogue mixes construction guarantees with file errors

- Severity: Nit
- Sections/lines: section 7.5 (lines 521–550), section 13.2 (lines 933–959)
- Evidence: "Gate orphan" is listed as a catalogue item and then
  explained as unreachable by construction, with a file carrying a
  gate without its step downgraded to a schema error. It is not a
  graph shape the validator checks; it is a schema violation. Section
  13.2 also promises live feedback "where cheap" without saying which
  checks are cheap, so implementers will guess differently.
- Fix: move gate-without-stage to the schema-error list, and name the
  live checks.

### N4. Section 3 and section 10.2 disagree about whether the floor is already decided

- Severity: Nit
- Sections/lines: section 3 (lines 207–245), section 10.2 (lines 693–715)
- Evidence: Section 3 says a dropped safety behaviour "is either
  refused at save time or visibly labelled (option set B decides
  which, per behaviour class)." Section 10.2 already refuses three
  cases and labels the rest. Either B decides, or section 10.2
  decides; the document says both.
- Fix: align section 3's wording with section 10.2, or move the three
  refusals behind the option.

### N5. Scope boundary

- Severity: Nit
- Sections/lines: section 14 (lines 972–995), section 15 header (line 996)
- Evidence: This review covers lines 1–995. Section 14 ends at line 995
  and section 15 starts at line 996, so the boundary falls cleanly
  between sections. No finding in this report depends on lines past 995.
- Fix: none; recorded so the next reviewer does not re-litigate the cut.

---

## What the next draft must contain before implementation

1. The DESIGNING omission rule, fixed to one rule (B1).
2. The snapshot-to-`RunRecord` field map and merge rule (B2).
3. Preset rows that do not override autonomy mode or planner policy,
   or an explicit override warning on the picker (B3).
4. One refusal list used in sections 7.3, 7.5, and 10.2, plus the
   `require`-under-`AUTONOMOUS` sentence (B4).
5. The validator's module path and the profile JSON schema with its
   schema version (B5).
6. The full 8-gate map and snapshot checks in the ship move and in
   `approve` (B6).
7. A decision on whether assurance labelling applies to legacy runs (B7).
8. Queued-run profile handling, preset-version survival across upgrades,
   and the stage-record recording-point map (M1, M2, M7).
9. The missing suggestion-evidence fields or rewritten reason lines (M5).
10. The approval-gate option picks, followed by re-audit of sections 6,
    9, 10, and 13 (M15).
