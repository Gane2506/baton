# Goldfish brainstorm review — Profile-based process flows (issue #368)
## Lens: codebase details

Scope: the hypothesis as stated in the task, checked against the worktree at
`/home/hatch/workspace/baton-profile-workflows` (branch `design/profile-workflows`,
main `c1078a4`). Files read: `src/jev_agent/core/models.py`,
`src/jev_agent/core/pipeline.py`, `src/jev_agent/core/store/records.py`,
`src/jev_agent/core/learning/profiles.py`, `lessons.py`, `graph.py`,
`src/jev_agent/core/cache.py`, `src/jev_agent/code_orchestrator/ledger.py`,
`src/jev_agent/quality/gate.py`, `checks.py`, `levels.py`, `scoring.py`,
`src/jev_agent/config/defaults.py`, `src/jev_agent/dispatch/jev.py`, plus the
store, harness, learner, and event code those files point to. No code was changed.

Plain-language summary first: the hypothesis talks as if the pipeline were a
configurable flowchart waiting for an editor. The code shows something else: a
hardcoded state machine whose next step is decided inside each step's handler,
plus several existing knobs that already change the flow. A profile cannot be
bolted on as data alone, per-stage measurement does not exist in usable form,
and the safety floor the hypothesis promises does not exist today. Those three
gaps have to be settled before an editor or a suggestion engine is designed,
not after.

## Strongest code-grounded objection

**Blocker — the flowchart is not a faithful picture of the pipeline, and the
safety floor contradicts existing settings.**

`Pipeline._advance` (`core/pipeline.py`) is a hardcoded dictionary from seven
work states to handler methods: INTAKE, PLANNING, VALIDATING, DESIGNING,
EXECUTING, INTEGRATING, SHIPPING. Anything not in that dictionary — approval
gates, DONE, NEEDS_HUMAN, FAILED — ends the loop. But the handlers themselves
decide what happens next, and they do it in ways a node-and-edge drawing
cannot express without rewriting them:

- `_validate` does not just "go to the next node". It runs the validator,
  asks D3 for a verdict, and `_apply_d3_verdict` then either escalates,
  loops back to PLANNING, goes to DESIGNING only if `Plan.needs_design` is
  true, or goes to the execution gate.
- `_integrate` does not just "go to the next node". It loops back to
  EXECUTING when a fix task is added, stops for a human when fix rounds are
  spent, and only ships after D6 plus the score gate in
  `_ship_after_score_gate`.
- `_plan` stops the run after `budget.max_plan_rounds` (default 3) rounds.
- `_execute` always ends at INTEGRATING; `_design` always ends at the
  execution gate.

So a rearranged graph drawn in the browser would either be decoration over
these hardcoded transitions (the user moves a node, the run does something
else), or the design must restructure `_advance` and every handler so each
handler returns its next step and the profile supplies the transition table.
That is the core implementation job, and the hypothesis does not name it.

The safety floor has the same problem in the opposite direction. Today the
stages the hypothesis calls non-removable are already removable by existing
settings: quality level `"none"` means "the task's own test command only (no
gate)" (`quality/levels.py`); when no gate runs, `ship_gate_outcome` returns
"ship" for a missing score (`quality/scoring.py`); AUTONOMOUS mode skips the
plan-approval gate (`core/pipeline.py`, `execution_gate_target`); and
`[quality] score_skip_human = true` skips the ship score gate. A profile
that "drops validation" is therefore not a new danger to guard against — it
is a small step from knobs that already exist. The design must decide, in
code, whether profiles override those knobs, are overridden by them, or
replace them. "Either non-removable stages or a visible label" is not a
decision; it is two different products, and only the first one changes the
existing bypasses.

## (a) Can a profile be represented as data the existing `_advance` loop can execute?

No, not without restructuring. Exactly what changes:

1. **`core/pipeline.py::_advance`** — the `steps` dictionary is the whole
   transition table today, and it has no room for a profile. A data-driven
   version needs a per-run transition table (profile id + version, resolved
   at run start) and a loop that consults it instead of the hardcoded dict.
   The loop also needs a defined behaviour for states with no handler
   (gates, DONE, stopped states), which today just break the loop.
