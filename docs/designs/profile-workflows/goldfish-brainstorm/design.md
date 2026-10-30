# Goldfish brainstorm review: Profile-based process flows (Baton issue #368)

Lens: design / UX and ergonomics, plus YAGNI (does this need to exist, and what is
the smallest version that solves the real problem).

Mode: BRAINSTORM. This reviews the hypothesis as stated, grounded in the current
code on branch `design/profile-workflows`. It is not a review of a design document,
because no design document exists yet.

## The strongest objection

Nothing in the hypothesis shows that anyone wants to reorder pipeline stages.
The two user goals it names — spend fewer tokens, get better code — are knob
problems, not graph problems. Baton already has the knobs: quality levels
(`none` / `basic` / `medium` / `production` / `scalable`), the four planner model
policies, autonomy modes (`standard` / `autonomous` / `ask_every_step` /
`manual`), plan-approval settings, fix-round caps, and cache settings. A preset
picker that sets those knobs covers both named goals without a second state
machine. A flowchart editor, by contrast, creates a second state machine that
must stay in sync with the real one forever, and every place the two diverge is
a bug the user meets mid-run, after tokens are already spent. Build the picker
first, and only build the editor if real usage shows people asking to reorder
stages — that evidence does not exist in the hypothesis.

## 1. The editor misrepresents the machine it edits

The hypothesis describes the pipeline as seven stages in a line. The code is
not that. `RunState` in `core/models.py` has about twenty states, and the
`_advance` loop in `core/pipeline.py` is full of behaviour a seven-node diagram
cannot show:

- Loops, not lines. A rejected plan goes back from VALIDATING to PLANNING.
  A failed integration goes back to EXECUTING with a fix task, up to
  `max_fix_rounds` times. A below-threshold ship score goes back to EXECUTING
  with a score-fix task, up to `score_max_retries` times.
- Conditional stages. DESIGNING runs only when the plan says it needs one
  (`Plan.needs_design`). It is not a stage the user can simply place in a
  sequence; its existence depends on plan content.
- Gate states are states. `AWAITING_APPROVAL`, `AWAITING_SHIP`, and the six
  `ASK_EVERY_STEP` gate states (`AWAITING_PLANNING`, `AWAITING_VALIDATING`,
  `AWAITING_DESIGNING`, `AWAITING_INTEGRATING`, `AWAITING_SHIPPING`) are where
  runs actually wait. A diagram that shows only work stages hides the places
  where a run stops and waits for a person — which is exactly what a user
  rearranging a flow needs to see.
- Halts are not nodes. Every step can stop the run in NEEDS_HUMAN, FAILED,
  BUDGET_EXCEEDED, or CANCELLED. These are outcomes of every node, not boxes
  the user draws.
- Decisions are not stages. D1–D8 (intake clarity, validator choice, plan
  verdict, routing, rework, ship vote, learning) happen inside and between
  stages. A user who drags nodes around will reasonably expect to move or
  remove decisions too, and the hypothesis does not say whether they can.

Severity: Major. If the editor shows seven clean boxes, users will build flows
that look valid and behave differently from the diagram. If it shows the real
machine — twenty states, loops, gates, conditional branches — it is no longer a
simple editor; it is a state-machine IDE, and that is a much larger product
than the hypothesis describes. The design must pick one of these and be honest
about the cost of the choice. The hypothesis currently picks neither: it
promises "stages as nodes, transitions as edges" without saying which states
are nodes.

## 2. A picker plus toggles solves the stated goals

Work through each preset with existing knobs:

- Efficiency (shorter flow, cheaper models, cache reuse, fewer rework rounds)
  maps to: lower quality level, a cheaper planner policy, exact-cache reuse,
  lower `max_fix_rounds` / `max_plan_rounds`, and skipping the design step
  where possible. No stage reordering is needed for any of these except
  "shorter flow," and the hypothesis never says which stages the shorter flow
  drops — which suggests the shorter flow is really "fewer rounds and cheaper
  checks," not a different graph.
