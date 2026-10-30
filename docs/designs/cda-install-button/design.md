# CDA install button: one-click backend install from the dashboard

* **Author:** Hana (for Gane)
* **Date:** 2026-10-30
* **Tracking issue:** Gane2506/baton#366
* **Status:** Design phase only. No implementation. Picks for the
  option sets in section 9 were DECIDED 2026-10-30 by the
  implementation coordinator under the user's never-stop directive
  (work must never stop at approval gates); implementation proceeds
  from this decided spec. Any pick the user later overrules gets
  reworked; that is expected.
* **How to read this design (added in resolution):** Sections 5–8 state
  mechanisms, not decisions, wherever a rule depends on an option set
  in section 9 — and each such rule names the option it depends on.
  Nothing in sections 5–8 is decided until its option is picked,
  except the mechanics explicitly marked DECIDED. Where the first
  draft stated a decided rule that depended on an unpicked option,
  resolution rewrote it as conditional; the resolution report lists
  every such rewrite. The brainstorm brief is dated 2026-10-29 and
  its lens reports 2026-10-30 (UTC); this design is dated 2026-10-30.
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
page. One click plus confirm installs the backend on the machine the dashboard runs
on, in the background, with progress shown in the browser and a check
afterwards. No package bytes flow through the browser: the page
itself still loads over HTTP as usual, but the install downloads
happen on the server. The install runs where the dashboard server
runs, so the same button must have a stated answer on every machine
the dashboard runs on: Mac, Windows, Linux, and a VPS.

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
* **Checkpoint:** the trained model file, about ~842 MB, downloaded into
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
* **argv:** the argument list a process is started with, as a list of
  strings, never joined into a shell string.
* **Wheel:** a packaged Python distribution file that pip installs.
* **Index URL:** the web address of a package index pip downloads
  from (PyPI by default; the PyTorch CPU index for the CPU path).
* **venv:** a self-contained Python environment (its own interpreter
  and packages) created with `python -m venv`.
* **PEP 668:** a Python rule under which a system Python refuses
  direct pip installs, to protect system-managed packages.
* **Site-packages:** the directory where a Python environment keeps
  its installed packages.
* **Subprocess:** a separate process started by the dashboard, with
  its own memory; installing or importing in a subprocess does not
  affect the dashboard process's own imports.
* **tmpfs:** a temporary filesystem held in memory, with a fixed size
  (512M on the dev VM's `/tmp`).
* **Live region / live fragment:** a page area that refreshes itself
  by polling the server, and the template fragment it reloads.
* **Flash:** a one-time message shown after an action (success or
  error), gone on the next page load.
* **Banner:** a persistent notice on a page that links to a job's
  status page.

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
    256 KiB (262,144 bytes) when the install finishes, the install can be stopped by
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
* The pip instruction appears on three dashboard surfaces, and all
  three must change together: the status panel (`code_grade.html`),
  the failed grade message (`unavailable_message()` on
  `/code-grade/status`), and the Learning page Ratings tab empty
  state (`src/jev_agent/dashboard/templates/learning.html`). The
  dashboard UI never renders the backend modules' own "pip install
  laya" exception text (failed dashboard grades render
  `unavailable_message()`, not the exception), and the CLI keeps
  its own guidance — neither is a fourth dashboard surface.
  A dashboard process serves one repo (its `settings.root`); see
  section 6.9 for what that means for a machine-wide install.
* A third runner, `ReverseRunner` (`design_reverse.py`), exists
  for reverse-from-Figma work; it is in scope for the scaffold
  assessment in option set B, not an afterthought.
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

DECIDED 2026-10-30: C1, A2, D3; the C2/C3, A1/A3, D1/D2 wordings below are retained alternatives, not live options.

### Goals (as specified by issue #366; option set C can narrow them — a C2 or C3 pick removes the button goals it replaces with the guide)

* An Install button on the Code Grade status panel, shown when the
  backend is not usable, that installs the backend server-side, in the
  background, in the dashboard's own environment, with progress and a
  log tail visible in the browser.
* Verification after install, in a fresh subprocess, that proves
  the backend is constructible (section 5.2) — not only that a
  package is present. The status panel flips per option set A
  without a dashboard restart when verification passes in the
  running process's environment, or says a restart is needed when
  it cannot.
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

The brainstorm lenses raised 16 blockers. The design addresses each
one in the section noted; where the address is an option set, the
section names the option, and nothing in this section is decided
until that option is picked. A blocker with no addressing section
is a gap in this design, not a deferred detail.

* Presence is not loadability. A pip run that leaves torch broken or
  partial still flips a `find_spec` check to "installed", and the
  first real grade then fails as an inference error — which blocks
  the quality gate fail-closed, where a missing backend would have
  let the run proceed. A half-installed backend is worse than a
  missing one. Addressed in section 5.2 (subprocess loadability
  verification) and section 5.5 (repair).
* The obvious fixed command is wrong. Three wordings exist today
  (template and grade runner: `jev-agent[code-orchestrator]`;
  `backend.py`: `laya`; docs: CPU torch wheel, then `laya`). Running
  the page's command verbatim asks pip to install the dashboard's own
  package into its own running environment. Addressed in section 7
  (the fixed argv installs `laya` only, never `jev-agent` itself)
  and option set G (version policy).
* "Same button everywhere" contradicts the deploy model. A Docker
  container-side pip install is refused or wiped on recreate; a
  `pipx`-style tool reinstall recreates the environment and strands
  an extra installed into it. Addressed in the matrix in section 5.4:
  every cell states persistence or ephemerality, and the UI shows it
  before the user clicks.
* The true footprint is several gigabytes, not ~842 MB. The default
  torch wheel carries about 2.5 GB of CUDA libraries, plus `laya`,
  transformers, the ~842 MB checkpoint, and temp staging. Addressed in
  section 5.3 (torch strategy and honest sizes) and section 6.1
  (preflight).
* "Installed" is three layers and `available` checks only the first
  two. Addressed in section 5.1.
* The target environment is a decision tree, not a default. PEP 668
  refuses system-Python pip installs; service accounts may have no
  writable home or no pip; a dedicated venv needs a location and a
  discovery path for the next start; a `--user` install may not be
  visible to the dashboard interpreter. Addressed in section 5.4.
* A third bespoke runner triples a duplicated skeleton. Addressed in
  option set B.
* All install state today is in memory; restart mid-install is
  undefined. Addressed in section 6.5 (durable job record,
  `interrupted` state).
* Install/grade coordination is missing, and the hazard is file
  replacement, not just contention. Addressed in section 6.4.
* Broken-but-present backends never see the button, because the
  button is keyed on "missing". Addressed in section 5.5.
* Stop semantics contradict themselves across the two precedents.
  Addressed in option set E and section 6.6.
* Progress has no home; the panel cannot hold two jobs and three
  boolean families. Addressed in section 6.7 and the state matrix in
  section 6.8.
* The install is machine-wide; the page and log are repo-scoped.
  Addressed in section 6.9.
* Authorization is unstated: who may click Install on a
  network-reachable dashboard. Addressed in section 7.
* No preflight before a multi-gigabyte download; no failure taxonomy;
  no acceptance criteria, test matrix, or rollback. Addressed in
  sections 6.1, 6.2, 8, and 10.
* Grade and route forms are ungated on availability and can ledger a
  spurious "unavailable" record. Addressed in option set F.
* Post-install state: installed is not enabled is not grading.
  Addressed in option set D.
* No lifecycle after install (version display, update, uninstall,
  repair). Addressed in option set G.
* Progress honesty: a log tail is not a percentage. Addressed in
  section 6.3 (named phases, no invented percentage).
* Observability: neither runner emits metrics or structured events.
  Addressed in section 6.10.
* The value question: measured on real diffs, nothing cleared the 0.7
  threshold (merge-action confidence 0.36–0.52 across 13 grades), so
  auto-accept and auto-reject are effectively dead, and grading is
  advisory. The design must justify why the CDA backend crosses the
  discover-don't-install boundary that worker CLIs do not, or the
  cheaper fits (install-time opt-in, a diagnosing status page with a
  copyable command, a narrowed button) may be the right scope.
  Addressed in option set C, which puts that question to the human
  with the evidence, not by assertion.

## 5. What "installed" means, and where it installs

### 5.1 Three layers, one panel state