2. **Every handler that picks its own next state** — `_plan`, `_validate`
   (via `_apply_d3_verdict` and `_d3_verdict`), `_design`, `_execute`,
   `_integrate` (via `_d6_action`, `_ship_after_score_gate`), `_after_ship_vote`,
   and `_ship`. Each currently calls `_move`, `_move_to_step`, or
   `_move_to_execution` directly. Under a profile, each must either return a
   symbolic outcome ("approved", "revise", "fix_task", "ship") that the
   profile maps to a next state, or the profile language must be able to
   express their internal branches (plan-round limits, fix-round limits,
   `needs_design`, ship mode). The second option is a small programming
   language, not a flowchart.
3. **The gate machinery, which lives outside `_advance`** — `STEP_GATES`
   (PLANNING→AWAITING_PLANNING, VALIDATING→AWAITING_VALIDATING,
   DESIGNING→AWAITING_DESIGNING, EXECUTING→AWAITING_APPROVAL,
   INTEGRATING→AWAITING_INTEGRATING, SHIPPING→AWAITING_SHIPPING),
   `step_gate_target`, `execution_gate_target`, `Pipeline.approve` (which
   inverts `STEP_GATES` and adds AWAITING_SHIP→SHIPPING), and
   `_check_resumable` (which refuses to resume runs sitting at a gate).
   A profile that inserts, removes, or reorders a gate must update all of
   these, or `jev approve` will not know what state to move to.
4. **`core/models.py::RunState`** — a profile can only permute or subset
   existing states. There is no generic "custom stage" concept: adding one
   means a new enum value, a new handler, a new gate entry if it needs
   approval, store migration, and dashboard handling. The editor must refuse
   invented stages, and the hypothesis should say so plainly instead of
   implying arbitrary rearrangement.
5. **`core/store/records.py::RunRecord`** — there is no workflow-profile
   field. A run records `learning_profile`, `quality_level`, `plan_only`,
   `ship_mode`, `autonomy_mode`, `autonomous_opt_in`, `medium_folder`, and
   `design_system`, but nothing saying "this run followed workflow profile
   X version Y". Without that field (plus a store migration, following the
   precedent of migration 14 for autonomy columns), a resumed run cannot
   know which profile it is on, and changing a profile mid-run is undefined.
   Precedent worth copying: `servers/profiles.py::write_run_profile` snapshots
   the run's workspace profile to `.jev/runs/<id>/profile.json` at start so a
   resumed run reaches the same servers. A workflow profile needs the same
   snapshot-at-start rule, or editing a profile will silently change live runs.

Related trap — the word "profile" already means three different things, and
the hypothesis adds a fourth:

- `Pipeline.start(profile=...)` and `QueueItem.profile` are the **workspace
  profile** (MCP servers, access overrides, learning, quality — see
  `config/defaults.py` `[profiles.<name>]` and `servers/profiles.py`).
- `RunRecord.learning_profile` is the **learning profile** (a lesson store;
  `core/learning/profiles.py` — just a name, an enabled flag, and a lesson
  count; no version, no flow, no settings).
- The proposal is a **workflow profile** (stage order + goal preset).

If the dashboard, CLI flags, and stored fields all say "profile", users and
code will confuse them. The new concept needs a distinct name in code, flags,
and UI (for example `workflow` / `flow`), and the design must state how a
workflow profile interacts with a workspace profile that already sets
`quality` and `learning` for the same run.

Validation the editor needs, none of which exists today:

- Unknown or invented nodes, missing start/end, unreachable states
  (for example SHIPPING when `[ship] enabled` is false, DESIGNING when no
  plan ever sets `needs_design`).
- Cycles: the pipeline already has legitimate bounded loops (plan revise
  up to `max_plan_rounds`, fix tasks up to `max_fix_rounds`). A naive
  "reject cycles" rule would reject the current default flow. The validator
  must distinguish bounded loops from unbounded ones, and say so.
- Gate orphans: a step with no approval path, or an approval gate with no
  step behind it, will strand a run in a state `_check_resumable` refuses to
  resume and `approve` cannot map.
