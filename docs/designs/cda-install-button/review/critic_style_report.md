# Style Critic Report — Stage 7, Design Critic (style lens)

**Target:** `docs/designs/cda-install-button/design.md`, feature-specification part only:
everything before section 8, "Detailed implementation plan" — that is, lines 1–764
(sections 1–7). Sections 8–12 were not reviewed, except where a line inside sections
1–7 itself refers forward to them; a forward reference is treated here as what it is
inside sections 1–7: an undecided point, not as content reviewed.

**Method:** read as a naive reader. Every term was checked for first use versus first
definition; every number was checked against the source the design itself cites
(`code_grade.html`, `backend.py`, `laya_backend.py`, `browser_install.py`,
`code_grade_runner.py`, `core.py`, `routes_code.py`, `security.py`, `learning.html`,
`pyproject.toml`, `docs/code-orchestrator.md`, `docs/deploy.md`, `Dockerfile`).
No other files were used. One limit: the route list at lines 147–150 cites
`routes_code.py` *and* `app.py`; `app.py` was not in the permitted set, so that list
could not be verified from the permitted files alone and is not counted as a finding.

**Counts: 6 Blockers, 28 Majors, 14 Nits (48 findings).**

Severity key, as applied here: **Blocker** = the wording makes the spec
self-contradictory, or misstates a security boundary, so an implementer cannot
follow it as written. **Major** = a term, number, or claim is undefined, vague,
internally inconsistent, or does not match its cited source, and will change what
gets built or shown. **Nit** = a smaller precision, redundancy, or wording defect
that does not by itself change behaviour but degrades checkability.

---

## Blockers

### B1 — "Installed" is defined by pointer, then given a default that bypasses the approval gate
- **Section/line:** §1, lines 37–38; header, lines 6–7; §5.1, lines 292–297.
- **What is wrong:** The glossary entry for the document's central term is not a
  definition at all, it is a forward pointer. Worse, §5.1 then supplies a default
  ("if the human picks no option") directly under a header that says human approval
  of the option sets "is required before any code is written". A required decision
  with a built-in default for not deciding is not a required decision.
- **Evidence:**
  > "* **Installed:** defined in section 5.1. It is not a single fact; it has three layers, and the design says which layer flips the status panel."
  > "The default position of this design, if the human picks no option, is: the panel flips on layer 2 (loadable backend)"

### B2 — `available`, `find_spec`, and "loadable" are used for three different things
- **Section/line:** §2, lines 89–92; §4, lines 230–231; §5.1, lines 284–288.
- **What is wrong:** §2 says loadability "is checked with `importlib.util.find_spec("laya")`".
  §5.1 says that same check "proves layer 1 only; layer 2 needs the subprocess probe".
  §4 then says "`available` checks only the first two" layers. All three cannot be
  true of the same check. In the cited code, `load_backend` does more than
  `find_spec`: it also constructs `LayaBackend(model_id)` (and has a separate
  `coding_decision_agent` branch), so the design never distinguishes the
  `available` property from the `find_spec` step inside it. The reader cannot tell
  what "loadable" means in any given sentence.
- **Evidence:**
  > "Loadability is checked with `importlib.util.find_spec("laya")`, deliberately without importing `laya`"
  > "(the `find_spec` check today proves layer 1 only; layer 2 needs the subprocess probe in section 5.2)"
  > "`"Installed" is three layers and `available` checks only the first two."

### B3 — The loopback security statement contradicts the cited security model
- **Section/line:** §2, lines 135–137; §7.2, lines 739–743.
- **What is wrong:** The design states, twice, that any local (page) visitor can
  reach the dashboard on loopback. The cited `security.py` says the opposite in
  the relevant respect: on loopback only requests whose `Host` header names a
  loopback address are served, "so a web page cannot reach the dashboard through
  DNS rebinding". "Any local visitor" also erases the Host check. A security
  section that broadens the stated access rule is not a simplification, it is a
  different rule. The claim that the browser-install precedent is "explicitly
  local-only" is likewise not established by the permitted browser-install files,
  which describe a fixed command and a confirm step, not a local-only restriction.
- **Evidence:**
  > "On loopback, any local page visitor can reach it."
  > "any local visitor can reach the dashboard, so any local visitor can press Install. That matches the browser-install precedent, which is explicitly local-only"

### B4 — "One-click" and "zero terminal use" are stated unconditionally, then contradicted by the design's own fallback
- **Section/line:** Title, line 1; §1, lines 19–20 and 22–24; §3, lines 177–179; §5.4, lines 394–397.
- **What is wrong:** The title and the problem statement promise one click and
  zero terminal use with no qualifier. The same spec then defines branch 3, in
  which "no install job starts" and the user is shown "the environment-specific
  command for the user to run elsewhere" — i.e. a terminal step. A promise that
  holds only on the machines where it holds is not the stated promise, and the
  unqualified form is what a reader, and a later UI copywriter, will copy.
