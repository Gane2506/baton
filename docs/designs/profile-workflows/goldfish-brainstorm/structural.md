# Structural Goldfish review — Profile-based process flows (Baton issue #368)

Lens: STRUCTURAL — coupling, boundaries, maintainability, migration, backward compatibility.
Mode: BRAINSTORM (concept review, not a document audit).
Grounding: read before writing — `src/jev_agent/core/models.py`,
`src/jev_agent/core/pipeline.py`, `src/jev_agent/core/learning/profiles.py`,
`src/jev_agent/core/cache.py`, `src/jev_agent/code_orchestrator/ledger.py`,
`src/jev_agent/dashboard/app.py`, all on branch `design/profile-workflows`
(main `c1078a4`). No source files were modified.

Plain-English summary first: the proposal describes the pipeline as a simple
chain of stages that a user can rearrange in a flowchart editor. The pipeline
in the code is not a simple chain. That gap between the picture and the code
is where most of the structural risk lives. Everything below follows from it.

---

## The strongest structural objection

**Blocker — Long-Term Tech Debt & Maintainability**

**Tech debt description:** The proposal adds a second pipeline. The current
pipeline is not the seven-state chain in the hypothesis
(INTAKE → PLANNING → VALIDATING → approval → EXECUTING → INTEGRATING → DONE).
In `pipeline.py`, `_advance` already maps seven work states to handlers, but
the real flow also includes DESIGNING (only when `Plan.needs_design` is true),
SHIPPING (only when the ship mode says so), six `AWAITING_*` gate states driven
by `STEP_GATES` and `AutonomyMode`, and loops that a flowchart of boxes and
arrows does not naturally express: VALIDATING → PLANNING on a revise verdict
(bounded by `max_plan_rounds`), INTEGRATING → EXECUTING on a fix task (bounded
by `max_fix_rounds` and by the score gate), and NEEDS_HUMAN → resume via a
stored `resume_state`.

A node/edge editor can only do one of two things with all of that. Either it
hides it — in which case a "rearranged" flow silently drops a branch, a loop
bound, or a gate, and the run that comes out is not the flow the user drew —
or it grows controls for conditions, loop bounds, gate modes, and ship modes,
in which case it has become a small programming language with its own
interpreter, validator, versioning, and test burden. Either way, every future
change to a stage handler, a decision point (D1/D2/D3/D6), or a gate must now
be made in two places — the handler and the profile interpreter — or the two
drift apart. That is a second state machine to maintain for the life of the
project, and it is the thing most likely to rot: the default flow stays
correct because it is exercised every day, while an odd custom flow rots
until someone runs it and finds out.

**Maintainability requirement:** Before any editor is built, the design must
state, in one place, what a profile is allowed to express and what it is
not. If a profile can only order and skip a fixed set of stages, the design
must list exactly which branches, loops, and gates are outside a profile's
reach and always apply — and the interpreter must enforce that list, not the
editor alone. If a profile can express conditions and loops, the design must
treat the interpreter as a first-class pipeline component with the same test
and review bar as `_advance` itself, and must say who keeps the two in step
when a handler changes. "Rearranged flow maps to real pipeline states" is not
specific enough to build from: it does not say what happens to DESIGNING,
SHIPPING, the `AWAITING_*` gates, `plan_only`, or the fix/score loops.

---

## (a) Where profile data should live, and what breaks on delete, rename, or edit

**Blocker — Long-Term Tech Debt & Maintainability**

**Tech debt description:** The hypothesis says "per repo or per user" without
choosing, and the two choices have opposite failure modes. Repo storage
(`.jev/`, gitignored, like the CDA ledger at `<repo>/.jev/cda-ratings.jsonl`
and the state database) means a profile does not travel with the repo: a
fresh clone, a second machine, or the Mac dashboard pointed at a different
checkout cannot see it, and there is no sharing story. User-level storage
(`~/.jev/`) means a profile travels across repos, but the evidence used to
justify it — per-stage outcomes, CDA ratings, cache behaviour — is
per-repo, so a profile tuned for one repo silently carries its assumptions
into another. The hypothesis also never says whether a run points at a
profile by name or holds a copy of it. A live name reference is the worst
option: editing a profile mid-run changes what a resumed run does, deleting
it orphans in-flight and finished runs, and renaming it breaks every stored
reference — exactly the failure the learning-profile store already invites,
where the profile name is the key (`learning_profiles` table, `lessons`
keyed by `profile`) and `delete_profile` deletes the profile's lessons
with it.