- The only existing graph validation in the codebase is narrower: `Plan`
  rejects task-id cycles (`core/models.py`), and the lesson graph validates
  edge types. Neither validates a pipeline graph.

## (b) Is per-stage token/time measurement available today?

Not as a usable per-stage measurement. What exists is scattered, partial,
and in two cases buried in JSON blobs rather than queryable columns. The
hypothesis's "real measurement, not estimated" constraint therefore requires
building the measurement layer first, before any suggestion can claim to be
based on it.

What actually exists:

- **Run totals only on the run record.** `RunRecord` carries `spend_usd`
  (a running total), `created_at`, and `updated_at`. No per-stage spend,
  tokens, or time columns.
- **Per-attempt cost, time, and (sometimes) tokens.** `AttemptRecord` has
  `started_at`, `finished_at`, worker, model, effort, and a `result`
  (`WorkerResult`) whose `cost_usd` is stored as a column and whose
  `tokens` dict survives inside `result_json` (the full
  `model_dump_json()` is stored by `store/attempts.py::finish_attempt`).
  The API, Claude Code, and Codex workers all populate `tokens` from provider
  usage when the provider reports it — but "when the provider reports it" is
  the catch: a missing usage block gives `tokens = None`, and a cache hit
  costs $0 with no token accounting at all. Tokens are also not a column,
  so aggregating "tokens per stage" today means parsing JSON blobs.
- **Per-decision cost and latency, no token column.** `DecisionRecord`
  stores `cost_usd` and `latency_ms`; Jev dispatcher usage (input tokens,
  sometimes a cost) lives inside `Decision.details["usage"]`. The rules
  dispatcher records $0 and 0 ms. Decision cost is a small part of a stage's
  cost, and it is not tagged with a stage.
- **Stage timing can be approximated from the event log, not read from the
  run.** `core/events.py` has `RUN_STATE_CHANGED` with `from_state` /
  `to_state`, plus `ATTEMPT_STARTED` / `ATTEMPT_FINISHED` (the latter
  carries `duration_s` and `cost_usd`) and `SPEND_ADDED`. So entry/exit
  times per state are reconstructable — but waiting time inside a gate
  (a human taking three days to approve) is indistinguishable from work time
  unless the design separates "time in state" from "time working", and
  EXECUTING collapses all tasks, retries, and rework rounds into one span.
  Plan rounds and fix rounds are countable (`plan_round`, fix-task titles),
  but their individual costs are not separated.
- **No timing at all for the quality gate or the ship step as stages.**
  `GateResult` records checks, findings, review, score, and blocked reasons;
  it has no duration field. Integration test time and gate tool time are
  invisible to any per-stage table today.

Recording points that must be built (this is the minimum, not a wish list):

1. `Pipeline._advance` (or the `set_run_state` path it funnels through):
   record stage entry/exit timestamps and the run's profile id/version, so
   a stage's wall time is a first-class row, not derived from events.
2. `Harness.decide` and `Harness.run_worker` / `run_worker_with_fallback`:
   tag every decision and attempt with the pipeline stage it served, and
   extract tokens into a real column (or a dedicated table) instead of
   leaving them in `result_json` / `details` JSON. Cache hits need an
   explicit "served from cache, $0, tokens not spent" record, or efficiency
   claims will undercount nothing and overcount savings at the same time.
3. `quality/gate.py::run_gate` and `core/pipeline.py::_ship`: start/end
   timestamps and cost attribution for the gate and ship stages, which
   currently have none.
4. The budget path (`Harness.check_budget`, `store.add_spend`) already
   enforces per-run and per-task caps; per-stage measurement should reuse
   that path rather than invent a second accounting system, or the two will
   disagree.

Until those points exist, a suggestion engine can only use run totals,
attempt aggregates, and gate reports — useful for "this repo's runs are
expensive", not for "dropping VALIDATING saves X tokens on this kind of
work". The hypothesis must not let the suggestion UI present the first as
the second.

## (c) What would the suggestion engine actually have to read — and what is absent or too thin?

The engine would need to join five stores that were never designed to be
joined, and two of them are empty or off by default in a typical repo.

**Usable today, with limits:**

