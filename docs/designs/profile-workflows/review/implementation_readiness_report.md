# Implementation readiness report — Stage 9

Target: `docs/designs/profile-workflows/design.md` (1,498 lines, branch
`design/profile-workflows`, Baton issue #368), detailed range
lines 996–end: section 15 (implementation plan), section 16 (option
sets), section 17 (acceptance criteria, test matrix, rollback),
section 18 (alternatives), section 19 (open questions). Earlier
sections were read only to check whether the plan matches them.
Referenced code was checked for file existence and for the mechanism
the plan names. The stage 8 report
(`review/intermediate_zoom_out_report.md`) was read first.

Method note: the design document is unchanged since stages 5–7. Its
history is still a single commit (`c78e7c4`). Findings from stage 8
that are still present are marked "persisting, stage 8" with the
stage 8 identifier, not presented as new work. New findings are
marked "new".

Counts: **8 Blockers, 12 Majors, 6 Nits.**

## Verdict, stated first

Do not start implementation from section 15 alone. An implementer
following section 15 exactly would hit, in order: a migration
directory that does not exist, a package whose name depends on an
unpicked option, a validator built twice with no stated boundary, a
phase 4 that lists routes for an engine built in phase 6, a safety
floor label that no phase builds, a conformance test with no harness
for non-deterministic model decisions, and a rollback flag with no
name, location, or behaviour. Most picks in section 16 do not let a
builder proceed; they invalidate sections 6–15 and section 17 and
require the re-audit the document itself promises. Only the
proposed-answer branch (A1, B1, C1, D1, E1, F1, G1, H1) matches the
plan as written, and even that branch fails on the assurance phase,
the conformance harness, and the file list below.

This report fails the plan against the implementation-readiness
standard: a competent engineer unfamiliar with the codebase must be
able to execute the plan on the first attempt without guessing,
asking questions, or making independent design choices. Section 15
does not meet that bar.

## Blockers

### B1 — Blocker: the section 16 gate is not a gate a builder can pass and then build from

- Sections: §15 (lines 996–1137) against §16 (lines 1138–1379).
- Persisting, stage 8 P4 and N11.
- Evidence: section 16 says no option is ranked, marked preferred,
  or pre-picked, and that where a set picks otherwise "the document
  is amended by re-audit before implementation (max 1 re-audit per
  stage, per the review rules)". Section 15 gives no branch plan,
  no per-option acceptance, and no phase for that re-audit. A
  builder who receives eight picks still has no amended document
  to build from, and a builder who receives no picks cannot start
  phase 2 (package names follow option A), phase 4 (editor form
  follows option D), or phase 5 (preset tables follow option H).
  The plan therefore does not start after the gate; it starts
  after the gate plus an unplanned rewrite. See B5 for the
  per-set breakdown.

### B2 — Blocker: assurance computation and label rendering have no phase — claim verified

- Sections: §10.3 (lines 716–734) against §15 (lines 996–1137),
  especially phase 1 (lines 1020–1040).
- Persisting, stage 8 N1. The task asked that this claim be
  verified against section 15 before repeating it; it was.
- Evidence: phase 1 adds run-record columns (`profile_id`,
  `profile_version`, `goal`, `assurance`, `assurance_absent`) and
  states "no handler logic changes in this phase". No later phase
  names assurance computation: not phase 2 (storage, versioning,
  validator), not phase 3 (step lookup, gate policy), not phase 4
  (editor routes and templates), not phase 5 (presets as data),
  not phase 6 (suggestions). Label rendering is likewise assigned
  to no phase: not the run page badge, not the board row, not the
  PR body. The PR body writer exists in code
  (`src/jev_agent/core/ship/pr_body.py`, `render_pr_body`) and is
  named in no phase. Section 17 acceptance nevertheless requires
  a reduced-assurance run's label rendering to be verified live
  on the Mac dashboard. A plan that requires an outcome no phase
  produces cannot be executed as written, and the safety floor —
  the protection against the design's own section 4 item 3 failure
  mode — is unowned work.

### B3 — Blocker: phase 4 lists routes for an engine that arrives in phase 6

- Sections: §15 phase 4 (lines 1085–1101) against §15 phase 6
  (lines 1119–1137).
- New.
- Evidence: phase 4 lists `routes_workflows.py` routes including
  "suggestion request/result". Phase 6 builds the suggestion
  engine (`src/jev_agent/core/suggestions/`). Phase 4 acceptance
  covers only creating, rearranging, saving, and running a custom
  flow; it does not cover suggestions, so the suggestion routes
  in phase 4 have no backend to call and no acceptance to pass
  against. An implementer in phase 4 must either stub the engine
  (work the plan never names, to be thrown away in phase 6), build
  the engine early (breaking the phase order), or ship routes
  that fail. The same inversion affects the phase 4 detail page:
  section 13.1 puts an assurance note and suggestion history on
  that page, and assurance has no phase (B2) while suggestion
  history arrives in phase 6. Phase 4 therefore needs two things
  phase 3 does not provide and the plan does not supply elsewhere.

### B4 — Blocker: the conformance test matrix is not executable as written

- Sections: §15 phase 3 (lines 1065–1084) against §17 test matrix
  (lines 1380–1436) and §8.2 (lines 585–599).
- Persisting, stage 8 P8 and N2, with a readiness consequence.
- Evidence: phase 3 acceptance requires a profiled run and a
  legacy run of "the same scenario" to produce identical stage
  sequences, gate parkings, and outcomes "except where the profile
  deliberately differs, and every deliberate difference is named
  in the test". The plan never names the harness that makes this
  checkable: no stubbing, replay, fixed-seed, or recorded-decision
  mechanism; no definition of "same scenario" (same prompt, same
  repo state, same worker, same model answers); no format or
  location for the registry of deliberate differences. Pipeline
  transitions are driven by model decisions (D1 intake, D3
  verdict, D6 vote) and worker output, which are not deterministic
  across two live runs. Without the harness, the suite is either
  flaky by construction or the implementer must invent a harness
  the design never describes — exactly the independent design
  choice the readiness standard forbids. For the `default` preset
  the two statements agree (it deliberately differs in nothing);
  for any other profile compared against a legacy run, the pass
  criterion is undefined.

### B5 — Blocker: most section 16 picks cannot be built without re-designing sections 6–15

- Sections: §16 (lines 1138–1379) against §§6–15 and §17.
- Persisting, stage 8 P4, tested here pick by pick as the task
  asked.
- The question is whether a builder can pick one option per set
  and proceed without re-designing. For the proposed answers
  (A1, B1, C1, D1, E1, F1, G1, H1) the answer is yes in outline,
  subject to B2, B4, and B7. For the other options it is no:

  - **A2 ("Flow", route `/flows`, ids `flow:<slug>`):** phase 4
    already hardcodes `routes_workflows.py` and
    `templates/workflows/` (see Nit N1), section 6.1 defines ids
    as `<scope>:<slug>`, and section 13.1 defines routes as
    `/workflows`. A2 changes all three at once, and the plan
    states the rename rule only for the phase 2 package, not for
    ids, routes, or templates.
  - **A3 (UI-only relabelling of learning profiles and server
    profiles):** no phase or file list covers relabelling UI text
    across templates, nor the mapping between code names in
    errors/logs (`learning_profiles`) and display names
    ("lesson profiles"). The builder must invent that mapping.
  - **B2 (refuse everything):** section 10.3's label path, the
    section 17 criterion "refused or labelled", and the live
    verification of a reduced run's label all become dead. There
    is no reduced run left to verify, so section 17 acceptance
    must be rewritten, not followed.
  - **B3 (label everything):** section 7.3's non-omittable stage
    list and the section 7.5 catalogue refusals for omitted
    required stages contradict a label-only floor. Section 7.3
    itself admits this is "the first thing re-audit revisits".
    Picking B3 invalidates the validator the plan builds in
    phases 2–3.
  - **C2 (all in one phase):** the plan gives no single-phase
    acceptance, no sequencing inside the combined phase, and no
    phase gates to check. The builder must write a new plan.
  - **C3 (picker and toggles only):** phases 4 and 6, and the
    section 17 editor and suggestion criteria, are false by
    design. The builder must write a new, smaller plan; following
    section 15 would build the parts C3 drops.
  - **D2 (list only) / D3 (canvas only):** section 17 requires
    creation "in both editor modes". That criterion is false
    under either pick, and D3 additionally breaks the
    browser-first rule the design itself cites for mouse-free
    users.
  - **E2 (repo only) / E3 (user only):** section 6.5, phase 2
    storage and acceptance (both scopes), scope badges, and the
    cross-scope re-validation rule all change. Under E3, a
    shipped PR's flow cannot be reproduced from the repo alone,
    which section 17 snapshot acceptance assumes.
  - **F2 (profile always wins):** section 9.2's order, the
    per-setting winning-source record, and the autonomy-mode
    exception all change, and a per-run flag typed for one run
    is silently ignored — the opposite of what section 9.2
    promises for typed flags.
  - **F3 (configuration always wins):** phase 5 acceptance
    ("presets behave as their section 9.1 tables say") is false
    whenever repo configuration overrides a preset, which is
    the normal case section 9.2 level 3 describes.
  - **G2 (default re-expressed as a profile):** no phase removes
    the legacy path, no phase migrates existing runs, and
    section 8.2's rule (picking `default` is a no-op on the
    legacy path until conformance passes) is replaced. See also
    B6 on rollback.
  - **G3 (default as profile, legacy removed after one
    release):** no phase, release criterion, or deletion step
    exists for the removal (see Major M11). Picking G3 leaves
    the end state unplanned.
  - **H2 (Accuracy optimizes wall-clock speed):** the section
    9.1 Accuracy table (production quality, CDA grading on)
    describes H1, not H2, and the speed mechanisms H2 names
    (parallel task execution, faster models where safe) have no
    phase, no pipeline support named in the plan, and no
    evidence rule. Phase 5 acceptance describes the wrong
    preset under H2.
  - **H3 (quality first, speed second):** the section 9.1 table
    has no tie-break column, so the builder must invent how
    priority order is represented in the preset format and shown
    to the user.

  A gate whose picks mostly require re-design is not an
  implementation gate; it is a second design phase with no plan.

