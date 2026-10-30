# Critic review: "Decided picks (2026-10-29, never-stop directive)" amendment only

Scope: this review covers ONLY the amendment subsection in
`docs/designs/profile-workflows/design.md` (sitting just before
"## 17. Acceptance criteria"), checked against the sections it cites.
No other file was read, no live machine was contacted, `design.md`
was not modified, and nothing was committed or pushed.
Method: each pick's wording was compared word-for-word with its
normative section; each escalated decision was compared with
sections 10.2, 10.3 and 19; the amendment's "already assume" and
phase-0 claims were compared with the document header, the
"Decided vs. open" preamble, and sections 15–17.

Verdict in one line: the amendment's substantive picks are mostly
faithful restatements, but it does NOT validly close the gate it
claims to close, and it leaves three implementation-guessing gaps
(H3, G1, B1) plus one hand-ship rationale that does not cover the
cases the design itself defines.

Counts: **1 Blocker, 6 Majors, 4 Nits.**

## 1. Does any pick create a NEW contradiction?

No pick invents a new preset value, a new stage, a new route, or a
new refusal. A1, C1, D1, E1 and F1 are faithful to the sections they
cite. The new contradictions are narrower, and they are about
closure, scope, and traceability — not about a pick choosing the
wrong value:

- The amendment declares a gate closed (phase 0) whose completion
  depends on this very re-audit (Blocker B-1).
- It claims "no section text changes" while leaving every
  "(proposed)" / "decided finally by option set" marker in place
  (Major M-1).
- It compresses B1, G1 and H3 in ways that drop distinctions the
  normative sections explicitly preserve (Majors M-2, M-3, M-4).

## 2. Does "sections 6–15 already assume A1, B1, C1, D1, E1, F1, G1" hold?

Mechanically, yes for A–G, with the qualifications below. It does
NOT hold as a claim that no textual work remains, because the
document still marks those sections as undecided.

| Set | Verdict | Evidence |
|---|---|---|
| A1 | Holds mechanically. Sections 6, 13 and 15 already use `/workflows`, `workflow-profiles` directories, `workflow_profile` field, `core/workflow_profiles/` and `routes_workflows.py`. | Sec. 13.1, sec. 15 phase 5, sec. 6.2. Caveat: the header still says option set A "decides the final name" (see M-1). |
| B1 | Holds mechanically. Sec. 10.2's refusal list plus its label-only bypass classes are exactly the B1 mix defined in sec. 16. | Sec. 10.2 vs sec. 16 B1. Caveat: the amendment's shorthand drops the "sets" qualifier (see M-2). |
| C1 | Holds. Sec. 15's phase order is contract, measurement, storage/selection/assurance, validator + conformance, presets + picker, editor, suggestions — the order the amendment lists. | Sec. 15, sec. 16 C1. |
| D1 | Holds. Sec. 13.2 is already "canvas plus list, one validator", and phase 5 builds both modes. Note: D and C were not even in the preamble's "proposed" list, so for them "already assume" is trivially true — they were stated as design, and only the sec. 16 gate still blocked them. | Sec. 13.2, sec. 15 phase 5. |
| E1 | Holds. Sec. 6.5 already says "Both scopes exist", and sec. 6.2 already gives both store layouts. | Sec. 6.5, sec. 6.2. Caveat: the amendment hardens "by default" to an absolute (see N-1). |
| F1 | Holds. Sec. 9.2's order is flags, then snapshot (profile version), then repo config + workspace profile, then defaults, with `resolved_from` recorded — the order the amendment states. | Sec. 9.2, sec. 6.6. |
| G1 | Holds in substance, unresolved in one place. Sec. 8.2 already gives the legacy path, the conformance suite, and the `[workflows] default_via_profile` flag (default false). | Sec. 8.2, sec. 16 G1. Caveat: sec. 13.1 vs sec. 8.2 on what the flag flips (see M-3). |
| H3 | Correctly EXCLUDED from the "already assume" list — sec. 9.1 contains no priority row — but the amendment then fails to actually put the row where H3 requires it (see M-4). | Sec. 9.1 table, sec. 16 H3. |

