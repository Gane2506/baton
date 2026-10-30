# Profile-based process flows (Baton issue #368)

Design phase only. No production code is changed by this document.
Implementation happens only after the human approval gate in section 16.

Working term: this document calls the new concept a **workflow profile**
(a profile of the pipeline). That term collides with two existing uses
of "profile" (learning profiles, workspace/server profiles); section 16,
option set A, decides the final name and route. If the human picks a
different name, the document is amended by re-audit before
implementation; nothing in the mechanism depends on the word.

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

Existing bypasses, stated plainly because the safety floor must deal
with them: quality level `none` runs only the task's own test command;
`AUTONOMOUS` skips the plan gate; `score_skip_human` skips the score
gate; a missing ship score still ships. Any floor rule that calls
these stages "non-removable" while leaving these settings in place is
false. Section 10 deals with this instead of pretending otherwise.

### 2.3 The knobs a preset can actually turn

`config/defaults.py` defines the settings a profile can map onto
without inventing new machinery:

* `[roles.*]` model lists per role (planner, validator, coder,
  reviewer, code_reviewer, learner, designer), and
  `models.planner_policy` (`free_training_ok`, `mixed`,
  `mixed_no_training`, `paid_only`).
* `[quality]` level and `score_threshold`, `score_max_retries`,
  `score_skip_human`.
* `[budget]` per-run and per-task USD caps, `max_plan_rounds`,
  `max_task_attempts`, `test_timeout_s`.
* `[integration]` test command and `max_fix_rounds`.
* `[cache]` settings, including the semantic section (below).
* `[autonomy]` mode (`standard`, `autonomous`, `ask_every_step`) and
  `[approval] require_plan_approval`.
* `[ship]` mode and `after_approval` (`merge` or `ask`).
* `[dispatcher.cost]` cost accounting for routing decisions.
* `[learning]` settings and the run's learning profile.

A preset that cannot be expressed as values for these knobs, plus
stage inclusion and gate policy (section 7), is not a preset — it is a
new feature, and the design must say so wherever that happens.

### 2.4 Learning profiles already exist, and the name is taken

`core/learning/profiles.py` implements learning profiles: a
`learning_profiles` table with create, list, enable, flush, and delete,
plus a per-profile lesson count. Lessons attach to a profile; the
learning graph tab and the `/profiles` dashboard page show them. A
second concept called "profile" will confuse every user who opens
`/profiles` expecting one kind and finding the other. The design keeps
the learning-profiles concept untouched and gives the new concept its
own name, route, and page (option set A).

The learning step itself (`_finish` plus `core/learning/learner.py`)
distills lessons at the end of a run: what struggled, what worked,
which role it applies to. Lessons are the suggestion engine's
struggle/worked signal, not its cost signal — lessons carry no token
or time data.

### 2.5 Cache: exact serving exists, semantic serving does not

