# Goldfish review resolution report

**Target:** `docs/designs/cda-install-button/design.md` (resolved version:
1,588 lines, up from 1,263)
**Status:** resolved. No re-audit was used (0 of the 1 allowed per
stage); the human approval gate is the next check, and re-running a
stage after the human picks the option sets is the right time for it,
not now.
**Rule applied:** every finding is PROPOSE FIX (applied to the design)
or CONTEST (not applied, with checkable evidence). Findings that
demand the human's option-set decisions are not pre-decided here;
instead the design was rewritten so its normative sections are
conditional on the option sets, and the option sets themselves are
untouched. Duplicate findings across lenses are grouped; the group
lists every report and finding it covers, so no finding is silently
dropped.

## Totals per report (as filed)

| Report | Blockers | Majors | Nits | Total |
|---|---|---|---|---|
| zoom_out_report.md (Stage 5) | 3 | 7 | 5 | 15 |
| comprehension_report.md (Stage 6) | 12 | 20 | 7 | 39 |
| critic_design_report.md (Stage 7, design) | 6 | 12 | 6 | 24 |
| critic_structural_report.md (Stage 7, structural) | 5 | 9 | 7 | 21 |
| critic_style_report.md (Stage 7, style) | 6 | 28 | 14 | 48 |
| intermediate_zoom_out_report.md (Stage 8) | 5 | 7 | 4 | 16 |
| implementation_readiness_report.md (Stage 9) | 10 | 15 | 4 | 29 |
| implementation_codebase_report.md (Stage 9) | 2 | 7 | 5 | 14 |
| implementation_style_report.md (Stage 9) | 7 | 12 | 6 | 25 |
| **Total** | **56** | **124** | **58** | **231** |

## Grouped findings and classification

### R1. Fixed argv never stated; CPU mechanism and Repair argv missing
Covers: comprehension B1; design B2, B5; style B5, M12, M15;
intermediate B5 (argv contradiction); implementation-readiness B7.
**PROPOSE FIX — applied.** New section 5.3a prints the literal
sequences: default `[sys.executable, -m, pip, install,
"laya>=0.4.15"]`; CPU path (torch from the PyTorch CPU index, then
plain `pip install laya`, which keeps the already-satisfied CPU
torch — the mechanism `docs/code-orchestrator.md` describes, with no
`--no-deps` flag); Repair (uninstall `laya`, then the install
sequence); branch-2 venv creation; warm-up (not a pip command).
Sections 5.3 and 7.1 now reference 5.3a instead of restating the
command differently.

### R2. GPU detection deferred and circular
Covers: comprehension B2; design B2; style M13; implementation-readiness B3.
**PROPOSE FIX — applied.** Section 5.3 now states the rule:
macOS → CPU always; Linux/Windows → default wheel only when
`nvidia-smi` exits 0, else CPU. The rule never asks torch (not
installed yet), and a wrong answer costs size only, since the
default wheel runs on CPU too. The probe records the landed torch
build, so a wrong detection is visible.