- **Evidence:**
  > "# CDA install button: one-click backend install from the dashboard"
  > "no feature is done until it works from the browser with zero terminal use."
  > "the page shows the environment-specific command for the user to run elsewhere (the guide fallback in option set C), and no install job starts."

### B5 — The "exact", "constant" command is never printed, and its torch step contradicts itself
- **Section/line:** §3, lines 180–182; §5.3, lines 335–342 and 351–352; §7.1, lines 710–722.
- **What is wrong:** The design repeats "constant argv sequence", "exact command",
  and "exact argv sequence" but nowhere in sections 1–7 prints a single argv list:
  no pip flags, no version, no PyTorch CPU index URL value. "Exact" is therefore
  an unfulfilled claim, not a specification. In addition, §5.3 says torch is
  installed before `laya` on *both* paths, while §7.1 says "The argv installs
  `laya` (and torch first, on the CPU path) only" — read literally, torch-first
  happens only on the CPU path.
- **Evidence:**
  > "The fixed-command rule: the install command is a constant argv sequence on the server."
  > "On a machine with a CUDA-capable GPU, install the default torch wheel (about 2.5 GB of CUDA libraries) then `laya`."
  > "The argv installs `laya` (and torch first, on the CPU path) only."

### B6 — The Backend panel field is defined as binary, then given a third value that violates its own ban
- **Section/line:** §1, lines 75–76; §5.1, lines 296–297; §5.5, lines 437–450; §6.8, line 645.
- **What is wrong:** The status-panel definition allows exactly two Backend values,
  "available" or "not installed". §5.5 then adds "installed but not loadable" as
  a panel state, and §6.8 renders it as a Backend value. That label shows the word
  "installed" for a backend that has only passed presence — precisely what §5.1
  forbids: "The panel must never show 'installed' on layer 1 alone." The spec
  bans a label and then mandates it.
- **Evidence:**
  > "* **Backend:** whether the backend can be used right now (available) or not (not installed)."
  > "The panel must never show "installed" on layer 1 alone."
  > "The panel shows "installed but not loadable" with a Repair action."

---

## Majors

### M1 — Infrastructure jargon is used throughout but never defined, for a stated terminal-free reader
- **Section/line:** §1, lines 29–31 (audience) and 53–57; §2, lines 101–106; §4, line 232; §5.4, lines 378–382; §6.1, line 470.
- **What is wrong:** The intended reader "never touches a terminal", yet the spec
  uses, without any glossary entry: CUDA, wheel, Hugging Face (cache / model id),
  venv, uv / uv tool environment, PEP 668, PyPI, PyTorch index, argv, subprocess,
  tmpfs / RAM disk, DNS, proxy, checksum. Each is load-bearing: PEP 668 decides a
  whole branch, CUDA decides a 2.5 GB download, the Hugging Face cache decides
  where the checkpoint lives. A terminal-free reader cannot check any of them.
- **Evidence:**
  > "The intended reader is a person who never touches a terminal."
  > "The default wheel carries about 2.5 GB of CUDA libraries; the CPU wheel is smaller."
  > "PEP 668 refuses system-Python pip installs"

### M2 — Dashboard/UI vocabulary is used before it is defined, or never defined
- **Section/line:** §2, lines 126 and 135–137; §6.7, lines 625–630; §7.1, line 715.
- **What is wrong:** "live region", "flash" (grade-job flash), "banner", "loopback",
  and "CSRF" all appear as if the reader already knows them. "Loopback" is first
  used in §2 with no definition and is absent from the §1 glossary, even though
  §7.2 turns on it. "CSRF token" first appears in §7.1; §2 only says "per-session
  token", so the reader cannot tell whether they are the same token until §7.2
  says so by implication.
- **Evidence:**
  > "with a status page (`/code-grade/status`) whose live region polls the job."
  > "mirroring the existing grade-job flash on the panel."
  > "The form posts only the CSRF token and, when a confirm step exists, the confirmation."

### M3 — Grading vocabulary is used before it is defined, or never defined
- **Section/line:** §1, lines 42–48; §4, lines 207–209 and 258–259; §3, line 187.
- **What is wrong:** "grade ledger" (line 44) is never defined anywhere in sections
  1–7, although the ledger rule is a goal. "Merge-action", "model tier", "skill",
  and "fail-closed" are likewise undefined on first use; "fail-closed" is then
  load-bearing in §4's central contrast (inference error blocks the gate, missing
  backend does not), but §2's summary of `available` omits the block / does-not-block
  distinction that contrast depends on. "Warm-up" / "pre-warming" is used as a goal
  in §3 before any definition in §5.2/§6.3.
- **Evidence:**
  > "A grade is not an install outcome, and an install outcome is never written to the grade ledger."
  > "the confidence bar a grade must clear before its merge-action or routing hint is used."
  > "which blocks the quality gate fail-closed, where a missing backend would have let the run proceed."