"Installed" has three layers. The words matter here, because the
first draft used "loadable" for two different things:

1. **Packages present.** `laya` is importable by the dashboard
   interpreter, and torch is importable with it. This is all
   `CodeOrchestrator.available` (via `find_spec`) proves today.
2. **Backend constructible.** A backend object (`LayaBackend` or
   `CodeOrchestrator`) can be constructed without loading the
   checkpoint. Construction is lazy in the cited code
   (`LayaBackend.__init__` only stores the model id; loading
   happens in `_agent_or_load` on first grade), so layer 2 proves
   import plus wiring, and nothing about the checkpoint.
3. **Checkpoint loadable.** The model file is in the Hugging Face
   cache and `laya.load` can read it. Layer 3 is proven only by
   actually loading it: the warm-up subprocess (option set A2) or a
   first grade. No probe short of a load proves layer 3.

The status panel shows one Backend value, and which layer flips it
is option set A — no flip rule is decided until A is picked, and
there is no default. The panel must never show "installed" on layer
1 alone, and must never claim layer 3 without a load having
succeeded. Under A3, the panel shows two lines: the Backend value
from layers 1–2, and a checkpoint line that reads "downloaded" only
when a warm-up success is recorded in the job record, otherwise
"downloads on first grade". DECIDED (2026-10-30, A2): the panel
flips to "warmed" only for the warmed model id, using the section
6.5 probe record's `warmed` flag plus its model id field. Another
repo's dashboard flips to "available" (layers 1–2, shared
environment) but NOT to "warmed" for its different model id. Panel
logic must compare model ids; a warmed record for one model id
never warms a different model id. The panel's source of truth is:
`CodeOrchestrator.available` for presence, plus the recorded probe
verdict (section 5.2) when a probe has run; a probe verdict never
overrides a failed `available` check into "installed".

### 5.2 Verification: a fresh subprocess, never an in-process import

DECIDED (not an option): verification runs in a fresh subprocess,
never by importing inside the dashboard process, because the
dashboard process's import cache does not see packages installed
after it started, and importing torch (2–3 GB) into the server
process is itself a hazard. The probe command is:

```
<sys.executable> -c "import laya, torch; print(laya.__version__, torch.__version__, torch.version.cuda)"
```

followed, in the same subprocess, by constructing
`LayaBackend(model_id)` without calling any load method. The probe
steps, in order:

1. **Presence and versions.** Import `laya` and torch; report their
   versions and whether the torch build is CPU or CUDA
   (`torch.version.cuda` is None for a CPU build). This proves
   layer 1 and records which torch variant actually landed.
2. **Constructibility.** Construct the backend object without
   loading. This proves layer 2 as defined in section 5.1 — import
   plus wiring only. It does not prove the checkpoint loads, and
   the probe output must say so.
3. **Checkpoint load (only when option set A2 is picked).** A
   warm-up subprocess runs `laya.load(model_id)` with TMPDIR set to
   a real disk (section 5.3) and `$HF_HOME` set when the service
   account needs it (section 5.4). It runs no grade and writes
   nothing to the grade ledger. Success proves layer 3 for that
   repo's model id; failure is its own failure class
   ("checkpoint could not be loaded", section 6.2).

The probe never runs a grade and never writes to the grade ledger.
Probe subprocess timeout: 5 minutes, applying to probe steps 1–2
only. A probe step 1–2 run that exceeds it fails as
"verification timed out". Step 3 has no wall-clock timeout; its exits are user stop per
section 6.6 and dashboard shutdown per section 6.11. Pip itself likewise has no
wall-clock timeout, because a multi-gigabyte download has no honest
deadline — its exits are user stop per section 6.6 and dashboard shutdown per section 6.11.
Verification failure after a pip exit of 0 is its own failure class
("installed but not constructible", section 6.2). It surfaces the
repair path (section 5.5), not a success state. The panel must not
flip on pip exit code alone.

A **Verify** action is always present on the panel when the backend
is present: it re-runs steps 1–2 (and step 3 only when a warm-up
record exists for that model). Verify uses the same timeouts as the
install probe: 5 minutes for steps 1–2, none for a step-3 re-run.
Verify refreshes the section 6.5 `probe_by_model` map entry for its
model (a step-3 re-run when a warm-up record exists for that
model). A failed Verify surfaces the repair
path. This is how a broken-but-present backend reaches repair
without waiting for a failed grade.

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

Detection (DECIDED): the sequence is chosen server-side, never from
a browser claim, and never by asking torch — torch is not installed
yet at decision time, so asking it whether CUDA is available is
circular. The rule is:

* macOS: CPU path, always. No CUDA path exists for this stack on Mac.
* Linux or Windows: default path only when an NVIDIA driver query
  succeeds (`nvidia-smi` exits 0); otherwise CPU path.
* A wrong answer costs size, not breakage: the default wheel runs on
  a CPU-only host too, just with about 2.5 GB of CUDA libraries
  aboard. The probe in section 5.2 records which variant actually
  landed, so a wrong detection is visible in the job record.

### 5.3a Fixed argv sequences (DECIDED)