### R3. Probe contract wrong: argv bug, "loadability" does not exist
Covers: comprehension B3; design B1; zoom-out B2; style B2, M12;
implementation-readiness B6; codebase B2.
**PROPOSE FIX — applied.** The probe argv is corrected to
`<sys.executable> -c ...` (the draft's `-m python -c` was invalid).
Layer 2 is renamed from "loadable" to "constructible" throughout:
`LayaBackend.__init__` only stores the model id (lazy), so
construction proves import plus wiring, never checkpoint loading.
Layer 3 is proven only by a load (warm-up or first grade). Section
5.2 states what each probe step proves and what it does not, and
section 8 now records no `backend.py` change: lazy construction
already allows the probe, with a stated criterion for revisiting.

### R4. Pre-warm has no mechanism
Covers: comprehension B4; design M6.
**PROPOSE FIX — applied.** Section 5.2, step 3: a warm-up subprocess
runs `laya.load(model_id)` with TMPDIR on a real disk and `$HF_HOME`
set when needed, runs no grade, writes nothing to the grade ledger.
Warm-up applies to the current process's repo model only.

### R5. Broken-but-present backends cannot reach Repair
Covers: comprehension B5; design B6; intermediate B2 (Repair).
**PROPOSE FIX — applied.** A Verify action is always present when
the backend is present (section 5.2); a failed Verify surfaces
Repair. Repair is uninstall-then-install per R1, not a vague
"same sequence". The out-of-scope message for a repair that itself
fails is stated in section 5.5.

### R6. Branch-2 venv unimplementable as written
Covers: comprehension B6; design B3; implementation-readiness B4.
**PROPOSE FIX — applied.** Path fixed to `~/.jev/cda-venv`
(section 5.4), discovery by checking that path, and section 8 lists
the grade-runner interpreter override as required work for the
branch, with the decided fallback to branch 3 if it cannot be added
cleanly.

### R7. Durable record: no location, schema, or pid
Covers: comprehension B7; design B4; implementation-readiness B5;
codebase M4; style M23.
**PROPOSE FIX — applied.** Location `~/.jev/cda-install-job.json`
(mode 0700), full schema in section 6.5 (job id, argv, target,
torch path, child pid, times, phase, state, exit code, probe
outcome), retained log at `~/.jev/cda-install.log`, startup rule
(pid-existence check; live child of a dead dashboard is terminated
and the record becomes `interrupted`).

### R8. Panel source of truth, and the section 5.1 default that bypassed the gate
Covers: comprehension B8, B9; design B5; structural Blocker 1;
style B1, B6; intermediate B1, B3; zoom-out (state-matrix Nits).
**PROPOSE FIX — applied.** The section 5.1 default is deleted. No
flip rule is decided until option set A is picked; a "How to read"
note at the top of the design states that sections 5–8 are
conditional on section 9 wherever they depend on an option. Panel
source of truth is now stated: `available` for presence, plus the
recorded probe verdict when a probe has run; a probe verdict never
overrides a failed `available` check into "installed". The state
matrix (section 6.8) is annotated per option A.

### R9. Docker persistence is a dangling pointer
Covers: comprehension B10; zoom-out B3; implementation-readiness B8.
**PROPOSE FIX — applied, plus a new option set.** The matrix row
pointed at option set G, which never decided Docker. Docker
persistence is now its own option set I (named volume / documented
rebuild plus reinstall / ephemeral guide-only), and the matrix row,
section 10 rollback, and section 12 point at I. The volume itself
is not pre-picked: I1/I2/I3 are for the human.

### R10. Route set, template names, and form protection overstated
Covers: comprehension B11; codebase M1, M2, N1; implementation-readiness M3;
implementation-style B6, M2.
**PROPOSE FIX — applied.** Section 6.7 now names every route with
method (start, status, live, verify, repair, stop-if-E, confirm-if-H1)
and states the split honestly: POST routes through `checked_form`,
GET routes as reads under token gating. Template filenames are
named in section 8 (`code_grade_install_status.html`,
`_code_grade_install_live.html`).

### R11. Coordination: no API, ledger guard in the wrong layer, queued vs refused undecided
Covers: comprehension B12, M15; design M2; implementation-readiness B9.
**PROPOSE FIX — applied.** Section 6.4 names the coordinator API
(`try_begin_install`, `try_begin_grade`, `end_install`,
`end_grade` on `DashboardCore`), places the ledger guard in the
route layer before `code_grade_runner.start`, and decides
refused-not-queued with the reason (queuing would add queue
semantics neither runner has).

### R12. Preflight without thresholds or methods
Covers: comprehension M1, M2, M19; design M1.
**PROPOSE FIX — applied.** Section 6.1 now states: disk thresholds
5 GB default / 3 GB CPU with the arithmetic shown and an amendment
rule (confirm against a measured test install); memory below 3 GB
free is a warning, not a refusal; writability by write test; network
by 10-second-timeout requests to named hosts (PyPI, PyTorch CPU
index when CPU path, Hugging Face host when pre-warming).

### R13. Failure taxonomy without classifier signals
Covers: comprehension M3; implementation-readiness M2; design M3.
**PROPOSE FIX — applied.** Section 6.2 now gives the marker order
(PEP 668, temp-full, disk-full, permission, network, no wheel, hash,
killed, pip-lock-after-stop) with fallback text, and requires
marker fixtures in tests.

### R14. State matrix incomplete; vocabulary drift
Covers: comprehension M4; design M4; structural matrix Blocker;
zoom-out N3, N4; style M28.
**PROPOSE FIX — applied.** Section 6.8 now carries the DECIDED job
vocabulary (`idle`, `running`, `done`, `failed`, `stopped`,
`interrupted`; `verifying`/`warming up` are phases within
`running`, never states) and the A-conditionality note. The matrix
itself is unchanged in rows because its gaps were conditionality,
not missing rows: the Verify and Repair rows were already present
via sections 5.2 and 5.5.

### R15. "Every repo" contradicts the single-repo dashboard
Covers: comprehension M5; intermediate §6.9 Blocker.
**PROPOSE FIX — applied.** Section 6.9 is rewritten: a dashboard
process serves one repo (`settings.root`); the install targets the
shared environment, so every dashboard process on the machine reads
the same job record (`~/.jev/`) and the same availability. The
"banner on every repo" claim is replaced with per-process banner
plus shared-record visibility.

### R16. Observability: three stores, log cap, redaction without evidence
Covers: comprehension M6; design M11; implementation-readiness M10, M13.
**PROPOSE FIX — applied.** Section 6.10 states the three artifacts
(job record, retained log, attempt events) and that the log tail is
a view of the retained log, not a fourth store. The cap is corrected
to 262,144 bytes (256 KiB). Redaction is corrected from "existing
practice" to a new rule for this runner, and event fields are named
(timestamp, session id, argv, target, outcome class).

### R17. Shutdown contract rests on a false "draining" claim
Covers: comprehension M7, N4; codebase B1; implementation-readiness M5;
implementation-style B4; style M17.
**PROPOSE FIX — applied, verified against code.** Section 6.11 now
states the real contract: `shutdown_jobs()` order is scheduler,
browser installer, reverse runner, grade runner, then the install
runner; grade shutdown is a join with a 30-second timeout, never a
cancel or drain (draining is entered only via `cancel()`); install
shutdown terminates pip, joins its watcher up to the same timeout,
and marks the record `interrupted`; a grade thread still alive
after its join timeout is ended by process exit, and the record
still says `interrupted`.

### R18. Section 8 conditional, unordered, and missing items
Covers: comprehension M8; implementation-readiness B1, B2, M4, M6, M11, M12, N2, N4;
implementation-style B1, B7, M6, M12.
**PROPOSE FIX — applied in part; CONTEST in part.** Applied:
class and attribute names (`CdaInstallRunner`,
`DashboardCore.cda_install_runner`), the route table, the status
template, the reverse-runner scope note, option annotations on
dependent bullets, and the grade-time TMPDIR/HF_HOME startup-check
item. Contested (with evidence): the demand that section 8 be
phase-ordered before the options are picked. Section 8's dependency
order is now stated — decide section 9, then build the runner, then
routes, then templates, then tests — and a finer phase plan would
be inventing decisions the human gate exists to make. That is a
scope boundary, not an omission.

### R19. Test matrix drift, stubbing, and narrow live verification
Covers: comprehension M9; implementation-readiness M8, M15;
implementation-style M5, M9.
**PROPOSE FIX in part; CONTEST in part.** Applied: the platform
list is aligned to the section 5.4 rows (Mac venv or editable
checkout, not a "Mac system Python" row the matrix never defined),
and live verification is stated as Mac-only per the issue's own
acceptance, not as proof of the full matrix. Contested: the
complaint that stubbing defeats the riskiest tests. Stubbing is
required — no test may download torch or a checkpoint — and the
existing runners' seams (injected spawn doubles, monkeypatched
orchestrator) are the cited precedent. The residual risk is carried
by the live Mac verification, which is stated as such, not hidden.

