# Zoom-out review — Stage 5 (initial zoom out)

Target: `docs/designs/profile-workflows/design.md` (1,498 lines, Baton issue #368),
read in full, checked against the code files named in the review assignment
and against `docs/designs/profile-workflows/concepts_brief.md`.

Stage: STAGE 5 — INITIAL ZOOM OUT, against the zoom-out standard:
root problem focus, simplicity, YAGNI, reuse, no premature detail, no repetition.

Counts: **2 Blockers, 9 Majors, 5 Nits.**

Context check first, because the standard requires it: the objective and the
reason for it are stated clearly enough to review (§1, §2). The document does
not fail on context. Everything below is about scope, mechanism, and fit.

---

## Blockers

### Blocker 1 — The flowchart editor cannot rearrange anything, but the goals promise rearrangement

- Sections: §3 (lines 207–245), §7.1 (lines 433–451), §13.2 (lines 933–959),
  §17 acceptance (lines 1380–1436).
- Evidence in the doc: §7.1 says a profile's stage list is a subsequence of
  the canonical order and "Reordering two stages is refused." §13.2 says the
  canvas user "reorders only within what the validator accepts (in practice:
  no reordering passes, and the editor says why when one is attempted)."
  §3 still lists as a goal that a user can "create, rearrange, save, and
  version a workflow profile entirely from the browser," and §17 acceptance
  still requires a user to "rearrange" a flow in both editor modes.
- Why this matters: the only things the editor can actually change about the
  shape of a run are omitting DESIGNING (conditional today anyway), omitting
  SHIPPING by setting ship mode off, and editing parameters. That is a
  settings form with boxes drawn around it, not a flowchart editor, and the
  document's own §2.1 explains why: there is no transition table, because
  the edges live inside handler code (`core/pipeline.py`, `_advance` at line
  361, handler map at lines 362–370; `_validate` and `_integrate` choose the
  next state internally). A goal that the mechanism forbids is not a goal;
  it is a mislabelled deliverable.
- What would fix it: either drop "rearrange" from §3 and §17 and describe
  the editor as what it is (stage omission plus parameter editing), or pick
  option set D2 (list only) or C3 (picker and toggles, no graph editor) and
  remove the canvas. Do not ship a canvas mode whose only reorder interaction
  is a refusal message.

### Blocker 2 — Eight unpicked option sets mean the detailed design cannot be implemented as written

- Sections: §16 (lines 1138–1379) versus §6–§15 (lines 345–1137).
- Evidence in the doc: §16 states none of the eight sets (A naming, B
  enforcement, C phasing, D editor form, E scope, F precedence, G default
  representation, H what Accuracy optimizes) is ranked, marked preferred,
  or pre-picked, and that "where a set picks otherwise, the document is
  amended by re-audit before implementation." §6.5, §9.2, §10.2, and §13.1
  are each explicitly marked "proposed" and conditional on their set.
- Why this matters: the 500 lines of DetailedRange (lines 996–end: file
  layouts, module names, route names, phase order, acceptance tests) all
  assume one branch of each set. A different pick in any set breaks a
  different part of the detail: G changes whether phase 3 touches every run
  or only profiled runs; H changes whether the §9.1 Accuracy table is even
  the right table; B changes §10 enforcement and §17 acceptance; D changes
  phase 4; E changes §6 storage; F changes §9.2; C changes §15 outright.
  The document is therefore a conditional sketch with detailed appendages,
  not a plan an implementation team can follow. The re-audit the document
  promises is not a small edit; it is a rewrite of the affected sections.
- What would fix it: run the §16 gate first, then cut §15 and §17 down to
  the picked branches only. Until the gate is run, §15 should not name
  files, routes, or module paths that depend on unpicked sets (see Nit 5).

---

## Majors

### Major 1 — The safety floor's enforcement is split across sections that do not agree, and the label branch does not protect

- Sections: §4 item 3 (lines 246–308), §7.3 (lines 465–492), §10.2 (lines
  693–715), §10.3 (lines 716–734), §16 set B (lines 1176–1205).