The fixed command of section 7 is these sequences, built from
`sys.executable` (the dashboard's own interpreter) and nothing from
the request. "Constant" in section 7 means no request input, not
byte-identical across machines: the interpreter path and the torch
path vary by machine, and nothing else varies.

* **Default path:** `[sys.executable, -m, pip, install,
  "laya>=0.4.15"]`.
* **CPU path** (from `docs/code-orchestrator.md`): first
  `[sys.executable, -m, pip, install, torch, --index-url,
  https://download.pytorch.org/whl/cpu]`, then `[sys.executable,
  -m, pip, install, "laya>=0.4.15"]`. The second install keeps the
  CPU torch already installed, because pip does not replace a
  requirement that is already satisfied; no `--no-deps` flag and no
  constraint flag is used, because skipping dependency checks would
  also skip transformers.
* **Repair:** `[sys.executable, -m, pip, uninstall, -y, laya]`
  followed by the install sequence for the detected path.
  Uninstall-then-install is chosen over `--force-reinstall` because
  a plain reinstall is a no-op when pip believes the package is
  already satisfied, and `--force-reinstall` on `laya` would
  re-resolve torch and could replace a CPU torch with the default
  wheel.
* **Branch-2 venv creation:** `[sys.executable, -m, venv,
  ~/.jev/cda-venv]`; the install sequence then runs under that
  venv's interpreter instead of `sys.executable`.
* **Pip bootstrap (preflight only, never an install step):**
  `[interpreter, -m, ensurepip]`, where `interpreter` is the target
  interpreter from section 5.4 (the dashboard's own interpreter on
  branch 1, the venv interpreter on branch 2). It runs only when the
  section 6.1 pip-presence check fails. It installs pip from that
  interpreter's own bundled wheel: no download, no network, and no
  dependence on `uv` being on PATH. The actual install still runs
  `interpreter -m pip ...` afterwards — the bootstrap never replaces
  the install sequences above.
* **Warm-up (option set A2 only):** `[sys.executable, -c, ...]`
  running `laya.load(model_id)` as in section 5.2, step 3 — not a
  pip command at all.

`jev-agent` appears in none of these sequences. The version policy
is the floor from `pyproject.toml`, `laya>=0.4.15`, kept as-is; a
pin replaces it only if testing finds a breaking `laya` version,
in which case this design is amended before implementation proceeds
on that version (see option set G).

"Honest" in this design means one thing: no invented number. Every
size below is either a measured figure from `docs/code-orchestrator.md`
or a planning figure whose arithmetic section 6.1 shows.
Honest sizes the UI must state before the user clicks (the confirm
step, or the button's detail text when there is no confirm step):

* torch: about 2.5 GB (default wheel) or the CPU wheel size for the
  CPU path.
* `laya` plus transformers: about 0.5 GB planning figure (see the
  section 6.1 arithmetic; to be confirmed by a measured test install).
* Checkpoint: about ~842 MB, downloaded on first grade unless
  pre-warming is chosen.
* Temp staging during install and checkpoint reconstruction: at
  least 1 GB of free space in the temp directory, on top of the
  above. TMPDIR handling: the install and verification subprocesses
  run with TMPDIR set to a directory on a real disk (home disk by
  default), never a 512M tmpfs `/tmp`, on every platform. On Windows
  the equivalent is a temp directory on a real disk, not a RAM disk.

On Windows the same real-disk rule applies through the TEMP/TMP
environment variables: the install subprocesses set them to a
real-disk directory when the default points at a RAM disk.
Time: pip install takes minutes on a normal connection and longer on
a slow one; the first grade after install takes about a minute to
load the model plus 1–3 minutes for a small diff, up to about 10 for
a large one. The UI states "minutes, not seconds" and shows elapsed
time, never a countdown it cannot justify.

### 5.4 Target environment: decision tree and deployment matrix

The target environment is decided server-side, in this order, and the
decision is shown to the user before any download starts (on the confirm page under H1, in detail text under the button under H3, or as preflight output under H2):

1. **The dashboard's own interpreter** (`sys.executable`). The pip
   command is `sys.executable -m pip`, never a `pip` found on PATH.
   If that interpreter's environment is writable by the dashboard
   process, install there. This is the normal case for a venv, an
   editable checkout, and a uv tool environment. A uv tool
   environment ships without pip: that is not a branch-3 case.
   Preflight (section 6.1) bootstraps pip via ensurepip and
   re-checks; only a bootstrap that still leaves pip missing falls
   to the guide.
2. **A dedicated venv owned by the dashboard**, when the own
   environment is not writable. Location (DECIDED): `~/.jev/cda-venv`
   — the same user-level directory that already holds
   `~/.jev/design-systems/`, created with mode 0700. The dashboard
   discovers it on the next start by checking that fixed path, not
   by searching, and records it in the durable job record. The
   grade runner must accept an interpreter override so the
   verification probe and later grades use that venv's interpreter;
   section 8 lists that override as required work for this branch,
   and if it cannot be added cleanly, this branch is unavailable
   and the design falls to branch 3.
3. **Refusal with a command.** When neither branch works, the page
   renders the guide block in place of the button — the
environment-specific command, a copy button, and a "Check again"
   action — and no install job starts. The button is replaced, not
   disabled in place, so the user is never left staring at a dead
   button. Branch 3 is a pre-click environment decision, rendered
   at page render from this branch decision; it is distinct from
   the section 6.1 preflight checks, which run only after the user
   starts an install.

   DECIDED (2026-10-30, C1 exception): branch 3 keeps guide
   replacement — the button is replaced by the guide and no job
   starts. C1's preflight-refusal applies only where a job can
   start or a failure is learned at preflight; it never turns a
   branch-3 guide into a button that refuses after the click.

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
| Docker (pinned image, non-root `jev`) | See option set I | By default ephemeral: a container recreate wipes a container-side pip install | The ephemerality warning, the volume path, or the guide, per option set I. The button must never be shown on Docker without that statement. |

The matrix is normative: an implementation that shows the button on a
deployment shape without the row's UI statement has not implemented
this design. "Works on a VPS" must not appear in the UI until the VPS
row's persistence answer is true for that machine.

### 5.5 Broken-but-present backends and the repair path

The button is keyed on "backend not usable", not on "backend
missing". Two cases:

* **Absent or unloadable (`available` false).** The Install button
  shows. Clicking it runs the full sequence (preflight, pip, verify).
* **Present but failing verification (probe fails while packages are
  present).** The panel shows "installed but not constructible"
  (or "checkpoint could not be loaded" for a warm-up failure) with a
  Repair action, reachable both from that panel state and from a
  failed Verify action (section 5.2). Repair runs the sequence in
  section 5.3a: uninstall `laya`, then the install sequence for the
  detected path, then the probe again; it first records the failing
  probe output for the failure message. Whether repair exists at all
  beyond this reinstall, and whether uninstall exists as a separate
  action, is option set G; the panel message for a repair that
  itself fails is: "Repair did not fix the backend. Remove it with
  the command below, or use the Install button to start again."
  with the environment-specific uninstall command.

A backend that passes presence but fails loadability must never be
reported as "installed" with no action. That is the half-installed
state section 4 warns about, and reporting it as installed is
the one outcome this design forbids.

## 6. The install job

### 6.1 Preflight, before any download

Preflight runs synchronously when the user starts an install, before
any package is downloaded. Each check maps to a plain-English refusal
when it fails, and no download starts after a refusal. The
preflight-refusal shape (button kept, retry offered) applies only to
retryable preflight failures: disk, network, cache, single-flight,
and a grade running. Non-retryable failures (permission-denied,
PEP 668, no wheel) swap the button for the guide block per
section 6.2, including when first learned at preflight under
changed conditions. Branch 3 (section 5.4) is not a preflight
outcome: it is decided at page render, before any click.

* Pip present on the target interpreter, checked by running
  `[interpreter, -m, pip, --version]` from the dashboard process —
  never assumed, because uv tool environments ship without pip.
  When pip is missing, preflight runs the fixed ensurepip bootstrap
  from section 5.3a, then re-checks. When the bootstrap itself fails
  (class `pip-bootstrap`) or pip is still missing after a successful
  bootstrap (class `pip-missing`), preflight refuses with the guide
  block: the button is swapped for the guide, because no install
  sequence can run without pip. The bootstrap is bundled and
  offline, so it needs no network check first, and it writes almost
  nothing, so it runs before the disk check.
* Free disk in the target environment's disk and in the temp
  directory. Thresholds (DECIDED, with the arithmetic shown, to be
  confirmed against a measured test install and amended if the
  measurement differs materially): default path 5 GB (about 2.5 GB
  torch + about 0.5 GB `laya` and transformers + 0.842 GB checkpoint
  + 1 GB staging, rounded up); CPU path 3 GB (about 0.2 GB CPU torch
  + about 0.5 GB `laya` and transformers + 0.842 GB checkpoint + 1 GB
  staging, rounded up). A failed disk check refuses; it never warns
  and proceeds. On a small VPS, a pip run that fills the disk can
  break the host itself, so this check is not optional.
* Free memory: below 3 GB free, the UI warns that grading will be
  slow or may fail, stated before the download — a warning, not a
  refusal, because memory at install time does not predict memory
  at grade time. (The 2–3 GB figure is runtime memory for torch
  plus the model, from `docs/code-orchestrator.md`.)
* Writable target environment (branch decision, section 5.4) and
  writable Hugging Face cache location (or `$HF_HOME` set), checked
  with a write test to a temp file that is then removed, not by
  assuming the directory exists.
* Network reachability, checked with a 10-second-timeout request
  from the dashboard process (not the browser) to: PyPI
  (`https://pypi.org/simple/laya/`); the PyTorch CPU index when the
  CPU path is chosen; the Hugging Face host when pre-warming is
  chosen. A failed reachability check refuses with "check the
  network or proxy", not with a pip traceback.
* No other install running (single-flight, section 6.4) and no grade
  running or draining that would conflict (section 6.4).
* Record writability: at least one job-record location from
  section 6.5 (the primary `~/.jev/` location, or the fallback
  state-directory location) is writable. Refusal here is shaped
  like branch 3: the page renders the guide block and no job
  starts.

Preflight results are shown as a short list ("Disk: enough", "Network:
reachable") so a refusal names the failing check, not just "preflight
failed".

### 6.2 Failure taxonomy

Every failure maps to a message, a retry offer, and what must change
before retry. The runner classifies by checking, in this order,
pip's stderr markers, then the exit code, then probe output — never
by showing pip's raw output as the message (the log tail stays
available for diagnosis). Marker order (first match wins):
"externally-managed-environment" → PEP 668; "No space left" with the
TMPDIR path in the message → temp directory full; "No space left"
otherwise → disk full; "Permission denied" → permission;
"Could not find a version" or "NameResolutionError" or "Connection"
→ network; "No matching distribution" → no wheel;
"DO NOT MATCH THE HASHES" → hash mismatch; exit code 137 or a
SIGKILL → killed (out of memory or host shutdown); a pip lock error
after a stopped install → "A previous install left a lock behind.
Clear the pip cache and retry" (retry: yes, after cache purge). Anything else
falls back to "pip failed (exit N); see the log", with N filled in.
The classifier and its marker fixtures are unit-tested (section 8);
a marker that matches nothing in tests must not ship.

| Condition | Message (plain English) | Retry offered? | What must change first |
|---|---|---|---|
| No network / DNS / proxy failure | "Could not reach the package index. Check the network or proxy settings." | Yes | Network or proxy fixed |
| Disk full during download or unpack | "Ran out of disk space. Free space and retry." | Yes | Disk freed |
| Temp directory full | "Ran out of space in the temp directory. Point TMPDIR at a disk with free space and retry." | Yes | Temp location or space |
| Permission denied on target | "The dashboard cannot write to its Python environment." plus the branch-3 command | No (button becomes guide) | Permissions, or run the command shown |
| PEP 668 refusal | "This Python refuses direct installs. Use a virtual environment." plus the branch-2/3 path | No (button becomes guide) | A venv, or run the command shown |
| Pip bootstrap fails (preflight) | "Could not set up pip in this Python environment." | No (button becomes guide) | Install pip into the environment, or run the command shown |
| Pip still missing after bootstrap (preflight) | "This Python environment has no pip, so the dashboard cannot install into it." | No (button becomes guide) | Install pip into the environment, or run the command shown |
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
  the browser precedent), elapsed time, and the latest pip output
  line. Pip runs without a terminal, so its progress bars do not
  render; the log lines are the only progress signal, and the UI
  must not claim a percentage pip never reported.
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

Rules (DECIDED):

* An install cannot start while a grade is running or draining, and
  a grade cannot start while an install is running. The blocked side
  is refused, not queued: the install page says "a grade is
  finishing; retry install when it ends", and the grade form says
  "an install is running; see its status". Refusal is chosen over
  queuing because queuing would add queue semantics (ordering,
  expiry, cancellation of queued work) that neither runner has.
* A draining grade blocks an install until the thread exits, because
  the thread still holds torch files.
* A grade submitted mid-install is refused with a message that links
  to the install status, and it must not ledger a spurious
  "unavailable" record. The ledger guard lives in the route layer,
  before `code_grade_runner.start` is called: the route checks the
  coordinator first, and a refused submission never reaches the
  runner, so nothing is recorded as a grade outcome at all.
* Repair and Uninstall are treated as installs for coordination:
  they cannot start while a grade is running or draining, and a
  grade cannot start while a repair or uninstall is running, with
  the same refused-not-queued behaviour. Verify (a probe
  subprocess, no mutation) is exempt from coordinator blocking,
  EXCEPT: while an install, repair, or uninstall is running,
  Verify is refused with a link to install status and ledgers
  nothing, because a probe against mutating files would fail
  spuriously.
* API (DECIDED): `DashboardCore` owns the coordinator with four
  methods — `try_begin_install()` and `try_begin_grade()`, each
  returning the blocking job's name or None, and `end_install()` /
  `end_grade()` releasing the claim. The methods are the only place
  the two runners' mutual exclusion is decided.
* The coordination lock is per dashboard process. Two dashboard
  processes on one machine (two repos, two ports) can still contend;
  that case is out of scope and the failure message for it is the
  pip failure taxonomy entry for the condition that surfaces (file
  lock, disk, or verification failure), not a new lock.

### 6.5 Durable job record and restart mid-install

The install job record is durable, not in-memory only. Location
(DECIDED): `~/.jev/cda-install-job.json` (mode 0700), the same
user-level directory that already holds `~/.jev/design-systems/` —
not a repo's `.jev/` directory, because the install is
machine-wide (section 6.9) and must be visible to every dashboard
process on the machine. Fallback (DECIDED 2026-10-30): when home is
not writable, the record lives instead in the state directory for
that repo — the `.jev` directory under `settings.root`, i.e.
`<repo>/.jev/cda-install-job.json` (file mode 0700, directory mode
0700). Under the fallback, the retained log lives at
`<repo>/.jev/cda-install.log` (mode 0600, same 256 KiB cap as the
primary log). A fallback record is visible only to dashboard
processes serving that repo, not to every dashboard process on
the machine; the machine-wide visibility of section 6.9 holds only
for the primary `~/.jev/` location. If no writable state directory
exists either, the page renders the branch-3 guide (section 5.4)
and no install job starts. Precedence: the primary location is
read first; the fallback is read only if no primary record exists;
a record in the non-chosen location never authorizes a second
concurrent job. Cross-repo concurrent-offer under the fallback is
an ACCEPTED limitation (it inherits section 6.4's existing
out-of-scope two-process contention), recorded explicitly;
same-repo dashboards share the fallback record, so a running state
there is visible and no second job starts. Schema (DECIDED):
`job_id` (uuid),
`argv` (the list from section 5.3a), `target` (branch 1/2/3 plus
the interpreter path, and the venv path when branch 2),
`torch_path` ("cpu" or "default"), `child_pid` (the id of the
currently-running child whatever the phase — pip, probe, or
warm-up — or null), `started_at`, `phase`, `state`, `exit_code`
when finished, `probe` (laya version, torch version and build,
constructible true/false, warmed true/false plus model id when
warmed, the latest install's verdict), and `probe_by_model` (a map
keyed by model id; each entry holds that model's warmed flag plus
its probe outcome). The retained log lives at
`~/.jev/cda-install.log` (section 6.10).

On dashboard start, the runner reads the record. A record that says
"running" becomes `interrupted` when its child pid is not alive
(checked with the platform's pid-existence test); when the pid is
alive, the child belongs to a dead dashboard, so it is terminated
and the record becomes `interrupted` anyway. The page then shows:
"The last install was interrupted by a restart. Retry to start
again." A second install after an interruption truncates the old
log only after copying it to the retained log, so the interruption
evidence is not destroyed by the retry.

If neither the home location nor a state-directory location is
writable, the page renders the branch-3 guide and no install job
exists to be interrupted, so the restart criterion of section 10
is vacuous there. Wherever a record location exists — primary or fallback —
the section 10 restart criterion applies: a restart mid-install
yields `interrupted` and a working retry.

### 6.6 Stop semantics and retry

Stop and retry are defined per phase, and the UI message after a stop
names the phase and the state:

* **Preflight:** stop cancels immediately; nothing was downloaded;
  retry starts from preflight.
* **Downloading and installing:** stop terminates the pip process
  (the browser-install precedent). Partial downloads stay in pip's
  cache where pip can reuse them; partial unpacks are left for the
  next pip run to overwrite. The state after stop is "stopped, safe
  to retry", where "safe" is defined positively by the environment
  state, not by the absence of a problem: the panel still says
  "not installed" (verification has not run, so no half-installed
  package is reported as installed), the job record says
  "stopped", and retry re-runs preflight then pip, which
  overwrites any partial unpack. Retry is defined and can never
  report a half-installed backend as installed; it does not promise
  the next run succeeds without a cache purge — if the next run
  surfaces a pip lock error, the section 6.2 remedy applies (clear
  the pip cache, then retry). If retry's probe still fails, the
  state becomes "installed but not constructible" with the repair
  path, never "installed".
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

* A new route set under `/code-grade/install` (DECIDED):
  `GET /code-grade/install` (status page),
  `GET /code-grade/install/live` (live fragment),
  `POST /code-grade/install/start`,
  `POST /code-grade/install/stop` (only when option set E provides
  a stop button),
  `POST /code-grade/install/verify` (re-run the probe, section 5.2),
  `POST /code-grade/install/repair` (section 5.5),
  `POST /code-grade/install/uninstall` (only under G2, section 5.5),
  and, only when option set H picks confirm-first,
  `GET /code-grade/install/confirm` (confirm page).
  POST routes go through `checked_form` as every other dashboard
  form does, including the uninstall POST under G2; GET routes are
  reads under the existing token gating
  and do not carry CSRF tokens. Single-flight applies to the
  uninstall POST as to every other install POST. The status page and fragment
  mirror `/code-grade/status` and `/code-grade/live`, showing
  phase, elapsed time, log tail, and the retained-log link.
* The Code Grade main page shows a banner while an install runs,
  finishes, or fails, linking to that status page — mirroring the
  existing grade-job flash on the panel.
* The status panel itself shows the install state in the Backend row
  area only as a short state word ("installing", "failed", "stopped",
  "interrupted"); detail lives on the install status page.

### 6.8 State matrix: Enabled × Backend × Install job

Under DECIDED A2, 'available' means warmed for the warmed model; the A1/A3 readings are superseded.

Rows the UI must render correctly (Backend here is the panel value
after section 5.1; Install job is none unless an install has run).
Under DECIDED A2, the A2 promise is per warmed model id, not
machine-wide: the panel compares the repo's configured model id
against the section 6.5 `probe_by_model` map. The A1/A3 readings
below are superseded; the done rows below are the decided ones.

| Enabled (gate) | Backend | Install job | What the panel shows |
|---|---|---|---|
| no | not installed | none | Install button (per option set C); grade/route forms per option set F |
| no | not installed | running | Banner "installing…", button disabled, forms per option set F |
| no | not installed | failed | "Install failed: <message>. Retry." plus log link |
| no | not installed | stopped | "Install stopped. Retry." |
| no | not installed | interrupted | "Last install was interrupted by a restart. Retry." |
| no | warmed | done | "Backend warmed." plus, when the backend is available and gate grading is off, the "Turn on gate grading" button (section 9, D3) |
| no | available (layer 2, model not warmed) | done | "Backend available." plus the explicit second line "available — checkpoint downloads on first grade", for that non-warmed model only |
| no | installed but not loadable | failed (verify) | "Installed but not loadable." plus repair action (section 5.5) |
| yes | warmed | done | "Backend warmed.", plus gate grading active |
| yes | available (layer 2, model not warmed) | done | As the "no / available (layer 2, model not warmed)" row, plus gate grading active |
| yes | not installed | none | Install button; gate records grades as unavailable, never guessed |
| either | available | running (reinstall/repair) | Banner; grading blocked per section 6.4 until it finishes |

Job-state vocabulary (DECIDED): the job states are `idle`,
`running`, `done`, `failed`, `stopped`, and `interrupted`.
`verifying` and `warming up` are phases within `running`, not
states, and the UI shows them as "installing — verifying" and
"installing — warming up". Earlier drafts used `cancelled` (grade
runner) and `draining` (grade runner) for other jobs; this job
never uses those words.
Second-click behavior: clicking Install while an install runs does
nothing except show the status page (the button is disabled and
links there). Double-submit from two tabs is stopped by the
single-flight guard, and the second submit sees the running job's
status, not an error.

### 6.9 Scope: machine, not repo

A dashboard process serves one repo (its `settings.root`), but the
install targets a Python environment shared by every dashboard
process on the machine. The button sits on that repo's page, whose
status is built from that repo's config (including its `model_id`).
The design resolves that mismatch as follows:

* The install job record and log live at `~/.jev/` (section 6.5),
  not under any repo's `.jev/` directory. The browser precedent's
  repo-scoped log is not reused, because a repo-scoped log would
  misattribute a machine-wide install to one repo and would be
  invisible to other dashboard processes. This bullet holds for
  the primary `~/.jev/` location; under the section 6.5 fallback,
  only dashboard processes serving that repo see the job state.
* The install status banner appears on the Code Grade page of the
  dashboard process that started the install, and any other
  dashboard process on the same machine shows the shared install
  state when its page loads, because both read the same job record
  and the same environment availability. This bullet holds for
  the primary `~/.jev/` location; under the section 6.5 fallback,
  only dashboard processes serving that repo see the job state.
* The `model_id` shown is the current repo's configured model. The
  install installs the backend runtime (`laya` + torch); the
  checkpoint for a repo's model downloads on that repo's first grade
  unless pre-warming is chosen, in which case pre-warming warms the
  current process's repo model only, and other repos' models still
  download on their first grade. The UI must say which model's
  checkpoint was warmed, by model id. DECIDED (2026-10-30, A2):
  the panel flips to "warmed" only for the warmed model id (section
  6.5 `probe_by_model` map, keyed by model id); another repo's
  dashboard flips to "available" (layers 1–2, shared environment)
  but NOT to "warmed" for its different model id. Panel logic must
  compare the repo's configured model id against the map.
* A finished install flips every dashboard process's panel as to
  package availability, because availability is a property of the
  shared environment, not of a repo. The install-state banner is
  separate: it is primary-location only — under the section 6.5
  fallback, only dashboard processes serving that repo show the
  install-state banner.

### 6.10 Observability

Beyond the log tail, the install records structured attempt events:
start, phase change, finish, failure with a failure class from
section 6.2 (not just an exit code), stop, and interruption. Each
event records: timestamp, session id (the dashboard has sessions,
not user accounts), argv, target environment, and outcome class.
The log tail is a view of the retained log, not a separate store.
The three artifacts are: the job record, the retained log, and the
attempt events — nothing else is stored.
The retained log is capped the same way the browser precedent caps
its log — 262,144 bytes (256 KiB) — when the job finishes, and is
linked from the status page for diagnosis; "retained log" in this
design always means that capped log, never an unbounded file. No
secrets appear in the log or the events: this is a new redaction
rule for this runner, not an existing practice — any
credential-looking token in a URL is redacted before storage.

### 6.11 Shutdown contract

The install runner is owned by `DashboardCore` and stopped from
`shutdown_jobs()`, in this position in the existing order
(scheduler, browser installer, reverse runner, grade runner): after
the grade runner, before the return. The grade runner's shutdown is
a join with a 30-second timeout, not a cancel and not a drain —
"draining" is a state entered only via `cancel()`, which shutdown
never calls — so shutdown does not guarantee the grade thread has
exited. The install shutdown therefore works as follows: join the
grade thread only through the grade runner's own shutdown call;
then terminate the currently-running child — pip, probe, or
warm-up subprocess — (if an install is running) and join the install
watcher thread up to the same 30-second timeout; mark the job
record `interrupted` (not `stopped`, because the user did not stop
it). If a grade thread is still alive after its join timeout,
shutdown proceeds anyway — process exit ends it — and the job
record still says `interrupted`, so the next start shows the
interrupted message from section 6.5 rather than pretending the
install finished cleanly.

## 7. Security: the fixed command, and who may press it

### 7.1 The fixed argv rule, and how it is enforced

The install command is a constant argv sequence built on the server
from the environment decision in section 5.4. It is never built from
request data. Enforcement, in code review terms:

* The route handler takes no package, flag, version, or index-URL
  field from the request. The form posts only the CSRF token and,
  when a confirm step exists, the confirmation.
* The argv builder takes only server-side inputs: `sys.executable`,
  the torch path decision (CPU vs default, from the rule in section
  5.3), and the `laya` floor version from section 5.3a. Its output
  is the sequences in section 5.3a, logged (argv list, not a shell
  string) in the job record.
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
* Single-flight is the only concurrency bound; it is not rate
  limiting, and this design claims no rate limit. A second submit
  sees the running job (section 6.8), which bounds abuse to one pip
  process.
* Logging: every install attempt (start, stop, finish, failure
  class) is logged in the attempt events (section 6.10) with
  timestamp, session id, argv, target, and outcome class. No new
  identity system is introduced: the dashboard has sessions, not
  user accounts.
* No new privilege: installing does not grant anything a token
  holder or local visitor does not already have — token holders can
  already start runs that execute code through the run pipeline, and
  local visitors can already reach a loopback dashboard. The install
  button is therefore not a new trust boundary; it is the same
  boundary, applied to a host mutation instead of a run. The one
  new warning this design adds: a dashboard exposed beyond loopback
  (for example through a tunnel) without a token inherits loopback
  trust on a network-reachable address, so the deploy docs must say
  never to expose a token-less dashboard publicly.
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
  shared-scaffold file when option set B picks B1): class
  `CdaInstallRunner`, with methods `start`, `stop`, `status`,
  `shutdown`, and `busy`, and a status value carrying state, phase,
  message, started and finished times, exit code, and probe outcome.
  It owns: argv building (section 5.3a), preflight (section 6.1),
  phased execution (section 6.3), the subprocess verification probe
  (section 5.2), the durable job record (section 6.5), stop and
  shutdown (sections 6.6, 6.11), failure classification (section
  6.2), and attempt events (section 6.10). Rationale: this is the
  only new stateful component; the rest of the plan connects routes, templates, and the coordinator to it
  around it.
* `src/jev_agent/dashboard/core.py` (modify): own the new runner as
  `DashboardCore.cda_install_runner`, wire it into `shutdown_jobs()`
  in the position stated in section 6.11 (after the grade runner),
  own the coordinator methods from section 6.4
  (`try_begin_install`, `try_begin_grade`, `end_install`,
  `end_grade`), and read the durable job record on start (section
  6.5). Rationale: coordination must live above both runners, and
  shutdown order is a `DashboardCore` decision.
* `src/jev_agent/dashboard/routes_code.py` (modify) and
  `src/jev_agent/dashboard/app.py` (modify): register the route set
  from section 6.7 in the app's route table and implement the
  handlers in the code-grade routes — start, status, live fragment,
  verify, repair, uninstall, and (only when their option sets pick
  them) stop and confirm. The Verify route is baseline (§5.2); the
  Repair and Uninstall routes exist under G2; the stop route exists
  only under E1 and the confirm route only under H1. Repair and
  Uninstall are treated as installs for coordination (section 6.4):
  they cannot start while a grade is running or draining, and a
  grade cannot start while a repair or uninstall is running, with
  the same refused-not-queued behaviour; Verify is exempt except
  while an install, repair, or uninstall is running, when it is
  refused with a link to install status and ledgers nothing. The
  grade/route handlers check the coordinator
  before calling `code_grade_runner.start` (section 6.4). Rationale:
  all install endpoints live with the code-grade routes, with POSTs
  under `checked_form` protection and GETs as reads.
* `src/jev_agent/dashboard/templates/code_grade.html` (modify): the
  Install button, the install banner, the Backend row's install
  states, the Verify action (section 5.2), the "Turn on gate
  grading" button (D3, below), and the replacement of
  the pip instruction with a link to the install action. Rationale:
  this is the surface the issue names.
* "Turn on gate grading" button (D3, decided 2026-10-30): the
  button lives on the Code Grade panel (`code_grade.html`) only,
  and is shown whenever the backend is available AND gate grading
  is off — not only transiently after a successful install. It
  flips the setting through the existing settings route, not
  through the install job: `POST /settings/set`, method POST,
  through `checked_form` under the existing token gating, with
  form fields `path="code_orchestrator.enabled"` carrying the
  bool-true value per the settings editor's convention. The flip
  must return to `/code-grade`; the implementer may add a `next`
  field honored solely as the post-write redirect target
  (validated same-site path only) without changing behavior for
  forms that do not send it. The install status page's success
  state links to the panel, where the button lives. If the
  return-to cannot be done cleanly, the degradation trigger fires
  and D3 ships as D1-with-link (the panel states the Settings
  path; the link flips nothing). Rationale: one page, two explicit
  steps; the install job never becomes a config writer.
* `src/jev_agent/dashboard/templates/code_grade_status.html`
  (modify): the install banner on the grade status page too, so a
  user watching a grade sees a running install. Rationale: the
  grade status page is the second surface that reports job state.
* `src/jev_agent/dashboard/templates/code_grade_install_status.html`
  (new) and `src/jev_agent/dashboard/templates/_code_grade_install_live.html`
  (new): the install status page and its polling fragment,
  mirroring `code_grade_status.html` and `_code_grade_live.html`.
  Rationale: progress needs its own home (section 6.7).
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
* `src/jev_agent/code_orchestrator/backend.py` (no change): the
  probe constructs `LayaBackend`/`CodeOrchestrator` without loading,
  which the existing lazy construction already allows, so no new
  entry point is needed. Criterion for revisiting this: if the
  probe cannot construct without loading, the probe contract in
  section 5.2 is wrong and this design is amended before code is
  written. Rationale: verification must not import torch into the
  dashboard process, and must not run a grade.
* `src/jev_agent/dashboard/browser_install.py` (modify only when
  option set B picks B1): extract the shared job scaffold (lock,
  thread, state, timestamps, `busy()`, `status()`, shutdown hook,
  log cap) that the browser, grade, reverse, and install runners
  share. The reverse runner (`design_reverse.py`) is in scope for
  the extraction assessment: its executor seam is kept, and only
  the common skeleton is shared. When option set B picks B2 or B3,
  this file is untouched. Rationale: option set B, not a drive-by
  refactor.
* `src/jev_agent/dashboard/design_reverse.py` (modify only when
  option set B picks B1): adopt the shared scaffold if the
  assessment above finds its skeleton fits; otherwise untouched,
  with the reason recorded. Rationale: a scaffold that silently
  excludes the third runner is not shared.
* Grade-time temp and cache (modify `docs/code-orchestrator.md`,
  plus a startup check in `core.py`): the install sets TMPDIR and
  `$HF_HOME` for its own subprocesses (sections 5.3, 5.4), but a
  grade runs in the dashboard process thread, so grade-time TMPDIR
  and cache location are start-time concerns, not install-time
  ones. The implementation adds a dashboard startup check that
  records temp-directory free space in diagnostics and warns when
  it is below the section 6.1 staging need, and documents that a
  dashboard started with a small tmpfs still fails first grades
  until restarted with a real-disk TMPDIR. Rationale: without this
  item, the install fixes verification and leaves the first grade
  broken for the same reason.
* `tests/unit/test_cda_install_runner.py` (new): argv tests
  exercising `CdaInstallRunner`'s own argv builder (including
  "jev-agent never in argv" on every argv branch —
  default, CPU, repair, venv, uninstall — where "branch" means the
  argv branches of section 5.3a), preflight refusal tests, failure
  classification tests against the marker fixtures of section 6.2,
  stop-per-phase tests (only when option set E provides a stop
  button), restart/interrupted tests, and the coordination tests
  (install blocks grade, grade blocks install, refused grade
  ledgers nothing). Test seams: injectable process spawn (as
  `BrowserInstallRunner`'s injected doubles expose `wait()`), an
  availability seam (as `code_grade_runner._orchestrator` is
  monkeypatched in tests), and a stubbed environment decision, so
  no test downloads torch or a checkpoint.
* `tests/unit/test_cda_install_routes.py` (new): route tests for
  the install route set (section 6.7), CSRF refusal on POST routes,
  single-flight double-submit, the Verify, Repair, and Uninstall
  routes (CSRF refusal plus single-flight for Uninstall,
  alongside Verify and Repair), and
  the three retired pip-instruction surfaces (status panel,
  failed-grade message, Ratings empty state — with the guide,
  repair, and rollback commands explicitly exempt, per section 10).
* `tests/unit/test_cda_install_matrix.py` (new): the platform ×
  deployment decision-tree tests from section 10, with the
  environment detection stubbed per cell.
* `docs/code-orchestrator.md` (modify): the install button, the
  fixed sequence per platform, the torch sizes, the Docker
  persistence answer, and the manual recovery commands from
  section 10. Rationale: the doc is the surface users read when the
  UI message is not enough.
* `docs/deploy.md` (modify): the Docker persistence answer from
  option set I (I2: documented rebuild plus reinstall, no deploy
  change), and the uv-tool reinstall caveat from section 5.4.
  Rationale: deploy docs must not promise persistence the install
  does not have.
* `docs/dashboard.md` (modify): the Code Grade page's install
  states, if that doc describes the page. Rationale: dashboard docs
  must match the shipped UI.
* `docs/designs/cda-install-button/design.md` (this file, already
  written): updated 2026-10-30 with the decided option-set picks
  (section 9, DECIDED subsection), so the implementer works from a
  decided spec, not from open options.

Out of the implementation phase, explicitly: no change to
`.github/workflows/`, no change to lint/type/test config, no change
to grading semantics, no change to the threshold, no change to the
model.

## 9. Option sets: the human-approval gate

Each set below has at most three options. None is ranked, none is
marked as preferred, and none is pre-picked. Each option states its
mechanism, its trade-offs, and the conditions under which it fails.
Before 2026-10-30, the human was to pick one option per set before implementation. All nine sets are now DECIDED below; the option texts are retained for audit.

### DECIDED (2026-10-30): the picks for all nine sets

The picks below were decided 2026-10-30 by the implementation
coordinator under the user's never-stop directive (work must never
stop at approval gates). They are decided, not proposed: the
implementation proceeds from this spec. Any pick the user later
overrules gets reworked; that is expected. The option texts below
are unchanged; this subsection records which option was picked and
why.