### R20. Rollback without literal commands
Covers: comprehension M10; implementation-style B3, M10.
**PROPOSE FIX — applied.** Section 10 now gives literal commands
with `<python>` defined as the detected environment's interpreter
(uninstall, cache purge, cache-directory removal), and separates
retry (re-run from preflight) from undo (uninstall) instead of
conflating them.

### R21. Authorization holes: tunnels, token-holder abuse, phantom logging
Covers: comprehension M11; design M5; style B3.
**PROPOSE FIX in part; CONTEST in part.** Applied: recorded fields
are named (section 6.10, R16); a tunnel warning is added (never
expose a token-less dashboard publicly). Contested: token-holder
abuse as a new boundary. Evidence: token holders can already start
runs that execute code through the run pipeline, and local visitors
can already reach a loopback dashboard — the install adds no
privilege beyond that existing boundary, applied to a host mutation
instead of a run. That argument is now stated in section 7.2 so the
human can reject it, rather than left unstated.

### R22. Repair: button or manual command
Covers: comprehension M12.
**PROPOSE FIX — applied.** Repair is a button action (route in
section 6.7, mechanism in section 5.5); the manual command appears
only when repair itself fails.

### R23. Version policy temporally impossible; uninstall detection undefined
Covers: comprehension M13.
**PROPOSE FIX — applied.** The version policy is DECIDED in section
5.3a (floor `laya>=0.4.15` kept; pin only on a breaking version
found in testing, with amendment). Option set G3 is rewritten
accordingly (display without lifecycle UI). G2 uninstall is DECIDED
as laya-only, never torch, so no pre-existing-torch detection is
needed; when torch itself is broken, the panel says so and points
at repair.

