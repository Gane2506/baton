# Comprehension re-audit: "Decided picks (2026-10-29, never-stop directive)"

Scope of this review: the amendment subsection only, read in
`docs/designs/profile-workflows/design.md` (it sits just before
"## 17. Acceptance criteria"), checked against the sections it
cites — 9.1 (preset tables), 10.2 (enforcement mix), 15 (phased
plan), 16 (option sets A–H), 8.2 (legacy path / default via
profile flag) — plus the further sections the amendment itself
cites (6.2, 6.5, 7.1, 7.3, 10.3, 13.2) and section 19, which is
where the two "escalated" questions come from. No other file was
read, no live machine was contacted, and this review judges
whether the amendment is understandable, consistent with the
sections it cites, and unambiguous about what implementation
must do — not whether the picks themselves are the right policy
choices.

## Verdict in brief

A fresh reader can understand, from the amendment plus the cited
sections, what was picked for sets A–G and why: each pick names
its option, and its rationale traces to the cited section (see
the per-set table below). The amendment fails that test for
set H and for the two escalated decisions in part, and it
overstates one closing claim. Counts: **1 Blocker, 6 Majors,
6 Nits.**

The single most important finding is Blocker B-1: the H3 pick
contradicts its own cited mechanism about where the "priority"
row lives and leaves the pick with no executable effect, so an
implementer cannot derive from the amendment what code or data
must change.

## Per-set comprehension check

| Set | Pick understood? | Rationale traceable to cited section? |
|---|---|---|
| A. Naming and route | Yes — A1, "workflow profile", route `/workflows`. | Yes — section 15 states the plan's names "are the A1 names". |
| B. Safety-floor strictness | Yes — B1, mix from section 10.2. | Yes, except Major M-5 (the B3 contrast is misleading by omission). |
| C. Phasing | Yes — C1, phased per section 15. | Yes, except Nit N-1 (the order shorthand drops two phase items). |
| D. Editor form | Yes — D1, canvas plus list per section 13.2. | Yes. |
| E. Scope and storage | Yes — E1, both scopes per section 6.5. | Yes, except Nits N-2 and N-5 (dropped qualifiers). |
| F. Precedence | Yes — F1, explicit order table per section 9.2. | Yes, except Nit N-4 (intra-tier shorthand). |
| G. Default representation | Yes — G1, default stays hardcoded, guarded by conformance. | Yes — matches section 8.2, including the `[workflows] default_via_profile` flag, default false. |
| H. What Accuracy optimizes | No — see Blocker B-1 and Major M-6. | No — the stated mechanism has no executable content beyond a display row whose location is contradicted. |

## Blocker

### B-1 — H3: the priority row's location and effect contradict the cited H3 mechanism, and the tie-break has nothing to execute

Amendment wording:

> "Sections 6–15 already define Accuracy mechanically as a production quality floor plus CDA grading on (section 9.1); H3 keeps those mechanics exactly and adds only the priority row on the picker entry ("quality first, speed second")."
>
> "H3 answers both readings in priority order without changing any preset value."
>
> "H3 adds one display requirement to phase 4: the Accuracy picker entry shows the priority row "quality first, speed second" before selection."

What the cited sections actually say. Section 16, H3:

> "Mechanism: Accuracy optimizes output quality first, and within equal-quality choices prefers the faster one (fewer redundant rounds, no repeated work). The tie-break lives in the preset's table as a "priority" row reading "quality first, speed second", shown on the picker entry before selection."

So the cited mechanism puts the row in **the preset's table**
and shows it on the picker entry; the amendment narrows it to
**only the picker entry** and to **one display requirement in
phase 4**, with no preset value changed. Three questions an
implementer cannot answer from the amendment:

1. **Data or display string?** Is `priority` a field in the
   Accuracy preset file (validated, versioned, covered by the
   phase 4 preset test that "each preset's setting table matches
   section 9.1")? Section 9.1's table has no priority row, so if
   the row lives in the preset table, section 9.1 needs a row
   added — which the amendment's "one display requirement"
   framing denies. If it is picker-only text, the amendment never
   says where that text lives or what owns it.
2. **What does the tie-break execute?** Section 16's tie-break
   is "fewer redundant rounds, no repeated work", but section
   9.1 gives Accuracy exactly the default's round caps
   (`max_plan_rounds` 3, `max_fix_rounds` 2,
   `score_max_retries` 2) and `exact` cache mode. With "no preset
   value" changed, there is no encoded faster choice for the
   tie-break to prefer, so "answers both readings in priority
   order" reduces to displaying a slogan. A reader cannot tell
   whether Accuracy is supposed to behave differently from the
   default in any case, or merely be labelled differently.
3. **Which picker?** The amendment's Effect paragraph pins the
   requirement to phase 4, whose picker is defined in section 15
   as "preset picker entries on the run-creation form". But
   section 15 phase 2 also specifies a picker on the queue form
   ("queue add (picker on the queue form; snapshot at queue
   time"). The amendment never says whether the priority row
   appears there too.

Fix required before implementation: state in the amendment
whether `priority` is a preset-file field (and add the row to
section 9.1 and the phase 4 preset test) or picker-only display
text (and name its owner); and state what, if anything, the
tie-break changes at run time versus the section 9.1 values. As
written, H3 is not implementable from the amendment alone.

## Majors

### M-1 — "No section text changes" is overstated: the cited sections still present these decisions as open

Amendment wording:

> "Effect on the rest of this document: sections 6–15 already assume A1, B1, C1, D1, E1, F1, and G1, so no section text changes for those picks."

The semantics of sections 6–15 do lean the way the amendment
says (section 15's names "are the A1 names"; section 13.2 is
canvas-plus-list; section 6.5 says "Both scopes exist"; section
9.2 gives the F1 table; section 8.2 describes the G1 guard).
But the cited sections still carry provisional language that a
fresh reader — or an implementer working from sections 6–15 —
will read as undecided:

- Section 9.2 heading: "### 9.2 Precedence (proposed; option set F may reorder it)"
- Section 6.5 heading: "### 6.5 Scope rule (proposed; option set E may narrow it)"
- Section 13.1 heading: "### 13.1 Routes and pages (proposed; final names follow option set A)"
- Section 8.2, final sentence: "Whether the default is ever re-expressed as a profile, or stays hardcoded with the conformance test as its guard, is option set G."
- Section 10.2, opening: "The design's position is a mix, decided finally by option set B."
- Section 10.3: "A reduced run is not blocked from shipping by the label alone (unless option set B chooses refusal for that behaviour class)"
- Section 16, introduction: "The human picks one option per set before implementation. A set left unpicked blocks implementation of the parts that depend on it."

The amendment never amends that introduction, so the document
now contains two decision authorities side by side (human gate
vs. never-stop amendment) with no statement of which governs.
At minimum the provisional headings/sentences need to be marked
decided (even if the semantics are unchanged); as written, "no
section text changes" is false in the literal sense a fresh
reader will take it.

### M-2 — The Phase 0 entry-condition claim is self-referential and unverifiable at read time

Amendment wording:

> "Phase 0's entry condition (every option set picked, document amended by re-audit) is met by this amendment plus the re-audit report that follows it."

Section 15, Phase 0 entry, requires:

> "Entry: the section 16 gate has picked every option set, and this document has been amended by the reserved re-audit."

and its deliverable bullet requires that "the amendment touches
the sections the gate picks changed, listed in the re-audit
report." The amendment touches no existing section — it only
adds its own subsection — while simultaneously claiming no
section text changes are needed (see M-1), so there is no list
of touched sections for a re-audit report to carry. And "the
re-audit report that follows it" does not exist inside the
document at the point the amendment is read; a reader cannot
verify that the condition "is met" from the amendment alone.
The amendment also never addresses section 16's re-audit
conflict route ("If that single re-audit still finds a conflict,
the conflict goes back to the human gate as an added option"),
which sits uneasily with the never-stop premise that no gate
blocks work. The entry claim should either point at a concrete,
already-existing re-audit artefact or be phrased as conditional
on that report's findings.

### M-3 — Hand ship of a reduced run: pick is clear, but its scope and its rationale do not cover legacy reduced runs

Amendment wording:

> "Hand ship of a reduced run: the label travels into the PR body and the ship proceeds; no extra confirmation step is added. Rationale: the label is already visible where the ship decision is made, and a second confirmation gate would re-decide a choice the user already made when picking the reduced flow."

Two gaps:

1. **Scope — which ship paths?** Section 10.3 states the PR-body
   writer "is `core/ship/pr_body.py`, used by both the dashboard
   ship path and the CLI ship path, so a reduced run shipped from
   either path carries the label." The amendment's "ship
   proceeds; no extra confirmation" does not say whether it
   covers the dashboard hand-ship path, the CLI ship path, or
   both. An implementer must guess.
2. **Rationale — legacy runs never "picked a reduced flow".**
   Section 10.2 states "the assurance computation (section 10.3)
   applies to every run, profiled or not, so a legacy run that
   uses one of those bypasses carries the same reduced label a
   profiled run would." For such a legacy run, the user made no
   flow pick at all — the reduction came from configuration
   bypasses (quality `none` in config, `AUTONOMOUS` plan-gate
   absence, and the other classes section 10.2 lists). The
   rationale therefore justifies the decision only for profiled
   reduced runs, while the decision as written covers all
   reduced runs. Related staleness: section 19 says of this
   question "the re-audit would add [a confirmation gate] to
   section 10.2" if the human wanted one; the amendment decides
   the opposite and adds nothing to section 10.2, leaving
   section 19's open-question framing dangling (see also M-1).

### M-4 — "Efficiency quality" is presented as an escalated decision but is a restatement of section 9.1, and "as resolved" is unexplained

Amendment wording:

> "Efficiency quality: repo configuration, never lowered (section 9.1 as resolved). A quality cut stays a user choice in a custom profile with a visible label, never a preset default."

Section 9.1 already states exactly this: Efficiency quality is
"Repo configuration (never lowered by the preset)", and "A user
who wants a lower quality level than the repo's can set it in a
custom profile, and that profile's assurance computation records
the reduction (section 10.3) — a quality cut is a user choice
with a visible label, not a preset default." A fresh reader
therefore cannot tell what was "escalated", what "as resolved"
refers to, or what (if anything) this bullet changes. The
provenance exists elsewhere in the document — section 19:

> "The resolution changed the Efficiency preset from quality `basic` to "repo configuration, never lowered", because `basic` drops secret scanning, SAST, and dependency audit — a security cut, not an efficiency gain. The human gate confirms this change"

— but the amendment does not cite section 19, does not mention
the `basic`-to-repo-config change, and (like M-3) leaves section
19's "human gate confirms" framing standing against a
never-stop decision. Either cite section 19 and state that this
bullet confirms rather than changes section 9.1, or drop the
"escalated, decided here" framing for it.

### M-5 — The B3 contrast is misleading by omission: B1's labelled classes also ship on a label alone

Amendment wording (B row):

> "B3 would let a labelled reduced run ship with nothing but a label standing between it and main."

That sentence is true of B3 (section 16, B3: "a label does not
stop a shipment, so a reduced run can still merge code the full
flow would have caught, and the label's protection depends
entirely on someone reading it"). But it is equally true of the
picked B1 for its labelled classes: section 10.2 states "A label
alone never blocks a ship: the classes that must block are
refused, not labelled", and section 10.3 repeats that a reduced
run "is not blocked from shipping by the label alone". The real
B1-vs-B3 distinction is *which* classes are refused (the
dangerous three the amendment lists first) versus labelled, not
that B1 somehow prevents labelled ships. A reader following only
the amendment's contrast could conclude B1 blocks labelled
reduced runs from shipping, which it does not.

### M-6 — The H row's summary of Accuracy drops section 9.1's third difference (the goal label) without saying whether it survives

Amendment wording:

> "Sections 6–15 already define Accuracy mechanically as a production quality floor plus CDA grading on (section 9.1)"

Section 9.1:

> "Accuracy as defined here differs from the default in exactly three observable ways: CDA grading on, a production quality floor, and the `accuracy` goal label."

The amendment lists two of the three. The word "mechanically"
may be intended to exclude the goal label (section 7.4 notes
"The goal label changes nothing about execution by itself"),
but the amendment never says that, and its follow-on — "H3
keeps those mechanics exactly" — leaves it ambiguous whether
the `accuracy` goal label is retained, dropped, or considered
out of scope for H3. Combined with B-1, a reader cannot
reconstruct the full Accuracy definition from the amendment.

## Nits

### N-1 — The C1 order shorthand drops two phase items

Amendment wording:

> "The phased plan is the design's own order (contract, measurement, storage/selection/assurance, validator + conformance, presets + picker, editor, suggestions)"

Section 15's phases are: contract (0), measurement (1),
storage/**versioning**/selection/assurance (2),
**snapshot-aware lookup** and shared validator plus conformance
(3), presets + picker (4), editor (5), suggestions (6). The
shorthand omits versioning and snapshot-aware lookup — the
latter is phase 3's main code change, not just "validator +
conformance". The order is not wrong, but a reader using the
parenthetical as the plan will understate phase 3.

### N-2 — The E1 git claim drops section 6.2's "by default"

Amendment wording:

> "Repo profiles do NOT travel via git (`.jev/` is gitignored) — sharing is dashboard export/import only, as section 6.2 states."

Section 6.2:

> "They do not travel via git by default, because `.jev/` is ignored by the repo: sharing a repo-scope profile with another machine means exporting its JSON from the dashboard (download) and importing it there (upload)"

The amendment's absolute "do NOT travel" is stronger than the
source's "by default". The supported sharing mechanism
(export/import) is correctly stated; only the dropped qualifier
overstates the guarantee.

### N-3 — The "Rearrange" confirmed reading cites sections 7.1 and 7.3, but the no-reorder-control rule lives in section 13.2

Amendment wording:

> "'Rearrange' means omit stages where allowed and edit parameters; stage reordering is refused by dependency, and the editor offers no reorder control (sections 7.1, 7.3)."

Sections 7.1 and 7.3 do support refusal by dependency, but the
editor rule is in section 13.2: "The canvas offers no reorder
control: dragging a node to a new position is refused with the
section 7.1 reason". Add section 13.2 to the citation.

### N-4 — The F1 order shorthand collapses a single tier into what reads as two ordered tiers

Amendment wording:

> "Per-run flags, then snapshot, then repo configuration and workspace profile, then built-in defaults"

Section 9.2 makes tier 3 a single tier — "Repo configuration:
`jev.toml` plus the run's workspace profile, applied as today
(this design does not change the intra-tier order between a
config file and a workspace profile)". The amendment's "then
repo configuration and workspace profile" can be misread as
repo configuration beating the workspace profile. State that
they are one tier with their existing intra-tier order
unchanged.

### N-5 — The E1 user-scope summary drops "on that machine"

Amendment wording: "user-scope profiles give one personal
library across repos." Section 6.2: "User-scope profiles are
visible in every repo on that machine." Without the machine
qualifier, a fresh reader could infer a cross-machine library,
which nothing in the cited sections provides (cross-machine
sharing is the export/import mechanism described for repo
scope). Low implementation risk, since section 6.2's storage
paths are machine-local, but the summary should carry the
qualifier.

### N-6 — "The user's stated 'performance' reading" is unattributed in the cited sections

Amendment wording (H row): "H1 alone would ignore the user's
stated "performance" reading". The cited sections establish
that "performance" is ambiguous — section 16, H3, "answers both
readings of "performance" in priority order", and section 16,
H1, fails "when: a user picks Accuracy expecting faster results
and gets slower, more careful ones" — but they quote no user
statement to that effect. The point survives without the
attribution ("H1 answers only the quality reading"); as written,
a fresh reader cannot verify the "user's stated" claim from the
amendment plus the cited sections.

## What was checked and found sound

- **A1:** section 15 explicitly anchors the plan's names to A1,
  and the amendment's churn rationale matches section 15's sweep
  list (routes, modules, templates, directories, messages,
  labels).
- **B1 refusals:** the three refusal cases named in the
  amendment match section 10.2 exactly (omitted non-omittable
  stages; quality `none` with shipping on;
  `score_skip_human` with shipping on), and the label surfaces
  (run record, run page, board row, PR body) match sections
  10.2/10.3.
- **B2/B3 rejected-option summaries:** B2 removing the
  "refused or labelled" alternative and pushing reduced work
  out-of-band, and B3 labelling behaviour reductions while the
  five-stage core stays refused, both match section 16 — subject
  only to M-5.
- **C1/C2/C3:** the phased order, the C2 weak-link failure
  (usually measurement or the validator), and C3 abandoning the
  editor and suggestions all match sections 15 and 16.
- **D1:** canvas-plus-list with one validator, canvas-only
  excluding users who cannot drag, and list-only being a form
  rather than the promised drawing, all match sections 13.2 and
  16.
- **E1 rejected options:** the E2 five-copies failure and the
  E3 shipped-PR-irreproducibility failure match section 16.
- **F1/F2/F3:** the four-tier order, the F2 silently-ignored-flag
  failure, and the F3 forgotten-config-overrides-the-preset
  failure match section 9.2 and section 16.
- **G1/G2/G3:** zero-risk-to-existing-runs, the conformance
  suite plus the default-false flag, G2 moving every run at
  once, and G3 paying two-path maintenance for a release all
  match section 8.2 and section 16.
- **H2 rejection:** "promise parallelism and faster models the
  pipeline cannot yet deliver" matches section 16, H2
  ("the pipeline has limited parallelism today", and speed work
  "is not yet available").
- **Confirmed reading on the default:** "a profile is opt-in
  per run, never a silent change to the default (G1, section
  8.2)" is consistent with section 8.2's legacy path plus the
  section 10.3 display exception it names.

## Bottom line for the parent

Do not treat the amendment as implementation-ready on H3: fix
B-1 first (data-vs-display location of the priority row, and
what the tie-break executes). The remaining Majors are
comprehension hazards, not policy disputes — stale
"proposed/decided by the gate" language (M-1), a Phase 0 entry
claim that points at a not-yet-existing report (M-2), an
under-scoped hand-ship decision (M-3), a restated-as-decided
Efficiency point (M-4), a misleading B3 contrast (M-5), and a
dropped goal-label difference (M-6). Sets A–G are otherwise
understandable from the amendment plus the cited sections.