- Evidence: §7.3 forbids omitting five stages (INTAKE, PLANNING, VALIDATING,
  EXECUTING, INTEGRATING). §10.2 lists refusals for omitting only three
  (VALIDATING, EXECUTING, INTEGRATING) plus quality `none` with shipping on
  and `score_skip_human` with shipping on. The INTAKE/PLANNING refusal lives
  only in §7.3/§7.5, so there are two different lists of "refused" shapes
  in a safety-critical rule. The document itself notes the deeper clash:
  §7.3 says if option set B picks label-only, "this rule is the first thing
  re-audit revisits, because a label-only floor with a non-omittable core
  is the same as the refuse floor for these five stages" — meaning B3 as
  offered cannot coexist with §7.3. And §10.3 states "A reduced run is not
  blocked from shipping by the label alone," so under the label branch the
  answer to §4's failure mode 3 ("the label decorates instead of
  protecting") is that the label travels to the run page, the board row,
  and the PR body — visibility, not prevention. A reviewer who misses the
  label still merges the reduced run.
- What would fix it: one refusal list in one place, referenced from §7 and
  §10 instead of restated; a B3 option rewritten to state plainly whether
  label-without-blocking is accepted risk for shipped code, or refusal for
  every ship-affecting drop with labelling reserved for non-shipping
  reductions.

### Major 2 — Presets re-bundle knobs that already exist, and the document never shows what they add over option C3

- Sections: §2.3 (lines 116–140), §9.1 (lines 611–645), §16 set C (lines
  1206–1235), §18 (lines 1437–1468).
- Evidence: every Efficiency/Accuracy value in §9.1 is an existing setting
  from `config/defaults.py` (quality level, `score_threshold` 85,
  `score_max_retries`, `max_plan_rounds`, `max_fix_rounds`, planner policy,
  cache mode, autonomy mode, ship mode). The document itself says Accuracy
  "is close to the default flow plus CDA grading and a production quality
  level" and that option set H exists because of that closeness. §16 set C3
  (picker and toggles only, no graph editor, no suggestions) is described
  as "the smallest build that delivers goal-conditioned flows and real cost
  data," and §18 keeps it as a phasing option. The root problem in §1 — a
  user cannot ask for a cheaper or stricter run — is solved by C3 alone.
  The remaining build (profile store, versioning, validator, editor,
  suggestion engine) is justified only by the editor and the suggestions,
  which are the two weakest parts of the design (Blocker 1; Major 3).
- What would fix it: a short passage stating what a user cannot do today
  with `jev.toml` plus per-run flags that presets add (naming, versioning,
  snapshot, picker), and why that gap is worth the full build rather than
  shipping C3 first and measuring whether anyone asks for more.

### Major 3 — The suggestion engine is specified in detail for data that does not exist and may not arrive for a long time

- Sections: §11 (lines 735–805), §12 (lines 806–904), §19 (lines 1469–1498).
- Evidence: §19 states most repos will see preset-only suggestions "for a
  long time" because learning is off by default, CDA grading is off by
  default and backend-less on the Mac (`code_orchestrator/ledger.py`,
  `unavailable_record` at line 285, `best_tier` at line 376 requiring 3
  graded diffs), shadow data measures answer agreement rather than
  stage-skip safety (`core/cache.py`, `similar()` at line 600, `log_shadow`
  at line 636), and no run carries a goal label today. Yet §12 specifies a
  full engine: thresholds (10 runs, 90-day window, 5 runs per model/tier),
  reasoning records with falsification lines, cross-repo aggregation,
  suggestion records with approve/discard lifecycle, and discarded
  suggestion history to avoid re-offering. Three of its mechanisms have no
  producer in §11: (a) §12.2's example "it caught plan defects in 7 of your
  last 12 runs" needs per-stage defect attribution, but the §11.1 schema
  has only outcome (`success`/`halted`/`failed`/`skipped`) plus halt reason
  — no field saying which stage caught a defect; (b) stage-drop
  recommendations (§12.3) can only concern DESIGNING and SHIPPING, because
  §7.3 forbids dropping the other five stages, so the elaborate stage-drop
  threshold governs at most two stages, one of which is conditional anyway;
  (c) "cheapest configured model per role" (§9.1) has no price source —
  §12.1 says the engine never reads prices it was not configured with, and
  §2.6 says Codex reports no cost, so "cheapest" is undefined for exactly
  the workers whose costs are unknown. Goal-labelled training data also
  starts at zero on launch day, since no historical run has a goal label;
  the document never states that lead time.
