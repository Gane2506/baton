# Goldfish brainstorm review — ZOOM OUT lens

## Lens

Does this feature solve the right problem at the right altitude, or is it
scope creep, a premature optimisation, or a solution in search of a problem?

Mode: BRAINSTORM. Concept under review: Baton issue #368,
"Profile-based process flows" — named, versioned workflow profiles per repo
or per user, a browser flowchart editor, two built-in goal presets
(Efficiency and Accuracy), and suggested flows drawn from the user's own
usage history.

Grounding read before writing: `src/jev_agent/core/models.py` (RunState,
AutonomyMode), `src/jev_agent/core/pipeline.py` (the `_advance` loop,
STEP_GATES, `_integrate` with the D6 ship vote and score gate),
`src/jev_agent/core/learning/profiles.py` (the existing learning-profiles
store), `src/jev_agent/core/cache.py` (semantic similarity, default OFF,
shadow mode), and `src/jev_agent/dashboard/app.py` (the route table).

## Short verdict

This proposal bundles three different features — a settings preset, a
graph editor, and a recommendation engine — and presents them as one
feature. Only the first one has a clear job to do. The other two add a
large amount of new machinery for a benefit the proposal does not show
anyone asked for. Cut the proposal down to presets plus a picker, with
the safety stages fixed, and the stated goals (cheaper runs when that is
safe, careful runs when that matters) are still met. Everything else
should have to earn its way back in with evidence, one piece at a time.

## The pain is assumed, not shown

### Finding 1 — No one in the proposal asked to rearrange the pipeline

- **Severity:** Blocker
- **Dimension:** Contrarian / YAGNI
- **Challenge/Risk:** The proposal never says who asked for rearrangement,
  or what went wrong on a real run because the stage order was fixed.
  It lists three possible pains — cost, waiting time, and control — and
  treats them as if they were one pain with one answer. They are not.
  A user who wants cheaper runs wants cheaper models and fewer retries.
  A user who wants faster runs wants fewer rounds and less waiting at
  gates. A user who wants control wants to approve or skip a step. None
  of those requires moving stages around on a canvas. Rearrangement is
  the proposal's answer, not the user's request, and the proposal gives
  no run, no quote, and no measurement that shows stage order was ever
  the problem.
- **Alternative / Reuse Opportunity:** Before any editor is built, name
  the pain in one sentence with a number attached — for example, "runs
  on repo X cost $Y on average and users abandon them at stage Z" — and
  check whether that number moves when only model choice and retry
  limits change. If it does, stage order was never the problem.

### Finding 2 — The fixed pipeline is the product's promise, not a limitation

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI
- **Challenge/Risk:** Baton's claim to a user is that an idea goes through
  the same checked path every time: plan, validate, approve, build,
  integrate, vote on shipping. That sameness is what makes a run's result
  comparable to the last run's result, and what makes a failure
  explainable ("it stopped at validation for this reason"). Letting
  every repo or user reorder that path trades that sameness away. Two
  runs of the same idea on two profiles can then differ in ways that
  have nothing to do with the idea, and debugging becomes "which flow
  were you on?" before it can become anything else. The proposal's own
  safety-floor clause admits this tension: it cannot decide whether
  safety stages are non-removable or merely labelled when removed.
  Those are two different products, and leaving the choice open at
  brainstorm stage means the hardest decision in the whole feature is
  being deferred while the easy parts (an editor, presets) are built
  around it.
- **Alternative / Reuse Opportunity:** Keep the stage order fixed and
  vary only what is already a knob: model choice, retry limits, cache
  use, and which approval gates are on. That keeps every run
  comparable and keeps the safety question from arising at all.

## The flowchart editor is not proportionate

### Finding 3 — Almost no rearrangement is actually legal, so the editor's power is mostly illusory