### B6 — Blocker: rollback is not a real plan

- Sections: §17 rollback (lines 1380–1436) against §16 set G
  (lines 1315–1348).
- Persisting, stage 8 N5, with new gaps added.
- Evidence: rollback is "disabling profile selection at run
  creation (a configuration flag, default on) and letting
  in-flight snapshots finish". The flag has no name, no location
  (config file, dashboard setting, or environment), no stated
  effect on already-queued runs, and "default on" is ambiguous
  between "selection enabled" and "disabling enabled". Under G2
  there is no untouched legacy path to return to, so disabling
  selection does not restore old behaviour; under G1 the flag
  itself is still undefined. The plan says nothing about
  suggestion records after rollback (can an approved suggestion
  still be picked? do unapproved drafts stay visible?), nothing
  about a resumed run that must re-validate its snapshot under
  section 7.5 when the store or validator is disabled, and
  nothing about whether the phase 1 schema migration is
  forward-only and whether old code tolerates the new columns.
  "Snapshots are self-contained" is asserted, not shown: a
  snapshot stored on the run record still depends on the
  snapshot-aware lookup built in phase 3 to be read at resume
  time. A rollback paragraph that is correct only under one
  branch of an unpicked option, and names no flag under that
  branch, is not a rollback an operator can execute.