## 3. The two escalated decisions

### Hand ship of a reduced run — consistent with sec. 10.2's stated position, NOT with its own rationale, and it closes an open safety question without answering its hardest case (Major M-5, not a Blocker)

It does not contradict sec. 10.2's mix position, which already
states that a hand ship proceeds with the label. It does not
change a promised confirmation step, because none was promised:
sec. 19 explicitly left confirmation as an open human-gate
question. What it does is decide that open question in the weaker
direction, on a rationale that is false for two of the design's
own reduction classes, and without addressing the one circumvention
route the design itself creates. That is a gap, not a restatement.

### Efficiency quality: repo configuration, never lowered — consistent (with one definition gap, Major M-6)

It restates sec. 9.1 and sec. 19 exactly: the preset never lowers
the repo level, and a cut is a custom-profile choice. It does not
quietly change a safety promise. The gap is only what "visible
label" means for a non-`none` cut (see M-6).

## 4. Findings

### Blocker

#### B-1. The amendment self-certifies phase 0 before the re-audit it depends on can pass

Amendment wording:

> "Phase 0's entry condition (every option set picked, document amended by re-audit) is met by this amendment plus the re-audit report that follows it."

Conflicting sections:

- Document header: "Implementation happens only after the human approval gate in section 16."
- Sec. 16: "The human picks one option per set before implementation. A set left unpicked blocks implementation of the parts that depend on it."
- Sec. 15, phase 0: "Entry: the section 16 gate has picked every option set, and this document has been amended by the reserved re-audit."
- Sec. 16, re-audit definition: "If that single re-audit still finds a conflict, the conflict goes back to the human gate as an added option — it does not trigger a second re-audit."

Why this is a Blocker, not a wording nit: at the moment the
amendment was written, the re-audit report did not yet exist, so
the entry condition was not met — it was predicted. If this
re-audit finds any conflict (and it does, below), sec. 16 requires
that conflict to go back to the human gate; it cannot already have
been satisfied by "this amendment plus the report that follows
it." The amendment also invokes an authority ("never-stop
directive") that appears nowhere in the design document, while the
document's own gate authority is the human pick. Whether an
external directive overrides the document's gate is a decision for
the human/owner, not something the amendment can assert into
existence. Until the human gate is recorded as passed (or the
document is explicitly amended to say the never-stop directive
replaces it), phase 0 must not be treated as entered, and phase 1
must not start on the strength of this sentence. Fix: change the
sentence to a conditional — entry is met only if this re-audit
reports no Blocker and the human gate (or its documented
replacement) is recorded — and do not start phase 1 until then.

### Majors

#### M-1. "No section text changes" leaves the contract marked as undecided, contradicting the phase-0 "contract frozen" claim

Amendment wording:

> "sections 6–15 already assume A1, B1, C1, D1, E1, F1, and G1, so no section text changes for those picks."

Still-undecided markers left in the document (a non-exhaustive list):

- Preamble: "The following points are proposed, not decided, and change under the option set named: scope (section 6.5; set E), precedence (section 9.2; set F), enforcement (sections 7.3, 10.2, 10.3; set B), routes and names (section 13.1, phase 5; set A), the default's representation (section 8.2, phase 3; set G), and what Accuracy optimizes (section 9.1; set H)."
- Sec. 6.5 heading: "(proposed; option set E may narrow it)".
- Sec. 9.2 heading: "(proposed; option set F may reorder it)".
- Sec. 13.1 heading: "(proposed; final names follow option set A)".
- Sec. 10.2: "The design's position is a mix, decided finally by option set B."
- Sec. 8.2: "Whether the default is ever re-expressed as a profile, or stays hardcoded with the conformance test as its guard, is option set G."