* **A — A2 (pip + probe + warmed checkpoint).** The panel promise
  is complete: "installed" means fully ready, so there is no
  first-grade download surprise. The upfront checkpoint cost
  (~842 MB plus time) is disclosed on the H1 confirm page before
  anything is spent. Warming is per model id (§5.1, §6.9).
* **B — B2 (deliberate third copy).** The install runner's
  machine-wide durable record, probe, and preflight fit no existing
  runner without rework, and a separate runner risks no regression
  to the working browser, grade, and reverse runners. The future
  cost — lifecycle fixes ported across runners by hand — is
  accepted and recorded.
* **C — C1 (button everywhere, per-cell fallback).** This maximizes
  browser-first. The false-promise risk is contained by the H1
  confirm page and the branch-3 guide exception (section 5.4):
  branch 3 keeps guide replacement and no job starts.
* **D — D3 (enable button on the status page).** One page, two
  explicit steps; the install job never becomes a config writer.
  If settings-route reuse proves impossible during implementation,
  it degrades to D1 with a link.
* **E — E1 (terminate, like the browser install).** The user keeps
  in-browser control during a multi-GB install, matching the only
  existing install precedent. Partial state is handled by the
  defined retry, which never reports a half-installed backend as
  installed.
* **F — F2 (forms live, failures link to install).** This preserves
  current form behaviour. The §6.4 refused-job ledger guard already
  fixes spurious "unavailable" records for grades refused because
  an install is running. Accepted limitation: a grade submitted
  while the backend is missing still fails and still ledgers
  "unavailable" as today; the remedy if that becomes a complaint
  is F1, not a patch to F2.
