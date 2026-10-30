# EGM Stage 6 — Comprehension Test: CDA install button design

- **Target:** `docs/designs/cda-install-button/design.md` (1,263 lines)
- **Method:** naive-reader / re-implementation test. The question for every
  finding is: could a competent engineer with no prior conversation
  implement this behaviour on the first attempt, from the document alone,
  without guessing or inventing a design choice?
- **Verdict: FAIL under the re-implementation standard.** The document is
  unusually explicit about failure modes and option trade-offs, but the
  implementable core — the exact install command, the GPU decision, the
  verification probe, the panel's source of truth, the durable record, and
  the Docker answer — is either absent, deferred to the implementer, or
  left to option sets that the document itself says block implementation.
  A fresh reader would have to invent those, which is exactly what the
  standard forbids.
- **Scope note:** only the design document, `concepts_brief.md`, and the
  code files named in the task were read. No other file was used, and no
  code was changed. This report is the only file written.
- **Counts:** 12 Blockers, 20 Majors, 7 Nits (39 findings).

Severity key: **Blocker** = the implementer cannot build the behaviour
without guessing, or two sections demand incompatible behaviour.
**Major** = a behaviourally significant contract (threshold, route,
state, error, test, or rollback) is underspecified or contradicted.
**Nit** = a wording, arithmetic, or terminology slip that can mislead
a fresh reader but does not by itself block a build.

---

## Blockers

### B1. The fixed install command is never actually stated — §7.1 (lines 708–734), §5.3 (lines 333–372), §8 (lines 765–855)
**What a fresh reader could not determine:** the argv itself. Is it one
pip invocation or two? What are the literal arguments, in order, for the
default path and for the CPU path? Is `laya` installed with `--no-deps`,
a constraint, or a plain `pip install laya` after torch (the docs' CPU
path says "without its torch dependency resolution", but the design
never names the mechanism)? What torch version, if any, is pinned?
What argv does Repair use — plain reinstall, `--upgrade`, or
`--force-reinstall`? What argv creates the branch-2 venv?
**Evidence:** §7.1 says the argv "installs `laya` (and torch first, on
the CPU path) only" and that the only index URLs are "the PyPI default
and the PyTorch CPU index, both constants in code" — but it prints
neither the PyTorch URL nor any argv. §8 then demands a test asserting
"`jev-agent` appears nowhere in the built argv, on every branch" for an
argv the document never builds. The §4 claim that this blocker is
"answered by section 7" is therefore false: §7 states the *rule*, not
the command.

### B2. GPU detection — the 2.5 GB decision — is explicitly deferred — §5.3 (lines 333–372), §12 Q5 (lines 1218–1263)
**What a fresh reader could not determine:** which call, on which
machine, decides CPU vs default torch. The document says detection is
"done by the dashboard process (for example, whether CUDA is reported
available)" and that "the exact detection call is an implementation
detail the implementer must name in code review". That is a guess the
standard forbids, and it is the highest-cost guess in the document: a
wrong answer wastes ~2.5 GB. Worse, torch is not installed yet at
decision time, so the obvious reading — ask torch whether CUDA is
available — is unavailable; the alternatives (e.g. a GPU tool, a
driver query, platform heuristics) are never named, let alone chosen
per platform. §12 Q5 admits this is unanswered. A design that leaves
its own named 2.5 GB mistake to code review has not decided it.

