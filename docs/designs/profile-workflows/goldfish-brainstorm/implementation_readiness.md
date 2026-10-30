# Goldfish review: profile-based process flows — Implementation Readiness

Lens: implementation readiness. Mode: BRAINSTORM.
Scope: the anchored hypothesis for Baton issue #368, checked against the
worktree at `branch design/profile-workflows, main c1078a4`. No prior
conversation was used. File references below are to
`src/jev_agent/` in that worktree.

Plain-language summary first: as written, this cannot be built in the
order stated. The editor and the presets look like the work, but they
are not. The work is (1) deciding what a profile is allowed to change,
(2) recording real per-stage measurements, and (3) replacing a hardcoded
loop with something that runs a graph without breaking existing runs.
Until those three are settled, the editor has nothing valid to draw and
the suggestion engine has nothing true to say.

## 0. What the hypothesis gets wrong about the current code

These are not wording nits. Each one changes what must be built.

**The pipeline is not 7 states.** `core/models.py` defines 19
`RunState` values. The `_advance` loop in `core/pipeline.py` maps only
7 work states (INTAKE, PLANNING, VALIDATING, DESIGNING, EXECUTING,
INTEGRATING, SHIPPING) to handler methods. The rest are approval gates
(`AWAITING_APPROVAL`, `AWAITING_PLANNING`, `AWAITING_VALIDATING`,
`AWAITING_DESIGNING`, `AWAITING_INTEGRATING`, `AWAITING_SHIPPING`,
`AWAITING_SHIP`), terminal states (DONE, NEEDS_HUMAN, FAILED,
BUDGET_EXCEEDED, CANCELLED), and the resume machinery around them. A
flowchart that only shows 7 nodes hides the gates, and the gates are
where a human currently intervenes. Any graph model must say, for every
node, whether its gate node is part of the graph, implied, or absent —
and `Pipeline.approve` only accepts runs sitting in an exact gate set
(`STEP_GATES` plus `AWAITING_SHIP`), while `_check_resumable` refuses
to resume a run waiting at a gate. Removing or renaming a gate node
breaks `approve` and `resume` unless that code changes too.

**Steps are not single actions.** The routing decisions live inside
the step methods, not between nodes:

- `_validate` calls `_apply_d3_verdict`: revise loops back to
  PLANNING (bounded by `[budget] max_plan_rounds`), an approved plan
  goes to DESIGNING only when `Plan.needs_design` is true, otherwise
  to the execution gate.
- `_integrate` calls D6, then the ship score gate
  (`quality/scoring.py: ship_gate_outcome`): a "ship" vote below
  `[quality] score_threshold` adds a rework task and loops to
  EXECUTING, bounded by `[quality] score_max_retries`; only after
  retries are spent does it stop for a human.
- `_after_ship_vote` branches on `[ship] after_approval`: merge goes
  to SHIPPING, ask goes to AWAITING_SHIP, off goes straight to DONE.
- `_integrate` also loops back to EXECUTING when any task is not DONE
  (a fix task added just before a crash).

A node-and-edge editor at stage granularity cannot express any of
this. Either the graph language includes conditional edges and
bounded loops with these exact semantics, or the editor must forbid
rearrangements that touch them — and that restriction must be stated
before the editor is designed, because it decides what "rearrange"
means.

**Hard limits already exist, and they are not in the graph.** In
`_d6_action`, a "ship" vote is rewritten to "fix_task" when the
quality gate failed, when the merge is not clean, or when integration
tests failed. The score gate then has the last word after a "ship"
vote. A custom flow that "drops validation" therefore does not
automatically ship bad code today — but only because these limits are
buried in `_integrate` and `_ship_after_score_gate`, not because the
flow is validated. If the new engine replaces `_advance` without
carrying these limits over verbatim, the safety floor silently moves.
That is the most dangerous silent change in this proposal.

**"Profile" already means three different things.** This must be
resolved before any code, or every later discussion, flag, and error
message will be ambiguous:

1. Learning profile: `RunRecord.learning_profile`, the lesson store
   a run reads and writes (`core/learning/profiles.py`).
2. Workspace/server profile: `Pipeline.start(profile=...)`,
   `QueueItem.profile`, copied to `.jev/runs/<id>/profile.json`
   (`servers/profiles.py: snapshot, write_run_profile`), and the
   dashboard's existing `/profiles` route (`dashboard/app.py`).