### B7 — Blocker: the phase 2 file list names things that do not exist and omits the mechanisms they need

- Sections: §15 phase 1 (lines 1020–1040), phase 2
  (lines 1041–1064), phase 5 (lines 1102–1118).
- New, verified against the worktree.
- Evidence, three items:
  1. Phase 1 names `src/jev_agent/core/store/migrations/` as a
     new migration directory. That directory does not exist, and
     the existing mechanism is a single file,
     `src/jev_agent/core/migrations.py`, with a `MIGRATIONS`
     list of SQL scripts applied on open. An implementer
     following the plan creates a directory the codebase never
     reads, or must redesign the migration mechanism without
     instruction.
  2. Phase 2 names `src/jev_agent/core/workflow_profiles/` and
     says "module names follow the option-set-A name if it
     differs", but option A is unpicked (B1), so the package
     cannot be named yet. Phase 2 also never names the validator
     file that phase 3 later "changes"
     (`validator.py` appears first in phase 3), so the phase 2
     validator's file, scope, and interface are unknown.
  3. Phase 5 names `src/jev_agent/data/workflow-presets/`.
     There is no `src/jev_agent/data/` directory, no preset
     loader module is named in any phase, and no packaging step
     is named for shipping package data. An implementer must
     invent how presets are loaded and how startup validation
     (which phase 5 tests assume) is wired in. A file list that
     points at non-existent directories without a loader or a
     migration mechanism is not a file list a builder can follow.