* **G — G2 (minimal lifecycle: version display + repair +
  laya-only uninstall).** This is the only option under which every
  broken state except a broken torch is recoverable from the
  browser. Uninstall removes laya only, never torch, never
  jev-agent. Version floor laya>=0.4.15 is DECIDED and unchanged;
  a pin replaces it only if testing finds a breaking laya version,
  in which case this design is amended before implementation proceeds on that version.
* **H — H1 (confirm first, naming the exact command).** The
  multi-GB download and the Docker ephemerality warning are read
  before disk or time is spent, matching the browser-install
  confirm precedent. "One click" becomes two clicks: Install, then confirm.
* **I — I2 (documented rebuild plus reinstall).** The Docker button
  stays honest without changing the deploy contract or risking a
  stale volume breaking a new image. A container recreate costs a
  re-offered reinstall, still zero-terminal, with the persistence
  warning shown before the click.

### Re-audit resolutions (2026-10-30)

DECIDED 2026-10-30: a three-lens re-audit (consistency,
implementability, security) over the amendment diff found conflicts
between the decided picks and the draft text. The coordinator
resolved every one below; this subsection records the resolutions,
and the in-place fixes each names are applied in the sections
cited. Plain English, decided voice.

* **R1 (A2 panel value).** The section 6.8 done row under A2 reads
  Backend "warmed", panel "Backend warmed.", with no "downloads on
  first grade" alternative for the warmed repo. For another repo
  whose model is not warmed, the panel shows "available —
  checkpoint downloads on first grade" as an explicit second line
  for that non-warmed model only. The A2 promise is re-scoped to
  per warmed model id, not machine-wide. Record shape (section
  6.5): the schema gains a `probe_by_model` map keyed by model id
  (each entry: warmed flag plus probe outcome); the existing
  `probe` field remains the latest install's verdict. The panel
  compares the repo's configured model id against the map. Verify
  refreshes the map entry for its model (step-3 re-run when a
  warm-up record exists for that model). Section 10's A2 value
  "warmed" stands.