3. The proposed workflow profile.

The dashboard route, the CLI flag (`jev run --profile`), the run
record field, and the docs cannot all keep the name "profile". Pick a
different name for the new concept (for example "flow") and rename in
the hypothesis before design starts. Severity: Blocker. Dimension:
migration and backward compatibility. Compatibility hazard: reusing
`--profile` or the `/profiles` route changes the meaning of an
existing user-facing control. Migration requirement: the new name,
flag, route, and record field must be fixed in the design doc, with
the old ones left untouched.

## Build order

The order below is the order things must exist in. Each step names
what the next step depends on. Building the editor first, or the
suggestions first, produces a UI over an engine that does not exist
and claims over data that does not exist.

### Step 0 — Decisions that must be written down before any code

Severity: Blocker for each item. Nothing below can be specified
without them.

1. **The safety floor, stated as a list, not a principle.** The
   hypothesis says the quality gate, ship gate, and human approval
   "must not be silently bypassable", then offers "non-removable
   stages, or a visible reduced-assurance label" as alternatives.
   Those are different products, and the current code contradicts a
   blanket "non-removable" reading:
   - `[quality] level = "none"` is a valid level today
     (`quality/levels.py`): task tests only, no gate.
   - `[quality] score_skip_human` skips the ship score gate for
     human-authored code.
   - `AutonomyMode.AUTONOMOUS` already skips the plan-approval gate
     with an explicit opt-in (`core/models.py: AutonomyConfig`).
   - CDA grading is opt-in and off by default
     (`[code_orchestrator] enabled = false`); a missing backend
     records `status="unavailable"` and does not block the run
     (`quality/gate.py: apply_code_grade`). CDA therefore cannot be
     part of a floor today: on a machine without the backend, the
     floor would be empty.
   - The ship gate itself is conditional: no score (no gate ran)
     ships (`quality/scoring.py: ship_gate_outcome`).
   The design must list, stage by stage, what is non-removable, what
   is removable only with an explicit opt-in, and what the
   "reduced-assurance" label says and where it appears (run page?
   ship confirmation? PR description?). A label that only appears in
   the editor does not protect a run started from the queue, the CLI,
   or a chat. Until this list exists, "refused or visibly labelled"
   cannot be tested: there is no acceptance test for a sentence that
   has not been written.
2. **What a profile is allowed to change.** A profile that only
   reorders stages is one product. A profile that also changes
   models per role, quality level, cache settings, fix-round limits,
   and approval mode is a different, much larger one. Today only a
   few knobs are per-run: quality level, ship mode, plan-only,
   autonomy mode, learning profile, and workspace profile
   (`Pipeline.start`, `Store.create_run`). Everything else a preset
   would need — role models (`[roles.*] models`), validator
   different-vendor preference, `[budget] max_plan_rounds`,
   `[integration] max_fix_rounds`, `[cache]` settings, `[quality]
   score_threshold`, `[approval] require_plan_approval` — lives in
   the repo or user config and is read live from `Harness.config`
   during steps. A per-run preset that changes those needs new
   per-run override plumbing through `Pipeline`, `Harness`, the
   gate, and the task runner, or it needs to be honestly scoped
   down to "reorder stages plus the existing per-run knobs". The
   hypothesis must pick one. Severity: Blocker.
3. **The graph-to-handler contract.** For each node type the editor
   can draw, the design must name the exact handler method, its
   required inputs, and its possible exits. In particular:
   - What data must exist before VALIDATING (a plan), before
     EXECUTING (tasks upserted from the plan), before INTEGRATING
     (all tasks DONE or the loop back), before SHIPPING (a result
     branch)? These are enforced today by `Halt(FAILED)` branches
     and by `_integrate`'s not-all-DONE loop, not by the state
     machine.
   - Which edges are conditional (needs_design, ship enabled,
     plan_only, autonomy mode) and what value each condition reads?
   - Which loops are allowed, and what bounds them (plan rounds,
     fix rounds, score rework rounds)? An unbounded cycle in a user
     graph is an infinite run that burns budget until
     `check_budget` stops it; the validator must reject it at save
     time, not discover it at run time.
   - What happens to a run in NEEDS_HUMAN with a `resume_state`
     when its profile changes? `resume` restores that state and
     relearns at the end (`_restore_resume_state`, `_finish`).
     A profile edit must not strand a resumed run in a state its
     new graph no longer contains.

