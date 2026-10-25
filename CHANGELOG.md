# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Design detail for every entry is in
[docs/decisions.md](docs/decisions.md).

## [Unreleased]

### Added

- **Cache dashboard UI (issue #254).** The Tools page (`/tools`) gains a **Cache** section:
  `jev cache stats` as a live table (entries, hits, dollars saved, size, shadow agreement,
  semantic mode and thresholds), a **Clear cache** form (all/decisions/roles, optional
  older-than filter) behind a confirmation page naming what goes, and **Run calibration**
  (`jev cache calibrate` with one-click apply of the candidate threshold). Misses aren't
  recorded by the store, so there is no hit rate to show — the section says so instead of
  inventing one. Calibration is shared with the CLI (`core.calibrate_thresholds`), so the
  two surfaces can never disagree about the curve.

### Removed

- **Laya second vote (issue #193).** The Laya second vote, `jev laya tune` / `jev laya reset`,
  and the `[dispatcher.laya]` config table are removed. Jev is now the sole decision-maker as
  main orchestrator; code grading and routing belong to the Coding Decision Agent
  (`[code_orchestrator]`, see [docs/code-orchestrator.md](docs/code-orchestrator.md)).
  Historical entries below still mention Laya: they describe released versions, not current
  behavior.

## [0.5.0] - 2026-10-17

### Added

- **Design systems** ([#109](https://github.com/Gane2506/baton/issues/109)).
  `jev design new` is an interactive wizard (purpose → app type mobile/website/PWA/
  dashboard → stack recommendation → brand inputs) that saves a complete kit —
  type-aware `tokens.json`, component docs, logo mark SVG+PNG, multi-size favicon —
  into the user-level library at `~/.jev/design-systems/<name>/`. The library
  supports `list`/`use`/`clone`/`export`/`delete`; `export` writes W3C design
  tokens, a Tailwind config, or CSS variables. `jev design reverse <url>` extracts
  a system from a website or screenshots (URL fetching is SSRF-guarded). A
  design-system picker is available in `jev chat` and on the dashboard New chat
  form, and the `designer` role grounds UI work in the repo's stored system
  (see [docs/design.md](docs/design.md)).
- **Full audit fix program** ([#108](https://github.com/Gane2506/baton/issues/108)).
  All 22 major, ~60 minor and ~25 nit findings from the 2026-10-16 full-repo audit
  (152 source files) fixed, with ~175 new regression tests.
- **Issue #59 closeout verification**
  ([#59](https://github.com/Gane2506/baton/issues/59)).
  All 48 findings from the 2026-10-13 review verified fixed one-by-one against the
  current code; the 4 genuine residuals ship here: timeout-guarded `run_git` in
  `cli/migrate` and capbench (60s quick / 600s slow), redact-before-slice in
  `fix_task_card`, the directly-imported `pydantic-core>=2.18` declared in
  `pyproject.toml`, and a lightweight `views.run_live_view()` for the 3s live poll.
- **CI green on main** ([#112](https://github.com/Gane2506/baton/issues/112)).
  All required checks (lint, build, tests on 3.11/3.12/3.13, screenshots,
  structural principles) pass on `main`, which is now branch-protected (PR
  required, all checks green, enforced on admins, no force-push).

- **Decision/role cache** ([#10](https://github.com/Gane2506/baton/issues/10)).
  Exact-match caching for dispatcher decisions (phase 1) and read-only role calls (phase 2):
  `Harness.decide` and `Harness.run_worker` serve settled answers from the repo's own
  state DB (new `cache_entries` table, migration v17) without another model call. Volatile
  state (round counters, spend, timestamps) is stripped so `jev resume` hits; the repo
  fingerprint (HEAD + diff + untracked files) and the config fingerprint (`min_confidence`,
  Laya weights, model, templates, lessons) invalidate automatically. Hard rules in code:
  coder/learner never cached, only `success` results stored, rules-fallback answers never
  stored, CLI workers and D6 excluded by default, `--no-cache` kill-switch. Hits are
  recorded with `details.cached_from`/`cache_hit` provenance, a `[served from cache]` marker
  on worker summaries, a `cached` badge in `jev log` and on the dashboard, and `saved_usd`
  on the Spend page. Phase 3 ships default-off: the `Embedder` protocol with a stdlib
  SimHash backend, `jev cache calibrate` for threshold picking, and shadow mode that
  measures agreement without serving. `jev laya tune` dedupes on `cached_from` so a
  replayed vote can't overweight the original. See [docs/config.md](docs/config.md)
  "`[cache]`".
- **Event-sourced execution state** ([docs/events.md](docs/events.md), schema v18).
  Every run mutation is dual-written as an append-only event in the new `run_events`
  table — same transaction as the state change (`run_seq = MAX(run_seq)+1`), so the
  log and the snapshots never diverge; the single-writer invariant (one lease holder
  per run) is documented in `Store._emit`. 22 typed event kinds with causal
  `parent_run_seq` links; payloads carry ids and summaries only (prompts and
  transcripts stay in the redacted log files). `[events] enabled = true` (default)
  is the kill switch. New commands: `jev events RUN` (timeline, `--type/--after/
  --limit/--json`), `jev events backfill RUN` (synthetic history for pre-log runs),
  `jev replay RUN [--to SEQ] [--step] [--diff]` (verify mode: envelope integrity,
  transition consistency, decision contracts, snapshot agreement — exit 0/2, no LLM
  calls), and `jev bisect RUN --gate/--test/--task/--mark` (first-bad-event scan +
  causal-chain blame walk, `--export timeline.html`). Cassette re-execution and
  checkpoints are deliberately out of scope; the dashboard timeline view is deferred
  (the dashboard keeps reading the snapshot tables). A drift test classifies every
  mutating `Store` method as emitting or snapshot-only, so future mutations cannot
  silently skip the log.

### Fixed

- **Ship/merge path verified live** ([#68](https://github.com/Gane2506/baton/issues/68)).
  `gh pr view --json` and `gh pr merge` were exercised against a disposable scratch repo:
  merge success (exit 0, no output off-terminal), refusal on a wrong `--match-head-commit`
  (exit 1) and on required reviews (exit 1), idempotent re-merge of an already-merged PR,
  and the real `statusCheckRollup` shapes — all match what `merge_check` assumes. Findings
  are recorded in `docs/verification.md` section 8. Offline tests now also cover the
  checks-timeout stop in the polling loop and a `gh pr merge` timeout that GitHub still
  merged (counts as merged).

### Added

- **`jev init --demo` and `jev demo reset`** ([#105](https://github.com/Gane2506/baton/issues/105)).
  One command scaffolds a demo project pre-populated with sample runs, queue items, lessons,
  spend data, shipped PRs and a saved chat, reusing `examples/demo/seed_demo.py` (now also
  importable as `jev_agent.demo.seeder`, with a `demo_config=False` mode for real user use).
  Demo data is clearly marked (`.jev/demo.json`, a dashboard banner, `[demo]` log notes);
  seeding touches nothing outside the repo and needs no repo trust, and `jev demo reset`
  removes the demo state again. See [docs/demo.md](docs/demo.md).

- **Capability benchmark scoreboard (Phase 1).** New `src/jev_agent/capbench/` package
  with `jev capbench run|grade|report|tasks`: three fixed tasks (`benchmarks/tasks/`,
  each a `brief.md` + self-contained `fixture/` + deterministic `grade.py`) run through
  the real pipeline in throwaway sandbox repos (fixed `$2` budget cap, `basic` quality
  level, pinned worker profile) and graded by hidden pytest, diff-size bands and the
  gate's ship score — no LLM judge. The public scoreboard is `docs/benchmarks.md`
  (currently an unevaluated template — no scores are fabricated) plus a README badge
  fed by `docs/benchmarks.json`. Manual trigger only: no nightly schedule was created
  (the ~$3–7/night model spend needs explicit go-ahead).

## [0.4.0] - 2026-10-14

### Added

- **Autonomy modes and access tiers.** New `[autonomy]` settings: `mode` (`standard` — the
  historical approve-once flow, `autonomous`, `ask_every_step`, `manual`, `medium`) with a
  plain-language picker in the dashboard's beginner settings, plus `access_tier`
  (`basic`/`medium`/`unrestricted`) in Advanced. `unrestricted` is an explicit opt-in
  tier (needs `unrestricted_acknowledged = true`, coder role only, audit-logged on grant);
  `medium` servers can set `project_folder` to scope agents to one folder, enforced
  fail-closed against `..` escapes and symlink breakouts. Ask-every-step runs wait at an
  explicit `AWAITING_*` gate per pipeline step. Run state DB schema v14 records the mode.

- **`jev mcp runs`: pipeline tools for external agents.** A second minimal MCP server
  (the same stdio JSON-RPC protocol as `jev mcp servers`, no SDK): `run_list`,
  `run_status` and `quality_show` are read-only; `run_start` and `chat_send` need
  `--allow-mutations`. `run_start` creates the run and records the `[autonomy]` mode
  without advancing it or approving any gate; every call is audit-logged (redacted) to
  `.jev/audit/mcp_runs.log`.

- **Dashboard: Settings editor** ([#9](https://github.com/Gane2506/baton/issues/9)). Typed
  controls for the common `jev.toml` settings with a section list, validation exactly as `jev`
  loads the config, a diff before saving, reset to default, and atomic writes that keep
  comments. Keys that decide where API keys go and what agents may run, `[trust]` and the user
  config stay read-only. Adds `tomlkit` to the `dashboard` extra.

### Changed

- **Screenshots come from the running app** ([#83](https://github.com/Gane2506/baton/issues/83)).
  `examples/demo/screenshots.py` builds a demo repo, starts `jev dashboard` and captures every
  image in `docs/images/` with headless Chrome; all of them are recaptured from v0.3.0 (the old
  dashboard images showed v0.2.0, without the KPIs page or Laya's votes). CI's new
  `screenshots` job uploads the same captures for every pull request, and a pull request that
  changes the UI commits the regenerated images.

### Fixed

- **Laya confidence range.** `_parse_scores` now rejects Laya confidence values outside
  [0, 1] with a `DispatchError` instead of silently accepting them ([#79](https://github.com/Gane2506/baton/issues/79)).
- A chat open in a terminal (`jev chat`) and a dashboard reply to the same chat could overwrite
  each other's `meta.json` (spend, turns, brief, runs). One process has a chat at a time now
  (`chat.lock`). The dashboard's new-chat form also rejects `inf` and `nan` budgets.
- `examples/demo/seed_demo.py` failed on a fresh repo since repo-level tools and skills need
  `[trust]`; it now trusts the demo in a user config inside the demo repo, and only there.

## [0.3.0] - 2026-10-13

### Added

- **Consensus dispatcher: Jev votes, then Laya** ([#66](https://github.com/Gane2506/baton/issues/66)).
  `[dispatcher] primary = "consensus"` is the new default; `"jev"` and `"rules"` still work.
  - Every decision needs Jev and Laya to agree confidently. Laya is Jev scoring each allowed
    answer 1-5 on the Laya CDA matrix (correctness 0.30, safety 0.20, simplicity 0.15,
    maintainability 0.15, tool fit 0.10, user preference 0.10).
  - Without agreement the question is asked again up to `follow_up_rounds` times, then the run
    stops in `NEEDS_HUMAN` and `jev resume` continues it. It never falls back to rules.
  - New `[dispatcher.laya]` table: `weights`, `min_margin` (0.2) and `follow_up_rounds` (2).
  - `[approval] require_plan_approval` is unset by default: off under `consensus` (the D3
    approve vote is the gate), on with `jev` and `rules`. An explicit `true` or `false` wins.
  - `jev doctor` fails the dispatcher check under `consensus` when the Jev key env var is unset
    (runs would stop at their first decision); with `jev` it stays a warning.

- **Tune Laya's weights from run outcomes** ([#69](https://github.com/Gane2506/baton/issues/69)).
  Consensus decisions store Laya's final scores and the weights used. `jev laya tune` derives
  whether the work each decision chose passed (D4 from the task's next D5; the rest from the
  ended run and its tasks) and suggests weights that favour the criteria that told passing work
  from failing work; it never applies them on its own. `--apply` saves them on a learning
  profile, runs that learn with it use them (weights in `jev.toml` still win), and
  `jev laya reset` clears them. State DB schema 13, shared learning DB 3.

- **The validator asks "why X, not Y"** ([#67](https://github.com/Gane2506/baton/issues/67)).
  It records a challenge per plan choice (the alternative, the planner's reason, whether it
  holds); a choice without a sound reason is an issue that sends the plan back to the planner,
  and the D3 vote sees the failed challenges.
- **The dispatcher picks the task reviewer** ([#67](https://github.com/Gane2506/baton/issues/67)).
  A new decision point, D8, chooses the reviewer's worker, model and effort before each review
  (the rules dispatcher keeps the old first worker, first model, middle effort).
  `[review] enabled` is unset by default: on under `consensus`, so a task is done only when Jev
  and Laya accept a reviewed attempt, and off with `jev` and `rules`. An explicit value wins.
  `[roles.reviewer] prefer_different_vendor_than` now works (`"coder"`: this attempt's coder).
- **Skill lookup in every role prompt** ([#67](https://github.com/Gane2506/baton/issues/67)). The built-in planner, validator, coder, reviewer, code reviewer, learner and chat prompts now tell the agent to look for relevant skills first, use the ones already installed, and never install a new skill without the maintainer's approval. See [docs/roles.md](docs/roles.md#skills-in-every-prompt).
- **Ship and merge once both votes approve, or ask first** ([#68](https://github.com/Gane2506/baton/issues/68)).
  With `[ship] enabled = true`, a D6 "ship" pushes the result, opens the PR ready, waits for
  its checks and merges it (`gh pr merge --match-head-commit`, `merge_method` configurable),
  or with `[ship] after_approval = "ask"` waits for `jev ship approve | modify --notes | stop`
  (also on the dashboard run page). `jev run` / `jev queue add` take `--auto-ship` and
  `--ask-before-ship`. Any failure stops for a person; this reverses "jev never merges".

- **Contributor guidelines.** A detailed `CONTRIBUTING.md`, a pull request template and issue
  forms for bugs and features. `SECURITY.md` explains private vulnerability reporting, and
  `CODE_OF_CONDUCT.md` is the Contributor Covenant 2.1.
  - `CONTRIBUTING.md` covers finding work, the CI check command, a pre-PR checklist, branch and
    commit style, the docs that change with code, and review and merge.
  - Its module map now lists every module (#55).
  - It now sets out the 16-point code-quality bar review applies, and the rules for changes
    written with a coding agent; the pull request template asks about both (#71).

- **Servers over SSH, virtual browser, workspace profiles**
  ([#3](https://github.com/Gane2506/baton/issues/3), first version: `basic` and `medium`).
  `[servers.<name>]` (host or `~/.ssh/config` alias, `access = "basic" | "medium"`, `workdir`,
  `allowed_paths`); agents reach them only through jev's stdio MCP server (`jev mcp servers`:
  `list_servers`, `run_command`, `read_file`, and at medium `write_file`, `upload`), which
  checks every command against the access policy and an always-blocked list and appends to
  `.jev/audit/servers.log`. SSH runs with `BatchMode=yes`, `StrictHostKeyChecking=yes` and
  connection reuse under `.jev/ssh/`. Roles get servers with `[roles.<role>] servers = [...]`;
  every role but the coder is capped at basic. `[browser]` and `browser = true` give roles the
  pinned Playwright MCP server (`@playwright/mcp@0.0.83`, headless, isolated);
  `jev browser install [--server NAME] [--yes] [--dry-run]`. `[profiles.<name>]` bundles
  servers, lower-only access overrides, browser, learning and quality; `--profile` on
  `jev run`, `resume`, `queue add` and `chat`, a menu on a terminal, `jev profile
  list|show|use`; the run's copy lives in `.jev/runs/<id>/profile.json`. `jev doctor` gets
  server, browser and profile rows and `--connect`. `access = "full"` is rejected ("not
  available yet; see issue #3"). `python -m jev_agent` now works. See
  [docs/servers.md](docs/servers.md).
- **`jev ship`, KPIs and benchmarks** ([#8](https://github.com/Gane2506/baton/issues/8)).
  Opt-in (`[ship] enabled = true`): `jev ship RUN` pushes a DONE run's result branch to a new
  branch and opens a (draft) GitHub PR with your `gh` login; the body has the idea, plan, task
  table, quality summary, a KPI card and decision stats. Never force-pushes, never merges;
  `--dry-run`, `--yes`, `--force-new`. `jev ship sync` reads state, reviews, CI, follow-up
  commits and reverts back; `jev kpi [--last N] [--json]` shows per-PR KPIs, medians and
  regression flags; the dashboard gets a **Ship as PR** button and a **KPIs** page with SVG
  trend charts (rolling median, regressions in red). New migration 7 (`shipped_prs`,
  `run_kpis`). See [docs/ship.md](docs/ship.md).
- **`jev dashboard`.** A local web dashboard (optional `dashboard` extra: Starlette, uvicorn,
  python-multipart, markdown-it-py): live Kanban board, run details (plan, decisions with Jev
  confidences, attempts, learning, quality review, log files) with Approve / Resume / Cancel,
  the queue (add, reorder, remove, scheduler status), learning profiles and lessons, chat
  transcripts, spend per run and per day, and read-only settings with `jev doctor`. Binds
  127.0.0.1; any other host requires a token. CSRF-protected POST actions, strict CSP, no
  CDNs. See [docs/dashboard.md](docs/dashboard.md).
- **Logo, dashboard screenshots.** A Baton mark and wordmark (`docs/images/`, light and dark),
  a social preview image, the mark as the dashboard favicon and header logo, and dashboard
  screenshots in the README and docs/dashboard.md, generated from illustrative demo data by
  `examples/demo/seed_demo.py`.
- **Feature screenshots in README.** The Features section is now a gallery: one dashboard
  and/or terminal image per feature. `examples/demo/capture.py` regenerates every terminal
  image (`docs/images/cli-*.svg`) from the seeded demo repo in one command, offline; the seed
  script now also adds a saved chat, a review with findings of several severities, a cached
  model list and example tool config. See [docs/dashboard.md](docs/dashboard.md#screenshots).

### Fixed

- **Jev option descriptions match the request contract; confidence is range-checked**
  ([#35](https://github.com/Gane2506/baton/issues/35), thanks @KeuqS in
  [#62](https://github.com/Gane2506/baton/pull/62)). Every allowed answer now reaches Jev with
  its description, the instructions no longer mention state that is never sent, and a NaN,
  infinite, negative, above-1 or overflowing confidence is rejected (the `jev` dispatcher then
  falls back to rules).
- **Two processes can no longer drive the same run** ([#61](https://github.com/Gane2506/baton/issues/61)).
  `jev run`, `approve`, `resume`, the scheduler and the dashboard hold a run lease in the state
  DB while they drive a run; a second one fails with "run ... is in use by another jev process,
  pid P on HOST". A lease whose process died (same host) or whose heartbeat is over 60 s old
  (another host) is taken over. The dashboard shows Force resume while another process holds
  the lease. State DB schema version 11.
- **CLI errors** (#25). They go to stderr with their text verbatim. A state DB from a newer jev
  gives a one-line error, not a traceback.
- **Read-only commands no longer build the pipeline** (#26). These commands build no workers,
  fetch no model list and create no `.jev/`: `status`, `log`, `board`, `queue list`,
  `learning list/show`, `quality show`, `scheduler status`, `chat --list`, `kpi` and
  `ship sync`.
- **Dashboard jobs show the real error** (#51), and they include run-start warnings.
- **Concurrent state DB upgrades** (#23). Two processes that open an old state DB or shared
  learning DB at the same time no longer crash. Migrations re-check the version under the write
  lock, and the switch to WAL mode waits for the lock.
- **`jev ship` recovers a lost PR response** (#41). If GitHub opened the PR but `gh pr create`
  failed or timed out, the PR is recorded instead of being refused or opened twice. A timeout is
  reported as an unknown outcome.
- **KPIs no longer parse text** (#42). Lessons added are read from counts stored on the run
  (migration 10), and the PR state is typed.
- **Config typos are errors** (#30). These used to be ignored silently:
  - unknown `[dispatcher.jev.instructions]` keys;
  - effort levels that none of a role's workers accept;
  - `[dispatcher] fallback` values other than `"rules"`. The key is deprecated.
- **Test isolation** (#53). The test suite no longer picks up the developer's jev or git config,
  or their API keys. Narrow-terminal output has its own tests.
- **Jev call timeout** (#36). `[dispatcher.jev] timeout_s` now bounds the whole call, retry and
  backoff included. A connect error is retried once, sharing the single retry with 429/5xx.

- **Git calls cannot hang a run** (#24). Worktree and `jev ship` git calls have timeouts (1
  minute; 10 minutes for commits, merges, checkouts and pushes) and run off the event loop; a
  failed commit (for example a hook that hangs) stops the run resumably, so `jev resume`
  continues once it is fixed. `jev ship` never waits on a credential prompt. A half-created or
  stray worktree directory is rebuilt instead of reused. New `[workspace] run_hooks` (default
  true) can skip the repo's git hooks on jev's commits and merges.
- **`jev init --migrate` no longer stops half-way** (#29): `state.db` is updated right after
  `.jao/` moves, refs it cannot rename are reported, and a rerun completes what is left.
- **`jev run` / `approve` / `resume` exit 3 when the run stops short of DONE** (#28):
  NEEDS_HUMAN, FAILED, BUDGET_EXCEEDED or CANCELLED (0 for DONE or awaiting approval, 1 for
  errors), so `jev approve RUN && jev ship RUN` no longer ships a failed run.
- **Workspace-profile docs are current** (#31): docs/queue.md and docs/chat.md no longer say
  profiles are refused; they are applied to scheduled and chat-launched runs. Leftover shims
  removed.
- **A killed scheduler no longer leaves its item `running` forever** (#38, #39): the run is
  linked to its queue item when it is created, the next `jev scheduler start` recovers orphaned
  items, and `jev queue remove` works when no scheduler is alive. Remove can no longer delete an
  item being claimed, a removed item no longer crashes the scheduler, and the scheduler lock
  uses `flock`, so two starters cannot both take over a stale lock.
- **Learned lesson ids are never reused** (#40) after `jev learning flush`/`delete` (state DB
  schema 9, shared learning DB version 2); end-of-run scoring only touches the run's own
  profile and counts each task once, even after a crash and `jev resume`.
- **CI builds and smoke-tests the wheel; the PyPI publish action is pinned** (#56).
- **The servers MCP process survives bad input** (#47): non-finite numbers are refused, a
  failing tool returns an error result, a request that cannot be handled gets a JSON-RPC error,
  and the next request is served as usual. Tests now cover run failure, cleanup and
  cancellation paths, migration rollback and server failure paths (#54).
- **Cancellations stick; `--plan-only` survives `jev resume`** (#14, #22). Run state changes
  are conditional in the store, so a `jev cancel` (or the dashboard's Cancel) made while a
  worker runs is no longer overwritten when the worker then fails, and a cancel cannot replace a
  run that just finished. `--plan-only` is stored on the run (state DB migration 8), so a
  resumed plan-only run still stops at the approval gate.
- **The dashboard does not offer a plain Resume for a run that may be running** (#15): an
  attempt open for less than its worker's timeout plus 5 minutes, or the scheduler's current
  item. "Force resume" asks for a confirmation that names the risk of a second pipeline.
- **A task is never DONE after its worker or its tests failed** (#21), whatever the dispatcher
  answers at D5; the override to retry is noted on the task.
- **Workers that cannot run are not offered** (#19): a CLI worker whose command is not on
  `PATH`, the api worker without its key, or without a model it can run for the role (it never
  runs the coder). Each gives one warning at run start; `jev doctor` uses the same rule.
- **`jev init` writes every setting commented out** (#20), so a new `jev.toml` no longer
  overrides your user config (for example a lower spend cap) or trips the trust gate.
- **Workers are paired only with models they can run** (#33): `vendor/name` slugs run on the
  api worker, other names on Claude Code / Codex; a mismatched answer gets the worker's first
  runnable model. A missing or non-executable worker CLI is a worker `error`, and D5 reassigns
  at once on `error`; dropped API connections are a retryable `failure` (#34).
- **A repository's `jev.toml` can no longer redirect credentials or widen permissions**
  (#16, security). Keys that choose where API keys go, which credential variables are read,
  worker commands and permissions, tools, servers, browser and profiles may be changed by a
  repo file only when your user config trusts it: `[trust] repos = ["~/code/app"]` (or `"*"`).
  Otherwise jev stops with an error naming the keys. Before, a cloned repo's `jev.toml` could
  make even `jev status` send e.g. `GITHUB_TOKEN` to another host. Error messages now print
  config tables such as `[trust]` verbatim.
- **Redaction covers common credential formats** (#27, security): GitHub, Slack, AWS and Google
  tokens, private-key blocks, JWTs, `Authorization` header values and `user:password@`
  in URLs, from one shared list that the quality gate's secret scan and the config check use
  too. Summaries are redacted before they are shortened, so no token prefix is left.
- **Quality gate: tool errors block when the tool is required** (#18, #17, security). A check
  whose tool crashes, times out, prints unparseable output or lists scan errors (bandit,
  Semgrep), and a check that raises, now reports a new `error` status instead of `skipped` or a
  fake minor finding; a required check with that status blocks. gitleaks exiting 1 with output
  that cannot be parsed now fails with a critical finding instead of passing.
- **Server tools: credential reads, writes, `.git` and audit hardened** (#44, #45, #46, #48,
  security). Recursive `grep` only inside workdir/allowed_paths and never into credential
  files (`-R` refused); `docker inspect`, `systemctl show` and `ps e` are no longer allowed;
  recursive `cp` only from inside allowed_paths; `git show REV:path` is checked and
  `git diff --no-index` refused. File writes use a fresh `mktemp` file in the target's folder,
  so a planted symlink can no longer redirect them. Medium access never writes under `.git`,
  and every `git` runs with hooks and fsmonitor off; the docs now say plainly that medium
  access means code execution as the server account. The audit log writes a `started` line
  before ssh runs and a `finished` line after; audit write failures become tool errors.
- **Processes are cleaned up on cancellation, timeout and shutdown** (#11, #12, #13, #49).
  Ctrl-C (or any cancellation) now kills the Claude Code / Codex process group instead of
  leaving it running. Test commands (task, integration and quality tools) run through the
  same process-group runner, so `[budget] test_timeout_s` and `[quality] timeout_s` are
  enforced even when the command starts children (`uv run pytest`, `npm test`); a timeout is
  reported as such rather than as exit code -1. Stopping `jev dashboard` cancels the jobs it
  started and waits for them. Server tool commands run under the server's `timeout`, so they
  stop there too.
- **Fix: Spend page labels** ([#6](https://github.com/Gane2506/baton/issues/6)). Long run
  labels in "Costliest runs" are cut with an ellipsis to fit, with the full label as a tooltip,
  and every value is drawn after its bar, so a $0.00 bar no longer puts its value over the label.
- Text in square brackets such as `[budget]`, `[quality]` and `[models]` was swallowed as Rich
  markup in `jev doctor`, `jev quality checks` and `jev models list --free`.

### Changed

- Logic that lived inside CLI commands is now reusable by the dashboard: `core.queue.add_idea`,
  `remove_item`, `queue_counts`; `core.learning.use_profile`, `enable_learning`,
  `disable_learning`, `delete_profile`; `core.pipeline.cancel_run`;
  `cli.run_cmds.approve_run`, `resume_run`; `core.scheduler.scheduler_lock`;
  `core.chat.read_transcript`. CLI behaviour is unchanged.
- CI installs every extra (`uv sync --locked --all-extras`).

## [0.2.0] - 2026-10-12

### Added

- **Learning profiles and the D7 learner.** Optional recursive learning: a learner step at the
  end of a run turns problems into short lessons, kept per profile in the repo or in a shared
  database, and injected into later prompts. `jev learning` manages profiles and lessons;
  `jev run --no-learning` skips them for one run. See [docs/learning.md](docs/learning.md).
- **`jev chat`.** Talk an idea through with a model that can read the repo, get a brief for the
  planner, then plan, run or queue it from the chat. See [docs/chat.md](docs/chat.md).
- **Idea queue, scheduler and Kanban board.** `jev queue`, `jev scheduler start` (run limits and
  a daily budget) and `jev board` (with `--watch`). See [docs/queue.md](docs/queue.md).
- **Free OpenRouter models.** `[models] prefer_free`, the `"free"` model entry for api roles,
  fallback on rate limits, and `jev models free` / `jev models list`.
- **Per-role agent tools.** MCP servers, skills and Claude Code plugins defined once under
  `[tools]` and given to roles by name; `jev doctor` lists them.
- **Quality and security gate.** `[quality] level` / `--quality` with levels `none`, `basic`,
  `medium`, `production` and `scalable`: linters, tests, scanners and an AI code review after
  each task and at integration, with reports via `jev quality show`. See
  [docs/quality.md](docs/quality.md).
- **`jev init --migrate`** moves a repo set up with v0.1.0 (`jao.toml`, `.jao/`, `jao/*`
  branches) to the new names.

### Changed

- **CLI renamed `jao` → `jev`**, with `jev.toml`, `.jev/`, `JEV_*` environment variables and
  `jev/<run_id>/...` branches. Run `jev init --migrate` in existing repos.
- **Import package is now `jev_agent`** (was `jao`).
- **Project renamed to Baton Agent Orchestrator.** PyPI distribution `baton-agent-orchestrator`
  (was `jev-agent-orchestrator`), long command alias `baton-agent-orchestrator` (the
  `jev-agent-orchestrator` alias is removed), repository
  `github.com/Gane2506/baton`.
- **Rules dispatcher D4 complexity heuristic:** files changed are now the main signal (hard
  needs 4+ files or 8+ acceptance criteria), so small tasks with many criteria are no longer
  rated "hard".

## 0.1.0 - 2026-10-12

Released before this public repository; its history is not included here.

First release, with the CLI named `jao`.

- Idea-to-code pipeline: plan, validate, approve, code, test, integrate, on local branches in
  isolated git worktrees; nothing is pushed.
- Jev dispatcher (via OpenRouter or native TypeSafe) at decision points D1 to D6, with a
  deterministic rules dispatcher as fallback.
- Workers: Claude Code CLI, Codex CLI and an OpenAI-compatible API worker (OpenRouter by
  default); planner, validator, coder and reviewer roles with custom prompt templates.
- Per-run and per-task budget caps, an audit log of every decision and attempt, redacted logs.
- Commands `init`, `doctor`, `run`, `approve`, `status`, `log`, `resume` and `cancel`.
- Times shown in UTC; a broken prompt template stops a run in a resumable `NEEDS_HUMAN`.
- Tag-triggered release workflow.

[Unreleased]: https://github.com/Gane2506/baton/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Gane2506/baton/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Gane2506/baton/releases/tag/v0.2.0