### M4 — "Grade" and "Route" overlap: a routing hint is both
- **Section/line:** §1, lines 42–46.
- **What is wrong:** "Grade" is defined as "one grading of a diff, or one routing
  hint", and "Route" is then defined as "a routing hint … produced by the same
  backend". By those two sentences a routing hint is simultaneously a grade and
  a route. The rest of the spec uses "grade" and "route" as distinct jobs and
  forms (§6.4, §6.8), so the glossary collision propagates.
- **Evidence:**
  > "* **Grade:** one grading of a diff, or one routing hint, produced by the backend."
  > "* **Route:** a routing hint (which model tier and skill a coding task needs), produced by the same backend."

### M5 — "Backend" is defined three ways
- **Section/line:** §1, lines 35–36; §5.1, lines 284–290; §6.9, lines 670–672.
- **What is wrong:** The glossary says the backend is "`laya` plus torch, plus a
  trained model checkpoint" — one thing including the checkpoint. §5.1 then
  separates the checkpoint as layer 3, distinct from the loadable backend.
  §6.9 then calls the installed thing "the backend runtime (`laya` + torch)",
  excluding the checkpoint again. Whether "install the backend" includes the
  checkpoint cannot be answered from the definitions.
- **Evidence:**
  > "* **Backend:** the CDA grading engine, provided by the `laya` package plus torch, plus a trained model checkpoint."
  > "The install installs the backend runtime (`laya` + torch); the checkpoint for a repo's model downloads on that repo's first grade"

### M6 — "Environment" is defined as the dashboard's own, then branch 2 installs somewhere else
- **Section/line:** §1, lines 39–41; §5.4, lines 383–393.
- **What is wrong:** The glossary definition restricts the environment to "the
  Python environment the dashboard process itself runs from" and says the install
  targets "that environment, never an environment found on PATH". Branch 2 then
  installs into "a dedicated venv owned by the dashboard" that the dashboard
  process does *not* run from, and requires an interpreter override so grades
  can use it. The definition and the decision tree disagree about what the word
  covers.
- **Evidence:**
  > "* **Environment:** the Python environment the dashboard process itself runs from. The install targets that environment, never an environment found on PATH."
  > "A dedicated venv owned by the dashboard, when the own environment is not writable."

### M7 — "Endpoint" drifts from a URL to the button itself
- **Section/line:** §1, lines 63–64; §3, lines 177–179; §7.1, line 731.
- **What is wrong:** "Endpoint" is defined as "one dashboard URL that accepts a
  request". §3 then promises "The same endpoint on Mac, Windows, Linux, and VPS",
  where "endpoint" is plainly being used for the button/feature, not for a URL
  (the URL does not vary by OS). §7.1 then uses "install endpoint" for the
  server-side handler. One word, three referents, in a section that is supposed
  to pin down a security boundary.
- **Evidence:**
  > "* **Endpoint:** one dashboard URL that accepts a request (for example a form post)."
  > "The same endpoint on Mac, Windows, Linux, and VPS, with a stated persistence answer for every deployment shape"

### M8 — Log cap unit does not match the cited source: 256 KB vs 256 KiB
- **Section/line:** §2, lines 116–117; §6.10, lines 686–687.
- **What is wrong:** The cited code caps the finished log at `_MAX_LOG_BYTES =
  262_144` and describes it as "the last 256 KiB". The design writes "256 KB"
  in both places. 256 KB (256,000 bytes) is not 256 KiB (262,144 bytes). A spec
  that copies a number should copy its unit.
- **Evidence:**
  > "the log is capped at 256 KB when the install finishes"
  > "The full log is retained (capped the same way the browser precedent caps its log, 256 KB, when the job finishes)"