**Maintainability requirement:** Three rules, all mandatory, all decided
before implementation:

1. **A run snapshots its profile, it never references it live.** At run
   creation, the fully resolved graph — stage order, per-stage settings
   (models, cache policy, rework bounds, gate settings), preset name and
   version, and a content hash — is written into the run record (or the
   run's directory, following the existing precedent: `Pipeline.start`
   already copies a server profile to `.jev/runs/<id>/profile.json` so a
   resumed run reaches the same servers). Editing, renaming, or deleting
   the definition afterwards must change nothing about a run that already
   started, and a finished run must stay auditable from its snapshot alone.
   The `resume_state` mechanism makes this non-optional: a resumed run
   restores a stored state, and that state must exist in the snapshot's
   graph, not in whatever the definition says today.
2. **Identity is a stable id, not a name.** Names are display text and may
   be renamed freely. References (run snapshots, suggestion records) carry
   the id plus the version. A name-keyed store, like the learning-profile
   store, must not be reused for this.
3. **Edit means new version; delete means archive, not erase.** Editing a
   definition creates a new version and leaves old versions readable for as
   long as any run references them. Deleting a definition must be refused
   while runs reference it, or must archive it (hidden from the picker,
   still resolvable for old runs) — never a cascading delete of the kind
   `delete_profile` performs on lessons. The design must also state a
   retention rule for old versions, or they accumulate forever in `.jev/`.
   A sensible default: a version may be garbage-collected only when no
   active (non-terminal) run references it, and finished runs keep their
   snapshot regardless.

A related split the design must make explicit: the *definition* (the graph
the user edits) and the *run snapshot* are two different objects with
different lifetimes. Conflating them — one record that is both the editable
thing and the run's record — is how an edit corrupts a running pipeline.

**Major — Long-Term Tech Debt & Maintainability**

**Tech debt description:** The flowchart editor's layout (node positions) and
the executable graph (stages and transitions) must not be the same object.
If positions are saved inside the profile definition, every drag in the
editor creates a new "version" of something that did not change, version
history becomes noise, and diffing two versions of a flow means diffing
pixel coordinates. If layout is stored separately, the design must say
where, and what happens when the graph changes but the layout does not
(a node with no position, a position with no node).

**Maintainability requirement:** Store layout separately from semantics, or
do not store layout at all (auto-layout on open). The versioned, snapshotted
object is the semantic graph only.

---

## (b) Boundaries: executor, profile interpreter, suggestion engine

**Blocker — Long-Term Tech Debt & Maintainability**

**Tech debt description:** Three components are being sketched as if they
were one feature, and if their knowledge is not fenced off, each will grow
into the others. The failure mode is already visible in the hypothesis
itself: the Efficiency preset is described as "cheaper models where history
says they are safe" — that is not a static preset, it is a preset that
reads history at run time. If the executor learns to ask the suggestion
engine which model is safe, or stage handlers learn to branch on the
profile name (`if profile == "efficiency": ...`), then every handler
carries profile knowledge, every profile change touches handler code, and
the preset can never be reasoned about without running the whole system.
The same tangle threatens on the other side: if the suggestion engine
writes profiles directly, or reads the CDA ledger JSONL and the state
database's attempt/decision tables directly, then a change to either store
breaks suggestions, and a suggestion bug can corrupt a definition.

**Maintainability requirement:** Draw and enforce these four boundaries:

1. **The executor knows stages, not profiles.** `Pipeline` and the step
   handlers (`_plan`, `_validate`, `_execute`, `_integrate`, `_ship`) take
   resolved per-stage parameters (which worker/model, which cache policy,
   how many rework rounds) as input. They never see a profile name, a
   preset name, or a suggestion. This is what makes "today's pipeline stays
   the default, unchanged" actually true: the default is just the resolved
   parameters the current code already produces.
2. **The interpreter knows graphs, not workers.** The profile interpreter
   turns a validated graph into a sequence of stage invocations and
   transitions. It knows stage ids and transition conditions from a fixed
   registry. It does not know worker implementations, model catalogs, or
   how a stage does its work. New stage, new registry entry, new tests —
   never a quiet edit to the interpreter's internals.