- **Run and task records** (`store/records.py`, `store/runs.py`,
  `store/tasks.py`): final state, stop reason, `plan_round`, spend, quality
  level, `plan_only`, `ship_mode`, autonomy mode; per task, state, attempt
  count, spend, worker/model/effort. This supports "runs that needed many
  plan rounds" or "tasks that took many attempts" — coarse, run-level and
  task-level signals, not stage-level ones.
- **Attempts and decisions** (see (b)): which worker/model/effort was used,
  which lessons were injected (`lesson_ids`), what each decision point
  answered, whether the rules dispatcher fell back (`fallback_reason`).
  This is the closest thing to "what worked", and it is per attempt, not
  per stage.
- **Quality reports** (`QualityReportRecord`, `GateResult`): blocked
  reasons, failing checks, findings, ship score breakdown
  (`quality/scoring.py` — six criteria, deterministic, threshold default
  85). This supports "the gate blocked on X", not "stage Y was the problem".
- **Lessons** (`core/learning/lessons.py`, `models.py`): text capped at 300
  characters, roles, tags, `uses`, `score`, `retired`, `source_run` /
  `source_task`. Scoring is narrow: `score_task` credits a lesson once per
  (run, task), +1 when the task finished DONE on its first attempt
  (`core/learner.py::score_lessons`), and a lesson retires at score −2.0.
  Lessons are prompt advice ("do X"), not structured outcome records: there
  is no field for "stage", "cost", "what was tried", or "what failed". Any
  suggestion that claims "history says VALIDATING struggled here" would have
  to parse free lesson text to get there.
- **Lesson graph** (`core/learning/graph.py`): `supersedes`, `contradicts`,
  and `applies-to` (lesson→module) edges; `derived-from` is a view over
  `source_run`, and `co-occurs-with` is computed on demand from attempts'
  `lesson_ids`. The graph answers "which lessons relate to this module",
  not "which stage should change". `supersedes` is advisory only and never
  retires the old lesson.
- **Learner signals** (`core/learner.py::learning_signals`, `run_card`):
  per-task `attempts` and a `struggled` flag (not DONE, or more than one
  attempt), plus `plan_rounds` and a signals list. This is the nearest
  existing "what struggled" summary — but it is produced only by the
  end-of-run learning step, only when learning is on, and only for runs in
  `LEARN_STATES`. It is not a queryable history table.

**Thin or absent — the parts the hypothesis leans on hardest:**

- **CDA ratings** (`code_orchestrator/ledger.py`): a separate JSONL file,
  `<repo>/.jev/cda-ratings.jsonl`, append-only, best-effort writes, corrupt
  lines skipped. Records carry agent, model, effort, tier, skill,
  `code_quality` (0–4), `instruction_followed`, `likely_correct`,
  `merge_action`, confidence, and gate decision — but only for graded diffs
  and routing grades. Aggregates count graded diffs only for quality/accept
  rates; `unavailable`/`error` records count in `n` but never as a 0.
  `best_tier` requires at least 3 graded diffs (`MIN_GRADES_FOR_BEST_TIER`),
  and the ledger "never decides anything on its own" — it only enriches a
  D4 hint Jev may follow. Three hard limits for suggestions: the grader is
  off by default (`[code_orchestrator] enabled = false`), a missing backend
  records `unavailable` with no quality fields (and on the Mac there is no
  backend by design, so the ledger there is mostly `unavailable`), and a
  grade is about a diff/task, not about a pipeline stage — it cannot tell
  the engine whether VALIDATING or DESIGNING was worth its cost.
- **Cache behaviour** (`core/cache.py`): exact-match entries for decisions
  and read-only roles, with `cost_usd`, `hit_count`, and derived `saved_usd`
  (`store/cache.py`); coder and learner roles are hard-denied; only
  `success` results are stored; decisions are stored only from clean
  primary answers. Semantic matching is default off
  (`[cache.semantic] enabled = false`, mode `off`/`shadow`/`serve`), and
  live serving stays off until shadow data proves it safe. Shadow data
  (`Cache.log_shadow`, `calibrate_thresholds`) measures agreement between a
  cached answer and the live answer at similarity thresholds 0.90–0.99,
  shipping only at ≥0.99 agreement. That is evidence about answer
  equivalence, not evidence that a stage can be skipped or a cheaper model
  is safe. The hypothesis is right to keep live semantic serving off, but
  it should not let "cache behaviour" in a suggestion quietly upgrade shadow
  agreement into a recommendation to shorten the flow.