### M9 — The 2–3 GB figure is misattributed, then turned into a range-minimum
- **Section/line:** §5.2, lines 308–310; §6.1, lines 465–467.
- **What is wrong:** The cited doc gives "roughly 2–3 GB RSS for torch + the
  bf16 [model]" — torch *plus model*, as resident memory, roughly. §5.2
  attributes the same 2–3 GB to importing torch alone ("would also pull torch
  (2–3 GB) into the server process"), and §6.1 turns the rough range into a
  threshold: "at least the 2–3 GB". "At least" a range is not a checkable
  threshold, and the attribution change understates what the figure covers.
- **Evidence:**
  > "importing inside the dashboard process would also pull torch (2–3 GB) into the server process."
  > "Free memory: at least the 2–3 GB the backend needs at runtime, or a warning that grading will be slow or may fail"

### M10 — A dev-VM measurement is universalised to every platform
- **Section/line:** §2, lines 101–103; §5.3, lines 362–365.
- **What is wrong:** The cited doc qualifies the tmpfs fact: "the default /tmp
  is a 512M tmpfs on the dev VM". The design drops the qualifier in §2 and
  then hardens it in §5.3 into a rule "on every platform", including Windows,
  where `/tmp` does not exist — patched only by a vague "equivalent". A
  VM-specific measurement has become a cross-platform requirement without
  evidence.
- **Evidence:**
  > "which fails with "No space left on device" on a 512M tmpfs `/tmp` unless TMPDIR points at a real disk."
  > "run with TMPDIR set to a directory on a real disk (home disk by default), never a 512M tmpfs `/tmp`, on every platform. On Windows the equivalent is a temp directory on a real disk, not a RAM disk."

### M11 — "Honest sizes" contains a placeholder, an invented figure, and no total
- **Section/line:** §4, lines 225–227; §5.3, lines 351–362.
- **What is wrong:** Under a heading promising honest sizes, the CPU wheel size
  is never given ("or the CPU wheel size for the CPU path" — no number);
  `laya` plus transformers is given as "a few hundred MB when it cannot be known
  in advance", an invented range with no cited source; temp staging is given as
  "at least 1 GB", also uncited (the cited sources give only 512M tmpfs and an
  842 MB checkpoint, from which 1 GB does not follow as stated); and §4's
  "several gigabytes" is never replaced by a summed total, although the parts
  listed (2.5 GB + 842 MB + a few hundred MB + 1 GB) sum to well over 4 GB.
  Preflight in §6.1 then says the check must cover "torch variant plus checkpoint
  plus temp staging" without ever stating that total either.
- **Evidence:**
  > "* `laya` plus transformers: the size pip reports for the resolved set, or "a few hundred MB" when it cannot be known in advance."
  > "* Temp staging during install and checkpoint reconstruction: at least 1 GB of free space in the temp directory, on top of the above."
  > "The true footprint is several gigabytes, not ~842 MB."

### M12 — The verification command as written is wrong
- **Section/line:** §5.2, lines 304–306; contrast §5.4, lines 378–379.
- **What is wrong:** The probe is written as "`sys.executable -m python -c ...`".
  `sys.executable` already *is* the Python interpreter, so `-m python` is
  nonsense as written; the design's own correct form, two sections later, is
  "`sys.executable -m pip`". A spec command that cannot be typed as written
  will be copied wrongly or "fixed" silently by the implementer.
- **Evidence:**
  > "A fresh subprocess running the dashboard's own interpreter (`sys.executable -m python -c ...`) imports `laya` and torch"
  > "The pip command is `sys.executable -m pip`, never a `pip` found on PATH."

### M13 — The GPU-detection example is circular and the detection call is unnamed
- **Section/line:** §5.3, lines 344–349.
- **What is wrong:** Detection is "for example, whether CUDA is reported
  available" — reported by whom, before torch is installed? The only thing that
  could report CUDA availability in the usual sense is torch itself, which is
  precisely what has not been installed yet. The design admits the exact call
  "is an implementation detail the implementer must name in code review", while
  calling a wrong detection "the 2.5 GB mistake this section exists to prevent".
  A section that exists to prevent a mistake cannot leave its only detection
  mechanism as an unimplementable example.
- **Evidence:**
  > "Detection: the sequence is chosen server-side from platform and GPU detection done by the dashboard process (for example, whether CUDA is reported available), never from a browser claim. The exact detection call is an implementation detail"

### M14 — Matrix rows that only point at another row are not UI statements, and one misdescribes its branch
- **Section/line:** §5.4, lines 419–423; §5.4, lines 425–428.
- **What is wrong:** The matrix is declared normative: an implementation showing
  the button "without the row's UI statement has not implemented this design".
  Yet the Windows-service, Linux-system-Python, and VPS rows contain no statement
  at all, only "As Windows service" / "Branch 2: as Mac venv" / "As Windows
  service, plus the cache location". Worse, "as Mac venv" copies the Mac-venv
  text ("Installs into the dashboard's Python environment") onto branch 2, whose
  target is a *dedicated* venv — the opposite of the dashboard's own environment.
  The normative rule is violated by the table that states it.
- **Evidence:**
  > "| Windows, service | Branch 2 if a writable state path exists, else branch 3 | Branch 2 survives service restarts; the venv path is recorded and re-discovered | Branch 2: as Mac venv. Branch 3: the command to run as the service owner. |"
  > "| Linux, system Python (PEP 668) | Branch 2, else branch 3 | As Windows service | As Windows service. |"

### M15 — "Constant" argv versus a builder with three variable inputs
- **Section/line:** §3, lines 180–182; §7.1, lines 710–720.
- **What is wrong:** §3 and §7.1 call the command "a constant argv sequence".
  §7.1 then lists the argv builder's inputs: `sys.executable`, the torch-path
  decision, and the `laya` version policy — all of which vary by machine and by
  undecided option. "Constant" here can only mean "not taken from the request",
  but the word as written claims the sequence itself does not vary, which it
  does. The precise claim ("never built from request data") is present in the
  next sentence and should replace the imprecise one.
- **Evidence:**
  > "The install command is a constant argv sequence built on the server from the environment decision in section 5.4. It is never built from request data."
  > "The argv builder takes only server-side inputs: `sys.executable`, the torch path decision (CPU vs default, from server-side detection), and the `laya` version policy from option set G."

### M16 — Single-flight concurrency is mislabelled "rate limiting"
- **Section/line:** §7.2, lines 749–751.
- **What is wrong:** "Rate limiting: one install at a time (single-flight) is
  the limit. No additional rate limit is specified". One-at-a-time is a
  concurrency limit, not a rate limit: it bounds simultaneous pip processes, not
  how often installs may be attempted over time. After one install finishes,
  nothing in the stated rule stops immediate, repeated re-attempts. In a
  security section, mislabelling the control overstates the protection.
- **Evidence:**
  > "Rate limiting: one install at a time (single-flight) is the limit. No additional rate limit is specified; a second submit sees the running job (section 6.8), which bounds abuse to one pip process."

### M17 — The shutdown order sentence contradicts itself, then resolves itself
- **Section/line:** §6.11, lines 695–700.
- **What is wrong:** The runner is stopped "after the browser installer and
  before or after the grade runner" — "before or after" states no order, in a
  paragraph whose first claim is that "the order matters". The sentence then
  resolves the order anyway ("grade shutdown precedes install shutdown"). The
  reader is told the order both matters and is undecided and is grade-first.
  The cited `core.py` order (scheduler, browser, reverse, grade) is also never
  mentioned, so where the new runner fits relative to the other runners is
  unstated.
- **Evidence:**
  > "The install runner is owned by `DashboardCore` and stopped from `shutdown_jobs()`, after the browser installer and before or after the grade runner — the order matters because of section 6.4, and the implementer must state it: stopping the grade runner first (draining) and then the install runner (terminating pip) avoids terminating pip while a grade still holds torch files, so grade shutdown precedes install shutdown."

### M18 — "Safe to retry" is defined negatively, not by the environment's state
- **Section/line:** §6.6, lines 587–593.
- **What is wrong:** After a stop during download/install, "safe" is defined
  only as "no half-installed package is reported as installed (verification has
  not run, so the panel still says 'not installed')". That is a statement about
  what the panel *reports*, not about the environment: the same paragraph admits
  "partial unpacks are left for the next pip run to overwrite". A panel that
  does not report a half-installed package is not the same as an environment
  that is safe to retry, and the definition invites the exact misreading §5.1
  warns against (reporting state confused with actual state).
