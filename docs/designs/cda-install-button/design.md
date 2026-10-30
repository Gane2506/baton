# CDA install button: one-click backend install from the dashboard

* **Author:** Hana (for Gane)
* **Date:** 2026-10-29
* **Tracking issue:** Gane2506/baton#366
* **Status:** Design phase only. No implementation. Human approval of the
  option sets in section 9 is required before any code is written.
* **Inputs:** `concepts_brief.md` in this directory (brainstorm, six
  Goldfish lenses, 2026-10-30) and the code files named in section 2.
* **Review reports:** `review/` in this directory (five Goldfish stages);
  `review/resolution_report.md` records how each finding was handled.

## 1. The problem

The Code Grade page (`/code-grade`) shows the Coding Decision Agent (CDA)
backend as "not installed" and tells the user to run
`pip install "jev-agent[code-orchestrator]"` in a terminal. That is a
terminal step for a dashboard feature, and it breaks the product's own
browser-first law: no feature is done until it works from the browser
with zero terminal use.

This design replaces that instruction with an Install button on the same
page. One click installs the backend on the machine the dashboard runs
on, in the background, with progress shown in the browser and a check
afterwards. The browser downloads nothing. The install runs where the
dashboard server runs, so the same button must have a stated answer on
every machine the dashboard runs on: Mac, Windows, Linux, and a VPS.

The intended reader is a person who never touches a terminal. A second
reader is the operator of a dashboard on a server or container, who
needs to know when an install will not survive a restart or an upgrade.

### Words used in this design

* **Backend:** the CDA grading engine, provided by the `laya` package
  plus torch, plus a trained model checkpoint.
* **Installed:** defined in section 5.1. It is not a single fact; it has
  three layers, and the design says which layer flips the status panel.
* **Environment:** the Python environment the dashboard process itself
  runs from. The install targets that environment, never an environment
  found on PATH.
* **Grade:** one grading of a diff, or one routing hint, produced by the
  backend. A grade is not an install outcome, and an install outcome is
  never written to the grade ledger.
* **Route:** a routing hint (which model tier and skill a coding task
  needs), produced by the same backend.
* **Threshold:** 0.7, the confidence bar a grade must clear before its
  merge-action or routing hint is used. This design does not change it.
* **Gate:** the quality gate that grades diffs during a run. Grading
  through the gate is off by default; grading from the Code Grade page
  works on demand even when the gate is off.
* **laya:** the package that provides the backend runtime.
* **torch:** the machine-learning library `laya` depends on. The default
  wheel carries about 2.5 GB of CUDA libraries; the CPU wheel is
  smaller.
* **Checkpoint:** the trained model file, about 842 MB, downloaded into
  the Hugging Face cache on first use. The model has 421M parameters.
* **VPS:** a Linux server rented from a host, running the dashboard as
  a service.
* **Service account:** a system user that runs the dashboard without a
  normal login shell or writable home directory.
* **Volume:** a Docker storage area that survives container recreation.
* **Endpoint:** one dashboard URL that accepts a request (for example a
  form post).
* **Single-flight:** at most one install running at a time.
* **Log tail:** the last lines of a job's log, shown in the browser.
* **Draining:** a cancelled job whose background work is still
  finishing; its result is discarded.

### Status-panel fields, in one sentence each

* **Enabled:** whether the quality gate grades every diff (default no).
* **Model:** the Hugging Face model id the backend loads.
* **Confidence threshold:** 0.7, the bar described above.
* **Backend:** whether the backend can be used right now (available) or
  not (not installed).

## 2. What exists today

All paths are relative to the repo root, on branch `design/cda-install-button`
at `c1078a4`.

* Status panel: `src/jev_agent/dashboard/templates/code_grade.html`. It
  renders Enabled, Model, Confidence threshold, and Backend (available
  vs "not installed"), plus the pip command when the backend is
  missing. It also shows the current grade job state, with a link to
  `/code-grade/status`.
* Availability: `CodeOrchestrator.available` in
  `src/jev_agent/code_orchestrator/backend.py`. It is true only when a
  backend is already loaded or loadable now. Loadability is checked
  with `importlib.util.find_spec("laya")`, deliberately without
  importing `laya`, because importing it pulls in torch. A missing
  backend raises `CodeOrchestratorUnavailable`, and callers record
  "unavailable" instead of a guessed grade. That behavior must not
  change.
* Backend loading: `load_backend` in the same file, and
  `LayaBackend._agent_or_load` in
  `src/jev_agent/code_orchestrator/laya_backend.py`, which calls
  `laya.load(model_id)`. The first load downloads the checkpoint into
  the Hugging Face cache (`~/.cache/huggingface/hub`, or `$HF_HOME`)
  and reconstructs it through the temp directory, which fails with
  "No space left on device" on a 512M tmpfs `/tmp` unless TMPDIR
  points at a real disk.
* The package extra: `pyproject.toml` defines `code-orchestrator` as
  `laya>=0.4.15`, unpinned above. There is no torch pin; `laya` pulls
  the default torch wheel unless a CPU wheel is installed first.
* The documented CPU path (`docs/code-orchestrator.md`): install the
  CPU torch wheel first from the PyTorch CPU index, then install
  `laya`. Measured numbers from that doc: model load about a minute,
  small-diff grades 1–3 minutes, large-diff grades up to about 10
  minutes, torch plus model about 2–3 GB of memory while running.
* Two background-job precedents in the dashboard:
  - `BrowserInstallRunner`
    (`src/jev_agent/dashboard/browser_install.py`): one install at a
    time, output streams to a log file under the repo's `.jev/`
    directory, the page shows the last 30 lines, the log is capped at
    256 KB when the install finishes, the install can be stopped by
    terminating the process, and the command is fixed server-side —
    no request can change it. The button asks for confirmation first
    on a separate template, naming the exact command. The runner is
    owned by `DashboardCore` (`src/jev_agent/dashboard/core.py`) and
    stopped from `shutdown_jobs()`.
  - `CodeGradeRunner`
    (`src/jev_agent/dashboard/code_grade_runner.py`): one grade at a
    time in a background thread, with a status page
    (`/code-grade/status`) whose live region polls the job. Cancel
    never kills the model call; it discards the result and reports
    "draining" until the thread exits. The runner records outcomes in
    the CDA ratings ledger, including "unavailable" records when the
    backend is missing. Its missing-backend message is
    `unavailable_message()`, which prints the same pip command as the
    status panel.
* Form protection: every dashboard form post goes through
  `checked_form` (`src/jev_agent/dashboard/security.py`), which
  checks the per-session token. Off loopback, the dashboard requires
  a token (`jev_token` cookie, `X-Jev-Token` header, or token query
  parameter). On loopback, any local page visitor can reach it.
* Settings toggle: Settings → Code orchestrator turns gate grading on
  and off. No runner today writes config; the install job must not
  silently become the first one to do so without a decision (see
  option set D).
* The pip instruction appears on three surfaces, and all three must
  change together: the status panel (`code_grade.html`), the failed
  grade message (`unavailable_message()` on `/code-grade/status`),
  and the Learning page Ratings tab empty state
  (`src/jev_agent/dashboard/templates/learning.html`).
* Routes today (`src/jev_agent/dashboard/routes_code.py` and
  `src/jev_agent/dashboard/app.py`): `/code-grade`, `/code-grade/grade`,
  `/code-grade/route`, `/code-grade/status`, `/code-grade/live`,
  `/code-grade/cancel`. There is no install route pair.