* **R2 (D3 enable button).** The button routes through
  `POST /settings/set`, method POST, through `checked_form` under
  the existing token gating, with form fields
  `path="code_orchestrator.enabled"` carrying the bool-true value
  per the settings editor's convention. Button surface: the Code
  Grade panel (`code_grade.html`) only. Persistence: shown
  whenever the backend is available AND gate grading is off, not
  transiently. The install status page's success state links to
  the panel. Return-to: the flip must return to `/code-grade`;
  the implementer may add a `next` field honored solely as the
  post-write redirect target (validated same-site path only)
  without changing behavior for forms that do not send it. If that
  cannot be done cleanly, the degradation trigger fires and D3
  ships as D1-with-link (the panel states the Settings path; the
  link flips nothing). The button is added to the section 8
  `code_grade.html` bullet and to the section 6.8 done row.
* **R3 (fallback record).** Fallback record file mode 0700,
  directory mode 0700; fallback retained log
  `<repo>/.jev/cda-install.log`, mode 0600, same 256 KiB cap. The
  state directory is named literally: `.jev` under
  `settings.root`. Section 6.9 is qualified: its bullets hold for
  the primary `~/.jev/` location; under the section 6.5 fallback,
  only dashboard processes serving that repo see the job state;
  package availability still flips everywhere (shared
  environment), but the install-state banner is primary-location
  only. Section 6.9's final bullet is split into package
  availability (flips everywhere) versus install-state banner
  (primary only). Visibility wording is "dashboard processes
  serving that repo". Precedence: the primary location is read
  first; the fallback is read only if no primary record exists; a
  record in the non-chosen location never authorizes a second
  concurrent job. Cross-repo concurrent-offer under the fallback
  is an ACCEPTED limitation (it inherits section 6.4's existing
  out-of-scope two-process contention); same-repo dashboards share
  the fallback record, so a running state there is visible and no
  second job starts.
