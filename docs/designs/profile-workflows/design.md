# Profile-based process flows (Baton issue #368)

Design phase only. No production code is changed by this document.
Implementation follows the decided picks in section 16 (Decided picks subsection, 2026-10-29, confirmed by re-audit decisions).

Working term: this document calls the new concept a **workflow profile**
(a profile of the pipeline). That term collides with three existing
uses of "profile": learning profiles, workspace/server profiles, and
the `profile` field on queue items (the workspace-profile name a
queued run carries). Section 16's Decided picks adopt A1: the final name is workflow profile and the route is /workflows. Nothing in the mechanism depends on the word, but every user-facing string that uses the word (routes, directory names, messages, labels) uses the A1 names — section 5 lists the strings a rename must touch.

Decided in sections 6–15 (Decided picks subsection at the end of section 16, 2026-10-29, confirmed by the re-audit decisions that follow it): scope is both scopes (section 6.5; set E1), precedence is the explicit order table (section 9.2; set F1), enforcement is the mix of refusals and labels (sections 7.3, 10.2, 10.3; set B1), routes and names are the workflow-profile names (section 13.1, phase 5; set A1), the default stays hardcoded with a conformance guard (section 8.2, phase 3; set G1), and Accuracy optimizes quality first, speed second (section 9.1; set H3). Everything else in sections 6–15 is stated as the design. A section marked "(normative)" states the rule;
other sections summarise it and point back, so a restatement never
wins over the normative statement.

## 1. The problem

Baton turns a natural-language idea into reviewed, tested code through a
pipeline of AI agents. Today that pipeline is one fixed chain, identical
on every run. A user who wants a cheaper run cannot ask for a shorter
flow. A user who wants a stricter run cannot ask for a fuller one.
Nothing suggests a flow from how that user actually works. The cost of
the fixed chain is paid on every run whether or not the run needs it,
and the strictness of the fixed chain is unavailable to anyone who
would want more of it — because there is no way to ask.

The proposal adds three things: named, versioned workflow profiles a
run can pick; a flowchart editor in the dashboard for building and
rearranging those profiles; and flow suggestions drawn from the user's
own usage history, conditioned on whether their goal is saving tokens
or getting the most careful result. Today's pipeline stays the default
and stays unchanged for existing runs.

## 2. What exists today

This section states only what the code shows, at main `c1078a4`. File
paths are relative to the repo root.

### 2.1 The pipeline is a state machine with handlers, not a table

`RunState` (`core/models.py`) carries 19 values:

* 7 work states handled by the `_advance` loop (`core/pipeline.py`):
  `INTAKE`, `PLANNING`, `VALIDATING`, `DESIGNING`, `EXECUTING`,
  `INTEGRATING`, `SHIPPING`.
* 7 gate states where a run waits for a person: `AWAITING_PLANNING`,
  `AWAITING_VALIDATING`, `AWAITING_DESIGNING`, `AWAITING_APPROVAL`
  (the plan gate, and the EXECUTING gate in `ASK_EVERY_STEP` mode),
  `AWAITING_INTEGRATING`, `AWAITING_SHIPPING`, `AWAITING_SHIP`.
* 5 stopped states: `DONE`, `NEEDS_HUMAN`, `FAILED`,
  `BUDGET_EXCEEDED`, `CANCELLED`.

`_advance` maps each work state to a handler method (`_intake`,
`_plan`, `_validate`, `_design`, `_execute`, `_integrate`, `_ship`)
and loops until the run reaches a state with no handler. Handlers
choose the next state internally: `_validate` applies the D3 verdict
(approve the plan, revise it, or stop for a human); `_integrate`
merges task branches, runs the integration tests, applies the D6 vote
(ship, add a fix task, or stop for a human), and then applies the ship
score gate. A `Halt` exception stops the run in `NEEDS_HUMAN` or
`FAILED`, with `resume_state` recorded when the run can be resumed.
`_finish` runs the learning step once for a run that ended, when the
run has a learning profile and no learning outcome yet.

Three facts about this machine matter for everything that follows:

1. There is no transition table. A flowchart cannot be executed by
   reading edges off a drawing, because the edges live inside handler
   code. Any profile mechanism must either restructure `_advance` and
   its handlers, or express profiles as parameters the existing
   handlers already understand. Section 8 chooses the second shape and
   says exactly where the first shape would still be needed.
2. Loops are bounded parameters, not graph cycles. Plan revise rounds
   are capped by `[budget] max_plan_rounds` (default 3). Fix tasks for
   failing integration tests are capped by `[integration]
   max_fix_rounds` (default 2). Ship-score rework rounds are capped by
   `[quality] score_max_retries` (default 2). A graph with a cycle
   drawn by hand has no cap unless the design gives it one.
3. Two stages are conditional, not always present. `DESIGNING` runs
   only for UI work (`needs_design`). `SHIPPING` runs only when the
   ship mode asks for it; otherwise a ship vote ends the run at `DONE`
   without shipping. A node list that omits them is not necessarily
   broken, and a node list that includes them unconditionally is not
   necessarily right.

### 2.2 Gates and safety checks are scattered behaviours, not stages

* The plan gate: after `VALIDATING`, D3 approval moves the run to
  `AWAITING_APPROVAL` (or straight to `EXECUTING` when no gate
  applies). `AUTONOMOUS` autonomy mode skips the plan gate.
  `ASK_EVERY_STEP` mode parks the run in a per-step `AWAITING_*` gate
  before each step (`STEP_GATES` in `pipeline.py`).