- **Severity:** Blocker
- **Dimension:** Contrarian / YAGNI
- **Challenge/Risk:** Read against the real pipeline, the graph editor
  has very little room to move. `pipeline.py` shows why. A plan must
  exist before validation (`_validate` halts as FAILED with no plan).
  Tasks must exist before execution, and execution must finish before
  integration (`_integrate` sends the run back to EXECUTING if any task
  is unfinished). Integration loops back to execution for fix tasks, up
  to `max_fix_rounds`, and the D6 ship vote is then overruled by hard
  limits: a ship vote never ships while the quality gate blocks, while
  the merge is unclean, or while integration tests fail. DESIGNING only
  runs when the plan says it is needed. On top of that, `models.py`
  shows the real state list is not 7 tidy stages but around 20 states,
  including the ASK_EVERY_STEP gates in STEP_GATES
  (AWAITING_PLANNING, AWAITING_VALIDATING, AWAITING_DESIGNING,
  AWAITING_INTEGRATING, AWAITING_SHIPPING) and AWAITING_SHIP. A node
  editor that hides those gates misrepresents the pipeline; one that
  shows them asks the user to reason about a state machine the codebase
  itself handles through mode flags, not through graph order. What is
  left after all the real constraints is: skip a step, add a gate, or
  change a retry limit. All three are settings, not a graph.
- **Alternative / Reuse Opportunity:** Two presets and a picker on the
  run-creation screen (or the existing Settings page — `app.py` already
  has `/settings`, `/runs/new`, and a Tools page) deliver the same legal
  choices with no graph validation engine, no "plain-language refusal"
  logic for invalid graphs, and no new file format to version and
  migrate.

### Finding 4 — The editor is a second product, not a control

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** A browser flowchart editor means: a canvas UI,
  drag-and-drop, a graph data model, validation of that graph against
  pipeline invariants, versioning of saved profiles, migration when the
  pipeline gains a stage, and error messages for every invalid shape a
  user can draw. That is a product in its own right, with its own bugs
  and its own support questions, built to serve a choice (stage order)
  that Finding 3 shows is nearly empty. It also has to live somewhere
  in the dashboard, which already has a large route table split across
  route mixins (`routes_board`, `routes_settings`, `routes_learning`,
  and others). A new editor page is not a small addition to that
  surface; it is a new area to maintain, test, and keep consistent with
  every future pipeline change. Every new pipeline stage would now need
  two implementations: the stage itself, and its node in the editor.
- **Alternative / Reuse Opportunity:** A preset picker — a named bundle
  of existing settings, chosen when a run starts — needs no canvas, no
  graph validator, and no migration story, because it stores settings
  the codebase already understands.

### Finding 5 — The word "profile" is already taken, twice

- **Severity:** Major
- **Dimension:** Contrarian / Code Minimization
- **Challenge/Risk:** `learning/profiles.py` already implements
  learning profiles: named rows in the lesson database that scope which
  lessons a run learns from, with create, list, enable, flush, and
  delete. `pipeline.py` also takes a `profile` argument at run start,
  but that is a workspace server profile, snapshotted into the run's
  directory. Adding "workflow profiles" makes three unrelated things
  share one word in the same product, in the same UI, and in the same
  docs. Users will ask whether disabling a profile disables learning,
  or whether a workflow profile carries server settings, and every
  answer will start with "no, that is the other kind of profile."
  Confusion like that is not a cosmetic problem; it produces wrong
  runs, because a user picks the wrong profile thinking it means
  something else.
- **Alternative / Reuse Opportunity:** If presets ship at all, call them
  something that cannot collide — "run modes", "goal settings", or
  simply "Efficiency / Accuracy" as a choice on the run form — and do
  not introduce a new profile store, a new profile file format, or a
  new profile management screen.

## The suggestion engine risks becoming a second orchestrator

### Finding 6 — Suggestions second-guess decisions Jev already makes

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI
- **Challenge/Risk:** Baton already has a decision layer. The pipeline
  asks Jev at D1 (is the idea clear), D2 (which validator, including a
  different vendor than the planner), D3 (approve, revise, or escalate
  the plan), and D6 (ship, fix, or ask a human), and the learning loop
  already feeds past lessons into planner, validator, and designer
  prompts. A suggestion engine that recommends flow changes from
  per-stage outcomes, CDA ratings, and cache behaviour is a second
  decision-maker sitting next to that one, and the proposal gives no
  rule for what happens when they disagree. If Jev says "revise" and the
  suggester says "this flow wastes tokens, shorten it," which one does
  the user follow? If the suggester is right, why is Jev not already
  using that history? If it is wrong, the user has just been given a
  confident-sounding reason to override the system that was built to
  make that call. Putting the user in the middle as tie-breaker on
  every run is more work for the user, not less, and it weakens trust
  in both systems.