- **No goal label anywhere.** No run, lesson, grade, or cache row records
  whether the user wanted efficiency or accuracy. A suggestion "conditioned
  on their goal" therefore has no training signal: it can only apply the
  preset's fixed settings and narrate them as if they came from history.
  The design must either record the goal on the run from day one (a new
  `RunRecord` field, like the profile field in (a)) or admit that early
  suggestions are presets with explanations, with no history behind them.
- **Learning is off by default.** `[learning] enabled = false`, and D7 only
  distills lessons when there are struggle signals or the profile has fewer
  than 3 lessons. A new repo therefore has, by design, almost no lesson
  history, and a repo that never enabled learning or the CDA grader has no
  suggestion input at all. The hypothesis's "early history is thin" sentence
  understates this: for many repos the input is not thin, it is absent, and
  the suggestion UI must say that instead of showing a confident-looking
  flow.
- **No counterfactuals.** Nothing records what a cheaper model, a skipped
  validation, or a shorter flow would have produced on the same work. Every
  suggestion of the form "cheaper models are safe here because history says
  so" is therefore an extrapolation from runs that used the full flow, not
  evidence from runs that used the short flow. The honest version of the
  Efficiency preset is "these settings are cheaper; we have not measured
  their quality on this repo yet", and the design should require that
  wording until short-flow runs exist to compare against.

Useful existing thresholds to copy, not reinvent: `[dispatcher.cost]
min_history_runs = 3` before cost estimates trust history, and the CDA
ledger's 3-graded-diff floor before naming a best tier. Both are small, and
both are floors for naming something, not proof of significance. A suggestion
engine needs its own stated minimum (runs per repo, graded diffs per tier,
cache hits per stage) before it upgrades a preset into a "suggested" flow —
and the hypothesis does not set one.

## (d) Which existing mechanisms already vary the flow, and where do they overlap or conflict?

The pipeline already varies along nearly every axis the presets claim. A
profile that does not define precedence against these will fight them:

| Existing mechanism (code) | What it already varies | Overlap / conflict with profiles |
|---|---|---|
| `AutonomyMode` (`core/models.py`): STANDARD, AUTONOMOUS, ASK_EVERY_STEP, MANUAL, MEDIUM | Gates: STANDARD is approve-once; AUTONOMOUS skips the plan gate (explicit opt-in only); ASK_EVERY_STEP adds six gates via `STEP_GATES`; MANUAL stops at INTAKE; MEDIUM is STANDARD scoped to a folder | A profile that adds or removes gates duplicates autonomy mode. Which wins when a profile says "no approval gate" and the run is ASK_EVERY_STEP, or vice versa? Unspecified. |
| `plan_only` (`Pipeline.start`, `RunRecord.plan_only`) + `[approval] require_plan_approval` (`config/schema.py::plan_approval_required`) | Whether a validated plan waits at AWAITING_APPROVAL | The Accuracy preset's "human approval gates kept" is already these two flags. A profile must map to them, not duplicate them. |
| Quality levels (`quality/levels.py`): none, basic, medium, production, scalable; per-run `--quality` | Which gate checks run; `gate_from` decides what blocks (`quality/policy.py::blocking_reasons`); coverage and security/performance blocking rise with level | The Efficiency preset's "fewer checks" is just level `none` or `basic`; the Accuracy preset's "quality gate" is `production`/`scalable`. But level `none` also removes the safety floor (see above). |
| Ship score gate (`quality/scoring.py`): threshold default 85, `score_max_retries` default 2, `score_skip_human` | Whether a D6 "ship" vote actually ships; rework rounds before a human stop | A profile that drops the ship gate is one boolean (`score_skip_human`) away today. The safety-floor rule must cover this flag explicitly. |
| Ship mode (`core/ship.py::after_approval`, `RunRecord.ship_mode`): merge / ask / off | Whether D6 "ship" goes to SHIPPING, AWAITING_SHIP, or straight to DONE | A profile with a SHIPPING node but ship disabled, or without one but ship enabled, is incoherent. The profile validator must read `[ship]` and the run's `ship_mode`. |
| Budget caps (`[budget]`: per-run, per-task, `max_plan_rounds` 3; `[integration] max_fix_rounds` 2 via `PipelineSettings`; `max_task_attempts` 3) | How many retries, revisions, and fix rounds before a human stop | The Efficiency preset's "fewer rework rounds" is these three numbers. A profile that sets them must say whether it overrides `jev.toml` and per-run flags. |
| Role models and planner policy (`config/defaults.py` `[roles.*]`, `[models] planner_policy`: free_training_ok / mixed / mixed_no_training / paid_only; `prefer_free`; `[dispatcher.cost] prefer_cheaper_leg`) | Which worker/model/effort each role gets; whether free models are tried first; Jev leg order by price | The Efficiency preset's "cheaper models where history says they are safe" is mostly `planner_policy` + `prefer_free` + role model lists. "Where history says they are safe" is the only new part, and (c) shows that history does not exist in that form. |
| Validator independence (`roles.validator.prefer_different_vendor_than = "planner"`, `Pipeline._validator_assignment` via D2) | Whether plan validation comes from a different vendor | The Accuracy preset's "plan validated by a different vendor's agent" is this one setting, already available. It is not a flow change. |
| CDA grading (`[code_orchestrator] enabled`, threshold 0.7, `quality/gate.py::apply_code_grade`) | Whether diffs get a calibrated merge grade on top of the gate; below-threshold grades ask a human, never silently pass | The Accuracy preset's "CDA grading" is this toggle. Off by default, and unavailable without the model backend — so the preset's behaviour differs by machine (see (c)). |
| Cache (`[cache] enabled`, TTLs, `cache_roles`; semantic mode off/shadow/serve) | Whether decisions and read-only role outputs are reused; whether semantic reuse is measured or served | The Efficiency preset's "aggressive cache reuse" is cache settings, not flow. "Aggressive" must not mean turning semantic mode to `serve` before the ≥0.99 calibration bar is met — the hypothesis says this, but the preset definition must enforce it in code, not in prose. |
| Review toggle (`[review] enabled`) and DESIGNING condition (`Plan.needs_design`) | Whether each task diff gets a reviewer (D8); whether DESIGNING runs at all | A profile editor that lists DESIGNING as a free node hides that it is conditional on the planner's `needs_design` flag today. |
| Workspace profiles (`[profiles.<name>]`, `Pipeline.start(profile=...)`) | Servers, access overrides, learning, quality for a run | See the naming collision in (a). A workspace profile already sets `quality` and `learning`; a workflow profile that also sets them needs precedence rules. |

The practical consequence: both presets, as described, are mostly bundles of
existing settings plus a stage order. That is a reasonable product — a
one-click bundle is genuinely useful — but the design should describe them
honestly as bundles, with a mapping table from each preset to the exact
settings it sets (`quality` level, `planner_policy`, cache mode, autonomy
mode, round caps, ship mode, CDA toggle). Without that table, "Efficiency"
and "Accuracy" are marketing labels over settings the user can already
change, and a user who changes one underlying setting will not know whether
they are still "on" the preset.

## Open questions the design must answer before implementation

1. **Transition ownership.** Do handlers return outcomes that a profile maps
   to next states, or does the profile language express plan-round limits,
   fix-round limits, `needs_design`, and ship mode? The answer determines
   whether this is a refactor of `pipeline.py` or a new interpreter.
2. **Precedence.** When a workflow profile, a workspace profile, `jev.toml`,
   per-run flags (`--quality`, `--plan-only`, `--auto-ship`), and autonomy
   mode disagree, which wins, in what order? List the full order; "profile
   wins" is not enough, because there are two kinds of profile.
3. **Safety floor, precisely.** Which stages are non-removable, and does
   that rule also close the existing bypasses (quality `none`, AUTONOMOUS
   skipping the plan gate, `score_skip_human`, ship score `None` shipping)?
   If not, the reduced-assurance label must appear on those existing
   configurations too, or it is misleading.
