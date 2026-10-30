# Comprehension report — Stage 6: could a fresh implementer re-implement from this document alone?

Target: `docs/designs/profile-workflows/design.md` (1,498 lines, branch `design/profile-workflows`, Baton issue #368).
Method: read as a reader with no prior conversation about this feature, checking the document against the code it cites. Standard applied: the re-implementation standard — a competent reader unfamiliar with the codebase must be able to write functionally identical code on the first attempt using only the document, without guessing or making independent design choices.

## Verdict first

No. The document fails that standard in both ranges tested.

- Feature range (lines 1–995, sections 1–14): a reader can follow the intent — named profiles, an editor, two presets, suggestions with approval, a safety floor — but cannot implement the profile language, the gate rules, the snapshot, or the safety floor without guessing, because the central tables those parts need are missing and because sections 6–14 assume answers that section 16 leaves open.
- Detailed range (lines 996–1498, sections 15–19): a reader cannot follow the implementation plan without guessing either, because the phase order, the file paths, the acceptance criteria, and the rollback mechanism all change depending on option sets that are not yet picked, and several phase acceptances contradict the feature range.

This report leads with what is wrong, missing, or risky. Nothing in it resolves an ambiguity; each finding states exactly what is missing or contradictory so the gate can settle it.

Counts: **12 Blockers, 18 Majors, 7 Nits — 37 findings.**

Severity meanings used here: Blocker = the implementer cannot write the code without inventing a rule or waiting for the section 16 gate. Major = the implementer will probably write the wrong code, or two parts of the document point in different directions. Nit = a wording or consistency slip that causes confusion but does not block code.

## Blockers

### B1. No gate-policy table, and the gate rules contradict each other
Sections 7.2 (lines 452–464), 9.2 (lines 646–668), 10.1 item 5 (lines 671–692).

Section 7.2 defines three gate policies per step: `default`, `require` (park "even when the mode would not"), and `skip` (only where existing rules already allow skipping). No table maps step × autonomy mode × policy to an actual `AWAITING_*` state, and no such table can be derived from the text, because:

- `INTAKE` has no gate state at all: `STEP_GATES` in `core/pipeline.py` has no `INTAKE` entry. A gate policy on `INTAKE` has nothing to park in.
- `EXECUTING` shares its gate state with the plan gate: `STEP_GATES` maps `EXECUTING` to `AWAITING_APPROVAL`, the same state the plan gate uses after `VALIDATING`. The document never says how a reader tells a plan-gate parking from an executing-gate parking, or which step's policy governs `AWAITING_APPROVAL` at each point.
- `require` contradicts the autonomy rule. Section 7.2 says `require` parks even when the mode would not. Section 10.1 item 5 says the plan gate applies "except under `AUTONOMOUS` opt-in, which is an explicit creation-time choice outside any profile." Section 9.2 says a profile never turns an `AUTONOMOUS` run into a gated run. So: does `require` on `VALIDATING` reimpose a plan gate on an `AUTONOMOUS` run, or not? Both answers are stated.
- `skip` is defined only negatively ("never overrides an explicit human gate the user asked for elsewhere, and never skips the ship gate when `[ship] after_approval` is `ask`"). The ship gate is `AWAITING_SHIP`, which is not a `STEP_GATES` entry for the `SHIPPING` step (that entry is `AWAITING_SHIPPING`). The document never says whether the `SHIPPING` step's gate policy governs `AWAITING_SHIPPING`, `AWAITING_SHIP`, or both.

An implementer must invent the gate table. That table is the safety-critical part of the feature.

### B2. Snapshot resolution is stated two incompatible ways
Sections 6.4 (lines 397–409), 8.1 (lines 553–584), 9.2 (lines 646–668).

- Section 6.4: at run creation the profile version "is resolved to its full parameter set" and "that resolved set is stored on the run record." The executor "reads only the snapshot."
- Section 8.1: round caps, quality level, score settings, ship mode, cache mode, and role model overrides "are read from the snapshot, falling back to the repo configuration for any parameter the profile does not set."
- Section 9.2: the precedence order is per-run flags, then snapshot, then repo `jev.toml` and workspace profile, then defaults, "applied in one place at run creation, and the run record names the winning source per setting."

These cannot all be true as written. If the snapshot is already the full resolved set, there is nothing left to fall back to, and the precedence order must have been applied before the snapshot was stored — in which case the snapshot is post-flag, post-config values, not the profile version. If instead the snapshot stores only what the profile sets and the executor falls back at run time, then the snapshot is partial, per-run flags (which beat the snapshot) are not in the snapshot, and the "winning source per setting" record is a separate structure that appears in no schema in the document (section 6.4 lists snapshot contents as stage list, per-stage parameters, gate policies, setting overrides, assurance computation — no per-setting source field; phase 1, lines 1020–1040, lists run-record fields `profile_id`, `profile_version`, `goal`, `assurance`, `assurance_absent` — again no per-setting source field).

The implementer must guess: what exactly is stored, when precedence is applied, and where the winning source is recorded.

### B3. The executed stage list contradicts the snapshot stage list, and "ship mode" does not match the code
Sections 7.1 (lines 433–451), 7.3 (lines 465–492), 8.1 (lines 553–584).

- Section 8.1: "a step not in the snapshot is never entered."
- Section 7.3: a profile that omits `DESIGNING` "still runs it when the run has UI work only if the profile's `designing` parameter says `when_needed` (the default)."

So an omitted stage is entered after all, when a parameter says so. The document never states whether `DESIGNING` under `when_needed` is present in the snapshot stage list or absent from it with a side parameter — and that choice changes the validator, the editor rendering, the stage-record rows, and the conformance comparison of stage sequences.

- Section 7.3: "`SHIPPING` may be omitted only by choosing ship mode `off`."
- Section 7.4: "Ship mode and `after_approval`, within the existing values."

The code has no single "ship mode" with values `off`/`merge`/`ask`. It has `[ship] enabled` (a boolean, off by default), `[ship] after_approval` (`merge` or `ask`), and a separate per-run `ship_mode` flag (`--auto-ship` / `--ask-before-ship`, else `after_approval`). The document never maps its three ship-mode words onto those three mechanisms. An implementer cannot write the `SHIPPING`-omission rule, the section 7.5 incoherence message ("SHIPPING is omitted but ship mode is merge"), or the section 10.1 item 4 rule ("merge, ask, or off") from this document alone.

### B4. The safety floor cannot be implemented until option set B is picked, and the document contradicts itself in the meantime
Sections 7.3 (lines 465–492), 7.4 (lines 493–520), 10.2 (lines 693–715), 10.3 (lines 716–734), 16B (lines 1176–1205).

- Section 7.3 forbids omitting five stages: `INTAKE`, `PLANNING`, `VALIDATING`, `EXECUTING`, `INTEGRATING`. Section 10.2 lists refusals for only three omissions (`VALIDATING`, `EXECUTING`, `INTEGRATING`). The status of `INTAKE` and `PLANNING` under the floor is therefore unclear: refused by the profile language, but not listed as a floor refusal.
- Section 7.4 says quality level `none` is allowed "only when shipping is off." Under option B3 (label everything, no refusal for safety reasons), that sentence is false. Section 7.3 itself flags this: "If option set B chooses a label-only floor, this rule is the first thing re-audit revisits."
- Section 4 item 3 (lines 246–308) says the label "blocks shipping when the enforcement option says so." Section 10.3 says "a reduced run is not blocked from shipping by the label alone (unless option set B chooses refusal for that behaviour class)." Under B1, labelled classes are by definition not refused, so for those classes the label never blocks anything. The document never defines what a `reduced` label blocks at runtime, if anything, beyond being displayed: can a reduced run merge? Can it open a PR? Does anything check the label at ship time, or is the label display-only (section 14 says a browser-side label "is never trusted for shipping decisions," but never says what the server-side label gates)? Until B is picked and the runtime effect is defined, "reduced assurance" is a display string, not a safety property, and the implementer cannot write the ship path.

### B5. Sections 6–15 assume answers to all eight section 16 option sets
Section 16 (lines 1138–1379) and its dependent sections.

The document is honest that sections 6–15 are "written for the design's proposed answers" and that a different pick means re-audit. For a comprehension test, that means the following parts are unimplementable until the gate picks, not merely polishable afterwards:

- A (naming/route): sections 6.1–6.2 (ids `<scope>:<slug>`, directories `<repo>/.jev/workflow-profiles/`, `~/.jev/workflow-profiles/`), 13.1 (routes `/workflows`, `/workflows/new`, `/workflows/{id}/suggest`), 15 phases 2 and 4 (package `workflow_profiles`, `routes_workflows.py`, `templates/workflows/`). Under A2 all of these change, including the id format (`flow:<slug>`), and the error messages in section 7.5, which already say "flow" while the working term is "workflow profile" (see Nit N1).
- B (enforcement): see B4. Affects sections 7.3, 7.4, 10.2, 10.3, and acceptance criterion 5 in section 17.
- C (phasing): section 15 is C1. Under C2 the phase boundaries and per-phase acceptances do not exist; under C3 phases 4 and 6 and the editor/suggestion acceptance criteria do not exist. Section 17 acceptance requires the editor and suggestions unconditionally, so it is false under C3 as written.
- D (editor form): section 13.2 (canvas plus list) and section 17 acceptance ("in both editor modes") are void under D2 (list only) or D3 (canvas only). Under D3 the document's own browser-first argument (section 4 item 8) is violated by the approved design, and nothing in section 17 is conditioned on that.
- E (scope): sections 6.1, 6.2, 6.5 (both scopes, cross-scope re-validation) are void under E2 (repo only) or E3 (user only). Section 12.1 cross-repo suggestions ("user-scope suggestions may aggregate across the user's repos") assume E1; under E2 there is no user scope to aggregate across.
- F (precedence): sections 8.1 and 9.2. Under F2 (profile always wins) a typed per-run flag is silently ignored; under F3 (configuration always wins) a preset's documented behaviour can be silently overridden. The document says the gate-behaviour paragraph "is re-audited" if F changes — which is the core of section 8.1.
- G (default representation): sections 8.2, 15 phase 3, and section 17 acceptance ("runs without a profile produce identical stage sequences"). Under G2 every run changes code paths at once, and the "legacy path untouched" guarantee in sections 3, 8.2, and 15 disappears. Under G1 the document never states the trigger that would ever allow the default through the profile path (see B9).
- H (what Accuracy optimizes): section 9.1's Accuracy column (production quality, CDA on, full list) assumes H1. Under H2 (wall-clock speed) nearly every row is wrong, and the profile language in section 7 has no parallelism parameter at all, so H2 cannot be expressed as a preset without a new feature the document does not specify. Under H3 the tie-break rule ("quality first, speed second") appears nowhere in the preset table.

A fresh implementer reading sections 6–15 first, as the document order invites, will implement the proposed answers and then discover at section 16 that every one of them was provisional.

### B6. No schemas: no profile format, no snapshot format, no suggestion-record format
Sections 6.2 (lines 361–378), 6.4 (lines 397–409), 12.4 (lines 876–891), 14 (lines 972–995).

- Section 6.2 says one JSON file per profile version plus a `current.json` pointer. No field list, no field names, no types, no required-vs-optional marking, no example file. Section 7.4 lists parameters in prose ("`designing`: `when_needed` or `never`", round caps, quality level, role model overrides) but never gives the JSON keys, so two implementers will produce two formats and the section 7.5 validator has no schema to validate against.
- The snapshot format is likewise prose only ("stage list, per-stage parameters, gate policies, setting overrides, assurance computation"). Phase 1 names run-record columns but does not reconcile them with the snapshot: is the snapshot one JSON column, or spread across columns? Is `assurance_absent` part of the snapshot or a separate column?
- Section 12.4 says a draft "is stored as a suggestion record, not as an active profile version." No storage location (file? table?), no id, no fields, no link back to the profile it was suggested from, no retention rule. Section 13.3 says discarded suggestions "stay visible in the profile's suggestion history" — visible from what store, for how long?
- Section 14 adds a 64 KB size cap per version file and a rule that unknown fields refuse rather than being dropped — both unenforceable without a schema defining the known fields.

The validator, the store, and the suggestion lifecycle cannot be written from this document.

### B7. The stage-record write path is underspecified at every decision point
Sections 11.1–11.3 (lines 735–805).

- Wrapper placement: "a wrapper around each `_advance` step call writes the row; the wrapper is the only writer." `_advance` loops until the run reaches a state with no handler, and handlers move the run themselves (section 8.1). The document never says whether the wrapper wraps one loop iteration, one handler call, or one state visit — and retries, handler-chosen loops (plan revise rounds, fix tasks, score rework), and resume-after-halt each produce different row counts under each reading.
- Attempt numbering: "a retried stage writes one row per attempt, linked by attempt number." Attempt numbers across a `Halt` and resume are undefined: does a resumed stage restart at attempt 1 or continue? Does a `NEEDS_HUMAN` halt close the row (with `ended_at` and outcome `halted`) or leave it open? What closes a row when the run is cancelled mid-stage?
- Gate attribution: gate time is measured by "the transition into an `AWAITING_*` state." That fails for the two shared/confused gates: `AWAITING_APPROVAL` serves both the plan gate and the `EXECUTING` gate (see B1), and `AWAITING_SHIP` is a ship decision, not a step gate — which stage row does ship-decision waiting time belong to, `INTEGRATING` (where the D6 vote happens) or `SHIPPING`? The document also says the gate clock stops at "the approval (or timeout, where one exists)" without stating whether any gate timeout exists.
- Score-gate attribution: the ship score gate runs in `_ship_after_score_gate`, after `INTEGRATING` and before `SHIPPING`. No stage row is assigned its time or tokens.
- Tagging mechanism: "Jev decisions and worker attempts made during a stage carry the stage name." No field name, no storage location, no rule for decisions that span stages or for attempts recorded before the wrapper exists. "Carry the stage name" is not an implementable instruction.
- Completeness flag: section 11.3 introduces `measured: partial` vs `measured: full` but section 11.1's schema list has no completeness field. It is unclear whether completeness is a column, a computed value, or a display rule — and section 11.3's aggregation rule for medians over partial rows is unstated (see Major M18).

### B8. The Efficiency "cheapest model" rule and the Accuracy preset have no implementable definition
Sections 9.1 (lines 611–645), 16H (lines 1349–1379).

- Efficiency "Role models: cheapest configured model per role where the repo has one configured." No price source is named. Section 7.4 says "a profile never invents a model, a provider, or a price," and section 12.1 says the engine "never reads model prices it was not configured with." `[dispatcher.cost]` holds leg prices (native/OpenRouter per-million rates and a worker price guess), not per-role model prices, and role configurations are model lists, not price lists. "Cheapest" is therefore undefined: cheapest by what number, read from where, and what happens when a role has only one configured model (Efficiency equals default for that role — the table does not say so, and does not say whether that is shown to the user).
- Efficiency planner policy is unconditionally `free_training_ok`. Under precedence (section 9.2, snapshot beats repo config), this overrides a repo configured as `paid_only` — including the training-consent meaning of `free_training_ok` (free providers may train on prompts). The document never addresses whether a preset may narrow a privacy-relevant repo setting, while the Accuracy row carefully says "never narrowed by the preset." The asymmetry is unexplained.
- Accuracy's table assumes H1 (see B5). Under H2 there is no way to express the preset at all in the section 7 language.

### B9. The default's migration trigger is undefined, and phase 3 and phase 5 acceptances pull in opposite directions
Sections 8.2 (lines 585–599), 15 phases 3 and 5 (lines 1065–1118).

- Section 8.2: picking `default` is a no-op that runs the legacy path "until" the conformance suite passes. Section 15 phase 3: the conformance suite "must pass before any profiled run beyond tests is allowed." Section 15 phase 5 acceptance: "both presets run end to end on the Mac dashboard." Presets are profiled runs. So: can Efficiency and Accuracy run end to end before the default conformance suite passes, or does phase 3 gate phase 5? The document does not say, and the two readings give different phase orders.
- Under G1, even after the suite passes, "the legacy path remains available." The document never states what, if anything, ever causes the default to actually execute through the profile path — a human decision, a release flag, a config value. The conformance suite is described as both a migration gate and a permanent guard, with no trigger distinguishing the two.

### B10. Assurance is computed at run creation but shown before the run starts
Sections 10.3 (lines 716–734), 13.1 (lines 907–932).

- Section 10.3: "Assurance is computed at run creation from the snapshot."
- Section 13.1: run creation shows "the assurance of the picked version shown before the run starts."

Either assurance is pre-computable from the profile version alone (in which case section 10.3's "at run creation from the snapshot" is the wrong timing, and the pre-computation rule — which inputs, including per-run flags and repo config that beat or lose to the snapshot under section 9.2 — is missing), or it is only computable at creation (in which case the picker cannot show it beforehand). The document does not resolve this, and the same timing problem applies to the picker itself: section 13.1 says `default` is pre-selected, but section 8.2 says picking `default` runs the legacy path — so is the picker showing a preset or a legacy-path marker? A reader cannot tell what the picker selects.

### B11. "One shared validator" is stated, never specified
Sections 7.5 (lines 521–550), 13.2 (lines 933–959), 15 phases 2–3 (lines 1041–1084).

- What is shared: a function? a module? a service? What does it take as input — a profile file, a draft profile, a resolved snapshot, or all three? The editor validates before saving (draft input); the executor validates "the snapshot before the first step" (resolved input). A validator that accepts a draft profile file cannot be the same function that accepts a resolved snapshot unless the two formats and their validation rules are defined — they are not (see B6).
- Which checks run live while editing ("where cheap") versus only at save is left to the implementer ("live while editing where cheap (unknown model names, duplicate stages)"). The catalogue has nine entries; the document names two as live examples and leaves the rest unassigned. "Where cheap" is a guess, not a contract.
- Phase 2 creates the validator; phase 3 "changes" it to the full catalogue. The document never says what the phase 2 validator validates (a subset? the schema only?) or what "change" means for code already calling it.

### B12. Suggestion thresholds use metrics that are never defined
Sections 12.1–12.3 (lines 806–875), 12.2 examples (lines 833–854).

- "A stage-drop recommendation requires at least 10 runs in which the stage ran, with a measured-median saving and a defect-escape count stated alongside it." Defect-escape count is never defined: escapes from what, counted from which rows (stage rows? lessons? quality-gate failures? shipped defects?), over what window?
- "A cheaper-model or cheaper-tier recommendation requires ... no quality-gate failure attributable to that model/tier in the set." Attribution is never defined: a quality-gate failure is caused by code, not by a model in any direct sense the document explains. How does the engine attribute a gate failure to a model or a CDA tier?
- "At least 10 completed runs ... within a 90-day freshness window." "Completed" is never defined (only `DONE`? or also stopped-for-human and failed runs, which section 12.1 lists as run outcomes?). The freshness anchor is never defined (run creation? run end? stage row timestamp?).
- The droppable set is never enumerated. Only `DESIGNING` and `SHIPPING` are omittable under section 7.3; `VALIDATING`, `EXECUTING`, `INTEGRATING` are non-omittable. Yet the threshold text discusses stage-drop recommendations in general, and section 12.2's examples discuss keeping `VALIDATING` and not shortening `INTEGRATING` — "shortening" (reducing rounds) and "dropping" (removing a stage) are used as if interchangeable, but they are different operations with different thresholds, and no threshold is given for shortening rounds.
- "A stage that caught a defect in any of the last 10 runs may not be recommended for dropping on cost grounds alone." For `DESIGNING` (the main droppable stage), "caught a defect" has no meaning in the document. The rule reads as if written for `VALIDATING`/`INTEGRATING`, which cannot be dropped at all.

## Majors

### M1. Core terms are used before they are defined, or defined inconsistently
Sections 1–5 (lines 1–344), 7 (lines 423–550), 10 (lines 669–734).

- "Profile" vs "flow": the header (lines 1–12) says the working term is "workflow profile" and warns the term collides with learning profiles and workspace/server profiles. Section 5 defines "Workflow profile," "Flow version," and "Flow snapshot" — then the rest of the document uses bare "flow" for all three (error messages say "flow `<id>` version `<n>`"; section 6.4 says "the run executes the snapshot"; section 13 says "flows"). A reader cannot tell, in any given sentence, whether "flow" means the profile, the version, or the snapshot.
- "Stage" vs "step": section 7.1 calls the palette entries "stages" and "work states"; section 7.2 calls them "steps" with gate policies; section 8.1 calls it "step lookup"; section 11 calls them "stages" again. The document never states stage = step = work state, and section 7.2's "step's `AWAITING_*` gate" mapping (see B1) depends on that equivalence being exact.
- "Gate": used for five different things — the plan gate, the ship gate, the per-step `STEP_GATES` gates, the quality gate, and the ship score gate — with no glossary entry distinguishing them. Section 7.2's gate policies apply to "steps," but the quality gate and score gate are not steps and have no gate policy; section 10's floor treats them as "behaviours," a sixth word for the same area.
- "Assurance": defined in section 5 as `full` when every section 10 behaviour is present and `reduced` when any is absent "under the label option" — so the definition itself is conditional on option B, and under B2 (refuse everything) `reduced` can never occur, while under B1 it occurs only for labelled classes. The definition is not stable across the gate.
- "Performance": section 4 item 10 correctly notes the word is used for both speed and quality, and option H exists to pin down Accuracy. But the document itself never defines "efficiency" precisely either: section 9.1's Efficiency promises "fewer tokens, shorter wall time where history supports it," while its table only reduces rounds, models, and quality level — no mechanism connects those reductions to wall time, and gate-wait time (often the dominant wall-time component) is untouched by the preset.
- "Workspace profile" and "server profiles": mentioned in the header and in section 9.2 ("repo `jev.toml` and workspace profile settings") but never defined anywhere in the document. The code's `[workspace]` section is about git hooks (`run_hooks`), not profiles. A reader cannot implement precedence level 3 without knowing whether "workspace profile" is a second source, a synonym for `jev.toml`, or something else — and if it is a second source, its order relative to `jev.toml` is unstated.

### M2. Autonomy modes are incomplete: MANUAL and MEDIUM are missing
Sections 2.3 (lines 116–140), 9.1 (lines 611–645), 17 test matrix (lines 1380–1436).

Section 2.3 lists three autonomy modes: `standard`, `autonomous`, `ask_every_step`. The code (`core/models.py`, `AutonomyMode`) has five: those three plus `manual` (run waits in `INTAKE` for an explicit instruction) and `medium` (standard flow scoped to a folder). `AUTONOMOUS` also requires an explicit opt-in flag (`autonomous_opt_in`) in code, which the document never mentions. Consequences:

- Gate policies, profiles, and the conformance matrix say nothing about `MANUAL` or `MEDIUM` runs. The section 17 conformance list covers `AUTONOMOUS` and `ASK_EVERY_STEP` only. A `MANUAL` run that picks a profile: does the profile apply when the run is later advanced? A `MEDIUM` run: does the folder scope interact with profile scope (section 6.5)? Unknown.
- The `AUTONOMOUS` opt-in flag's interaction with gate policy `require` is undefined (see B1): is the opt-in a per-run flag that beats the snapshot under section 9.2, or a mode property the profile cannot touch?

### M3. Round-cap ceilings contradict precedence
Sections 7.3 (lines 465–492), 9.2 (lines 646–668).

Section 7.3: round caps are "bounded above by the corresponding `[budget]`/`[integration]`/`[quality]` ceiling when one is configured, and never below 1 where the step needs at least one attempt." Section 9.2: the snapshot beats repo `jev.toml`. If a profile sets `max_plan_rounds` above the repo's `[budget] max_plan_rounds`, which rule wins — the ceiling or the snapshot? The document states both without reconciling them. A second circularity: `max_task_attempts` is itself a `[budget]` setting, so "bounded above by the corresponding ceiling" for `max_task_attempts` is the setting bounding itself. The mapping from each profile cap to its ceiling setting is also never tabulated (which ceiling bounds `score_max_retries` — `[quality] score_max_retries` itself?).

### M4. "The validator's accepted set is exactly the set the executor runs" is false as written
Sections 4 item 1 (lines 246–308), 7.1 (lines 433–451), 8.1 (lines 553–584).

Section 4 claims the validator's accepted set is exactly the set the executor runs. But section 8.1 correctly states that handlers own their outcomes: D3 verdicts (approve/revise/stop), D6 votes (ship/fix/stop), score-gate rework, and `Halt` conditions are handler behaviour, not profile content, and "a profile that tried to express 'after a failed quality gate, skip to SHIPPING' has no language for it." The executed flow therefore includes branches the profile language cannot express and the validator cannot see. The accepted set (stage lists) is not the executed set (stage lists plus handler branches). A reader implementing "exactly the set the executor runs" from section 4 will either over-promise the validator or under-specify the executor.

### M5. The learning-profile mechanism is underspecified
Sections 2.4 (lines 141–157), 7.4 (lines 493–520), 9.1 (lines 611–645).

- Section 7.4: a profile carries a "learning profile name (optional): the learning profile whose lessons the run reads and to which its outcome is distilled." Section 9.1: presets use the "run's existing setting." The document never states what the run's existing setting is or where it comes from. In code it is `[learning] profile` (default `"default"`) plus a separate `[learning] enabled` flag (off by default) — neither is cited in section 2.3's knob list.
- Interaction with the enabled flag is undefined: does a profile naming a learning profile also enable learning for that run, or does learning stay off unless `[learning] enabled` is set? Does naming an unknown learning profile refuse (section 7.4 says yes) even when learning is off?
- Section 2.4 says lessons are the suggestion engine's "struggle/worked signal" and carry no token or time data — but section 12.1 lists lessons as an input alongside stage rows without saying how a lesson (a text distillation with a role and a retirement state) is converted into a comparable signal for a suggestion decision.

### M6. Semantic cache: the document contradicts itself and the code
Sections 2.5 (lines 158–177), 7.4 (lines 493–520).

- Section 2.5 says semantic similarity "exists behind `[cache.semantic] enabled`, which defaults to off," and shadow mode measures agreement "without serving anything."
- Section 7.4 says "there is no semantic serving mode to select, because serving does not exist."
- The code (`core/cache.py`) contains a semantic serve path that serves when semantic mode is explicitly enabled ("only when explicitly enabled — by live semantic serving").

A reader comparing document to code finds three stories: serving exists behind a flag; serving does not exist; serving exists only when enabled. The practical question for implementation is unanswered either way: if repo config enables semantic serving, does a profile's cache mode (`exact`/`exact_off`) override it, leave it, or refuse? Section 9.2 precedence would suggest the snapshot wins, but section 7.4 allows only two cache values, neither of which names the semantic flag. The interaction is unspecified.

### M7. Suggestion approval storage and lifecycle are unspecified
Sections 12.4 (lines 876–891), 13.1–13.3 (lines 907–971).

Beyond B6 (no schema), the lifecycle rules are incomplete: approval "is saved as a new profile version, like any other edit" — does approval create version n+1 even when the draft is identical to the current version? What provenance field records "suggested" (section 13.3 mentions a "suggested" provenance note, but no field is defined in section 6)? Discarded suggestions "stay visible" and the same suggestion "is not re-offered without new evidence" — what counts as new evidence (one more run? crossing a threshold? a new lesson?), and who enforces the re-offer rule? An implementer must invent all of it.

### M8. The editor specification contains a no-op interaction and an undefined validation split
Sections 13.2 (lines 933–959), 17 test matrix (lines 1380–1436).

- Canvas mode: the user "reorders only within what the validator accepts (in practice: no reordering passes, and the editor says why when one is attempted)." A drag-to-reorder interaction that can never succeed is specified as a feature. The document never says whether the canvas should prevent the drag, allow it and then error, or omit reorder affordances entirely — three different implementations with different accessibility and testing consequences.
- Validation timing: full catalogue at save, "live while editing where cheap," and again at run start. The live-vs-save split is undefined beyond two examples (see B11). The run-start failure path is also unclear: section 13.2 says a run-start failure "offers the profile picker again" — but if run creation already stored the run record and snapshot, what state is that run in? Is it deleted, left in a failed state, or never created (in which case "run start" means "creation-time validation," and the re-check is not a separate step at all)?

### M9. Creation paths and per-run flags are incompletely specified
Sections 2.7 (lines 193–206), 9.2 (lines 646–668), 15 phase 2 (lines 1041–1064), 3 (lines 207–245).

- Section 9.2 lists per-run flags as "`--quality`, `--plan-only`, `--auto-ship`, and their dashboard equivalents." The code also has `--ask-before-ship`, which is omitted. `plan_only` ("stop the run after the plan is validated") directly conflicts with the non-omittable stage rule: a plan-only run with a profile never reaches `EXECUTING`/`INTEGRATING`, which section 7.3 says cannot be omitted. Does the flag beat the profile (per section 9.2, yes) and thereby produce a run whose executed stage list violates the profile's own validator? The document never addresses flag-vs-profile conflicts beyond the precedence sentence.
- Phase 2 requires the same resolver in "dashboard `/runs/new`, CLI run creation, queue add, recurring queue items, chat-started runs." Section 2.7 introduces only `/runs/new` and `/queue/add`; recurring queue items and chat-started runs (`/chats/{id}/queue` exists in code) are never explained as creation paths, and their picker behaviour (does a queued or chat-started run show a profile picker? what is the default when no picker is shown?) is undefined.
- Section 3 says CLI `jev workflow` commands "are not part of this design's acceptance," but phase 2 includes a CLI change (list, show, validate). The document never says whether the CLI change is required for phase 2 acceptance or merely permitted alongside it.

### M10. Scope re-validation and retention are incomplete
Sections 6.3 (lines 379–396), 6.5 (lines 410–422).

- Cross-scope re-validation (section 6.5) names three checks: models exist in the target repo's role configuration, quality levels parse, cache modes are known. It omits planner policy, ship settings, autonomy interaction, learning profile existence, and CDA availability — all of which are profile parameters under section 7.4. An implementer must guess whether the list is exhaustive or illustrative.
- Retention: "Retention of archived versions follows the repo's existing run-retention behaviour; the design does not invent a second retention policy." No run-retention behaviour is cited in section 2, and none is identified by name or location. If no such behaviour exists, retention is undefined. The ordering question is also unanswered: versions are "retained while any run references them," but if run retention deletes the referencing run, does the version then become deletable, and who performs that cleanup?
- Section 6.1 says a run referencing an id that "no longer resolves" refuses with "flow `<id>` version `<n>` is archived or deleted." Section 6.3 says archived profiles' "existing snapshots keep running" and phase 2 acceptance says "a run created from an archived profile version still runs." "Archived" therefore means two different things in adjacent sections: a state that refuses new runs but permits existing snapshots — the document never distinguishes "archived profile" (hidden from creation lists) from "archived version" (a version string in an error message), and the error message as written would mislead a user whose snapshot is still valid.

### M11. Preset assurance values are never stated
Sections 9.1 (lines 611–645), 10.3 (lines 716–734).

Section 10.3 defines assurance for runs, but section 9.1 never states the assurance of either preset. Efficiency uses quality `basic` and fewer rounds: section 10.1 item 1 says the level "may vary," which suggests `basic` is still `full` — but then nothing distinguishes Efficiency's assurance from the default's, and the list page's "assurance note where applicable" (section 13.1) has no rule for when a note applies to a preset. Accuracy uses production quality and CDA on: also presumably `full`, but unstated. If either preset is `reduced` under some reading of the floor (for example, fewer fix rounds arguably weakening floor item 2's "failed integration tests never ship" only if rounds run out — the document never says whether exhausting rounds counts as dropping the behaviour), the implementer cannot compute the badge the picker is required to show before the run starts (see B10).

### M12. Phase ordering and the two conformance suites are unclear
Sections 15 phases 0–3 (lines 996–1084), 11.1 (lines 744–768).

- Phase 1 adds stage records whose schema (section 11.1) includes `profile_id`, `profile_version`, and `goal` — but profiles do not exist until phase 2. The document says legacy runs read back with `profile_id` null, but never states the nullability of the other stage-row fields for legacy runs, or whether phase 1 rows are written for runs without profiles at all.
- Phase 0 creates a "conformance suite skeleton" that "documents today's behaviour"; phase 3 creates the conformance suite that compares legacy vs `default` preset paths. The document never says whether these are one suite grown in two phases or two suites, what the skeleton contains (cases? recorded outputs? both?), or what phase 0 acceptance requires beyond "documents today's behaviour."
- Phase 2 acceptance ("snapshots survive edits and deletes") overlaps phase 3's snapshot-related acceptance and section 17's snapshot tests without assigning each test to a phase.

### M13. Rollback is specified as a flag that is never defined
Section 17 rollback (lines 1380–1436).

"Rollback is disabling profile selection at run creation (a configuration flag, default on)." No flag name, no location (`jev.toml` section? environment variable? dashboard setting?), and "default on" is ambiguous: does "on" mean selection is enabled by default, or the flag (disabling) is on by default? The rollback also assumes snapshots are "self-contained and do not need the editor or the store to keep running," which sits uneasily with section 6.2 ("the store validates every file on read") and section 6.3 ("a deleted profile's snapshots still resolve") — resolve from what, if the store is bypassed? If the snapshot on the run record is truly self-contained, the word "resolve" is wrong; if it is not, rollback needs a store read path that the rollback paragraph does not describe.

### M14. Acceptance criteria are not conditioned on the gate picks
Section 17 (lines 1380–1436), section 16 (lines 1138–1379).

Section 17 lists acceptance criteria unconditionally: browser creation "in both editor modes," both presets behaving per table, suggestions approvable and runnable, reduced-label rendering. Under C3 (no editor, no suggestions) the first and fourth criteria are false by design; under D2/D3 the "both editor modes" criterion is false; under B2 the "refused or labelled" criterion collapses to refused-only; under H2 the Accuracy criterion (production quality, CDA on) describes the wrong preset. Acceptance that is false under approved options is not acceptance — it is a second, conflicting specification of the feature.

### M15. Floor items 5 and 6 rely on mechanisms the document never defines
Sections 10.1 (lines 671–692), 2.3 (lines 116–140).

- Item 5: the plan passes D3 validation "by a validator (a different vendor than the planner when the repo configures it)." No knob for validator vendor appears in section 2.3's knob list, and no configuration key is named. An implementer cannot check "different vendor" without knowing where the vendor choice lives.
- Item 6: "every shipped result has a branch, a test record, and a quality record the run page can show." No format, field list, or storage location is given for any of the three records, and the existing `[ship] require_quality_pass` setting (refuse runs whose latest quality gate blocked) and `[ship] enabled` flag are never mentioned in the floor discussion, although both bear directly on what "ships" means.

### M16. Goal-label semantics are incomplete
Sections 5 (lines 309–344), 9.1 (lines 611–645), 12.1 (lines 808–832).

- A run without a stated goal records `null`, and "the suggestion engine treats goal-blind history as weaker evidence." Weaker by how much — excluded, down-weighted, or flagged? No rule is given, so the evidence computation in section 12 cannot be implemented.
- Preset tables set the goal label (`efficiency`/`accuracy`). If a user picks the Efficiency preset but states an accuracy goal (or vice versa), which label wins — the preset's or the user's? The document says the goal label "changes nothing about execution by itself" but is "the suggestion engine's training signal," so a wrong label corrupts future suggestions. The conflict rule is missing.
- Section 12.1 says the engine "never uses a run whose goal label is `null` as evidence for a goal-conditioned recommendation," which contradicts "treats goal-blind history as weaker evidence" unless "weaker" means "excluded for goal-conditioned recommendations but usable elsewhere" — that distinction is never drawn.

### M17. D1–D8 are used but never defined
Sections 3 (lines 207–245), 8.1 (lines 553–584).

The document refers to "Jev's decisions (D1, D2, D3, D4, D5, D6)" and names D3 (plan verdict), D4 (routing), D5 (feedback, in section 8's context of rework briefs only by implication), and D6 (ship vote) in passing. It never defines the decision points, their order, or what D1, D2, or D5 decide. The code defines eight decision points (D1–D8). A fresh reader cannot map a decision to a stage, cannot tell which decisions a profile constrains, and cannot verify the non-goal "no authority over Jev's decisions" without that mapping.

### M18. Aggregation over partial rows and the worked examples do not line up
Sections 11.3 (lines 786–805), 12.2 (lines 833–854).

Section 11.3: a stage with any `null` attempt "is never quoted as an exact token number — it is quoted as 'at least N tokens' or not quoted." Section 12.2's example reason line quotes "a median 8,100 tokens over 12 runs (measured: full)." The document never says how a median over 12 runs is computed when some of those runs are partial: are partial runs excluded (shrinking the sample below the stated 12?), included as at-least values (making the median an at-least, not an exact?), or treated as full when their `null` attempts are few? The example asserts a clean case without giving the rule that produces it. The same gap applies to "median 12,400 tokens over 14 runs" in section 11.3: median over what — full rows only, or all rows?

## Nits

### N1. Error messages already use the undecided term
Header (lines 1–12), sections 6.1, 7.5 (lines 347–360, 521–550).

The working term is "workflow profile," but section 6.1's refusal message and section 7.5's catalogue messages say "flow `<id>`." Under A1 ("workflow profile" kept) those messages use the wrong word; under A2 they are correct but every other occurrence of "workflow profile" is wrong. The messages should not be frozen in section 7.5 before option A is picked.

### N2. "Never narrowed" confuses absence with a rule
Section 9.1 (lines 611–645).

Accuracy rows say "Repo configuration (never narrowed by the preset)" for planner policy and role models. Under section 9.2 precedence, a preset that leaves a value unset does not narrow anything — it simply does not set it. "Never narrowed" reads as a protective rule (the preset must not narrow) when it means "the preset does not set this value." The Efficiency rows, which do set values, have no corresponding statement about whether they may narrow a repo setting (see B8).

### N3. Two display terms for the same label
Sections 10.3 (lines 716–734), 13.1 (lines 907–932).

Section 10.3 specifies a badge ("Assurance: reduced — ..."). Section 13.1's list page shows an "assurance note where applicable." Badge, note, and the section 5 phrase "assurance is displayed, never just stored" are three wordings for one display requirement, with no statement of whether list page, run page, board row, and PR body show the same text.

### N4. The SITEMAP coordination note names no owner or step
Section 19 (lines 1469–1498).

The note correctly identifies that two parallel design branches add `docs/designs/SITEMAP.md` and that the merge "must be merged by hand." It does not say who merges, when (before implementation? at merge time?), or what the merged file must contain. As a design dependency it is a reminder, not a plan.

### N5. File-path sweep for option A is not listed
Sections 15 phases 2, 4, 5 (lines 1041–1118).

Phase 2 notes "module names follow the option-set-A name if it differs," but phases 4 and 5 paths (`templates/workflows/`, `data/workflow-presets/`, `routes_workflows.py`) carry no such note, and no sweep list (code paths, route paths, template directories, data directories, error strings, documentation) is given. A rename applied to the package but not to the routes or messages reproduces the A2 failure mode ("applied inconsistently") that section 16 itself warns about.

### N6. "Lease behaviour" is invoked without introduction
Section 8.1 (lines 553–584).

"Halt, resume, cancel, and lease behaviour are unchanged." Leases are never introduced in section 2 (a `leases.py` store module exists in code). A fresh reader cannot tell what lease behaviour constrains — whether a snapshot change affects lease ownership, expiry, or recovery — from this document.

### N7. Knob list is imprecise in small ways that accumulate
Section 2.3 (lines 116–140).

- Planner policy is listed under `[roles.*]` discussion as `models.planner_policy`; in code it is `[models] planner_policy` (default `mixed`). The section reference is loose, not wrong, but an implementer looking for the key in `[roles]` will not find it.
- The list omits `[ship] enabled`, `[ship] require_quality_pass`, `[approval] require_plan_approval`, and `[review] enabled`, all of which interact with the gate and floor rules in sections 7 and 10. The section claims to define "the settings a profile can map onto"; an omitted setting is ambiguous between "not mappable" and "forgotten."

## What is specified well enough to implement without guessing

Stated briefly, for balance, without resolving the findings above:

- The canonical stage order and the subsequence rule (section 7.1), the catalogue messages for unknown nodes, reordered stages, drawn cycles, and empty/duplicate stage lists (section 7.5), and the preset round/quality numbers in the section 9.1 table (as values, once B8's price question and option H are settled) are concrete enough to test against.
- The stage-record field list in section 11.1, the aggregation principles in section 11.3 (medians, sample sizes, never quote partial rows as exact, never count cache hits as negative tokens), and the suggestion approval lifecycle's user-visible steps in section 12.4 (draft inert until approved, approval covers one version only) are clear as rules, even though their storage and edge cases (B6, B7, M7) are not.
- The non-goals in section 3 (no silent auto-application, no authority over Jev decisions, no CLI-first editor, no new providers/checks/workers) and the alternatives in section 18 draw a usable boundary around the feature.

These do not offset the Blockers: the parts that are concrete are the parts that do not decide safety, execution, or measurement, and the parts that decide safety, execution, and measurement are the parts that are missing or contradictory.

## Section 16 dependency map (for the gate)

| Option set | Sections that cannot be implemented until it is picked | What breaks if implemented on the proposed answer and the gate picks otherwise |
|---|---|---|
| A naming/route | 6.1, 6.2, 7.5 messages, 13.1, 15 phases 2/4/5 | Ids, routes, directories, messages all renamed; inconsistent rename strands users |
| B enforcement | 7.3, 7.4, 10.2, 10.3, 17 acceptance 5 | Non-omittable core, quality-none rule, and label runtime effect all change |
| C phasing | 15 (whole), 17 acceptance | Phase boundaries and editor/suggestion acceptance do not exist under C3 |
| D editor form | 13.2, 15 phase 4, 17 acceptance | Half the editor and its tests are built for nothing under D2/D3 |
| E scope | 6.1, 6.2, 6.5, 12.1 cross-repo | Second store, id format, and cross-repo suggestions vanish under E2/E3 |
| F precedence | 8.1, 9.2, 10.1 item 5 | Snapshot contents, fallback order, and gate behaviour all change |
| G default representation | 8.2, 15 phase 3, 17 acceptance | Legacy-path guarantee disappears under G2; migration trigger missing under G1 |
| H Accuracy meaning | 9.1 Accuracy column, 12 goal-conditioned suggestions | Preset table is wrong in nearly every row under H2; tie-break invisible under H3 |

## Bottom line for the gate

Do not approve this document for implementation as written. The minimum repair, in order, is: (1) pick all eight option sets, or explicitly scope implementation to the subset that does not depend on the unpicked sets; (2) add the missing contracts — profile/snapshot/suggestion schemas, the gate-policy table (step × mode × policy → gate state), the snapshot resolution order with per-setting source recording, the runtime effect (if any) of `reduced`, and the stage-record write-path rules for halt/resume/cancel and shared gates; (3) reconcile the contradictions listed as B1–B4, B9–B10, and M3–M4, M6, M10; (4) re-audit after those changes, since the document itself requires re-audit when any option set picks otherwise.