- What would fix it: cut §12 to preset-only mode plus the threshold
  definitions, and defer the reasoning-record format, cross-repo
  aggregation, and discard history until stage records and goal labels
  have produced real data. Add the missing producers (defect attribution,
  price source) or drop the example reason lines that depend on them.

### Major 4 — Measurement picks a new table without the comparison the brainstorm brief required

- Sections: §2.6 (lines 178–192), §11 (lines 735–805), §15 phase 1 (lines
  1020–1040).
- Evidence: the brief carried "new stage records vs. derivation from
  attempt records" as a decision for the draft. The design picks new
  stage records (new table, migration, `_advance` wrapper, decision/attempt
  stage tagging) without showing why derivation fails. Existing records
  already carry much of this: `core/store/attempts.py` records attempt
  start/finish with timestamps and `cost_usd`; `core/store/records.py`
  defines `AttemptRecord`, `DecisionRecord`, `QualityReportRecord`;
  `core/store/events.py` emits attempt started/finished payloads; gate
  transitions already pass through `store.set_run_state` in
  `core/pipeline.py`. Gate-wait time, the one quantity the design says is
  "mixed into wall time" today, is in principle derivable from
  AWAITING_* state transitions already recorded. The §11.1 schema also
  includes fields with no stated producer, such as `semantic_shadow_hits`
  per stage (shadow observations in `cache.py` carry a key hash and kind,
  not a stage).
- What would fix it: a short derivation-vs-new-table comparison stating
  which §11.1 fields cannot be derived from existing attempt, decision,
  quality, and event records, and why. If derivation covers most fields,
  the phase 1 build shrinks to a view over existing records plus the
  genuinely new fields.

### Major 5 — The inventory of what profiles must coexist with is incomplete

- Sections: §2.3 (lines 116–140), §2.4 (lines 141–157), §6 (lines 345–422),
  §9.2 (lines 646–668), §15 phase 2 (lines 1041–1064).
- Evidence, checked against code:
  (a) Autonomy modes: §2.3 lists three modes (`standard`, `autonomous`,
  `ask_every_step`), but `core/models.py` `AutonomyMode` (line 68) has
  five: STANDARD, AUTONOMOUS, ASK_EVERY_STEP, MANUAL (chat-driven, waits
  in INTAKE for an instruction), and MEDIUM (folder-scoped). Profile
  behaviour for MANUAL and MEDIUM runs is never defined, and §7.2 gate
  policy cannot express MANUAL behaviour because `STEP_GATES`
  (`core/pipeline.py`, line 118) has no INTAKE entry.
  (b) Name collisions: the header and §2.4 count two existing uses of
  "profile" (learning profiles, workspace/server profiles). The brief
  counts three, adding `QueueItem.profile`, and `core/store/queue.py`
  confirms a `profile` column on queue items. Phase 2 requires the same
  resolver on five creation paths including queue add, but the design
  never says how the new profile relates to the queue item's existing
  `profile` field.
  (c) Snapshot precedent: `Pipeline.start` already snapshots workspace
  profiles (`servers/profiles.py`, `snapshot` at line 59,
  `write_run_profile` at line 133, copied to `.jev/runs/<id>/profile.json`;
  `core/pipeline.py` line 221). The design's §6.4 snapshot (resolved
  parameter set on the run record) never states its relationship to that
  existing run-profile snapshot — two snapshot systems, one name.
  §9.2 tier 3 then merges "repo `jev.toml` and workspace profile
  settings" into a single precedence tier, blurring a config file and a
  named, snapshotted profile into one thing.
- What would fix it: complete the inventory (five autonomy modes, three
  profile collisions, existing run-profile snapshot) and state, for each,
  whether profiles interact with it, ignore it, or replace it.

### Major 6 — The shared-validator guarantee is overstated, and the default-path choice is a dead end either way

- Sections: §7.5 (lines 521–550), §8.1–§8.2 (lines 553–599), §16 set G
  (lines 1315–1348).