3. **The suggestion engine knows history, not execution.** It reads
   per-stage outcomes, costs, CDA aggregates, and cache behaviour through
   one defined read interface — not by parsing the ledger file and the
   state database ad hoc — and it produces a *draft profile plus a written
   explanation*. It never writes a definition, never starts a run, and
   never modifies a run in flight. The user's approval step is the only
   path from suggestion to definition, and it must be a real write through
   the same validation as a hand-built profile, not a shortcut.
4. **One validator, used by both editor and executor.** The editor's
   "plain-language refusal" and the executor's refusal of an invalid graph
   must be the same validation code. Two validators — one friendly one in
   the dashboard, one strict one in the pipeline — will disagree, and the
   disagreement will surface as a flow that saves fine and fails mid-run,
   which is the worst outcome for a feature whose promise is "no crash."

**Major — Long-Term Tech Debt & Maintainability**

**Tech debt description:** The safety floor is not one stage, it is
scattered code, and a node-presence check cannot enforce it. The quality
gate lives inside `_integrate` (the hard limits in `_d6_action` that turn a
ship vote into a fix task, and `_ship_after_score_gate`); the ship gate
lives in `after_approval` plus the `AWAITING_SHIP` state; human approval
lives in `execution_gate_target`, `STEP_GATES`, and `AutonomyMode`. A
profile that "drops VALIDATING" does not obviously drop D2/D3, and a
profile that keeps INTEGRATING does not obviously keep the score gate,
because the gate is not a node in `_advance` — it is logic inside the
`_integrate` handler. Checking "is the validation node present" therefore
proves nothing about whether validation actually runs.

**Maintainability requirement:** The design must define the safety floor as
a list of *enforced behaviours*, not node names — for example, "a plan is
reviewed by a different vendor before execution" and "a result below the
ship-score threshold never ships" — and must say, for each behaviour, which
code enforces it and what a profile may and may not change about it. If
some behaviours are non-removable, the validator must reject a graph that
removes them at save time, with the reason in plain language, and the
executor must re-check the snapshot at run start (a definition that was
valid under an older validator must not run under a newer safety rule
without re-validation). If instead a flow may drop a safety behaviour with
a "reduced-assurance" label, the label must be carried on the run record
and shown on the run page for the life of the run — a label that exists
only on the profile picker is forgotten the moment the run starts, which
is exactly when it matters.

**Major — Long-Term Tech Debt & Maintainability**

**Tech debt description:** "Real measurement, not estimated" collides with
the cache and the suggestion engine. Cache keys in `cache.py` already bake
in a config fingerprint, the jev version, role config, and repo
fingerprint — but not a workflow profile. If a profile changes which model
runs a stage, or how aggressively the cache is reused, and the profile is
not part of the cache key material, then a result cached under the
Efficiency preset can be served to an Accuracy run, or the reverse, and
the per-stage cost numbers the suggestion engine reports will be wrong in
exactly the cases where the user is comparing presets. The semantic-cache
shadow data has the same hazard in the other direction: shadow mode
measures agreement without serving, and a suggestion that quotes shadow
agreement as if it were live serving evidence overstates what is known.

**Maintainability requirement:** The resolved profile (id, version, hash)
and the per-stage parameters that affect output must be part of cache key
material, or the design must prove why they need not be. Suggestion text
must distinguish three things that are currently easy to blur: exact-cache
hits (real, served), semantic shadow agreement (measured, never served),
and live semantic serving (off until proven). A suggestion must never
present shadow data as a saving the user will actually get.

---

## (c) Migration: old runs, the default flow, the existing learning profiles

**Major — Long-Term Tech Debt & Maintainability**

**Tech debt description:** Three existing things must keep working, and
each has a different migration shape. Stored runs from before profiles have
no profile field; if the code reads "no profile" as "use the current
default definition," then editing the default later rewrites the behaviour
of old runs on resume — the same live-reference bug as in (a), applied to
history. The default flow itself is the riskiest migration of all: either
the default stays as hardcoded `_advance` and custom profiles go through a
new interpreter (two paths, guaranteed drift, and "default unchanged" is
true only until the first handler change lands in one path and not the
other), or the default is re-expressed as a profile definition (one path,
but then the default's correctness now depends on the interpreter, and
any interpreter bug breaks every run, not just custom ones). The existing
learning profiles are a third, separate migration: they already occupy the
name, the dashboard page (`/profiles`, `dash.profiles_page`,
`/profiles/use`), and the `learning_profile` argument on `Pipeline.start`
— and `Pipeline.start` *also* already has a `profile` argument, for server
profiles, snapshotted to the run directory. That is three meanings of
"profile" before this feature adds a fourth.

