# Codebase details — CDA install button concepts brief

Lens: codebase details only. Every claim below was checked against the
listed source files. No production code was written or changed.

Scope checked: `concepts_brief.md` (2026-10-29, tracking issue
Gane2506/baton#366) against `code_grade.html`, `routes_code.py`,
`browser_install.py`, `code_grade_runner.py`, `backend.py`, and
`docs/code-orchestrator.md`.

## What the brief gets right

These claims match the code and need no correction:

- The status panel in `code_grade.html` renders Enabled, Model,
  Confidence threshold, and Backend (`available` vs `not installed`
  badge), plus the pip command when the backend is missing.
- `CodeOrchestrator.available` (`backend.py`) is true only when a
  backend is already loaded or loadable now, and a missing backend
  surfaces as `CodeOrchestratorUnavailable`, never as a guessed grade.
- `BrowserInstallRunner` is single-flight, streams output to a log
  under `.jev/`, shows the log tail, can be stopped, and runs a fixed
  command no request can change.
- The grade runner runs one job at a time in a background thread, the
  status page polls it, and cancel is honest: the model call cannot be
  interrupted mid-inference, so cancel discards the result and the
  runner drains.
- Grading is off by default; the enable control lives under
  Settings → Code orchestrator.

The rest of this report is what the brief gets wrong, omits, or glosses
over. Findings are ordered by severity.

## 🔴 Blocker: the pip command lives on three surfaces, not one

The brief describes the pip instruction as if it were only on the Code
Grade status panel. It is not. All three must change, or the button
ships next to a terminal instruction telling the user to do the same
thing by hand:

1. `templates/code_grade.html` — the `flash warn` panel: `pip install
   "jev-agent[code-orchestrator]"`, plus the note that the first grade
   downloads the ~842 MB checkpoint into the Hugging Face cache, plus a
   pointer to `docs/code-orchestrator.md`.
2. `code_grade_runner.py`, `unavailable_message()` — the failed-job
   error text shown on `/code-grade/status` when a user grades while
   the backend is missing. This is the surface a user actually hits if
   they submit the grade form while uninstalled (see the next finding:
   the form is not gated). If only the template changes, a failed grade
   still tells the user to open a terminal.
3. `templates/learning.html` — the Ratings tab empty state, which also
   prints the `pip install "jev-agent[code-orchestrator]"` command.
   (Found by grep; outside the listed files, but it is the same
   instruction and will contradict the button if left.)

Fix for the draft: enumerate every surface that prints the command and
state what each one shows after the change (button, link to
`/code-grade`, or nothing).

## 🔴 Blocker: "verification via `available`" cannot mean what the brief needs it to mean

The brief's goal says: "Verification after install via
`CodeOrchestrator.available`." That check, as implemented in
`backend.py:load_backend`, verifies package presence only, not a
working backend and not a downloaded checkpoint:

- The `laya` branch checks `importlib.util.find_spec("laya")` and then
  returns `LayaBackend(model_id)` without importing `laya` or torch.
  A present-but-broken `laya`/torch install still yields
  `available == True`, and the failure then appears later as an
  inference failure (`CodeOrchestratorError`), not as "not installed".
  The two failure classes have opposite handling at the quality gate
  (unavailable records "not graded" and does not block; an inference
  failure blocks fail-closed), so conflating them in the UI misleads
  the user about what went wrong.
- `available` says nothing about the ~842 MB checkpoint. The brief
  itself flags this under "Install is not one thing", but its own goal
  then proposes the check that cannot distinguish "pip done" from "pip
  plus checkpoint". After a pip-only install, `available` flips to true
  while the first grade still downloads 842 MB, still needs TMPDIR
  pointed at a disk with room (see Major below), and still takes
  minutes. The draft must define verification as something stronger
  than `available` (for example, a checkpoint-presence check or a
  smoke grade) or state plainly that `available == True` means
  "pip done, checkpoint still pending" and show that state in the UI.

Related precision issue: `available` is not a pure predicate. It calls
`_backend()`, which caches the loaded backend on that orchestrator
instance. That cache is harmless in practice only because `routes_code.py`
constructs a fresh `CodeOrchestrator` per request (`_orchestrator()`)
and the grade executor constructs one per job — so a post-install page
load does see the new backend. The draft should still say so
explicitly, because a reader who assumes process-wide caching will
wrongly conclude a restart is mandatory, and a reader who assumes no
caching will miss the one case that matters: a long-lived orchestrator
instance created before the install keeps its stale state. (The
dashboard's own paths do not keep one, but the quality gate's path was
not in the listed files and is out of scope for this check.)

One more `available` gap the draft must answer: after a pip install
into a running process, does `find_spec` see the new package without
`importlib.invalidate_caches()`? The listed code never calls it. The
brief's open decision 7 ("restart: yes or no") cannot be answered
without settling this, and the answer differs between "installed into
the dashboard's own environment" and "installed into `--user` or a
dedicated venv" (see below).

## 🔴 Blocker: there is no precedent for installing into the dashboard's own Python environment

The brief leans on `BrowserInstallRunner` as the precedent, but the
analogy covers runner shape only, not the install itself:

- The browser precedent installs a Node tool: `install_argv()` returns
  `npx -y playwright@<pinned> install --with-deps chromium`. It never
  modifies the dashboard's Python environment, never runs pip, and
  never installs a package into the interpreter that is currently
  running. The CDA install would be the first in-process self-modifying
  Python install in the listed code. Nothing in the listed files
  resolves `sys.executable -m pip`, checks that pip exists in the
  dashboard's interpreter, or handles a venv without pip.
- The browser precedent's executable resolution
  (`resolve_executable(argv[0])`, PATH plus per-platform locations,
  with a `BrowserInstallError` naming what was searched) does not
  transfer to pip: pip is not a PATH lookup but an interpreter module,
  and the failure modes are different (no pip in this interpreter,
  read-only site-packages, externally-managed environment on modern
  Linux, `--user` vs venv vs system). The brief names `--user` and a
  dedicated venv as fallbacks, but neither exists anywhere in the
  listed code, and a dedicated venv would additionally need its own
  activation story for later grades — the grade runner constructs a
  plain `CodeOrchestrator` in-process and cannot grade from another
  venv without new machinery.
- The browser precedent's route lives on the Connections page with a
  two-step confirm flow (`routes_connections.py:browser_install`:
  first POST without `confirm=yes` renders
  `confirm_browser_install.html` naming the exact command; only
  `confirm=yes` starts it; a GET bounces back with a flash error).
  The CDA button would live on `/code-grade` itself, which has no
  confirm page and no install route at all. The draft must name the new
  routes (`app.py` currently registers `/code-grade`,
  `/code-grade/grade`, `/code-grade/route`, `/code-grade/status`,
  `/code-grade/live`, `/code-grade/cancel` — no install/stop pair),
  the new runner's home in `DashboardCore` (which currently constructs
  `browser_installer` and `code_grade_runner` side by side and shuts
  both down in `shutdown_jobs`), and the CSRF rule every dashboard POST
  follows (`checked_form` in both `routes_code.py` and
  `routes_connections.py`). The brief names none of these touch points.

