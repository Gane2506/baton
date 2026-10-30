# Zoom Out — CDA install button (BRAINSTORM)

Lens: zoom out. Sources judged: `concepts_brief.md` (issue Gane2506/baton#366)
and the listed code only — `code_grade.html`, `routes_code.py`,
`browser_install.py`, `code_grade_runner.py`, `code_orchestrator/backend.py`,
`docs/code-orchestrator.md` — plus the deploy story those files sit inside
(`docs/deploy.md`, `Dockerfile`, `install.sh`, `pyproject.toml`).

## Summary

The brief treats the install as a page feature: a button, a background job,
a status flip. Stepping back, it is a deployment and operations act —
installing a multi-gigabyte ML runtime into a running process — and the
brief's central premise, "the same button on Mac, Windows, Linux, and VPS",
does not survive contact with how Baton is actually deployed. Baton has
four different deploy shapes (native pipx, editable checkout, service-managed
dashboard, pinned Docker image), and on at least two of them a runtime pip
install is either wiped by the next upgrade, refused by the platform, or
installed into a place the grading process never sees. The brief names
Docker persistence and environment detection as open risks; this report
argues they are not open details to be settled in the draft. They are the
concept itself, and they point the other way: install-time provisioning
and runtime discovery are the established patterns in this codebase, and a
dashboard button is the exception, not the rule.

A second, separate problem: the button only fixes one surface. It installs
into one dashboard process on one host. It does not install for the
pipeline's quality gate on another host, for a second dashboard, or for a
fleet — and it has no lifecycle after install (no update, no uninstall, no
version shown). So the terminal returns at the first upgrade, the first
broken torch, or the first version drift, and the browser-first claim ends
at first install rather than holding for the life of the backend.

---

## Findings

### 1. The "same button everywhere" premise contradicts the deploy model

- **Severity:** Blocker
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** The deploy documentation already draws the line this
  concept crosses. The Docker image is dashboard-only: worker CLIs and
  Node/`npx` are not in it, and the docs state plainly that the
  browser-install button cannot work in the container. The image runs as
  non-root user `jev`, with Baton copied into `/usr/local` from the build
  stage. A pip install into that system environment will fail on
  permissions; a `--user` install lands in a home directory that no volume
  persists and that the next `docker pull` / container recreate discards.
  The Docker upgrade path is "pull a new image", not "mutate the running
  container" — a button that mutates the container fights the image model
  instead of using it. The native path has the same wipe problem in a
  quieter form: `install.sh` prefers pipx and upgrades with
  `pipx upgrade`, which recreates the isolated venv. An extra pip-installed
  into that venv by a dashboard button is not part of the pipx spec, and
  there is nothing in the brief or the installer guaranteeing it survives
  the next upgrade. "Works on a VPS" therefore cannot mean one endpoint
  behaving the same everywhere; on Docker it means either a new image
  variant, a persisted site-packages volume, or an honest
  "not supported here" — each of which is a deploy decision, not a page
  feature.
- **Alternative / Reuse Opportunity:** Decide the deploy answer first, then
  derive the button's scope from it. The existing deploy levers are
  `install.sh` (native), a Docker image variant or build argument, and
  `pipx inject`-style extras at install time. A button that is explicitly
  native-only, with Docker directing the user to the image path, is a
  smaller and more honest scope than a universal button with a Docker
  footnote.

### 2. "The dashboard's own environment" is not one thing

- **Severity:** Blocker
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** The brief's sane default, `sys.executable -m pip`,
  assumes the dashboard process's Python is writable, is the grading
  Python, and stays the same across restarts. None of those is guaranteed
  across the real install shapes: a pipx isolated venv; an editable
  checkout on the Mac (where the code tracks main, so the environment is a
  moving target); a system Python on Debian/Ubuntu, where PEP 668
  (externally-managed-environment) makes pip refuse a system install
  without `--break-system-packages` or a venv; a service-managed dashboard
  (launchd agent, systemd user unit, Windows Scheduled Task) whose
  recorded binary path and minimal service environment differ from the
  interactive shell the user tested in; and the Docker system Python
  above. The brief lists `--user` and a dedicated venv as fallbacks, but
  each fallback changes what "installed" means and where the next process
  looks: a `--user` install is per-user, a dedicated venv needs its own
  activation and discovery path, and none of them is visible to a
  dashboard started from a different user, shell, or service definition.
  These are not edge cases. They are the default paths on the platforms
  the brief claims to cover.
- **Alternative / Reuse Opportunity:** Reuse the detection Baton already
  does at startup instead of inventing install-time detection. The
  dashboard already resolves worker CLIs across Homebrew, nvm, and
  Windows Node locations without a login shell, and the service installer
  already records the absolute `jev` path for exactly this reason. If the
  concept proceeds, its environment story should start from that existing
  resolution (what binary, what user, what HOME, service-managed or not)
  and fail closed with the detected facts, rather than adding a second,
  install-only notion of "the environment".

### 3. The footprint is not ~842 MB, and one fixed command cannot be right everywhere

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** The brief correctly splits "pip done" from
  "checkpoint pre-warmed", but the pip step itself is already
  platform-dependent in a way a single fixed command hides.
  `docs/code-orchestrator.md` documents that `pip install laya` pulls the
  default torch wheel with CUDA libraries (~2.5 GB) and that CPU-only
  machines should install the CPU wheel first from the PyTorch index,
  then `laya`. A naive fixed command on a CPU-only VPS therefore downloads
  roughly three times the advertised size, takes far longer, and can fill
  a small disk — while a CPU-wheel-first command is a different command
  with its own index URL and ordering. Add the checkpoint (~842 MB), the
  Hugging Face cache location (`~/.cache/huggingface/hub` or `$HF_HOME`),
  and the TMPDIR trap the docs call out (the default /tmp is a 512M tmpfs
  on the dev VM, smaller than the model, so the first load fails with
  "No space left on device"), and the true cost is several gigabytes of
  disk, 2–3 GB of RAM at grade time, and three separate network fetches
  (PyPI, the PyTorch index, Hugging Face). A button that reports progress
  on pip alone understates all of this; a button that handles all of it
  is no longer a small page feature but a small package manager with
  platform-specific torch selection, disk-space checks, and cache/TMPDIR
  management.
- **Alternative / Reuse Opportunity:** Keep the documented CPU-wheel-first
  sequence as the install story and surface it at deploy time, where the
  platform is already known (the installer knows it is on macOS, Windows,
  or Linux; the Docker build knows it is CPU-only). If a runtime button
  is kept, its "fixed command" needs to be a fixed *plan* per platform,
  and the draft should say so explicitly instead of implying one command
  covers all four platforms.

### 4. Verification through `CodeOrchestrator.available` proves pip, not grading

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** In `backend.py`, `available` is true when a backend
  is already loaded or loadable now — in practice, when `find_spec("laya")`
  succeeds. It does not import torch, does not load the checkpoint, does
  not check disk space, TMPDIR, or the Hugging Face cache, and does not
  run a grade. The brief's verification step ("the status panel flips to
  installed") therefore flips on the weakest possible signal, and the
  failure the brief warns about — a user facing a multi-minute first
  grade with no warning — survives verification untouched. Worse, a pip
  install that succeeds but leaves a broken torch (wrong wheel, missing
  system libraries, architecture mismatch) also flips the panel to
  installed, and the first real grade then fails as an inference error,
  which the error taxonomy treats as fail-closed at the gate — a
  different, more confusing failure than "not installed". Verification
  that does not attempt a load (or at least a torch import plus a cache
  and TMPDIR check) is a status label, not a verification.
- **Alternative / Reuse Opportunity:** Reuse the existing cheap/expensive
  split in `backend.py`: `find_spec` stays the cheap availability check,
  and verification should be the expensive check the code already
  avoids at startup — a real backend load, or an explicit statement that
  the panel means "packages present, first grade still downloads the
  checkpoint". Do not let one boolean carry both meanings.

### 5. A remote install endpoint expands the trust boundary the codebase deliberately keeps local

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** `browser_install.py` is explicit: "Local installs
  only — the `--server` variant stays in the terminal." The CDA brief
  proposes the same fixed-command pattern but on a VPS dashboard, which
  is always in the token case, speaks plain HTTP, and is meant to be
  reached over an SSH tunnel or a TLS-terminating proxy. A pip install is
  code execution from PyPI by design: the packages' install hooks run
  during install, and torch/laya pull a large dependency tree. A fixed
  command prevents the browser from choosing *which* package, but it does
  not change the fact that anyone holding the dashboard token — or, on a
  loopback dashboard with no token, any local process that can pass the
  CSRF check — can trigger a multi-gigabyte download, fill the disk, and
  execute installer code on the host. The brief treats the fixed command
  as the whole security story. It is only half of it: the other half is
  who may trigger a host mutation from a browser at all, and whether
  that permission should exist on a remote dashboard in the first place.
  A per-repo page triggering a machine-global side effect sharpens this:
  clicking Install while viewing repo A mutates the environment shared
  by every repo that dashboard serves, with the log filed under repo A's
  `.jev/` directory.
- **Alternative / Reuse Opportunity:** Keep the browser-install precedent's
  boundary: local installs from the browser, remote installs from the
  terminal or the deploy. If remote installation is genuinely required,
  it needs its own decision in the draft — who is authorised, whether
  loopback-only is the rule, and how the user distinguishes "I installed
  this on this host" from "I installed this for this repo" — not a
  silent extension of the local pattern to VPS.

### 6. A third bespoke runner is codebase growth, not reuse

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** The dashboard already has two background-job
  shapes with different semantics: `BrowserInstallRunner` (a subprocess
  that can be terminated, output to a capped log file, stop wins over
  completion) and `CodeGradeRunner` (an in-process thread that cannot be
  interrupted mid-inference, cancel discards the result and drains, a
  generation counter drops stale outcomes). A CDA install is a third
  shape that resembles the first but differs in the ways that matter:
  pip is killable, torch download resumption and partial-environment
  state are not the same as an `npx` install, and the post-install
  verification step has no counterpart in either runner. Copying
  `BrowserInstallRunner` into a CDA-specific runner triples the surface
  that has already needed real fixes — the log-cap and tail-read fix
  (issue #200) exists in the browser runner and would need re-proving in
  a copy — and adds a third shutdown path (the lifespan already waits up
  to 30 s per job) and a third in-memory state that resets to idle on
  dashboard restart, losing a running or failed install's state while its
  log file lingers. The brief lists "mirror or generalise" as open
  decision 1; from a zoom-out view, mirroring is the wrong default.
  Every new runner is a new maintenance liability, and this codebase
  already pays that cost twice.
- **Alternative / Reuse Opportunity:** Generalise `BrowserInstallRunner`
  into one fixed-command runner parameterised by command, log name, and
  post-run verification — or reuse it directly with a different argv and
  log file — and only split into a CDA-specific runner if the draft can
  name a difference that parameterisation cannot express. The same
  argument applies to the status page: the live-region polling pattern
  from `/code-grade/status` already exists and should be reused, not
  re-implemented.

### 7. The install is per-host, but grading is needed per-pipeline, per-dashboard, and per-fleet

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** The button installs into the host running that one
  dashboard process. It says nothing about the other places a grade can
  run: the quality gate inside a `jev` pipeline (which may run on a
  different host, in CI, or under a worker server configured in
  `jev.toml`), a second dashboard on another machine, or a Mac dashboard
  plus a VPS deploy side by side. A user who enables grading under
  Settings → Code orchestrator on one host and clicks Install there has
  not installed anything for runs elsewhere, and nothing in the UI shows
  that gap — each dashboard reports only its own host's availability.
  The brief's scope is the Code Grade page, so this is arguably out of
  scope, but at the system level it means the button solves the
  "grading unavailable" message on one page while leaving the same message
  (as a recorded `unavailable` grade) in pipeline runs untouched. The
  draft should state this boundary plainly, or the button will be read
  as "grading is now installed" when it means "grading is now installed
  on this host, for this dashboard process".
- **Alternative / Reuse Opportunity:** Treat install state as host state
  and show it as such (host, environment, backend version, install date),
  and keep the pipeline's `unavailable` recording as the honest signal
  for hosts that have not been provisioned. Alternatively, provision at
  the level where grading actually runs — the pipeline host — through
  the deploy, not through one dashboard's page.

### 8. There is no lifecycle after install, so the terminal returns anyway

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** The brief's non-goals exclude automatic updates, and
  the goals contain no manual update, no uninstall, and no display of
  which backend version is installed. That leaves every later lifecycle
  event in the terminal: a broken torch after a system upgrade, a new
  `laya` release, a checkpoint change, a disk-full cache cleanup, and —
  per finding 1 — the next Baton upgrade itself, which may wipe or strand
  the extra. A feature whose first install is browser-first but whose
  maintenance is terminal-only has not met the browser-first law; it has
  deferred it. The same applies to repair: a half-finished install's
  "retry is safe" claim depends on pip idempotence per package, but a
  half-downloaded checkpoint in the Hugging Face cache, a partially
  extracted torch wheel, or a killed pip leaving a locked environment
  are not covered by that claim, and the brief gives no repair story
  beyond "offer retry".
- **Alternative / Reuse Opportunity:** Either scope the button honestly as
  first-install-only and say the lifecycle stays terminal (and accept
  that this weakens the browser-first justification), or include the
  minimal lifecycle in the draft: installed version display, uninstall,
  and a repair/reinstall path that is tested against a killed pip, not
  just a failed pip. Do not ship a button whose second use is already a
  terminal command.

### 9. The value question: this is a heavy installer for a feature that is off by default and rarely auto-decides

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** Measured on real Baton diffs, nothing cleared the
  0.7 threshold: merge-action confidence ran 0.36–0.52 across 13 grades,
  the auto-accept and auto-reject paths were effectively dead, and the
  feature's value was stated as human-review routing, the code-quality
  signal, and the recorded grade. Grading is off by default, the backend
  is a 421M model behind torch, and the install is several gigabytes and
  many minutes. That combination deserves a YAGNI question the brief
  does not ask: is "users cannot install the backend" actually the
  blocker for this feature's adoption, or is the blocker that the feature
  is opt-in, heavy, and advisory? If the audience is the small set of
  users who deliberately enable an experimental grader, a documented
  terminal command plus a copy button may serve them as well as a
  cross-platform installer, at a fraction of the maintenance cost. The
  brief's counter-argument — the product law — is doing all the work
  here, but the law as practiced in this codebase already has a
  boundary: the installer does not install worker CLIs (`claude`,
  `codex`) or Node, and the dashboard discovers them instead. The draft
  should justify why the CDA backend crosses that boundary when worker
  runtimes do not, rather than treating the law as self-evidently
  requiring a runtime installer.
- **Alternative / Reuse Opportunity:** The established pattern in this
  codebase is discover-and-guide, not install-from-the-browser: worker
  CLIs are prerequisites the dashboard finds; Figma, Claude OAuth, and
  Codex device flows are credential connections made in the browser
  without installing binaries; the browser install is the lone exception
  and is local-only. The cheaper alternatives that fit that pattern are:
  (a) an install-time opt-in (`install.sh` flag, Docker image variant)
  where the platform is known; (b) a status page that detects the exact
  missing piece (no laya, no torch, no checkpoint, no disk, TMPDIR too
  small) and gives the exact command for that piece, copyable from the
  browser; (c) keeping the pip command but fixing what makes it painful
  — the CPU-wheel sequence, the TMPDIR warning, and the checkpoint
  expectation — in the docs and the status message. Any of these is
  less code than a third runner with platform-specific torch selection.

### 10. Restart and service semantics are not one instruction

- **Severity:** Major
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** The brief's open decision 7 asks whether the running
  dashboard picks up the new backend without a restart. Across the deploy
  shapes, "restart" means four different things: a manually started
  dashboard (`jev dashboard stop`, then start); a launchd agent with
  `KeepAlive` (stopping it restarts it, and the service definition
  records the binary path at install time); a systemd user unit with
  `Restart=always`; a Windows Scheduled Task with no stop-timeout, where
  stopping ends the dashboard at once and running jobs lose graceful
  shutdown. A UI message saying "restart the dashboard" is therefore not
  actionable in the same way everywhere, and on service-managed dashboards
  the user's normal stop command does not even stop the service. There
  is a second, quieter question: whether a long-running Python process
  reliably sees packages pip-installed under it. `find_spec` may find
  `laya` after install, but importing torch in a process that has already
  imported parts of the dependency tree, or that holds a stale
  `sys.path` / import cache, is not guaranteed to behave like a fresh
  process — and the brief gives no evidence either way. If a restart is
  required, the button has not removed the terminal step; it has moved
  it to after the install, on exactly the service-managed setups where
  the restart is hardest to explain.
- **Alternative / Reuse Opportunity:** Reuse the deploy documentation's
  own restart instructions per platform (they already exist, per service
  type) instead of writing a new generic restart message, and prove the
  no-restart claim with a test — install into a running dashboard process
  and grade — before the draft promises it. If that test fails, the
  draft should say restart is required and link the per-platform restart
  steps, not leave it as an open question.

### 11. Service-user vs terminal-user environment mismatch will make installs appear and disappear

- **Severity:** Nit (with Major consequences if ignored)
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** The Hugging Face cache, TMPDIR, and `--user`
  site-packages are all per-user, per-environment variables. A user who
  installs from a terminal (HOME, HF_HOME, TMPDIR set in their shell)
  and then grades from a service-managed dashboard (minimal service
  environment, possibly a different HOME) may find the checkpoint in one
  place and the grader looking in another — or vice versa for a button
  install performed by the service user and later inspected from the
  terminal. The brief's environment discussion covers the pip target but
  not the cache and temp-dir identity of the grading process, even
  though the docs already record a real TMPDIR failure mode. This is the
  class of bug that presents as "I installed it and it still says not
  installed" and is debugged in the terminal — the exact outcome the
  button is meant to prevent.
- **Alternative / Reuse Opportunity:** Have the status panel show the
  facts that determine visibility — which Python, which user, HF cache
  location, TMPDIR — the way `jev code status` and `jev doctor` already
  report backend and environment facts. If the button cannot guarantee
  those match between installer and grader, it should not claim a
  verified install.

### 12. Runtime pip bypasses the reproducibility the rest of the release has

- **Severity:** Nit
- **Dimension:** Contrarian / YAGNI & Code Minimization
- **Challenge/Risk:** Baton's own dependencies are pinned (`uv.lock`,
  pinned Docker tags, `laya>=0.4.15` as a lower bound in
  `pyproject.toml`). A runtime pip install has no lockfile: two users
  clicking Install a month apart can get different `laya`, torch, and
  transitive dependency versions, and therefore different grading
  behaviour, with no record of which version produced which recorded
  grade. The rating ledger records grades, not the backend version that
  produced them, so a later calibration (Phase 4 of the CDA roadmap,
  "recalibrate 0.7 once 50+ real grades exist") would be calibrating
  against a mixed, unrecorded set of backends. This is a small point
  against a large installer, but it cuts the other way too: it is an
  argument for install-time provisioning, where the version is chosen
  once, recorded, and shipped with the release.
- **Alternative / Reuse Opportunity:** At minimum, record the installed
  backend version alongside the install (and, ideally, in ledger records)
  so grades stay attributable. Better, pin the runtime install to the
  versions the release was tested against, the way the Docker image pins
  its tag.

---

## What the draft must answer before this concept proceeds

1. **Deploy scope first:** Is the button native-only, with Docker
   directed to an image path — or does the deploy model change to make a
   persisted, writable install location a supported part of the image?
   "Same button everywhere" is not an available answer on the current
   deploy.
2. **Environment identity:** Which Python, which user, which HOME, and
   service-managed or not — detected how, and what happens on each of
   pipx, editable, system-Python (PEP 668), and Docker paths?
3. **Torch variant and true footprint:** CPU wheel or CUDA wheel, disk
   and RAM checks, checkpoint pre-warm or honest "first grade still
   downloads", TMPDIR and HF cache handling — as a per-platform plan,
   not a single fixed command.
4. **Trust boundary:** Local-only like browser install, or remote too —
   and if remote, authorised by whom, on what authentication, with what
   per-host (not per-repo) scoping?
5. **Runner reuse:** Generalised fixed-command runner, or a named reason
   a third runner is unavoidable?
6. **Lifecycle and restart:** Version display, update, uninstall, repair
   after a killed install, and per-platform restart instructions — or an
   explicit statement that first install is the whole scope and the rest
   stays terminal.
7. **Value check:** Evidence that install friction, rather than the
  feature being opt-in, heavy, and advisory at the measured threshold,
  is what blocks adoption — and why the CDA backend crosses the
  discover-don't-install boundary that worker CLIs do not.

Until those are answered, the concept is a page feature wrapped around a
deployment problem it has not yet owned.