**Maintainability requirement:** Follow the precedent the codebase already
set for `AutonomyMode` (migration 14): a new nullable column/field on the
run record, where a missing value reads as the historical behaviour — here,
as a snapshot of the default flow at version 1, pinned, not as a live
reference to whatever the default says today. State the choice about the
default explicitly and accept its cost either way: if the default stays
hardcoded, the design must name the mechanism that keeps the interpreter
and `_advance` in step (shared stage registry, shared tests, or a
conformance test that runs the default profile against the hardcoded path
and fails on divergence); if the default becomes a profile, the design
must say that the interpreter is now on the critical path for every run
and gets the corresponding test coverage. Do not leave this as "the
default stays unchanged" without saying which of the two architectures
that sentence describes — they have opposite maintenance profiles.
Learning profiles need no data migration (their table, API, and run field
stay as they are), but they need a naming migration in the interface
(see (e)), and the new feature must not reuse their store, their name
pattern, or their dashboard route.

**Major — Long-Term Tech Debt & Maintainability**

**Tech debt description:** `AutonomyMode` already varies the flow per run —
STANDARD, AUTONOMOUS (skips the approval gate), ASK_EVERY_STEP (adds a gate
before each step), MANUAL, MEDIUM — and it does so through the same
`_move_to_step` / `step_gate_target` / `execution_gate_target` machinery a
profile interpreter would need to drive. A profile that also controls
gates creates two sources of truth for the same decision: does the
approval gate appear because the autonomy mode says so, or because the
profile says so, and what happens when they disagree? If the answer is
"the profile wins," autonomy mode becomes dead config for profile runs;
if "autonomy wins," the editor's gate controls lie.

**Maintainability requirement:** The design must state the precedence rule
between profile gates and autonomy mode, and preferably must remove the
overlap: either gates belong to the profile and autonomy mode is
re-expressed as profiles (a migration of its own, with its own cost), or
gates belong to autonomy mode and profiles may not touch them — in which
case the editor must not offer gate controls it cannot honour. Leaving
both in place with an undocumented precedence is how a run silently skips
a human approval, which is precisely the safety failure the floor exists
to prevent.

---

## (d) Long-term maintenance: who owns the presets, and how a stale preset fails

**Blocker — Long-Term Tech Debt & Maintainability**

**Tech debt description:** A preset is a cross-cutting bundle: it names
stages (owned by `pipeline.py`), models (owned by the worker registry and
the OpenRouter model catalog), quality levels and thresholds (owned by the
quality gate), cache policy (owned by `cache.py`), and rework bounds
(owned by `PipelineSettings`). No single existing module owns all of that,
so without an explicit owner the preset will be updated by whoever happens
to touch one of its pieces — which means, in practice, by nobody, until a
user runs a preset that names a model that no longer exists, a quality
level that was renamed, or a stage whose handler changed what it accepts.
The failure mode matters more than the staleness itself. A preset that
fails loudly at save or at run start is an annoyance. A preset that fails
silently is a safety and cost bug: the model fallback ladder quietly
substitutes a different model, aggressive cache reuse quietly serves a
result from a different configuration, and fewer rework rounds quietly
stop a run earlier — and in each case the run's cost and quality numbers,
the very numbers the suggestion engine learns from, are recorded against a
preset that did not actually run.

**Maintainability requirement:** Four rules:

1. **One owner, one registry.** Preset definitions live in one module,
   written against a stage/model/quality registry — not as free-text names
   scattered through the editor and the pipeline. Built-in presets are
   immutable shipped data; a user edit is always copy-on-write to a user
   definition, so a jev upgrade can update the built-ins without
   overwriting the user's edits, and the user's copy records which
   built-in version it forked from.
2. **A preset is versioned with the code it runs against.** Like the cache
   fingerprint, which includes the jev version so upgrades invalidate
   stale entries, a preset (and every run snapshot) records the jev
   version and the registry version it was validated against. A preset
   validated against an older registry must be re-validated before it
   runs, not trusted.