- **Evidence:**
  > "Partial downloads stay in pip's cache where pip can reuse them; partial unpacks are left for the next pip run to overwrite. The state after stop is "stopped, safe to retry", where "safe" means: no half-installed package is reported as installed"

### M19 — The "full log" is capped, so it is not full
- **Section/line:** §6.5, lines 563–564; §6.10, lines 686–687.
- **What is wrong:** §6.5 says "The full log is retained beyond the 30-line
  tail". §6.10 says that log is "capped … 256 KB, when the job finishes".
  A log truncated to 256 KB is not the full log; §6.5 also says a retry
  "truncates the old log only after copying it to the retained full log", so
  the retained artefact is a capped copy of a truncated log. "Full" should be
  replaced by the actual retention rule everywhere it appears.
- **Evidence:**
  > "The full log is retained beyond the 30-line tail (section 6.10)."
  > "The full log is retained (capped the same way the browser precedent caps its log, 256 KB, when the job finishes)"

### M20 — The blocked-install behaviour is left undecided, with a subjective nudge
- **Section/line:** §6.4, lines 541–545.
- **What is wrong:** When a draining grade blocks an install, "the install may
  be queued or refused — queuing is the friendlier choice, but the implementer
  must pick one". A feature spec that leaves the visible behaviour (queued vs
  refused) to the implementer, while §6.8's matrix and §6.7's banner assume a
  definite behaviour, is not decided. "Friendlier" is a subjective adjective
  doing the work of a requirement: it recommends without requiring, so either
  choice can later be defended as compliant.
- **Evidence:**
  > "the install may be queued or refused — queuing is the friendlier choice, but the implementer must pick one and the UI must show which."

### M21 — §3's restart condition does not match §5.2's restart case
- **Section/line:** §3, lines 169–173; §5.2, lines 326–331.
- **What is wrong:** §3 says the panel flips without a restart "when verification
  passes in the running process's environment". §5.2's actual case is the
  opposite split: verification passes *in a fresh subprocess* while "the running
  dashboard process still cannot see the backend (import cache)", and then a
  restart *is* required. Verification passing does not entail the running
  process seeing the backend — that is the whole import-cache problem §5.2
  exists to state. §3's condition, read literally, would flip the panel in
  exactly the case §5.2 says must show a restart message.
