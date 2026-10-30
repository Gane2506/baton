# EGM Stage 9 — Implementation Readiness Test, codebase-details lens

Target: `docs/designs/cda-install-button/design.md`, section 8
(Detailed implementation plan) and section 10 (Acceptance criteria /
test matrix / rollback). Sections 1–7, 9, 11, 12 were read only as
context for the option sets section 8 depends on.

Code checked (only the files the task allowed): `src/jev_agent/dashboard/
templates/code_grade.html`, `src/jev_agent/code_orchestrator/backend.py`,
`src/jev_agent/code_orchestrator/laya_backend.py`,
`src/jev_agent/dashboard/browser_install.py`,
`src/jev_agent/dashboard/code_grade_runner.py`,
`src/jev_agent/dashboard/core.py`, `src/jev_agent/dashboard/routes_code.py`,
`src/jev_agent/dashboard/security.py`,
`src/jev_agent/dashboard/templates/learning.html`, plus `pyproject.toml`,
`docs/code-orchestrator.md`, `docs/deploy.md`, `Dockerfile`.
Directory listings were used only to confirm that `app.py`,
`code_grade_status.html`, and `_code_grade_live.html` exist; their
contents (other than what `routes_code.py` itself shows) were not read.

Verdict: **not implementation-ready as written.** Section 8's own
contract is "every file it touches, and why", but it omits a file the
plan needs, names the wrong file for another change, leaves route URLs,
a template filename, a class API, a durable-record location, and test
seams unnamed, and it incorporates a shutdown narrative that
contradicts the code. An implementer following section 8 literally
would build the wrong shutdown order, the wrong probe home, and an
incomplete route set — and would still have to invent the rest.

## What checked out (so the plan is not re-litigated on these points)

- The three pip-instruction surfaces are real and exactly as named:
  `code_grade.html` (panel warning), `code_grade_runner.unavailable_message()`
  (via `_INSTALL_GUIDANCE`, which prints
  `pip install "jev-agent[code-orchestrator]"`), and
  `templates/learning.html:286` (Ratings-tab empty state).
- The mirror templates exist: `routes_code.py` renders
  `code_grade_status.html` (`code_grade_status`) and
  `_code_grade_live.html` (`code_grade_live`), and the existing route
  methods are `/code-grade`, `/code-grade/grade`, `/code-grade/route`,
  `/code-grade/status`, `/code-grade/live`, `/code-grade/cancel`.
- `checked_form` exists in `security.py` and is used by the grade,
  route, and cancel handlers in `routes_code.py`.
- `BrowserInstallRunner` has the claimed seams and numbers: injectable
  `spawn`, `busy()`, `status()`, `shutdown(timeout=30.0)`,
  30-line tail, 256 KiB finish cap, `stop()` terminating the process.
- `pyproject.toml` confirms the extra is `laya>=0.4.15`, unpinned
  above; `docs/code-orchestrator.md` confirms the CPU-torch-first
  sequence and the ~2.5 GB default-wheel figure; `Dockerfile` /
  `docs/deploy.md` confirm non-root user `jev`, workdir `/data`, and
  the optional `jev-state` volume.
- `DashboardCore.shutdown_jobs()` exists in `core.py` with the order:
  scheduler, browser installer, reverse runner, grade runner, codex
  flow, claude flow. `app.py` exists (listing only).

## Blockers

### B1. Section 8's shutdown order rests on a false "draining" claim about the grade runner