3. **Stale presets fail at the boundary, loudly, before work starts.**
   Unknown stage, unknown model, removed quality level, or a per-stage
   parameter the handler no longer accepts must be refused at save time
   and re-checked at run start, in plain language naming the stale piece
   and the fix ("this flow names model X, which is no longer available;
   pick a replacement"). A mid-run failure on a stale preset is a design
   failure, not bad luck — the information to check it existed before the
   run started.
4. **A conformance test keeps presets honest.** The test suite must assert
   that every shipped preset validates against the current stage registry,
   model catalog, and quality levels, so a handler rename or a model
   removal breaks CI instead of breaking a user's run two months later.
   Without this test, preset maintenance depends on someone remembering to
   update a file they do not own, which is the same as nobody updating it.

**Major — Long-Term Tech Debt & Maintainability**

**Tech debt description:** The Efficiency preset's "cheaper models where
history says they are safe" is a dynamic rule, not a static definition,
and dynamic rules rot differently from static ones. The history it reads —
per-stage outcomes, CDA ratings, cache behaviour — is thin per repo at
first (the hypothesis admits this), changes as models and code change, and
is aggregated with floors that are already judgement calls (the CDA
ledger names a "best tier" only after 3 graded diffs, a floor the ledger
itself calls deliberately small). A rule that was safe six months ago, on
an old model and an old codebase, is not evidence about today — but a
preset has no expiry date, so nothing forces the question to be asked
again. Worse, the rule is self-reinforcing: if cheap models are only used
where history says they are safe, and history only records the runs that
used them, the evidence base narrows to the cases that already worked.

**Maintainability requirement:** Any history-dependent choice inside a
preset must be resolved at run start and recorded in the run snapshot
(which models were actually chosen, and on what evidence), so a run is
reproducible from its record. The design must also state an evidence
freshness rule — for example, history older than some window, or from
before a model or handler change, does not count — and must say what the
preset does when the evidence is too thin to support the cheap choice:
fall back to the more expensive default, visibly, rather than silently
running cheap on no evidence. "Cheaper where history says safe" without a
thin-evidence fallback is a cost-saving rule that spends quality it has
not measured.

**Major — Long-Term Tech Debt & Maintainability**

**Tech debt description:** The CDA ledger is a weak foundation for
suggestions in one specific way: it is best-effort and append-only, with
no file locking (noted in its own history), and its aggregates deliberately
count `unavailable` records in `n` but never as quality. A suggestion
engine that reads it casually will misread absence as failure ("this agent
has many records and low quality" when most records are `unavailable`
backends) or will treat a tier with 3 lucky grades as proven. The ledger
is also per-repo, while a user-level profile is not — suggestions built
from one repo's ledger and applied to another repo's profile inherit
evidence that does not belong to them.

**Maintainability requirement:** The suggestion engine's read interface
must expose the ledger's own distinctions (graded vs unavailable vs error,
and the graded count behind every mean) and must refuse to make a
model/tier recommendation below a stated evidence floor — with that floor
written in the suggestion's explanation, not hidden. Cross-repo
suggestions must say which repo the evidence came from, or must not be
made at all.

---

## (e) The naming collision, and how to resolve it

**Major — Long-Term Tech Debt & Maintainability**

**Tech debt description:** There are already three "profiles," and the
hypothesis adds a fourth without noticing the first two:

1. **Learning profiles** — lesson databases: `core/learning/profiles.py`,
   the `learning_profiles` table, `learning_profile` on the run record,
   the dashboard's `/profiles` page and `/profiles/use` action, nav item
   "Profiles."
2. **Server profiles** — workspace/server configuration: the `profile`
   argument on `Pipeline.start`, snapshotted to
   `.jev/runs/<id>/profile.json`.
3. **(Implied by the dashboard)** design-system and settings concepts that
   already compete for the same picker language.
4. **Proposed: workflow profiles** — the new flow definitions.

Calling the new concept "profiles" in the interface guarantees user
confusion ("I picked a profile, why did my lessons change?") and developer
confusion (every future `profile` parameter, route, and variable name is
ambiguous, and grep for "profile" already returns all three meanings).
Renaming the learning profiles in code would be the clean fix and the
expensive one: it touches the table, the run field, the CLI, the dashboard
routes, and every stored database.

**Maintainability requirement:** Do not rename the learning profiles in
code — the migration cost buys nothing the user can see. Instead, rename
the *new* concept everywhere the user and future developers meet it, and
fence the old names off:

- **The new concept is called "flows," not "profiles."** Dashboard route
  `/flows` (not `/profiles`, which stays the learning profiles page),
  nav item "Flows," module and type names built on `flow` (`FlowDefinition`,
  `FlowSnapshot`, `flow_id`), and documentation that says "flow" throughout.
  The run record field is `flow_id` / `flow_snapshot`, never `profile`.