### B3. The verification probe is misspelled and its API is undefined — §5.2 (lines 299–332)
**What a fresh reader could not determine:** (a) the probe command
itself: the text reads "`sys.executable -m python -c ...`". `sys.executable`
already *is* the Python interpreter, so `-m python` would ask Python to
run a module named `python`; the intended command is unrecoverable from
the document alone. (b) What the loadability probe constructs: which
class (`LayaBackend`? `CodeOrchestrator`? `laya.load`?), with which
arguments (which `model_id` — the current repo's? a default?), and what
counts as pass — no exception? a non-None return? (c) What "if the
backend API allows it" resolves to: the code the design cites does
construct `LayaBackend` cheaply without loading (its `__init__` takes
`model_id` and an optional agent, and loading happens in
`_agent_or_load`), but the design never says so, and §12 Q4 leaves the
question open. A fresh reader cannot write the probe script.
**Evidence:** §5.2 step 2, "constructs the backend object without loading
the checkpoint, if the backend API allows it; otherwise the probe stops
at the import step and says so plainly" — neither branch is specified
well enough to code.

### B4. Pre-warming has no mechanism — §5.2 step 3 (lines 299–332), §6.3 (lines 506–524), §9 A2 (lines 864–890)
**What a fresh reader could not determine:** how a checkpoint is
downloaded *without grading*. The only fetch path the cited code shows
is a real grade (`laya.load` on first use, inside a model call). The
design never names the alternative: a Hugging Face download call, a
`laya.load` in the probe subprocess (which would load torch, 2–3 GB,
and take minutes — is that "warming"?), or something else. It also
never states what "warms the current repo's model only" downloads when
several repos configure different `model_id`s, nor what the warm-up
phase's input, output, and success criterion are. §6.3's "bytes
downloaded when the download reports them" presupposes a download API
that reports bytes; none is named. Option A2 therefore cannot be built
from this document.

### B5. Broken-but-present backends can never surface the Repair path — §5.5 (lines 430–451), §5.2, §2 (lines 78–160)
**What a fresh reader could not determine:** what ever runs a probe
against a *pre-existing* backend. §5.2 runs verification only "after
pip exits 0", i.e. after an install the user already started. §5.5
keys Repair on "probe fails while packages are present" — but no
section specifies a probe on page load, on dashboard start, or on any
other trigger. Meanwhile the cited availability check,
`CodeOrchestrator.available` in `backend.py`, returns True when a
backend object can be constructed, and `LayaBackend`'s constructor does
not import `laya` or torch — so a backend with present-but-broken torch
reads as available, exactly the half-installed state §4 warns about,
with no Repair button in sight. The document claims §5.5 answers the
brainstorm blocker; it defines the message, not the trigger.

### B6. Branch 2 (dedicated venv) is unimplementable for grades as written — §5.4 branch 2 (lines 373–411), §12 Q6, §8
**What a fresh reader could not determine:** how a grade ever runs in
the dedicated venv. §5.4 says "the grade runner must accept an
interpreter override; if it cannot, this branch is unavailable" — but
the cited grade path is in-process: `code_grade_runner._orchestrator`
constructs a `CodeOrchestrator` in the dashboard process, and §8 lists
no change to that path (no subprocess grade execution, no interpreter
parameter, no new module). A verification probe in the venv's
interpreter is specifiable; a *grade* in another interpreter is a
different architecture, and it is never designed. The branch therefore
collapses, on the document's own condition, to "unavailable" — but §5.4
still presents it as a live branch of the decision tree, and §8's test
plan ("decision-tree tests with detection stubbed") does not cover the
override at all.

### B7. "Dashboard state directory" is never defined, and the durable record has no format or PID — §6.5 (lines 556–579), §6.9 (lines 656–680), §5.4 (lines 373–411)
**What a fresh reader could not determine:** where anything durable
lives, or how restart detection works. The glossary (§1) does not define
"dashboard state directory"; §5.4 gives only an example,
"`<state>/.jev/cda-venv`"; §6.5 says "the dashboard state directory, not
a repo's `.jev/` directory". Candidates visible in the cited code are
`~/.jev` (e.g. the CSRF secret), the repo's `.jev/` (browser-install
log, grade ledger via `ledger_path_for_repo(settings.root)`), and
Docker's `/data` — the document picks none of them. The record's fields
(argv, target decision, started time, phase, exit code, verification
outcome) omit the one field restart detection needs: the child PID (or
equivalent liveness handle). "A record that says 'running' but has no
living child process" cannot be evaluated without it. File format
(JSON? SQLite? a file per job?), filename, and locking are likewise
absent. §6.5's fallback ("if the implementer cannot provide a durable
record … the design's restart acceptance criterion fails") concedes
that the §4 claim "answered by section 6.5" is conditional, not an
answer.

### B8. The panel's source of truth after install is unspecified and contradicts the cited availability check — §5.1 (lines 280–298), §5.2 (lines 299–332), §6.6 (lines 580–614)
**What a fresh reader could not determine:** what flips the panel, and
how flip-without-restart works. §3 promises the panel "flips to
installed without a dashboard restart when verification passes in the
running process's environment, or says a restart is needed when it
cannot". §5.2 says the panel says "Backend installed. Restart the
dashboard to use it" when the running process "still cannot see" the
backend. But the panel's value today comes from `CodeOrchestrator.available`,
a `find_spec("laya")` check plus cheap construction — not an import —
so the document never explains: does the panel re-run `available`? Does
the probe subprocess's result override `available`? What check, in the
running process, distinguishes "can see" from "cannot see", given that
the cited check never imports torch? §6.6 then asserts that after a
stopped download or a cancelled probe "the panel still says 'not
installed'" — but a partial pip run that left `laya` findable would flip
`find_spec` to True. The panel cannot simultaneously be `find_spec`
gated (as cited) and probe gated (as §6.6 assumes). §8 does not resolve
this: `backend.py` is "modify only if needed", with no criterion for
"needed". Until the source of truth is named, no row of the §6.8 matrix
can be rendered correctly.