Phase 0's exit requires the contract to be frozen, and phase 0's
deliverable is "the amended document itself". A reader who opens
sec. 6.5, 9.2, 10.2 or 13.1 alone will reasonably conclude those
sections are still open. The amendment's own rule — "a restatement
never wins over the normative statement" (preamble) — cuts against
it here: the normative sections still say "proposed". Fix: either
edit those headings/sentences to "decided" (which contradicts the
amendment's "no text changes" sentence, so that sentence must go
too), or keep the markers and drop the claim that the contract is
frozen. The current combination cannot both be true.

#### M-2. B1's compressed refusal list drops the profile-sets vs configuration-inherited distinction that sec. 10.2 explicitly preserves

Amendment wording:

> "Refusals for the dangerous cases (omitted non-omittable stages, quality `none` with shipping on, `score_skip_human` with shipping on)"

Conflicting / qualifying section, sec. 10.2:

> "A profile is refused at save time when it omits any of the five non-omittable stages (section 7.3), sets quality level `none` while shipping is enabled (`ship_enabled` true), or sets `score_skip_human` while shipping is enabled."

and, in the same section:

> "Configuration-level bypasses outside profiles are not rewritten by this feature. Instead, the assurance computation (section 10.3) applies to every run, profiled or not, so a legacy run that uses one of those bypasses carries the same reduced label a profiled run would. The bypass classes that produce a reduced label are: quality `none` in configuration; `AUTONOMOUS` mode (plan gate absent); `score_skip_human` in configuration; plan approval off in configuration (section 10.1 item 5); and a missing ship score."

The amendment's list says "quality `none` with shipping on" with
no "sets" qualifier and no "in the profile" scope. Read literally,
an implementer following the amendment alone would refuse a run
whose effective quality is `none` with shipping on even when both
values come from repo configuration — the exact case sec. 10.2
says is label-only, not refused. Conversely, a profile that leaves
quality absent (so it falls back to a `none` repo config) with
shipping on would be ambiguous: refused as an effective setting, or
labelled as a config bypass? The normative section controls per
the preamble, so this is not a live contradiction in the full
document — but the amendment is the section implementers will
quote for B1, and as written it will mislead them. Fix: restore
the qualifier in the B1 row ("a profile that sets …") and state
explicitly that config-inherited `none` / `score_skip_human` with
shipping on stays label-only under B1.

#### M-3. G1 does not resolve what the `[workflows] default_via_profile` flag actually flips, because sec. 13.1 and sec. 8.2 point in opposite directions

Amendment wording:

> "runs without a profile execute today's code path unchanged, and the `default` preset is proven equivalent by the conformance suite before any run executes through the profile path (and then only behind `[workflows] default_via_profile`, default false)."

Conflicting sections:

- Sec. 13.1: "`default` in the picker means \"no profile\" (the legacy path), not a preset that runs."
- Sec. 8.2: "After the suite passes, the default executes through the profile path only when the configuration flag `[workflows] default_via_profile` is set to true (default false) — that flag is the migration trigger under G1".

If picker-`default` means "no profile" (sec. 13.1), then no
production run ever executes the `default` preset path, and the
flag can only ever affect the conformance harness — in which case
the amendment's "before any run executes through the profile path
(and then only behind the flag)" describes a path no run takes.
If instead flipping the flag turns picker-`default` runs into
profile-path runs, then sec. 13.1's definition is wrong the moment
the flag flips, and the amendment's confirmed reading — "a profile
is opt-in per run, never a silent change to the default" — is
violated for every user who had picked `default` expecting the
legacy path. The amendment repeats both sentences without choosing
between them. Implementation must guess. Fix: state which runs the
flag affects (harness only, or picker-`default` runs, and if the
latter, how the picker label changes when the flag flips).

#### M-4. H3's "speed second" has no mechanism, and its priority row exists in no normative table, phase bullet, or acceptance criterion

Amendment wording:

> "Sections 6–15 already define Accuracy mechanically as a production quality floor plus CDA grading on (section 9.1); H3 keeps those mechanics exactly and adds only the priority row on the picker entry (\"quality first, speed second\")."

> "H3 adds one display requirement to phase 4: the Accuracy picker entry shows the priority row \"quality first, speed second\" before selection."

Conflicting / missing sections:

- Sec. 9.1: "Accuracy as defined here differs from the default in exactly three observable ways: CDA grading on, a production quality floor, and the `accuracy` goal label." Its table gives Accuracy the same round caps as the default (`max_plan_rounds` 3, `max_fix_rounds` 2, `score_max_retries` 2), repo-configuration models, and repo-configuration planner policy — i.e. no faster model, no fewer rounds, no parallelism parameter. Sec. 17 confirms the last point for the sibling option: "the section 7 language has no parallelism parameter today".
- Sec. 16, H3 mechanism: "The tie-break lives in the preset's table as a \"priority\" row reading \"quality first, speed second\", shown on the picker entry before selection." The preset's table is sec. 9.1 — and sec. 9.1 has no priority row.
- Sec. 15, phase 4 bullets: picker entries show "documented behaviour … including the assurance preview (section 13.1) and the unpriced-role notice" — no priority row. The amendment says it "adds" this requirement to phase 4, but phase 4's text was not edited, and sec. 17's acceptance criteria contain no priority-row check.