- **Learning profiles get a clarifying label in the interface only:**
  nav and page titles become "Learning profiles" (code names unchanged),
  so the two pickers are distinguishable at a glance without a data
  migration.
- **Server profiles get the same treatment:** labelled "Server profiles"
  wherever they surface, and the `profile` argument on `Pipeline.start`
  is left alone but never reused as the parameter name for flows — a new
  argument with a distinct name, so a call site cannot silently pass a
  flow where a server profile was expected.
- **A naming rule for the future:** no new feature may introduce a bare
  `profile`, `Profile`, or `/profiles` identifier; every profile-like
  concept carries its qualifier in code and in the interface. Put this in
  the design doc so the next feature does not repeat the collision.

If the project insists on the word "profile" for the new concept, the
alternative is a full rename of the learning profiles — table, field,
routes, CLI — in one migration, with old databases upgraded, not a
half-rename where the interface says one thing and the code says another.
A half-rename is worse than either consistent choice.

---

## Other structural risks worth recording

**Major — Concurrency and partial saves.** The dashboard editor saves a
graph through POST actions like the rest of the dashboard. A graph is a
multi-part object (nodes, edges, per-stage settings, gate settings). A
save that writes some parts and fails on others leaves a definition that
validates as a whole but is not what the user drew. Saves must be atomic
— validate the complete graph, then write it in one transaction — and
concurrent edits to the same definition need a rule (last-write-wins with
a version check, or an edit lock), not silent overwriting.

**Major — The dashboard route table is already large.** `app.py` groups
routes into mixins (`BoardRoutes`, `LearningRoutes`, `DesignRoutes`, …).
A flowchart editor is a large new surface (editor page, save/validate
actions, suggestion list, approval action, run picker integration). It
belongs in its own routes mixin and its own templates, following the
existing pattern, not bolted onto the learning routes — bolting it onto
`/profiles` would entrench the naming collision in the URL structure as
well as the interface.

**Nit — Measurement storage.** Per-stage token and time measurement is
"real, not estimated," but the hypothesis does not say where per-stage
numbers live. Attempts and decisions already live in the state database;
stage timings do not obviously have a home yet. If stage measurements are
added as a new table, the suggestion engine's read interface (see (b))
should read them from there; if they are derived from existing attempt
records, the design should say so, or two sources of "stage cost" will
diverge and suggestions will learn from the wrong one.

**Nit — The editor must not become the only way to see a flow.** A flow
defined only as editor state (JSON the dashboard understands) cannot be
reviewed in a PR, diffed, or inspected from the terminal. If flows are
meant to be per-repo artefacts, a readable serialisation (versioned JSON
or equivalent, with layout separated per (a)) that a person can read and
diff is worth requiring; otherwise the flow's history is opaque to
everyone except the dashboard.

---

## Open questions for the design phase

1. What exactly can a profile express — stage order only, or also
   conditions, loop bounds, gate modes, ship mode, and `plan_only`? What
   is explicitly outside a profile's reach?
2. Does the default flow stay hardcoded with a conformance test against
   the interpreter, or is it re-expressed as a profile? Who owns that
   decision?
3. What is the precedence rule when a profile's gates and `AutonomyMode`
   disagree?
4. Is profile storage per-repo, per-user, or both — and if both, which
   wins when a repo definition and a user definition share a name?
5. What is the evidence floor (graded count, freshness window) below
   which a suggestion may not recommend a cheaper model or tier, and what
   does the preset do instead?
6. Is the safety floor enforced as non-removable behaviours, or as a
   reduced-assurance label — and if a label, where does it live for the
   life of the run?
7. What is the retention rule for old profile versions, and when may a
   definition be deleted rather than archived?
8. Are per-stage token/time measurements a new store, or derived from
   existing attempt records?

---

## Verdict

The idea is structurally expensive, not structurally impossible. Its cost
is a second state machine, a second vocabulary for safety that is currently
scattered through handler code, and a third (fourth, counting server
profiles) use of the word "profile." Those costs are payable, but only if
the design phase answers the questions above before the editor is built —
above all, what a profile is allowed to express, what a run snapshots, and
what the safety floor is made of. Building the editor first and answering
those questions from its behaviour afterwards will produce flows that
save cleanly, run differently from what was drawn, and fail in ways that
are blamed on the pipeline rather than on the profile — and that failure
will be discovered by users, one run at a time, over the next two years.
