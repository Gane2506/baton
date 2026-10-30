# Implementation readiness report — Stage 9 (codebase lens)

Target: `docs/designs/profile-workflows/design.md`, DetailedRange lines 996–end
(section 15 implementation plan, section 16 option sets, section 17 acceptance
criteria / test matrix / rollback, section 18 alternatives, section 19 open
questions). Earlier sections were read only to check that the plan matches them.
Code was read in the same worktree (branch `design/profile-workflows`).

Stage 9 question, in plain terms: can a competent engineer who does not know
this codebase follow section 15 and produce the right changes on the first
attempt, without guessing or making design choices? On the evidence below, no.
The plan points at the wrong file for run creation, reuses a parameter name
that already means something else, names a migration directory that does not
exist, uses a ship-mode value that does not exist, leaves the safety label
without a build step, and sets a conformance gate that cannot pass as written.
Several findings below were already reported at stages 6–8; they are still
present in the current text, so they are cited to the stage 8 report
(`review/intermediate_zoom_out_report.md`) rather than presented as new.

Counts: **12 Blockers, 16 Majors, 5 Nits.**

## What was checked and found correct

These items in the plan match the code, so they are not findings:

- Planner-policy values `free_training_ok`, `mixed`, `mixed_no_training`,
  `paid_only` exist (`config/schema.py`, `PlannerPolicy`).
- Quality-level names `none`, `basic`, `medium`, `production`, `scalable`
  exist (`quality/levels.py`, `QualityLevel`), and `medium` is where secret
  scanning, SAST, and dependency audit start.
- `STEP_GATES` and `execution_gate_target` exist in `core/pipeline.py` and
  behave as section 8.1 describes for the step gates and the plan gate.
- Learning profiles live in a `learning_profiles` table with create, list,
  enable, flush, and delete (`core/learning/profiles.py`).
- The CDA ledger's best-tier floor is 3 graded diffs
  (`code_orchestrator/ledger.py`, `best_tier`).
- Semantic shadow observation exists as `Cache.log_shadow`
  (`core/cache.py`), measuring agreement without serving.
- Ship `after_approval` is `merge` or `ask`, defaulting to `ask`
  (`core/ship/merge.py`, `after_approval`).

## Blockers

### B1 — Phase 2 puts run creation in the wrong file, and misses the real funnel