4. **Profile identity and versioning.** Where is a workflow profile stored
   (repo config, user config, dashboard store?), what is its version field,
   and is it snapshotted onto the run at start like the workspace profile
   is? What happens to running or resumed runs when a profile is edited?
5. **Measurement before suggestion.** Are the recording points in (b) a
   prerequisite phase, with suggestions disabled or labelled "preset only"
   until per-stage data exists? What minimum counts (runs, graded diffs,
   cache observations) promote a preset to a suggestion?
6. **Goal recording.** Is the user's goal (efficiency / accuracy / custom)
   stored on the run so future suggestions have a signal to learn from, or
   does the engine stay goal-blind and only apply presets?
7. **Editor validation rules.** What is the exact invalid-graph catalogue
   (unknown node, unreachable state, gate orphan, ship/DESIGNING
   incoherence, unbounded cycle), and what plain-language message does each
   produce? "Plain-language refusal, not a crash" needs the catalogue, not
   just the promise.
8. **Scope: per repo or per user?** The hypothesis says "per repo or per
   user" without choosing. Learning profiles already have repo vs shared
   scope (`core/learning/models.py::Scope`); workflow profiles need the same
   choice, including where the file or row lives and what happens when a
   user-level profile is used in a repo that lacks its referenced settings.
9. **What "run a custom flow entirely from the browser" leaves out.**
   Dashboard runs already go through `Pipeline.start` with its flags; the
   acceptance criterion needs the concrete mapping from editor actions to
   `Pipeline.start` arguments and stored profile fields, or "entirely from
   the browser" will quietly depend on a `jev.toml` edit.

## Findings list (severity, per the Goldfish format)

- **Blocker — pipeline.py `_advance` and handlers:** no data-driven
  transition table exists; handlers choose next states internally. A
  profile-as-data requires restructuring `_advance`, all next-state-choosing
  handlers, `STEP_GATES`, `approve`, and `_check_resumable`.
- **Blocker — safety floor vs existing bypasses:** quality `none`, missing
  ship score shipping, AUTONOMOUS skipping the plan gate, and
  `score_skip_human` already remove the stages the hypothesis calls
  non-removable. The floor must address these settings, not just new
  profiles.
- **Blocker — name collision:** `profile` already means workspace profile
  (`Pipeline.start`, `QueueItem.profile`) and learning profile
  (`RunRecord.learning_profile`). The new concept needs a distinct name and
  a precedence order against both.
- **Major — per-stage measurement absent:** no per-stage tokens/time/spend
  columns; tokens live in `result_json` / `details` JSON or are `None`;
  gate and ship stages have no duration. Recording points in (b) are a
  prerequisite for honest suggestions.
- **Major — suggestion inputs thin or off by default:** learning off by
  default; CDA grader off by default and backend-less on the Mac
  (`unavailable`, no quality fields); cache semantic serving off by default
  with shadow data measuring answer agreement, not stage-skip safety; no
  goal label on any record; no counterfactuals for short flows.
- **Major — presets duplicate existing settings:** both presets map almost
  entirely onto quality levels, planner policy, cache settings, autonomy
  modes, round caps, ship mode, and the CDA toggle. The design needs an
  explicit preset→setting mapping table and a rule for what happens when a
  user changes an underlying setting.
- **Major — editor validation undefined:** legitimate bounded loops (plan
  revise, fix tasks) vs unbounded cycles, gate orphans, DESIGNING's
  `needs_design` condition, and ship-mode coherence have no validator today.
- **Nit — hypothesis wording:** the pipeline is described as 7 work states,
  but `_advance` maps 7 work states while `RunState` also carries 6 gate
  states (AWAITING_PLANNING, AWAITING_VALIDATING, AWAITING_DESIGNING,
  AWAITING_INTEGRATING, AWAITING_SHIPPING, AWAITING_SHIP) plus AWAITING_APPROVAL,
  DESIGNING, SHIPPING, and 5 stopped states. The design should count gates
  explicitly, because gates are where a profile most easily strands a run.