`core/cache.py` serves exact cache entries keyed (among other things)
by repo fingerprint; a key whose repo fingerprint is `unknown` is
never served or stored (issue #201). Semantic similarity
(`similar()`, a table scan over SimHash fingerprints) exists behind
`[cache.semantic] enabled`, which defaults to off. Shadow mode
(`log_shadow`) measures whether a semantic hit would have agreed with
the exact answer, without serving anything. Calibration only ships a
threshold where the curve shows at least 0.99 agreement over up to
1500 decisions, at floors 0.90/0.95/0.98/0.99.

Consequences the design honours: "aggressive cache reuse" in a preset
can only mean exact-cache reuse, because that is the only serving that
exists. Shadow data may inform a suggestion ("similar past work was
answered from cache in shadow mode N times"), but it must never be
presented as serving evidence, and it says nothing about whether
skipping a stage is safe. Live semantic serving stays off, and this
feature does not change that.

### 2.6 No per-stage measurement exists

Totals live on the run record. Tokens sit inside attempt `result_json`
or decision `details` JSON, or are `None` — Codex reports no cost, and
Jev decisions carry `usage` with a computed cost only when the
provider reports usage. Gate waiting time is mixed into wall time;
there is no stage record, no stage duration, and no stage token column
anywhere. The dashboard has a Spend page (`/spend`) fed by run-level
views, not by stage records.

This is the design's central prerequisite: no efficiency claim and no
usage-based suggestion is honest until stage records exist. Section 11
specifies them, and section 15 phases them before any suggestion that
changes a run.

### 2.7 Dashboard patterns the editor must reuse

Routes (`dashboard/app.py`) relevant to this feature: `/` (board),
`/runs/new` (POST, creates a run), `/runs/{run_id}` (run page),
`/runs/{run_id}/live` (live fragment), `/queue` and `/queue/add`
(queued runs), `/learning` (learning page), `/profiles` (learning
profiles), `/spend`, `/design-systems` (a user-level library with
list/detail/showcase/code pages — the closest existing model for a
user-level library of named artifacts). Run creation already flows
through `Pipeline.start` with flags; the design maps profile selection
onto that path rather than inventing a second creation path. Forms
post through checked forms with off-loopback token gating, the same
pattern any new editor route must follow.

## 3. Goals and non-goals

Goals:

* A user can pick a workflow profile when creating a run, from the
  browser, and the run executes that profile's flow.
* A user can create, rearrange, save, and version a workflow profile
  entirely from the browser, with invalid graphs refused in plain
  language at save time and re-checked at run start.
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

1. **The drawing lies.** A 7-box flowchart hides loops, gates,
   conditional stages, and handler-chosen transitions. A user saves a
   drawing, runs it, and gets a different flow. Answered by section 7:
   the editor's node palette is the real stage list, loops are bounded
   parameters (never drawn cycles), gates are properties of steps
   (never free-floating nodes), and the validator's accepted set is
   exactly the set the executor runs.
2. **A second state machine rots.** If the profile interpreter drifts
   from `_advance`, identical-looking flows diverge, and the divergence
   is discovered by users mid-run, after tokens are spent. Answered by
   section 8: one shared validator, a conformance suite for the
   default, and a rule that the default stays on the legacy path
   until the interpreter reproduces it.
3. **The safety label decorates instead of protecting.** A
   "reduced assurance" badge that appears on the editor page but not
   on the run page or the shipped PR changes nothing. Answered by
   section 10: the label travels on the run record, renders on the run
   page and in the PR body, and blocks shipping when the enforcement
   option says so.
4. **Presets rot silently.** A preset that names a model, a quality
   level, or a cache mode keeps working after that model is removed
   or that check changes meaning, and fails only by producing worse
   runs. Answered by section 9: presets are validated at save and at
   run start against the live configuration, and a stale reference
   refuses with a named reason instead of falling back quietly.
5. **Suggestions launder thin data.** Ten runs, three grades, and a
   shadow log become "users like you save 40% with this flow".
   Answered by section 12: minimum-data thresholds, preset-only mode
   below them, evidence counts on every reason line, and no
   percentage that is not computed from measured medians.
6. **A profile edit corrupts a running run.** A run that references a
   live profile changes behaviour mid-flight, and a resumed run
   resumes under a flow it never started with. Answered by section 6:
   runs snapshot an immutable profile version at creation; edit
   creates a new version; delete archives.
7. **Precedence fights are invisible.** A profile, a workspace
   profile, `jev.toml`, a per-run flag, and an autonomy mode disagree,
   and the winner is whichever the code happens to read last.
   Answered by section 9: one explicit precedence table, applied in
   one place, and the run record names the winning source per
   setting.
8. **The editor excludes people.** A drag-only canvas fails on narrow
   screens and without a mouse, which breaks the browser-first law
   for those users. Answered by section 13: a list-based editor that
   builds the identical graph through the identical validator is a
   first-class path, not an afterthought.
9. **Scope confusion.** A profile saved "for this repo" used in
   another repo references settings that repo does not have, and
   fails or silently degrades. Answered by section 6: scope is part
   of identity, and cross-scope use re-validates against the target
   repo's configuration at run start.
10. **Goal words lie.** "Performance" is used for both speed and
    quality, so a suggestion conditioned on "performance" cannot be
    checked. Answered by option set H: the human pins down what the
    Accuracy preset optimizes before implementation.

## 5. Concepts and naming

* **Workflow profile.** A named definition of a run's flow: an
  ordered stage list (section 7), per-stage parameters, gate policies,
  and setting overrides (section 9). A profile is data, validated
  before it is saved and again before it runs. It contains no code,
  no shell commands, and no model names outside the configured
  allowlist (section 14).
* **Flow version.** One immutable revision of a profile. Versions are
  numbered per profile, starting at 1, and never edited in place.
  Editing a profile creates a new version; the old versions remain
  readable for runs that snapshotted them.
* **Flow snapshot.** The profile version frozen onto a run at
  creation, stored on the run record. The run executes the snapshot
  even if the profile is later edited, archived, or deleted.
* **Assurance.** A run-level value computed from its snapshot:
  `full` when every safety behaviour in section 10 is present,
  `reduced` when any is absent under the label option, plus the list
  of absent behaviours. Assurance is displayed, never just stored.
* **Goal.** A label on a run and on a suggestion request:
  `efficiency`, `accuracy`, or `custom`. The goal is recorded so
  future suggestions have a signal to learn from; a run without a
  stated goal records `null`, and the suggestion engine treats
  goal-blind history as weaker evidence (section 12).
* **Suggestion.** A draft profile version plus a reasoning record
  (one reason line per change, each with evidence counts). A
  suggestion is inert until a user approves it, at which point it is
  saved as a new profile version like any other edit. Approval covers
  one profile version, not all future runs.
* **Preset.** A built-in profile shipped with Baton: `default`
  (today's pipeline, behaviour-identical), `efficiency`, `accuracy`.
  Presets are data in the same format as user profiles, validated by
  the same validator, and versioned with the Baton release that
  shipped them (a preset's meaning never changes silently under a
  newer Baton; a new release ships a new preset version).

## 6. Profile model: identity, storage, scope, snapshot

### 6.1 Identity

A profile has a stable id, a display name, a scope, and a version
number. The id is the identity; the display name is not. Two profiles
may share a display name in different scopes; lists always show the
scope badge next to the name, and runs always reference the id plus
the version, never the name. The id format is
`<scope>:<slug>`, where scope is `repo` or `user` and slug follows
the existing profile-name rule (letters, digits, `-` and `_`,
starting with a letter or digit, at most 40 characters). A run that
references an id that no longer resolves refuses at run start with
the message "flow <id> version <n> is archived or deleted; pick a
current flow", never by substituting another profile.

### 6.2 Storage

* Repo scope: one JSON file per profile version under
  `<repo>/.jev/workflow-profiles/<slug>/v<n>.json`, plus a
  `<slug>/current.json` pointer to the latest version number.
  Repo-scope profiles travel with the repo and are visible to
  anyone with the repo.
* User scope: the same layout under `~/.jev/workflow-profiles/`.
  User-scope profiles are visible in every repo on that machine.
* Built-in presets ship as data files in the Baton package
  (`jev_agent/data/workflow-presets/`), versioned with the release.
  A preset is never edited in place; a user who edits a preset
  creates a user- or repo-scope copy under a new id.
* The store validates every file on read (schema plus the section 7
  validator). A corrupt or schema-invalid file is reported as
  "flow <id> version <n> is unreadable" and is never executed, never
  silently repaired, and never partially applied.

### 6.3 Versioning and deletion

* Creating a profile creates version 1. Editing creates version
  n+1; version n is never modified afterwards.
* A run snapshots the exact version it was created with (section
  6.4). Old versions are retained while any run references them.
* Deleting a profile archives it: the definition files stay, the
  profile disappears from creation lists, and existing snapshots
  keep running. Permanent deletion is refused while any run
  references any version, and the refusal names the referencing run
  count. Retention of archived versions follows the repo's existing
  run-retention behaviour; the design does not invent a second
  retention policy.
* A preset's version is the Baton release that shipped it. Upgrading
  Baton never rewrites a run's snapshot and never changes what a
  stored preset version means; a newer release adds a newer preset
  version alongside the old one.

### 6.4 Snapshot on the run

At run creation, the chosen profile version is resolved to its full
parameter set (stage list, per-stage parameters, gate policies,
setting overrides, assurance computation) and that resolved set is
stored on the run record as the flow snapshot. The executor reads
only the snapshot, never the live profile. Editing, archiving, or
deleting the profile afterwards cannot change the run. Resuming a
`NEEDS_HUMAN` run resumes under the same snapshot (`resume_state`
already records where the run stopped; the snapshot records what
flow it runs under). The run page displays the profile id, version,
goal, and assurance for the life of the run.

### 6.5 Scope rule (proposed; option set E may narrow it)

Both scopes exist. A run created in a repo may pick a repo-scope
profile from that repo or any user-scope profile. A repo-scope
profile may not be picked from another repo. When a user-scope
profile is used in a repo, its setting overrides are re-validated
against that repo's configuration at run start (models must exist in
that repo's role configuration; quality levels must parse; cache
modes must be known), and a failure refuses the run with the
offending setting named. If option set E narrows scope to one kind,
this section narrows with it by re-audit; the snapshot mechanism
does not change.

## 7. The profile language: what a profile may express

A profile is a stage list plus parameters. It is not a general
graph language. This restriction is the design's central honesty
measure: the stages have data dependencies (a plan must exist before
it is validated; code must exist before it is integrated), so an
arbitrary edge order is not a flow at all, and a validator that
accepted one would either refuse it at run time (after the user
believed it saved) or run something different from the drawing.

### 7.1 Stage list and canonical order

The node palette is exactly the 7 work states, in canonical order:

```
INTAKE -> PLANNING -> VALIDATING -> DESIGNING -> EXECUTING
    -> INTEGRATING -> SHIPPING
```

A profile's stage list is a subsequence of this order. Reordering
two stages is refused: the validator's message names the pair and
the dependency ("EXECUTING needs the approved plan that VALIDATING
produces; it cannot run before VALIDATING"). Omitting a stage is
allowed only where section 7.3 says so. The editor still draws nodes
and edges — the drawing is the stage list rendered as a chain — but
an edge that skips backwards or jumps over a required stage is a
validation error at save time, not a shape the executor must
interpret.

### 7.2 Gates are properties of steps, not nodes

Each work step carries a gate policy: `default` (the existing
behaviour for that step under the run's autonomy mode), `require`
(park in the step's `AWAITING_*` gate even when the mode would not),
or `skip` (run the step without parking, only where the existing
rules already allow skipping — `skip` never overrides an explicit
human gate the user asked for elsewhere, and never skips the ship
gate when `[ship] after_approval` is `ask`). A gate is therefore
never orphaned, never duplicated, and never drawn as a separate node
that a user could delete without understanding what was deleted.
The editor shows each step's gate policy on the step itself.

### 7.3 What may be omitted, and what may not

* `DESIGNING` may be omitted: it is conditional today (UI work
  only), so omitting it changes nothing for non-UI runs and skips
  design for UI runs that do not want it. A profile that omits
  `DESIGNING` still runs it when the run has UI work only if the
  profile's `designing` parameter says `when_needed` (the default);
  `never` skips it even for UI work, and that choice is recorded in
  the assurance computation (design is not a safety behaviour, so
  omitting it never reduces assurance).
* `INTAKE`, `PLANNING`, `VALIDATING`, `EXECUTING`, `INTEGRATING`
  may not be omitted. A profile that omits one is refused with the
  dependency named. (If option set B chooses a label-only floor,
  this rule is the first thing re-audit revisits, because a
  label-only floor with a non-omittable core is the same as the
  refuse floor for these five stages.)
* `SHIPPING` may be omitted only by choosing ship mode `off`
  (the run ends at `DONE` after a ship vote, as today). Omitting
  `SHIPPING` while ship mode is `merge` or `ask` is refused as
  incoherent.
* Loops are parameters, never drawn cycles: `max_plan_rounds`,
  `max_fix_rounds`, `score_max_retries`, `max_task_attempts`, each
  bounded above by the corresponding `[budget]`/`[integration]`/
  `[quality]` ceiling when one is configured, and never below 1
  where the step needs at least one attempt. A drawn cycle is
  refused with "flows do not loop by drawing; set the round limit
  on the step".

### 7.4 Parameters a profile carries

* Stage list (section 7.1) and gate policy per step (section 7.2).
* `designing`: `when_needed` or `never`.
* Round caps (section 7.3).
* Quality level (`none` only when shipping is off — see section
  10), `score_threshold`, `score_max_retries`, `score_skip_human`
  (only within the floor rules of section 10).
* Role model overrides: per role, a model name that already exists
  in that repo's role configuration. An unknown model name refuses
  with the model and role named. A profile never invents a model,
  a provider, or a price.
* Planner policy (`free_training_ok`, `mixed`, `mixed_no_training`,
  `paid_only`).
* Cache mode: `exact` (default) or `exact_off`. There is no semantic
  serving mode to select, because serving does not exist; a profile
  that names one is refused.
* CDA grading: `off` (default) or `on` (advisory only; the threshold
  stays 0.7; grading never blocks or auto-merges).
* Ship mode and `after_approval`, within the existing values.
* Learning profile name (optional): the learning profile whose
  lessons the run reads and to which its outcome is distilled. An
  unknown learning profile refuses with the name.
* Goal label (recorded, section 5): `efficiency`, `accuracy`, or
  `custom`. The goal label changes nothing about execution by
  itself; it is the suggestion engine's training signal and the
  preset's documented intent.

### 7.5 The invalid-graph catalogue

The validator refuses each of these shapes with the stated message
at save time, and re-checks the snapshot at run start (a profile
edited on disk between save and run start is caught there):

* Unknown node: "Unknown stage <name>. Stages are INTAKE, PLANNING,
  VALIDATING, DESIGNING, EXECUTING, INTEGRATING, SHIPPING."
* Reordered stages: "EXECUTING cannot run before VALIDATING:
  execution needs the approved plan." (Pair and dependency named.)
* Omitted required stage: "VALIDATING cannot be removed: integration
  needs a validated plan." (Stage and dependency named.)
* Drawn cycle: "Flows do not loop by drawing. Set the round limit on
  the step instead." (Section 7.3.)
* Gate orphan: unreachable by construction, because gates are step
  properties (section 7.2); a file that carries a gate without its
  step is a schema error: "Gate without a stage is not a flow."
* DESIGNING/SHIPPING incoherence: "SHIPPING is omitted but ship mode
  is merge. Set ship mode to off, or include SHIPPING."
* Unknown model, quality level, cache mode, or learning profile:
  named in the refusal, with the allowed values listed.
* Empty stage list, duplicate stage, or missing snapshot fields:
  "A flow needs at least INTAKE, PLANNING, VALIDATING, EXECUTING,
  INTEGRATING, in that order."

Validation runs in one place, shared by the editor and the executor:
the editor calls it before saving, the executor calls it on the
snapshot before the first step. A flow that saves cleanly cannot
fail differently at run time for a validation reason.

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
  and `execution_gate_target` helpers — the helpers gain a
  snapshot parameter; they do not gain a second gate system.
* Round caps, quality level, score settings, ship mode, cache mode,
  and role model overrides are read from the snapshot, falling back
  to the repo configuration for any parameter the profile does not
  set. The fallback order is the precedence table in section 9.
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
content, and no profile can override them.

### 8.2 The default stays on the legacy path until conformance passes

Runs without a profile (`profile_id` null) execute exactly today's
code path: no snapshot, no new lookup, no behaviour change. The
`default` preset is defined as the parameter set that reproduces
today's behaviour, and a conformance suite (same cases through the
legacy path and through the `default` preset path, requiring
identical stage sequences, gate parkings, and outcomes, including
Halt/resume and cancel cases) must pass before any run is allowed to
execute the default via the profile path. Until then, picking
`default` is a no-op that records the choice and runs the legacy
path. Whether the default is ever re-expressed as a profile, or
stays hardcoded with the conformance test as its guard, is option
set G.

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
| Stage list | Full canonical list | Full list minus nothing required; DESIGNING `when_needed` | Full canonical list; DESIGNING `when_needed` |
| Quality level | Repo configuration | `basic` | `production` |
| Planner policy | Repo configuration | `free_training_ok` | Repo configuration (never narrowed by the preset) |
| Role models | Repo configuration | Cheapest configured model per role where the repo has one configured | Repo configuration (never narrowed by the preset) |
| `max_plan_rounds` | 3 | 2 | 3 |
| `max_fix_rounds` | 2 | 1 | 2 |
| `score_max_retries` | 2 | 1 | 2 |
| `score_threshold` | 85 | 85 (unchanged: lowering the bar to save tokens is a quality cut, not an efficiency gain) | 85 |
| Cache mode | `exact` | `exact` (reuse is already the default; the preset never disables it) | `exact` |
| CDA grading | Off | Off | On (advisory only; threshold stays 0.7) |
| Ship mode / `after_approval` | Repo configuration | Repo configuration | Repo configuration |
| Autonomy mode | `standard` | `standard` | `standard` |
| Learning profile | Run's existing setting | Run's existing setting | Run's existing setting |
| Goal label | `null` | `efficiency` | `accuracy` |

Two honest statements about this table. First, Efficiency's savings
come from fewer rounds, cheaper models, and a lower quality level —
not from cache magic, because exact-cache reuse is already the
default and semantic serving does not exist. Second, Accuracy as
defined here is close to the default flow plus CDA grading and a
production quality level; if that closeness means Accuracy "just
renames the default", option set H exists to say so and to redefine
what Accuracy optimizes.

### 9.2 Precedence (proposed; option set F may reorder it)

When sources disagree, the winner is decided by this order, applied
in one place at run creation, and the run record names the winning
source per setting:

1. Explicit per-run flags at creation (`--quality`, `--plan-only`,
   `--auto-ship`, and their dashboard equivalents). A flag the user
   types for this run beats everything, because it is the most
   specific instruction.
2. The run's flow snapshot (profile version).
3. Repo `jev.toml` and workspace profile settings.
4. Baton defaults.

Autonomy mode is the exception that proves the order needs care: a
profile's gate policy `skip` never overrides an explicit human gate
the user asked for (section 7.2), and a profile never turns an
`AUTONOMOUS` run into a gated run or vice versa — autonomy mode is
set at creation outside the profile, and the profile's gate policies
apply within it. If option set F reorders precedence, this paragraph
is re-audited with it, because gate behaviour is where a wrong order
silently removes a human from the loop.

## 10. Safety floor: enforced behaviours, and the label

### 10.1 The floor as behaviours

A safety behaviour is something the pipeline enforces, not a node
that is present. The floor is this list, and a profile run must
satisfy every item or fall under section 10.2:

1. The quality gate runs at `INTEGRATING` and a failed gate never
   ships (level may vary; `none` is addressed in section 10.2).
2. Merge conflicts never ship; failed integration tests never ship.
3. A ship vote passes through the score gate at its threshold, or
   the run stops for a human after `score_max_retries` rounds.
4. A ship vote passes through the ship decision: merge, ask (with
   `AWAITING_SHIP` approval), or off (run ends `DONE` unshipped).
5. The plan passes through D3 validation by a validator (a
   different vendor than the planner when the repo configures it)
   and through the plan approval gate, except under `AUTONOMOUS`
   opt-in, which is an explicit creation-time choice outside any
   profile.
6. Every shipped result has a branch, a test record, and a quality
   record the run page can show; a run that cannot produce them
   does not ship.

### 10.2 Enforcement and the existing bypasses

The design's position is a mix, decided finally by option set B:

* A profile is refused at save time when it sets quality level
  `none` while shipping is enabled, sets `score_skip_human` while
  shipping is enabled, or omits any of `VALIDATING`, `EXECUTING`,
  or `INTEGRATING`. These refusals use the section 7.5 catalogue
  messages.
* Configuration-level bypasses outside profiles (quality `none` in
  `jev.toml`, `AUTONOMOUS` mode, `score_skip_human` in config) are
  not silently rewritten by this feature. Instead, the assurance
  computation (section 10.3) applies to every run, profiled or not,
  so a legacy run that uses one of those bypasses carries the same
  reduced label a profiled run would. The label is therefore not a
  tax only new flows pay.
* Under the label option (if option set B picks it over refusal for
  some behaviour class), the profile saves, the run executes, and
  the label travels (section 10.3). Under the refuse option, the
  profile does not save. The design does not allow a third outcome
  in which a dropped safety behaviour is neither refused nor
  labelled — that outcome is the failure mode the issue forbids.

### 10.3 Assurance and where the label lives

Assurance is computed at run creation from the snapshot (and, for
legacy runs, from the run's effective settings) and stored on the
run record:

* `full`: every floor behaviour in section 10.1 is present.
* `reduced`: otherwise, plus the exact list of absent behaviours
  (for example: `["quality gate: none", "ship score gate:
  skipped"]`).

The label renders on the run page next to the flow id and version,
on the board's run row, and in the PR body for any shipped result
("Assurance: reduced — quality gate: none"). A reduced run is not
blocked from shipping by the label alone (unless option set B
chooses refusal for that behaviour class); the label's job is to
make the reduction impossible to miss, not to re-decide the user's
choice in secret.

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
  `profile_id`, `profile_version`, `goal` (copied from the run).
* `started_at`, `ended_at` (wall clock), `work_seconds` (ended minus
  started minus gate-wait seconds — gate waiting is recorded
  separately, never mixed into work time).
* `gate_wait_seconds` (time parked in the step's `AWAITING_*` gate,
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
  (conditional stage not run, e.g. `DESIGNING` when not needed),
  plus the halt reason when halted.

### 11.2 Recording points

* Stage entry and exit: a wrapper around each `_advance` step call
  writes the row; the wrapper is the only writer, so a stage cannot
  run unrecorded by taking a different path.
* Gate timing: the transition into an `AWAITING_*` state starts the
  gate clock; the approval (or timeout, where one exists) stops it.
* Ship timing: the ship decision and the shipping work are recorded
  as `SHIPPING` stage rows, not folded into `INTEGRATING`.
* Decision and attempt tagging: Jev decisions and worker attempts
  made during a stage carry the stage name, so their reported usage
  aggregates into that stage's row instead of living only inside
  `result_json` / `details` JSON.
* Cache events during a stage increment that stage's counters;
  semantic shadow observations increment `semantic_shadow_hits`
  only.

### 11.3 Aggregation rules (what the numbers mean)

* A stage's tokens are the sum of its attempts' reported tokens;
  attempts that report `null` contribute nothing and lower the
  row's completeness flag (`measured: partial` vs `measured: full`).
  A stage with any `null` attempt is never quoted as an exact token
  number — it is quoted as "at least N tokens" or not quoted.
* Saved tokens from a cache hit are not counted as negative tokens.
  A cache-served stage reports `cache_served: true` and the tokens
  of the original entry when known, else `null`; the suggestion
  engine may say "this stage was served from cache N times", never
  "this stage saved X tokens" unless the original cost is known.
* Medians, not means, summarize costs across runs (one runaway run
  must not set the price of a stage). Every quoted number carries
  its sample size ("median 12,400 tokens over 14 runs").
* Per-profile and per-goal aggregates are computed from stage rows
  filtered by `profile_id`/`profile_version`/`goal`; a suggestion
  never mixes goals (Efficiency evidence never justifies an
  Accuracy recommendation).

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

The engine never reads model prices it was not configured with,
never reads another repo's runs for a repo-scope suggestion
(user-scope suggestions may aggregate across the user's repos only
when the user asks for a cross-repo suggestion, and say so), and
never uses a run whose goal label is `null` as evidence for a
goal-conditioned recommendation.

### 12.2 What it produces

A suggestion is a draft profile version plus a reasoning record.
The reasoning record has one line per change, each line in this
form: what changes, the evidence (counts and medians with sample
sizes), and what would falsify it. Examples of honest lines:

* "Drop CDA grading from this flow: grading is advisory, off by
  default, and your last 9 graded diffs produced no routing change
  (best tier: none, fewer than 3 graded diffs per tier)."
* "Keep VALIDATING: it caught plan defects in 7 of your last 12
  runs; dropping it would save a median 8,100 tokens over 12 runs
  (measured: full) against 7 defect escapes."
* "Do not shorten INTEGRATING: 3 of your last 10 runs needed a fix
  task from failed integration tests."

A suggestion that cannot produce an evidence line for a change
does not make that change. A suggestion with no changes is not
presented as a suggestion; the engine says "no change beats your
current flow on your stated goal with the data available" and
shows the numbers.

### 12.3 Thresholds (design values, tunable constants)

* A usage-based suggestion requires at least 10 completed runs
  with stage records for the scope it covers (repo, or user for a
  cross-repo suggestion), within a 90-day freshness window. Older
  runs age out of the evidence set; the reason lines say so when
  they do.
* A cheaper-model or cheaper-tier recommendation requires at least
  5 runs with that model/tier at the run's quality level, with no
  quality-gate failure attributable to that model/tier in the set,
  and (for CDA tiers) the existing 3-graded-diff floor.
* A stage-drop recommendation requires at least 10 runs in which
  the stage ran, with a measured-median saving and a defect-escape
  count stated alongside it; a stage that caught a defect in any of
  the last 10 runs may not be recommended for dropping on cost
  grounds alone.
* Below any threshold, the engine runs in preset-only mode: it may
  propose a preset, with the preset's documented behaviour as the
  reason, and must label the proposal "preset, not measured for
  your history".

### 12.4 Approval flow (the only way a suggestion changes a run)

1. The user asks for a suggestion for a stated goal, from the run
   creation page or the workflows page.
2. The engine returns a draft profile version plus its reasoning
   record. The draft is stored as a suggestion record, not as an
   active profile version, and it appears nowhere as a runnable
   flow until approved.
3. The user reviews the reason lines and either approves (the draft
   is saved as a new profile version, like any other edit, and may
   then be picked for a run) or discards it (the suggestion record
   is kept for audit, marked discarded, and never applied).
4. Approval covers that profile version only. It does not approve
   future suggestions, does not change any existing run, and does
   not change the default.

### 12.5 What the engine never does

* Never writes a profile, never edits a profile, never changes a
  run, never changes a default, never enables semantic serving,
  never overrides a Jev decision, never reorders a running run.
* Never recommends dropping a floor behaviour in section 10.1
  without the reduced-assurance label travelling with the
  recommendation (the recommendation's reason lines must name the
  assurance change).
* Never presents a preset as a measured suggestion, never presents
  shadow data as serving data, never quotes a token number without
  its sample size and completeness flag.

## 13. Dashboard: pages, editor, and validation feedback

### 13.1 Routes and pages (proposed; final names follow option set A)

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
* Run page (`/runs/{run_id}`): the snapshot's profile id, version,
  goal, and assurance badge next to the stage timeline; a reduced
  run names its absent behaviours there.
* Board (`/`): run rows show the assurance badge for reduced runs.

All new routes follow existing dashboard patterns: checked forms,
off-loopback token gating, server-side validation before any save,
and error text a non-programmer can act on.

### 13.2 The editor: canvas plus list, one validator

The editor has two modes that build the identical profile through
the identical validator:

* Canvas mode: stages as nodes in canonical order, edges drawn
  between consecutive stages. The user drags stages out of the chain
  (omission, where allowed), reorders only within what the
  validator accepts (in practice: no reordering passes, and the
  editor says why when one is attempted — section 7.1), and edits
  per-stage parameters and gate policies on the node itself.
* List mode: the same stage list as an ordered list with
  include/omit toggles, parameter fields, and gate policy selects.
  No dragging, no mouse dependency, fully usable on narrow screens.
  List mode is not a lesser editor: it produces the same profile
  format, runs the same validator, and shows the same messages.

Validation feedback timing: at save (the full catalogue, section
7.5, with the offending stage named and the fix stated), live
while editing where cheap (unknown model names, duplicate stages),
and again at run start on the snapshot. A save that fails lists
every error, not just the first, so the user is not sent around
the form one error at a time. A run-start failure names the
snapshot version and the setting, and offers the profile picker
again — never a bare traceback, never a silent fallback to the
default.

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
  the server check is the rule. Profile JSON has a size cap (64 KB
  per version file); larger files refuse with the size stated.
* Profile files contain no secrets, no tokens, and no credentials.
  A profile that carries a field outside the schema is refused
  rather than stored with the field ignored — silent field-dropping
  is how a user believes a setting is saved when it is not.
* The suggestion engine reads only the inputs in section 12.1, and
  only within the scope the user asked for (repo, or cross-repo
  when explicitly requested). Suggestion records contain aggregate
  counts and medians, never run contents, prompts, or diffs.
* Assurance labels are computed server-side from the snapshot at
  run creation. A browser-side label is display only and is never
  trusted for shipping decisions.

## 15. Detailed implementation plan

Phases run in order. A phase does not start until the previous
phase's acceptance checks pass, except phase 0's contract work,
which is documentation plus the conformance suite skeleton and may
overlap phase 1. No phase changes the default flow's behaviour for
runs without a profile. File paths are relative to the repo root;
"new" means created, "change" means modified. No production code is
written during the design phase; this section is the plan the
implementation phase will follow.

### Phase 0 — Contract: safety floor, knobs, graph-to-handler mapping

* `docs/designs/profile-workflows/design.md` (this document,
  amended by re-audit after the section 16 gate): the floor
  behaviour table, the allowed-knob list, and the stage/gate
  catalogue become the contract.
* `tests/` (new conformance suite skeleton): cases that run the
  same scenarios through the legacy path and record stage
  sequences, gate parkings, and outcomes for later comparison.
  Acceptance: the suite documents today's behaviour before any
  profile code exists, so a later change cannot quietly redefine
  "unchanged".

### Phase 1 — Measurement: stage records

* `src/jev_agent/core/store/records.py` (change): stage record
  model and run-record fields for the flow snapshot
  (`profile_id`, `profile_version`, `goal`, `assurance`,
  `assurance_absent` list).
* `src/jev_agent/core/store/migrations/` (new migration): stage
  records table and run-record snapshot columns. Existing runs
  read back with `profile_id` null (legacy path) and no stage rows;
  nothing backfills invented history.
* `src/jev_agent/core/pipeline.py` (change): the `_advance` step
  wrapper that writes stage rows, gate timing, and ship timing;
  decision/attempt stage tagging at the points where decisions and
  attempts are recorded. No handler logic changes in this phase.
* `src/jev_agent/dashboard/` (change, views only): run page shows
  per-stage rows (times, tokens with completeness flags, cache
  behaviour) and the Spend page aggregates stage rows per run.
  Acceptance: a run's stage rows sum to its recorded totals within
  the stated completeness rules, and a `null`-token stage is never
  displayed as zero.

### Phase 2 — Profile storage, versioning, selection

* `src/jev_agent/core/workflow_profiles/` (new package, module
  names follow the option-set-A name if it differs):
  schema, file store (repo and user scope), versioning
  (create/version/archive/delete rules from section 6), and the
  shared validator (section 7.5 catalogue).
* `src/jev_agent/core/pipeline.py` (change): run creation resolves
  a profile version into a snapshot and stores it; runs without a
  profile keep the legacy path untouched.
* `src/jev_agent/cli/` (change): `jev workflow` commands (list,
  show, validate) for parity and debugging; the dashboard remains
  the primary interface and the CLI is not required for
  acceptance.
* Creation paths (change, one per path, each reusing the same
  resolver): dashboard `/runs/new`, CLI run creation, queue add,
  recurring queue items, chat-started runs. A path that cannot
  resolve a profile refuses with the setting named; no path
  silently falls back to the default.
* Acceptance: create, version, archive, and delete a profile;
  snapshots survive edits and deletes; a run created from an
  archived profile version still runs; a deleted profile's
  snapshots still resolve.

### Phase 3 — Graph engine and shared validator

* `src/jev_agent/core/pipeline.py` (change): the snapshot-aware
  step lookup and gate-policy application from section 8.1, behind
  the profile path only; the legacy path is untouched code, not a
  flag that routes through the new lookup.
* `src/jev_agent/core/workflow_profiles/validator.py` (change
  from phase 2's validator): the full catalogue, used by both the
  editor and the executor, with the exact messages from section
  7.5.
* `tests/` (new): conformance suite runs the `default` preset path
  against the legacy path (section 8.2) and must pass before any
  profiled run beyond tests is allowed. Validator unit tests cover
  every catalogue entry.
* Acceptance: the conformance suite passes; a profiled run and a
  legacy run of the same scenario produce identical stage
  sequences, gate parkings, and outcomes except where the profile
  deliberately differs, and every deliberate difference is named
  in the test.

### Phase 4 — Flowchart editor (dashboard)

* `src/jev_agent/dashboard/routes_workflows.py` (new): list,
  editor, detail, suggestion request/result, approve/discard
  routes, following existing route patterns.
* `src/jev_agent/dashboard/templates/workflows/` (new):
  list, editor (canvas plus list mode), detail, suggestion result
  templates.
* `src/jev_agent/dashboard/static/` (new, scoped files only):
  canvas behaviour and list-mode behaviour, both calling the same
  server-side validator; no validation logic lives only in the
  browser.
* Acceptance: create, rearrange (within section 7.1), save, and run
  a custom flow entirely from the browser, in both editor modes;
  every catalogue error displays with the stage named and the fix
  stated; a save failure lists every error.

### Phase 5 — Presets as data

* `src/jev_agent/data/workflow-presets/` (new): `default`,
  `efficiency`, `accuracy` preset files in the profile format,
  versioned with the release, validated by the shared validator.
* `src/jev_agent/dashboard/` (change): preset picker entries with
  their documented behaviour shown before selection.
* `tests/` (new): preset conformance — each preset validates, each
  preset's setting table matches section 9.1, and a preset that
  references a removed model or renamed check fails validation
  loudly at startup checks, not at run time.
* Acceptance: both presets run end to end on the Mac dashboard;
  Efficiency measurably uses fewer rounds and cheaper models than
  the default on the same scenario (measured by stage rows, not
  estimated); Accuracy runs the full flow with CDA grading on and
  production quality level.

### Phase 6 — Suggestions

* `src/jev_agent/core/suggestions/` (new): engine reading only
  section 12.1 inputs, thresholds from section 12.3, reasoning
  records from section 12.2, suggestion records with
  approve/discard lifecycle from section 12.4.
* `src/jev_agent/dashboard/` (change): suggestion request page and
  result page (section 13.3); suggestion history on the profile
  detail page.
* `tests/` (new): threshold tests (below-threshold requests return
  preset-only mode, labelled as such); approval tests (an
  unapproved suggestion changes no run and appears in no picker);
  honesty tests (a reason line without evidence is refused by the
  engine, not displayed).
* Acceptance: a suggestion with reasoning shown can be approved
  and run; an unapproved suggestion never changes a run; a
  below-threshold request says so plainly instead of inventing a
  recommendation.

## 16. Option sets: the human-approval gate

Each set has at most three options. None is ranked, none is marked
preferred, none is pre-picked. Each states its mechanism, its
trade-offs, and the conditions under which it fails. The human picks
one option per set before implementation. A set left unpicked blocks
implementation of the parts that depend on it. Sections 6–15 are
written for the design's proposed answers; where a set picks
otherwise, the document is amended by re-audit before implementation
(max 1 re-audit per stage, per the review rules).

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
  settings" in UI text only (no code rename). Trade-off: all three
  concepts get distinct display names without a code rename; but
  UI text and code names diverge, so a user reading an error that
  names the code concept meets a word the UI never showed them.
  Fails when: an error or log names `learning_profiles` and the
  user cannot map it to "lesson profiles".

### B. Enforcement strictness for the safety floor

* **B1. Mix, as specified in section 10.2.** Mechanism: refusal at
  save time for quality `none` with shipping on,
  `score_skip_human` with shipping on, and omitted required stages;
  label for the remaining reductions, travelling on the run record,
  run page, board row, and PR body. Trade-off: the most protective
  refusals are hard stops while genuinely optional reductions stay
  possible and visible; but two enforcement shapes must both be
  understood, tested, and explained. Fails when: a user meets a
  refusal for one behaviour and a label for a similar one and
  cannot tell why they differ.
* **B2. Refuse everything.** Mechanism: any profile that drops any
  floor behaviour in section 10.1 does not save, and no label path
  exists. Trade-off: simplest to explain and to test — a flow is
  either safe or it does not exist; but it removes the issue's
  "refused or labelled" alternative entirely, and a user who wants
  a deliberately reduced flow for a scratch repo has no supported
  way to say so. Fails when: legitimate reduced-assurance work
  (a throwaway experiment) is forced through the full flow or
  through out-of-band configuration instead.
* **B3. Label everything.** Mechanism: no profile is refused for
  safety reasons; every reduction is labelled and travels as in
  section 10.3. Trade-off: maximum user freedom with maximum
  visibility; but a label does not stop a shipment, so a reduced
  run can still merge code the full flow would have caught, and
  the label's protection depends entirely on someone reading it.
  Fails when: a reduced run ships, the label is missed in review,
  and the defect the floor existed to catch reaches main.

### C. Phasing

* **C1. Phased, as specified in section 15.** Mechanism:
  measurement, then storage/versioning, then engine/validator,
  then editor, then presets as data, then suggestions. Trade-off:
  each phase is shippable and testable on its own, and the
  expensive parts (editor, suggestions) only start once their
  prerequisites exist; but the full feature takes longer to arrive,
  and early phases deliver no user-visible flow choice. Fails when:
  an early phase stalls and the feature is perceived as abandoned
  before the editor exists.
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
  for honest reporting. Trade-off: the smallest build that
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
  their scope, cross-scope use re-validated at run start.
  Trade-off: profiles travel with repos and follow users across
  repos; but two stores, two listings, and re-validation rules to
  maintain. Fails when: a user-scope profile references a model
  the target repo never configured, and the run-start refusal
  arrives after the user believed the flow was ready.
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
  workspace profile, then defaults, applied in one place, with the
  winning source recorded per setting. Trade-off: every conflict
  has a decided winner the run record can show; but the table
  must be learned, and the autonomy-mode exception adds a rule
  that breaks the table's simplicity. Fails when: the table's
  winner surprises a user who expected a different source to win
  and the run record's per-setting source is the only place the
  answer lives.
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
  reached in two steps — first G1, then, after one release in
  which profiled defaults ran cleanly in tests and on the Mac
  dashboard, the legacy path is removed. Trade-off: the migration
  risk is spread across releases with a real-world soak period;
  but the two-path maintenance cost of G1 is paid for a whole
  release, and removal can slip indefinitely. Fails when: the
  removal never happens and the codebase carries both paths
  permanently.

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
  repeated work). Trade-off: one preset that answers both readings
  of "performance" in priority order; but priority order must be
  visible in the preset's table, or a user cannot tell which goal
  won when they conflict. Fails when: quality and speed conflict
  on a real run and the preset's tie-break is buried in a table
  the user never read.

## 17. Acceptance criteria, test matrix, rollback

Acceptance criteria (each must be demonstrated, not asserted):

* A user creates, rearranges, saves, and runs a custom flow
  entirely from the browser, in both editor modes, with no
  terminal use and no `jev.toml` edit.
* Both presets exist, validate, and behave as their section 9.1
  tables say on the same scenario (Efficiency measurably fewer
  rounds and cheaper models; Accuracy full flow with grading on
  and production quality level).
* The default flow is unchanged for existing runs: the
  conformance suite passes, and runs without a profile produce
  identical stage sequences and outcomes to before the feature.
* A suggested flow with its reasoning shown can be approved and
  run; an unapproved suggestion appears in no picker and changes
  no run; a below-threshold request says so plainly.
* A flow that drops a safety behaviour is refused or labelled per
  the approved option set B, and the label appears on the run page
  and in the PR body for shipped reduced runs.
* Verified live on the Mac dashboard, including a reduced-
  assurance run's label rendering and a snapshot surviving a
  profile edit mid-flight (the edit creates a new version; the
  running run is unaffected).

Test matrix:

* Validator unit tests: every catalogue entry in section 7.5,
  including each invalid shape and its exact message.
* Conformance tests: legacy path vs. `default` preset path across
  scenarios covering plan revise loops, fix-task loops, score
  rework loops, `DESIGNING` on and off, ship modes (`merge`,
  `ask`, `off`), `AUTONOMOUS` and `ASK_EVERY_STEP` modes, Halt
  and resume, and cancel mid-run.
* Snapshot tests: edit, archive, and delete a profile while runs
  reference it; resumed runs keep their snapshot.
* Preset tests: each preset validates against live configuration;
  a preset referencing a removed model fails loudly at validation.
* Measurement tests: stage rows for a full run sum to recorded
  totals within completeness rules; `null` tokens are never
  displayed as zero; gate-wait seconds never enter work seconds.
* Suggestion tests: below-threshold requests return preset-only
  mode labelled as such; approval saves exactly one new version;
  discard changes nothing; a reason line without evidence is
  refused by the engine.
* Editor tests (dashboard): save-failure lists every error;
  run-start re-validation catches on-disk edits; list mode and
  canvas mode produce identical profiles for the same inputs.

Rollback: profiles are opt-in per run and the default path is
untouched (option set G notwithstanding, until it is picked), so
rollback is disabling profile selection at run creation (a
configuration flag, default on) and letting in-flight snapshots
finish — snapshots are self-contained and do not need the editor
or the store to keep running. No data migration is rolled back:
stage rows and snapshots are additive and remain readable.

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
  issue's explicit honesty constraint, removes the human from a
  decision that changes what checking code receives, and makes
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
  stage-skip safety. Thresholds in section 12.3 keep the engine
  honest, but they also mean most repos will see preset-only
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
  a SITEMAP; when both designs land, the two SITEMAPs must be
  merged by hand, not by a blind conflict resolution that drops
  one design's entry.
* **No implementation work was started and no Mac verification was
  done.** Both belong to the post-approval phase, after the
  section 16 gate and the reserved re-audit.