### B9. The document's own default contradicts its own approval gate — §5.1 (lines 280–298), §9 header (lines 856–863), §8 last bullet, §10 (lines 1131–1191)
**What a fresh reader could not determine:** whether implementation may
start, and on what spec. §5.1 states a "default position … if the human
picks no option": the panel flips on layer 2 with layer 3 reported
separately — which is, in substance, option A3 (two states), not A1.
§9 then states "a set left unpicked blocks implementation of the parts
that depend on it", and §8 requires the design to be "updated with the
human's option-set decisions before implementation starts, so the
implementer works from a decided spec, not from open options". Both
cannot be true: either the §5.1 default is operative (and §9's block is
false for set A), or nothing depending on A–H may be built until all
eight sets are picked (and §§5–8, 10, which repeatedly say "per option
set C/D/E/F/G/H", are not a buildable spec at all). The acceptance
criteria in §10 are likewise unconditional ("one click installs…",
"grading can be enabled from the UI per option set D") while C3
specifies *no button* and D1 specifies *no enablement from the page*.
A fresh reader cannot tell which document state is the contract.

### B10. The Docker persistence answer is a dangling pointer — §5.4 matrix (lines 412–429), §9 G (lines 1053–1096), §3 (lines 161–198)
**What a fresh reader could not determine:** what the Docker row
actually promises. The matrix cell says "See option set G" and "the
ephemerality warning, or the volume path, per option set G". Option set
G never mentions Docker, a volume, or a volume path: G1/G2/G3 decide
version display, repair, uninstall, and the `laya` version policy only.
The cited `Dockerfile` runs as non-root `jev` with workdir `/data`, and
the cited deploy doc mentions an optional named volume `jev-state` for
*state* — but the design never states whether that volume covers
site-packages, the Hugging Face cache, or the dashboard state directory,
nor what path a container-side pip install would target. §5.4 calls the
matrix "normative" and says the button "must never be shown on Docker
without that statement" — but the statement itself is never written.
This also breaks §3's goal of "a stated persistence answer for every
deployment shape, including Docker".

### B11. The install route pair is unnamed — §6.7 (lines 615–631), §8 (lines 765–855)
**What a fresh reader could not determine:** the routes to build. §6.7
promises "a new route pair under `/code-grade/install` (status page
plus a live fragment, mirroring `/code-grade/status` and
`/code-grade/live`)". The existing pair it mirrors does *not* share a
prefix: the page is `/code-grade/status` and the fragment is
`/code-grade/live`. So is the new pair `/code-grade/install` +
`/code-grade/install/live`, or `/code-grade/install` +
`/code-grade/install-live`, or something else? HTTP methods (GET for
the page/fragment; POST for start/stop/repair/confirm?) are never
given for any install route, and §8 names a status template
(`code_grade_install_status.html`) but no fragment template filename
(by analogy `_code_grade_install_live.html`, but analogy is guessing),
no confirm template filename for H1, and no route for the §6.7 "full
log link". Route registration in `app.py` is listed as a §8 change, but
`app.py`'s registration mechanism is never described in the document.
A fresh reader cannot wire the UI.

### B12. Install/grade coordination is left to the implementer at its decision point — §6.4 (lines 525–555), §6.8 (lines 632–655)
**What a fresh reader could not determine:** whether a blocked job
waits or is refused. §6.4 says a draining grade blocks an install and
"the install may be queued or refused — queuing is the friendlier
choice, but the implementer must pick one and the UI must show which".
That is an explicit delegation of a state-machine decision, not a spec.
It also contradicts §6.4's own rule two paragraphs earlier ("the blocked
side shows 'waiting'") and §6.8's rule for the reverse direction ("a
grade submitted mid-install is refused"). If install waits but grade
refuses, the asymmetry is never justified; if a queue exists, its
storage (durable? in-memory?), its behaviour on dashboard restart, and
its cancellation are never specified. The coordination lock itself is
"owned by `DashboardCore` (or its equivalent)" — the "or its
equivalent" leaves even the owner undecided.