- Accuracy (full flow, different-vendor validation, quality gate, CDA grading,
  human gates kept) maps to: the default flow plus production quality level,
  CDA grading on, autonomy `standard`. That is already the default behaviour
  with settings turned up, not a new flow.

So the smallest shippable version is:

1. A profile picker on `/runs/new` with three options: Default, Efficiency,
   Accuracy. Each is a named bundle of existing settings, shown with its
   settings listed in plain words so the user can see what changes.
2. A small set of toggles on the same screen for the settings people will
   actually vary per run (quality level, approval gate on/off, fix-round
   limit). These already exist as config; the work is surfacing them per run.
3. "Save these settings as a custom profile" — a name plus the same settings
   bundle, no graph. Editing a profile means changing its settings, not
   redrawing it.
4. Per-run measurement displayed after the run (tokens and time per stage, see
   section 5), so the presets can be judged on evidence instead of promises.

Only after steps 1–4 ship and usage shows people hitting the limits of
settings bundles — for example, repeatedly asking to skip a specific stage —
is there a case for a graph editor. The editor is phase 2, not phase 1, and
the hypothesis should say so.

## 3. Where the editor's flexibility creates confusion or danger

### Flows that look fine and do nothing, or do the wrong thing

- Skip INTEGRATING and keep SHIPPING: there is no merged result branch to
  ship. `_integrate` is what produces the result branch; `_ship` merges a PR
  from it. A flow that ships without integrating ships nothing, or ships a
  stale branch. The editor must refuse this, but the hypothesis does not list
  it.
- Skip EXECUTING and keep INTEGRATING: integration merges task branches that
  were never built. Same refusal needed, not listed.
- Skip VALIDATING and keep human approval: the human now approves a plan no
  validator challenged. That is a weaker gate wearing the same name. A label
  saying "reduced assurance" does not fix the fact that the approval screen
  looks identical to the full-assurance one.
- Skip PLANNING: there is no plan, so there are no tasks, so EXECUTING has
  nothing to run. Refusal needed, not listed.
- Loops drawn by hand: EXECUTING → PLANNING, or INTEGRATING → VALIDATING.
  The real machine already loops, but under caps (`max_plan_rounds`,
  `max_fix_rounds`, score retries). A hand-drawn loop has no visible cap. A
  user can draw an infinite loop, and the run will burn tokens until the
  budget gate stops it — the budget gate is the backstop, not a design.
- Reordering that breaks data dependencies: DESIGNING needs a plan;
  VALIDATING needs a plan; INTEGRATING needs executed tasks; the learning
  step (`_finish`) needs an ended run with a learning profile. Any
  reordering that violates these is not a "custom flow," it is a broken one.
  The hypothesis says "an invalid graph gets a plain-language refusal," but
  it does not list the dependency rules, and those rules are the whole
  validation design.

### The safety floor is undecided, and the two options are very different

The hypothesis offers two options — some stages non-removable, or a flow that
drops validation ships with a visible reduced-assurance label — without
choosing. These are not interchangeable:

- Non-removable stages: the editor is honest but less flexible. The user
  needs to know, before dragging, which nodes cannot be removed. Greyed-out
  nodes with a reason ("the quality gate cannot be removed because it is
  what stops broken code from shipping") are a design in themselves.
- Reduced-assurance label: the label must survive everywhere the result is
  seen — the run page, the PR description, the shipped code's provenance —
  or it is decoration. A label shown only on the profile editor does not
  protect anyone reading the PR later. The hypothesis does not say where the
  label travels, and a label that does not travel is worse than a refusal,
  because it lets the author feel safe while the reader has no warning.

There is a third question the hypothesis does not ask: who is allowed to drop
a safety stage? If profiles are per repo, one person's reduced-assurance
profile becomes the next person's default run. If profiles are per user, a
teammate's run is unaffected but the same person can still fool themselves.
Per-repo vs per-user is not just a scoping detail; it decides who bears the
risk of a weakened flow.

### The existing autonomy modes already cover "skip the gates"

`AUTONOMOUS` already skips the plan-approval gate (with an explicit opt-in).
`ASK_EVERY_STEP` already adds a gate before every step. `MANUAL` already
holds the run in INTAKE until told to move. A workflow profile that drops or
adds approval gates duplicates these modes. The design must decide: do
profiles replace autonomy modes, layer on top of them, or forbid combining
them? If a profile says "no approval gate" and the run's autonomy mode says
`ask_every_step`, which wins? The hypothesis does not mention autonomy modes
at all, and shipping both systems without an answer guarantees a run that
behaves differently from what either screen promised.

## 4. What the design must decide about the editor (currently open)

If the editor is built despite section 2, these decisions cannot be left to
implementation. Each one is a user-visible behaviour, not an engineering
detail.

**Node palette.** Which states are nodes? Work stages only, or gates too?
Are NEEDS_HUMAN / FAILED / DONE nodes, outcomes, or hidden? Is DESIGNING in
the palette for every flow, or only when the plan needs design — and if a
user adds it to a flow whose plans never need design, does it run empty, get
skipped silently, or refuse? Is the learning step (`_finish`) a node, or does
it always run at the end regardless of the graph? Is SHIPPING a node, given
that shipping is already gated by `[ship]` config and `after_approval` mode?

**Edge rules.** What edges are legal? The design needs a written rule set:
every flow must start at INTAKE and end at DONE; every node except DONE must
have an outgoing edge; loops must declare their exit condition and cap; an
edge into a gate state must say who approves and what happens on refusal.
Without this list, "invalid graph gets a refusal" is a slogan, not a
specification, and implementers will invent the rules one error message at a
time.

**Validation feedback: when, where, in what words.** Three moments need
different behaviour, and the hypothesis covers none precisely:

- While dragging: does an illegal edge snap back, refuse to connect, or
  connect and flag an error? Silent snap-back teaches nothing; a flag the
  user must hunt for is barely better.
- At save: the refusal must name the broken rule in plain words tied to the
  nodes ("INTEGRATING needs EXECUTING before it, because it merges the
  tasks EXECUTING built") — not "graph invalid" and not a code-level error.
- At run time: a flow that was valid when saved can become invalid later
  (config changes, quality level set to `none`, CDA backend uninstalled).
  Does the run refuse to start, fall back to default, or run degraded? Each
  is defensible; silently falling back is not, because the user asked for
  one flow and got another.

**Editing a profile that runs are using.** Profiles are versioned, but the
hypothesis does not say what a version means. If a run is mid-flight on
version 1 and the user saves version 2, does the run keep version 1, switch
mid-flight, or block the edit? If a run records only a profile name and not
a version, its behaviour is unreproducible after an edit — the run page will
show a flow the run did not follow. The minimum honest rule is: a run pins
the profile version it started with, and the run page shows that version.

**The editor's own ergonomics.** The dashboard is browser-first and is
verified at 390px width (the narrow-phone checks elsewhere in this program).
A drag-and-drop flowchart editor is a desktop-shaped tool. The design must
say how it works without a mouse, without drag-and-drop, and on a narrow
screen — a list-based editor (ordered stages with add/remove/reorder buttons)
works everywhere a canvas does not, and it matches the actual structure
better, since legal flows are mostly sequences with loops, not free-form
graphs. If the answer is "the editor is desktop-only," that contradicts the
browser-first rule and should be said out loud, not discovered later.

**Preview before running.** Before a custom flow runs for the first time, the
user should see what will happen in plain words: the stages in order, which
gates exist, which safety stages are missing, and what it will cost in
relative terms. A "dry run" or a written summary is cheaper than a flowchart
and catches more mistakes, because it describes behaviour instead of shape.

## 5. Suggestions: the evidence is thinner than the hypothesis admits

The suggestion feature rests on four data sources, and each is weaker than
the hypothesis implies:

- **Per-stage outcomes and costs.** Worker results carry cost and tokens per
  attempt, and gate results carry scores, but "per stage" is not a recorded
  unit today. The design must define it: is a stage's cost the sum of its
  attempts, including retries and fix rounds, or only the final attempt? Do
  validator and planner costs count under VALIDATING or under their own
  stages? Until this is defined, "real measurement, not estimated" is a
  promise without a unit.
- **What struggled and what worked (learning loop).** Lessons exist per
  learning profile, but a lesson is advice for a role, not a measurement of
  a stage. Turning lessons into "this stage struggles on this repo" needs a
  rule the hypothesis does not give, and inventing one risks presenting a
  guess as a finding.
- **CDA ratings per agent/tier.** The ledger (`cda-ratings.jsonl`) is real,
  but grades are rare and slow (minutes each on CPU), and a tier needs at
  least 3 graded diffs before it can even be named best — and 3 is a floor,
  not significance. On a repo with few runs, the ledger will mostly say "no
  grades yet," which is honest but useless for suggestions. The hypothesis
  correctly says early suggestions start as presets plus explanations; the
  design must also say what happens when the ledger contradicts a preset
  (for example, the "efficient" tier grades badly) — does the suggestion
  change, warn, or stay silent?
- **Cache behaviour.** Exact-cache hits are real and measurable. Semantic
  cache is shadow-only by design: `similar()` scans and `log_shadow` records
  agreement without serving, and live serving stays off until shadow data
  proves it safe. The Efficiency preset must therefore be explicit that its
  "aggressive cache reuse" is exact-cache only. Any suggestion text that
  implies semantic reuse is saving tokens today would be false, and the
  hypothesis is right to forbid pretending otherwise — but the preset's
  marketing copy is exactly where that pretence will creep in, so the design
  should show the exact wording the preset uses, not just the rule.

There is also a cold-start problem the hypothesis names but does not solve:
per-repo history is thin, so early suggestions are presets plus
explanations. That means the suggestion feature's first version is a preset
picker with a paragraph of reasoning — which is section 2's smallest version
again, with an explanation attached. The honest sequence is: ship presets
with explanations, collect real per-stage measurements, and only then build
suggestion logic that has something to stand on. Building the suggestion
engine before the measurements exist means building it on invented rules.

## 6. Naming collision: three things already called "profile"

The dashboard already has a `/profiles` page for learning profiles
(`core/learning/profiles.py`: named lesson collections with enable/disable,
flush, delete). `Pipeline.start` also takes a `profile` argument, which is a
workspace/server profile snapshot, a third meaning. Adding "workflow
profiles" gives three different objects one name, on overlapping screens:

- A user on `/profiles` who creates a "workflow profile" will expect it to
  behave like the learning profiles on the same page. It will not.
- A run record carrying both a learning profile and a workflow profile will
  show two fields called "profile" with different meanings.
- Documentation, CLI flags, and error messages will need constant
  disambiguation ("workflow profile" every time, never bare "profile"), and
  any place that shortens it will confuse.

Severity: Major for UX, minor for code. The design must rename before
building: candidates are "flow," "run flow," or "pipeline template." This is
cheapest to fix now and most expensive to fix after a `/profiles` page, a
route, and a stored format all carry the wrong name. The hypothesis
acknowledges the collision in passing; it should resolve it, because the
name is the first thing users meet.

## 7. Risks the hypothesis does not mention

- **Profile sprawl.** Named profiles per repo or per user, plus presets,
  plus suggestions, plus versions, quickly become a list nobody prunes.
  The design needs deletion, renaming, and a way to tell which profiles are
  actually used — otherwise the picker fills with stale experiments and the
  default stops being the obvious choice.
- **Sharing and portability.** Is a profile stored in the repo's `.jev/`
  (like the CDA ledger and state DB, gitignored, per-repo) or in the user's
  home (portable across repos)? Per-repo matches the learning graph; per-user
  matches "per user" in the hypothesis. The choice decides whether a good
  flow follows the user to their next repo or stays behind, and whether a
  profile can be shared by copying a file.
- **Cost of the editor itself.** A flowchart editor is a large frontend
  build (canvas or graph library, drag handling, validation UI, narrow-screen
  fallback) plus a graph-to-pipeline compiler plus migration for runs in
  flight. That is the most expensive part of the proposal and the least
  tied to the stated goals. The design should cost it separately from the
  presets, so the decision to build it is made with its price visible.
- **Testing burden.** Every legal graph shape needs a test that a run
  following it reaches DONE or a clean halt, and every illegal shape needs
  a test that it is refused in plain words. A settings bundle needs a
  handful of tests; a graph compiler needs a matrix. The acceptance bar
  ("verified live on the Mac dashboard") should name which graph shapes
  were verified, not just that the editor opened.

## Counter-arguments, stated fairly

- A flowchart editor could help a user who genuinely thinks in flows and
  wants, say, two validation rounds or a design review before execution.
  The reply: those are still settings (validation rounds, a design gate),
  and a settings screen expresses them with less machinery and fewer ways
  to break a run.
- Presets could feel rigid to a power user who knows exactly which stage
  to drop. The reply: that user is rare, and their need is better served by
  a small, explicit "advanced" screen (a checklist of stages with the
  safety stages locked and the dependency rules enforced) than by a
  free-form canvas. A checklist is also the honest middle ground if the
  editor is ever built: it enforces the dependency rules by construction
  instead of validating them after the fact.
- Suggestions could eventually beat presets, once history is rich. The
  reply: agreed, but "eventually" is the operative word. The suggestion
  engine should be built after the measurement exists, not before.

## What the design must answer before implementation

1. Rename: what are workflow profiles called, given learning profiles and
   server profiles already use "profile"?
2. Editor or not: is the phase-1 deliverable the picker plus toggles, with
   the editor deferred until usage justifies it? If the editor stays in
   phase 1, what evidence justifies it?
3. Node palette: which states are nodes, and how are gates, loops, halts,
   and the conditional DESIGNING stage shown?
4. Edge rules: the written list of legal and illegal flows, including the
   dependency rules (integrating needs executing, shipping needs
   integrating, designing and validating need a plan).
5. Safety floor: non-removable stages, travelling reduced-assurance label,
   or both — and who may drop a safety stage, per repo or per user?
6. Autonomy modes: how do profiles interact with `standard` /
   `autonomous` / `ask_every_step` / `manual`, and which wins on conflict?
7. Validation feedback: what happens while dragging, at save, and at run
   time when a saved flow becomes invalid — in what words, on which
   screen?
8. Version pinning: does a run pin the profile version it started with, and
   what happens to in-flight runs when a profile is edited?
9. Measurement unit: what counts as a stage's tokens and time, including
   retries and fix rounds?
10. Suggestion honesty: what does the Efficiency preset say about cache
    reuse, in exact words, given semantic serving is shadow-only? What
    happens when CDA data contradicts a preset?
11. Editor ergonomics: how does the editor work without a mouse and at
    390px, and is a list-based editor (ordered stages, add/remove/reorder)
    acceptable instead of a canvas?
12. Storage and scope: per repo or per user, where on disk, how shared,
    how deleted, and how stale profiles are pruned?

## Bottom line

Ship the picker, the toggles, and save-as-custom-settings first, with
presets whose settings are shown in plain words and per-stage measurement
collected from real runs. Defer the flowchart editor and the suggestion
engine until the measurements and the usage justify them. Resolve the name,
the node palette, the edge rules, the safety floor, and the autonomy-mode
interaction in the design document before any editor code is written — those
five decisions are the design, and everything else is implementation.