- **Evidence:**
  > "The status panel flips to installed without a dashboard restart when verification passes in the running process's environment, or says a restart is needed when it cannot."
  > "If verification passes but the running dashboard process still cannot see the backend (import cache), the panel says so explicitly: "Backend installed. Restart the dashboard to use it.""

### M22 — The design is dated before the brief it cites
- **Section/line:** Header, lines 4 and 8–9.
- **What is wrong:** The design is dated 2026-10-29 and lists as an input
  "`concepts_brief.md` in this directory (brainstorm, six Goldfish lenses,
  2026-10-30)" — a document event dated a day *after* the design itself. Either
  the design date or the brief date is wrong as written; a reader checking
  provenance cannot tell which input version this design used.
- **Evidence:**
  > "* **Date:** 2026-10-29"
  > "* **Inputs:** `concepts_brief.md` in this directory (brainstorm, six Goldfish lenses, 2026-10-30) and the code files named in section 2."

### M23 — The job-record location is three different places
- **Section/line:** §5.4, lines 384–388; §6.5, lines 558–560; §6.9, lines 663–667.
- **What is wrong:** The dedicated venv lives at "a fixed path under the
  dashboard's state directory (for example `<state>/.jev/cda-venv`". `<state>`
  is never defined: is the state directory itself `.jev`, does it contain
  `.jev`, or is `<state>` a placeholder for a repo root? §6.5 says the job
  record lives in "the dashboard state directory, not a repo's `.jev/` directory",
  and §6.9 repeats "the dashboard state directory, not under any repo's `.jev/`
  directory" — but the §5.4 example path puts `.jev` *inside* the state path.
  The reader cannot tell whether `.jev` is the state directory, a child of it,
  or something else entirely.
- **Evidence:**
  > "Location: a fixed path under the dashboard's state directory (for example `<state>/.jev/cda-venv`"
  > "It lives in a fixed location (the dashboard state directory, not a repo's `.jev/` directory"

### M24 — Vague quantities do the work of requirements
- **Section/line:** §5.3, lines 367–371; §5.4, lines 381–382; §6.1, lines 462–464.
- **What is wrong:** "minutes on a normal connection", "longer on a slow one",
  "the normal case", "a small VPS", "grading will be slow or may fail" — none
  of "normal", "small", "slow", or "longer" is quantified, although §6.1 turns
  the small-VPS case into a mandatory check ("this check is not optional") and
  §5.3 turns "minutes, not seconds" into UI copy. A preflight threshold built
  on "small" cannot be tested; a time promise built on "normal" cannot be
  falsified.
- **Evidence:**
  > "Time: pip install takes minutes on a normal connection and longer on a slow one"
  > "This is the normal case for a venv, an editable checkout, and a uv tool environment."
  > "On a small VPS, a pip run that fills the disk can break the host itself, so this check is not optional."

### M25 — Absolute claims that the spec itself cannot guarantee
- **Section/line:** §3, lines 174–175; §6.3, line 522; §6.5, lines 570–571; §5.2, lines 330–331.
- **What is wrong:** "never a bare traceback", "Closing the tab loses nothing",
  "orphaned children are never left running past a dashboard stop", "A restart
  must never be silently required". Each absolute fails on an ordinary case the
  spec itself names: a dashboard killed outright cannot terminate its children
  on shutdown; a tab closed mid-flush can lose unflushed output; a log capped
  at 256 KB (M19) means closing the tab and returning does lose detail. Absolutes
  should be scoped to the mechanism that enforces them ("on clean shutdown",
  "the durable record survives tab close"), not stated without bound.
- **Evidence:**
  > "Failure states in plain English with a retry, never a bare traceback."
  > "Closing the tab loses nothing: the job record is durable (section 6.5) and the status page shows the same state on return."
  > "orphaned children are never left running past a dashboard stop."

### M26 — "Button becomes guide" does not say what happens to the button
- **Section/line:** §5.4, lines 394–397; §6.2, lines 493–494.
- **What is wrong:** Branch 3 says "no install job starts" and the page "shows
  the environment-specific command". The failure table then says, for permission
  and PEP 668 failures, "No (button becomes guide)". Does the Install button
  remain and change its action, disappear, or become a link to the guide? The
  spec uses "button", "guide", and "command" as if they were one UI element,
  and §6.8's matrix (which lists an Install button in the "none" rows) never
  shows the guide state. An implementer cannot render "becomes" from this.
- **Evidence:**
  > "| Permission denied on target | "The dashboard cannot write to its Python environment." plus the branch-3 command | No (button becomes guide) | Permissions, or run the command shown |"

### M27 — Logging claims name no actual recorded fields
- **Section/line:** §6.10, lines 688–691; §7.2, lines 752–754.
- **What is wrong:** Logs and events are said to carry "the requesting context
  the dashboard already records" — but no field is named, and the permitted
  files do not establish that the dashboard records a per-request identity or
  origin context for form posts beyond the session/CSRF mechanism. "Any
  credential-looking token in a URL is redacted … following the dashboard's
  existing redaction practice for displayed tokens" likewise cites a practice
  that is never defined: what counts as "credential-looking", and where is that
  practice stated for *stored logs* (as opposed to displayed tokens)? Both
  sentences sound precise and check nothing.