* Deploy shapes (`docs/deploy.md`, `Dockerfile`): macOS via
  `install.sh` plus `jev init && jev dashboard`; Windows via
  `install.ps1`; Linux via a pinned Docker run (image runs as
  non-root user `jev`, workdir `/data`, optional named volume
  `jev-state` for state). Reboot survival is
  `jev dashboard --install-service` on all three. On the Mac used for
  live verification, `jev` is installed editable from the checkout
  via `uv tool install`, so its environment is a uv-managed tool
  environment, not a plain venv.

## 3. Goals and non-goals

### Goals

* An Install button on the Code Grade status panel, shown when the
  backend is not usable, that installs the backend server-side, in the
  background, in the dashboard's own environment, with progress and a
  log tail visible in the browser.
* Verification after install, in a fresh subprocess, that proves the
  backend can load — not only that a package is present. The status
  panel flips to installed without a dashboard restart when
  verification passes in the running process's environment, or says a
  restart is needed when it cannot.
* Failure states in plain English with a retry, never a bare
  traceback. A stopped or failed install leaves a state from which
  retry is defined (section 6.6).
* The same endpoint on Mac, Windows, Linux, and VPS, with a stated
  persistence answer for every deployment shape, including Docker
  (section 5.4).
* The fixed-command rule: the install command is a constant argv
  sequence on the server. No request field — package name, flag, or
  index URL — can change what gets installed (section 7).
* No change to grading semantics: no guessed grades while the backend
  is missing, threshold unchanged, off-by-default unchanged unless
  option set D decides otherwise.
* The pip instruction retired on all three surfaces named in section 2.
* No install or warm-up outcome ever written to the grade ledger, and
  no install outcome ever rendered as a grade outcome.

### Non-goals

* Changing the model, the threshold default, or the grading itself.
* Installing anything other than the code-orchestrator backend.
* A general package manager for the dashboard.
* Automatic updates of an installed backend (see option set G for the
  lifecycle question).
* Bundling the model with Baton.

## 4. Critique first: what can go wrong

The brainstorm lenses raised 16 blockers. The design answers each one
in the section noted; a blocker with no answering section is a gap in
this design, not a deferred detail.

* Presence is not loadability. A pip run that leaves torch broken or
  partial still flips a `find_spec` check to "installed", and the
  first real grade then fails as an inference error — which blocks
  the quality gate fail-closed, where a missing backend would have
  let the run proceed. A half-installed backend is worse than a
  missing one. Answered by section 5.2 (subprocess loadability
  verification) and section 5.5 (repair).
* The obvious fixed command is wrong. Three wordings exist today
  (template and grade runner: `jev-agent[code-orchestrator]`;
  `backend.py`: `laya`; docs: CPU torch wheel, then `laya`). Running
  the page's command verbatim asks pip to install the dashboard's own
  package into its own running environment. Answered by section 7
  (the fixed argv installs `laya` only, never `jev-agent` itself)
  and option set G (version policy).
* "Same button everywhere" contradicts the deploy model. A Docker
  container-side pip install is refused or wiped on recreate; a
  `pipx`-style tool reinstall recreates the environment and strands
  an extra installed into it. Answered by the matrix in section 5.4:
  every cell states persistence or ephemerality, and the UI shows it
  before the user clicks.
* The true footprint is several gigabytes, not ~842 MB. The default
  torch wheel carries about 2.5 GB of CUDA libraries, plus `laya`,
  transformers, the 842 MB checkpoint, and temp staging. Answered by
  section 5.3 (torch strategy and honest sizes) and section 6.1
  (preflight).
* "Installed" is three layers and `available` checks only the first
  two. Answered by section 5.1.
* The target environment is a decision tree, not a default. PEP 668
  refuses system-Python pip installs; service accounts may have no
  writable home or no pip; a dedicated venv needs a location and a
  discovery path for the next start; a `--user` install may not be
  visible to the dashboard interpreter. Answered by section 5.4.
* A third bespoke runner triples a duplicated skeleton. Answered by
  option set B.
* All install state today is in memory; restart mid-install is
  undefined. Answered by section 6.5 (durable job record,
  `interrupted` state).
* Install/grade coordination is missing, and the hazard is file
  replacement, not just contention. Answered by section 6.4.
* Broken-but-present backends never see the button, because the
  button is keyed on "missing". Answered by section 5.5.
* Stop semantics contradict themselves across the two precedents.
  Answered by option set E and section 6.6.
* Progress has no home; the panel cannot hold two jobs and three
  boolean families. Answered by section 6.7 and the state matrix in
  section 6.8.
* The install is machine-wide; the page and log are repo-scoped.
  Answered by section 6.9.
* Authorization is unstated: who may click Install on a
  network-reachable dashboard. Answered by section 7.
* No preflight before a multi-gigabyte download; no failure taxonomy;
  no acceptance criteria, test matrix, or rollback. Answered by
  sections 6.1, 6.2, 8, and 10.
* Grade and route forms are ungated on availability and can ledger a
  spurious "unavailable" record. Answered by option set F.
* Post-install state: installed is not enabled is not grading.
  Answered by option set D.
* No lifecycle after install (version display, update, uninstall,
  repair). Answered by option set G.
* Progress honesty: a log tail is not a percentage. Answered by
  section 6.3 (named phases, no invented percentage).
* Observability: neither runner emits metrics or structured events.
  Answered by section 6.10.
* The value question: measured on real diffs, nothing cleared the 0.7
  threshold (merge-action confidence 0.36–0.52 across 13 grades), so
  auto-accept and auto-reject are effectively dead, and grading is
  advisory. The design must justify why the CDA backend crosses the
  discover-don't-install boundary that worker CLIs do not, or the
  cheaper fits (install-time opt-in, a diagnosing status page with a
  copyable command, a narrowed button) may be the right scope.
  Answered by option set C, which puts that question to the human
  with the evidence, not by assertion.

## 5. What "installed" means, and where it installs

### 5.1 Three layers, one panel state

"Installed" has three layers:

1. **Packages present.** `laya` is importable by the dashboard
   interpreter, and torch is importable with it.
2. **Backend loadable.** A backend object can be constructed without
   loading the checkpoint (the `find_spec` check today proves layer 1
   only; layer 2 needs the subprocess probe in section 5.2).
3. **Checkpoint present and loadable.** The model file is in the
   Hugging Face cache and `laya.load` can read it.

The status panel shows one Backend value. The rule for which layer
flips it is option set A. The default position of this design, if the
human picks no option, is: the panel flips on layer 2 (loadable
backend), and layer 3 is reported separately as "model checkpoint:
downloaded" or "model checkpoint: downloads on first grade". The panel
must never show "installed" on layer 1 alone.

### 5.2 Verification: a fresh subprocess, never an in-process import

After pip exits 0, the runner runs verification as a separate job
phase, in this order:

1. **Presence probe.** A fresh subprocess running the dashboard's own
   interpreter (`sys.executable -m python -c ...`) imports `laya`
   and torch and reports their versions. A fresh subprocess is
   required because the dashboard process's import cache does not see
   packages installed after it started; importing inside the
   dashboard process would also pull torch (2–3 GB) into the server
   process.
2. **Loadability probe.** The same subprocess constructs the backend
   object without loading the checkpoint, if the backend API allows
   it; otherwise the probe stops at the import step and says so
   plainly. The probe never runs a grade and never writes to the
   grade ledger.