* **R4 (branch-3 vs preflight boundary).** Branch 3 is a pre-click
  environment decision rendered at page render from the section
  5.4 branch decision, distinct from section 6.1 preflight checks.
  C1 preflight-refusal (button kept, retry offered) applies only
  to retryable preflight failures (disk, network, cache,
  single-flight, grade running). Non-retryable failures
  (permission-denied, PEP 668, no wheel) swap the button for the
  guide block per section 6.2, including when first learned at
  preflight (changed conditions). Section 6.5's "preflight refuses
  (branch 3)" is reworded to "renders the branch-3 guide".
  Record-writability is an explicit section 6.1 preflight check
  whose refusal is shaped like branch 3 (guide, no job), with a
  matching section 10 exception footnote (the restart criterion is
  vacuous where no record location exists because no job exists to
  be interrupted).
* **R5 (broken-torch rollback).** Section 10's broken-torch
  rollback commands are literal. CPU path:
  `<python> -m pip uninstall -y torch`, then the section 5.3a CPU
  torch argv, then the section 5.3a laya argv. Default path:
  `<python> -m pip uninstall -y torch`, then
  `<python> -m pip install torch` (standalone torch argv pulling
  the default wheel), then the section 5.3a laya argv.
  Uninstall-first IS intended (a plain reinstall no-ops on
  satisfied-but-broken torch). This is manual recovery
  documentation, not a runner feature. Implementation propagates
  the commands to `docs/code-orchestrator.md` per the section 8
  doc item.
* **R6 (warm-up shutdown/stop).** The section 5.2 sentence is
  scoped to timeout exits: step 3 has no wall-clock timeout; its
  exits are user stop per section 6.6 and dashboard shutdown per
  section 6.11. Section 6.11 terminates the probe/warm-up
  subprocess as well as pip, then joins the watcher thread up to
  the 30-second timeout and marks the job interrupted. Section 6.5
  states that `child_pid` holds the currently-running child
  whatever the phase (pip, probe, or warm-up).
* **R7 (Verify timeouts).** Section 5.2's Verify paragraph gains
  one sentence: "Verify uses the same timeouts as the install
  probe: 5 minutes for steps 1–2, none for a step-3 re-run."
* **R8 (G2 uninstall route).** The section 6.7 DECIDED route set
  gains `POST /code-grade/install/uninstall` (only under G2),
  POST through `checked_form` under the existing token gating like
  every other install POST, with single-flight applying. It is
  added to the section 8 G2 route sentence and to the
  `test_cda_install_routes.py` list (CSRF refusal plus
  single-flight for uninstall, alongside Verify and Repair). No
  change to the uninstall argv.
* **R9 (repair/uninstall coordinator).** Repair and Uninstall are
  treated as installs for coordination (section 6.4 and the
  section 8 G2 route sentence): they cannot start while a grade is
  running or draining, and a grade cannot start while a repair or
  uninstall is running, with the same refused-not-queued
  behaviour. Verify (probe subprocess, no mutation) is exempt from
  coordinator blocking, EXCEPT: while an install, repair, or
  uninstall is running, Verify is refused with a link to install
  status and ledgers nothing (a probe against mutating files
  would fail spuriously).
* **R10 (G2 pin wording).** The section 5.3a clause is appended to
  the section 9 G2 bullet: "in which case this design is amended
  before implementation proceeds on that version."