- Evidence: §7.5 claims "A flow that saves cleanly cannot fail differently
  at run time for a validation reason." The validator checks static shape
  (stage order, known models, known levels). Run-time behaviour depends on
  things the validator cannot see: D3 verdicts inside `_validate`, D6 votes
  and the score gate inside `_integrate`/`_ship_after_score_gate`
  (`core/pipeline.py`, line 643), conditional DESIGNING/SHIPPING, budget
  caps, and Halt conditions. A saved-clean flow can still halt, take a
  different branch, or stop for a human in ways the drawing never showed —
  which is §4's failure mode 1 restated, not answered. Separately, §15
  phase 3 says the legacy path is "untouched code, not a flag," while
  §8.1 says the existing `STEP_GATES` and `execution_gate_target` helpers
  "gain a snapshot parameter" — helpers shared by both paths, so the
  legacy path is touched. And set G offers only two bad ends the document
  itself names: G1 maintains two paths with a conformance suite that must
  keep proving they agree as handlers change, and G2 moves every existing
  run onto a new path at once.
- What would fix it: narrow the §7.5 claim to what the validator actually
  guarantees (static shape only, with run-time divergence named as
  expected); resolve the shared-helper contradiction; and state which G
  branch is the default if the gate deadlocks, since the implementation
  cannot proceed without it.

### Major 7 — The implementation plan carries work the document admits is not required for acceptance

- Sections: §15 (lines 996–1137), §17 (lines 1380–1436).
- Evidence: phase 2 includes `jev workflow` CLI commands (list, show,
  validate) and then states "the CLI is not required for acceptance."
  Phase 2 also requires the same resolver on five creation paths
  (dashboard, CLI, queue add, recurring queue items, chat-started runs)
  when the root problem only needs the run-creation picker. §12.1 adds
  cross-repo suggestions; §13.3 adds discarded-suggestion history;
  §9.2 adds per-setting winning-source recording; phase 1 adds Spend page
  aggregation. Each is a separate build with its own tests, none of which
  is needed for "pick a cheaper or stricter flow and run it."
- What would fix it: remove the CLI commands and the non-dashboard creation
  paths from phase 2 (or mark them as later phases with their own
  acceptance), and defer cross-repo suggestions, discard history, and
  per-setting source recording out of the first implementation.

### Major 8 — Repetition: the same rules are restated in four to six places, and the restatements already drift

- Sections: passim; snapshot (§4 item 6, §5, §6.3, §6.4, §8.3, §15
  phases 2–3, §17), assurance travel (§4 item 3, §10.3, §13.1, §17),
  validator sharing (§4 items 1–2, §7.5, §8, §13.2, phase 3), semantic
  serving stays off (§2.5, §3, §7.4, §9.1, §12.5, §19), default unchanged
  (§3, §8.2, §15 phases 0/3, §17).
- Evidence: the FeatureRange (lines 1–995) already contains the full
  mechanism, and the DetailedRange (lines 996–end) repeats it as plan,
  acceptance, and alternatives. Repetition is not only length: it is
  already producing drift, as in Major 1's two refusal lists. A design
  that states each rule once and references it cannot drift this way;
  one that restates each rule per section will, and reviewers must then
  check every restatement on every edit.
- What would fix it: state each rule once (snapshot, assurance travel,
  validator sharing, semantic-off, default-unchanged) and replace the
  other occurrences with a cross-reference to the single statement.

### Major 9 — Phasing puts the only near-term user value last

- Sections: §15 phases 1–6 (lines 1020–1137), §16 set C (lines 1206–1235).
- Evidence: the build order is measurement, storage, engine, editor,
  presets as data (phase 5), suggestions (phase 6). Presets — the part
  that delivers "cheaper or stricter run" — arrive only after the editor
  is built, even though presets do not depend on the editor (they are
  data in the profile format, §9.1) and the brief's own build order and
  lenses favour presets, picker, and measurement first. The document
  admits the cost of C1: "early phases deliver no user-visible flow
  choice" and it "fails when an early phase stalls and the feature is
  perceived as abandoned before the editor exists." A phase plan that
  names its own abandonment failure mode and still orders the work that
  way has the order backwards.