So H3 as amended is display-only by its own admission ("without
changing any preset value"), yet it promises a tie-break that no
preset value ever exercises: there is no equal-quality choice in
the Accuracy table where "speed second" changes anything. A picker
entry that says "quality first, speed second" while the preset
behaves identically to "quality only" overstates the preset, and
sec. 9.1's own warning is left unanswered: "If that closeness
means Accuracy 'just renames the default', option set H exists to
say so and to redefine what Accuracy optimizes." H3 does not
redefine; it relabels. Fix: either add the priority row to the
sec. 9.1 table AND to a phase-4 bullet AND to a sec. 17 check and
name at least one concrete equal-quality tie the preset actually
breaks toward speed, or record explicitly that the row is
documentation of intent with no behavioural effect and that
Accuracy's closeness to the default stands as a known limitation.

#### M-5. The hand-ship decision's rationale does not cover the design's own reduction classes, and it locks in the shipping-off circumvention of a B1 refusal

Amendment wording:

> "Hand ship of a reduced run: the label travels into the PR body and the ship proceeds; no extra confirmation step is added. Rationale: the label is already visible where the ship decision is made, and a second confirmation gate would re-decide a choice the user already made when picking the reduced flow."

Conflicting / qualifying sections:

- Sec. 10.2: "Hand shipping does not re-decide the run: under the mix, the label travels and the ship proceeds; the human-gate question of whether a hand ship of a reduced run should also require confirmation is recorded in section 19."
- Sec. 19: "Whether a hand ship of a reduced run should also require an explicit confirmation is a human-gate question".
- Sec. 10.3: "The writer for the pull request body is `core/ship/pr_body.py`, used by both the dashboard ship path and the CLI ship path, so a reduced run shipped from either path carries the label."
- Sec. 10.2 bypass classes (quoted in M-2): quality `none` in configuration, `AUTONOMOUS` plan-gate absence, `score_skip_human` in configuration, plan approval off, missing ship score.

Three gaps in the rationale:

1. "A choice the user already made when picking the reduced flow"
   is false for config-derived reductions. A run reduced because
   the repo config has quality `none`, plan approval off, or
   `score_skip_human` was never "picked" as a reduced flow by
   anyone; the label is the first time the reduction may surface.
   The same holds when the hand-shipper is a different person
   from the run creator.
2. "Already visible where the ship decision is made" assumes the
   shipper sees the run page or board row before shipping. For
   the CLI ship path (sec. 10.3), the label's guaranteed surface
   is the PR body being created — i.e. visible to reviewers
   before merge, but not necessarily to the shipper before the
   ship action. The amendment does not state any pre-ship display
   requirement for the CLI path.
3. Circumvention, unaddressed: a profile that sets quality `none`
   with shipping on is refused under B1, but a profile with
   quality `none` and shipping off saves, the run ends `DONE`
   unshipped, and a later hand ship ships exactly the refused
   result with only a label. Sec. 2.2 already names this as an
   existing bypass ("a run that ends at `DONE` unshipped can
   still be shipped later by hand"), and sec. 10.2 already
   accepts label-travels for it — so the amendment creates no new
   loophole, but by deciding "no extra confirmation" it closes
   the one remaining chance to close it, without acknowledging
   the route exists.

Fix: scope the no-confirmation decision (profile-picked reductions
by the same actor via the dashboard, where the label was shown
before the ship action), and state explicitly whether
config-derived reductions and CLI hand ships are covered, and
whether the shipping-off-then-hand-ship route is accepted as a
known B1 limitation or needs a separate rule.

#### M-6. "A quality cut stays a user choice … with a visible label" overstates what sec. 10.3 defines as `reduced`

Amendment wording (Efficiency escalated decision):

> "A quality cut stays a user choice in a custom profile with a visible label, never a preset default."

Conflicting / qualifying sections:

- Sec. 9.1: "A user who wants a lower quality level than the repo's can set it in a custom profile, and that profile's assurance computation records the reduction (section 10.3)".
- Sec. 10.1, item 1: "The quality gate runs at `INTEGRATING` and a failed gate never ships. The level may vary, but `none` is addressed in section 10.2".
- Sec. 10.3: "`full`: every floor behaviour in section 10.1 is present. `reduced`: otherwise, plus the list of absent behaviours in plain words".

Lowering a level from `production` to `basic` (say) does not
remove any sec. 10.1 behaviour — the gate still runs, and the
level "may vary" — so under sec. 10.3 that run is still `full`,
not `reduced`, and there is no defined label text for it. Only
`none` is carved out, and only with shipping on is it refused;
with shipping off it is a different case again. The amendment's
 blanket "with a visible label" therefore promises a label the
assurance computation, as defined, does not produce for a
non-`none` cut. Implementation must guess: does any level below
the repo's trigger `reduced`, and if so, with what absent-behaviour
wording? Fix: define the trigger (any level below repo level, or
only `none`) and its label wording in sec. 10.3, or narrow the
amendment sentence to "`none` with shipping off" and other cases
sec. 10.3 actually covers.

### Nits

#### N-1. E1 hardens sec. 6.2's "by default" into an absolute

Amendment wording:

> "Repo profiles do NOT travel via git (`.jev/` is gitignored) — sharing is dashboard export/import only, as section 6.2 states."

Sec. 6.2 wording:

> "They do not travel via git by default, because `.jev/` is ignored by the repo".

"By default" matters: a repo that un-ignores `.jev/` or force-adds
a profile file would travel it, and the amendment's absolute would
then be false. Restore "by default" or state the assumption (the
standard ignore is in place).

#### N-2. H3's summary of Accuracy omits the third observable difference

Amendment wording:

> "Sections 6–15 already define Accuracy mechanically as a production quality floor plus CDA grading on (section 9.1)".

Sec. 9.1 wording:

> "Accuracy as defined here differs from the default in exactly three observable ways: CDA grading on, a production quality floor, and the `accuracy` goal label."

The goal label is dropped from the amendment's summary. Harmless
while sec. 9.1 controls, but an implementer working from the
amendment's two-item definition could drop the goal label from the
preset or from the picker, breaking sec. 12.1's evidence rule that
a preset's goal label is display only and never counts as a stated
goal. Add "plus the `accuracy` goal label" to the H3 row.

#### N-3. A1's rationale applies the amendment's own principle order backwards

Amendment wording (principles "in order: browser-first (zero
terminal use), least surprise, lowest resource use, and zero risk
to existing runs"):

> "Renaming now would churn every reference for no user benefit, and A1 keeps the issue's own words."

Sec. 16, A1's own trade-off concedes the cost the rationale omits:
"descriptive and close to the issue's words, but 'profile' still
appears next to learning profiles and server profiles, and users
must learn which 'profile' a page means", with the fail case "a
user opens `/profiles` expecting workflow profiles, or vice versa,
and picks the wrong kind." Churn is a lowest-resource-use
argument; the collision is a least-surprise argument, which the
amendment ranks higher. "No user benefit" to renaming is therefore
too strong: the benefit (a distinct word, A2) is stated in the
document itself, even if the amendment judges it outweighed by
churn. Fix: say so explicitly — A1 is chosen despite the known
collision, with scope badges (sec. 6.1) as the mitigation — rather
than claiming there is no benefit to weigh.

#### N-4. Two shorthand slips that will mislead a skim-reader (not live contradictions while normative sections control)

1. B1's refusal list, read as exhaustive, omits refusals that are
   not safety-floor refusals but are still refusals: SHIPPING
   omission while shipping is on ("Shipping is omitted but
   shipping is on. Turn shipping off, or include shipping.",
   sec. 7.5), INTAKE gate policies ("Intake has no waiting state,
   so it cannot carry a gate policy.", sec. 7.2), and
   `require` + AUTONOMOUS creation refusal (sec. 7.2). The B1
   row should say its list is the safety-floor refusals only.
2. F1 says "then snapshot" while the snapshot schema's
   `resolved_from` values are "`flag`, `profile`, `repo`,
   `workspace`, or `default`" (sec. 6.6). Sec. 9.2 itself uses
   both phrasings ("The run's flow snapshot (profile version)"),
   so the amendment copies a pre-existing slip rather than
   inventing one — but a single sentence clarifying that tier 2
   is the profile version whose resolved values land in the
   snapshot would remove the ambiguity.

## 5. What was checked and found consistent (so the parent need not re-litigate it)

- B1's refusal/label mapping matches sec. 10.2 and sec. 16 B1;
  B2/B3 characterisations in the amendment match their sec. 16
  fail cases (B2 forcing out-of-band work; B3 shipping on a label
  alone).
- C1's phase order and D1's two-mode editor match secs. 15 and
  13.2; E1's two scopes and export/import sharing match secs. 6.5
  and 6.2; F1's order and `resolved_from` auditability match
  secs. 9.2 and 6.6.
- G1's legacy path, conformance guard, and flag default match
  sec. 8.2 (subject to M-3).
- The Efficiency escalated decision matches sec. 9.1's table
  ("Repo configuration (never lowered by the preset)") and
  sec. 19's resolution note (subject to M-6).
- The "Rearrange" and default-unchanged confirmed readings match
  secs. 7.1, 7.3 and 8.2, including the sec. 10.3 display exception
  for legacy runs.
- No pick changes any preset value, stage list, gate policy, or
  storage layout in secs. 6–15.

## 6. Recommended disposition for the re-audit owner

Do not treat phase 0 as entered on this amendment alone (B-1).
Minimum repair before phase 1: (a) make the phase-0 sentence
conditional on this re-audit plus a recorded human-gate (or
documented replacement) decision; (b) fix or retract the "no
section text changes" sentence and update the "(proposed)"
markers (M-1); (c) restore the B1 "sets" qualifier (M-2); (d)
decide the G1 flag's scope (M-3); (e) give H3 a real mechanism or
record it as intent-only with its table/phase/acceptance entries
added (M-4); (f) scope the hand-ship decision and acknowledge the
shipping-off route (M-5); (g) define the label trigger for a
non-`none` quality cut (M-6). Items (a) and (b) are gate-blocking;
(c)–(g) are implementation-blocking if left as written, because
each forces the implementer to guess at a normative rule.