---

## Majors

### M1. Preflight has no computable thresholds — §6.1 (lines 454–480), §5.3 (lines 333–372)
The check must cover "the torch variant plus checkpoint plus temp
staging", but §5.3 gives no CPU-wheel size ("or the CPU wheel size for
the CPU path" — no number, no source), gives `laya` plus transformers
only as "the size pip reports for the resolved set, or 'a few hundred
MB' when it cannot be known", and gives temp staging only as "at least
1 GB". A fresh reader cannot compute the refusal threshold, cannot
state what pip command "reports" a size (no flag is named), and cannot
tell whether disk and temp requirements are summed when they share a
disk or counted separately.

### M2. Memory: refusal or warning? — §6.1 (lines 454–480)
§6.1 opens: "Each check maps to a plain-English refusal when it fails,
and no download starts after a refusal". Its memory bullet then says
"at least the 2–3 GB the backend needs at runtime, or a warning that
grading will be slow or may fail". A warning is not a refusal, and the
document never says whether a memory-short machine downloads or not.
It also never says whether "2–3 GB" means free memory, total memory,
or memory available to the dashboard process.

### M3. The failure taxonomy has no classifier — §6.2 (lines 481–505)
"The runner classifies by exit code, pip output markers, and probe
output" — but no exit codes, no markers, and no probe-output strings
are enumerated for any row (PEP 668, permission denied, no wheel,
hash mismatch, killed process). A fresh reader must invent the
classifier, and two implementations would classify the same pip log
differently. The taxonomy also omits the checkpoint-download failure
on first grade under A1/A3 (where there is no pre-warm phase to fail
in), and omits pip-lock / partial-wheel failures that §9 E1's
"fails when" clause later assumes exist.

### M4. The §6.8 state matrix is incomplete — §6.8 (lines 632–655)
Rows are missing for: Enabled=yes with install running/failed/stopped/
interrupted; Enabled=yes with "installed but not loadable"; the §5.2
"installed, restart required" state (no row shows it at all); a
preflight-refused / branch-3 guide state; a repair/reinstall running
state started from an "available" panel (see also M12: §6.8's first
rows only offer the button when not installed, so the trigger for a
reinstall from "available" is never specified); and a draining grade
during an install attempt. A matrix presented as "rows the UI must
render correctly" that omits the document's own restart-required state
cannot be implemented completely.

### M5. "Every repo" contradicts the cited single-repo dashboard — §6.9 (lines 656–680), §2, `core.py`
§6.9 requires the banner "on the Code Grade page of every repo while
an install runs" and says a finished install "flips every repo's
panel". The cited `DashboardCore` serves one repo: `settings.root` is a
single root, the grade ledger is `ledger_path_for_repo(settings.root)`,
and the status panel is built from that repo's config. The document
never explains multi-repo serving (separate dashboard processes? one
process, many roots?), so a fresh reader cannot implement cross-repo
visibility — and cannot tell which repo's `model_id` a pre-warm warms
when the banner is seen from a different repo than the one that
started the install.

### M6. Observability: three stores, no schema, and a "full log" that is capped — §6.10 (lines 681–692), §6.5, §6.7
"Structured attempt events" have no schema, no sink, and no retention
rule, and are conflated with the durable job record (§6.5) and the log
file (§6.7's "full log link", whose route is never specified — see
B11). "Redacted … following the dashboard's existing redaction
practice for displayed tokens" cites a practice that appears nowhere
in the cited files (`security.py` contains token/CSRF handling, no
redaction function). And §6.5's "full log is retained beyond the
30-line tail" contradicts §6.10's "retained (capped the same way the
browser precedent caps its log, 256 KB, when the job finishes)": a log
truncated to its last 256 KB is not the full log, and §6.5's "second
install truncates the old log only after copying it to the retained
full log" never names the retained copy's location or filename.