3. **Checkpoint probe (only when option set A includes pre-warming).**
   The probe reports whether the checkpoint file exists in the cache
   and is readable. It does not download it unless pre-warming is
   the chosen option.

Verification failure after a pip exit of 0 is its own failure class
("installed but not loadable", section 6.2). It surfaces the repair
path (section 5.5), not a success state. The panel must not flip on
pip exit code alone.

If verification passes but the running dashboard process still cannot
see the backend (import cache), the panel says so explicitly: "Backend
installed. Restart the dashboard to use it." with the restart method
for that deployment (dashboard stop/start, service restart, or
container recreate). A restart must never be silently required: the
panel states it, or the flip happens without one.

### 5.3 Torch strategy and honest sizes

The fixed install sequence (section 7) installs torch before `laya`,
in this order:

* On a machine with a CUDA-capable GPU, install the default torch
  wheel (about 2.5 GB of CUDA libraries) then `laya`.
* On a CPU-only machine, install the CPU torch wheel first from the
  PyTorch CPU index, then `laya` without letting pip re-resolve
  torch to the default wheel.

Detection: the sequence is chosen server-side from platform and GPU
detection done by the dashboard process (for example, whether CUDA
is reported available), never from a browser claim. The exact
detection call is an implementation detail the implementer must name
in code review, because a wrong detection on a CPU-only host is the
2.5 GB mistake this section exists to prevent.

Honest sizes the UI must state before the user clicks (the confirm
step, or the button's detail text when there is no confirm step):

* torch: about 2.5 GB (default wheel) or the CPU wheel size for the
  CPU path.
* `laya` plus transformers: the size pip reports for the resolved
  set, or "a few hundred MB" when it cannot be known in advance.
* Checkpoint: about 842 MB, downloaded on first grade unless
  pre-warming is chosen.
* Temp staging during install and checkpoint reconstruction: at
  least 1 GB of free space in the temp directory, on top of the
  above. TMPDIR handling: the install and verification subprocesses
  run with TMPDIR set to a directory on a real disk (home disk by
  default), never a 512M tmpfs `/tmp`, on every platform. On Windows
  the equivalent is a temp directory on a real disk, not a RAM disk.

Time: pip install takes minutes on a normal connection and longer on
a slow one; the first grade after install takes about a minute to
load the model plus 1–3 minutes for a small diff, up to about 10 for
a large one. The UI states "minutes, not seconds" and shows elapsed
time, never a countdown it cannot justify.

### 5.4 Target environment: decision tree and deployment matrix

The target environment is decided server-side, in this order, and the
decision is shown to the user before the install starts:

1. **The dashboard's own interpreter** (`sys.executable`). The pip
   command is `sys.executable -m pip`, never a `pip` found on PATH.
   If that interpreter's environment is writable by the dashboard
   process, install there. This is the normal case for a venv, an
   editable checkout, and a uv tool environment.
2. **A dedicated venv owned by the dashboard**, when the own
   environment is not writable. Location: a fixed path under the
   dashboard's state directory (for example `<state>/.jev/cda-venv`
   — the exact path is an implementation detail, but it must be one
   fixed path, recorded in the durable job record, and the dashboard
   must discover it on the next start by checking that path, not by
   searching). The dashboard process must then use that venv's
   interpreter for the verification probe and for later grades,
   which means the grade runner must accept an interpreter override;
   if it cannot, this branch is unavailable and the design falls to
   branch 3.
3. **Refusal with a command.** When neither branch works, the page
   shows the environment-specific command for the user to run
   elsewhere (the guide fallback in option set C), and no install
   job starts.

PEP 668 stance, decided here, not left to the implementer: the
install never passes `--break-system-packages`. A system Python that
refuses pip under PEP 668 goes to branch 2 or branch 3. Passing the
flag would silently modify a system Python the dashboard does not
own.

Service accounts: when the dashboard runs as a service account with
no writable home, the Hugging Face cache location must be set
explicitly (via `$HF_HOME` pointing at a writable directory owned by
the service) before any checkpoint download, or the checkpoint probe
reports "no writable cache" as a preflight failure. The install must
not assume `~` exists or is writable.

#### Deployment matrix

| Deployment | Target environment | Persistence | What the UI shows before install |
|---|---|---|---|
| Mac, venv or editable checkout | Own interpreter (branch 1) | Survives dashboard restarts; survives checkout updates; a reinstall of the tool from scratch wipes it | "Installs into the dashboard's Python environment." |
| Mac, uv tool install | Own interpreter (branch 1) | Survives restarts; a `uv tool install --force` reinstall recreates the tool environment and wipes the extra, so the button will be offered again afterwards | "Installs into the dashboard's tool environment. Reinstalling the tool removes it." |
| Windows, user pip/venv | Own interpreter (branch 1) | Survives restarts | "Installs into the dashboard's Python environment." |
| Windows, service | Branch 2 if a writable state path exists, else branch 3 | Branch 2 survives service restarts; the venv path is recorded and re-discovered | Branch 2: as Mac venv. Branch 3: the command to run as the service owner. |
| Linux, venv | Own interpreter (branch 1) | Survives restarts | "Installs into the dashboard's Python environment." |
| Linux, system Python (PEP 668) | Branch 2, else branch 3 | As Windows service | As Windows service. |
| VPS, service account | Branch 2 if writable, else branch 3 | As Windows service; `$HF_HOME` must be writable or preflight fails | As Windows service, plus the cache location. |
| Docker (pinned image, non-root `jev`) | See option set G | By default ephemeral: a container recreate wipes a container-side pip install | The ephemerality warning, or the volume path, per option set G. The button must never be shown on Docker without that statement. |

The matrix is normative: an implementation that shows the button on a
deployment shape without the row's UI statement has not implemented
this design. "Works on a VPS" must not appear in the UI until the VPS
row's persistence answer is true for that machine.

### 5.5 Broken-but-present backends and the repair path

The button is keyed on "backend not usable", not on "backend
missing". Two cases:

* **Absent or unloadable (`available` false).** The Install button
  shows. Clicking it runs the full sequence (preflight, pip, verify).
* **Present but failing loadability (probe fails while packages are
  present).** The panel shows "installed but not loadable" with a
  Repair action. Repair runs the same sequence as install, except it
  first records the failing probe output for the failure message.
  Whether repair is a reinstall, a targeted fix, or out of scope is
  option set G; the panel message for the out-of-scope choice is:
  "The backend is installed but cannot load. Reinstall it with the
  command below, or remove the backend and use the Install button."
  with the environment-specific command.

A backend that passes presence but fails loadability must never be
reported as "installed" with no action. That is the half-installed
state section 4 warns about, and hiding it behind a green badge is
the one outcome this design forbids.

## 6. The install job

### 6.1 Preflight, before any download

Preflight runs synchronously when the user starts an install, before
any package is downloaded. Each check maps to a plain-English refusal
when it fails, and no download starts after a refusal:

* Free disk in the target environment's disk and in the temp
  directory, covering the torch variant plus checkpoint plus temp
  staging for the chosen path (section 5.3). On a small VPS, a pip
  run that fills the disk can break the host itself, so this check
  is not optional.
* Free memory: at least the 2–3 GB the backend needs at runtime, or a
  warning that grading will be slow or may fail, stated before the
  download.
* Writable target environment (branch decision, section 5.4) and
  writable Hugging Face cache location (or `$HF_HOME` set).