### R24. H2 contradicts the before-start rule
Covers: comprehension M14; intermediate H2 Major.
**PROPOSE FIX — applied.** Section 5.4 now says "before any
download starts", with the per-H-option rendering (confirm page,
button detail text, or preflight output). H2's preflight summary is
therefore before the download, not after the decision.

### R25. Scaffold scope ignores the third runner
Covers: comprehension M16; implementation-readiness M6.
**PROPOSE FIX — applied.** Section 8 now includes `ReverseRunner`
in the B1 extraction assessment (executor seam kept, skeleton
shared) and lists `design_reverse.py` as conditionally modified.

### R26. Section 4 "answered by" map is circular
Covers: comprehension M17; design B5.
**PROPOSE FIX — applied.** "Answered by" is replaced with
"Addressed in" throughout section 4, and the section now states
that option-set addresses are not decisions until picked.

### R27. Checkpoint probe never identifies the file
Covers: comprehension M18; design M6 (in part).
**PROPOSE FIX — applied.** No filesystem guessing: the checkpoint
is proven only by a load (warm-up or first grade), and the A3
checkpoint line reads "downloaded" only when a warm-up success is
recorded. Section 5.2 states this explicitly.

### R28. Stop failure modes missing from the taxonomy
Covers: comprehension M20; design M3 (in part).
**PROPOSE FIX — applied.** A pip-lock-after-stop row is added to
the section 6.2 taxonomy (message plus cache-purge remedy), and
section 6.6 defines "safe to retry" positively by environment state
(panel still "not installed", record "stopped", retry overwrites
partials, probe failure becomes the repair state, never "installed").

### R29. No timeouts anywhere
Covers: design M8.
**PROPOSE FIX — applied in part.** Network checks: 10 seconds.
Probe subprocess: 5 minutes, then "verification timed out". Pip and
warm-up: explicitly no wall-clock timeout, with the reason (a
multi-gigabyte download has no honest deadline) and the exit
(dashboard shutdown). A timeout on pip would be an invented number,
so its absence is stated, not overlooked.

### R30. Windows temp and service-account cache glossed
Covers: design M9.
**PROPOSE FIX — applied in part.** Section 5.3 now states the
Windows TEMP/TMP real-disk rule, and section 5.4 keeps `$HF_HOME`
as the cache control on every platform (environment variable, not
path syntax). What remains unproven is the Windows service account's
ability to own a writable cache directory — that stays in section
12 as proof owed, not as a decided fact.