### M7. Shutdown: order wording, timeout, and the grade runner's actual semantics — §6.11 (lines 693–705), §8, `core.py`
§6.11 says the install runner is stopped "after the browser installer
and before or after the grade runner" and then resolves it ("grade
shutdown precedes install shutdown") — the implementer is told both to
state the order and that it is already stated. No shutdown timeout is
given for the install runner (the cited `shutdown_jobs` passes its
timeout only to launcher/chat/scheduler; browser/grade use a 30 s
default). And "stopping the grade runner first (draining)" misdescribes
the cited code: `CodeGradeRunner.shutdown` only *joins* the thread; it
does not cancel or drain — cancellation is a separate `cancel()` that
discards the result while the thread runs on. Whether shutdown cancels
grades, waits, or times out is never decided, so the install/grade
shutdown interaction cannot be built.

### M8. §8 is full of conditional and unnamed work — §8 (lines 765–855)
(a) `backend.py`: "modify only if needed" with no test for "needed"
(see B8). (b) `browser_install.py`: "modify only if option set B picks
the shared scaffold". (c) The new runner file is "new, or the
shared-scaffold file from option set B" — filename undecided.
(d) `docs/dashboard.md`: "modify … if that doc describes the page" —
a fresh reader given only the listed files cannot verify whether it
does, so the step is unexecutable as written. (e) §8 lists no change
for the config/settings storage that D2 (auto-enable) or D3 (enable
button via "the existing settings route", whose path is never given)
would require, and no change to `routes_code.code_grade_page`'s context
(which currently passes only the grade runner's `job`) to carry install
state to the template.

### M9. Test matrix: platform lists disagree, and stubbing defeats the riskiest tests — §10 (lines 1153–1168), §5.4 (lines 412–429), §8
§5.4's matrix has 8 rows; §10's platform list has 9 entries and adds
"Mac system Python", which §5.4 never rows. §10 crosses every platform
with every failure class, stop, restart, grade-during-install,
double-submit, partial recovery, verification failure, unwritable
cache, and small-tmpfs TMPDIR — but §8 says tests "stub process spawn,
environment detection, disk, and network". Stubbing detection means the
decision tree is tested against its own stubs, not against real
detection — the GPU choice (B2), PEP 668 writability, and service-account
cache writability, the three riskiest behaviours, are never actually
exercised. How a small-tmpfs TMPDIR case is tested with a stubbed disk
is never explained, and no live-verification platform beyond the Mac is
specified.

### M10. Rollback commands are described, not given — §10 (lines 1169–1191), §8
"Remove a broken backend: the pip uninstall command for `laya` in the
detected environment" is a description of a command, not a command.
No literal template is given for any environment (own interpreter,
dedicated venv, uv tool environment, Windows service, Docker), no
checkpoint-cache directory name is given (the Hugging Face hub layout
is never stated), and §8 defers the literal commands to a docs edit.
A fresh reader cannot write the page copy §10 requires ("the page
shows the one for the detected environment").

### M11. Authorization: loopback claim vs browser precedent, and phantom "requesting context" — §7.2 (lines 735–764), §2, `security.py`
§7.2 says loopback access "matches the browser-install precedent, which
is explicitly local-only" — but the cited browser-install docstring
says local installs only with the `--server` variant staying in the
terminal, while §7.2 then permits the CDA button off loopback under
the token rule. Whether the CDA button is local-only like its precedent
or token-gated-remote like other routes is therefore stated both ways.
"With the requesting context the dashboard already records" cites no
recording: the cited middleware sets session/CSRF state and the cited
runners log nothing about requesters. What is logged (session? host?
nothing?) is undefined, as is what a token-less visitor sees on the
*page* (as opposed to the route, which the middleware refuses with
401) — e.g. whether the button renders disabled, hidden, or not at
all.

### M12. Repair: button or manual command? — §5.5 (lines 430–451), §6.8, §9 G (lines 1053–1096)
§5.5 defines a "Repair action" and §6.8 renders "plus repair action
(section 5.5)". §5.5 then says "whether repair is a reinstall, a
targeted fix, or out of scope is option set G" — and for the
out-of-scope choice gives a *manual-command* message ("Reinstall it
with the command below, or remove the backend and use the Install
button"). G1 says the scope includes "repair of a failed verify" while
G2 defines a Repair action and G3 defines none, so the same word
"Repair" means three different UI elements depending on the pick, and
under the out-of-scope choice there is no Repair route for §8 to build
despite §8 listing "the repair route from section 5.5" unconditionally.

### M13. Option G's version policy is temporally impossible, and uninstall detection is undefined — §9 G (lines 1053–1096)
"G1 and G2 keep `>=0.4.15` unless the implementer finds a breaking
version during testing, in which case the policy becomes a pin and the
design is amended *before implementation*" — testing happens during
implementation, so the amendment cannot both follow testing and
precede implementation. G2's uninstall guard — "refusing uninstall when
torch was pre-existing, which the implementer must detect and state" —
never says how pre-existing torch is detected (install timestamp?
pip metadata? a marker file? user declaration?), and G2 never states
what happens to the checkpoint cache on uninstall beyond "kept, so
reinstall is faster, and stated" — stated where, and is the cache
shared with other models?

### M14. H2 contradicts §5.4's before-start rule — §9 H (lines 1097–1130), §5.4 (lines 373–411), §5.3
§5.4 requires the environment decision and persistence statement to be
"shown to the user before the install starts"; §5.3 requires sizes to
be stated "before the user clicks (the confirm step, or the button's
detail text when there is no confirm step)". H2 shows the preflight
summary — environment, sizes, persistence — only *after* the click, as
the first phase of a started install, and H2's own fails-when admits
"a user who would have declined on Docker grounds has already started
a download". The document never defines the boundary between "install
starts" (click? preflight? first download?) that would reconcile H2
with §5.4, so H2 as written violates §5.4.

### M15. F2 leaves the §4 ledger complaint half-answered — §9 F (lines 1030–1052), §4, §6.4
§4 says grade/route forms "can ledger a spurious 'unavailable' record"
and claims option set F answers it. F2 — forms stay live — guards only
jobs *refused* because an install is running ("ledgers nothing"); its
own fails-when admits "failed (not refused) jobs still ledger
'unavailable' as today". So under F2 the spurious record the §4 bullet
complains about still occurs for every ordinary missing-backend
submission. F1 would fix it by disabling the forms, but §6.8 renders
"forms per option set F" for every row, so no matrix row can be built
until F is picked — and the design never says which pick the §4 claim
depends on.

### M16. The scaffold scope ignores the third runner in the cited code — §9 B (lines 891–921), §2, `core.py`
§2 and §9 B describe "two" runners (browser, grade) and a scaffold
extracted "from the browser and grade runners". The cited `core.py`
owns a third background runner, `ReverseRunner`, alongside them (plus
scheduler/chat runners), and the concepts brief names three runners
including it. The design never says whether the scaffold covers the
reverse runner, excludes it deliberately, or leaves it as a fourth
dialect — and §8's scaffold component list (lock, thread, state,
timestamps, `busy()`, `status()`, shutdown hook, log cap) omits the
grade runner's generation counter, the mechanism that makes its
draining semantics correct. A B1 extraction built from that list would
lose a correctness mechanism the document never mentions.

### M17. §4's "answered by" map is circular for the hardest items — §4 (lines 199–277)
Several §4 bullets claim answers that the cited sections defer: stop
semantics are "answered by option set E and section 6.6", but §6.6
itself defers to E ("whether a stop button exists at all is option set
E; if E picks install-to-completion, this section's stop rows do not
apply"); restart-mid-install is "answered by section 6.5", but §6.5
contains the cannot-provide-durable-record fallback (B7); Docker is
"answered by the matrix in section 5.4", but the Docker cell points to
G, which never answers it (B10). A fresh reader following the map
arrives at an open option, not a decision.

### M18. The checkpoint probe never identifies the file — §5.2 step 3, §5.1 (lines 280–298)
"Reports whether the checkpoint file exists in the cache and is
readable" — which file? The cited docs name `model.safetensors` (bf16)
under `~/.cache/huggingface/hub` or `$HF_HOME`, but the design never
states the filename, the hub directory layout, or what "readable"
means (exists? non-empty? parses as a safetensors header? matches a
checksum?). Layer 3 therefore has no checkable definition, and two
implementations would disagree about whether a partially downloaded
checkpoint counts as "downloaded".

### M19. Preflight network checks have no method, host, or timeout — §6.1 (lines 454–480)
"Network reachability to PyPI and to the PyTorch index when the CPU
path is chosen, and to the Hugging Face host when pre-warming is
chosen" — no hostnames (the PyPI URL and Hugging Face host are never
printed in the design; the PyTorch URL appears only in the cited docs,
not in the design), no method (DNS? TCP connect? HTTP HEAD? a pip
dry-run?), no timeout, and no behaviour when reachability passes but
the actual download fails (is that a new failure class, or the
taxonomy's network row?). "Check the network or proxy" is the refusal
message, but a fresh reader cannot write the check.

### M20. E1 introduces a failure class the taxonomy lacks — §9 E1 (lines 1001–1029), §6.2
E1's fails-when says termination may leave "a pip lock or partial wheel
that makes the next run fail until the cache is cleared — the failure
taxonomy must then name cache clearing as the remedy". §6.2 has no
pip-lock row, no partial-wheel row, and no cache-clearing remedy in any
row, and §10's manual recovery lists "clear a corrupt pip cache" as a
docs item without a trigger condition (which failure class shows that
command?). The taxonomy and the option set disagree about the failure
universe.

---

## Nits

### N1. Dates and stage count do not line up — header (lines 1–12)
The design is dated 2026-10-29 but cites as an input a concepts brief
whose brainstorm review is dated 2026-10-30. The header also says
`review/` holds "five Goldfish stages", while this report is Stage 6.
A fresh reader cannot tell whether the header predates the brief, or
whether the stage numbering counts something else.

### N2. Arithmetic and timing slips — §9 C3 (lines 922–970), §5.3, §2
C3's example preflight verdict ("3.1 GB free, need about 4 GB") does not
reproduce from §5.3's numbers on the default path (2.5 GB torch +
0.842 GB checkpoint + "at least 1 GB" temp ≈ 4.3 GB, before `laya` and
transformers). Model load is "about a minute" in §5.3 and §2 but "~50 s"
in the cited code/docstring — trivial alone, but a preflight or
elapsed-time display built from either number would differ, and the
document never says which is canonical.

### N3. Glossary gaps — §1 (lines 33–69)
"Dashboard state directory", "warm-up/pre-warming", "probe", "attempt
events", "full log", and "checkpoint file" are load-bearing terms used
from §5 onward but absent from the §1 glossary, which does define
less-loaded terms such as "endpoint". "Endpoint" itself is then used in
§3 ("the same endpoint on Mac, Windows, Linux, and VPS") in a sense
closer to "the same button/API" than to its glossary definition ("one
dashboard URL that accepts a request").

### N4. Shutdown order is both undecided and decided in one paragraph — §6.11 (lines 693–705)
"Stopped from `shutdown_jobs()`, after the browser installer and before
or after the grade runner — the order matters … and the implementer
must state it: stopping the grade runner first … so grade shutdown
precedes install shutdown." A fresh reader cannot tell whether stating
the order is their job or already done; the sentence should pick one
posture. (Substance of the order is M7.)

### N5. "One-click" vs two clicks — title (line 1), §9 H1 (lines 1097–1130), §10 (lines 1133–1152)
The title and §3 promise a one-click install; H1 is explicitly a
two-click flow ("'one-click install' becomes 'two-click install'"),
and §10's acceptance criterion hedges it as "one click (plus a confirm
click if option set H picks one)". The acceptance criterion should be
written per H option, not as "one click (plus one more click)".

### N6. Acceptance machine is underspecified — §10 (lines 1133–1152), §5.4
"From a fresh 'not installed' state on the Mac live dashboard" — which
Mac shape? §5.4 distinguishes Mac venv/editable from Mac uv tool
install, with different persistence answers, and §12 Q1 leaves uv-tool
behaviour unproven. An acceptance test that does not name its Mac shape
cannot be marked done against the matrix.

### N7. Small-text persistence warning vs normative matrix — §9 H3 (lines 1097–1130), §5.4
H3 states sizes and persistence "in small text under the button before
the click", while §5.4 makes the matrix "normative" and forbids showing
the button on Docker "without that statement". The document never says
whether small text under the button satisfies the normative statement
requirement — the Docker case, where §9 H3 itself says missing the
warning "costs the most", is exactly where the weaker form is offered.

---

## What would make this document implementable

In priority order, a fresh reader needs: (1) the literal argv per path
(including the CPU-torch mechanism and the Repair argv); (2) the named
GPU-detection call per platform; (3) the literal probe script, its
constructed class and arguments, and its pass criteria; (4) the panel's
source of truth and the exact flip-vs-restart rule, reconciled with
`CodeOrchestrator.available`; (5) the dashboard state directory path,
the durable record's format (including PID), and the full-log location;
(6) a Docker persistence decision actually written in the matrix, not
pointed at option G; (7) the install route URLs, methods, and template
filenames; and (8) all eight option sets decided and folded into §§5–8
and §10, replacing every "per option set" pointer with the chosen
behaviour — or, failing that, an honest statement that the document is
a decision record awaiting picks, not yet an implementation contract.