## 🟡 Major: the grade/route forms are not gated on availability

`code_grade.html` renders the "Grade diff" and "Route task" buttons
unconditionally, even when the backend badge says "not installed".
Submitting while uninstalled starts a real background job that fails
with `unavailable_message()` and — via `code_grade_runner._record` —
writes an `unavailable` record to the rating ledger. Consequences for
the draft:

- The Install button's show/hide rule ("shown exactly when the backend
  is missing") is not the only UI decision. The draft must decide what
  happens to the grade/route forms in the missing state: disabled with
  the button as the only action, or left live with the failed-job path
  as the discovery route. Leaving them live while also showing the
  button means two competing calls to action on one page.
- The failed-job path is a second discovery route for the button: a
  failed grade's status page should link to the install action, not
  just print the pip command (see Blocker 1).
- An install that runs while a grade job is in flight, or a grade
  submitted mid-install, currently fails with `unavailable` and
  ledgers a spurious "unavailable" record. The brief flags
  install/grade contention for CPU/memory/disk but not this ledger
  pollution, nor the sharper hazard: pip replacing `laya`/torch files
  while a grade thread has torch loaded in-process. The draft's lock
  decision (shared vs independent) must cover file replacement, not
  just resource contention.

## 🟡 Major: the fixed command is not one command in the codebase today