- **Evidence:**
  > "is logged in the attempt events (section 6.10) with the requesting context the dashboard already records."
  > "pip output is stored as-is except that any credential-looking token in a URL is redacted before storage, following the dashboard's existing redaction practice for displayed tokens."

### M28 — State-word drift across the install lifecycle
- **Section/line:** §2, lines 126–128; §6.7, lines 628–630; §6.8, lines 640–648; §6.11, lines 701–704.
- **What is wrong:** The same phase is called "running" in the matrix ("Install
  job: running"), "installing" in the panel's short state words, and "running
  (reinstall/repair)" in the last matrix row — while the grade precedent uses
  "cancelled" plus "draining", the browser precedent uses "stopped", and
  shutdown uses "interrupted (not `stopped`, because the user did not stop
  it)". The spec inherits the three-dialect drift the brainstorm flagged, and
  adds "reinstall" in §6.8 without ever defining it against "repair" in §5.5.
  The matrix also switches the Backend column between "not installed" and
  "available (layer 2)" where §5.1's panel value would be "installed".
- **Evidence:**
  > "The status panel itself shows the install state in the Backend row area only as a short state word ("installing", "failed", "stopped", "interrupted")"
  > "| no | not installed | running | Banner "installing…", button disabled, forms per option set F |"
  > "the job record is marked `interrupted` (not `stopped`, because the user did not stop it)"

---

## Nits

### N1 — Precision drift on the checkpoint size
- **Section/line:** §1, line 56; §4, line 227.
- **What is wrong:** The same figure is "about 842 MB" in the glossary and
  bare "842 MB" in §4. The cited sources consistently write "~842 MB". Pick
  one form and keep it; a spec that drops "about" mid-document invites a reader
  to treat an approximation as exact.
- **Evidence:**
  > "* **Checkpoint:** the trained model file, about 842 MB, downloaded into the Hugging Face cache on first use."
  > "transformers, the 842 MB checkpoint, and temp staging."

### N2 — "The browser downloads nothing" is literally false as written
- **Section/line:** §1, line 25.
- **What is wrong:** The browser downloads the pages, the status fragment, and
  the log tail — that is how progress is "shown in the browser". The intended
  meaning (the install payload is not downloaded to the browser machine) needs
  that qualifier; as written, the sentence contradicts the progress story in
  the same paragraph.
- **Evidence:**
  > "The browser downloads nothing. The install runs where the dashboard server runs"