* **R11 (argv tests).** The section 8 test bullet states that argv
  tests exercise `CdaInstallRunner`'s own argv builder (default,
  CPU, repair, venv, uninstall branches).
* **R12 (section 8 Verify/Repair wording).** The section 8 wording
  is: "The Verify route is baseline (§5.2); the Repair and
  Uninstall routes exist under G2; the stop route exists only
  under E1 and the confirm route only under H1."
* **R13 (section 9 intro reframe).** The section 9 intro is
  reframed in history: "Before 2026-10-30, the human was to pick
  one option per set before implementation. All nine sets are now
  DECIDED below; the option texts are retained for audit." Option
  texts are untouched.
* **R14 (residual conditional wording).** A DECIDED resolution line
  stands at the top of section 3 ("DECIDED 2026-10-30: C1, A2, D3;
  the C2/C3, A1/A3, D1/D2 wordings below are retained
  alternatives, not live options") and at section 6.8 ("Under
  DECIDED A2, 'available' means warmed for the warmed model; the
  A1/A3 readings are superseded."), plus the section 6.8 done-row
  rewrite per R1 and the D3 button per R2.
* **R15 (section 1 double-count).** Section 1's trailing clause
  ("plus a confirm click when option set H picks confirm-first")
  is deleted, leaving "One click plus confirm installs...".
* **R16 (click-count harmonization).** The section 9 DECIDED H
  wording uses "two clicks: Install, then confirm"; section 1's
  "one click plus confirm" is the same count and stands.

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
  issue's "same button everywhere" is kept as written, and every user
  gets at least a correct command; but a button that refuses on
  some machines teaches users that the button may not work, and the
  Docker warning must be read to be useful. Fails when: users click
  through the warning and then report the button as broken.

  DECIDED (2026-10-30, C1 exception): branch 3 keeps guide
  replacement (section 5.4) — the button is replaced by the guide
  and no job starts. C1's preflight-refusal applies only where a
  job can start or a failure is learned at preflight.
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
  every broken state except a broken torch from the browser; but uninstall must define
  what happens to the checkpoint cache (kept, so reinstall is
  faster, and stated) and to other tools sharing torch. DECIDED
  rule for G2: uninstall removes `laya` only, never torch, so it
  cannot break another tool's torch. Fails when: the broken piece
  is torch itself, in which case uninstall of `laya` alone does not
  fix it and the panel says so, pointing at the repair sequence
  instead.
* **G3. Version display without lifecycle UI.** The panel shows
  the installed version (from the probe), but there is no update or
  uninstall button. Mechanism: probe reports version. Trade-off:
  staleness becomes visible without building update flows; but
  visibility without action frustrates the user who sees an old
  version and cannot update it from the page. Fails when: the
  installed version lags behind a `laya` fix the backend needs,
  and the only remedy is a terminal command — the same gap this
  design exists to close, in miniature.

  The `laya` version policy itself is DECIDED in section 5.3a and
  does not vary by G option: the floor `laya>=0.4.15` from
  `pyproject.toml` is kept, and a pin replaces it only if testing
  finds a breaking `laya` version, in which case this design is
  amended before implementation proceeds on that version.

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
  is read word for word.
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

### I. Docker persistence

* **I1. Named volume for the install.** The Docker run gains a
  named volume mounted over the installed packages and the Hugging
  Face cache, so a container recreate keeps the backend.
  Mechanism: volume mount plus a documented `docker run` change.
  Trade-off: installs survive recreate, which is the only option
  that makes the button honest on Docker; but it changes the
  deploy contract (volumes, upgrades, image rebuilds) and the
  volume's contents must be versioned or validated on start, or a
  stale volume can break a new image. Fails when: the volume
  outlives the image it was built for and the probe must catch the
  mismatch.
* **I2. Documented rebuild plus reinstall.** No volume. The deploy
  docs state that a container recreate wipes a container-side
  install, and the UI on Docker says so before the click; after a
  recreate, the button is offered again. Mechanism: docs plus UI
  copy, no deploy change. Trade-off: no deploy-contract change and
  no stale-volume risk; but every recreate costs another
  multi-GB install, and the user must remember to reinstall.
  Fails when: the user does not notice the backend is gone after
  a recreate and wonders why grading stopped.
* **I3. Ephemeral, stated, no reinstall promise.** As I2, but the
  UI goes further: on Docker, the button is replaced by the guide
  (option set C2's shape for this deployment only), because an
  install that vanishes on recreate is not a feature. Mechanism:
  deployment detection switches button to guide. Trade-off: no
  false promise anywhere; but Docker users never get a button,
  even when they would accept reinstalling after recreate.
  Fails when: detection misclassifies a persistent Docker setup
  (a volume the detector does not know about) as ephemeral.

## 10. Acceptance criteria, test matrix, and rollback

### Acceptance criteria (measurable)

* From a fresh "not installed" state on the Mac live dashboard, one
  click (plus a confirm click when option set H picks H1) installs
  the backend with no terminal use, and the status panel reports
  the state option set A defines (A1: constructible; A2: warmed;
  A3: two lines). The criterion is checked per the picked option,
  not against all three at once.
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
  Exception: where no record location exists (neither the primary
  nor a fallback location is writable), the restart criterion is
  vacuous, because no job exists to be interrupted.
* The three pip-instruction surfaces show no pip command after the
  change: the status panel, the failed-grade message, and the
  Ratings empty state. Exempt, by name, so the criterion is not
  read as an absolute: the branch-3 guide command, the repair
  message's uninstall command, and the rollback commands of section
  10. A checker that flags those exempt commands as failures is
  misreading this criterion.

### Test matrix (platform × failure)

Platforms: Mac venv or editable checkout, Mac uv tool environment,
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
  (with `<python>` meaning the detected environment's interpreter
  — the dashboard's own, or the branch-2 venv's):
  - Remove a broken backend: `<python> -m pip uninstall -y laya`.
  - Recover from a broken torch: uninstall torch first, then
    reinstall torch, then reinstall `laya` — uninstall-first is
    intended, because a plain reinstall no-ops on satisfied-but-
    broken torch. CPU path:
    `<python> -m pip uninstall -y torch`, then the section 5.3a
    CPU torch argv
    (`<python> -m pip install torch --index-url
    https://download.pytorch.org/whl/cpu`), then the section 5.3a
    laya argv (`<python> -m pip install "laya>=0.4.15"`).
    Default path:
    `<python> -m pip uninstall -y torch`, then the standalone
    torch argv pulling the default wheel
    (`<python> -m pip install torch`), then the section 5.3a
    laya argv (`<python> -m pip install "laya>=0.4.15"`). This is
    manual recovery documentation, not a runner feature: no new
    install path is invented here. The implementation copies these
    commands verbatim into `docs/code-orchestrator.md` per the
    section 8 doc item.
  - Clear a corrupt pip cache: `<python> -m pip cache purge`.
  - Clear a corrupt checkpoint cache: remove the model directory
    under the Hugging Face cache (path shown from the detected
    `$HF_HOME` or the default `~/.cache/huggingface/hub`), so the
    next grade re-downloads it.
  - Recover from a filled disk: free space, then retry; the button
    re-runs preflight first.
  - Recover from a Docker wipe: re-run the install after recreate,
    or add the volume from option set I and reinstall once.

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
* **Bundle the model with Baton.** Rejected: about ~842 MB plus torch
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
   refused, the Docker row collapses to branch 3 and option set I
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
5. **GPU detection call (rule decided in resolution; proof still
   owed).** Section 5.3 now states the rule: macOS → CPU always;
   Linux/Windows → default wheel only when `nvidia-smi` exits 0,
   else CPU. What is still unproven is the rule's behavior on real
   machines: a driver-present machine without `nvidia-smi` on PATH
   falls back to CPU (cost: size only, since the default wheel
   runs on CPU too), and a machine where `nvidia-smi` exists but
   CUDA is unusable gets the default wheel anyway. The
   implementation phase must prove the rule on the Mac (CPU path)
   and record the probe's torch-build output as the check.
6. **Dedicated-venv grade path (path decided in resolution; work
   still owed).** Section 5.4 now fixes the path
   (`~/.jev/cda-venv`) and section 8 lists the grade-runner
   interpreter override as required work for branch 2. If that
   override cannot be added cleanly, branch 2 is unavailable and
   the design falls to branch 3 — that fallback is decided, but
   the override work itself is still to be done.
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