### R31. Pip progress overstated
Covers: design M10.
**PROPOSE FIX — applied.** Section 6.3 now states pip runs without
a terminal, so its progress bars do not render and log lines are
the only signal; no percentage is claimed.

### R32. Section 2 inaccuracies
Covers: design M7.
**PROPOSE FIX — applied.** Section 2 now states the dashboard
serves one repo per process, names `ReverseRunner` as the third
runner, and notes the dashboard UI never renders the backend
modules' own pip-install exception text (failed dashboard grades
render `unavailable_message()`; the CLI keeps its own guidance).

### R33. Goals unconditional while justification is deferred (scope built before scope decided)
Covers: design M12; zoom-out B1, M1, M6; style B4.
**CONTEST in part; PROPOSE FIX in part.** Contested: the demand to
narrow the goals before the human gate. The goals are the issue's
scope, and option set C exists precisely so the human can narrow
them; narrowing them in resolution would pre-decide C. Applied:
section 3 now carries the note that option set C can narrow the
goals, and section 9's C options state the narrowed shapes. The
zoom-out concern about building the whole system before C is
decided is answered structurally: sections 5–8 are now conditional
text, not build instructions, until C (and the other sets) are
picked.

### R34. State vocabulary drift
Covers: design N2; style M28; zoom-out N4.
**PROPOSE FIX — applied.** The DECIDED vocabulary note in section
6.8 (states vs phases, never `cancelled`/`draining` for this job).

### R35. Numbers: units, attribution, placeholders
Covers: style M8, M9, M10, M11, N1; comprehension N2.
**PROPOSE FIX — applied.** 256 KB corrected to 256 KiB (262,144
bytes) in sections 2 and 6.10. The 2–3 GB figure is attributed to
runtime memory (torch plus model, from `docs/code-orchestrator.md`)
in section 6.1. The "few hundred MB" placeholder is replaced with
the 0.5 GB planning figure tied to the section 6.1 arithmetic.
Checkpoint size is normalized to "~842 MB" throughout. The
planning figures remain planning figures with an amendment rule;
they are not presented as measurements.

### R36. Dates and chronology
Covers: comprehension N1; design N6; style M22.
**PROPOSE FIX — applied.** Design date moved to 2026-10-30, with a
note explaining the brief date (2026-10-29) and lens-report date
(2026-10-30 UTC).

### R37. "One-click" vs confirm reality; "zero terminal" absolutes
Covers: comprehension N5; design N3; zoom-out N1; style B4.
**PROPOSE FIX — applied.** Section 1 now says one click plus a
confirm click when H1 is picked; section 10's first criterion is
checked per picked H option; "the browser downloads nothing" is
reworded to "no package bytes flow through the browser" with the
page-load caveat. The branch-3 guide and Docker cases are the
stated exceptions to zero-terminal operation, not contradictions
discovered later: option set C decides which exception applies.

### R38. Log tail vs log; "current pip line"; confirm vocabulary; live region vs fragment
Covers: style N4, N5, N10, N11.
**PROPOSE FIX — applied.** "Current pip line" is now "latest pip
output line"; the log tail is consistently a view of the retained
log; confirm vocabulary is fixed to the route names in section
6.7; "live region" (the page area) and "live fragment" (the
template it reloads) are defined in the glossary.

### R39. "Button becomes guide" undefined
Covers: style M26.
**PROPOSE FIX — applied.** Branch 3 now states the button is
replaced by the guide block (command, copy button, "Check again"),
not disabled in place.