### N3 — "The exact path is an implementation detail" is an oxymoron
- **Section/line:** §5.4, lines 385–387.
- **What is wrong:** A path given only as an example ("for example
  `<state>/.jev/cda-venv`") cannot simultaneously be "one fixed path" whose
  "exact path is an implementation detail". Either the spec fixes the path or
  it delegates it; "exact … detail" pretends to do both.
- **Evidence:**
  > "the exact path is an implementation detail, but it must be one fixed path, recorded in the durable job record"

### N4 — "The current pip line" is undefined
- **Section/line:** §6.3, lines 514–515.
- **What is wrong:** Progress shows "the current pip line" alongside the log
  tail, but pip emits many lines and the spec never says which line counts as
  current (last line? last non-blank line? last line matching a pattern?).
  Combined with the 30-line tail, "current line" is either redundant or
  undefined.
- **Evidence:**
  > "* **Downloading and installing** — the log tail (last 30 lines, as the browser precedent), elapsed time, and the current pip line."

### N5 — "The raw log stays available as the log tail" conflates tail with log
- **Section/line:** §6.2, lines 484–486.
- **What is wrong:** The taxonomy says raw pip output is never the message
  because "(the raw log stays available as the log tail)". The log tail is by
  definition the last 30 lines, not the raw log; the full (capped) log is a
  different artefact, linked from the status page per §6.10. The parenthetical
  reassures the reader with the wrong artefact.
- **Evidence:**
  > "The runner classifies by exit code, pip output markers, and probe output — never by showing pip's raw output as the message (the raw log stays available as the log tail)."

### N6 — Meta-sentences that assert their own success
- **Section/line:** §5.4, lines 425–427; §6.5, lines 575–578; §7.1, lines 731–733.
- **What is wrong:** "The matrix is normative: an implementation that shows the
  button … has not implemented this design" defines compliance circularly;
  "the fallback is stated here so it is a decision, not an accident" labels a
  failure mode as a decision without changing it; "This section exists so that
  failure mode is closed by construction, not by convention" claims closure
  that the surrounding bullets must establish, not the sentence itself. All
  three can be deleted without losing a requirement.
- **Evidence:**
  > "The matrix is normative: an implementation that shows the button on a deployment shape without the row's UI statement has not implemented this design."
  > "This section exists so that failure mode is closed by construction, not by convention."

### N7 — A tautology about restart
- **Section/line:** §5.2, lines 330–331.
- **What is wrong:** "A restart must never be silently required: the panel
  states it, or the flip happens without one." The two branches cover every
  case (either a restart is stated, or no restart is needed), so the sentence
  forbids nothing and requires nothing beyond the preceding sentence. Delete
  it or state the actual rule (which case triggers which branch).
- **Evidence:**
  > "A restart must never be silently required: the panel states it, or the flip happens without one."

### N8 — A "green badge" appears from nowhere
- **Section/line:** §5.5, lines 447–450.
- **What is wrong:** "hiding it behind a green badge is the one outcome this
  design forbids" introduces a coloured badge that no earlier section defines:
  the panel's Backend row is described as text values ("available",
  "not installed"), with no colour mapping. A forbidden outcome should be
  stated in the vocabulary the spec itself uses ("reported as 'installed'
  with no action", which the same paragraph already says).
- **Evidence:**
  > "That is the half-installed state section 4 warns about, and hiding it behind a green badge is the one outcome this design forbids."

### N9 — "A check afterwards" in §1 is never pinned down there
- **Section/line:** §1, lines 22–25.
- **What is wrong:** "One click installs the backend … with progress shown in
  the browser and a check afterwards." "A check" is vague until §5.2 defines
  verification five sections later, and §1's own glossary does not list it.
  The problem statement should name the check (verification in a fresh
  subprocess) or drop the phrase.
- **Evidence:**
  > "One click installs the backend on the machine the dashboard runs on, in the background, with progress shown in the browser and a check afterwards."

### N10 — Confirm vocabulary drifts between sections
- **Section/line:** §2, lines 119–120; §7.2, lines 761–763.
- **What is wrong:** §2 calls the browser precedent's confirm "a separate
  template, naming the exact command". §7.2 calls the same thing "the confirm
  step (whether the button asks first, naming the exact command, as the browser
  precedent does)". "Separate template", "confirm step", "asks first", and
  "confirmation" ( §7.1) are used for one mechanism without ever being equated.
  Small, but in a spec where the confirm step is an undecided option set, the
  drift makes it unclear what exactly is being opted into.
- **Evidence:**
  > "The button asks for confirmation first on a separate template, naming the exact command."
  > "The confirm step (whether the button asks first, naming the exact command, as the browser precedent does) is option set H."

### N11 — "Live region" versus "live fragment"
- **Section/line:** §2, line 126; §6.3, line 521; §6.7, lines 621–623.
- **What is wrong:** The grade precedent's progress is a "live region" that
  "polls the job"; the install surface is a "status page plus a live fragment,
  mirroring `/code-grade/status` and `/code-grade/live`". "Region" and
  "fragment" are never equated, and no poll interval is given for either,
  although the cited settings default to a 3-second refresh. One term should
  cover one mechanism.
- **Evidence:**
  > "whose live region polls the job."
  > "A new route pair under `/code-grade/install` (status page plus a live fragment, mirroring `/code-grade/status` and `/code-grade/live`)"

### N12 — A placeholder stands in for UI copy
- **Section/line:** §6.8, line 641.
- **What is wrong:** The matrix's failed row shows `"Install failed: <message>. Retry."`.
  `<message>` is not copy; it is a hole where copy belongs, in a matrix whose
  purpose is to pin down exactly what the panel shows. The failure table in
  §6.2 has the actual messages — the matrix should cite one or say which one.
- **Evidence:**
  > "| no | not installed | failed | "Install failed: <message>. Retry." plus log link |"

### N13 — "Honest" as a label for numbers
- **Section/line:** §5.3, lines 351–352; §6.3, line 506.
- **What is wrong:** "Honest sizes" and "Progress honesty" use a moral adjective
  where a measurable property is needed: stated before the click, sourced,
  with a total. As M11 shows, the sizes under the "honest" heading include an
  invented range and a missing figure, so the label does not describe the
  content. Replace the label with the requirement ("sizes stated before the
  click, with source and total").
- **Evidence:**
  > "Honest sizes the UI must state before the user clicks (the confirm step, or the button's detail text when there is no confirm step):"
  > "### 6.3 Progress honesty: named phases, no invented percentage"

### N14 — The preflight example list is itself vague
- **Section/line:** §6.1, lines 477–479.
- **What is wrong:** Preflight results are shown as `("Disk: enough", "Network:
  reachable")`. "Enough" for what — the unstated total from M11? "Reachable"
  to which host — PyPI, the PyTorch index, the Hugging Face host, or all three,
  which §6.1 distinguishes? An example that is vaguer than the rule it
  illustrates does not pin down the UI.
- **Evidence:**
  > "Preflight results are shown as a short list ("Disk: enough", "Network: reachable") so a refusal names the failing check, not just "preflight failed"."