- What would fix it: move presets and the run-creation picker ahead of
  the editor (measurement, presets/picker, storage, engine, editor,
  suggestions), or adopt C3 as the first shipped slice with the editor
  and suggestions as later slices behind their own gates.

---

## Nits

### Nit 1 — Design-phase detail that belongs to implementation

- Sections: §7.5 (lines 521–550), §14 (lines 972–995), §12.3 (lines 855–875).
- Evidence: §7.5 pins exact user-facing refusal strings; §14 pins a 64 KB
  per-version file size cap with no justification for that number; §12.3
  pins thresholds (10 runs, 90 days, 5 runs) the document itself calls
  "design values, tunable constants." None of these numbers has data
  behind it yet, and pinning them now means implementation must either
  follow invented constants or re-open the design to change them.
- Fix: state the rule and the shape of the message, and leave exact
  strings, the size cap, and threshold numbers to implementation with a
  note that they are starting values.

### Nit 2 — Phase 0 contradicts the phase rule in the same paragraph

- Section: §15 introduction (lines 996–1006).
- Evidence: "A phase does not start until the previous phase's acceptance
  checks pass, except phase 0's contract work, which is documentation plus
  the conformance suite skeleton and may overlap phase 1." A rule with
  its exception stated in the same breath is not a rule implementers can
  rely on for scheduling.
- Fix: state the overlap explicitly as the rule (contract work runs
  alongside phase 1) and drop the absolute form.

### Nit 3 — Identifier scheme is inconsistent across the option sets

- Sections: §6.1 (lines 347–360), §16 set A (lines 1149–1175).
- Evidence: §6.1 defines ids as `<scope>:<slug>`, while option A2 defines
  ids as `flow:<slug>` — a different scheme, not just a different word.
  §6.1 also allows two profiles to share a display name in different
  scopes, with lists showing a scope badge; the run page and PR body,
  where the label also travels (§10.3), are not list contexts, so the
  badge solution does not cover every place a name appears.
- Fix: one id scheme stated independently of the naming pick, and a rule
  for how a profile is identified outside list views (id plus version,
  never display name alone).

### Nit 4 — Storage pattern diverges from the existing profile store without a reason

- Sections: §2.4 (lines 141–157), §6.2 (lines 361–378).
- Evidence: learning profiles live in a database table
  (`core/learning/profiles.py`, `LessonProfilesMixin`: create, list,
  enable, flush, delete), while workflow profiles get one JSON file per
  version plus a `current.json` pointer. The document gives the
  repo-travel reason for repo-scope files but never addresses why a
  same-shaped concept (named profiles with versions and deletion rules)
  uses a different storage mechanism, or what happens to file-per-version
  stores under concurrent edits, partial writes, or backup.
- Fix: one sentence justifying files over the existing store pattern, or
  reuse of the store pattern for user-scope profiles.

### Nit 5 — Phase 4 hardcodes names that option set A has not picked

- Sections: §13.1 (lines 907–932), §15 phase 4 (lines 1085–1101).
- Evidence: §13.1 says route names follow option set A, but phase 4
  already names `routes_workflows.py` and `templates/workflows/`. If A2
  ("Flow", route `/flows`) wins, those names are wrong on arrival.
- Fix: describe phase 4 outputs generically ("workflow routes module,
  editor templates") until set A is picked.

---

## What checks out (short)

- Context sufficiency passes: §1 states the objective and §2 grounds the
  current state in code that matches what was read (`RunState` 19 values:
  7 work, 7 gate, 5 stopped; `_advance` handler map; `STEP_GATES`;
  `execution_gate_target`; cumulative quality levels in
  `quality/levels.py`; `score_threshold` 85, `score_max_retries` 2,
  `max_plan_rounds` 3, `max_fix_rounds` 2 in `config/defaults.py`;
  `/profiles`, `/spend`, `/design-systems` routes in `dashboard/app.py`).
- The §2.1 analysis that a flowchart cannot execute handler-internal
  transitions, and the §2.5/§2.6 limits on cache and measurement, match
  the code. The document's failure-mode list in §4 names real risks.
  The findings above are about what the design then does with that
  analysis: it keeps the editor the analysis undermines, details a
  suggestion engine the data cannot yet feed, and leaves eight decisions
  open while writing 500 lines of detail that assume them.