### Step 1 — Measurement (prerequisite for every efficiency claim)

Severity: Blocker for the Efficiency preset and for the suggestion
engine. Major for everything else.

**What exists today is cost and attempt data, not per-stage
measurement.** The pieces, and their gaps:

- `AttemptRecord` has `started_at` / `finished_at`, role, worker,
  model, effort, and a `WorkerResult` with `cost_usd` and a `tokens`
  dict. API, Claude Code, and Codex workers report tokens; Codex
  reports tokens but never cost (`workers/codex.py`: "Codex reports
  tokens but no cost, so `cost_usd` is always None").
- `DecisionRecord` has `latency_ms` and `cost_usd` per decision
  point (D1, D2, D3, D6, ...).
- `RunRecord.spend_usd` and `TaskRecord.spend_usd` accumulate cost,
  but not tokens and not per stage.
- `Run.state_changed` events carry from/to states with timestamps
  (`core/events.py`), so stage wall time is derivable — but only
  when `[events] enabled` is on, and that wall time includes human
  waiting time in `AWAITING_*` gates. Work time and waiting time
  must be recorded separately, or any "efficiency" comparison will
  mostly measure how fast a human clicked approve.
- Cache data exists (`CacheStats`, saved dollars, `log_shadow`),
  but a cache hit serves at $0 with no stated token accounting:
  the design must say whether a hit counts as zero tokens, as the
  original call's tokens, or as something else, or every
  token-saving claim will be unreproducible.
- No table records "stage X of run Y took T seconds and N tokens".
  Attempts do not carry a stage: run-level attempts have
  `task_id = None` (planner, validator, designer), task attempts
  belong to EXECUTING, and integration work (gate runs, D6, score
  gate) is spread across quality reports and decisions. Deriving
  per-stage numbers means joining attempts, decisions, and quality
  reports by run and mapping decision points and roles to stages —
  a mapping the design must write down, not leave to the
  implementer.

**Consequence: efficiency claims are not possible before this
lands.** The Efficiency preset promises "cheaper models where
history says they are safe" and "aggressive cache reuse". No
per-model safety history exists today: there is no stored table of
model → outcome quality. The closest signals are the CDA ledger
(per agent / tier / skill, see Step 5) and lesson scores, neither of
which is a per-model safety record, and the cost-estimate config
(`[dispatcher.cost] tokens_per_task`, `worker_price_per_million`)
is explicitly labelled a guess in the config template. Until Step 1
records real per-stage tokens, time, and outcome, the Efficiency
preset can only be a static bundle of cheaper settings, not an
evidence-based claim — and it must be described that way, or it
will mislead the user about what "history says".

What Step 1 must deliver, before presets or suggestions:

- A per-stage measurement record (new store table or event type —
  decided in design, not by the implementer): run id, stage, work
  seconds excluding gate waits, tokens in/out/total where the
  worker reported them, cost where known, cache hits, outcome
  (success / failed / fix task added / human stop).
- Rules for missing data: workers that report no cost, cache hits,
  rules-dispatcher fallback decisions, and CDA "unavailable"
  grades must be counted as "unknown", never as zero. A zero
  invents a saving that did not happen.
- A backfill decision: old runs have attempts and events but no
  stage records. Either backfill is defined (and its limits stated)
  or suggestions explicitly ignore pre-measurement runs.

### Step 2 — Profile storage, versioning, and selection at run creation

Severity: Blocker. Dimension: migration and backward compatibility.

**The store question is undecided, and each option has a different
migration.** Candidates, with what each one costs:

- A new table in the repo's `state.db`, added by a numbered
  migration (precedent: autonomy columns in migration 14, design in
  16, design-system choice in 19; `core/migrations.py` applies them
  on open and `SCHEMA_VERSION = len(MIGRATIONS)`). This keeps
  profiles with runs, but an older `jev` binary cannot open a
  migrated database (`SchemaVersionError` when the stored version
  exceeds the binary's). Rollback then needs a database backup
  taken before migration, not just "revert the code".
- A file under `.jev/` next to `state.db`, like the CDA ledger
  (`<repo>/.jev/cda-ratings.jsonl`, deliberately kept out of
  `state.db` so a database problem never takes the grades with it).
  No migration, but no transactions, no locking (the ledger notes
  it has none), and concurrent dashboard/CLI edits can interleave.
- Repo config (`jev.toml [profiles.*]` — note this section name is
  already taken by workspace profiles) or user config
  (`~/.config/jev/config.toml`). Config is version-controlled and
  portable, but repo config cannot change guarded keys (the
  `[trust]` section belongs in user config only, "a repository
  cannot trust itself"), and a profile stored in a repo file is
  shared with everyone who clones the repo whether the user meant
  that or not.

"Per repo or per user" in the hypothesis is two scopes, not one.
The design must pick the store per scope, or pick one scope for
version 1. The learning system already distinguishes repo-scope and
shared-scope databases (`core/learning`); the workflow design
should say explicitly whether it follows that split or invents a
new one, and what happens when a user-scope profile and a
repo-scope profile share a name.

**Schema, minimum.** Whatever the store, a profile record needs:
name (validated — the learning profile pattern, letters/digits/`-`/
`_`, max 40 chars, is a usable precedent, but the workflow name
rule must be stated, including what happens on rename); an
immutable version identifier; the graph (nodes, edges, conditions);
the per-run knob overrides it sets (Step 0, item 2 — empty if
profiles only reorder stages); the safety-floor status (full
assurance vs reduced-assurance label text); created/updated time;
and an owner/scope marker. A profile that can be edited in place
without a version bump is unbuildable for the reason below.

**In-flight runs: pin at creation, never follow edits.** The
precedent already exists and should be copied: the workspace
profile is snapshotted to `.jev/runs/<id>/profile.json` at run
creation so a resumed run reaches the same servers, and autonomy
mode, quality level, ship mode, and plan-only are recorded on the
run record at creation and read from the run thereafter. The
workflow design must do the same: a run records its profile name
**and** version at creation, and the pipeline runs the pinned
snapshot. Editing a profile creates a new version; it never rewrites
the version a running run pinned. Deleting a profile that running
runs reference must be refused, or must leave those runs on their
pinned snapshot with the profile marked deleted — the design must
pick one. What it cannot do is let a mid-flight edit change a
running run's next step: today many knobs are read live from
`Harness.config`, so a "profile" that is just a live config
reference would do exactly that. The snapshot must be what the
pipeline reads, which means the pipeline's config reads for
profile-controlled knobs must move to the snapshot — a refactor
whose size depends entirely on Step 0, item 2.

**Selection at run creation must cover every creation path, or it
is bypassable.** Runs are created by `Pipeline.start`, by queue
items (which carry their own `profile`, `quality`, `ship_mode`,
`learning_profile` fields — `QueueItem`, `RecurringTask`), and by
chat drivers. A dashboard-only profile picker leaves CLI, queue,
and chat runs on the default with no record of why, and a
"reduced-assurance" flow chosen in the dashboard could be bypassed
by starting the same work from the queue. The design must name the
selection point for each path, the default when none is chosen
(today's pipeline, unchanged), and how a run record shows which
profile and version it ran — including for runs created before
profiles existed (null = default flow, never a rewritten value).

**Migration requirement.** Existing runs, stored runs, and the
default flow stay untouched by construction, not by promise:

- New run-record columns (or the equivalent) are nullable; a null
  profile means today's flow, and the pipeline's default branch
  must be the existing `_advance` path, not a new interpreter
  emulating it. If the new engine cannot run the old flow
  byte-for-byte (same states, same gates, same D6 limits, same
  resume behaviour), the default must stay on the old code path
  and profiles must be opt-in.
- No new `RunState` values in version 1. New states would break
  stored runs, event replay (`jev replay`, `jev bisect`), the
  dashboard's state display, and any older binary reading the
  database. Profiles must map to the existing states only.
- A migration that adds columns must be reversible in practice:
  state.db backup before first run under the new version, and a
  documented downgrade path (or an honest statement that downgrade
  requires restoring the backup).

### Step 3 — The graph engine (before the editor)

Severity: Blocker. This is the largest build in the proposal, and
the hypothesis does not name it.

The `_advance` loop must become, or gain, a graph interpreter: given
a pinned profile snapshot, pick the next handler, honour gates,
conditional branches, and bounded loops, and stop exactly where the
old loop stopped (waiting gates, NEEDS_HUMAN, FAILED, DONE). The
interpreter must preserve, verbatim: the lease
(`core/lease.py`: two drivers never drive one run at once), the
`Halt` rules (resumable vs not, `resume_state`, cancel-wins via
`KEEP_CANCELLED`), the `_finish` learning step (runs once, only
for states in `LEARN_STATES` with a learning profile and no outcome
yet), and worktree cleanup on DONE. Each of those is a regression
surface that a new loop can break without any test failing on the
happy path, because the happy path never exercises them.

Validation rules the graph must satisfy before it is executable
(checked at save time **and** at run start, because a stored graph
can be edited, corrupted, or loaded by a newer/older version):

1. Exactly one entry node, and it must be INTAKE (or the design
   must say what a run that skips intake means for D1 and for the
   idea record — today `_intake` is where an unclear idea halts).
2. Every node is reachable from entry, and every node can reach a
   terminal (DONE, or a defined human-stop). No dead ends, no
   orphans. A node that cannot reach DONE is a run that can never
   finish.
3. Every node maps to a handler that exists, with its required
   inputs satisfied by an earlier node (plan before VALIDATING,
   tasks before EXECUTING, result branch before SHIPPING, design
   before EXECUTING when the plan needs one).
4. Cycles only where the code bounds them: plan revise
   (max_plan_rounds), integration fix and score rework
   (max_fix_rounds, score_max_retries). Any other cycle is
   rejected. A cycle's bound must be named in the graph or in the
   profile's knobs, not assumed.
5. Gate pairing: a step that has an ASK_EVERY_STEP gate must name
   its gate state, and a graph that drops a gate must say what
   `approve` does for that step (nothing to approve? auto-pass?
   refused?). A graph that drops AWAITING_SHIP must say what
   `jev ship approve|modify|stop` does.
6. Conditional nodes are explicit: DESIGNING runs only when the
   plan needs it; SHIPPING runs only when shipping is enabled and
   the ship vote says so. A graph that makes DESIGNING or SHIPPING
   unconditional changes behaviour for every run, not just for the
   runs the user imagined.
7. Safety-floor check (Step 0, item 1): a graph that drops a
   non-removable stage is refused with a plain-language reason
   naming the stage and why it cannot be dropped; a graph that
   drops a removable-with-label stage is saved only with its label
   attached, and the label travels with the run (Step 2).
8. The graph version the run pinned must still exist and still
   validate at resume time. If it does not, resume is refused with
   a plain-language error, not a crash and not a silent fall back
   to the default flow — a silent fallback would run work under a
   flow the user did not choose.

An invalid graph gets a plain-language refusal naming the rule
broken and the node involved. "Invalid graph" with no rule, no
node, and no fix is not a refusal the user can act on, and it
fails the browser-first bar the same way a crash does.

### Step 4 — The flowchart editor (after the engine and validator exist)

Severity: Major. The editor is a user interface over Step 3's
validator, not a separate design. Building it first inverts the
dependency: the UI will invent graph semantics the engine then has
to honour.

What must be decided before the editor is drawn:

- The editor's node palette is exactly the node types Step 3
  supports — no more. If the engine supports only the existing
  stages plus their gates, the palette is those stages plus their
  gates. A palette with generic nodes ("custom step") implies a
  plugin system that does not exist.
- Every editor action maps to a validator rule: dragging an edge
  that creates an unbounded cycle, disconnecting a node from DONE,
  or deleting a gate node must show the Step 3 rule it breaks, in
  plain language, at edit time — not only at save time.
- The editor needs the dashboard's existing patterns, and one new
  one. The dashboard is Starlette routes grouped by area
  (`dashboard/app.py` route table, `routes_*.py` mixins) with
  server-rendered templates and small static JavaScript files. A
  flowchart editor is the first drag-and-drop graph surface in that
  stack: it needs a new frontend component (node layout, edge
  drawing, keyboard-accessible editing — a mouse-only editor fails
  users who cannot drag), plus new routes. The existing `/profiles`
  route is taken (Step 0), so the workflow routes need a new prefix
  decided in design. Save conflicts (two editors, or editor plus
  CLI edit, on one profile) need a rule: last-write-wins silently
  loses work; version-conflict refusal is the honest default.
- "Zero terminal use" includes naming, versioning, viewing the
  reduced-assurance label, and approving a suggested flow (Step 5).
  Any of those that only works from the CLI fails the acceptance
  criterion, even if the editor itself works.

### Step 5 — The two presets (after Steps 0–3)

Severity: Major. Presets are profiles with fixed graphs and knob
bundles, so they cannot be built before the profile format, the
engine, and the safety floor exist. Their definitions must be
written as data, not prose:

**Efficiency (token saving).** As a bundle, the levers that exist
today are: cheaper role models (`[roles.*] models`, `[models]
prefer_free`, `planner_policy`), cache on with longer TTLs
(`[cache]`), fewer rework rounds (`max_plan_rounds`,
`max_fix_rounds`, `score_max_retries`), lower quality level, and
review/CDA off. Each lever needs a value, and each value needs the
Step 0 decision about whether it is a per-run override (new
plumbing) or a config change (repo-wide, not per-run). "Cheaper
models where history says they are safe" is not a lever: it is a
suggestion-engine claim (Step 6) smuggled into a preset. In version
1 the Efficiency preset must either name its cheaper models
explicitly or be described as a static cheap bundle with no safety
claim. "Aggressive cache reuse" must respect the cache's existing
safety rules: coder and learner are hard-denied, only `success`
results are stored, D6 decisions are excluded unless
`[cache] d6_decisions = true`, and live semantic serving stays off
until shadow data proves it safe (`core/cache.py`). A preset that
turns semantic serving on by default contradicts the cache's own
gate and must not do so.

**Accuracy (performance).** The levers that exist: full stage
sequence, validator with `prefer_different_vendor_than = "planner"`,
quality level at production or above, CDA grading on with the 0.7
confidence threshold (`[code_orchestrator]`), plan approval required,
ship `after_approval = "ask"`. Note what this preset cannot promise
without Step 1 and Step 6: that it is "more accurate" in any
measured sense. It can promise that it runs more checks and more
gates; whether those gates catch more real defects per run is a
measurement claim that needs the Step 1 data and a comparison the
hypothesis does not define (same idea run twice? different ideas?).
The preset description must not claim a measured accuracy gain it
has not measured.

**Custom profile as "preset edited or built from scratch"** needs
the Step 2 versioning rule: editing a built-in preset must create
a user profile (built-ins are immutable, or editing one silently
changes every run that used it). The design must say whether
built-ins live in code, in config, or in the profile store, and
how a built-in changes across `jev` upgrades without rewriting
user edits.

### Step 6 — The suggestion engine (last, not first)

Severity: Blocker for any suggestion that changes a run. Major for
suggestions shown as information only.

**Exact inputs.** The engine may only use data that exists and is
attributed correctly:

- Per-stage outcomes and costs: Step 1's stage records. Without
  Step 1, this input does not exist; attempts and spend totals are
  not a substitute, for the reasons in Step 1.
- What struggled and what worked: task outcomes, fix-round counts,
  integration failures, quality gate blocked reasons and score
  breakdowns (`quality_reports`, `GateResult`), plan rounds,
  NEEDS_HUMAN reasons, and lesson scores/retirements
  (`core/learning/lessons.py`: `uses`, `score`, `RETIRE_AT_SCORE`,
  scored once per run/task via `scored_tasks`).
- CDA ratings: the ledger at `<repo>/.jev/cda-ratings.jsonl`
  (`code_orchestrator/ledger.py`). Aggregates count graded diff
  grades only for quality/accept rates; `unavailable` and `error`
  records count in `n` but never as zero quality, and a tier needs
  at least 3 graded diffs before it can be named best
  (`MIN_GRADES_FOR_BEST_TIER`). The ledger is best-effort,
  append-only, and has no locking; a suggestion engine that treats
  a missing or partial ledger as "no signal" vs "bad signal"
  differently must say which. Grades are also slow and rare (the
  ledger notes 1–3 minutes each on CPU), so per-model CDA data will
  be thin for a long time.
- Cache behaviour: exact-cache hits and saved dollars
  (`CacheStats`), plus semantic shadow data (`log_shadow`,
  `calibrate_thresholds` over thresholds 0.90/0.95/0.98/0.99 with a
  0.99 minimum agreement bar). Shadow data measures agreement; it
  is not serving, and a suggestion must never describe shadow
  agreement as "semantic cache saved X", because nothing was
  served. Live semantic serving stays off until shadow data proves
  it safe — the suggestion engine must not recommend turning it on
  as a routine efficiency win, and must say plainly when its cache
  claim rests on exact hits only.
- What the engine cannot use without a new field: the user's goal.
  Runs today record no goal (efficiency vs accuracy). A suggestion
  "conditioned on their goal" needs the goal recorded at run
  creation (or at suggestion time, explicitly chosen by the user),
  or it is conditioned on a guess. Per-repo history is also scoped:
  the CDA ledger is repo-level, learning has repo and shared
  scopes, and cache tables live in the repo's own state.db. Pooling
  history across repos to reach a threshold faster would mix
  different codebases' evidence; the design must forbid it or
  justify it, not do it silently.

**Minimum data thresholds — to be decided and written into the
design, not left to the implementer.** Existing floors in the code
are a warning, not a precedent to copy: `[dispatcher.cost]
min_history_runs = 3` and the CDA best-tier floor of 3 graded diffs
are both small numbers chosen for narrow purposes (a cost estimate
starting point; avoiding crowning a tier on one lucky grade). Three
runs is not evidence for rearranging a pipeline. The design must
set, per suggestion type, all of: minimum ended runs in this repo;
minimum observations per stage the suggestion touches; minimum
observations per model or tier it recommends; minimum time window
(so a suggestion does not rest on runs from before a major repo
change); and a rule for what counts as an observation (an ended
run? a completed stage? a graded diff?). Below every threshold,
the engine shows presets plus an explanation of what data is
missing — exactly as the hypothesis requires — and never a
suggestion dressed up as data. Suggested thresholds must be
labelled as proposals to be validated against real history, not
presented as measured constants. Any threshold the design states
without a validation plan is a guess the implementer will have to
defend later.

**Explanation is part of the output, not an accessory.** Every
suggestion must show: the suggested graph change; the data behind
it (sample sizes, date range, per-stage numbers from Step 1); what
it expects to improve and by how much, with uncertainty stated
("based on N runs" is not a promise); what it does not know (thin
history, missing cost data, unavailable grades); and the safety
effect (does it touch a floor stage? does it add a
reduced-assurance label?). A suggestion that only shows the new
graph fails the "reasoning shown" acceptance criterion.

**Approval is a separate stored action.** A suggestion is not a
profile until the user approves it, and approval must be recorded
as its own event (who, when, which suggestion version, which
profile version it created). An unapproved suggestion must never
change a run, a profile, or a default — which means suggestions
live in their own store, not as draft profiles that a creation
path could accidentally pick up. The design must also say whether
a suggestion expires (history moves on; a suggestion based on
January runs should not be approvable unchanged in June) and what
happens when the user edits a suggested flow before approving it
(it becomes a custom profile, and the suggestion's reasoning no
longer describes it exactly — the UI must say so).

## Migration and rollback (whole feature)

Severity: Blocker. Dimension: migration and backward compatibility.

- **Existing runs:** untouched. Null profile = today's flow on the
  existing code path (Step 2). No backfill rewrites history, and no
  migration changes a stored run's state, plan, or outcome.
- **Stored runs and the event log:** events reference states by
  name; no new states in version 1 (Step 2), so replay and bisect
  keep working. If a later version adds states, that version needs
  its own migration and replay-compatibility note — not this one.
- **Default flow:** unchanged by test, not by assertion. The
  acceptance test must run the default flow end to end (including
  revise loops, fix rounds, score-gate rework, ship ask/merge, and
  NEEDS_HUMAN resume) and show identical states and gates to the
  pre-feature code. A test that only runs the happy path does not
  prove the default is unchanged.
- **Rollback:** code revert alone does not roll back a database
  migration (older binary refuses a newer database). The rollout
  plan must include a state.db backup before first migration, and
  the profile store (if file-based) must tolerate a missing or
  corrupt file the way the CDA ledger does (skip bad lines, never
  break a run) — or state explicitly that a corrupt profile store
  blocks profile runs but never default runs.
- **Live verification on the Mac dashboard** (the acceptance
  criterion) needs the Mac's existing dashboard patterns, not a new
  harness: create, rearrange, save, and run a custom flow entirely
  from the browser, with the reduced-assurance label visible on
  the run page, and with an unapproved suggestion demonstrably
  leaving the next run unchanged. A verification that only checks
  the editor renders does not verify any of this.

## The single biggest build risk

**Replacing the hardcoded `_advance` loop with a graph interpreter
without changing what existing runs do.** Everything else in the
proposal — the editor UI, the preset bundles, the suggestion text —
is bounded work once the engine exists. The engine is not: it must
reproduce conditional branches, bounded loops, approval gates,
D6 hard limits, the score gate, lease and cancel semantics,
Halt/resume behaviour, and end-of-run learning, while also
validating arbitrary user graphs and refusing bad ones in plain
language. If that engine is wrong, runs ship code under a flow the
user did not understand, or existing runs change behaviour on
upgrade. That is why the engine (Step 3) comes before the editor
(Step 4) and why the default flow must stay on the existing code
path until the engine proves, by test, that it reproduces it
exactly. Any plan that builds the editor first, or that treats the
graph as "just nodes and edges over the 7 stages", underestimates
this risk and will discover it after the UI is done, when it is
most expensive to fix.

## Findings list (persona format)

1. Severity: Blocker. Dimension: migration and backward
   compatibility. Compatibility hazard: "profile" already names
   learning profiles, workspace profiles, and the `/profiles`
   dashboard route; reusing it breaks existing flags, routes, and
   record fields. Migration requirement: rename the new concept in
   the design doc before any code; leave existing names untouched.
2. Severity: Blocker. Dimension: migration and backward
   compatibility. Compatibility hazard: no safety-floor list exists,
   and current code already allows quality "none", score skipping,
   autonomous plan-gate skipping, and opt-in/off-by-default CDA
   grading — so "non-removable" is currently undefined and partly
   false. Migration requirement: a stage-by-stage floor list with
   label text and placement, decided before design.
3. Severity: Blocker. Dimension: migration and backward
   compatibility. Compatibility hazard: per-stage tokens/time do
   not exist as a record; attempts, decisions, spend, and events
   cannot be joined into stage measurements without a defined
   mapping, and Codex reports no cost while cache hits have no
   token rule. Migration requirement: a stage-measurement record
   with work-vs-wait separation and unknown-vs-zero rules, built
   before presets or suggestions.
4. Severity: Blocker. Dimension: migration and backward
   compatibility. Compatibility hazard: profile edits read live
   from config would change running runs mid-flight, and no
   versioning or pinning rule exists. Migration requirement:
   immutable versions, run pins name plus version at creation,
   snapshot-based execution, and a deletion rule for profiles with
   running runs.
5. Severity: Blocker. Dimension: migration and backward
   compatibility. Compatibility hazard: a graph engine that
   replaces `_advance` can silently drop D6 hard limits, the score
   gate, lease/cancel rules, and resume behaviour. Migration
   requirement: the default flow stays on the existing code path
   until the engine reproduces it by test, including loops, gates,
   and resume; no new `RunState` values in version 1.
6. Severity: Blocker. Dimension: migration and backward
   compatibility. Compatibility hazard: an older binary cannot open
   a database migrated by a newer one, so code revert is not
   rollback. Migration requirement: state.db backup before first
   migration and a documented downgrade path.
7. Severity: Major. Dimension: migration and backward
   compatibility. Compatibility hazard: runs are also created from
   the queue, recurring tasks, CLI, and chat; a dashboard-only
   picker leaves those paths on an unrecorded default and bypasses
   the safety label. Migration requirement: a selection point and
   a recorded profile/version for every creation path.
8. Severity: Major. Dimension: migration and backward
   compatibility. Compatibility hazard: the suggestion engine has
   no goal field to condition on (runs record no goal), no
   per-model safety table, and only thin, slow CDA data plus
   shadow-only semantic cache data. Migration requirement:
   thresholds per suggestion type, repo-scoped history only, and
   explanations carrying sample sizes and unknowns; suggestions
   stored separately from profiles until approved.