* Network reachability to PyPI and to the PyTorch index when the CPU
  path is chosen, and to the Hugging Face host when pre-warming is
  chosen. A failed reachability check refuses with "check the
  network or proxy", not with a pip traceback.
* No other install running (single-flight, section 6.4) and no grade
  running or draining that would conflict (section 6.4).

Preflight results are shown as a short list ("Disk: enough", "Network:
reachable") so a refusal names the failing check, not just "preflight
failed".

### 6.2 Failure taxonomy

Every failure maps to a message, a retry offer, and what must change
before retry. The runner classifies by exit code, pip output markers,
and probe output — never by showing pip's raw output as the message
(the raw log stays available as the log tail).

| Condition | Message (plain English) | Retry offered? | What must change first |
|---|---|---|---|
| No network / DNS / proxy failure | "Could not reach the package index. Check the network or proxy settings." | Yes | Network or proxy fixed |
| Disk full during download or unpack | "Ran out of disk space. Free space and retry." | Yes | Disk freed |
| Temp directory full | "Ran out of space in the temp directory. Point TMPDIR at a disk with free space and retry." | Yes | Temp location or space |
| Permission denied on target | "The dashboard cannot write to its Python environment." plus the branch-3 command | No (button becomes guide) | Permissions, or run the command shown |
| PEP 668 refusal | "This Python refuses direct installs. Use a virtual environment." plus the branch-2/3 path | No (button becomes guide) | A venv, or run the command shown |
| No wheel for this Python / architecture | "No installable package for this Python version or machine." | No | A supported Python, or the docs path |
| Hash mismatch / corrupt download | "The download did not match its expected checksum. Retry." | Yes | Nothing, unless it repeats, then network |
| Process killed (out of memory, host shutdown) | "The install was interrupted. Retry." | Yes | Memory, or host stability |
| Pip exit 0 but verification fails | "Installed but not loadable." plus probe output summary and the repair action | Yes (repair) | Per probe output |
| Checkpoint download fails (pre-warm only) | "The model checkpoint could not be downloaded." | Yes | Network, cache writability, disk |
| Install stopped by the user | "Install stopped. Retry to start again." | Yes | Nothing |

Retry means re-running the full sequence from preflight (section
6.6), not resuming a partial pip run, unless pip itself resumes a
cached download — which the UI must not promise.

### 6.3 Progress honesty: named phases, no invented percentage

Pip output has no overall percentage and no time remaining, so the UI
must not invent one. Progress is shown as named phases, each with its
own signal:

* **Preflight** — the checklist from section 6.1, each item
  pass/fail.
* **Downloading and installing** — the log tail (last 30 lines, as
  the browser precedent), elapsed time, and the current pip line.
* **Verifying** — the probe steps from section 5.2, each pass/fail.
* **Warming up** (only when option set A includes pre-warming) — the
  checkpoint download, with bytes downloaded when the download
  reports them, otherwise elapsed time and log tail only.

The status surface polls the job (live region, as the grade status
page does). Closing the tab loses nothing: the job record is durable
(section 6.5) and the status page shows the same state on return.

### 6.4 Coordination with grading

Install and grade contend for CPU, memory, and disk, and pip
replacing `laya`/torch files under a running grade thread is a file
hazard, not just slowness — on Windows, file locking can make the
install itself fail. The coordination point lives above both runners,
owned by `DashboardCore` (or its equivalent), not inside either
runner, because each runner's lock guards only itself, in one
process.

Rules:

* An install cannot start while a grade is running or draining, and
  a grade cannot start while an install is running. The blocked side
  shows "waiting" with the name of the blocking job, not a generic
  busy error.
* A draining grade blocks an install until the thread exits, because
  the thread still holds torch files. The install page says "a grade
  is finishing; install starts when it ends", and the install may be
  queued or refused — queuing is the friendlier choice, but the
  implementer must pick one and the UI must show which.
* A grade submitted mid-install is refused with a message that links
  to the install status, and it must not ledger a spurious
  "unavailable" record. The ledger guard is: jobs refused because an
  install is running are not recorded as grade outcomes at all.
* The coordination lock is per dashboard process. Two dashboard
  processes on one machine (two repos, two ports) can still contend;
  that case is out of scope and the failure message for it is the
  pip failure taxonomy entry for the condition that surfaces (file
  lock, disk, or verification failure), not a new lock.

### 6.5 Durable job record and restart mid-install

The install job record is durable, not in-memory only. It lives in a
fixed location (the dashboard state directory, not a repo's `.jev/`
directory — see section 6.9 for why), and records: the fixed argv
sequence, the target environment decision, the started time, the
current phase, the exit code when finished, and the verification
outcome. The full log is retained beyond the 30-line tail (section
6.10).

On dashboard start, a record that says "running" but has no living
child process becomes `interrupted`, and the page shows: "The last
install was interrupted by a restart. Retry to start again." The
leftover child, if one is alive, is terminated on shutdown (section
6.11) and orphaned children are never left running past a dashboard
stop. A second install after an interruption truncates the old log
only after copying it to the retained full log, so the interruption
evidence is not destroyed by the retry.

If the implementer cannot provide a durable record, the fallback is
stated here so it is a decision, not an accident: the page can only
report "availability unknown — re-check and retry" after any restart,
and the design's restart acceptance criterion (section 10) fails.

### 6.6 Stop semantics and retry

Stop and retry are defined per phase, and the UI message after a stop
names the phase and the state:

* **Preflight:** stop cancels immediately; nothing was downloaded;
  retry starts from preflight.
* **Downloading and installing:** stop terminates the pip process
  (the browser-install precedent). Partial downloads stay in pip's
  cache where pip can reuse them; partial unpacks are left for the
  next pip run to overwrite. The state after stop is "stopped, safe
  to retry", where "safe" means: no half-installed package is
  reported as installed (verification has not run, so the panel
  still says "not installed"), and retry re-runs preflight then pip.
* **Verifying:** stop cancels the probe; the panel still says "not
  installed" until a probe passes.
* **Warming up:** stop cancels the checkpoint download; the Hugging
  Face cache resumes a partial download on the next attempt where
  the cache supports it, and the UI says so; where it does not, the
  download restarts from zero and the UI says that instead. The
  implementer must not promise resume where the cache does not
  provide it.

Whether a stop button exists at all is option set E. If option set E
picks install-to-completion, this section's stop rows do not apply,
and the page says plainly that an install cannot be stopped once
started, with the shutdown behavior from section 6.11 as the only
exit.

Retry is always re-run from preflight, never resume of a pip run,
except for the checkpoint-download resume named above. The UI copy
for retry is "Retry install", and it re-runs preflight first, so a
changed condition (disk freed, network fixed) is caught before any
download restarts.

### 6.7 Where progress lives

The install job gets its own status surface; it does not reuse
`/code-grade/status` by faking grade fields, and it does not live
only on `/code-grade` where navigating away loses it. Concretely:

* A new route pair under `/code-grade/install` (status page plus a
  live fragment, mirroring `/code-grade/status` and
  `/code-grade/live`), showing phase, elapsed time, log tail, and
  the full log link.
* The Code Grade main page shows a banner while an install runs,
  finishes, or fails, linking to that status page — mirroring the
  existing grade-job flash on the panel.
* The status panel itself shows the install state in the Backend row
  area only as a short state word ("installing", "failed", "stopped",
  "interrupted"); detail lives on the install status page.

### 6.8 State matrix: Enabled × Backend × Install job

Rows the UI must render correctly (Backend here is the panel value
after section 5.1; Install job is none unless an install has run):

| Enabled (gate) | Backend | Install job | What the panel shows |
|---|---|---|---|
| no | not installed | none | Install button (per option set C); grade/route forms per option set F |
| no | not installed | running | Banner "installing…", button disabled, forms per option set F |
| no | not installed | failed | "Install failed: <message>. Retry." plus log link |
| no | not installed | stopped | "Install stopped. Retry." |
| no | not installed | interrupted | "Last install was interrupted by a restart. Retry." |
| no | available (layer 2) | done | "Backend available." plus checkpoint state (downloaded / downloads on first grade) |
| no | installed but not loadable | failed (verify) | "Installed but not loadable." plus repair action (section 5.5) |
| yes | available | done | As "no / available", plus gate grading active |
| yes | not installed | none | Install button; gate records grades as unavailable, never guessed |
| either | available | running (reinstall/repair) | Banner; grading blocked per section 6.4 until it finishes |

Second-click behavior: clicking Install while an install runs does
nothing except show the status page (the button is disabled and
links there). Double-submit from two tabs is stopped by the
single-flight guard, and the second submit sees the running job's
status, not an error.

### 6.9 Scope: machine, not repo

The install targets a Python environment shared by every repo the
dashboard serves, but the button sits on a per-repo page whose status
is built from that repo's config (including its `model_id`). The
design resolves that mismatch as follows:

* The install job record and log live in the dashboard state
  directory, not under any repo's `.jev/` directory. The browser
  precedent's repo-scoped log is not reused, because a repo-scoped
  log would misattribute a machine-wide install to one repo and
  would be invisible from other repos.
* The install status banner appears on the Code Grade page of every
  repo while an install runs, because the backend is shared.
* The `model_id` shown is the current repo's configured model. The
  install installs the backend runtime (`laya` + torch); the
  checkpoint for a repo's model downloads on that repo's first grade
  unless pre-warming is chosen, in which case pre-warming warms the
  current repo's model only, and other repos' models still download
  on their first grade. The UI must say which model's checkpoint was
  warmed, by model id.
* Across repos, a finished install flips every repo's panel, because
  availability is a property of the shared environment, not of a
  repo.

### 6.10 Observability

Beyond the log tail, the install records structured attempt events:
start, phase change, finish, failure with a failure class from
section 6.2 (not just an exit code), stop, and interruption. The full
log is retained (capped the same way the browser precedent caps its
log, 256 KB, when the job finishes) and linked from the status page
for diagnosis. No secrets appear in the log or the events: pip output
is stored as-is except that any credential-looking token in a URL is
redacted before storage, following the dashboard's existing
redaction practice for displayed tokens.

### 6.11 Shutdown contract

The install runner is owned by `DashboardCore` and stopped from
`shutdown_jobs()`, after the browser installer and before or after
the grade runner — the order matters because of section 6.4, and the
implementer must state it: stopping the grade runner first (draining)
and then the install runner (terminating pip) avoids terminating pip
while a grade still holds torch files, so grade shutdown precedes
install shutdown. On shutdown with an install running, pip is
terminated, the job record is marked `interrupted` (not `stopped`,
because the user did not stop it), and the next start shows the
interrupted message from section 6.5.

## 7. Security: the fixed command, and who may press it

### 7.1 The fixed argv rule, and how it is enforced

The install command is a constant argv sequence built on the server
from the environment decision in section 5.4. It is never built from
request data. Enforcement, in code review terms:

* The route handler takes no package, flag, version, or index-URL
  field from the request. The form posts only the CSRF token and,
  when a confirm step exists, the confirmation.
* The argv builder takes only server-side inputs: `sys.executable`,
  the torch path decision (CPU vs default, from server-side
  detection), and the `laya` version policy from option set G. Its
  output is logged (argv list, not a shell string) in the job record.
* The command never installs or upgrades `jev-agent` itself. The
  argv installs `laya` (and torch first, on the CPU path) only. A
  test must assert that `jev-agent` appears nowhere in the built
  argv, on every branch of the decision tree.
* The subprocess is spawned with an argv list, never through a shell.
* No index URL comes from the request. The only index URLs are the
  PyPI default and the PyTorch CPU index, both constants in code.
  A custom index is out of scope; a user who needs one uses the
  branch-3 command.

A server-side install endpoint that accepted a package name from the
browser would be remote code execution by design. This section exists
so that failure mode is closed by construction, not by convention.

### 7.2 Who may press Install

The dashboard's existing gating is inherited, not reinvented:

* On loopback, the existing rule applies: any local visitor can
  reach the dashboard, so any local visitor can press Install. That
  matches the browser-install precedent, which is explicitly
  local-only, and it must be stated in the UI copy for loopback
  dashboards: "Anyone on this machine can install the backend."
* Off loopback, the existing token rule applies: every request must
  carry the dashboard token (cookie, header, or query parameter
  swapped for the cookie by redirect), and the install form posts
  carry the per-session CSRF token through `checked_form`, as every
  other dashboard form does.
* Rate limiting: one install at a time (single-flight) is the limit.
  No additional rate limit is specified; a second submit sees the
  running job (section 6.8), which bounds abuse to one pip process.
* Logging: every install attempt (start, stop, finish, failure
  class) is logged in the attempt events (section 6.10) with the
  requesting context the dashboard already records. No new identity
  system is introduced.
* What someone not allowed to use it sees: on a token-gated
  dashboard without a valid token, the existing token gate refuses
  the request before the install route runs, as it does for every
  other route. There is no separate "not allowed" install page.

The confirm step (whether the button asks first, naming the exact
command, as the browser precedent does) is option set H. Whatever
option set H picks, the security properties above do not change.

## 8. Detailed implementation plan

No code is written in this phase. The list below is the contract for
the implementation phase: every file it touches, and why. File paths
are relative to the repo root.

* `src/jev_agent/dashboard/cda_install_runner.py` (new, or the
  shared-scaffold file from option set B): the install job — argv
  building (section 7.1), preflight (section 6.1), phased execution
  (section 6.3), subprocess verification probe (section 5.2),
  durable job record (section 6.5), stop and shutdown (sections 6.6,
  6.11), failure classification (section 6.2), and attempt events
  (section 6.10). Rationale: this is the only new stateful
  component; everything else is wiring.
* `src/jev_agent/dashboard/core.py` (modify): own the new runner in
  `DashboardCore`, wire it into `shutdown_jobs()` in the order
  stated in section 6.11, and own the install/grade coordination
  lock from section 6.4. Rationale: coordination must live above
  both runners, and shutdown order is a `DashboardCore` decision.
* `src/jev_agent/dashboard/routes_code.py` (modify) and
  `src/jev_agent/dashboard/app.py` (modify): the install route pair
  (`/code-grade/install`, status plus live fragment), the confirm
  route if option set H picks one, the stop route if option set E
  picks one, and the repair route from section 5.5. Rationale: all
  install endpoints live with the code-grade routes, under the same
  `checked_form` protection.
* `src/jev_agent/dashboard/templates/code_grade.html` (modify): the
  Install button, the install banner, the Backend row's install
  states, and the retirement of the pip instruction. Rationale: this
  is the surface the issue names.
* `src/jev_agent/dashboard/templates/code_grade_install_status.html`
  (new) and a live fragment template (new): the install status page
  and its polling fragment, mirroring `code_grade_status.html` and
  `_code_grade_live.html`. Rationale: progress needs its own home
  (section 6.7).
* `src/jev_agent/dashboard/code_grade_runner.py` (modify):
  `unavailable_message()` stops printing the pip command and links
  to the install action instead; grade submission refused while an
  install runs must not ledger a spurious "unavailable" record
  (section 6.4). Rationale: the second of the three pip-instruction
  surfaces, and the ledger guard.
* `src/jev_agent/dashboard/templates/learning.html` (modify): the
  Ratings tab empty state stops printing the pip command and links
  to `/code-grade` instead. Rationale: the third pip-instruction
  surface.
* `src/jev_agent/code_orchestrator/backend.py` (modify only if
  needed): if the loadability probe needs a backend-construction
  entry point that does not load the checkpoint, add it here;
  otherwise no change. Rationale: verification must not import
  torch into the dashboard process, and must not run a grade.
* `src/jev_agent/dashboard/browser_install.py` (modify only if
  option set B picks the shared scaffold): extract the shared job
  scaffold (lock, thread, state, timestamps, `busy()`, `status()`,
  shutdown hook, log cap) that all three runners use. Rationale:
  option set B, not a drive-by refactor.
* `tests/unit/test_cda_install_runner.py` (new): argv tests
  (including "jev-agent never in argv" on every branch), preflight
  refusal tests, failure-classification tests, stop-per-phase tests,
  restart/interrupted tests, and the coordination tests (install
  blocks grade, grade blocks install, refused grade ledgers
  nothing). Test seams: injectable process spawn and an
  availability seam, matching the existing runners' seams, so no
  test downloads torch or a checkpoint.
* `tests/unit/test_cda_install_routes.py` (new): route tests for
  the install pair, CSRF refusal, single-flight double-submit, and
  the three retired pip-instruction surfaces.
* `tests/unit/test_cda_install_matrix.py` (new): the platform ×
  deployment decision-tree tests from section 10, with the
  environment detection stubbed per cell.
* `docs/code-orchestrator.md` (modify): the install button, the
  fixed sequence per platform, the torch sizes, the Docker
  persistence answer, and the manual recovery commands from
  section 10. Rationale: the doc is the surface users read when the
  UI message is not enough.
* `docs/deploy.md` (modify): the Docker persistence answer from
  option set G, and the uv-tool reinstall caveat from section 5.4.
  Rationale: deploy docs must not promise persistence the install
  does not have.
* `docs/dashboard.md` (modify): the Code Grade page's install
  states, if that doc describes the page. Rationale: dashboard docs
  must match the shipped UI.
* `docs/designs/cda-install-button/design.md` (this file, already
  written): updated with the human's option-set decisions before
  implementation starts, so the implementer works from a decided
  spec, not from open options.

Out of the implementation phase, explicitly: no change to
`.github/workflows/`, no change to lint/type/test config, no change
to grading semantics, no change to the threshold, no change to the
model.

## 9. Option sets: the human-approval gate

Each set below has at most three options. None is ranked, none is
marked as preferred, and none is pre-picked. Each option states its
mechanism, its trade-offs, and the conditions under which it fails.
The human picks one option per set before implementation. A set left
unpicked blocks implementation of the parts that depend on it.

### A. What "installed" means

* **A1. Pip complete, verified loadable.** The panel flips when pip
  exits 0 and the loadability probe passes. The checkpoint still
  downloads on the first grade. Mechanism: sections 5.1–5.2 as
  written. Trade-off: the fastest flip, and the first grade still
  takes minutes with a download the user was not warned is coming.
  Fails when: the checkpoint download fails on first grade, after
  the panel already said "installed".
* **A2. Pip complete, verified loadable, checkpoint warmed.** The
  panel flips only when pip, probe, and a checkpoint pre-warm all
  pass. Mechanism: a warm-up phase (section 6.3) that downloads the
  current repo's model checkpoint without grading, writing nothing
  to the grade ledger. Trade-off: the panel's promise is complete —
  the next grade needs no download — but install takes longer, uses
  more disk up front, and warms only the current repo's model.
  Fails when: the checkpoint host is unreachable (install fails
  even though the backend itself works), or disk is short for the
  checkpoint on top of torch.
* **A3. Two states, not one.** The panel shows "backend ready" on
  pip plus probe, and a separate checkpoint line ("downloaded" or
  "downloads on first grade"). Mechanism: the panel renders both
  layers. Trade-off: the most honest, and the most UI surface; the
  user must read two states to know what the next grade will do.
  Fails when: the user reads only the first line and is still
  surprised by the first-grade download.

### B. Runner shape

* **B1. Shared job scaffold, extracted now.** Extract the common
  skeleton (lock, thread, state, timestamps, `busy()`, `status()`,
  shutdown hook, log cap) from the browser and grade runners into
  one scaffold, and build the install runner on it. Mechanism: a
  refactor plus the new runner. Trade-off: future lifecycle fixes
  land once, not three times; but the refactor touches two working
  runners and can regress them. Fails when: the runners' differences
  (log-per-repo vs state-dir log, draining vs terminating) do not
  fit one scaffold without special cases that erase the saving.
* **B2. Deliberate third copy.** The install runner mirrors
  `BrowserInstallRunner` and does not share code. Mechanism: a new
  file, same shape, its own log cap and shutdown. Trade-off: no
  regression risk to working runners; but lifecycle fixes must be
  ported across three runners by hand, and the design must name who
  does that (the implementer, in the PR description, with a
  checklist). Fails when: a later fix lands on one runner and not
  the others, and the drift is discovered by a user-visible bug.
* **B3. Extend the browser runner.** Generalize
  `BrowserInstallRunner` into an install runner that takes a fixed
  argv and a log location, and use it for the CDA install.
  Mechanism: parameterize the existing runner, keep its confirm and
  stop semantics. Trade-off: the smallest new code, and the
  browser-install fixes carry over; but the browser runner's
  repo-scoped log and Node-oriented resolution must be reworked,
  and its name and tests will misdescribe a Python install until
  they are renamed. Fails when: the generalization leaks
  browser-install assumptions (PATH resolution, repo log) into the
  CDA path.

### C. Scope of the button: where it works, and the value question

* **C1. Button everywhere, with per-cell fallback.** The button
  shows on every deployment; where the matrix says it cannot work
  (Docker without a volume, read-only environment), the click runs
  preflight, which refuses with the environment-specific command.
  Mechanism: section 5.4 plus the guide fallback. Trade-off: the
  issue's "same button everywhere" is kept literally, and every user
  gets at least a correct command; but a button that refuses on
  some machines teaches users that the button may not work, and the
  Docker warning must be read to be useful. Fails when: users click
  through the warning and then report the button as broken.
* **C2. Narrowed button.** The button shows only where it can
  actually work (writable dashboard-owned environment, persistent
  deployment, preflight passed). Everywhere else the page shows the
  environment-specific command and preflight verdict (the guide),
  with no button. Mechanism: the page renders the button or the
  guide from the environment decision, before any click.
  Trade-off: zero-terminal operation where it is real, and no false
  promise where it is not; but the issue's "same button everywhere"
  is narrowed, and Docker/VPS users still need a terminal.
  Fails when: environment detection misclassifies a working machine
  as unable, hiding a button that would have worked.
* **C3. Guide only, no execution.** No install button. The page
  detects the environment server-side and renders the exact command
  plus a preflight verdict ("3.1 GB free, need about 4 GB — free
  space first"), a copy button, and a "Check again" action that
  re-runs `available`. Mechanism: detection plus rendering only.
  Trade-off: no server-side code execution, no runner, no lock, no
  stop semantics, no orphan handling, and it works on Docker and
  read-only environments; but it gives up zero-terminal operation
  everywhere, so the browser-first law is met by diagnosis, not by
  installation. Fails when: the human's goal is zero-terminal
  installation itself, not just a better command — in which case
  this option does not satisfy the issue.

  The value evidence for this set: measured on real diffs, nothing
  cleared the 0.7 threshold (merge-action confidence 0.36–0.52
  across 13 grades), grading is advisory, off by default, and heavy
  (multi-GB, minutes per grade). The established pattern is
  discover-and-guide (worker CLIs are found, not installed; the
  browser install is the lone local-only exception). The human
  decides whether install friction, rather than the feature being
  opt-in, heavy, and advisory, is what blocks adoption — and
  whether the CDA backend crosses the discover-don't-install
  boundary worker CLIs do not. If that evidence does not hold, C3
  (or C2) is the cheaper fit, and the rest of this design narrows
  to the guide plus detection work.

### D. Post-install grading enablement

* **D1. Leave grading off.** Install changes no config. After
  success, the panel says "Backend available. Grading from this page
  works on demand. To have the gate grade every diff, turn it on
  under Settings → Code orchestrator." Mechanism: no config write.
  Trade-off: the install job never becomes a config writer, matching
  every existing runner; but a user who installed in order to get
  gate grading must take a second step in a second place.
  Fails when: the user expects the install to have "turned grading
  on" and wonders why the gate still grades nothing.
* **D2. Auto-enable gate grading.** A successful install flips
  `[code_orchestrator] enabled` to true, and the panel discloses it:
  "Backend installed and gate grading turned on." Mechanism: the
  install job writes config on success only. Trade-off: one click to
  working gate grading; but the install job becomes the first runner
  that writes config, and a user who wanted on-demand grading only
  gets gate grading they did not ask for. Fails when: gate grading
  on a fresh install surprises a run with slow grades (minutes per
  diff) the user did not expect.
* **D3. Enable from the status page.** After success, the panel
  offers a second button, "Turn on gate grading", which flips the
  setting from the same page (a config write through the existing
  settings route, not through the install job). Mechanism: install
  writes nothing; the enable action is separate and explicit.
  Trade-off: one page, two explicit steps, no surprise; but it is a
  second control to build and test. Fails when: the settings route
  cannot be reused from this page without a settings-page round
  trip, in which case it degrades to D1 with a link.

### E. Stop

* **E1. Terminate, like the browser install.** A Stop button
  terminates the pip process (and the probe, and the warm-up
  download). Post-stop state follows section 6.6. Mechanism:
  process termination. Trade-off: the user stays in control during
  a multi-minute install; but a terminated torch install leaves
  partial state that only the next pip run cleans up, and the UI
  must not call that "clean". Fails when: termination leaves a pip
  lock or partial wheel that makes the next run fail until the
  cache is cleared — the failure taxonomy must then name cache
  clearing as the remedy.
* **E2. Install to completion, no stop.** Once started, an install
  runs to success or failure; dashboard shutdown is the only exit
  (section 6.11, marked `interrupted`). Mechanism: no stop route.
  Trade-off: no partial-state semantics to define or test; but a
  user on a slow connection cannot abandon a multi-GB download
  except by stopping the dashboard. Fails when: the user stops the
  dashboard mid-install and the interrupted path (section 6.5) is
  the only recovery, which it is designed to handle.
* **E3. Stop only before download.** A Stop button exists during
  preflight and the early download, and disappears once unpacking
  starts. Mechanism: phase-gated stop. Trade-off: control where
  stopping is cheap, no partial-unpack semantics; but the phase
  boundary is a new rule users must learn, and the button vanishing
  mid-install needs an explanation on the page. Fails when: the
  phase boundary is misreported (pip's phases are not cleanly
  observable), so the button disappears at the wrong time.

### F. Grade and route forms while the backend is missing or installing

* **F1. Forms disabled, install as the only action.** While the
  backend is not usable, the grade and route forms are disabled and
  the panel shows the Install button (or the install status) as the
  only action. Mechanism: template condition on availability and
  install state. Trade-off: no spurious "unavailable" ledger
  records, and one call to action; but a user who wants to see the
  failure path, or who has another way to install, cannot submit.
  Fails when: the forms are disabled by a transient availability
  misread, blocking grading that would have worked.
* **F2. Forms live, failure links to install.** The forms stay
  submittable; a failed job's message links to the install action
  instead of printing the pip command, and a job refused because an
  install is running ledgers nothing (section 6.4). Mechanism: the
  existing failed-job path, with the message changed. Trade-off:
  preserves existing behavior and lets the user see the failure;
  but the page carries two calls to action (submit, install), and a
  submitted grade that fails still costs the user a wait.
  Fails when: repeated failed submissions ledger noise — guarded by
  the refused-job rule, but failed (not refused) jobs still ledger
  "unavailable" as today.

### G. Lifecycle and version policy

* **G1. First install only.** The design covers install, verify,
  repair of a failed verify, and nothing else. No version display,
  no update, no uninstall. After install, version drift and
  staleness have no detection and no message, and the next Baton
  upgrade may wipe or strand the extra (the uv-tool caveat in
  section 5.4). Mechanism: scope statement, plus the panel message
  for a stranded backend ("Backend not found after upgrade. Use
  the Install button."). Trade-off: the smallest scope that
  satisfies the issue; but it accepts that the browser-first
  justification is partial, because update and uninstall stay
  terminal. Fails when: a `laya` upgrade breaks the backend and the
  user has no in-browser repair beyond reinstall.
* **G2. Minimal lifecycle.** First install plus: installed version
  display (from the probe), a Repair action for broken-but-present
  backends (section 5.5), and an Uninstall action that removes
  `laya` (never torch alone, never `jev-agent`) and returns the
  panel to "not installed". No update action. Mechanism: two more
  routes reusing the runner. Trade-off: the user can recover from
  every broken state from the browser; but uninstall must define
  what happens to the checkpoint cache (kept, so reinstall is
  faster, and stated) and to other tools sharing torch. Fails when:
  uninstall removes a torch another tool depends on — guarded by
  refusing uninstall when torch was pre-existing, which the
  implementer must detect and state.
* **G3. Version policy without lifecycle UI.** The `laya` version
  policy is decided (pin to a tested version, or keep
  `laya>=0.4.15` with a minimum-version check in the probe), and
  the panel shows the installed version, but there is no update or
  uninstall button. Mechanism: probe reports version; policy is a
  constant. Trade-off: staleness becomes visible without building
  update flows; but visibility without action frustrates the user
  who sees an old version and cannot update it from the page.
  Fails when: the pinned version lags behind a `laya` fix the
  backend needs, and the only remedy is a terminal command — the
  same gap this design exists to close, in miniature.

  The `laya` version policy itself (pin vs `>=0.4.15`) is decided by
  whichever G option the human picks: G1 and G2 keep `>=0.4.15`
  unless the implementer finds a breaking version during testing,
  in which case the policy becomes a pin and the design is amended
  before implementation; G3 decides the policy up front.

### H. Confirmation

* **H1. Confirm first, naming the exact command.** The Install
  button goes to a confirm page (a separate template, as the
  browser precedent does) that names the exact argv sequence, the
  target environment, the sizes from section 5.3, and the
  persistence statement from section 5.4. The user confirms, and
  the install starts. Mechanism: confirm template plus route.
  Trade-off: no surprise multi-GB download, and the persistence
  warning is read before money (disk, time) is spent; but it is a
  second click, so "one-click install" becomes "two-click install"
  with the first click being consent. Fails when: users treat the
  confirm page as friction and the acceptance test's "one click"
  is read literally.
* **H2. Direct install with a preflight summary.** Clicking Install
  runs preflight immediately and shows its summary (environment,
  sizes, persistence) with the progress page; there is no separate
  confirm step. Mechanism: preflight output rendered as the first
  phase. Trade-off: one click to a running install, and the
  persistence statement is still shown — but after the decision,
  not before it, so a user who would have declined on Docker
  grounds has already started a download (which they can stop only
  under option set E). Fails when: preflight passes and the user
  regrets the download size after it starts.
* **H3. No confirm, no summary page.** Clicking Install starts the
  install and the banner shows progress; sizes and persistence are
  stated in small text under the button before the click.
  Mechanism: button plus detail text. Trade-off: the fewest clicks
  and the simplest UI; but the persistence warning is small text
  that is easy to miss, and the Docker ephemerality case is the one
  where missing it costs the most. Fails when: a Docker user
  installs, the container recreates, and the backend is gone with
  no memory of having been warned.

## 10. Acceptance criteria, test matrix, and rollback

### Acceptance criteria (measurable)

* From a fresh "not installed" state on the Mac live dashboard, one
  click (plus a confirm click if option set H picks one) installs
  the backend with no terminal use, and the status panel reports
  installed per option set A.
* Progress (phase, elapsed time, log tail), success, and failure
  states are all visible in the browser, and closing the tab loses
  nothing.
* After install, grading can be enabled from the UI per option set
  D (or the panel states the Settings path, per D1).
* No guessed grades while the backend is missing (existing behavior,
  unchanged), and no install outcome is written to the grade ledger.
* The fixed-argv test passes on every branch: `jev-agent` never
  appears in the built argv.
* Restart test: a dashboard restart mid-install yields the
  `interrupted` state and a working retry, not a stuck "running".
* The three pip-instruction surfaces show no pip command after the
  change.

### Test matrix (platform × failure)

Platforms: Mac venv, Mac uv tool environment, Mac system Python,
Windows user install, Windows service, Linux venv, Linux system
Python (PEP 668), VPS service account, Docker. Each crossed with:
success; each failure class from section 6.2; stop during install
(where option set E provides one); restart during install;
grade during install; double-submit; partial-install recovery;
verification failure after pip exit 0; checkpoint-cache unwritable;
TMPDIR on a small tmpfs. Tests stub process spawn, environment
detection, disk, and network — no test downloads torch or a
checkpoint. Live verification on the Mac (implementation phase
only) covers: fresh "not installed" state, one-click install,
progress visibility, success flip, grading enablement, and one
failure injection (network off) with retry.

### Rollback

* What can be undone: a failed or stopped install, by retry or by
  removing the installed packages (see option set G for uninstall).
  A successful install can be undone by uninstalling `laya` (G2)
  or, under G1/G3, by the manual command below.
* What cannot be undone: time and disk spent on a download; a
  checkpoint download that filled a disk (freeing space is manual).
* Manual recovery commands, per failure the button cannot fix
  (environment-specific; the page shows the one for the detected
  environment):
  - Remove a broken backend: the pip uninstall command for `laya`
    in the detected environment.
  - Clear a corrupt pip cache: the pip cache purge command for the
    detected environment.
  - Clear a corrupt checkpoint cache: remove the model directory
    under the Hugging Face cache (path shown from the detected
    `$HF_HOME` or default), so the next grade re-downloads it.
  - Recover from a filled disk: free space, then retry; the button
    re-runs preflight first.
  - Recover from a Docker wipe: re-run the install after recreate,
    or add the volume from option set G and reinstall once.

## 11. Alternatives considered

* **Copy button plus "Check again".** Replace the bare pip command
  with the exact command for the detected environment, a
  copy-to-clipboard button, and a "Check again" button re-running
  `available`. Rejected as the whole answer because it keeps the
  terminal step, but kept as the branch-3 fallback and as option
  set C3. It is the honest baseline every other option is measured
  against.
* **Environment-aware install guide, no execution.** Detect the
  environment and render the correct command plus a preflight
  verdict. Rejected as the whole answer for the same reason, but
  kept as option set C3 and as the fallback inside C1/C2. It reuses
  the detection work the button needs anyway, so building the guide
  first is not wasted work if the human picks C1 or C2 later.
* **Auto-install on first grade.** Rejected: it hides a multi-minute,
  multi-GB download behind a "Grade diff" click, with no consent and
  no progress story, and it fails the same way on Docker without
  solving persistence. An explicit button with progress is more
  honest than a surprise download.
* **Bundle the model with Baton.** Rejected: about 842 MB plus torch
  would bloat every install for a feature that is off by default.
  The opt-in extra stays the right shape; only the way it gets
  installed changes.
* **Narrowed button.** Not rejected; it is option set C2.

## 12. Open questions and blockers for the design

Stated plainly, not papered over. None of these blocks drafting, but
each blocks some part of implementation until answered, and the
human or the implementer must answer it in the implementation phase.

1. **uv tool environment behavior on the Mac.** Does
   `sys.executable -m pip` into a uv-managed tool environment work
   and persist across dashboard restarts, and does a
   `uv tool install --force` reinstall wipe it (assumed yes in
   section 5.4, not proven)? The implementer must prove this on the
   Mac before the Mac row of the matrix can be marked done.
2. **Docker image writability.** Can the non-root `jev` user in the
   pinned image run pip at all, and into what path? If pip is
   refused, the Docker row collapses to branch 3 and option set G
   decides between a volume and ephemerality. Not proven from the
   docs alone.
3. **Windows service account home and cache.** Where does the
   Hugging Face cache live for a Windows service account, and is it
   writable? The design assumes `$HF_HOME` can be pointed at a
   writable directory; the implementer must prove the service
   account can own one.
4. **Backend-construction probe.** Can a backend object be
   constructed without loading the checkpoint, or does the probe
   stop at import? Section 5.2 allows both, but the verification
   strength differs, and the implementer must name which one ships.
5. **GPU detection call.** The exact server-side detection that
   chooses the CPU vs default torch wheel is not named in this
   design (section 5.3 says the implementer must name it in code
  review). A wrong detection is a 2.5 GB mistake, so this is a
  code-review gate, not a footnote.
6. **Dedicated-venv grade path.** If branch 2 of section 5.4 is
  used, the grade runner must accept an interpreter override for
  the verification probe and later grades. Whether that override
  exists or must be added is an implementation-phase finding; if it
  cannot be added cleanly, branch 2 is unavailable.
7. **`laya` version drift.** `laya>=0.4.15` is unpinned above. If a
  newer `laya` breaks the backend, the version policy in option set
  G must become a pin, and the design must be amended before
  implementation proceeds on that version.
8. **Two dashboard processes, one machine.** The coordination lock
  is per process (section 6.4). Two dashboards on one machine can
  still contend for torch files. That case is out of scope, but its
  failure message is whatever pip surfaces, which may confuse the
  user. Naming a better message for it is implementation-phase work
  if it surfaces in testing.