- Design: section 15, Phase 2, lines 1041–1064 ("`src/jev_agent/core/pipeline.py`
  (change): run creation resolves a profile version into a snapshot").
  Also section 2.7's claim that run creation "flows through `Pipeline.start`".
- Code: production run creation does not go through `Pipeline.start`. The
  funnel is `app/runs.py`, `create_run` / `start_run`:
  - Dashboard `/runs/new` (`dashboard/routes_board.py`, `runs_new`) calls
    `dashboard/runner.py`, `prepare_run`, which calls `app/runs.py`,
    `create_run`, and then starts the run in a background launcher job
    without advancing it inline.
  - CLI `jev run` (`cli/run_cmds.py`) calls `app/runs.py`, `start_run`.
  - The scheduler (`cli/scheduler_cmds.py`, `start_item`) calls
    `app/runs.py`, `start_run` for each queue item.
  - Chat-started runs (`app/chat.py`, `start_run_from_chat`) call
    `app/runs.py`, `create_run`.
  - `Pipeline.start` (`core/pipeline.py`) is used only by tests in this
    worktree, not by any production front end found.
- Consequence: an implementer who follows Phase 2 edits `pipeline.py` and
  changes nothing for dashboard, CLI, scheduler, or chat runs. Phase 2 never
  names `app/runs.py`, `dashboard/runner.py`, `cli/scheduler_cmds.py`, or
  `app/chat.py`. The resolver belongs in `app/runs.py`, `create_run`, with
  the call sites above passing the resolved value through; the plan must say
  that, and must say what happens to `Pipeline.start` (kept for tests, given
  the same resolver, or left alone).

### B2 — The name `profile` already means workspace/server profile on every creation path

- Design: section 15, Phase 2, lines 1041–1064 ("each reusing the same
  resolver" across dashboard, CLI, queue, recurring, chat).
- Code: every existing `profile` parameter on those paths means a workspace
  (server/browser/learning) profile, not a workflow profile:
  - `Pipeline.start(profile=...)`, `app/runs.py`, `create_run(profile=...)`
    and `start_run(profile=...)` pass it to `servers/profiles.py`,
    `snapshot` / `get_profile`, and `write_run_profile` writes it to
    `.jev/runs/<id>/profile.json`.
  - `QueueItem.profile` and `RecurringTask.profile`
    (`core/store/records.py`) carry the same workspace profile through the
    queue and the scheduler.
  - `ChatSession` meta `profile` (`app/chat.py`, `start_run_from_chat`,
    `queue_from_chat`) is the same workspace profile.
  - `Pipeline.start` raises `ProfileError` (a `ValueError`) for an unknown
    workspace profile.
- Consequence: Phase 2's "same resolver" with no distinct parameter, form
  field, queue column, or recurring-task column name means a workflow
  profile id handed to any of these paths is looked up as a server profile
  and fails, or is silently treated as one. The plan must name the new
  parameter and field at each layer (`app/runs.py`, `QueueItem`,
  `RecurringTask`, chat meta, dashboard forms, CLI flags) and must state
  that the existing `profile` field is untouched. Until it does, no
  creation path in Phase 2 can be implemented as written.

### B3 — Ship mode `off` is not a value anywhere in the code

- Design: section 7.3 (line 481) and section 7.5 (line 539) let a profile omit
  `SHIPPING` by "choosing ship mode `off`"; section 10.1 lists ship outcomes
  as "merge, ask, or off". Section 15, Phases 2–3 never define how `off` is
  stored or executed.
- Code: `ShipMode = Literal["merge", "ask"]` (`config/schema.py`). "Off" is
  represented differently: `config.ship.enabled` is `False`, and
  `after_approval` (`core/ship/merge.py`) returns `None` in that case, which
  `_after_ship_vote` (`core/pipeline.py`) treats as "end DONE unshipped".
  `RunRecord.ship_mode` and `Pipeline.start`'s `ship_mode` are
  `ShipMode | None`, so neither accepts `"off"`.
- Consequence: a validator that accepts ship mode `off` has no executor
  mapping, and a snapshot that stores `"off"` cannot be written through the
  existing run fields. The plan must state the translation (for example,
  snapshot carries a shipping-enabled flag plus `ship_mode=None`, or a new
  run field) and which layer performs it. The test matrix (section 17) lists
  ship modes as `merge`, `ask`, `off` without noting that `off` is not a
  stored mode, so the test cannot be written from the plan alone.

### B4 — The migration directory in Phase 1 does not exist

- Design: section 15, Phase 1, lines 1020–1040
  ("`src/jev_agent/core/store/migrations/` (new migration): stage records
  table and run-record snapshot columns").
- Code: there is no `src/jev_agent/core/store/migrations/` directory. All
  migrations live in one file, `src/jev_agent/core/migrations.py`: the
  `MIGRATIONS` list, one script per schema version, applied with
  `PRAGMA user_version`, each script in its own transaction, with the rule
  "never edit a released script: append a new one". A separate
  `SHARED_MIGRATIONS` list covers the shared learning database.
- Consequence: an implementer following Phase 1 creates a directory the
  loader never reads, so the stage table is never created and Phase 1
  acceptance cannot pass. The plan must name the real file, the new
  migration's position (appended, not inserted), which database it targets
  (run database or shared learning database), the `rows.py` row-to-record
  conversion, and the `RunsMixin` methods in `core/store/runs.py` that read
  and write the new table. None of those are named.

### B5 — The safety label has no build step and no writer

- Design: section 10.3 (label on the run page, the board row, and the PR
  body) against section 15, Phases 1–6, lines 1020–1137, and section 17
  acceptance ("the label appears on the run page and in the PR body").
  Persisting: stage 8, N1 (no phase builds the floor's label) and P10
  (legacy-run labelling contradicts the unchanged-default rule).
- Code: no phase names the places that would have to change:
  - The PR body writer is `core/ship/pr_body.py`, `render_pr_body(store,
    run_id, kpis)`; it takes no assurance value and renders no assurance
    line today. It is called from `core/ship/prepare.py` for the CLI ship
    path (`jev ship` on a finished run), which is outside the dashboard
    entirely — so a reduced run shipped from the CLI ships without the
    label unless that call site is changed, and no phase lists it.
  - The run page and board row are rendered from `dashboard/views.py`
    (`run_view`, `board`) and the flat templates `run.html` / `board.html`;
    no phase lists any of them.
  - Phase 1 adds run-record columns for assurance but states "no handler
    logic changes in this phase", and no later phase names an assurance
    computation function, its inputs for legacy runs (effective settings
    read at display time, or a backfill), or when it runs.
- Consequence: section 17 acceptance for the label cannot be met by any
  phase in section 15. The plan needs a phase (or an explicit step inside
  an existing phase) that builds assurance computation plus rendering in
  `pr_body.py`, `prepare.py`, the run page, and the board row, and states
  what happens for legacy runs whose columns are null.

### B6 — Phase 3 names only two of the places that decide a run's flow

- Design: section 15, Phase 3, lines 1065–1084 (snapshot-aware step lookup
  and gate-policy application "through the existing `STEP_GATES` and
  `execution_gate_target` helpers"). Persisting: stage 8, P5 (handler moves
  and `approve` have no snapshot check).
- Code: handlers and commands read run fields and config directly, and
  Phase 3 names none of these read sites:
  - `_integrate` reads `run.quality_level` (`core/pipeline.py`).
  - `_after_ship_vote` reads `run.ship_mode` through `after_approval`.
  - `_move_to_execution` reads `run.plan_only` and
    `config.plan_approval_required`.
  - `_plan` / `_validate` read `config.budget.max_plan_rounds`;
    `_ship_after_score_gate` reads `config.quality.score_max_retries`;
    `_integrate` reads `PipelineSettings.max_fix_rounds` (set from wiring,
    not from the run).
  - `Pipeline.approve` builds a static inverse map from `STEP_GATES` plus
    `AWAITING_SHIP -> SHIPPING` with no snapshot parameter.
  - `request_changes` hardcodes a move to `EXECUTING`;
    `stop_ship` hardcodes a move to `DONE`.
  - `_restore_resume_state` restores `resume_state` with no check that the
    snapshot still contains that stage.
- Consequence: even if the step lookup and the two named helpers gain a
  snapshot parameter, a profiled run still executes the run record's
  quality level, ship mode, plan-only flag, and config round caps, and a
  human can approve a run into a stage its snapshot omits. The plan must
  list every read site and state, per site, whether the handler changes to
  read the snapshot or run creation overwrites the run columns — and if
  creation overwrites them, how the per-setting winning source (section
  9.2) is still recorded (see M3, which shows it is not).

### B7 — The rollback flag is unnamed, needs a schema change the plan never lists, and fails under option G2

- Design: section 17, rollback, lines 1424–1436 ("disabling profile
  selection at run creation (a configuration flag, default on)").
  Persisting: stage 8, N5.
- Code: no such flag exists, and config classes inherit `_Strict`
  (`config/schema.py`), which forbids unknown fields — so a new flag
  requires a named schema addition, not just a code check. Under option
  set G2 (default re-expressed as a profile, every run through the
  snapshot-aware path), there is no legacy path left to roll back to, so
  "disabling selection at creation" does not restore previous behaviour.
  Under G1, the plan still does not say which creation paths the flag
  gates (see B1: the funnel spans `app/runs.py`, the dashboard launcher,
  the scheduler, and chat).
- Consequence: the rollback paragraph cannot be executed. It needs a flag
  name, a schema location, the list of gated paths, and a stated behaviour
  under each G branch — or a different rollback mechanism for G2.

### B8 — Autonomy cannot come from a profile through today's creation funnel

- Design: section 15, Phase 2 (resolver at run creation) and section 9.1
  (presets set autonomy mode `standard`). Persisting in part: stage 8, P12
  (five modes, only three described).
- Code: production run creation derives autonomy from config only.
  `app/runs.py`, `_autonomy_for_new_run` maps the `[autonomy]` config
  section onto the run's autonomy fields; `create_run` takes no autonomy
  parameters at all. `Pipeline.start` does validate an `AutonomyConfig`
  (`core/models.py`): `AUTONOMOUS` needs `autonomous_opt_in=True`,
  `MEDIUM` needs a folder (`medium_folder` / `project_folder`), but that
  validation sits on a path production front ends do not use.
- Consequence: a profile that sets autonomy mode, or a `MEDIUM` profile
  without a folder, has no defined mapping and no defined error. Section
  9.2's rule that a profile never flips an `AUTONOMOUS` run into a gated
  run (or vice versa) cannot be enforced at a funnel the plan does not
  touch. The plan must also cover `MANUAL`: `Pipeline.start` returns
  without advancing and the run waits in `INTAKE` for an instruction, so
  "resolve at creation" and "check at first advance" are different moments
  for manual runs, and the plan picks neither.

### B9 — The conformance gate cannot pass as written

- Design: section 15, Phase 3, lines 1065–1084, and section 17, test matrix,
  lines 1405–1413 (legacy path vs. `default` preset path, "identical stage
  sequences, gate parkings, and outcomes, including Halt/resume and cancel
  cases"). Persisting: stage 8, P8.
- Code: pipeline transitions are driven by model decisions (D1 intake, D3
  verdict, D6 vote) and worker output, which are not deterministic across
  two live runs. Nothing in section 15 names a stub, replay, or
  fixed-decision harness that would make two runs comparable. The
  Halt/resume/cancel cases add further unspecified setup: `cancel_run`
  needs no lease and only touches the store (`core/pipeline.py`), state
  writes guard against overwriting a cancellation (`KEEP_CANCELLED`), and
  resume closes interrupted attempts as errors (`_close_interrupted_attempts`).
  A conformance run that cancels mid-step and resumes therefore has
  attempt and stage history the plan never describes.
- Consequence: Phase 3's gate ("must pass before any profiled run beyond
  tests is allowed") blocks everything after it on a test that cannot pass
  as specified. The plan must name the harness (what is stubbed, what is
  replayed, how a cancelled-then-resumed run is compared) before Phase 3
  can start.

### B10 — Eight unpicked option sets, with the plan written as if they were picked

- Design: section 16, lines 1138–1379, against sections 6–15 and section 17.
  Persisting: stage 8, P4.
- Evidence in the plan itself: section 15 states that sections 6–15 are
  written for the proposed answers and that a different pick means re-audit
  before implementation. A pick in any set breaks a different part of the
  plan: G changes whether Phase 3 touches every run or only profiled runs;
  H changes whether the section 9.1 Accuracy table is the right table; B
  changes section 10 enforcement and section 17 acceptance; D changes
  Phase 4; E changes section 6 storage; F changes section 9.2; C changes
  section 15 outright.
- Consequence: under the implementation-readiness standard's zero-decision
  rule, section 15 is not executable yet. The gate in section 16 must be
  run first and the document re-audited, or implementation must be
  explicitly scoped to the subset that does not depend on unpicked sets.
  This is a property of the plan as a whole, not of one phase.

### B11 — The snapshot is still defined two ways, and Phase 1–3 build the copies without a merge rule

- Design: section 5 (snapshot as the profile version frozen onto a run),
  section 6.4 (snapshot as the full resolved parameter set stored on the
  run record), section 8.1 (values read from the snapshot, falling back to
  repo configuration for anything the profile does not set), section 9.2
  (winning source per setting). Persisting: stage 8, P2 and N6.
- Consequence for the plan: Phase 1 writes snapshot columns, Phase 2
  resolves a snapshot, Phase 3 reads it with a fallback — but a full copy
  has nothing left to fall back to, and a partial copy is not the full set
  section 6.4 promises. No phase states the field-by-field map from
  snapshot fields to existing run-record fields (`quality_level`,
  `ship_mode`, `plan_only`, `learning_profile`, `autonomy_mode`), nor what
  happens when a run column and the snapshot disagree. An implementer
  must invent that map, which is a design decision, not an implementation
  detail.

### B12 — Two different refusal lists for stages that cannot be left out, and a goal that contradicts the mechanism

- Design: section 7.3 (five stages may not be omitted: `INTAKE`,
  `PLANNING`, `VALIDATING`, `EXECUTING`, `INTEGRATING`) against section
  10.2 (refusal only for `VALIDATING`, `EXECUTING`, `INTEGRATING`).
  Persisting: stage 8, P1. Related: stage 8, P3 — section 3 and section 17
  require a user to "rearrange" a flow, while section 7.1 refuses every
  reordering and section 13.2 admits no reordering passes in practice.
- Consequence for the plan: Phase 2 builds the validator and Phase 4
  builds the editor against a refusal rule that gives two different
  answers for a profile omitting `INTAKE` or `PLANNING`, and Phase 4
  acceptance requires "rearrange" in both editor modes for an operation
  the validator refuses. One refusal list and one honest statement of
  what the editor can change (omission of `DESIGNING`, ship mode, and
  parameters) must be settled before Phases 2 and 4 start.

## Majors

### M1 — The Phase 1 "step wrapper" does not match how `_advance` and gates actually work

- Design: section 15, Phase 1, lines 1020–1040 ("the `_advance` step wrapper
  that writes stage rows, gate timing, and ship timing; decision/attempt
  stage tagging"). Persisting in part: stage 8, P11 (write path
  underspecified; derivation from attempt records never compared).
- Code: `_advance` (`core/pipeline.py`) loops over a steps dict and breaks
  on any state with no handler — which includes every `AWAITING_*` gate
  state, `DONE`, and the stopped states. Gate entry and exit happen inside
  handlers via `_move`, `_move_to_step`, `_move_to_execution`, and
  `_after_ship_vote`, not in the loop, so a wrapper around the step call
  cannot see gate-wait time. `_finish` runs after the loop on every exit,
  including gate stops, but only acts for `LEARN_STATES` (`DONE`, `FAILED`,
  `NEEDS_HUMAN`, `BUDGET_EXCEEDED`). `approve` with `execute=False`
  passes a gate without calling `_advance` at all. Cancel needs no lease
  and only touches the store, while the state-write guard
  (`KEEP_CANCELLED`) applies to state writes — the plan names no
  equivalent guard for stage-row writes after a cancel. Resume closes
  interrupted attempts as errors, and no stage-row close rule exists for
  the interrupted stage. There is no per-stage attempt counter in the
  pipeline (`plan_round`, fix rounds, and score-fix rounds are separate
  counters), so "one row per attempt, linked by attempt number" has no
  named counter to link to. Decisions and attempts carry no stage
  parameter today, and the plan names no call sites where tagging would
  be added.
- Consequence: Phase 1 acceptance ("stage rows sum to recorded totals
  within completeness rules") cannot be built from the wrapper described.
  The plan must name the instrumentation points (`_move` for gate timing,
  `approve`/`resume`/`cancel_run` for row lifecycle, the attempt and
  decision recording sites for tagging) and must show, or rule out, the
  derivation alternative the brief required (attempt records already carry
  start/finish timestamps and cost).

### M2 — Cache mode names in the profile language do not exist in config

- Design: section 7.4 (cache mode `exact` or `exact_off`), section 9.1
  (all presets `exact`).
- Code: `[cache]` has `enabled` (a boolean master kill-switch, also
  `--no-cache` per run) and `[cache.semantic]` has `mode` (`off`,
  `shadow`, `serve`) plus `enabled` (default off) (`config/schema.py`,
  `CacheSettings`, `CacheSemanticSettings`). There is no `exact` /
  `exact_off` enum.
- Consequence: the validator cannot validate a cache mode the config
  cannot express, and the executor cannot apply it, until the plan states
  the mapping (presumably `exact_off` means `enabled=False`) and its
  scope: whether it also stops shadow logging (`log_shadow` writes to
  `cache_shadow_log` independently of serving), and how it interacts with
  the per-run `--no-cache` flag under the section 9.2 precedence table.
  The plan states none of this.

### M3 — The per-setting winning source has no column in the Phase 1 schema change

- Design: section 9.2 (the run record names the winning source per setting)
  against section 15, Phase 1, lines 1020–1040 (snapshot columns listed as
  `profile_id`, `profile_version`, `goal`, `assurance`,
  `assurance_absent`).
- Consequence: the schema change Phase 1 actually specifies cannot hold
  the data section 9.2 requires. The plan must add the field (or state
  that the snapshot JSON itself carries the source map, and where that
  JSON lives), or section 9.2 must be cut. As written, an implementer
  reaches Phase 3 and finds nowhere to write the source map.

### M4 — Phase 4 contradicts the dashboard's actual conventions

- Design: section 15, Phase 4, lines 1085–1101 (`routes_workflows.py`,
  `templates/workflows/`, `static/` scoped files, "following existing
  route patterns").
- Code: dashboard routes are mixin classes combined in `Dashboard`
  (`dashboard/app.py`, bases `BoardRoutes`, `QueueRoutes`, `ChatRoutes`,
  and others) with `Route` entries in `create_app` and a nav entry in
  `dashboard/nav.py`; a standalone routes file that is not added as a
  mixin base and registered in `create_app` serves nothing, and Phase 4
  names neither change. Templates are flat files in `dashboard/templates/`
  (`design_systems.html`, `design_system.html`, and others — the closest
  existing library model, design systems, uses flat templates, not a
  subdirectory), and static files are flat in `dashboard/static/`.
  Form handling is `checked_form` with off-loopback token gating
  (`dashboard/security.py`, `DashboardCore.act` / `page`); Phase 4 says
  "following existing route patterns" without naming any of them.
- Consequence: Phase 4 as written produces files in places the app never
  loads from and routes the app never registers. The plan must name
  `app.py` (mixin base + routes + nav) and the flat template/static file
  names, or state a new subdirectory loading rule.

### M5 — The preset package directory does not exist and no packaging step is named

- Design: section 15, Phase 5, lines 1102–1118
  (`src/jev_agent/data/workflow-presets/`).
- Code: there is no `src/jev_agent/data/` directory in the package, and
  Phase 5 names no packaging mechanism (package data declaration or
  resource loading) that would make preset files present in an installed
  Baton rather than only in a source checkout. Presets "versioned with
  the release" also collide with the upgrade behaviour in section 6.3:
  package data is replaced on upgrade, so "a newer release adds a newer
  preset version alongside the old one" needs a named retention or
  install step. Persisting in part: stage 8, P9.
- Consequence: Phase 5 acceptance ("both presets run end to end on the
  Mac dashboard") may pass from a checkout and fail from an install, and
  the old-version retention rule has no mechanism.

### M6 — The validator is built twice with no stated boundary

- Design: section 15, Phase 2 (shared validator alongside storage) and
  Phase 3 (validator "changed" to the full catalogue). Persisting:
  stage 8, N9.
- Consequence: Phase 2 acceptance (create, version, archive, delete) runs
  against an unknown validation strength — schema only, a catalogue
  subset, or the full catalogue early — and nothing re-checks Phase 2
  artefacts (stored profiles, archived versions) against the Phase 3 full
  catalogue. A profile accepted in Phase 2 can fail in Phase 3 with no
  named step that catches it, and the Phase 4 editor is then built against
  a different validator than the store it edits.

### M7 — Phase 0 breaks its own phase rule, and the two conformance suites are undefined as one or two

- Design: section 15, introduction and Phase 0, lines 996–1019 ("a phase
  does not start until the previous phase's acceptance checks pass, except
  phase 0's contract work, which may overlap phase 1") and Phase 3
  (conformance suite). Persisting: stage 8, N8.
- Consequence: the contract Phase 1 measurement is built against (floor
  table, knob list, stage/gate catalogue) is still being written while
  Phase 1 is built, and the plan never says whether the Phase 0 skeleton
  and the Phase 3 suite are one suite grown in two phases or two suites,
  what the skeleton contains, or what Phase 0 acceptance requires beyond
  "documents today's behaviour".

### M8 — The Phase 3 gate contradicts Phase 5 acceptance

- Design: section 15, Phase 3 ("must pass before any profiled run beyond
  tests is allowed") against Phase 5 acceptance ("both presets run end to
  end on the Mac dashboard"). Persisting: stage 8, N2.
- Consequence: presets are profiled runs. The plan never says whether
  Phase 5 is gated on Phase 3 or preset runs are exempt, so the gate
  either blocks the very acceptance it is supposed to enable or is
  silently waived for presets. Separately, Phase 3 acceptance allows
  results to differ "where the profile deliberately differs", but for the
  `default` preset (which deliberately differs in nothing) and for any
  other profile compared against a legacy run, "deliberately differs" has
  no definition, so the suite's pass condition is undefined.

### M9 — The goal-label clock starts at Phase 5, not Phase 1

- Design: section 15, Phases 1, 2, 5 against section 12.3 (usage-based
  suggestions need at least 10 completed runs with stage records).
  Persisting: stage 8, N3.
- Code and plan interaction: Phase 1 rows for legacy runs carry goal
  `null` by construction, and section 12.1 excludes goal-`null` runs from
  goal-conditioned evidence. Goal labels arrive with presets in Phase 5
  (section 9.1 sets `efficiency` / `accuracy` from the preset). So the
  evidence clock for Phase 6 suggestions starts four phases after the
  measurement phase that was supposed to start it, and no phase checks
  labelled-run volume before Phase 6 starts.

### M10 — Section 17 acceptance is false under approved option picks

- Design: section 17, lines 1380–1436, against section 16. Persisting:
  stage 8, N11.
- Cases: under C3 (no editor, no suggestions) the browser-creation and
  suggestion criteria are false by design; under D2/D3 the "both editor
  modes" criterion is false; under B2 (refuse everything) there is no
  reduced run whose label rendering can be verified live; under H2
  (Accuracy optimizes wall-clock speed) the Accuracy criterion
  (production quality level, CDA grading on) describes the wrong preset.
- Consequence: section 17 cannot serve as the gate's checklist until it
  is rewritten as one checklist per option branch, or held until the gate
  picks and then rewritten — and section 15 gives that rewrite no phase.

### M11 — Suggestion tests miss the hardest parts of the engine

- Design: section 15, Phase 6, lines 1119–1137 (threshold, approval, and
  honesty tests) against sections 12.1, 12.3, and 13.3. Persisting:
  stage 8, N13.
- Gaps: no test covers cross-repo aggregation (normalization across repos
  with different configurations and models, or the "say so" safeguard's
  display), no test covers the discarded-suggestion dedup rule (what
  counts as new evidence), and no test covers evidence freshness at
  approval time (a draft approved after new runs arrive is approved on
  stale reason lines). Phase 6 tests the easy thresholds and leaves the
  aggregation rules untested.

### M12 — Repo-scope storage: committed or local is unstated, and version writes have no atomicity rule

- Design: section 15, Phase 2 (repo-scope files under
  `<repo>/.jev/workflow-profiles/`) against section 6.2 ("travel with the
  repo", "visible to anyone with the repo"). Persisting in part: stage 8,
  P9.
- Code precedent: the existing per-run workspace snapshot is written to
  local state (`.jev/runs/<id>/profile.json` via `write_run_profile`),
  not to committed repo content. The plan never says whether
  workflow-profile files are committed, copied by hand, or local state —
  and if local, the "travel with the repo" claim fails. Version creation
  is two steps (write `v<n>.json`, then update `current.json`) with no
  locking or atomic rule, even though a file-lock helper exists in the
  codebase (`core/file_lock.py`) that the plan never mentions.
- Consequence: concurrent edits and a crash between the two writes leave
  a pointer and a version file disagreeing, with no named recovery, and
  a teammate cannot reproduce a shipped PR's flow from the repo alone
  unless the storage question is answered.

### M13 — Section 2.3 lists three autonomy modes; the code has five, and the test matrix tests two

- Design: section 2.3 (autonomy mode as `standard`, `autonomous`,
  `ask_every_step`), section 17 test matrix (`AUTONOMOUS` and
  `ASK_EVERY_STEP` modes only).
- Code: `AutonomyMode` (`core/models.py`) has five values — `standard`,
  `autonomous`, `ask_every_step`, `manual` (waits in `INTAKE` for an
  instruction), and `medium` (folder-scoped) — and the config literal
  (`config/schema.py`, `AutonomyMode`) matches. Persisting: stage 8, P12.
- Consequence: `MANUAL` and `MEDIUM` have no defined profile behaviour
  and no test, so Phase 2's resolver and section 17's matrix are
  incomplete for two of the five modes a run can actually be in.

### M14 — `plan_only` conflicts with the non-omittable stage rule, unresolved by the plan

- Design: section 7.3 (non-omittable stages include `EXECUTING` and
  `INTEGRATING`) against `plan_only` runs, which stop at the approval gate
  and never reach them. Persisting: stage 8, P12.
- Code: `plan_only` is a first-class run field (`core/store/records.py`,
  `RunRecord.plan_only`), applied in `_move_to_execution`
  (`core/pipeline.py`) and carried on queue items and recurring tasks.
- Consequence: Phase 2's resolver must state whether a `plan_only` run
  with a profile is validated against the non-omittable rule, exempted
  from it, or refused — the plan states none of the three, so the
  validator's behaviour for a common run shape is left to the implementer.

### M15 — Phase 5 acceptance ("cheaper models") cannot be measured by Phase 1 data

- Design: section 15, Phase 5, lines 1102–1118 ("Efficiency measurably
  uses fewer rounds and cheaper models than the default on the same
  scenario, measured by stage rows, not estimated"). Persisting:
  stage 8, N4.
- Code and plan interaction: stage rows record tokens, time, and cache
  behaviour, but no prices; section 12.1 forbids the engine from reading
  prices it was not configured with, and no price source is named for
  "cheapest configured model per role". Codex workers report no cost, so
  their stage rows carry null costs. Fewer rounds is measurable from
  stage rows; cheaper models is not, because token counts do not prove
  price.
- Consequence: Phase 5 contains an acceptance criterion its own
  measurement phase cannot supply. The plan must name a price source or
  drop the word "cheaper" from the acceptance.

### M16 — Directory-level targets leave file choices to the implementer

- Design: section 15, Phase 1 ("`src/jev_agent/dashboard/` (change, views
  only)"), Phase 2 ("`src/jev_agent/cli/` (change)"), Phases 0/3/5/6
  ("`tests/` (new)").
- Consequence: under the zero-decision rule, "dashboard, views only" is
  not a target. The Spend page is `dashboard/views.py`, `spend_view`,
  rendered by `spend.html` and reached through `routes_settings.py`,
  `spend_page`; the run page is `run_view` plus `run.html` through the
  board routes. The CLI change is presumably `cli/run_cmds.py`,
  `cli/queue_cmds.py`, and a new `jev workflow` command module, but the
  plan names none of them. The test targets are likewise unnamed
  (conformance suite location, validator test file, preset test file,
  suggestion test file). Each of these is a choice the implementer must
  make, and each must be named before implementation.

## Nits

### N1 — Phase 4 hardcodes names option set A has not picked

- Design: section 15, Phase 4, lines 1085–1101 (`routes_workflows.py`,
  `templates/workflows/`), while section 16, set A, leaves the name and
  route unpicked. Phase 2 carries a note that module names follow option
  A; Phase 4 carries no such note. Persisting: stage 8, Nit 1.
- Fix: add the same option-A note to Phase 4, or leave the names
  unpicked until the gate.

### N2 — Phase 3 is titled "Graph engine" in a document that rejects a graph interpreter

- Design: section 15, Phase 3, lines 1065–1084, against section 18
  (a full graph interpreter replacing `_advance` is rejected). Persisting:
  stage 8, Nit 2.
- Fix: retitle the phase to the snapshot-aware lookup it actually builds,
  so a future reader does not take the title as scope.

### N3 — Profile ids with colons in a path parameter

- Design: section 6.1 (id format `<scope>:<slug>`) against section 13.1
  and Phase 4 routes (`/workflows/{id}`).
- Gap: the plan never says how a colon id is carried in a URL path
  segment, encoded, matched by the route, or displayed in the
  "flow <id> version <n>" error messages. Small, but it must be stated
  before Phase 4, because it affects every editor and detail URL.

### N4 — CLI commands sit in Phase 2 while the document says they are not required for acceptance

- Design: section 15, Phase 2, lines 1041–1064 (`jev workflow` list, show,
  validate; "the CLI is not required for acceptance"). Persisting:
  stage 8, Nit 4.
- Fix: move the CLI commands to a later phase with their own acceptance,
  or remove them from Phase 2.

### N5 — "Fails loudly at startup checks" names no startup check

- Design: section 15, Phase 5, lines 1102–1118 (a preset referencing a
  removed model "fails loudly at startup checks, not at run time").
- Gap: no startup check location is named — dashboard lifespan
  (`dashboard/app.py`), CLI context setup, or `jev doctor` are all
  candidates, and the plan picks none. The test for this behaviour cannot
  be written from the plan alone.

## What must change before implementation, in order

1. Run the section 16 gate first (B10), then re-audit; the plan is
   conditional until then.
2. Fix the creation funnel: point Phase 2 at `app/runs.py`, `create_run`,
   give the workflow profile its own parameter and field at every layer
   (B1, B2), and settle queue-time versus execution-time snapshot timing
   for queued, recurring, and chat-started runs, including the dashboard
   recurring form's current exclusion of profile options and the
   `jev resume --profile` path for existing runs.
3. Fix the value mappings the code does not have: ship `off` (B3), cache
   `exact` / `exact_off` (M2), autonomy from config only (B8), and the
   per-setting source column (M3).
4. Fix the storage and measurement mechanics: the real migration file and
   database (B4), the `_advance` / gate / cancel / resume instrumentation
   points (M1), the committed-versus-local storage answer and version-write
   atomicity (M12), and the snapshot field map plus every handler read
   site (B6, B11).
5. Give the safety label a build step and a writer in `pr_body.py`,
   `prepare.py`, the run page, and the board row, including legacy runs
   and the CLI ship path (B5).
6. Make the gates executable: name the conformance harness (B9), resolve
   the Phase 3 / Phase 5 contradiction (M8), define the rollback flag and
   its behaviour under each G branch (B7), and rewrite section 17
   acceptance per option branch (M10).
7. Settle the remaining rule contradictions before the phases that depend
   on them: one refusal list (B12), `MANUAL` / `MEDIUM` / `plan_only`
   behaviour (M13, M14), the validator's Phase 2 scope and Phase 3 re-check
   (M6), and the price source for "cheaper models" or the removal of that
   word (M15).

## Scope note

This report covers section 15 (lines 996–1137), section 16 (1138–1379),
section 17 (1380–1436), section 18 (1437–1468), and section 19 (1469–1498),
checked against the code files listed in the task. Findings that cite the
stage 8 report were verified against the current text where they touch
sections 15–17; findings about earlier sections (B12, M13, M14) were
verified against the current text of those sections. No code was modified.