- **Design location:** section 8, core bullet (~line 780: "wire it
  into `shutdown_jobs()` in the order stated in section 6.11"), via
  section 6.11 (~lines 696–704).
- **What is wrong:** Section 6.11 says "stopping the grade runner
  first (draining) and then the install runner (terminating pip)
  avoids terminating pip while a grade still holds torch files, so
  grade shutdown precedes install shutdown." Neither half is true of
  the code. `CodeGradeRunner.shutdown()` does not drain anything: it
  only joins the thread (`code_grade_runner.py`, `shutdown()`:
  "Wait for a running grade"). Draining is a state entered only by
  `cancel()`, which `shutdown_jobs()` never calls (`core.py`,
  `shutdown_jobs()` calls `self.code_grade_runner.shutdown()` with
  no preceding cancel). Worse, `join(timeout)` returns after the
  timeout with the thread still alive, and shutdown then proceeds —
  so even grade-first ordering does **not** avoid terminating pip
  while a grade thread holds torch files. The safety rationale the
  plan gives for the order is false, and section 6.11 additionally
  contradicts itself ("after the browser installer and before or
  after the grade runner" in the same paragraph as the resolved
  grade-first order).
- **Evidence:** `src/jev_agent/dashboard/code_grade_runner.py` —
  `shutdown()` (join only) vs `cancel()` (the only draining entry);
  `src/jev_agent/dashboard/core.py` — `shutdown_jobs()` (no
  `cancel()` call anywhere).
- **Fix required:** State the real mechanism (e.g. cancel-on-shutdown
  plus a join, or an explicit accept-the-race statement) and one
  unambiguous insertion point in `shutdown_jobs()`; do not ship the
  "draining avoids the race" sentence.

### B2. The loadability-probe entry point is assigned to the wrong file, and the seam does not exist

- **Design location:** section 8, `backend.py` bullet (~line 810:
  "if the loadability probe needs a backend-construction entry point
  that does not load the checkpoint, add it here; otherwise no
  change").
- **What is wrong:** The backend class is not in `backend.py` at
  all. It is `LayaBackend` in `laya_backend.py`, and section 8 does
  not list `laya_backend.py` as modified anywhere. More
  fundamentally, no "construct without loading the checkpoint" entry
  point exists, and constructing one proves nothing as the code
  stands: `LayaBackend(model_id)` never imports `laya` (import
  happens only inside `_agent_or_load`), and `_agent_or_load` calls
  `laya.load(self.model_id)` — the same call that loads (and first
  downloads) the checkpoint. So the plan's conditional presupposes
  a seam that is not there, points at the file that does not contain
  the backend, and leaves the implementer to discover — mid-task —
  that section 5.2 step 2 ("constructs the backend object without
  loading the checkpoint, if the backend API allows it") may be
  unbuildable. Note also the internal tension this creates: if the
  probe "stops at the import step" (section 5.2's fallback), the
  panel flips on layer 1 alone, which section 5.1 explicitly
  forbids ("must never show 'installed' on layer 1 alone").
- **Evidence:** `src/jev_agent/code_orchestrator/laya_backend.py` —
  `LayaBackend.__init__` (no import, no load) and
  `LayaBackend._agent_or_load` (`import laya`, then
  `laya.load(self.model_id)`); `src/jev_agent/code_orchestrator/backend.py`
  — `load_backend` (only `find_spec("laya")` + `LayaBackend(model_id)`).
- **Fix required:** Either name the real probe mechanism (with
  `laya_backend.py` in the file list if it changes), or decide now
  that verification is import-plus-version only and amend section
  5.1 accordingly. Section 12 Q4 openly deferring this does not
  cure section 8 assigning it to the wrong file.

## Majors

### M1. The install route set is unnamed: one URL for a pair, no confirm/stop/repair URLs, no route table, no app.py change specified

- **Design location:** section 8, routes bullet (~lines 786–790).
- **What is wrong:** The bullet names a single URL,
  `/code-grade/install`, for a "route pair (status plus live
  fragment)". The live fragment's URL is never given (the existing
  pattern is `/code-grade/status` + `/code-grade/live` in
  `routes_code.py`, but the install analogue — e.g.
  `/code-grade/install/live` — is not stated). The confirm route
  (option H), stop route (option E), and repair route (section 5.5)
  get no URLs and no HTTP methods at all, so even after the human
  picks H1/E1 the implementer must invent URLs. The bullet also
  names `app.py` (modify) without stating what changes there; the
  mechanism by which a `CodeRoutes` method becomes a URL is never
  described in the plan, so the most load-bearing wiring step is
  unspecified. This fails the standard's zero-decision rule and
  section 8's own "every file it touches" contract.
- **Evidence:** `src/jev_agent/dashboard/routes_code.py` —
  `code_grade_status` / `code_grade_live` (the pattern being
  mirrored, with no install analogue specified).
- **Fix required:** A route table: path, method, handler, and
  template for every route (install page, install live fragment,
  confirm, stop, repair), plus the exact `app.py` edit.

### M2. The live fragment template has no filename

- **Design location:** section 8, templates bullet (~lines 795–797:
  "`code_grade_install_status.html` (new) and a live fragment
  template (new)").
- **What is wrong:** The status page template is named; the fragment
  template is not. The mirror name (`_code_grade_install_live.html`
  by analogy with `_code_grade_live.html`) is guessable but not
  stated, and section 8 claims to list every file touched. The
  fragment's context (which status fields it renders) is likewise
  never specified, unlike `_code_grade_live_context()` in
  `routes_code.py`, which the plan could have mirrored explicitly.
- **Evidence:** `src/jev_agent/dashboard/routes_code.py` —
  `_code_grade_live_context()` and the `_code_grade_live.html`
  render; design section 8 names no fragment file.
- **Fix required:** Name the fragment file and its context.

### M3. Grade-refusal coordination is assigned to the wrong file, and the file that must change is not listed for it

- **Design location:** section 8, `code_grade_runner.py` bullet
  (~lines 801–806: "grade submission refused while an install runs
  must not ledger a spurious 'unavailable' record") and routes
  bullet (~lines 786–790, which lists no grade/route-handler change).
- **What is wrong:** In the code, a refused submission never reaches
  the runner: `code_grade_grade` / `code_grade_route` in
  `routes_code.py` call `self.code_grade_runner.start(...)` and
  catch only `CodeGradeBusy`; ledgering happens later, inside
  `_work`, and only for jobs that actually ran (`done` or
  `unavailable`). So there is no runner-level ledger guard to write
  — the refusal has to happen in the route handlers (or in a
  `DashboardCore` coordination check the handlers consult) before
  `start()`. But section 8's routes bullet lists only the install
  pair, confirm, stop, and repair — no change to the grade/route
  submission paths. And putting the check inside `CodeGradeRunner`
  would contradict section 6.4's own rule that "the coordination
  point lives above both runners, not inside either runner". The
  plan therefore either contradicts section 6.4 or omits a required
  change to `routes_code.py`.
- **Evidence:** `src/jev_agent/dashboard/routes_code.py` —
  `code_grade_grade` / `code_grade_route` (catch `CodeGradeBusy`
  only); `src/jev_agent/dashboard/code_grade_runner.py` — `_work`
  (ledgers only `done`/`unavailable` outcomes of started jobs).
- **Fix required:** Assign the refusal to the route handlers (or a
  named `DashboardCore` check they call), name the refusal signal
  (new exception, return value, or pre-check), and list it in the
  routes bullet.

### M4. The durable job record's location, format, and startup reader are all unnamed

- **Design location:** section 8, runner bullet (~lines 771–779:
  "durable job record (section 6.5)"), via sections 6.5 (~lines
  560–575) and 6.9.
- **What is wrong:** Section 6.5 says the record lives in "a fixed
  location (the dashboard state directory, not a repo's `.jev/`
  directory)". No such directory exists as a concept in the code
  read: `DashboardCore` knows only `settings.root` (the repo root);
  `BrowserInstallRunner` logs under `root/.jev/`; the only
  home-level dashboard state is `~/.jev/dashboard-secret`
  (`security.py`, `load_or_create_secret`). Section 8 names no path,
  no filename, no format (JSON? JSONL?), and no startup
  reconciliation step — the core bullet lists owning the runner,
  shutdown wiring, and the lock, but not "on start, read the record
  and flip `running` to `interrupted`". Yet section 10's restart
  acceptance criterion ("a dashboard restart mid-install yields the
  `interrupted` state and a working retry") depends entirely on this
  unnamed machinery. The implementer must invent the location, the
  format, and the reader.
- **Evidence:** `src/jev_agent/dashboard/core.py` —
  `DashboardCore.__init__` (runners take `settings.root` only; no
  state-dir attribute); `src/jev_agent/dashboard/browser_install.py`
  — `_log_path()` (`root / ".jev"`); `src/jev_agent/dashboard/security.py`
  — `load_or_create_secret()` (`~/.jev/dashboard-secret`, the only
  home-level state precedent).
- **Fix required:** One fixed path, one format, and the startup
  reconciliation owner (e.g. `DashboardCore.__init__` or the
  lifespan) in section 8.

### M5. The shared-scaffold member list omits the grade runner's load-bearing mechanisms

- **Design location:** section 8, `browser_install.py` bullet
  (~lines 815–818: "extract the shared job scaffold (lock, thread,
  state, timestamps, `busy()`, `status()`, shutdown hook, log cap)
  that all three runners use").
- **What is wrong:** The two runners actually read do not share the
  listed skeleton uniformly, and the list omits the mechanisms whose
  absence would regress the grade runner:
  - The generation counter: `CodeGradeRunner._work` drops outcomes
    whose generation is stale (the cancel-safety mechanism). It is
    not in the scaffold list.
  - Draining: `CodeGradeRunner.busy()` means "a thread is alive,
    including a cancelled one draining"; `BrowserInstallRunner.busy()`
    means "state == running". The two `busy()` contracts differ and
    cannot be merged without a decision the plan never makes.
  - The grade runner has no log file and no log cap at all
    (`CodeGradeStatus` carries no `log_tail` or `command`, unlike
    `BrowserInstallStatus`); "log cap" is a browser-runner-only
    feature being presented as shared.
  A scaffold extracted per the listed members would either drop the
  generation guard (a correctness regression in the grade runner)
  or require implementer-designed special cases — which is exactly
  option B1's stated failure mode. Under B3, no scaffold filename is
  named at all.
- **Evidence:** `src/jev_agent/dashboard/code_grade_runner.py` —
  `_generation`, `_work` generation check, `busy()` ("a thread is
  alive"), `CodeGradeStatus` (no log fields);
  `src/jev_agent/dashboard/browser_install.py` — `busy()` ("state ==
  running"), `_cap_log()`, `BrowserInstallStatus` (`command`,
  `log_tail`).
- **Fix required:** Either include the generation counter and the
  draining contract in the scaffold specification (with the
  reconciled `busy()` semantics), or scope B1 to the install runner
  plus browser runner only and say the grade runner is untouched.

### M6. The test seams for the new runner do not match any existing seam as specified

- **Design location:** section 8, test bullet (~lines 820–826:
  "Test seams: injectable process spawn and an availability seam,
  matching the existing runners' seams, so no test downloads torch
  or a checkpoint").
- **What is wrong:** The existing seams are specific and different
  from what is named: `BrowserInstallRunner` takes a constructor
  `spawn: (argv, log) -> Popen`; `CodeGradeRunner` takes a
  constructor `executor`; availability is reached only via the
  module-level `_orchestrator()` function in
  `code_grade_runner.py`, which tests monkeypatch, and via the
  `CodeOrchestrator.available` property, which calls `load_backend`
  (`find_spec` + import). There is no injectable "availability seam"
  to match, and the plan never says whether the new runner's seam is
  a constructor parameter, a module function, or a monkeypatched
  property — nor what its spawn seam looks like, even though the new
  runner needs at least two spawn shapes (pip argv; probe argv with
  a rewritten TMPDIR environment) versus the browser runner's single
  `(argv, log)` spawn. Section 10's "tests stub … environment
  detection, disk, and network" presumes the same unnamed seams.
- **Evidence:** `src/jev_agent/dashboard/browser_install.py` —
  `BrowserInstallRunner.__init__` (`spawn` parameter), `_spawn`;
  `src/jev_agent/dashboard/code_grade_runner.py` — `_orchestrator()`
  ("Module-level seam … tests monkeypatch it");
  `src/jev_agent/code_orchestrator/backend.py` —
  `CodeOrchestrator.available` (property, calls `_backend()` /
  `load_backend`).
- **Fix required:** Name each seam (constructor parameter name and
  signature, or module function) for spawn, probe-spawn, and
  availability.

### M7. Section 8's file list omits `code_grade_status.html`, although the plan's own section 2 puts a changed surface there

- **Design location:** section 8, `code_grade_runner.py` bullet
  (~lines 801–806: "`unavailable_message()` stops printing the pip
  command and links to the install action instead").
- **What is wrong:** Section 2 locates this message on
  `/code-grade/status` ("the failed grade message
  (`unavailable_message()` on `/code-grade/status`)"). A plain
  string cannot "link" anywhere unless either the string carries a
  raw URL (which the template would render as escaped text, not a
  link) or `code_grade_status.html` is changed to render a link —
  and section 8 does not list `code_grade_status.html` as modified.
  The plan's "every file it touches" list is therefore incomplete
  for its own second pip surface, and the implementer must choose
  between a non-linking string and an unlisted template edit.
- **Evidence:** `src/jev_agent/dashboard/code_grade_runner.py` —
  `unavailable_message()` (returns a plain `str`);
  `src/jev_agent/dashboard/routes_code.py` — `code_grade_status`
  renders `code_grade_status.html`; design section 2 (~line 144)
  locates the message on `/code-grade/status`.
- **Fix required:** Either list `code_grade_status.html` (modify)
  with the link rendering specified, or specify the message as a
  plain-text path and drop the word "links".

## Nits

### N1. "Same `checked_form` protection" is overstated for the GET half of the route pair

- **Design location:** section 8, routes bullet (~line 790).
- **What is wrong:** `checked_form` (`security.py`) reads POST form
  data and raises 403 without a valid per-session CSRF field; the
  existing status/live GET handlers in `routes_code.py` do not call
  it. The protection that covers the install status page and live
  fragment GETs is the token middleware, not `checked_form`. As
  written, an implementer could read the bullet as requiring
  `checked_form` on the GETs, which would 403 every poll.
- **Evidence:** `src/jev_agent/dashboard/security.py` —
  `checked_form()`; `src/jev_agent/dashboard/routes_code.py` —
  `code_grade_status` / `code_grade_live` (no `checked_form` call).
- **Fix:** Say "`checked_form` on the POST actions; token gating on
  the GET pair, as today."

### N2. The new runner's class name and public API are never stated

- **Design location:** section 8, runner bullet (~lines 771–779).
- **What is wrong:** The file is named (`cda_install_runner.py`)
  but the class is not, and no method signatures are given
  (`start`/`stop`/`status`/`busy`/`shutdown` presumably mirroring
  `BrowserInstallRunner`, but the plan never says so, and the
  record/phase/event API from sections 6.5/6.10 has no method home).
  The routes that must call this API are specified in the same
  bullet list, so the API is load-bearing, not cosmetic.
- **Evidence:** `src/jev_agent/dashboard/browser_install.py` —
  `BrowserInstallRunner` (the class the new runner is said to
  mirror, with no corresponding class name given for the new one).
- **Fix:** Name the class and its public methods with signatures.

### N3. The "existing redaction practice" behind attempt events does not appear in the code read

- **Design location:** section 8, runner bullet ("attempt events
  (section 6.10)"), via section 6.10 ("following the dashboard's
  existing redaction practice for displayed tokens").
- **What is wrong:** Neither runner file read redacts anything:
  `BrowserInstallRunner._spawn` streams pip-style output to the log
  as-is, and no redaction helper or token-redaction rule appears in
  `browser_install.py`, `code_grade_runner.py`, or `security.py`.
  The redaction rule (what patterns, applied where — log file,
  tail, events, or all three) is therefore an implementer invention,
  not an inherited practice.
- **Evidence:** `src/jev_agent/dashboard/browser_install.py` —
  `_spawn()` (output appended to log unfiltered).
- **Fix:** Name the redaction rule or drop the "existing practice"
  claim.

### N4. Shutdown marking (`interrupted` vs `stopped`) has no mechanism

- **Design location:** section 8, runner bullet ("stop and shutdown
  (sections 6.6, 6.11)"), via section 6.11 (~line 702: the record is
  marked `interrupted`, "not `stopped`, because the user did not
  stop it").
- **What is wrong:** The precedent the runner is told to mirror
  cannot make this distinction: `BrowserInstallRunner.shutdown()`
  delegates to `stop()`, which unconditionally sets state
  `"stopped"`. The plan never names the mechanism by which the CDA
  runner tells a shutdown termination apart from a user stop (a
  flag, a separate method, a shutdown-only path).
- **Evidence:** `src/jev_agent/dashboard/browser_install.py` —
  `shutdown()` (calls `self.stop()`); `stop()` (sets
  `self._state = "stopped"`).
- **Fix:** Name the distinguishing mechanism in section 8.

### N5. Section 10's test matrix lists a platform section 5.4 never defines

- **Design location:** section 10, test matrix (~lines 1155–1156:
  "Mac venv, Mac uv tool environment, Mac system Python, …").
- **What is wrong:** Section 5.4's deployment matrix has no "Mac
  system Python" row (its Mac rows are venv/editable and uv tool;
  the PEP 668 row is Linux-only). The decision-tree branch for a Mac
  system Python — PEP 668 refusal → branch 2 or 3 — is therefore
  unstated for the one platform the matrix tests claim to cover,
  leaving that matrix cell without specified behavior.
- **Evidence:** Design section 5.4 matrix rows vs section 10
  (~line 1155) platform list.
- **Fix:** Add the Mac system Python row to section 5.4 (or drop it
  from section 10).

## Counts

- Blockers: 2 (B1 shutdown/draining; B2 probe file and seam)
- Majors: 7 (M1 routes/URLs; M2 fragment filename; M3 coordination
  misassignment; M4 durable-record location; M5 scaffold omissions;
  M6 test seams; M7 omitted `code_grade_status.html`)
- Nits: 5 (N1 `checked_form` scope; N2 runner class/API unnamed;
  N3 redaction precedent; N4 shutdown marking; N5 Mac system Python
  row)

## Most important finding

B1: the shutdown story is not just underspecified, it is wrong about
the code it builds on — `shutdown_jobs()` never cancels a grade, and
`CodeGradeRunner.shutdown()` is a join-with-timeout, not a drain, so
the plan's claimed protection against terminating pip under a running
grade does not hold. Everything else in the plan can be fixed by
naming things; this one would ship a false safety argument into the
implementation.