* The quality gate (`quality/gate.py`, checks in `quality/checks.py`,
  levels in `quality/levels.py`): levels are cumulative — `none`,
  `basic`, `medium`, `production`, `scalable`. The gate runs inside
  `_integrate`. A failed gate forces a fix task, never a ship (issue
  #201). Merge conflicts or failed integration tests also never ship.
* The ship score gate (`_ship_after_score_gate`): a ship vote with a
  score below `[quality] score_threshold` (default 85) goes back for
  rework, up to `score_max_retries` rounds, then stops for a human.
  `score_skip_human` skips the score gate for human-authored code.
* The ship gate: when `[ship] after_approval` is `ask`, a ship vote
  parks the run in `AWAITING_SHIP`, where a person approves, modifies,
  or stops.
* The CDA grade: the Coding Decision Agent grades a diff at the
  quality gate and advises D4 routing. It is off by default, advisory
  only (threshold 0.7, nothing cleared it across 13 real diffs), and
  records `unavailable` with no quality fields when its backend is
  missing. Grades accumulate in an append-only ledger at
  `<repo>/.jev/cda-ratings.jsonl` (`code_orchestrator/ledger.py`).

Existing bypasses, because the safety floor must deal with them:
quality level `none` runs only the task's own test command;
`AUTONOMOUS` skips the plan gate; `score_skip_human` skips the score
gate; a missing ship score still ships; a run that ends at `DONE`
unshipped can still be shipped later by hand. Any floor rule that
calls these stages "non-removable" while leaving these settings in
place is false. Section 10 deals with this instead of pretending
otherwise.

### 2.3 The knobs a preset can actually turn

`config/defaults.py` defines the settings a profile can map onto
without inventing new machinery:

* `[roles.*]` model lists per role (planner, validator, coder,
  reviewer, code_reviewer, learner, designer).
* `[models] planner_policy` (`free_training_ok`, `mixed`,
  `mixed_no_training`, `paid_only`).
* `[quality]` level and `score_threshold`, `score_max_retries`,
  `score_skip_human`.
* `[budget]` per-run and per-task USD caps, `max_plan_rounds`,
  `max_task_attempts`, `test_timeout_s`.
* `[integration]` test command and `max_fix_rounds`. The test
  command itself is not a profile parameter (section 7.4): a run
  always uses the repo's configured command.
* `[cache]` settings, including the semantic section (below).
* `[autonomy]` mode — five values in code (`standard`,
  `autonomous`, `ask_every_step`, `manual`, `medium`) — and the
  `AUTONOMOUS` opt-in flag (`autonomous_opt_in`, explicit opt-in
  only, never the default). `manual` means the run waits in
  `INTAKE` for an explicit instruction; `medium` means the
  standard flow scoped to a folder.
* `[approval] require_plan_approval`.
* `[ship]` `enabled` (a boolean, off by default),
  `require_quality_pass`, and `after_approval` (`merge` or `ask`;
  unset means `ask`). There is no ship mode `off`: "off" in this
  document always means `enabled = false` (see section 7.4).
* `[review] enabled`.
* `[dispatcher.cost]` cost accounting for routing decisions.
* `[learning]` `profile` (default `"default"`) and `enabled`
  (off by default), and the run's learning profile.

A preset that cannot be expressed as values for these knobs, plus
stage inclusion and gate policy (section 7), is not a preset — it is a
new feature, and the design must say so wherever that happens.

### 2.4 Learning profiles already exist, and the name is taken

`core/learning/profiles.py` implements learning profiles: a
`learning_profiles` table with create, list, enable, flush, and delete,
plus a per-profile lesson count. Lessons attach to a profile; the
learning graph tab and the `/profiles` dashboard page show them.
Two further uses of "profile" already exist: workspace/server
profiles (a named server/workspace configuration, looked up via
`servers/profiles.py` and snapshotted to
`.jev/runs/<id>/profile.json` at run creation — the precedent the
new snapshot mechanism follows, with a different field name, see
section 6.4), and the `profile` field on queue items (the
workspace-profile name a queued run carries). A third concept called
"profile" will confuse every user who opens `/profiles` expecting
one kind and finding the other. The design keeps the
learning-profiles concept untouched and gives the new concept its
own name, route, and page (option set A), and its own creation
field name, `workflow_profile` (section 6.1).

The learning step itself (`_finish` plus `core/learning/learner.py`)
distills lessons at the end of a run: what struggled, what worked,
which role it applies to. Lessons are the suggestion engine's
struggle/worked signal, not its cost signal — lessons carry no token
or time data.

### 2.5 Cache: exact serving exists, semantic serving does not

`core/cache.py` serves exact cache entries keyed (among other things)
by repo fingerprint (the identity of the repo a cache entry belongs
to); a key whose repo fingerprint is `unknown` is never served or
stored (issue #201). Semantic similarity (`similar()`, a table scan
over SimHash fingerprints — similarity hashes of cached content)
exists behind `[cache.semantic] enabled`, which defaults to off;
when a repo explicitly enables it, semantic serving happens. Shadow
mode (`log_shadow`) measures whether a semantic hit would have
agreed with the exact answer, without serving anything. Calibration
only ships a threshold where the curve shows at least 0.99 agreement
over up to 1500 decisions, at floors 0.90/0.95/0.98/0.99 (a floor is
the agreement level a threshold must clear before it ships).

Consequences the design honours: "aggressive cache reuse" in a preset
can only mean exact-cache reuse, because exact serving is the only
serving on by default. Shadow data may inform a suggestion ("similar
past work was answered from cache in shadow mode N times"), but it
must never be presented as serving evidence, and it says nothing
about whether skipping a stage is safe. Profiles never enable
semantic serving: a profile's cache mode governs exact serving
only, and a repo that explicitly enabled semantic serving keeps it.
This feature does not change the semantic-serving default.

### 2.6 No per-stage measurement exists

Totals live on the run record. Tokens sit inside attempt `result_json`
or decision `details` JSON, or are `None` — Codex reports no cost, and
Jev decisions carry `usage` with a computed cost only when the
provider reports usage. Gate waiting time is mixed into wall time;
there is no stage record, no stage duration, and no stage token column
anywhere. The dashboard has a Spend page (`/spend`) fed by run-level
views, not by stage records.

This is the design's central prerequisite: no efficiency claim and no
usage-based suggestion can be measured until stage records exist.
Section 11
specifies them, and section 15 phases them before any suggestion that
changes a run.

Why a new table instead of deriving stage numbers from existing
records: attempt records carry start/finish timestamps and cost but
no stage attribution, so a stage's tokens cannot be separated from
another stage's when attempts overlap a gate; decision records carry
no stage either; gate-wait time is derivable in principle from
`AWAITING_*` transitions, but not for the two shared gates
(`AWAITING_APPROVAL` serves both the plan gate and the
`ASK_EVERY_STEP` executing gate, and `AWAITING_SHIP` is a ship
decision, not a step gate); cache and shadow events carry a key hash
and kind, not a stage; and ship-decision timing belongs to no
existing record. Derivation therefore cannot produce the fields
section 11.1 requires (per-stage work vs. gate-wait split,
per-stage cache/shadow attribution, stage-tagged decisions and
attempts, ship-decision timing). The new table is built, and run
totals are cross-checked against it (section 11.3), rather than the
other way around.

### 2.7 Dashboard patterns the editor must reuse

Routes (`dashboard/app.py`) relevant to this feature: `/` (board),
`/runs/new` (POST, creates a run), `/runs/{run_id}` (run page),
`/runs/{run_id}/live` (live fragment), `/queue` and `/queue/add`
(queued runs), `/learning` (learning page), `/profiles` (learning
profiles), `/spend`, `/design-systems` (a user-level library with
list/detail/showcase/code pages — the closest existing model for a
user-level library of named artifacts). Run creation in production
flows through `app/runs.py create_run`, which the dashboard runner,
the scheduler runner, the chat path, the MCP path, and CLI run
creation all call; `Pipeline.start` is the chat-driver and test
path. The design maps profile selection onto that funnel (one
resolver function, called from both entry points) rather than
inventing a second creation path. Forms post through checked forms:
each form carries a token the server checks, and the check applies
to requests from outside the machine (off-loopback token gating).
Any new editor route must follow the same pattern, with
server-side validation before any save.

## 3. Goals and non-goals

Goals:

* A user can pick a workflow profile when creating a run, from the
  browser, and the run executes that profile's flow.
* A user can create, save, and version a workflow profile entirely
  from the browser, with invalid graphs refused in plain language
  at save time and re-checked at creation. "Rearrange" in this
  document means only what the mechanism allows: include or omit
  stages where section 7.3 allows it, and edit parameters and gate
  policies. Stage reordering is refused (section 7.1), and the
  editor offers no reorder control — an attempted reorder is
  refused with the reason, not presented as a feature.
* Two built-in presets exist and behave as their tables say:
  Efficiency (fewer tokens, shorter wall time where history supports
  it) and Accuracy (fullest checking, fullest review).
* A user can ask for a suggested flow for a stated goal, read its
  reasoning with evidence counts, approve it, and run it — and an
  unapproved suggestion changes nothing.
* The default flow is unchanged for existing runs, proven by a
  conformance test that runs the default profile and the legacy path
  against the same cases and requires identical stage sequences and
  outcomes.
* A flow that drops a safety behaviour is either refused at save time
  or visibly labelled for the life of the run (option set B decides
  which, per behaviour class).

Non-goals:

* Changing live semantic serving, the default flow, or the meaning of
  any existing setting for runs that do not pick a profile.
* Silent auto-rearrangement, silent auto-application of a suggestion,
  or a suggestion engine with authority over Jev's decisions (D1, D2,
  D3, D4, D5, D6) or over the learning loop.
* A graph interpreter that replaces `_advance` for all runs. The
  default path stays on existing code until conformance is proven
  (section 8).
* A CLI-first or terminal-dependent editor. The editor is
  browser-first; the CLI may gain `jev workflow` commands later, but
  they are not part of this design's acceptance.
* New model providers, new quality checks, or new worker types. A
  profile references models and checks that already exist.

## 4. Critique first: what can go wrong

The brainstorm lenses found the following failure modes. The design
answers each one in the section cited; an answer that only restates
the problem is marked as such in review and must not survive
resolution.

1. **The drawing can differ from the run.** A 7-box flowchart hides
   loops, gates, conditional stages, and handler-chosen transitions.
   A user saves a drawing, runs it, and gets a different flow.
   Answered by section 7: the editor's node palette is the real
   stage list, loops are bounded parameters (never drawn cycles),
   gates are properties of steps (never free-floating nodes), and
   the editor and the executor run the same validator, so a
   save-time pass and a creation-time pass use the same rules.
   The validator guarantees the static shape only; handler-chosen
   branches (D3 verdicts, D6 votes, score-gate rework) are runtime
   behaviour the drawing never shows, and section 8 names the
   check that applies at each handler move site.
2. **A second state machine can drift from the pipeline.** If the
   profile interpreter drifts from `_advance`, identical-looking
   flows diverge, and the divergence is discovered by users
   mid-run, after tokens are spent. Answered by section 8: one
   shared validator, a conformance suite for the default, and a
   rule that the default stays on the legacy path until the
   interpreter reproduces it.
3. **The safety label can decorate instead of protecting.** A
   "reduced assurance" label that appears on the editor page but
   not on the run page or the shipped pull request changes
   nothing. Answered by section 10: the label travels on the run
   record, renders on the run page, the board row, and in the pull
   request body, and the refused classes block at save time when
   the enforcement option says so. A label alone never blocks a
   ship; the classes that must block are refused, not labelled.
4. **Presets can go stale without warning.** A preset that names a
   model, a quality level, or a cache mode keeps working after
   that model is removed or that check changes meaning, and fails
   only by producing worse runs. Answered by section 9: presets
   are validated at startup and at creation against the live
   configuration, and a stale reference refuses with a named
   reason instead of falling back quietly.
5. **Suggestions can overstate thin data.** Ten runs, three grades,
   and a shadow log become "users like you save 40% with this
   flow". Answered by section 12: minimum-data thresholds,
   preset-only mode below them, evidence counts on every reason
   line, and no percentage that is not computed from measured
   medians.
6. **A profile edit can corrupt a running run.** A run that
   references a live profile changes behaviour mid-flight, and a
   resumed run resumes under a flow it never started with.
   Answered by section 6: runs snapshot an immutable profile
   version at creation; edit creates a new version; delete
   archives.
7. **Precedence can be unclear.** A profile, a workspace profile,
   `jev.toml`, a per-run flag, and an autonomy mode disagree, and
   the winner is whichever the code happens to read last.
   Answered by section 9: one explicit precedence table, applied
   in one place at creation, and the snapshot records the winning
   source per setting.
8. **The editor can exclude people.** A drag-only canvas fails on
   narrow screens and without a mouse, which breaks the
   browser-first law for those users. Answered by section 13: a
   list-based editor that builds the identical profile through
   the identical validator is a first-class path, not an
   afterthought.
9. **Scope confusion.** A profile saved "for this repo" used in
   another repo references settings that repo does not have, and
   fails or silently degrades. Answered by section 6: scope is
   part of identity, and cross-scope use re-validates against the
   target repo's configuration at creation, before anything is
   spent.
10. **Goal words can mislead.** "Performance" is used for both speed
    and quality, so a suggestion conditioned on "performance"
    cannot be checked. Answered by option set H: the human pins
    down what the Accuracy preset optimizes before implementation.

## 5. Concepts and naming

Words used in this document, each with one meaning. "Flow" means
the stored profile definition. The picture in the editor is the
"drawing". The execution is the "run". Code states are `RunState`
values; a document "stage" is one work state in a profile's stage
list; an editor "node" is a stage drawn on screen; a "step" is a
stage together with its gate policy and parameters. A "gate" is
one of three things, and the text says which: a waiting state (an
`AWAITING_*` state a run parks in), a check (the quality gate, the
ship score gate), or a gate policy (section 7.2). "Default" is
also overloaded, so the text distinguishes: the `default` preset
(the built-in profile), the legacy path (today's code path for
runs without a profile), a gate policy value `inherit` (section
7.2), and Baton built-in defaults (precedence level 4, section
9.2). The strings a rename under option set A must touch are:
route paths, template and module names, storage directory names,
the picker/list/badge label text, and every message in the
section 7.5 catalogue.

* **Workflow profile (normative).** A named definition of a run's
  flow: an ordered stage list (section 7), per-stage parameters,
  gate policies, and setting overrides (section 9). A profile is
  data, validated before it is saved and again before it runs. It
  contains no code, no shell commands, and no model names outside
  the configured allowlist (section 14). A profile never names an
  integration test command: a run always uses the repo's
  configured command.
* **Flow version.** One immutable revision of a profile.
  User-scope and repo-scope versions are numbered per profile,
  starting at 1, and never edited in place. Editing a profile
  creates a new version; the old versions remain readable for
  runs that snapshotted them. A user copy of a preset restarts
  numbering at 1 under its new id.
* **Flow snapshot (normative).** The stored full copy of the
  resolved parameter set for one run: the stage list, per-stage
  parameters, gate policies, setting overrides, the per-setting
  winning source (section 9.2), and the assurance computation
  inputs. The snapshot is stored on the run record as one JSON
  column (`flow_snapshot`) plus scalar columns for querying
  (section 6.4). The run executes the snapshot even if the
  profile is later edited, archived, or deleted. A "profile
  reference" is the other sense, and the document uses that term
  for it: an id plus a version pointing at a stored version, with
  no parameters of its own.
* **Assurance.** A run-level value computed from the run's
  effective settings (the snapshot for a profiled run, the run's
  settings for a legacy run): `full` when every safety behaviour
  in section 10 is present, `reduced` when any is absent under
  the label option, plus the list of absent behaviours in plain
  words (section 10.3). The display term everywhere is
  "assurance label": the run page, the board row, and the pull
  request body show the same text.
* **Goal.** A label on a run and on a suggestion request, with
  four values: `efficiency`, `accuracy`, `custom`, or `null`.
  `custom` means a user-stated goal that is neither preset's
  goal. `null` means no goal was stated. The goal recorded for
  evidence is always the user-stated goal (section 12.1); a
  preset's goal label is display only and never counts as a
  stated goal. A run without a stated goal records `null`, and
  the suggestion engine never uses a `null`-goal run as evidence
  for a goal-conditioned recommendation (section 12.1).
* **Suggestion.** A draft profile version plus a reasoning record
  (one reason line per change, each with evidence counts). A
  suggestion is inert until a user approves it, at which point it
  is saved as a new profile version (section 12.4). Approval
  covers one profile version, not all future runs.
* **Preset.** A built-in profile shipped with Baton: `default`
  (today's pipeline, behaviour-identical), `efficiency`,
  `accuracy`. Presets are data in the same format as user
  profiles, validated by the same validator. A preset is
  identified by the Baton release that shipped it (its `release`
  field), not by an integer version; a new release ships a new
  preset release, and a run snapshotted on an old preset release
  keeps running from its snapshot (section 6.3).
* **Workspace profile.** A named server/workspace configuration,
  looked up via `servers/profiles.py` and snapshotted to
  `.jev/runs/<id>/profile.json` at run creation. It is a
  precedence source (section 9.2, tier 3), not a workflow profile.
* **Jev decisions.** D3 is the plan verdict (approve, revise, or
  stop for a human); D4 is routing; D5 is feedback to the next
  attempt; D6 is the ship vote (ship, add a fix task, or stop for
  a human). D1, D2, D7, and D8 exist in code but this design does
  not constrain them, and profiles never override any Jev
  decision.
* **CDA, tier, threshold.** The Coding Decision Agent (CDA) grades
  a diff — a change set — at the quality gate and advises D4
  routing. A tier groups graded diffs by agent/model family. The
  threshold 0.7 is the minimum merge-action confidence to act on
  a grade; nothing cleared it across 13 real diffs, so grading
  stays advisory only.
* **Lease.** Run-execution leases (the `leases.py` store module)
  govern single-writer ownership of a run. Snapshots do not change
  lease ownership, expiry, or recovery; Halt, resume, cancel, and
  lease behaviour are unchanged by profiles.
* **Checked form.** A dashboard form that carries a token the
  server checks before acting, including for requests from
  outside the machine. The browser check is convenience; the
  server check is the rule.
* **Pull request body.** The description text of a pull request,
  where the assurance label renders for shipped results.
* **JSON.** A text format for structured data; profile files are
  JSON files.

## 6. Profile model: identity, storage, scope, snapshot

### 6.1 Identity

A profile has a stable id, a display name, a scope, and a version
number. The id is the identity; the display name is not. Two profiles
may share a display name in different scopes; lists always show the
scope badge next to the name, and runs always reference the id plus
the version, never the name. The id format is
`<scope>:<slug>`, where scope is `repo` or `user` and slug follows
the existing profile-name rule (letters, digits, `-` and `_`,
starting with a letter or digit, at most 40 characters). The id
scheme does not change under option set A: the scope prefix is not
the concept name, so a rename changes routes, directories, and
labels, but not ids. The creation field is named
`workflow_profile`, distinct from the queue item's existing
`profile` field and from the run's workspace profile. A run that
references an id that no longer resolves refuses at creation with
a plain message naming the id and version and telling the user to
pick a current profile, never by substituting another profile.
"Archived" and "deleted" are different states (section 6.3): an
archived profile is hidden from creation lists but its snapshots
still run; a permanently deleted profile has no versions left, and
a run can never reference one, because permanent deletion is
refused while any run references any version.

### 6.2 Storage

* Repo scope: one JSON file per profile version under
  `<repo>/.jev/workflow-profiles/<slug>/v<n>.json`, plus a
  `<slug>/current.json` pointer to the latest version number.
  Repo-scope profiles live with the repo checkout. They do not
  travel via git by default, because `.jev/` is ignored by the
  repo: sharing a repo-scope profile with another machine means
  exporting its JSON from the dashboard (download) and importing
  it there (upload), which the editor supports (phase 5).
* User scope: the same layout under `~/.jev/workflow-profiles/`.
  User-scope profiles are visible in every repo on that machine.
* Built-in presets ship as package data in the Baton package
  (`src/jev_agent/workflow_profiles/presets/`, loaded with
  `importlib.resources`, the same pattern the roles package uses
  for its designer files), identified by release (section 5).
  A preset is never edited in place; a user who edits a preset
  creates a user- or repo-scope copy under a new id.
* Files, not the learning-profiles table, because a profile must
  be reviewable with the repo checkout, exportable as one file,
  and readable without the database: the same-shaped concept uses
  a different mechanism for a stated reason, not by accident.
* The store validates every file on read (schema plus the section 7
  validator). A corrupt or schema-invalid file is reported with
  a plain message naming the profile id and version and saying
  the file cannot be read, and is never executed, never repaired
  without the user saving a new version, and never partially
  applied.

### 6.3 Versioning and deletion

* Creating a profile creates version 1. Editing creates version
  n+1; version n is never modified afterwards.
* Version creation is atomic per step: the writer writes
  `v<n>.json` to a temporary file and renames it into place, then
  writes `current.json` the same way. A crash between the two
  steps leaves `current.json` pointing at the previous version,
  so the new version simply is not current yet. If `v<n>.json`
  already exists when a writer claims version n (two editors
  racing), the writer retries as n+1 instead of overwriting.
* A run snapshots the exact version it was created with (section
  6.4). Old versions are retained while any run references them.
* Deleting a profile archives it: the definition files stay, the
  profile disappears from creation lists, and existing snapshots
  keep running. Permanent deletion is refused while any run
  references any version, and the refusal names the referencing run
  count. Referenced versions are never auto-deleted: when the last
  referencing run is itself deleted (if run retention ever deletes
  runs), the version becomes eligible for permanent deletion, and
  no background cleanup is specified — deletion stays a user
  action.
* A preset is identified by the Baton release that shipped it.
  Upgrading Baton never rewrites a run's snapshot and never changes
  what a stored snapshot means; a newer release ships a newer
  preset release. Old preset files are not retained as runnable
  presets after an upgrade — only snapshots on runs keep the old
  meaning.

### 6.4 Snapshot on the run

At run creation, the chosen profile version is resolved to its full
parameter set and that resolved set is stored on the run record as
the flow snapshot (section 5): one JSON column (`flow_snapshot`)
holding the stage list, per-stage parameters, gate policies,
setting overrides, the per-setting winning source map (section
9.2), and the assurance computation inputs, plus scalar columns
for querying (`workflow_profile_id`, `workflow_profile_version`,
`goal`, `preset_goal`, `assurance`, `assurance_absent`). The
resolution order is the section 9.2 precedence table, applied once
at creation: per-run flags, then the profile version, then repo
configuration and the workspace profile, then Baton defaults.
Because the snapshot is the complete resolved set, the executor
reads only the snapshot and never falls back to repo configuration
at run time — there is nothing left to fall back to. Handlers read
snapshot fields, not the run record's legacy duplicates: for a
profiled run, the snapshot is the only source; the run record's
`quality_level`, `ship_mode`, and `learning_profile` fields keep
their meaning for legacy runs only. The executor reads only the
snapshot, never the live profile. Editing, archiving, or deleting
the profile afterwards cannot change the run. Resuming a
`NEEDS_HUMAN` run resumes under the same snapshot (`resume_state`
already records where the run stopped; the snapshot records what
flow it runs under). The run page displays the profile id,
version, goal, and assurance label for the life of the run.

### 6.5 Scope rule (decided: E1, both scopes)

Both scopes exist. A run created in a repo may pick a repo-scope
profile from that repo or any user-scope profile. A repo-scope
profile may not be picked from another repo. When a user-scope
profile is used in a repo, every section 7.4 parameter is
re-validated against that repo's configuration at creation, before
anything is spent: role models must exist in that repo's role
configuration; quality levels, cache modes, and ship settings must
parse against their enums; planner policy must be a known value;
the learning profile must exist; CDA availability must match the
grading setting (grading `on` with no backend refuses at creation,
it does not silently run ungraded). A failure refuses creation
with the offending setting named. The creation-time re-check
repeats this validation for profiles edited on disk between save
and creation. If option set E narrows scope to one kind, this
section narrows with it by re-audit; the snapshot mechanism does
not change.

### 6.6 Schemas (normative field lists)

Profile JSON (one file per version; required fields marked *):

* `id`* (string, `<scope>:<slug>`), `display_name`* (string),
  `scope`* (`repo` or `user`), `version`* (integer, from 1;
  presets carry `release`* (string) instead of `version`).
* `stage_list`* (array of work states, a subsequence of the
  canonical order, section 7.1).
* `gate_policies`* (map from stage to `inherit`, `require`, or
  `skip`; every stage in `stage_list` has an entry; `INTAKE`
  must not carry a policy — it has no gate state).
* `designing`* (`when_needed` or `never`).
* `max_plan_rounds`, `max_fix_rounds`, `score_max_retries`,
  `max_task_attempts` (integers, section 7.3 ceilings apply).
* `quality_level`, `score_threshold`, `score_skip_human`.
* `role_models` (map from role to model name; empty means repo
  configuration).
* `planner_policy` (one of the four values, or absent meaning
  repo configuration).
* `cache_mode` (`exact` or `exact_off`).
* `cda_grading` (`off` or `on`).
* `ship_enabled` (boolean), `after_approval` (`merge`, `ask`,
  or absent meaning `ask`).
* `learning_profile` (string, or absent).
* `goal` (`efficiency`, `accuracy`, `custom`, or absent).
* `priority` (string, optional, display only; Accuracy carries "quality first, speed second").
* `origin` (`user` or `suggested`; suggestions record
  `suggested` here on approval, section 12.4).

Snapshot JSON (`flow_snapshot` column): the resolved values of
every field above, plus `resolved_from` (a map from each setting
to its winning source: `flag`, `profile`, `repo`, `workspace`,
or `default`), plus `workflow_profile_id`,
`workflow_profile_version` (or `workflow_profile_release` for
presets), and `assurance_absent` (a list
of absent behaviours in plain words). Unknown fields refuse
rather than being dropped.

Suggestion record JSON (stored under the profile's directory in
`suggestions/<suggestion-id>.json`): `id`,
`from_workflow_profile_id`,
`from_workflow_profile_version`, `goal` (the stated goal), `draft`
(the draft profile JSON), `reasoning_lines` (each with change,
evidence counts, sample size, window, completeness flag, and what
would prove it wrong), `thresholds_met` (map), `status`
(`pending`, `approved`, or `discarded`), `dedup_key` (a hash of
from-profile version plus change set), and `evidence_window`
(the run set the reasoning used).

## 7. The profile language: what a profile may express

A profile is a stage list plus parameters. It is not a general
graph language. This restriction is the design's central rule: the
stages have data dependencies (a plan must exist before it is
validated; code must exist before it is integrated), so an
arbitrary edge order is not a flow at all, and a validator that
accepted one would either refuse it at run time (after the user
believed it saved) or run something different from the drawing.

### 7.1 Stage list and canonical order

The node palette is exactly the 7 work states, in canonical order:

```
INTAKE -> PLANNING -> VALIDATING -> DESIGNING -> EXECUTING
    -> INTEGRATING -> SHIPPING
```

A profile's stage list keeps this fixed order and may leave some
stages out (a subsequence of this order). Reordering two stages is
refused: the validator's message names the pair and the dependency
in plain words ("Execution cannot run before validation: execution
needs the approved plan."). Omitting a stage is allowed only where
section 7.3 says so. The editor still draws nodes and edges — the
drawing is the stage list rendered as a chain — but an edge that
skips backwards or jumps over a required stage is a validation
error at save time, not a shape the executor must interpret.

### 7.2 Gates are properties of steps, not nodes

Each work step carries a gate policy: `inherit` (the existing
behaviour for that step under the run's autonomy mode), `require`
(park in the step's waiting state even when the mode would not),
or `skip` (run the step without parking, only where the existing
rules already allow skipping). A gate is therefore never orphaned,
never duplicated, and never drawn as a separate node that a user
could delete without understanding what was deleted. The editor
shows each step's gate policy on the step itself.

Gate-policy table (normative). A gate record carries the step it
parks for (`parked_step`), so the two parkings that share
`AWAITING_APPROVAL` are distinguishable:

| Step | Waiting state | `inherit` | `require` | `skip` |
|---|---|---|---|---|
| INTAKE | none (no gate state exists) | no parking | refused at save: "Intake has no waiting state, so it cannot carry a gate policy." | refused at save, same reason |
| PLANNING | AWAITING_PLANNING | park only in ASK_EVERY_STEP mode | park always | park never; refused where ASK_EVERY_STEP already parks (an explicit human gate stands) |
| VALIDATING | AWAITING_VALIDATING (step gate) and AWAITING_APPROVAL (plan gate after VALIDATING) | step gate parks only in ASK_EVERY_STEP; plan gate parks when plan approval applies (section 10.1 item 5) | both park always | step gate skipped where rules allow; plan gate never skipped by `skip` when plan approval applies |
| DESIGNING | AWAITING_DESIGNING | park only in ASK_EVERY_STEP mode | park always | park never where ASK_EVERY_STEP parks |
| EXECUTING | AWAITING_APPROVAL (ASK_EVERY_STEP executing gate; `parked_step` = EXECUTING, distinct from the plan gate) | park only in ASK_EVERY_STEP mode | park always | park never where ASK_EVERY_STEP parks |
| INTEGRATING | AWAITING_INTEGRATING | park only in ASK_EVERY_STEP mode | park always | park never where ASK_EVERY_STEP parks |
| SHIPPING | AWAITING_SHIPPING (step gate) | park only in ASK_EVERY_STEP mode | park always | park never where ASK_EVERY_STEP parks |

The ship decision waiting state, AWAITING_SHIP, is not a step
gate and no step policy governs it: it parks exactly when
`after_approval` is `ask`, regardless of the SHIPPING step's
policy. `skip` never overrides an explicit human gate the user
asked for elsewhere (a per-run flag, `require_plan_approval`, or
an ASK_EVERY_STEP parking), and never skips the ship decision
gate when `after_approval` is `ask`. Autonomy cases, in plain
form: the user asked for a gate → the run parks; the run is
AUTONOMOUS → the run does not park; the profile says `require`
and the run is AUTONOMOUS → creation refuses with the message
"Autonomous runs do not park for approval. Pick a gated mode or
drop the required gate."; the profile says `skip` and an explicit
gate applies → the gate stands. MANUAL runs apply the snapshot
when the run is advanced; MEDIUM runs apply the snapshot within
their folder scope, which the profile does not change.

### 7.3 What may be omitted, and what may not

* `DESIGNING` and the `designing` parameter (normative). The
  stage list includes `DESIGNING` unless `designing` is `never`.
  Under `when_needed` (the default), the stage list contains
  `DESIGNING` and the executor enters it only when the run has UI
  work (`needs_design`); for non-UI runs the stage row records
  outcome `skipped`. Under `never`, the stage list omits
  `DESIGNING`, and a handler move into `DESIGNING` halts the run
  with a named error (section 8.1) instead of stalling. Design is
  not a safety behaviour, so omitting it never reduces assurance.
* `INTAKE`, `PLANNING`, `VALIDATING`, `EXECUTING`, `INTEGRATING`
  may not be omitted (normative list; section 10.2 points here
  and does not restate it). A profile that omits one is refused
  with the dependency named. Under option set B3 (label-only),
  this stage list stays refused: B3 covers behaviour classes
  (quality level, score-gate flags), not stage omissions. A
  label-only floor with a non-omittable stage core is the stated
  shape of B3, not a contradiction of it.
* `SHIPPING` may be omitted only when shipping is off, meaning
  `[ship] enabled = false` (the run ends at `DONE` after a ship
  vote, as today). Omitting `SHIPPING` while `enabled` is true
  with `after_approval` `merge` or `ask` is refused as
  incoherent.
* Loops are parameters, never drawn cycles: `max_plan_rounds`,
  `max_fix_rounds`, `score_max_retries`, `max_task_attempts`.
  Round-cap ceilings (normative mapping): `max_plan_rounds` is
  bounded above by `[budget] max_plan_rounds`;
  `max_fix_rounds` by `[integration] max_fix_rounds`;
  `score_max_retries` by `[quality] score_max_retries`;
  `max_task_attempts` has no separate ceiling — it is the
  `[budget]` setting itself, so a profile value replaces it.
  Every cap is never below 1 where the step needs at least one
  attempt. A profile cap above the target repo's ceiling refuses
  at save (repo scope) or at creation re-validation (user scope,
  section 6.5), naming the ceiling — it is never silently
  clamped. A drawn cycle is refused with "Flows do not loop by
  drawing. Set the round limit on the step instead."

### 7.4 Parameters a profile carries

* Stage list (section 7.1) and gate policy per step (section 7.2).
* `designing`: `when_needed` or `never`.
* Round caps (section 7.3).
* Quality level (`none` only when shipping is off — see section
  10), `score_threshold`, `score_max_retries`, `score_skip_human`
  (only within the floor rules of section 10).
* Role model overrides: per role, a model name that already exists
  in that repo's role configuration. An unknown model name refuses
  with the model and role named. A profile never invents a model
  or a provider.
* Planner policy (`free_training_ok`, `mixed`, `mixed_no_training`,
  `paid_only`). A user profile may set any value (the user's own
  choice); a preset never narrows the repo's policy (section 9.1).
* Cache mode: `exact` (default) or `exact_off`. `exact_off`
  stops exact serves only: it leaves the repo's semantic-serving
  flag untouched, and shadow logging continues under `exact_off`
  because shadow mode measures rather than serves. There is no
  semantic serving mode to select, because profiles never enable
  semantic serving; a profile that names one is refused.
* CDA grading: `off` (default) or `on` (advisory only; the threshold
  stays 0.7; grading never blocks or auto-merges). Grading `on`
  with no backend refuses at creation; it does not run ungraded.
* Ship settings: `ship_enabled` (boolean) and `after_approval`
  (`merge`, `ask`, or absent meaning `ask`). "Ship mode off" in
  this document always means `ship_enabled = false`.
* Learning profile name (optional): the learning profile whose
  lessons the run reads and to which its outcome is distilled. An
  unknown learning profile refuses with the name, whether or not
  learning is enabled — a saved reference must resolve. Naming a
  learning profile does not enable learning: learning runs only
  when `[learning] enabled` is set; otherwise the run's existing
  setting (default `"default"` profile, learning off) stands.
* Goal label (recorded, section 5): `efficiency`, `accuracy`, or
  `custom`. The goal label changes nothing about execution by
  itself; it is the suggestion engine's training signal and the
  preset's documented intent.

### 7.5 The invalid-graph catalogue

The validator refuses each of these shapes at save time, and the
creation-time re-check repeats the full catalogue for profiles
edited on disk between save and creation. Every message states
the problem, the reason, and the fix, using the plain stage names
the editor shows (intake, planning, validation, design,
execution, integration, shipping):

* Unknown stage: "Unknown stage '<name>'. The stages are intake,
  planning, validation, design, execution, integration, shipping.
  Remove the unknown stage or spell it as shown."
* Reordered stages: "Execution cannot run before validation:
  execution needs the approved plan. Put validation before
  execution."
* Omitted required stage: "Validation cannot be removed:
  integration needs a validated plan. Add validation back."
  (One message per omitted required stage, each naming its
  dependency; the required list is section 7.3.)
* Drawn cycle: "Flows do not loop by drawing. Set the round limit
  on the step instead."
* Gate without its stage: a file that carries a gate policy for a
  stage that is not in the stage list is a schema error:
  "The gate policy for <stage> has no stage to attach to. Add
  the stage back, or remove its gate policy."
* DESIGNING/SHIPPING incoherence: "Shipping is omitted but
  shipping is on. Turn shipping off, or include shipping."
* Unknown model, quality level, cache mode, ship setting, or
  learning profile: named in the refusal, with the allowed values
  listed and the fix stated ("pick one of the listed values").
* Empty stage list, duplicate stage, or missing fields: "A flow
  needs at least intake, planning, validation, execution, and
  integration, in that order. Add the missing stages."

Validation runs in one place, shared by the editor and the
executor: the module is
`src/jev_agent/core/workflow_profiles/validator.py` (the package
name follows option set A), and it exposes one function that
validates profile-format JSON. The editor calls it on drafts
before saving; the executor calls it on the stored profile
version before resolving the snapshot, and then checks the
resolved snapshot fields against the same schema. Live checks
while editing (schema parse, unknown names against current
configuration, duplicate stages, empty list) are a subset of the
same catalogue, not a second validator: the live list is exactly
those catalogue entries, and the save check runs the rest.
Because the editor and the executor run the same validator, a
save-time pass and a creation-time pass use the same rules, and
a creation-time failure names the changed setting. A profile can
still go stale after save (a model removed, a check renamed);
that is why the creation-time re-check exists, and why section 9
requires preset validation at startup as well as at creation.

## 8. Execution: profiles as resolved parameters, not a new interpreter

### 8.1 The mechanism

At run creation, the chosen profile version is resolved (section
6.4) into a frozen parameter set on the run record. `_advance`
keeps its handler mapping, and each handler keeps owning its
outcome (D3 verdicts, D6 votes, score-gate outcomes, Halt
conditions). What changes is narrow:

* The step lookup consults the run's snapshot stage list instead of
  the hardcoded handler map: a step not in the snapshot is never
  entered, and a step in the snapshot runs its existing handler.
* Gate parking consults the snapshot's gate policy per step plus
  the existing autonomy rules, through the existing `STEP_GATES`
  and `execution_gate_target` helpers — the helpers gain an
  optional snapshot parameter (defaulting to no snapshot, which
  preserves legacy behaviour); they do not gain a second gate
  system. The legacy path's behaviour is unchanged; only the
  helper signatures are extended, with a defaulted parameter.
* Round caps, quality level, score settings, ship settings, cache
  mode, and role model overrides are read from the snapshot only.
  There is no run-time fallback: precedence was applied once at
  creation (section 6.4), and the snapshot is complete.
* Halt, resume, cancel, and lease behaviour are unchanged: a halted
  run resumes under its snapshot; a cancelled run stays cancelled;
  the learning step in `_finish` runs under the snapshot's learning
  profile setting.

Handlers do not return "next state" values for the profile to map,
because they never did: they move the run themselves. The profile
constrains which steps exist and with what parameters; it does not
re-wire handler outcomes. A profile that tried to express "after a
failed quality gate, skip to SHIPPING" has no language for it — the
D6 hard limits (section 2.2) are handler behaviour, not profile
content, and no profile can override them. Because handlers move
the run themselves, the snapshot check applies at every move site,
not only at the step lookup (normative table):

| Move site | Snapshot check |
|---|---|
| `_apply_d3_verdict` moving to DESIGNING | Enters DESIGNING only when the snapshot stage list includes it (that is, `designing` is `when_needed` and the run has UI work, or the list includes it unconditionally). Otherwise the run halts with a named error ("Design was requested but the profile omits design."), never stalls and never silently skips. |
| `_integrate` loop-back to EXECUTING for fix tasks | EXECUTING is non-omittable, so the loop-back always targets a listed stage; no additional check, stated here so the site is not missed. |
| `_ship_after_score_gate` rework loop-back to EXECUTING | Same as above: EXECUTING is non-omittable. |
| `_after_ship_vote` moving to SHIPPING, AWAITING_SHIP, or DONE | SHIPPING entered only when the snapshot stage list includes it; AWAITING_SHIP parks exactly when `after_approval` is `ask`; DONE ends the run when shipping is off. A ship vote for a snapshot that omits SHIPPING while shipping is on is refused at creation (section 7.3 incoherence), so this site never sees it. |
| `_integrate` moving directly to AWAITING_SHIP | The ship decision gate: parks exactly when `after_approval` is `ask`, independent of any step policy (section 7.2). |
| `Pipeline.approve` mapping a gate back to a step | The map is built from `STEP_GATES` plus AWAITING_SHIP → SHIPPING, and the target step is checked against the snapshot stage list: approving the gate of an omitted step refuses with a named error instead of resuming into a stage the profile omits. |
| `_advance` loop breakout (current state has no handler) | If the run lands in a state with no snapshot entry, `_advance` halts with a named error instead of calling `_finish`. A run never ends its learning step early because a stage went missing. |

### 8.2 The default stays on the legacy path until conformance passes

Runs without a profile (`workflow_profile_id` null) execute exactly
today's code path: no snapshot, no new lookup, no execution
behaviour change. One stated exception: display surfaces gain the
assurance label (section 10.3), so a legacy run's run page, board
row, and pull request body show its assurance label where they
showed none before. Execution behaviour is unchanged; display is
not byte-identical, and the conformance suite compares label
output as well as stage sequences, so the exception is tested
rather than hidden. The `default` preset is defined as the
parameter set that reproduces today's behaviour, and a conformance
suite must pass before any run is allowed to execute the default
via the profile path. The suite runs the same cases through the
legacy path and through the `default` preset path under a
stub/replay harness: Jev decisions (D1/D3/D6) and worker outputs
are stubbed from recorded fixtures, and the clock is fixed, so
the comparison is deterministic. It requires identical stage
sequences, gate parkings, and outcomes, including Halt/resume and
cancel cases, except differences named in a deliberate-difference
registry (a file listing each allowed difference with its reason);
for the `default` preset the registry must be empty. Until the
suite passes, picking `default` records the choice and runs the
legacy path. After the suite passes, the default executes through
the profile path only when the configuration flag
`[workflows] default_via_profile` is set to true (default false)
— that flag is the migration trigger under G1, and the suite must
stay green while it is set. Decided as G1: the default stays hardcoded with the conformance test as its guard. The flag's scope is decided as follows: picker `default` always means no profile (section 13.1); when `[workflows] default_via_profile` is true and the conformance suite is green, no-profile runs execute via the profile path using the default preset's parameters — an execution-path change only, not a picker-meaning change. The flag is distinct from `[workflows] enabled` (default true, section 17), which disables profile selection entirely. The flag's implementation (config definition, run-creation branch, and a test distinct from the `enabled` test) is owned by phase 3.

### 8.3 In-flight runs and profile edits

In-flight runs are unaffected by profile edits by construction
(snapshot, section 6.4). A run that is waiting in a gate (`NEEDS_HUMAN`
is not a gate; `AWAITING_*` is) resumes under its snapshot's gate
policy, not under any edited policy. A profile version referenced by
a snapshot cannot be permanently deleted until no run references it
(section 6.3).

## 9. Presets and precedence

### 9.1 Preset tables

Presets are data in the profile format, validated by the same
validator. Values not listed here fall back to the repo
configuration. "History says safe" in the Efficiency column is
implemented by the suggestion engine (section 12), not by the
preset: the preset itself carries fixed values, and a suggestion may
propose changing a value with evidence.

| Setting | Default (today) | Efficiency | Accuracy |
|---|---|---|---|
| Stage list | Full canonical list | Canonical list; DESIGNING `when_needed`; no stage omitted | Canonical list; DESIGNING `when_needed` |
| Quality level | Repo configuration | Repo configuration (never lowered by the preset) | `production` as a floor: the repo's level is kept when stricter (a `scalable` repo stays `scalable`) |
| Planner policy | Repo configuration | Repo configuration (never narrowed by the preset; free-first ordering applies within the repo's allowed policy) | Repo configuration (never narrowed by the preset) |
| Role models | Repo configuration | Cheapest priced model per role (see price rule below); a role with no priced model stays at repo configuration and the picker says so | Repo configuration (never narrowed by the preset) |
| `max_plan_rounds` | 3 | 2 (maximum) | 3 |
| `max_fix_rounds` | 2 | 1 (maximum) | 2 |
| `score_max_retries` | 2 | 1 (maximum) | 2 |
| `score_threshold` | 85 | 85 (unchanged: lowering the bar to save tokens is a quality cut, not an efficiency gain) | 85 |
| Cache mode | `exact` | `exact` (reuse is already the default; the preset never disables it) | `exact` |
| CDA grading | Off | Off | On (advisory only; threshold stays 0.7) |
| Ship settings | Repo configuration | Repo configuration | Repo configuration |
| Autonomy mode | Creation setting | Creation setting (not set by any preset) | Creation setting (not set by any preset) |
| Learning profile | Run's existing setting | Run's existing setting (naming a profile does not enable learning, section 7.4) | Run's existing setting |
| Goal label (preset) | `null` | `efficiency` | `accuracy` |
| Priority (display only) | — | — | quality first, speed second |
| Assurance | Computed from effective settings | `full` (no floor behaviour is dropped) | `full` (no floor behaviour is dropped) |

Price rule for "cheapest": the price source is `[dispatcher.cost]`
configured prices. A model with no configured price is excluded
from any "cheapest" claim and listed as unpriced; if no model in
a role has a configured price, Efficiency leaves that role at repo
configuration and the picker says so. Token counts alone never
prove a model is cheaper, and a Codex worker's `null` cost never
counts as zero. Preset semantics, stated once: round caps are
maximums; quality level is a floor (a preset never lowers the
repo's level); planner policy is never narrowed by a preset; a
preset never sets autonomy mode. A user who wants a lower quality
level than the repo's can set it in a custom profile, and that
profile's assurance computation records the reduction (section
10.3) — a quality cut is a user choice with a visible label, not
a preset default.

Two statements about this table. First, Efficiency's savings come
from fewer rounds and cheaper priced models — not from cache
magic, because exact-cache reuse is already the default, and not
from a lower quality level, because the preset never lowers one.
Second, Accuracy as defined here differs from the default in
exactly three observable ways: CDA grading on, a production
quality floor, and the `accuracy` goal label. If that closeness
means Accuracy "just renames the default", option set H exists to
say so and to redefine what Accuracy optimizes.

### 9.2 Precedence (decided: F1, explicit order table)

When sources disagree, the winner is decided by this order, applied
in one place at run creation, and the snapshot records the winning
source per setting in its `resolved_from` map (section 6.6):

1. Explicit per-run flags at creation (`--quality`, `--plan-only`,
   `--auto-ship`, `--ask-before-ship`, and their dashboard
   equivalents). A flag the user types for this run beats
   everything, because it is the most specific instruction.
2. The run's flow snapshot (profile version).
3. Repo configuration: `jev.toml` plus the run's workspace
   profile, applied as today (this design does not change the
   intra-tier order between a config file and a workspace
   profile).
4. Baton built-in defaults.

Autonomy cases, in plain form (the table in section 7.2 is
normative for gate behaviour): the user asked for a gate → the
run parks; the run is AUTONOMOUS → the run does not park; the
profile says `require` and the run is AUTONOMOUS → creation
refuses (section 7.2); the profile says `skip` and an explicit
gate applies → the gate stands. Autonomy mode is set at creation
outside the profile, and the profile's gate policies apply within
it — a profile never turns an AUTONOMOUS run into a gated run or
vice versa. If option set F reorders precedence, this paragraph
is re-audited with it, because gate behaviour is where a wrong
order silently removes a human from the loop.

`plan_only` and the non-omittable list: `plan_only` ("stop the
run after the plan is validated") is a tier-1 per-run flag that
ends the run early by design. It does not "omit" EXECUTING or
INTEGRATING from the profile: the profile's stage list stays
valid, and the run simply ends after VALIDATING. The executed
stage list is a prefix of the profile's list, which conformance
treats as a planned early stop, not a violation of section 7.3.

## 10. Safety floor: enforced behaviours, and the label

### 10.1 The floor as behaviours

A safety behaviour is something the pipeline enforces, not a node
that is present. The floor is this list, and a profile run must
satisfy every item or fall under section 10.2:

1. The quality gate runs at `INTEGRATING` and a failed gate never
   ships. The level may vary, but `none` is addressed in section
   10.2, and a preset never lowers the repo's level (section 9.1).
2. Merge conflicts never ship; failed integration tests never ship.
3. A ship vote passes through the score gate at its threshold, or
   the run stops for a human after `score_max_retries` rounds. A
   missing score does not ship: for a profiled run, a ship vote
   with no score parks for a human in `AWAITING_SHIP` instead of
   shipping (fail-closed); for a legacy run, the existing
   behaviour stands and the assurance computation records the
   missing score as an absent behaviour.
4. A ship vote passes through the ship decision: `ship_enabled`
   true with `after_approval` `merge` (ships), `ask` (parks in
   `AWAITING_SHIP` for approval), or `ship_enabled` false (the
   run ends `DONE` unshipped).
5. The plan passes through D3 validation and through the plan
   approval gate when plan approval applies: that is, when
   `[approval] require_plan_approval` is set, when the run is in
   `ASK_EVERY_STEP` mode, or when the VALIDATING gate policy is
   `require`. Under `AUTONOMOUS` opt-in (an explicit
   creation-time choice outside any profile) the plan gate does
   not apply. A standard-mode run with plan approval off in
   config does not satisfy this item, and the assurance
   computation records the plan gate as absent. "Different
   vendor than the planner" applies only when the repo configures
   a distinct validator model: then the validator's vendor must
   differ from the planner's vendor, checked against the role
   configuration; when the repo configures no distinct validator
   model, D3 validation alone satisfies the item, and the
   assurance computation records that.
6. Every shipped result has a branch (the run's result branch), a
   test record (the integration-test result stored on the run),
   and a quality record (the latest quality-gate report) that the
   run page can show. All three are already run-record data; a
   run that cannot produce them does not ship. `[ship]
   require_quality_pass` (refuse runs whose latest quality gate
   blocked) and `[ship] enabled` are part of this item's
   mechanism, not separate from it.

### 10.2 Enforcement and the existing bypasses

The design's position is a mix, decided as B1 (section 16 Decided picks).
The refusal list is section 7.3 (normative) plus the two
setting refusals below; this section points at that list and does
not restate it:

* A profile is refused at save time when it omits any of the five
  non-omittable stages (section 7.3), sets quality level `none`
  while shipping is enabled (`ship_enabled` true), or sets
  `score_skip_human` while shipping is enabled. These refusals
  use the section 7.5 catalogue messages.
* Configuration-level bypasses outside profiles are not rewritten
  by this feature. Instead, the assurance computation (section
  10.3) applies to every run, profiled or not, so a legacy run
  that uses one of those bypasses carries the same reduced label
  a profiled run would. The bypass classes that produce a reduced
  label are: quality `none` in configuration; `AUTONOMOUS` mode
  (plan gate absent); `score_skip_human` in configuration; plan
  approval off in configuration (section 10.1 item 5); and a
  missing ship score. The label is therefore not a tax only new
  flows pay.
* A run that ends at `DONE` unshipped and is later shipped by hand
  carries its assurance label into the pull request body through
  the same writer (section 10.3). Hand shipping does not
  re-decide the run: under the mix, the label travels and the
  ship proceeds; the human-gate question of whether a hand ship
  of a reduced run should also require confirmation is recorded
  in section 19.
* Under the label option (if option set B picks it over refusal
  for some behaviour class), the profile saves, the run executes,
  and the label travels (section 10.3). Under the refuse option,
  the profile does not save. Under B3 (label everything), the
  label covers behaviour classes only; the five-stage core stays
  refused (section 7.3). The design does not allow a third
  outcome in which a dropped safety behaviour is neither refused
  nor labelled — that outcome is the failure mode the issue
  forbids. A label alone never blocks a ship: the classes that
  must block are refused, not labelled.

### 10.3 Assurance and where the label lives

Assurance is computed at run creation from the run's effective
settings (the snapshot for a profiled run, the run's settings for
a legacy run) and stored on the run record:

* `full`: every floor behaviour in section 10.1 is present.
* `reduced`: otherwise, plus the list of absent behaviours in
  plain words (for example: "quality gate off", "ship score gate
  skipped").

The assurance label renders on the run page next to the profile
id and version, on the board's run row, and in the pull request
body for any shipped result, with the same text everywhere, for
example: "Reduced checks — the quality gate was off for this run.
Review the changes before merging." The writer for the pull
request body is `core/ship/pr_body.py`, used by both the
dashboard ship path and the CLI ship path, so a reduced run
shipped from either path carries the label. A reduced run is not
blocked from shipping by the label alone (unless option set B
chooses refusal for that behaviour class); the label's job is to
show the reduction where the ship decision is made, not to
re-decide the user's choice out of sight. Assurance labels are
computed server-side from the snapshot at run creation. A
browser-side label is display only and is never trusted for
shipping decisions.

## 11. Measurement: stage records (prerequisite phase)

No suggestion that changes a run, and no efficiency claim in the
product, may ship before stage records exist. This section is a
prerequisite phase, not a later refinement: until it lands, the
suggestion engine runs in preset-only mode (section 12.4) and the
Efficiency preset's savings are described qualitatively ("fewer
rounds, cheaper models"), never as measured token numbers.

### 11.1 Stage record schema

One row per stage execution of a run (a retried stage writes one row
per attempt, linked by attempt number):

* `run_id`, `stage` (a `RunState` work state), `attempt` (1-based),
  `workflow_profile_id`, `workflow_profile_version` (or
  `workflow_profile_release` for presets), `goal` (the user-stated
  goal copied from the run), `preset_goal` (the preset's goal
  label, display only, section 12.1).
* `started_at`, `ended_at` (wall clock), `work_seconds` (ended minus
  started minus gate-wait seconds — gate waiting is recorded
  separately, never mixed into work time).
* `gate_wait_seconds` (time parked in the step's waiting state,
  0 when no gate applied).
* `tokens_in`, `tokens_out` (integers when the worker reports them,
  `null` when it does not — `null` means unknown, never zero).
* `cost_usd` (computed from reported usage and the configured price
  when computable, else `null` with a reason string such as
  `codex-no-cost`).
* `cache_hits` (count of exact-cache serves during the stage),
  `cache_served` (boolean: the stage's headline result came from
  cache), `semantic_shadow_hits` (count of shadow agreements
  observed, never presented as serves).
* `outcome`: `success`, `halted`, `failed`, `skipped`
  (conditional stage not run, e.g. `DESIGNING` under `when_needed`
  for a non-UI run), plus the halt reason when halted.
* `defects_caught` (stage-specific defect signal: for VALIDATING,
  the count of plan revise rounds; for INTEGRATING, the count of
  fix tasks created; for other stages, 0 unless the stage has a
  defined signal — DESIGNING has no defect signal in the data,
  and reason lines must say so).
* `effective_model` (the model that produced the stage's headline
  result, `null` when unknown) and `effective_level` (the quality
  level that applied to the stage).
* `completeness` (computed, not typed: `full` when no attempt in
  the row reports `null` tokens, `partial` otherwise).

### 11.2 Recording points

* Stage entry and exit: a wrapper around each `_advance` handler
  call writes the row — one row per handler call, which is one
  state visit. The wrapper is the only writer, so a stage cannot
  run unrecorded by taking a different path. A retried stage
  writes one row per attempt, and attempt numbers continue across
  a Halt and resume (they never restart at 1). A halt closes the
  open row with outcome `halted` and the halt reason; a cancel
  closes open rows with outcome `halted` and reason `cancelled`.
* Gate timing: the transition into a waiting state starts the
  gate clock; the human action (approval, modification, or stop)
  or the run ending stops it. No gate timeout exists today, so
  the clock stops only on human action or run end. Gate time is
  attributed by the gate record's `parked_step` (section 7.2):
  the plan gate's waiting time belongs to the VALIDATING row,
  the `ASK_EVERY_STEP` executing gate's waiting time belongs to
  the EXECUTING row, ship-decision waiting time (`AWAITING_SHIP`)
  belongs to the SHIPPING row, and score-gate time (which runs
  inside `_integrate`) belongs to the INTEGRATING row.
* Ship timing: the ship decision and the shipping work are recorded
  as `SHIPPING` stage rows, not folded into `INTEGRATING`.
* Decision and attempt tagging: Jev decisions are tagged at the
  point they are recorded (the decision write), and worker
  attempts at the point they are recorded (the attempt write);
  each carries the stage name, so reported usage aggregates into
  that stage's row instead of living only inside `result_json` /
  `details` JSON. A decision or attempt that spans stages carries
  the stage where it started.
* Cache events during a stage increment that stage's counters;
  semantic shadow observations increment `semantic_shadow_hits`
  only. Shadow observations are tagged with the current stage at
  observation time (`log_shadow` gains a stage parameter); a
  shadow observation with no current stage is not attributed to
  any stage row.

### 11.3 Aggregation rules (what the numbers mean)

* A stage's tokens are the sum of its attempts' reported tokens;
  attempts that report `null` contribute nothing and the row's
  completeness is `partial`. A stage with any `null` attempt is
  never quoted as an exact token number — it is quoted as "at
  least N tokens" or not quoted.
* Saved tokens from a cache hit are not counted as negative tokens.
  A cache-served stage reports `cache_served: true` and the tokens
  of the original entry when known, else `null`; the suggestion
  engine may say "this stage was served from cache N times", never
  "this stage saved X tokens" unless the original cost is known.
* Medians, not means, summarize costs across runs (one runaway run
  must not set the price of a stage). Medians are computed over
  `full` rows only; the quoted sample size is the full-row count.
  A median over a mixed full/partial set is quoted as "at least
  N tokens over M full runs" with the partial count stated, never
  as an exact median over all runs. Every quoted number carries
  its sample size ("median 12,400 tokens over 14 full runs").
* Per-profile and per-goal aggregates are computed from stage rows
  filtered by `workflow_profile_id` / `workflow_profile_version`
  / `goal`; a suggestion never mixes goals (Efficiency evidence
  never justifies an Accuracy recommendation). Run totals are
  cross-checked against stage-row sums within the completeness
  rules: full rows must sum exactly; partial rows sum to "at
  least" the recorded total.

## 12. Suggestion engine: drafts, explanations, thresholds

### 12.1 What it reads

Exactly these inputs, and no others without a design amendment:

* Stage rows (section 11): per-stage outcomes, medians, cache
  behaviour, gate waits, per goal and per profile version.
* Lessons (`core/learning`): struggle/worked signals per role,
  with lesson counts and retirement state.
* CDA grades (`code_orchestrator/ledger.py`): per-agent and
  per-tier aggregates. The existing floor applies: a tier needs at
  least 3 graded diffs before it can be called "best" (Phase 1
  rule, unchanged).
* Cache rows: exact-cache hit counts per role/kind, and shadow
  agreement counts with their thresholds. Shadow data is labelled
  as shadow data in every reason line that uses it.
* Run outcomes: shipped vs. stopped-for-human vs. failed, quality
  scores, and rework rounds, per profile version.

The engine never reads model prices it was not configured with
(the price source is `[dispatcher.cost]` configured prices;
unpriced models are excluded from cheapest claims, section 9.1),
never reads another repo's runs for a repo-scope suggestion
(user-scope suggestions may aggregate across the user's repos only
when the user asks for a cross-repo suggestion, and say so), and
never uses a run whose user-stated goal is `null` as evidence for
a goal-conditioned recommendation. The goal used as evidence is
always the user-stated goal (section 5): a run that picks the
Efficiency preset without stating a goal records `goal` `null`
and `preset_goal` `efficiency`, and counts as no goal evidence,
not as efficiency-goal evidence. `custom`-goal runs are evidence
only for `custom`-goal requests, never for efficiency or accuracy
recommendations.

### 12.2 What it produces

A suggestion is a draft profile version plus a reasoning record.
The reasoning record has one line per change, each line in this
form: what changes, the evidence (counts and medians with sample
sizes, the evidence window, and the completeness flag), and what
would prove it wrong. Examples (each line carries its window and
completeness flag, as section 11.3 requires):

* "Drop CDA grading from this flow: grading is advisory, off by
  default, and your last 9 graded diffs in the last 90 days
  produced no routing change (best tier: none, fewer than 3
  graded diffs per tier; measured: full)."
* "Keep validation: it caught plan defects in 7 of your last 12
  completed runs in the last 90 days (plan revise rounds counted
  on validation rows; measured: full). Dropping it is refused in
  any case (section 7.3), so this line explains why the stage
  stays, not a saving."
* "Do not shorten integration: 3 of your last 10 completed runs
  in the last 90 days needed a fix task from failed integration
  tests (fix tasks counted on integration rows; measured: full)."

A suggestion that cannot produce an evidence line for a change
does not make that change. A suggestion with no changes is not
presented as a suggestion; the engine says "no change beats your
current flow on your stated goal with the data available" and
shows the numbers.

### 12.3 Thresholds (design values, tunable constants)

* A usage-based suggestion requires at least 10 completed runs
  with stage records for the scope it covers (repo, or user for a
  cross-repo suggestion), within a 90-day freshness window
  anchored at run end time. "Completed" means reached `DONE`;
  stopped-for-human and failed runs count as evidence for failure
  rates but not toward the 10-run savings threshold. Older runs
  age out of the evidence set; the reason lines say so when
  they do.
* A cheaper-model or cheaper-tier recommendation requires at least
  5 runs with that model/tier at the run's quality level, with no
  quality-gate failure attributable to that model/tier in the set,
  and (for CDA tiers) the existing 3-graded-diff floor.
  Attribution rule: a quality-gate failure is attributed to a
  model when the failing code's producing attempt used that model
  (attempt records carry the model); a failure is attributed to
  a CDA tier through the ledger's tier for that grade.
* A stage-drop recommendation concerns only DESIGNING and
  SHIPPING, the only omittable stages (section 7.3), and requires
  at least 10 runs in which the stage ran, with a measured-median
  saving stated alongside it. DESIGNING has no defect signal in
  the data, and a DESIGNING-drop reason line must say so. A
  stage-shortening recommendation (fewer rounds, not removal)
  requires at least 10 runs, a measured-median saving, and the
  stage's defect signal stated: validation's signal is plan
  revise rounds, integration's signal is fix tasks. A stage whose
  defect signal is above zero in the window may not be
  recommended for shortening on cost grounds alone. Defect-escape
  count, where a reason line uses it, means quality-gate failures
  plus fix tasks in the evidence window whose origin stage is the
  candidate stage, counted from gate-failure and fix-task
  records.
* Below any threshold, the engine runs in preset-only mode: it may
  propose a preset, with the preset's documented behaviour as the
  reason, and must label the proposal "preset, not measured for
  your history".

### 12.4 Approval flow (the only way a suggestion changes a run)

1. The user asks for a suggestion for a stated goal, from the run
   creation page or the workflows page.
2. The engine returns a draft profile version plus its reasoning
   record. The draft is stored as a suggestion record (section
   6.6, under the profile's directory), not as an active profile
   version, and it appears nowhere as a runnable flow until
   approved.
3. The user reviews the reason lines and either approves (the draft
   is saved as a new profile version, like any other edit, and may
   then be picked for a run; if the draft is identical to the
   current version, approval creates no new version and instead
   records approval of the existing version) or discards it (the
   suggestion record is kept for audit, marked discarded, and
   never applied; the draft's new version is never created, it
   never appears in the picker, and no run changes — only the
   record's status changes). An approved suggestion lands as a new
   profile version with `origin: suggested` (section 6.6).
4. Approval covers that profile version only. It does not approve
   future suggestions, does not change any existing run, and does
   not change the default.
5. Re-offer rule: a discarded suggestion is not re-offered unless
   new evidence exists, checked by the engine at request time.
   New evidence means the evidence window changed: a section 12.3
   threshold was crossed by new runs, or a new lesson arrived in
   the window. The dedup key (section 6.6) is the from-profile
   version plus the change set; a request whose key matches a
   discarded record with unchanged evidence returns the discarded
   record marked as such, not a fresh suggestion.

### 12.5 What the engine never does

* Never writes a profile, never edits a profile, never changes a
  run, never changes a default, never enables semantic serving,
  never overrides a Jev decision, never reorders a running run.
  The engine has no decision path during a run: it runs only on
  request, produces drafts, and never executes, so there is no
  conflict path with a Jev decision for it to win or lose.
* Never recommends dropping a floor behaviour in section 10.1
  without the reduced-assurance label travelling with the
  recommendation (the recommendation's reason lines must name the
  assurance change).
* Never presents a preset as a measured suggestion, never presents
  shadow data as serving data, never quotes a token number without
  its sample size and completeness flag.

## 13. Dashboard: pages, editor, and validation feedback

### 13.1 Routes and pages (decided: A1 names)

* `/workflows` — list page: built-in presets, repo-scope profiles,
  user-scope profiles, each with scope badge, current version,
  goal label, and assurance note where applicable. (If option set A
  picks "flows", this route becomes `/flows`; the page structure
  does not change.)
* `/workflows/new` and `/workflows/{id}` — editor page (section
  13.2) and detail page (stage list rendered as a chain, parameters,
  version history, runs that snapshotted each version, suggestion
  records for the profile).
* `/workflows/{id}/suggest` (POST plus result page) — suggestion
  request for a stated goal, returning the draft plus its reasoning
  record and an approve/discard action.
* Run creation (`/runs/new` and the new-run form): a profile picker
  with `default` pre-selected, the goal label shown, and the
  assurance of the picked version shown before the run starts.
  The preview is a pre-computation, not a second rule: the same
  assurance function runs over the form's inputs (picked version,
  repo configuration, selected flags) for the preview, and over
  the resolved snapshot at creation for the stored value. If the
  two differ (a flag changed between preview and creation), the
  creation value is the stored one, and the run page shows it.
  `default` in the picker means "no profile" (the legacy path), not a preset that runs. The Accuracy picker entry shows the priority row "quality first, speed second" before selection.
* Run page (`/runs/{run_id}`): the snapshot's profile id, version,
  goal, and assurance label next to the stage timeline; a reduced
  run names its absent behaviours there in plain words.
* Board (`/`): run rows show the assurance label for reduced runs.

All new routes follow existing dashboard patterns: checked forms
(section 5), server-side validation before any save, and error
text a non-programmer can act on.

### 13.2 The editor: canvas plus list, one validator

The editor has two modes that build the identical profile through
the identical validator:

* Canvas mode: stages as nodes in canonical order, edges drawn
  between consecutive stages. The user drags stages out of the
  chain (omission, where allowed) and edits per-stage parameters
  and gate policies on the node itself. The canvas offers no
  reorder control: dragging a node to a new position is refused
  with the section 7.1 reason, and that refusal is the documented
  behaviour, not a failed reorder feature.
* List mode: the same stage list as an ordered list with
  include/omit toggles, parameter fields, and gate policy selects.
  No dragging, no mouse dependency, fully usable on narrow screens.
  List mode is not a lesser editor: it produces the same profile
  format, runs the same validator, and shows the same messages.
  Mode equivalence is defined by an input-equivalence table: a
  canvas drag-out maps to a list toggle-off, a canvas parameter
  edit maps to the same list field edit, and both produce the
  identical profile JSON for the same selections.

Validation feedback timing: at save (the full catalogue, section
7.5, with the offending stage named and the fix stated), live
while editing for exactly the live subset (schema parse, unknown
names against current configuration, duplicate stages, empty
list), and again at creation on the stored version. A save that
fails lists every error, not just the first, so the user is not
sent around the form one error at a time. A creation-time
failure names the profile version and the setting, creates no
run, and offers the profile picker again — never a bare
traceback, never a silent fallback to the default. An
execution-time existence failure (a model removed after creation)
halts the run in `NEEDS_HUMAN` with the setting named.

### 13.3 Suggestion display

The suggestion result page shows the draft as a stage chain with
changes highlighted against the profile it was suggested from, the
reasoning record line by line (section 12.2), the evidence
thresholds met or unmet (section 12.3), and the assurance change
if any. Approve and discard are the only actions. An approved
suggestion lands as a new profile version with a "suggested"
provenance note; a discarded suggestion stays visible in the
profile's suggestion history as discarded, so the same suggestion
is not re-offered without new evidence.

## 14. Security: profiles are data, validated server-side

* A profile file is data. The executor never evaluates profile
  content as code, never runs a command named by a profile, and
  never installs anything because a profile says so. Model names
  are allowlisted to the repo's role configuration; quality levels,
  cache modes, ship modes, and autonomy values are parsed against
  their enums; anything else is a validation refusal.
* All editor and suggestion routes validate server-side even when
  the browser already validated: the browser check is convenience,
  the server check is the rule. Profile JSON has a size cap
  (64 KB per version file, a starting value the implementation
  may tune with a reason recorded); larger files refuse with the
  size stated.
* Profile files contain no secrets, no tokens, and no credentials.
  A profile that carries a field outside the schema is refused
  rather than stored with the field ignored — a dropped field is
  how a user believes a setting is saved when it is not.
* The suggestion engine reads only the inputs in section 12.1, and
  only within the scope the user asked for (repo, or cross-repo
  when explicitly requested). Suggestion records contain aggregate
  counts and medians, never run contents, prompts, or diffs.
* Assurance labels are computed server-side from the snapshot at
  run creation. A browser-side label is display only and is never
  trusted for shipping decisions.

## 15. Detailed implementation plan

Phases run in order. A phase does not start until the previous
phase's acceptance checks pass, with no exception: phase 0
completes (contract frozen) before phase 1 starts. No phase
changes the default flow's execution behaviour for runs without a
profile (the section 10.3 display exception is stated in section
8.2). File paths are relative to the repo root; "new" means
created, "change" means modified. Module, route, and template
names follow option set A: the names below are the A1 names, and
a different A pick renames them by the section 5 sweep list
(routes, modules, templates, directories, messages, labels). No
production code is written during the design phase; this section
is the plan the implementation phase will follow. Every phase
states an entry rule (what must be true before it starts, beyond
the previous phase passing) and an exit check with a threshold or
artefact. "Fails loudly" means one thing everywhere in this
section: the failure refuses with a plain message naming the
setting, and writes a log line; it never fails silently and never
falls back to another value.

### Phase 0 — Contract: safety floor, knobs, stage/gate catalogue

Entry: the section 16 gate has picked every option set, and this
document has been amended by the reserved re-audit. Exit: the
contract files below exist and the conformance skeleton runs
green on the legacy path.

* This document, amended by re-audit after the section 16 gate:
  the floor behaviour table (section 10.1), the allowed-knob list
  (section 2.3), the gate-policy table (section 7.2), and the
  stage/gate catalogue (section 7.5) become the contract. The
  contract is a document, not a code artefact, and phase 0's
  deliverable is the amended document itself — not a
  self-reference: the amendment touches the sections the gate
  picks changed, listed in the re-audit report.
* `tests/conformance/` (new): one conformance suite grown in two
  phases. Phase 0 builds its skeleton: the scenario list
  (section 17 test matrix, conformance row) plus recorded
  baselines — stage sequences, gate parkings, outcomes, and label
  output for each scenario through the legacy path under the
  stub/replay harness (section 8.2), stored under
  `tests/conformance/baseline/`. Acceptance: the skeleton runs
  green on the legacy path and the baselines are recorded, so a
  later change cannot quietly redefine "unchanged".

### Phase 1 — Measurement: stage records

Entry: phase 0 exit met (contract frozen, baselines recorded).
Exit: the measurement tests in section 17 pass.

* `src/jev_agent/core/store/records.py` (change): stage record
  model (section 11.1) and run-record fields for the flow snapshot
  (`workflow_profile_id`, `workflow_profile_version`,
  `workflow_profile_release`, `goal`, `preset_goal`, `assurance`,
  `assurance_absent` list, `flow_snapshot` JSON column). Legacy
  runs read back with `workflow_profile_id` null and no stage
  rows; nothing backfills invented history. Stage rows are
  written for runs with and without profiles from this phase on;
  legacy runs (created before this phase) have no stage rows.
* `src/jev_agent/core/migrations.py` (change, append-only: a new
  migration script appended to `MIGRATIONS`, moving `PRAGMA
  user_version` forward by one — there is no
  `core/store/migrations/` directory): stage records table and
  run-record snapshot columns.
* `src/jev_agent/core/pipeline.py` (change): the `_advance`
  handler-call wrapper that writes stage rows, gate timing, and
  ship timing (section 11.2); decision tagging at the decision
  write and attempt tagging at the attempt write; shadow
  observations tagged with the current stage. No handler logic
  changes in this phase.
* `src/jev_agent/dashboard/` (change, views only): the run-page
  template shows per-stage rows (times, tokens with completeness
  flags, cache behaviour) and the Spend page template aggregates
  stage rows per run, following the existing run-page and Spend
  template patterns.
  Acceptance: a run's full stage rows sum exactly to its recorded
  totals, partial rows sum to "at least" the totals, and a
  `null`-token stage is never displayed as zero. A gate-wait
  test with a known gate wait (a run parked in a gate, then
  approved) shows gate-wait seconds outside work seconds.

### Phase 2 — Profile storage, versioning, selection, assurance

Entry: phase 1 exit met. Exit: the phase acceptance below passes.

* `src/jev_agent/core/workflow_profiles/` (new package; module
  names follow option set A): `schema.py` (section 6.6 field
  lists), `store.py` (file store, repo and user scope, atomic
  version writes per section 6.3), `resolver.py` (one resolver
  function: validate the picked version, apply section 9.2
  precedence once, store the snapshot, compute assurance),
  `validator.py` (schema plus the live catalogue subset,
  section 7.5 — the full catalogue lands in phase 3).
* `src/jev_agent/app/runs.py` (change): `create_run` calls the
  resolver — this is the production funnel (dashboard runner,
  scheduler runner, chat path, MCP path, CLI run creation all
  call it). `src/jev_agent/core/pipeline.py` (change):
  `Pipeline.start` calls the same resolver for the chat-driver
  path. Runs without a profile keep the legacy path: the
  resolver is skipped, no snapshot is stored, and execution
  behaviour is unchanged.
* Creation paths and snapshot timing (each reusing the same
  resolver; a path that cannot resolve a profile refuses with
  the setting named, never silently falls back to the default):
  dashboard `/runs/new` (picker; snapshot at creation);
  CLI run creation (via the `create_run` funnel, no separate
  path); queue add (picker on the queue form; snapshot at queue
  time — the queued run executes the version it was queued with,
  even if the profile is edited before execution); recurring
  queue items (snapshot at each scheduled creation, from the
  profile version current at that time); chat-started runs (no
  picker in this design; they resolve `default` unless the chat
  form picks a profile, which a later change may add). Queue
  items gain a `workflow_profile` field, distinct from the
  existing `profile` (workspace profile) field.
* Assurance computation and label rendering (this phase owns
  them): the resolver computes assurance at creation from the
  resolved snapshot (profiled runs) or the run's effective
  settings (legacy runs); the run page template and the board
  row template render the assurance label; `core/ship/pr_body.py`
  (change) writes the label into the pull request body on both
  the dashboard and CLI ship paths.
* `jev workflow` CLI commands (list, show, validate) are not
  phase-2 work: they are a later phase with their own acceptance,
  after phase 6, because the CLI is not required for acceptance
  and browser-first comes first.
* Acceptance: create, version, archive, and delete a profile;
  snapshots survive edits and deletes; a run created from an
  archived profile version still runs; a deleted profile's
  snapshots still resolve; the assurance label renders on the run
  page, the board row, and in a pull request body for a reduced
  run shipped from the dashboard path; a creation path that
  cannot resolve a profile refuses with the setting named.

### Phase 3 — Snapshot-aware lookup and shared validator

Entry: phase 2 exit met. Exit: the conformance suite passes
under the stub/replay harness with an empty deliberate-difference
registry for `default`.

* `src/jev_agent/core/pipeline.py` (change): the snapshot-aware
  step lookup and gate-policy application from section 8.1,
  including every handler move-site check in the section 8.1
  table and the `approve` gate-to-step snapshot check, behind
  the profile path only. The legacy path's behaviour is
  unchanged; the shared helpers gain an optional snapshot
  parameter defaulting to no snapshot (section 8.1).
* `src/jev_agent/core/workflow_profiles/validator.py` (change
  from phase 2's validator): the full catalogue, used by both the
  editor and the executor, with the messages from section 7.5.
  Phase 3 also re-checks every artefact stored under phase 2
  (stored profiles, archived versions) against the full
  catalogue, and reports any that now fail with the setting
  named.
* `tests/conformance/` (change): the phase-0 skeleton grows into
  the comparison suite — the same scenarios through the legacy
  path and through the `default` preset path under the
  stub/replay harness (section 8.2). Validator unit tests cover
  every catalogue entry, with each bundled shape counted
  separately (unknown model, quality level, cache mode, and
  learning profile are four cases, not one).
* Acceptance: the conformance suite passes with an empty
  deliberate-difference registry for `default`; a profiled run
  and a legacy run of the same scenario produce identical stage
  sequences, gate parkings, outcomes, and label output except
  differences named in the registry. This gate covers phase 4: no preset run beyond tests happens before it passes. Phase 3 also implements the `[workflows] default_via_profile` flag (config definition, run-creation branch, and a test distinct from the `[workflows] enabled` rollback test): when true and the conformance suite is green, no-profile runs execute via the profile path using the default preset's parameters.

### Phase 4 — Presets as data, plus the run-creation picker

Entry: phase 3 exit met (the conformance gate covers preset
runs). Exit: the phase acceptance below passes.

* `src/jev_agent/workflow_profiles/presets/` (new package data,
  loaded with `importlib.resources`): `default`, `efficiency`,
  `accuracy` preset files in the profile format, identified by
  release (section 5), validated by the shared validator.
  Preset validation runs at dashboard startup ("fails loudly"
  per the section 15 definition: a plain message naming the
  setting plus a log line) and again at creation; a stale preset
  (removed model, renamed check) refuses at startup, not at run
  time.
* `src/jev_agent/dashboard/` (change): preset picker entries on
  the run-creation form with their documented behaviour shown
  before selection, including the assurance preview (section 13.1), the unpriced-role notice where it applies (section 9.1), and the Accuracy priority row (section 9.1).
* `tests/` (new): preset conformance — each preset validates,
  each preset's setting table matches section 9.1, and a preset
  referencing a removed model fails validation at startup.
  Fixture: a repo with a named set of configured models, stated
  in the test, so "validates against live configuration" has a
  fixture.
* Acceptance: both presets run end to end on the Mac dashboard,
  where "end to end" means to `DONE` with a shipped pull request
  when shipping is on, or to `DONE` unshipped when shipping is
  off, on the Mac dashboard (Mac verification applies to preset
  acceptance and to the section 17 live checks, stated once
  here). Efficiency measurably uses fewer rounds and cheaper
  models than the default on the same scenario, where "the same
  scenario" means the same prompt, the same repo state, the same
  starting branch, and the same configured models under the
  replay harness (section 8.2); the metric is summed `cost_usd`
  over measured-`full` stages only plus round counts, with
  `null`-cost stages excluded by rule and sample sizes stated.
  If no measured-`full` comparison is possible, the criterion
  falls back to a static check (the preset's resolved snapshot
  matches its section 9.1 table) and says so. Accuracy runs the
  full canonical stage list with CDA grading on and a quality
  level at or above `production`, with every section 10.1 item
  present.

### Phase 5 — Flowchart editor (dashboard)

Entry: phase 4 exit met. Exit: the phase acceptance below passes.

* `src/jev_agent/dashboard/routes_workflows.py` (new; name
  follows option set A): list, editor, detail, suggestion
  request/result, approve/discard routes, following the existing
  `routes_<area>.py` pattern (for example `routes_queue.py`).
* `src/jev_agent/dashboard/templates/` (new templates following
  the existing flat template pattern): list, editor (canvas plus
  list mode), detail, suggestion result templates.
* `src/jev_agent/dashboard/static/` (new, scoped files only):
  canvas behaviour and list-mode behaviour, both calling the
  same server-side validator; no validation logic lives only in
  the browser.
* Editor extras: profile export (download JSON) and import
  (upload JSON) for repo-scope sharing (section 6.2).
* Acceptance: create, save, and run a custom flow entirely from
  the browser, in both editor modes, with include/omit and
  parameter edits demonstrated and a reorder refusal
  demonstrated in canvas mode (the refusal counts as the
  reorder check passing, because refusal is the specified
  behaviour); every catalogue error displays with the stage
  named and the fix stated (the two catalogue messages that
  previously lacked a fix now carry one, section 7.5); a save
  failure lists every error; list mode and canvas mode produce
  identical profile JSON for the same selections per the
  input-equivalence table (section 13.2).

### Phase 6 — Suggestions

Entry: phase 5 exit met, plus the entry check: the engine's
labelled-run volume is measured and reported (count of
goal-labelled runs with stage records per scope). If the volume
is below the section 12.3 thresholds, phase 6 ships preset-only
mode verified plus the usage-based path verified on fixtures;
preset-only mode alone satisfies phase 6 only with that fixture
verification, not on empty data. Exit: the phase acceptance below
passes.

* `src/jev_agent/core/suggestions/` (new): engine reading only
  section 12.1 inputs, thresholds from section 12.3, reasoning
  records from section 12.2, suggestion records with
  approve/discard lifecycle from section 12.4, stored under the
  profile's directory (`suggestions/<suggestion-id>.json`,
  section 6.6).
* `src/jev_agent/dashboard/` (change): suggestion request page
  and result page (section 13.3); suggestion history on the
  profile detail page, including discarded records and the
  re-offer rule (section 12.4).
* `tests/` (new): threshold tests (below-threshold requests return
  preset-only mode, labelled as such); approval tests (an
  unapproved suggestion changes no run and appears in no picker;
  discard changes only the record's status — no new version, no
  picker entry, no run change); evidence tests (a reason line
  without evidence is refused by the engine, not displayed);
  cross-repo tests (aggregation across repos with different
  configurations, and the "cross-repo" label displayed);
  dedup tests (a discarded suggestion with unchanged evidence is
  not re-offered); staleness tests (a draft approved after new
  runs arrive is re-checked against the current evidence window
  before saving).
* Acceptance: a suggestion with reasoning shown can be approved
  and run; an unapproved suggestion never changes a run; a
  below-threshold request says so plainly instead of inventing a
  recommendation.

A later phase, with its own acceptance, adds `jev workflow` CLI
commands (list, show, validate) for parity and debugging. It is
not required for the acceptance in section 17.

## 16. Option sets: the human-approval gate

Each set has at most three options. None is ranked, none is marked
preferred, none is pre-picked. Each states its mechanism, its
trade-offs, and the conditions under which it fails. All sets are decided in the Decided picks subsection below (2026-10-29), confirmed by the re-audit decisions that follow it; the unpicked-set rule no longer applies to this design. Sections 6–15 are
written for the design's proposed answers; where a set picks
otherwise, the document is amended by re-audit before implementation.
Re-audit, defined: one review pass by fresh reviewers over the
changed sections only, owned by the design-phase owner (the
Elephant). If that single re-audit still finds a conflict, the
conflict goes back to the human gate as an added option — it does
not trigger a second re-audit. The picks interact: C3 removes the
editor and suggestion acceptance criteria, D2/D3 remove one editor
mode's criteria, B2 removes the label-rendering criterion (replaced
by a refusal-verification criterion), H2 replaces the Accuracy
criterion, and G2/G3 replace the legacy-path criterion — section
17 states the per-branch checklists, so the human can see what
each pick rewrites before picking.

### A. Naming and route

* **A1. "Workflow profile", route `/workflows`.** Mechanism: the
  document's working term, kept. Trade-off: descriptive and close
  to the issue's words, but "profile" still appears next to
  learning profiles and server profiles, and users must learn which
  "profile" a page means. Fails when: a user opens `/profiles`
  expecting workflow profiles, or vice versa, and picks the wrong
  kind.
* **A2. "Flow", route `/flows`, ids `flow:<slug>`.**
  Mechanism: the structural lens's proposal — a distinct word with
  its own page. Trade-off: no collision with any existing concept;
  but a new word the issue did not use, and existing "profile"
  documentation must be checked so the rename does not strand it.
  Fails when: users search for "profile" and miss the feature, or
  the rename is applied inconsistently (some pages say flow, some
  say profile).
* **A3. "Workflow profile" with UI-only relabelling of the old
  concepts.** Mechanism: keep the new term, and relabel learning
  profiles as "lesson profiles" and server profiles as "workspace
  settings" in UI text only (no code rename). The implementation
  phase owns a UI-string inventory (every page, error message,
  and log line naming the old concepts) and an error-display
  mapping (code-concept names shown with their display names),
  delivered as phase-5 work. Trade-off: all three concepts get
  distinct display names without a code rename; but UI text and
  code names diverge, so a user reading an error that names the
  code concept meets a word the UI never showed them. Fails
  when: an error or log names `learning_profiles` and the user
  cannot map it to "lesson profiles" — the check is whether the
  error-display mapping covers that error.

### B. Enforcement strictness for the safety floor

* **B1. Mix, as specified in section 10.2.** Mechanism: refusal at
  save time for quality `none` with shipping on,
  `score_skip_human` with shipping on, and omitted required stages
  (section 7.3); label for the remaining reductions — the
  configuration bypass classes in section 10.2 (quality `none`
  in config, AUTONOMOUS plan-gate absence, `score_skip_human` in
  config, plan approval off, missing ship score) — travelling on
  the run record, run page, board row, and pull request body.
  Refusals carry the message prefix "Refused:" and labels carry
  the text "Reduced checks", so the two shapes are
  distinguishable at a glance. Trade-off: the most protective
  refusals are hard stops while genuinely optional reductions stay
  possible and visible; but two enforcement shapes must both be
  understood, tested, and explained. Fails when: a user meets a
  refusal for one behaviour and a label for a similar one and
  cannot tell why they differ — the check is whether the
  refusal/label mapping above explains the pair.
* **B2. Refuse everything.** Mechanism: any profile that drops any
  floor behaviour in section 10.1 does not save, and no label path
  exists. Trade-off: simplest to explain and to test — a flow is
  either safe or it does not exist; but it removes the issue's
  "refused or labelled" alternative entirely, and a user who wants
  a deliberately reduced flow for a scratch repo has no supported
  way to say so. Fails when: legitimate reduced-assurance work
  (a throwaway experiment) is forced through the full flow or
  through out-of-band configuration instead.
* **B3. Label behaviour reductions; stages stay refused.**
  Mechanism: no profile is refused for a behaviour reduction;
  every behaviour reduction is labelled and travels as in section
  10.3. The five-stage core (section 7.3) stays refused under
  B3: stage omissions are structural, not behaviour reductions,
  and a label-only floor with a non-omittable stage core is the
  stated shape of this option. Trade-off: maximum user freedom
  with maximum visibility for behaviour choices; but a label
  does not stop a shipment, so a reduced run can still merge
  code the full flow would have caught, and the label's
  protection depends entirely on someone reading it. Fails when:
  a reduced run ships, the label is missed in review, and the
  defect the floor existed to catch reaches main.

### C. Phasing

* **C1. Phased, as specified in section 15.** Mechanism:
  contract (phase 0), then measurement, then storage/versioning
  and selection with assurance, then the shared validator and
  conformance, then presets as data with the picker, then the
  editor, then suggestions. Presets land before the editor
  because presets are data in the profile format and do not
  depend on the editor; the picker is the first user-visible
  flow choice. Trade-off: each phase is shippable and testable
  on its own, and the expensive parts (editor, suggestions) only
  start once their prerequisites exist; but the full feature
  takes longer to arrive, and phases 0–3 deliver no user-visible
  flow choice. Fails when: an early phase stalls and the feature
  is perceived as abandoned before the picker exists.
* **C2. All in one phase.** Mechanism: measurement, storage,
  engine, editor, presets, and suggestions land together.
  Trade-off: the complete feature arrives at once, matching the
  issue's acceptance as a whole; but the build is large, review
  is harder, and a flaw in measurement undermines suggestions
  that shipped in the same change. Fails when: the combined change
  cannot be verified as a whole and ships with a weak link
  (usually measurement or the validator) hidden inside it.
* **C3. Picker and toggles only; no graph editor, no suggestions.**
  Mechanism: presets plus a profile picker plus per-run setting
  overrides ("save settings as custom"), with measurement added
  for reporting from measured data. Trade-off: the smallest build that
  delivers goal-conditioned flows and real cost data; but it
  abandons the flowchart editor and the suggestion engine the
  issue asks for, so the issue is only partly satisfied. Fails
  when: the human's goal was the editor itself, not just cheaper
  or stricter runs — in which case this option does not satisfy
  the issue.

### D. Editor form (final form, whenever it lands)

* **D1. Canvas plus list, as specified in section 13.2.**
  Mechanism: two modes, one validator, one profile format.
  Trade-off: the flowchart experience the issue asks for, plus a
  mouse-free path; but two modes to build, test, and keep
  identical. Fails when: the modes drift (a graph saved in canvas
  mode renders differently in list mode) and users lose trust in
  the editor.
* **D2. List only.** Mechanism: the ordered list with toggles and
  parameter fields is the whole editor; no canvas. Trade-off:
  half the UI work, fully mouse-free and narrow-screen safe, and
  the validator's job is simpler to show; but it is not a
  flowchart in any visual sense, so the issue's "as easy as a
  flowchart" is met by structure, not by drawing. Fails when:
  users judge the feature by the drawing they were promised and
  find a form instead.
* **D3. Canvas only.** Mechanism: drag-and-drop graph editing is
  the whole editor. Trade-off: the most direct reading of the
  issue; but it excludes mouse-free and narrow-screen users from
  full use, which breaks the browser-first law for those users.
  Fails when: a user who cannot drag cannot build a flow at all.

### E. Scope and storage

* **E1. Both scopes, as specified in section 6.5.** Mechanism:
  repo-scope files under `<repo>/.jev/workflow-profiles/` and
  user-scope files under `~/.jev/workflow-profiles/`, ids carrying
  their scope, cross-scope use re-validated at creation, before
  anything is spent. Trade-off: profiles live with the repo
  checkout and follow users across repos; but two stores, two
  listings, and re-validation rules to maintain. Fails when: a
  user-scope profile references a model the target repo never
  configured, and the creation refusal arrives after the user
  believed the flow was ready — the check is whether the
  refusal names the setting at creation time.
* **E2. Repo scope only.** Mechanism: profiles live with the repo
  and nowhere else. Trade-off: one store, no cross-repo
  validation problem, and profiles are reviewable with the repo;
  but a user with the same flow across five repos maintains five
  copies, and there is no personal default flow. Fails when:
  copies drift and the "same" flow behaves differently per repo.
* **E3. User scope only.** Mechanism: profiles live under
  `~/.jev/workflow-profiles/` and are picked in any repo.
  Trade-off: one personal library, no duplication; but profiles
  cannot be shared by shipping the repo, a teammate cannot see
  the flow a run used without the author's machine, and repo
  reviewers lose the flow definition. Fails when: a shipped PR's
  flow cannot be reproduced from the repo alone.

### F. Precedence

* **F1. Explicit order table, as specified in section 9.2.**
  Mechanism: per-run flags, then snapshot, then repo config and
  workspace profile, then defaults, applied in one place, with
  the winning source recorded per setting in the snapshot's
  `resolved_from` map. The autonomy cases cover all five modes
  (`standard`, `autonomous`, `ask_every_step`, `manual`,
  `medium`) plus the `AUTONOMOUS` opt-in flag (section 7.2).
  Trade-off: every conflict has a decided winner the run record
  can show; but the table must be learned, and the autonomy-mode
  cases add rules that break the table's simplicity. Fails when:
  the table's winner surprises a user who expected a different
  source to win — the check is whether the run page shows the
  winning source for the setting in question.
* **F2. Profile always wins.** Mechanism: a picked profile's
  values override repo config, workspace profiles, and per-run
  flags (flags become creation-time defaults that a profile
  replaces). Trade-off: one rule, easy to state — "the flow you
  picked is the flow that runs"; but a per-run flag typed for
  this run is silently ignored, which is the opposite of what a
  typed flag normally means. Fails when: a user passes
  `--quality` for one run, picks a profile, and gets the
  profile's quality with no warning.
* **F3. Configuration always wins.** Mechanism: repo config,
  workspace profiles, and per-run flags override the profile; the
  profile supplies only what nothing else sets. Trade-off: no
  existing behaviour is ever surprised by a profile; but a profile
  that promises "Efficiency" can be silently overridden by a repo
  config the user forgot, and the preset's documented behaviour
  stops being a promise. Fails when: the Efficiency preset runs
  at production quality because `jev.toml` says so, and the user
  concludes presets do not work.

### G. The default flow's representation

* **G1. Default stays hardcoded, guarded by conformance.**
  Mechanism: runs without a profile execute today's code path
  unchanged; the `default` preset exists as a definition that the
  conformance suite proves equivalent, but no run executes the
  default through the profile path until the suite passes — and
  even then, the legacy path remains available. Trade-off: zero
  risk to existing runs at every step; but two paths to maintain
  (legacy and profile) and a conformance suite that must keep
  proving they agree as handlers change. Fails when: handlers
  change, the suite is not updated, and the two paths drift
  without anyone noticing until a profiled default run differs.
* **G2. Default re-expressed as a profile.** Mechanism: the
  default flow becomes the `default` preset, and every run —
  profiled or not — executes through the snapshot-aware path.
  Trade-off: one path, one mechanism, and the conformance suite
  becomes a migration test rather than a permanent guard; but the
  migration itself is the largest behaviour risk in the feature,
  because every existing run changes code paths at once. Fails
  when: a subtle handler behaviour (a gate parking, a resume
  edge, a cancel race) differs under the new path and existing
  runs break in ways the suite did not cover.
* **G3. Default as profile, legacy path removed only after a
  release of profile-only defaults.** Mechanism: G2's end state,
  reached in two steps — first G1, then a removal phase (phase 7,
  added by re-audit) after one release in which profiled defaults
  ran cleanly. "Cleanly" is measured, not asserted: the
  conformance suite green for the whole release, zero
  profile-path regressions on Mac dashboard runs during the soak,
  and the decision owned by the implementation phase's release
  review. Trade-off: the migration risk is spread across
  releases with a real-world soak period; but the two-path
  maintenance cost of G1 is paid for a whole release, and removal
  can slip indefinitely. Fails when: the removal never happens
  and the codebase carries both paths permanently — the check is
  whether phase 7 is scheduled in the release after the soak.

### H. What Accuracy optimizes

* **H1. Output quality.** Mechanism: Accuracy means defects
  caught, review depth, and verification strength — production
  quality level, CDA grading on, full validation by a different
  vendor, all gates kept. Trade-off: a definition that can be
  checked (defects caught per run, gate findings per run); but it
  overlaps the default flow closely, so Accuracy must justify
  itself against "default plus grading" in its documentation.
  Fails when: a user picks Accuracy expecting faster results and
  gets slower, more careful ones.
* **H2. Wall-clock speed.** Mechanism: Accuracy means the fastest
  correct result — parallel task execution where the pipeline
  allows it, faster models where history shows no quality loss,
  and no waiting on gates that add no information. Trade-off: a
  genuinely different preset from Efficiency (which saves tokens,
  not necessarily time); but the pipeline has limited parallelism
  today, and "faster models where safe" needs the same evidence
  floor as any cheaper-model recommendation. Fails when: speed
  work (parallelism, model choice) is not yet available, and the
  preset promises speed it cannot deliver.
* **H3. Quality first, speed second.** Mechanism: Accuracy
  optimizes output quality first, and within equal-quality
  choices prefers the faster one (fewer redundant rounds, no
  repeated work). The tie-break lives in the preset's table as a
  "priority" row reading "quality first, speed second", shown on
  the picker entry before selection. Trade-off: one preset that
  answers both readings of "performance" in priority order; but
  the priority row must be read for the tie-break to work.
  Fails when: quality and speed conflict on a real run and the
  preset's tie-break row was not shown on the picker entry.

### Decided picks (2026-10-29, never-stop directive)

The user directed on 2026-10-29 that work must never stop at
approval gates. The option sets below are therefore decided here,
under these principles in order: browser-first (zero terminal
use), least surprise, lowest resource use, and zero risk to
existing runs. A pick the user later overrules gets reworked;
that is expected, not a failure. Where sections 6–15 already
assume an answer, that answer is adopted. No pick is presented
as "recommended" — each is a decision with its rationale stated.

| Set | Pick | Rationale |
|---|---|---|
| A. Naming and route | A1 — "workflow profile", route `/workflows` | Sections 6–15 and the section 15 plan already use the A1 names (routes, modules, and directories; template filenames follow the workflows_* pattern, fixed in phase 5). Renaming now would churn every reference for no user benefit, and A1 keeps the issue's own words. |
| B. Safety-floor strictness | B1 — mix, as specified in section 10.2 | Refusals for the dangerous cases (omitted non-omittable stages, a profile that sets quality level `none` while shipping is enabled, a profile that sets `score_skip_human` while shipping is enabled — configuration-inherited `none` is label-only per section 10.2) and visible labels travelling on the run record, run page, board row, and PR body for the remaining reductions. B2 would remove the issue's "refused or labelled" alternative and force reduced-assurance work through out-of-band configuration; B3 would let a labelled reduced run ship with nothing but a label standing between it and main for behaviour reductions (the stage core stays refused under B3). A label alone never blocks a ship; refusals are the blocking mechanism. B1 is the least surprising shape: hard stops where harm is direct, visibility where the choice is genuinely the user's. |
| C. Phasing | C1 — phased, as specified in section 15 | The phased plan is the design's own order (contract, measurement, storage/selection/assurance, validator + conformance, presets + picker, editor, suggestions), and each phase is shippable and testable on its own. C2 would hide a weak link (usually measurement or the validator) inside one large change; C3 would abandon the editor and suggestions the issue asks for. |
| D. Editor form | D1 — canvas plus list, as specified in section 13.2 | The issue asks for a flowchart, and browser-first covers mouse-free and narrow-screen users too. Canvas only (D3) excludes users who cannot drag; list only (D2) is a form, not the drawing promised. D1 costs two modes kept identical by one validator, and that cost is the price of serving both groups. |
| E. Scope and storage | E1 — both scopes, as specified in section 6.5 | Repo-scope profiles are reviewable with the repo and shareable by dashboard export/import; user-scope profiles give one personal library across repos. Repo only (E2) forces five copies of one flow across five repos; user only (E3) means a shipped PR's flow cannot be reproduced from the repo alone. Repo profiles do not travel via git by default (`.jev/` is gitignored) — sharing is dashboard export/import only, as section 6.2 states. |
| F. Precedence | F1 — explicit order table, as specified in section 9.2 | Per-run flags, then snapshot, then repo configuration and workspace profile, then built-in defaults, with the winning source recorded per setting in the snapshot's `resolved_from` map. A tier-3 win records `repo` or `workspace` per whichever source the existing intra-tier resolution actually resolves — never collapsed. F2 would silently ignore a flag typed for one run; F3 would let a forgotten repo config silently override the picked profile so presets stop being promises. The table is learnable and auditable from the run record. |
| G. Default representation | G1 — default stays hardcoded, guarded by conformance | Zero risk to existing runs is the governing constraint: runs without a profile execute today's code path unchanged, and the `default` preset is proven equivalent by the conformance suite before any run executes through the profile path (and then only behind `[workflows] default_via_profile`, default false). Picker `default` always means no profile; the flag flips the execution path for no-profile runs only when conformance is green, and it is distinct from `[workflows] enabled` (phase 3 owns its implementation and test). G2 would move every existing run to a new code path at once; G3 pays two-path maintenance for a whole release before the same risk. |
| H. What Accuracy optimizes | H3 — quality first, speed second | Sections 6–15 already define Accuracy mechanically as a production quality floor, CDA grading on, and the `accuracy` goal label (section 9.1's exactly three differences); H3 keeps those mechanics exactly and adds only the priority row ("quality first, speed second"). H1 alone would ignore the user's stated "performance" reading; H2 (wall-clock speed) would promise parallelism and faster models the pipeline cannot yet deliver. H3 answers both readings in priority order without changing any preset value. |

Escalated questions from resolution, decided here on the same
principles:

* Hand ship of a reduced run: the label travels into the PR body
  and the ship proceeds; no extra confirmation step is added.
  Rationale: the label is already visible where the ship decision
  is made, and a second confirmation gate would re-decide a
  choice the user already made when picking the reduced flow.
  Overrule reworks this to a confirmation step.
* Efficiency quality: repo configuration, never lowered (section
  9.1 as resolved). A quality cut stays a user choice in a custom
  profile with a visible label, never a preset default.

Confirmed readings (not reopened):

* "Rearrange" means omit stages where allowed and edit
  parameters; stage reordering is refused by dependency, and the
  editor offers no reorder control (sections 7.1, 7.3).
* The default flow stays behavior-identical for existing runs;
  a profile is opt-in per run, never a silent change to the
  default (G1, section 8.2). The section 10.3 display exception
  (assurance label on legacy runs' display surfaces) stands as
  stated in section 8.2.

Effect on the rest of this document: sections 6–15 already
assume A1, B1, C1, D1, E1, F1, and G1, so no section text changes
for those picks. H3 adds display requirements now normatively homed: a `priority` row in the section 9.1 Accuracy column and in the preset file format (section 6.6, display only), the row shown on the Accuracy picker entry before selection (section 13.1, phase-4 bullet, phase-4 acceptance). The row has no execution effect in phase 4: Accuracy's round caps and models equal the default's, so the speed tie-break is a declared priority, not a mechanism; any future speed mechanism is out of scope. Phase 0's entry condition (every option set picked, document amended by re-audit) is met by this amendment, the three re-audit reports (comprehension, critic, codebase), and the re-audit decisions recorded below. The never-stop direction is a chat direction from 2026-10-29, not a rule stated in this document, and it does not by itself certify entry — the reports and decisions below do.

### Re-audit decisions (2026-10-29)

One review pass by fresh reviewers over the changed sections only (comprehension, critic, codebase reports under `review/re-audit/`). No second re-audit is used; residual conflicts are decided here under the same principles as the picks (browser-first, least surprise, lowest resource use, zero risk to existing runs).

| Re-audit finding | Decision |
|---|---|
| Self-certification (Blocker, comprehension + critic): the amendment claimed phase-0 entry was met by itself plus a report that did not yet exist, invoking a never-stop direction found nowhere in the design. | Fixed: the entry sentence now cites the amendment, the three reports, and this subsection. The never-stop direction is recorded as a chat direction, not a design rule. Phase 0 entry is met by the amended document as a whole, not by any single sentence. |
| H3 priority row homeless and effect-free (Major, all three reports). | Fixed: the row is now in the section 9.1 Accuracy column, the section 6.6 preset format (`priority`, display only), the section 13.1 picker spec, the phase-4 bullet, and phase-4 acceptance. Stated explicitly: no execution effect in phase 4 (caps and models equal the default); the speed reading is a declared priority, not a mechanism. |
| G1 flag homeless and ambiguous (Major, codebase + critic). | Fixed: scope decided in section 8.2 (picker `default` always means no profile; flag flips execution path for no-profile runs only when conformance is green), distinguished from `[workflows] enabled`, and owned by phase 3 (config definition, run-creation branch, distinct test). |
| Stale "proposed" status text (Major, all three reports). | Fixed by sweep: front matter, section 6.5/9.2/13.1 headings, section 10.2 wording, section 8.2 G sentence, section 16 intro, and both section 19 bullets now read as decided, with the decisions cited. |
| Hand-ship scope and circumvention (Major, critic + comprehension). | Decided: label travels on both dashboard and CLI ship paths; a hand ship is a human action with the label visible, not a bypass of save/creation refusals. No extra confirmation step. |
| B1 shorthand and B3 contrast (Major/Nit, critic + codebase). | Fixed: B1 row now says "sets quality level `none` while shipping is enabled" with the configuration-inherited case labelled per section 10.2, states that a label alone never blocks a ship, and carries the B3 stage-core exception. |
| Efficiency label accuracy (Major, critic). | Fixed: section 19 now records assurance per section 10.3 (cut to `none` with shipping on refused at save; other level changes are resolved settings). |
| E1 git-travel qualifier, H third difference, A1 templates, F1 `resolved_from` split, G1 shorthand (Nits). | Fixed in the table rows as noted above; the G1 title is the set's own label and always travels with its flag exception in the rationale. |

## 17. Acceptance criteria, test matrix, rollback

Acceptance criteria (each must be demonstrated with the artefact
named, not asserted):

* A user creates, saves, and runs a custom flow entirely from the
  browser, in both editor modes, with no terminal use and no
  `jev.toml` edit. Demonstrated: include/omit and parameter edits
  in both modes, plus a reorder refusal in canvas mode (the
  refusal is the specified behaviour, section 13.2). Artefact:
  screenshots of both editor modes plus the saved profile JSON.
* Both presets exist, validate, and behave as their section 9.1
  tables say on the same scenario, where "the same scenario" and
  the metric are defined in phase 4. Artefact: the phase-4
  comparison output (round counts, summed `cost_usd` over
  measured-`full` stages, sample sizes) or the static-check
  fallback with its reason stated.
* The default flow is unchanged for existing runs: the
  conformance suite passes under the stub/replay harness, and
  runs without a profile produce identical stage sequences,
  outcomes, and label output to the phase-0 baselines.
  Artefact: the conformance suite output plus the baseline
  comparison, stored under `tests/conformance/`.
* A suggested flow with its reasoning shown can be approved and
  run; an unapproved suggestion appears in no picker and changes
  no run; a below-threshold request says so plainly. Artefact:
  the suggestion result page screenshot plus the stored
  suggestion record.
* A flow that drops a safety behaviour is refused or labelled per
  the approved option set B, and the label appears on the run page
  and in the pull request body for shipped reduced runs.
  Artefact: screenshots of the run page and the pull request body
  for a reduced run (under B1/B3) or refusal messages for the
  refused classes (under B2).
* Verified live on the Mac dashboard, including a reduced-
  assurance run's label rendering and a snapshot surviving a
  profile edit mid-flight. Procedure: start a run, edit its
  profile while the run waits in a gate (the edit creates a new
  version), resume the run, and confirm the run page still shows
  the original version and the run finishes under it. Pass
  condition: version on the run page unchanged, run completes.
  Artefact: screenshots plus the session log.

Acceptance under each option pick (the checklists that replace
the list above when the gate picks otherwise):

* Under C3 (picker and toggles only): the editor-mode criterion
  is replaced by "presets plus a profile picker plus per-run
  setting overrides, with measurement for reporting", and the
  suggestion criterion does not apply.
* Under D2 (list only) or D3 (canvas only): the "both editor
  modes" criterion narrows to the picked mode; under D3 the
  browser-first law's mouse-free requirement is waived by the
  gate's pick, recorded as such.
* Under B2 (refuse everything): the label-rendering criterion is
  replaced by refusal verification — every floor behaviour drop
  refuses at save with the catalogue message, and no reduced run
  exists to render.
* Under H2 (wall-clock speed): the Accuracy criterion is replaced
  by speed criteria, which the re-audit must specify, because
  the section 7 language has no parallelism parameter today; the
  production-quality criterion does not apply.
* Under G2 (default re-expressed as a profile): the legacy-path
  criterion is replaced by a migration criterion — every run
  executes through the snapshot-aware path and the conformance
  suite passes as a migration test.
* Under G3: as G2, plus a phase-7 removal criterion (section 16,
  G3) after the soak release.

Test matrix:

* Validator unit tests: every catalogue entry in section 7.5,
  with each bundled shape counted separately (unknown model,
  quality level, cache mode, and learning profile are four cases;
  empty stage list, duplicate stage, and missing fields are
  three), including each invalid shape and its exact message.
* Conformance tests: legacy path vs. `default` preset path under
  the stub/replay harness (section 8.2), with recorded fixtures
  for Jev decisions and worker outputs, across scenarios
  covering plan revise loops, fix-task loops, score rework loops,
  `DESIGNING` on and off, ship settings (`enabled` true with
  `after_approval` `merge`/`ask`, and `enabled` false),
  `AUTONOMOUS`, `ASK_EVERY_STEP`, `MANUAL`, and `MEDIUM` modes,
  Halt and resume, and cancel mid-run. Expected output form:
  stage sequences, gate parkings, outcomes, and label output
  compared against the phase-0 baselines.
* Snapshot tests: a run-state × operation matrix — run states
  queued, awaiting a gate, executing, and `NEEDS_HUMAN` against
  operations edit, archive, and delete. Resume entry points
  named: the approve action on the run page and the resume
  command. Resumed runs keep their snapshot in every cell.
* Preset tests: each preset validates against a stated fixture
  (a repo with a named set of configured models); a preset
  referencing a removed model fails validation at startup, where
  "fails loudly" means the section 15 definition (plain message
  naming the setting plus a log line).
* Measurement tests: stage rows for a full run sum exactly to
  recorded totals on full rows and to "at least" the totals on
  partial rows; `null` tokens are never displayed as zero;
  gate-wait seconds never enter work seconds (tested with a
  known gate wait: park, approve, compare).
* Suggestion tests: below-threshold requests return preset-only
  mode labelled as such; approval saves exactly one new version
  (or records approval of the existing version when the draft is
  identical); discard changes only the record's status — no new
  version, no picker entry, no run change; a reason line without
  evidence is refused by the engine; cross-repo aggregation is
  labelled; dedup and staleness behave per section 12.4.
* Editor tests (dashboard): save-failure lists every error;
  creation-time re-validation catches on-disk edits (no run
  created, picker offered again); list mode and canvas mode
  produce identical profile JSON for the same selections per
  the input-equivalence table (section 13.2).
* Rollback tests: flag-off blocks profile selection at creation;
  in-flight snapshots finish; queued runs with profiles finish
  as snapshotted; recurring items create default runs while
  disabled; suggestion drafts stay visible but not pickable.

Rollback (executable procedure). The flag is `[workflows]
enabled` in `jev.toml` (a new configuration section), default
true. "Default on" means selection is enabled by default. Steps,
in order:

1. Set `[workflows] enabled = false`. The change takes effect on
   the next run creation (configuration is re-read at creation);
   no restart is required. Editor routes return a "workflows
   disabled" page for edits, while detail pages stay readable.
2. In-flight snapshots finish: snapshots are self-contained JSON
   on the run record and do not need the editor to keep running,
   but they do need the snapshot-aware lookup code to keep
   running — if that lookup code is itself the defect being
   rolled back from, rollback does not fix it, and the fix is a
   code fix, not a flag flip. This limit is stated, not hidden.
3. Queued runs with profiles finish as snapshotted. Recurring
   items create default runs while disabled (the profile pick is
   skipped and the run records `default`). Suggestion drafts stay
   visible but not pickable; approved-but-unrun versions stay
   stored but are not offered at creation until re-enabled.
4. No data migration is rolled back: stage rows and snapshots are
   additive and remain readable.

Rollback under each G branch: under G1, the legacy path is still
present, and the procedure above restores it for new runs. Under
G2, there is no legacy path to return to: rollback means
disabling selection plus in-flight snapshots finishing through
the same snapshot-aware path, and any defect in that path is
fixed forward — G2 has no rollback to a legacy path, and the
gate accepts that when it picks G2. Under G3 during the soak,
rollback is as G1; after removal, as G2.

## 18. Alternatives considered

* **Status quo plus toggles.** Ship presets as setting bundles
  with no editor and no suggestions (option set C3's shape).
  Rejected as the whole feature because the issue asks for a
  flowchart editor and suggestions, but kept as a phasing option
  because it delivers goal-conditioned flows at the lowest cost.
* **Profiles as `jev.toml` sections only.** A profile is a
  `[workflow.<name>]` table edited by hand. Rejected: it breaks
  the browser-first law, puts flow definitions in a file users
  edit by hand with no validation feedback, and cannot carry
  versions or snapshots cleanly.
* **A full graph interpreter replacing `_advance`.** Profiles
  become programs with edges, conditions, and loops, executed by
  a new interpreter for every run. Rejected: it replaces the
  pipeline's most-tested code with a second state machine, and
  every handler-chosen transition (D3 verdicts, D6 votes, score
  gates) would need re-expression in the graph language before
  the first profile could run.
* **AI-generated flows without approval.** The suggestion engine
  writes and applies flows directly. Rejected: it violates the
  issue's explicit constraint against silent application, removes the
  human from a decision that changes what checking code receives, and makes
  every surprising run behaviour attributable to an invisible
  author.
* **Per-stage model routing as the whole feature.** Instead of
  profiles, let users pick models per stage and call that the
  flow. Rejected as incomplete: model choice is one parameter
  among many (gates, rounds, quality level, ship mode), and a
  model picker alone cannot express "shorter flow" or "stricter
  flow" at all.

## 19. Open questions and blockers for the design

* **Per-stage measurement does not exist.** Section 11 is a
  prerequisite phase. Until it lands, efficiency claims are
  qualitative and suggestions are preset-only. This is the
  design's largest dependency, and it is a build, not a decision.
* **Suggestion data is thin by default.** Learning is off by
  default, CDA grading is off by default and backend-less on the
  Mac, and semantic shadow data measures answer agreement, not
  stage-skip safety. Thresholds in section 12.3 keep the engine from overstating
  thin data, but they also mean most repos will see preset-only
  suggestions for a long time. The design does not promise
  otherwise.
* **The default's representation (option set G) is the largest
  behaviour risk.** Re-expressing the default as a profile touches
  every run; keeping two paths pays maintenance forever. The
  human's pick here constrains the whole implementation.
* **Editor canvas accessibility.** Canvas mode must not become
  the only usable mode; list mode is specified as equal, but its
  equality must be tested (same inputs, same profiles, same
  messages), not assumed.
* **SITEMAP coordination.** This design creates
  `docs/designs/SITEMAP.md` in its worktree because none exists on
  main. The parallel #366 design branch (draft PR #367) also adds
  a SITEMAP. Owner and step: whichever design merges second
  merges the two SITEMAPs by hand before implementation starts,
  and the merged file must contain both designs' entries with
  their paths and statuses.
* **Manual ship of a reduced run.** Section 10.2 states the
  label travels on a hand ship and the ship proceeds. Whether a
  hand ship of a reduced run should also require an explicit
  confirmation is a human-gate question: the resolution chose label-travels (consistent with the mix). Decided 2026-10-29: label travels only, no extra confirmation step, on both the dashboard and CLI ship paths (the section 10.3 writer covers both). A hand ship is a human action with the label visible in the PR body, so it is not a bypass of profile-save refusals: refusals apply at save and creation, and the hand ship does not re-decide them.
* **Goal-label clock.** Stage records exist from phase 1, but
  goal-labelled evidence starts only when presets land (phase
  4), because legacy runs record goal `null` and only a stated
  goal counts as evidence (section 12.1). Phase 6's entry check
  measures that volume and reports it; the plan does not assume
  it.
* **Efficiency's quality promise.** The resolution changed the
  Efficiency preset from quality `basic` to "repo configuration,
  never lowered", because `basic` drops secret scanning, SAST,
  and dependency audit — a security cut, not an efficiency gain.
  Decided 2026-10-29 (confirmed): Efficiency saves tokens through rounds and priced models, not through a lower quality level. A quality cut in a custom profile is recorded through the assurance computation per section 10.3: a cut to `none` with shipping on is refused at save, and other level changes are resolved settings whose assurance follows section 10.3.
* **No implementation work was started and no Mac verification was
  done.** Both belong to the post-approval phase, after the
  section 16 gate and the reserved re-audit.