### B8 — Blocker: the phase 3 gate contradicts phase 5 acceptance as written

- Sections: §15 phase 3 (lines 1065–1084) against §15 phase 5
  (lines 1102–1118).
- Persisting, stage 8 N2.
- Evidence: phase 3 states the conformance suite "must pass
  before any profiled run beyond tests is allowed". Phase 5
  acceptance requires both presets to "run end to end on the Mac
  dashboard". Presets are profiled runs, so phase 5 either
  violates the gate or is gated on it — and the document never
  says which, nor what happens if the gate fails (fix the
  engine, waive the gate, or stop). Because phases run in order,
  a reader can argue phase 5 implicitly waits for phase 3, but
  the gate's own pass criterion is undefined for non-default
  profiles (B4), so the implicit wait does not resolve anything:
  the gate cannot be shown to have passed for the Efficiency
  preset, which deliberately differs from the legacy path. A gate
  that cannot state its pass condition for the runs it gates
  cannot guard the migration it exists for.

## Majors

### M1 — Major: most phase acceptance criteria are not checkable

- Sections: §15 phases 0–6 (lines 996–1137).
- New.
- Evidence: phase 0 acceptance is that the suite "documents
  today's behaviour" — no cases, no count, no pass condition.
  Phase 5 acceptance is that Efficiency "measurably uses fewer
  rounds and cheaper models than the default on the same
  scenario" — no scenario named, no round or cost threshold, no
  price source (stage 8 N4: stage rows record tokens and time,
  not prices, and Codex rows carry `null` costs, so "cheaper
  models" cannot be measured from stage rows alone). Phase 5
  also requires Accuracy to run "with CDA grading on" on the Mac
  dashboard, while CDA is backend-less on the Mac and records
  `unavailable` with no quality fields — so on the Mac the
  criterion is either unobservable or vacuous. Phase 6 acceptance
  ("a suggestion with reasoning shown can be approved and run")
  can be satisfied by a preset-only suggestion, which section
  12.4 explicitly allows below threshold; nothing in phase 6
  acceptance requires a single usage-based suggestion to exist.
  A phase gate that can pass without delivering the phase's
  purpose is not a gate.

### M2 — Major: phase 4 needs things phase 3 does not provide

- Sections: §15 phase 4 (lines 1085–1101) against §§13.1–13.2.
- New, in addition to B3.
- Evidence: phase 4 names only a new routes file, new templates,
  and new static files. It omits the wiring the dashboard
  pattern requires: registration in `dashboard/app.py` (which
  imports each `routes_*` module), a navigation entry
  (`dashboard/nav.py`), and the checked-form / off-loopback
  token gating that section 13.1 says every new route must
  follow. It also omits the run-creation picker: section 13.1
  puts a profile picker on `/runs/new`, and no phase owns that
  picker (phase 2 lists creation paths, phase 5 lists "preset
  picker entries" — neither is the picker itself). An implementer
  building phase 4 from its file list alone will produce routes
  that are not registered, not navigable, and not connected to
  run creation.

### M3 — Major: presets are ordered after the editor although they do not depend on it

- Sections: §15 phases 4–5 (lines 1085–1118) against §9.1.
- Persisting, stage 8 N10, stated here as a dependency finding.
- Evidence: presets are data in the profile format (section
  9.1); they do not depend on the editor. The plan nevertheless
  puts presets in phase 5, after the editor in phase 4, and after
  the engine in phase 3. That ordering is not a false dependency
  that blocks the build, but it is a real dependency error in
  the other direction: the plan imposes an unnecessary
  dependency (editor before presets) that delays the part that
  answers the section 1 problem — a cheaper or stricter run —
  until after the most expensive part. The brief carried
  "presets + picker + measurement first, editor later" as the
  lens consensus; section 15 orders it the other way without
  giving a reason. An implementer cannot tell from the plan
  whether presets may be pulled forward, and pulling them
  forward changes phase 4 and 5 acceptance.

### M4 — Major: the validator is built twice with no stated boundary and no re-check

- Sections: §15 phase 2 (lines 1041–1064) against §15 phase 3
  (lines 1065–1084) and §7.5.
- Persisting, stage 8 N9.
- Evidence: phase 2 creates the shared validator alongside
  storage, and phase 2 acceptance includes create, version,
  archive, and delete. Phase 3 "changes" the validator to the
  full catalogue. The plan never says what the phase 2 validator
  validates — schema only, a catalogue subset, or the full
  catalogue early — so phase 2 acceptance runs against unknown
  validation strength. Nothing names a re-check of phase 2
  artefacts (stored profiles, archived versions) against the
  phase 3 full catalogue, so a profile that passed phase 2 could
  fail phase 3 with no step that catches it. The editor in phase
  4 is then built against the phase 3 validator while the store
  it edits was accepted under the phase 2 validator. One
  validator, built in two phases with no boundary, is how the
  editor-versus-executor drift the design fears reappears inside
  the plan itself.

### M5 — Major: phase 6 can start and pass without any goal-labelled evidence existing

- Sections: §15 phases 1, 5, 6 (lines 1020–1040, 1102–1137)
  against §12.1 and §12.3.
- Persisting, stage 8 N3, stated here as an entry-criterion
  finding.
- Evidence: section 12.1 excludes every run whose goal label is
  `null` from goal-conditioned evidence, and section 12.3
  requires at least 10 runs for a usage-based suggestion. Goal
  labels arrive with profiles and presets: phase 1 rows for
  legacy runs carry goal `null` by construction, and a
  preset-assigned goal label only exists once presets land in
  phase 5. Phase 6 has no entry criterion that checks labelled
  run volume, so phase 6 can start on schedule, pass its
  threshold tests (which only prove below-threshold requests
  return preset-only mode), and ship without ever producing a
  usage-based suggestion. The plan's phase order looks like it
  resolves the measurement-first dependency because stage records
  come first, but the dependency that actually gates suggestions
  — labelled, comparable runs — is not satisfied by stage
  records alone, and no phase checks it.

### M6 — Major: creation paths are listed but not specified, and snapshot timing is undecided

- Sections: §15 phase 2 (lines 1041–1064) against §13.1 and §17.
- Persisting, stage 8 N12.
- Evidence: phase 2 requires the same resolver on five creation
  paths (dashboard `/runs/new`, CLI run creation, queue add,
  recurring queue items, chat-started runs) but names no files
  for three of them (queue, recurrence, chat) and section 13.1
  specifies a picker only for `/runs/new`. Nothing says whether
  a queued run snapshots its profile at queue time (the snapshot
  can go stale against repo configuration before the run starts
  and then fail run-start re-validation) or at execution time
  (the run executes a version the user never saw). Section 17
  acceptance tests no queued or chat-started path. An
  implementer must invent both the file changes and the timing
  rule for paths that can silently go stale — the section 4
  item 7 failure mode (invisible precedence) on the paths the
  plan leaves untested.

### M7 — Major: the test matrix names tests whose inputs, fixtures, and code do not exist in the plan

- Sections: §17 test matrix (lines 1380–1436) against §15.
- New, in addition to B4.
- Evidence: editor tests require list mode and canvas mode to
  "produce identical profiles for the same inputs", but no
  input corpus is named. Suggestion tests cover thresholds,
  approval, and reason-line honesty, but no test covers
  cross-repo aggregation, the dedup rule ("not re-offered
  without new evidence" — what counts as new evidence?), or
  approval-time staleness (a draft approved after new runs
  arrive is approved on stale reason lines). Snapshot tests
  require resumed runs to keep their snapshot, but no phase
  names the resume path or its test fixture. Preset tests
  require a preset referencing a removed model to "fail loudly
  at startup checks", but no phase builds startup-check code —
  section 9 says presets are validated "at save and at run
  start", which is a different timing from startup. Each of these
  is a test an implementer cannot write from the plan without
  first deciding the rule the test is supposed to check.

### M8 — Major: work the plan needs is assigned to no file at all

- Sections: §15 (lines 996–1137) against §§10.3, 13.1, 14, 19.
- New.
- Evidence, in addition to B2 and B7: (a) suggestion-record
  storage — phase 6 names the engine package but no table, file,
  or record schema for suggestion records with an
  approve/discard lifecycle; (b) the 64 KB profile size cap from
  section 14 — no phase names where the cap is enforced; (c)
  the SITEMAP merge from section 19 (this design and the
  parallel #366 design both add a SITEMAP, to be merged by
  hand) — no phase or step names it; (d) CLI wiring — phase 2
  names `src/jev_agent/cli/` as a directory, not a file, while
  the CLI in code is split across `run_cmds.py`, `wiring.py`,
  and others, none named. A plan whose file list omits whole
  work items forces the implementer to discover them mid-phase,
  which breaks the phase acceptance that was written without
  them.

### M9 — Major: phase 0 breaks the phase rule it states, and the two conformance suites are one suite or two

- Sections: §15 introduction and phase 0 (lines 996–1019)
  against §15 phase 3 (lines 1065–1084).
- Persisting, stage 8 N8.
- Evidence: section 15 states "a phase does not start until the
  previous phase's acceptance checks pass, except phase 0's
  contract work, which is documentation plus the conformance
  suite skeleton and may overlap phase 1". The contract (floor
  table, knob list, stage/gate catalogue) is what phase 1
  measurement is built against, so the overlap means
  measurement is built while its contract is still being
  written. Separately, phase 0 creates a "conformance suite
  skeleton" that documents today's behaviour, and phase 3
  creates the conformance suite that compares legacy against
  `default` preset paths. The plan never says whether these are
  one suite grown in two phases or two suites, what the skeleton
  contains, or what phase 0 acceptance requires beyond
  documenting behaviour. The plan's first step is therefore both
  exempt from its own gate and undefined in its output.

### M10 — Major: phase 1 dashboard work is specified as a directory, not as files

- Sections: §15 phase 1 (lines 1020–1040).
- New.
- Evidence: phase 1 lists `src/jev_agent/dashboard/` (change,
  views only) for the run page per-stage rows and the Spend page
  aggregates. The dashboard in code is split across `views.py`,
  `templates/run.html`, `templates/spend.html`, and route
  modules; none is named. The acceptance rule (stage rows sum to
  recorded totals within completeness rules; a `null`-token stage
  is never displayed as zero) needs a display rule per template,
  and the plan gives the implementer no file to put it in and no
  way to know whether "views only" excludes the route modules
  that feed those templates. The same vagueness recurs in phases
  5 and 6, which list `src/jev_agent/dashboard/` (change) with
  no files. A directory is not a file list.

### M11 — Major: option G3 has no removal phase, so its end state cannot be built within the plan

- Sections: §16 set G, option G3 (lines 1315–1348) against §15
  (lines 996–1137).
- New.
- Evidence: G3 is "G2's end state, reached in two steps — first
  G1, then, after one release in which profiled defaults ran
  cleanly in tests and on the Mac dashboard, the legacy path is
  removed". Section 15 has six phases and none removes the
  legacy path, none defines the release criterion ("ran cleanly"
  — which tests, which Mac runs, over what period), and none
  deletes the legacy code or its tests. A builder who picks G3
  finishes phases 0–6 and still has not delivered G3. The option
  is presented as pickable, but the plan contains no step that
  completes it.

### M12 — Major: phase dependencies are partly false and partly missing

- Sections: §15 (lines 996–1137).
- New, summarising the dependency check the task asked for.
- Real dependencies the plan gets right: phase 4 (editor) needs
  phase 2 (store) and phase 3 (validator); phase 6 (suggestions)
  needs phase 1 (stage records). False dependency: phase 5
  (presets as data) is ordered after phase 4 (editor) although
  presets do not need the editor (M3). Missing dependencies:
  phase 4 needs the phase 6 engine for its suggestion routes
  (B3) and needs assurance computation, which has no phase
  (B2); phase 5 needs a price source for "cheaper models" that
  no phase builds (M1, stage 8 N4); phase 6 needs goal-labelled
  run volume that no phase checks (M5); phase 2 needs the
  section 16 gate, which no phase schedules (B1). A phase plan
  with a false dependency, two inversions, and three missing
  dependencies cannot be scheduled as written.

## Nits

### N1 — Nit: phase 4 hardcodes names that option set A has not picked

- Sections: §15 phase 4 (lines 1085–1101) against §16 set A.
- Persisting, stage 8 Nit 1.
- Evidence: phase 2 carries a note that module names follow
  option A; phase 4 carries no such note but already names
  `routes_workflows.py` and `templates/workflows/`. If A2 wins,
  those names are wrong on arrival.

### N2 — Nit: phase 3 is titled "Graph engine" in a document that rejects a graph interpreter

- Sections: §15 phase 3 (lines 1065–1084) against §8 and §18.
- Persisting, stage 8 Nit 2.
- Evidence: sections 8 and 18 reject a graph interpreter
  replacing `_advance`. Phase 3 is titled "Graph engine and
  shared validator". The step's content is the snapshot-aware
  lookup, not a graph engine; the title should say so, because a
  future reader will take the phase title as the scope.

### N3 — Nit: the phase rule's exception is stated in the same breath as the rule

- Sections: §15 introduction (lines 996–1006).
- Persisting, stage 8 Nit 3.
- Evidence: "A phase does not start until the previous phase's
  acceptance checks pass, except phase 0's contract work, which
  is documentation plus the conformance suite skeleton and may
  overlap phase 1." State the overlap as the rule and drop the
  absolute form. See Major M9 for the substantive consequence.

### N4 — Nit: CLI commands are listed as phase work the document admits are not required

- Sections: §15 phase 2 (lines 1041–1064) against §3.
- Persisting, stage 8 Nit 4.
- Evidence: phase 2 includes `jev workflow` commands (list,
  show, validate) and then states "the CLI is not required for
  acceptance". Work that is not required for acceptance should
  not sit in a phase whose acceptance it does not serve.

### N5 — Nit: no test location is assigned for any suite

- Sections: §15 phases 0, 3, 5, 6 and §17 test matrix.
- New.
- Evidence: every suite is listed as `tests/` (new). The repo
  has `tests/unit/`, `tests/integration/`, and `tests/live/`.
  The plan never says which suite lives where, so the
  implementer guesses — and a conformance suite placed under
  `unit/` will be run (and will fail, for the B4 reason) under
  different conditions than one placed under `live/`.

### N6 — Nit: preset validation timing is stated three ways

- Sections: §9 (save and run start), §15 phase 5 (startup
  checks), §17 (validation at run start for on-disk edits).
- New.
- Evidence: section 9 says presets are validated at save and at
  run start; phase 5 tests require a stale preset to fail
  "loudly at startup checks"; section 13.2 requires re-validation
  at run start. The plan never reconciles these into one rule
  (startup check plus run-start check, or run-start check only),
  so the implementer must decide which check the phase 5 test is
  actually testing.

## What section 15 alone does and does not give an implementer

- File list: partial. Phases 2–4 name real packages and some
  real files (`pipeline.py`, `records.py`, `validator.py`,
  `routes_workflows.py`), but with wrong directories (Migrations,
  B7), unpicked names (package, B1/B7), directory-level entries
  where files are needed (dashboard, M10; CLI, M8), and whole
  work items assigned to no file (assurance, suggestion records,
  preset loader, SITEMAP, M8).
- Rationale per file: mostly absent. No phase states why its
  files are the right ones, or what happens if a file is skipped
  — except the phase 2 note that the CLI is not required for
  acceptance, which undercuts its own inclusion (N4).
- Entry/exit criteria: not checkable as written (M1, M5, M9).
  Criteria use unmeasured thresholds ("measurably fewer"),
  unnamed scenarios ("the same scenario"), and outcomes no phase
  produces (label rendering, B2).
- Dependencies: partly real, partly false, partly missing (M12).
- Option sets: not implementable as stated except on the
  proposed-answer branch, and even there only after B2, B4, and
  B7 are fixed (B5).
- Test matrix: not executable for conformance (B4) and
  underspecified for editor, suggestion, snapshot, and preset
  tests (M7).
- Rollback: not real (B6).

## What must change before implementation, in readiness order

1. Run the section 16 gate, then amend the document by re-audit
   into one branch-specific plan with one acceptance checklist
   per picked branch (B1, B5). Do not start phases on unpicked
   options.
2. Add a phase that builds assurance computation and label
   rendering — run page, board row, PR body via
   `core/ship/pr_body.py` — including legacy runs, or remove
   the section 17 label criterion (B2).
3. Define the conformance harness before phase 3: stub/replay/
   fixed-decision mechanism, scenario definition, and the
   deliberate-difference registry, and state whether phase 5 is
   gated on phase 3 and what happens if the gate fails (B4, B8).
4. Correct the file list: the real migration mechanism
   (`core/migrations.py`), the validator file in phase 2 with
   its scope, the preset loader and startup-check code, the
   dashboard files per phase, the CLI files, app/nav
   registration, and suggestion-record storage (B7, M2, M8, M10).
5. Fix phase order and ownership: move suggestion routes to
   phase 6 or scope phase 4 to the editor alone (B3); decide
   whether presets precede the editor (M3); give the
   run-creation picker an owning phase (M2); add a phase or
   release criterion for G3 removal (M11).
6. Define rollback as an executable procedure: flag name and
   location, behaviour for queued and in-flight runs, for
   suggestion records, and for resume-time re-validation, under
   each G branch (B6).
7. Add entry criteria that check what later phases actually need:
   labelled run volume before phase 6 (M5), and a price source
   or a dropped "cheaper models" claim before phase 5 (M1).
8. Re-audit after those changes, since the document requires
   re-audit when any option set picks otherwise, and since the
   findings above concern sections 15–17, which the earlier
   stages did not cover as a plan.