- **Alternative / Reuse Opportunity:** Feed the same history into the
  existing decision points and the existing learning loop instead of
  building a parallel recommender. If a cheaper model is safe for a
  stage, that is a lesson or a model assignment rule, and it should
  show up where Jev already decides — not as a separate suggestion the
  user must approve before it has any effect.

### Finding 7 — Early suggestions add nothing beyond the presets

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI
- **Challenge/Risk:** The proposal's own honesty constraint concedes
  the point: early per-repo history is thin, so suggestions start as
  presets plus explanations. That means the first version of the
  suggestion engine, the version that would actually ship, is the
  presets with a paragraph of text attached. The engine part — the
  per-stage analysis, the CDA-rating aggregation, the cache-behaviour
  analysis — only becomes real after enough runs accumulate, and by
  then the user will already have formed a habit around whichever
  preset they picked. Building the engine now means building, testing,
  and maintaining machinery whose output is, by the proposal's own
  account, the preset the user could have picked directly.
- **Alternative / Reuse Opportunity:** Ship presets with a short, honest
  explanation of what each one changes ("fewer rework rounds, cheaper
  models, cache reuse on"), and record per-stage cost and time as plain
  run data. Revisit a real recommender only when that data shows a
  pattern no preset captures — and state that pattern in the proposal
  for it, rather than assuming it now.

### Finding 8 — The cache leg of the suggestion is premature

- **Severity:** Major
- **Dimension:** Premature Optimisation
- **Challenge/Risk:** The proposal leans on cache behaviour — exact hits
  plus semantic-cache shadow data — as an input to suggestions, while
  also correctly noting that live semantic serving stays OFF until
  shadow data proves it safe. `cache.py` confirms that position:
  semantic similarity is default OFF, shadow mode measures agreement
  without serving, and a threshold ships only where calibration shows
  at least 0.99 agreement. Suggesting flows on the basis of shadow data
  that has not yet cleared that bar puts unproven similarity
  judgements into user-facing recommendations. A suggestion that says
  "reuse more, this is safe" because a shadow measurement looked
  promising is a serving decision by another name, made before the
  safety case exists.
- **Alternative / Reuse Opportunity:** Keep semantic data out of
  suggestions entirely until live serving itself is approved. Exact
  cache hits are already measured and safe to report; they need no
  suggestion engine to be useful.

## Measurement is a prerequisite being sold as a feature

### Finding 9 — "Real measurement" is the unbuilt foundation under everything else

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI
- **Challenge/Risk:** The proposal requires per-stage token and time
  measurement to be real, not estimated, and both presets and
  suggestions depend on it: Efficiency claims savings, Accuracy claims
  care, and suggestions claim to know what struggled and what worked.
  But measurement is not a small add-on. It means recording cost and
  time at every stage boundary, attributing retries and fix rounds to
  the right stage, and keeping those numbers honest when a run is
  resumed, cancelled, or retried — all inside a pipeline whose steps
  already loop back on each other (validation back to planning,
  integration back to execution). If that measurement is not already
  solid, then the presets cannot prove they do what they claim, and the
  suggestions cannot be trusted. The proposal treats measurement as a
  constraint line; in practice it is the first deliverable, and it
  should be judged on its own before anything is built on top of it.
- **Alternative / Reuse Opportunity:** Ship measurement first, alone,
  as run history the user can already see. If the numbers then show a
  stage that is consistently expensive or slow, that evidence — not
  this brainstorm — decides what gets built next.

## What already exists and should be reused, not rebuilt

- **Autonomy modes already vary gate behaviour.** `models.py` defines
  STANDARD (one plan gate plus ship gate), AUTONOMOUS (skips the plan
  gate, explicit opt-in only), ASK_EVERY_STEP (a gate before every
  step, via STEP_GATES), MANUAL (waits for an explicit instruction),
  and MEDIUM (scoped folder). A user who wants more or fewer gates
  today picks a mode; a workflow profile that re-implements gate
  control as graph nodes duplicates this.
- **Model and vendor separation already exists.** D2 already picks a
  validator from a different vendor than the planner when configured.
  The Accuracy preset's headline feature — "plan validated by a
  different vendor's agent" — is therefore existing behaviour being
  re-labelled as a preset, not a new capability the profile system
  unlocks.
- **Quality and ship gates already have hard limits.** `_integrate`
  overrules a D6 ship vote when the quality gate blocks, the merge is
  unclean, or tests fail, and `_ship_after_score_gate` sends
  below-threshold results back for rework. Any profile system must not
  weaken these, and a "reduced-assurance label" does not substitute for
  them: a label tells the user the run is less checked, but the run
  still ships under Baton's name.
- **Learning profiles and the learning loop already exist.** Lessons
  are scoped per profile and fed into role prompts. A suggestion system
  that reads "what struggled and what worked" should read that loop,
  not build a parallel history store.
- **The dashboard already has homes for a picker.** `/runs/new`,
  `/settings`, and `/tools` exist in the route table. A preset choice
  fits on one of those pages; it does not need a new editor area.

## What could be cut (YAGNI) while still delivering the stated goals

Cut, in this order:

1. **The flowchart editor — cut entirely.** Two presets and a picker
   deliver every legal flow the constraints allow. If a future,
   evidenced need for custom stage order appears, it can be proposed
   then, with the legal permutations listed.
2. **The suggestion engine — cut entirely for v1.** Ship presets with
   explanations. Revisit only when per-stage measurements show a
   pattern presets miss.
3. **Versioned, per-repo, per-user profiles — cut.** A preset is a
   named bundle of settings, not a versioned artifact. No versioning,
   no migration, no sharing story until someone needs to share one.
4. **Custom profiles built from scratch — cut.** "A preset, edited"
   covers the real case (a user who mostly wants Efficiency but with
   one gate kept). From-scratch graph building covers a case no one
   has described.
5. **Semantic-cache inputs to suggestions — cut.** Exact cache hits
   are reportable now; semantic shadow data stays an experiment until
   its own safety bar is cleared.
6. **The reduced-assurance label as an alternative to non-removable
   stages — cut the alternative, keep the rule.** Safety stages
   (validation, quality gate, ship gate, human approval) are
   non-removable, full stop. A label that permits shipping unchecked
   work is not a safety floor; it is a way around one.

What remains after those cuts is small enough to describe in one
sentence: when starting a run, pick Efficiency or Accuracy (default:
today's flow, unchanged), and the run records what each stage cost in
tokens and time. That sentence is the whole feature. Everything removed
above should have to return with evidence, not with enthusiasm.

## Strongest objection

The proposal has not shown that anyone wants to rearrange the pipeline,
and it would trade away the one property that makes Baton's output
trustworthy — the same checked path on every run — in exchange for
configurability whose legal range, once the pipeline's real constraints
are accounted for, is barely wider than the settings that already
exist. Build the presets and the measurement; do not build the editor
or the recommender until a real run, with a real number attached,
proves they are needed.

## Open questions for the design phase

1. Who asked for rearrangement, and what exactly failed on their runs
   because the stage order was fixed? A name, a run, and a number.
2. Is the real pain cost, waiting time, or control? Each points to a
   different fix, and the proposal must pick one before it picks a UI.
3. Which stages, exactly, are non-removable — and is the
   "reduced-assurance label" still on the table, or is it dropped?
4. If a suggested flow and a Jev decision disagree, which one wins,
   and who is accountable for the outcome?
5. How many runs of per-stage history, on one repo, are enough before
   a suggestion is more than a preset with an explanation?
6. What does a "versioned profile" version against — pipeline changes,
   model changes, or both — and who migrates a saved profile when the
   pipeline gains a stage?
7. What happens to a run in flight when its profile is edited or
   deleted?
8. Does the Efficiency preset's "cheaper models where history says they
   are safe" change model assignment, cache policy, retry limits, or
   all three — and which of those is already a setting today?