Three wordings exist, and the draft must pick one and reconcile the
other two:

- Template and grade runner: `pip install "jev-agent[code-orchestrator]"`.
- `backend.py:load_backend`'s unavailable message and
  `laya_backend.py`: `pip install laya`, with the note that CPU-only
  torch works.
- `pyproject.toml`: the `code-orchestrator` extra declares only
  `laya>=0.4.15`; torch arrives transitively through `laya`.

And the fixed-command rule collides with the docs' CPU advice.
`docs/code-orchestrator.md` says the default torch wheel includes CUDA
libraries (~2.5 GB) and recommends installing the CPU wheel first, then
`laya`, on CPU-only machines. A single fixed command cannot do both
"plain extra" and "CPU wheel first". The brief's security requirement
(fixed command, no request-supplied package name) is correct and
matches the precedent, but the draft must decide the actual argv: plain
extra, bare `laya`, or a two-step fixed sequence — and must state the
download size honestly for the chosen one (see Nit on "~1 GB").

## 🟡 Major: first-grade traps the brief does not carry forward

The brief correctly says the checkpoint downloads on first grade. It
omits three documented first-grade traps that an install button's
"success" story must survive, all from `docs/code-orchestrator.md`:

1. **TMPDIR.** `laya.load` reconstructs the checkpoint through the
   temp dir, and the default `/tmp` is a 512M tmpfs on the dev VM —
   smaller than the model — so the first load fails with "No space
   left on device" unless TMPDIR points at a home-disk directory. An
   install button that reports success after pip, with no TMPDIR
   handling or warning, hands the user this failure on their first
   grade. The draft must say whether the dashboard sets TMPDIR for
   grade jobs, warns, or leaves the documented manual step in place.
2. **Hugging Face cache location.** The checkpoint goes to
   `~/.cache/huggingface/hub` (or `$HF_HOME`), i.e. the dashboard
   process user's home — on a VPS service account or a Windows service
   that home may differ from the human user's, may be small, or may
   not persist across restarts. "Works on a VPS" needs this answered,
   not just Docker persistence.
3. **Checkpoint pre-warm mechanism is unspecified beyond a question.**
   The brief lists pre-warming as open decision 2 but gives no
   candidate mechanism grounded in the code. The listed code has no
   "download the checkpoint without grading" function: the only
   checkpoint fetch path is a real grade through `LayaBackend`, which
   also loads torch (~2–3 GB RSS) and takes minutes. Pre-warming is
   therefore either a throwaway grade (with ledger and UX
   consequences — a warm-up grade would record a real grade or need
   suppressing from the ledger) or a new HF-download step. The draft
   cannot treat it as a free option.

## 🟡 Major: BrowserInstallRunner details the brief glosses over (the draft will inherit them)

The brief's one-paragraph summary is accurate but drops the exact
behaviours a mirror implementation would copy, intentionally or not:

- **Log mechanics.** Log file is `<root>/.jev/browser-install.log`
  (`root` = `DashboardSettings.root`, the same root the grade
  runner's ledger uses). Each install truncates it fresh
  (`write_bytes(b"")`). The tail shown is the last 30 lines, read from
  the last 64 KiB only; a finished log is capped at the last 256 KiB.
  A CDA runner gets log growth bounded only if it copies the cap, and
  needs its own filename (reusing `browser-install.log` would mix two
  jobs' output).
- **Stop semantics differ from grade cancel, and that difference is
  the point.** `BrowserInstallRunner.stop()` calls `proc.terminate()`
  — the installer process is genuinely killed, unlike a grade, whose
  atomic `predict` cannot be interrupted. But the stop is also
  optimistic: `stop()` sets state `stopped` and `finished_at`
  immediately, before the process has actually exited, and `_watch`
  lets a stop win over the exit code. A CDA runner mirroring this gets
  real termination of pip (good) and the same "stopped while still
  exiting" window (must be disclosed if the UI promises the process
  is gone).
- **Start-time validation.** `start()` resolves `argv[0]` before
  spawning and raises `BrowserInstallError` (e.g. "`npx` not found;
  install Node.js…") when unresolvable; a thread-start failure resets
  state to `idle`. The CDA analog needs its own start-time validation
  (interpreter? pip? writable target?) — pip's absence is not covered
  by the npx precedent.
- **Single-flight is per-process and in-memory only.** Both runners
  hold state in instance attributes guarded by a `threading.Lock`.
  A dashboard restart resets the install runner to `idle` while an
  orphaned pip keeps running; nothing on disk records "an install is
  in flight". The brief's "one at a time" needs the qualifier
  "per dashboard process", plus a restart story (re-attach? refuse?
  allow a second pip?).
- **Progress display is not specified for CDA.** The browser
  precedent shows the tail on the Connections page. The CDA page has
  no log-tail UI: the status page (`/code-grade/status`) polls
  `/code-grade/live` every 3 seconds (`data-live-every="3000"`) for
  grade jobs only. The draft must say where install progress lives —
  the `/code-grade` page itself, the existing status page extended to a
  second job kind, or a new page — and whether install progress reuses
  the live-fragment pattern or needs its own.
- **Plain-English failures are new work, not inherited.** The browser
  runner's failure message is raw: "the install exited with code N".
  The grade runner maps failures through `dashboard/errors.py`
  `friendly_error` (a rewrite table keyed on known jargon). Neither
  maps pip's output to plain English. The brief's goal ("failure states
  in plain English with retry") therefore requires a new mapping layer
  for pip failures (network, disk full, permission, externally-managed
  environment), not a reuse of either precedent.

## 🟡 Major: grade-runner details the brief simplifies

- **"One grade at a time" understates the lock.** `CodeGradeRunner`
  serialises grades *and* routes through one runner: a route blocks a
  grade and vice versa. The lock check is thread-liveness
  (`self._thread is not None`), not state — so a cancelled job still
  blocks new jobs while it drains, with a distinct busy message
  ("the previous grade was cancelled and is still finishing…").
  `busy()` likewise reports draining as busy. Any shared-lock design
  must decide whether a draining grade blocks an install, and whether
  an install blocks a route.
- **Generation counter.** Stale outcomes are dropped by generation:
  a cancelled job's thread finishing late cannot overwrite a newer
  job's result. A CDA runner that shares the grade runner's status page
  needs the same guard if install and grade outcomes ever share a
  status object; separate runners avoid it.
- **Test seams to mirror.** `CodeGradeRunner` takes an injectable
  executor and ledger path (wired in `DashboardCore` with
  `ledger_path_for_repo(settings.root)`); `BrowserInstallRunner`
  takes an injectable `spawn` callable; `routes_code._orchestrator`
  is a module-level seam tests monkeypatch ("like `app.check_gh`").
  A CDA runner should take the same shape (injectable spawn, and a
  seam for the availability check) or the draft must justify why not.
  The brief does not mention test seams at all.
- **Shutdown.** `DashboardCore.shutdown_jobs` stops the browser
  installer (30 s default join) and joins a running grade (passed
  timeout). A new runner must be added there, or a dashboard shutdown
  mid-install leaves pip orphaned even on a clean stop.

## 🟡 Major: "installed" ≠ "enabled" ≠ "grading"

The brief's success criterion is "the status panel flips to
installed". That is necessary but not sufficient for the feature to
work end to end, because three independent gates exist in the code:

1. Backend installed (`available`).
2. `[code_orchestrator] enabled` (default `False`; the quality gate
   only grades when enabled, and the template shows a separate
   "Grading is off" hint with a Settings link even when the backend
   is present).
3. On-demand grading from the page, which works regardless of
   `enabled`.

After a successful install with grading still off, the panel says
"available" but the gate still grades nothing. The brief lists
auto-enable as open decision 4, which is the right place for this —
but the draft's definition of done must name which of the three gates
the button flips, and what the user sees when the other two are still
closed. Otherwise "installed" will be read as "working" and it is not.

## 🟡 Major: observability gap (brief is silent)

Neither runner emits metrics, structured events, or anything beyond
its log file and in-memory status. The grade runner logs at debug
level only when a ledger write fails. There is no record of install
attempts, durations, failure classes, or disk/network causes anywhere
except the raw pip output in the log tail. For a multi-minute,
multi-GB background install, the draft should require at minimum:
attempt start/finish/failure with a failure class (not just an exit
code), and retention of the full log beyond the 30-line tail for
diagnosis. This is a gap in the precedents, not just in the brief —
mirroring them copies the gap.

## Nit: sizes and durations are loose in one place

- The counter-argument says auto-install would hide "a multi-minute,
  ~1 GB download". The total first-use download is larger: the default
  torch wheel alone is ~2.5 GB of CUDA libraries (docs), plus `laya`,
  plus the 842 MB checkpoint. "~1 GB" understates it; the brief's own
  "~842 MB checkpoint" figure is the checkpoint only and is correct.
- "Multi-GB torch process" for grade contention matches the code's
  "~2–3 GB RSS" (`code_grade_runner.py` docstring); the model itself
  is described as a 421M-parameter ModernBERT derivative in
  `backend.py` and the docs. The draft should keep these three numbers
  distinct (parameters, checkpoint bytes, RSS, download bytes) instead
  of letting "multi-GB" cover all of them.
- The brief says the grade runner's status page has a "live region
  [that] polls the job". Precisely: the status page polls
  `/code-grade/live` every 3 s and renders `_code_grade_live.html`;
  elapsed time is computed server-side in `_code_grade_live_context`.
  An install progress UI reusing this pattern inherits the 3 s
  cadence, which is fine for pip's slow output but worth stating.

## Nit: small wording corrections

- The brief says the browser precedent's "page shows the log tail".
  Precisely, the Connections page shows the tail of
  `.jev/browser-install.log`; the install itself is started from the
  Connections page, not from a dedicated install page, and the confirm
  step is a separate template, not a dialog on the same page.
- The brief says the browser install "can be stopped". Precisely,
  stop is a separate POST route (`/connections/browser/install/stop`,
  Tier 1 in the code's own labelling) while the install start is the
  Tier 2 confirm flow — the two actions have different tiers and
  different routes, and the CDA design should keep that distinction
  rather than describing one "button".
- The brief says "the command is fixed — no request can change it".
  The fixed thing in code is `install_argv()` (server-side, argv list),
  resolved through `resolve_executable` at start time. The CDA analog
  must fix the argv list server-side the same way; fixing only the
  package name while letting the browser supply flags or an index URL
  would not meet the bar.

## What the draft must answer (checklist distilled from the above)

1. Every surface that prints the pip command, and what each shows
   after the change (status panel, failed-grade status page, Learning
   empty state).
2. What "installed" means in a checkable sense, given `available`
   verifies package presence only — and what verification, if any,
   covers the checkpoint, torch health, and TMPDIR.
3. The exact fixed argv (extra vs bare `laya`, CPU vs CUDA torch),
   and the honest total download size for it.
4. The target environment in implementable terms
   (`sys.executable -m pip` into the dashboard's interpreter? `--user`
   fallback with what `sys.path` consequences? dedicated venv with
   what grade-time activation?), plus `invalidate_caches` and the
   restart answer.
5. Docker persistence and the service-account home/cache story
   (HF cache, TMPDIR, disk space) on VPS/Windows service installs.
6. New routes, `DashboardCore` wiring, shutdown handling, CSRF, and
   where install progress renders (which page, which poll cadence).
7. Install/grade interaction covering the ledger-spurious-unavailable
   case and pip-replacing-loaded-torch-files, not just CPU/memory.
8. Stop semantics for pip (real terminate, partial-env retry safety,
   "stopped while still exiting" window) and the per-process
   single-flight limit across restarts.
9. Plain-English failure mapping for pip (new work) and which failure
   classes are retryable.
10. Whether the button also flips `enabled`, and what "done" means
    across the installed/enabled/grading gates.
11. Test seams (injectable spawn, availability seam) and install
    telemetry beyond the 30-line log tail.