### R40. Remaining style Nits (wording only)
Covers: style N2 (reworded, R37), N3 ("exact path is an
implementation detail" — removed with the fixed `~/.jev/cda-venv`
path, R6), N6 (meta-sentences — the "How to read" note replaces
self-asserting claims with conditional statements), N7 (restart
tautology — section 5.2 now states the restart message and its
trigger separately), N8 ("green badge" — reworded to "reporting it
as installed"), N9 ("a check afterwards" — pinned to section 5.2
in section 1), N12 (placeholder UI copy — the repair-failure
message is now literal text in section 5.5), N13 ("honest" as a
label — defined in section 5.3 as "no invented number"),
N14 (vague preflight list — replaced by section 6.1's thresholds).
**PROPOSE FIX — applied**, except N13's underlying complaint:
"honest" is kept as a defined term, not dropped, because the
design's no-invented-numbers rule needs a name and now has a
definition.

### R41. "Single-flight mislabelled rate limiting"
Covers: style M16.
**PROPOSE FIX — applied.** Section 7.2 now states single-flight is
the only concurrency bound and explicitly not rate limiting.

### R42. "Constant" argv vs builder with variable inputs
Covers: style M15.
**PROPOSE FIX — applied.** Section 5.3a defines "constant" as no
request input; interpreter path and torch path vary by machine and
are the only variables.

### R43. Section 3 restart condition vs section 5.2
Covers: style M21.
**PROPOSE FIX — applied.** Section 3 now matches section 5.2: the
panel flips per option A when the probe passes and the panel can
reflect it without a restart, or says a restart is needed when the
running process cannot see the backend.

### R44. Implementation-style remaining items
Covers: implementation-style M1 (two link destinations — fixed by
the route table: failed-grade message links to
`/code-grade/install`, Ratings empty state links to
`/code-grade`), M3 ("stops printing" — fixed: replacement text is
the install link, stated in section 8), M7 (progress/enablement
unmeasured — fixed: progress is phase plus elapsed time plus log
tail on the install status page; enablement is per option D),
M8 ("while the backend is missing" narrowing — fixed: trigger is
"backend not usable", section 5.5, and "working retry" is defined
as preflight plus pip plus probe passing), N5 (PEP 668 annotated
on one row — the stance is now global in section 5.4, not
per-row), N6 (quantities — the retry and reinstall counts are now
stated: retry re-runs once per click with no cap; Docker reinstall
is once per recreate).
**PROPOSE FIX — applied.**

### R45. Codebase Nits
Covers: codebase N1 (GET CSRF — fixed, R10), N2 (class/API names —
fixed, R18), N3 (redaction practice — fixed, R16), N4 (shutdown
marking mechanism — fixed, R17), N5 (platform drift — fixed, R19).
**PROPOSE FIX — applied.**

### R46. Zoom-out M2, M3, M4, M7 (reuse, altitude, fan-out, two scopes)
Covers: zoom-out M2, M3, M4, M7.
**PROPOSE FIX in part; CONTEST in part.** Applied: reuse is decided
by option set B (not deferred silently); altitude is corrected by
the conditionality rewrite (detail in sections 5–8 is mechanism,
not build order, until options are picked); the two scopes are
separated in rewritten section 6.9 (runtime vs per-repo checkpoint).
Contested: the fan-out complaint (M4). Fanning install state to
each dashboard process's panel is inherent to a shared-environment
install: each process reads the same `~/.jev/` job record and the
same availability. The alternative — showing install state only in
the starting process — would leave other processes reporting a
stale "not installed" for a shared backend. Evidence: section 6.5
record location plus section 6.9 per-process reading.

## What resolution did NOT do

* It did not pick any option set. Section 9 now has nine sets
  (A–I; I, Docker persistence, was added because the matrix pointed
  at G for a decision G never made). Every set is still unranked
  and unpicked.
* It did not re-audit any stage. Re-audit (max 1 per stage) is
  reserved for after the human picks the option sets, when the
  design changes from conditional to decided.
* It did not resolve the section 12 proof items (uv tool behavior,
  Docker writability, Windows service cache ownership, `laya`
  version drift, two-process contention). Those are implementation-
  phase proofs, stated as such, not decided facts.
* It did not change the value question. Option set C still carries
  it to the human with the threshold evidence (0.36–0.52 across 13
  grades, nothing clearing 0.7).

## Files changed in resolution

* `docs/designs/cda-install-button/design.md` — all fixes above.
* `docs/designs/cda-install-button/review/resolution_report.md` —
  this file.
