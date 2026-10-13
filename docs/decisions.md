# Design decisions

Deviations from [PLAN.md](../PLAN.md) and choices it left open. Newest first.

## 2026-10-12: Web dashboard (`jev dashboard`)

Code: `src/jev_agent/dashboard/` (app, views, security, runner, render, templates, static),
`cli/dashboard_cmd.py`; tests in `tests/unit/test_dashboard.py`. User docs:
[dashboard.md](dashboard.md). Issue #1; PLAN.md's roadmap had "small FastAPI server + web
dashboard".

- **Starlette, not FastAPI.** The dashboard is server-rendered HTML and form POSTs; it needs
  no OpenAPI schema, request models or dependency injection. Starlette is the layer FastAPI is
  built on, with fewer moving parts, and its routes are a plain list in `create_app`.
  Templates are plain Jinja2 (already a core dependency) with `autoescape=True`; uvicorn
  serves it; python-multipart parses forms; markdown-it-py (already pulled in by rich) renders
  `review.md` with raw HTML disabled.
- **An optional extra, not core dependencies.** `[project.optional-dependencies] dashboard`.
  Most installs (CI machines, headless VPS runners) never open a browser, and an ASGI server
  is not small. `jev dashboard` checks with `importlib.util.find_spec` and prints
  `uv tool install 'baton-agent-orchestrator[dashboard]'` when something is missing. CI and
  release workflows use `uv sync --locked --all-extras`; contributors `uv sync --all-extras`.
- **No htmx.** The plan suggested vendoring htmx; the dashboard only needs "replace this
  element with the HTML at this URL every few seconds" and "confirm before submitting", so a
  40-line `static/live.js` does that (`data-live`, `data-confirm`). No third-party JavaScript to
  vendor, audit or update; it works under a strict CSP with no inline script; pages work
  without JavaScript.
- **Same functions as the CLI.** Logic that lived inside Typer commands moved to reusable
  functions, used by both: `core.queue.add_idea` / `remove_item` / `queue_counts`,
  `core.learning.use_profile` / `enable_learning` / `disable_learning` / `delete_profile`,
  `core.pipeline.cancel_run` (no pipeline needed to cancel), `cli.run_cmds.approve_run` /
  `resume_run`, `core.scheduler.scheduler_lock`, `cli.quality_cmds.review_text`,
  `core.chat.read_transcript`; `cli.board_cmds.load_board` takes a title width. Read-only
  pages reuse `load_board`/`build_board`, `format_answers`, `learning_line`, `quality_line`,
  `item_flags`, `effective_status`, `spent_today`, `run_checks` (doctor).
- **Runs execute in a background thread.** Approve and resume call `execute_run`, which is
  `jev approve` / `jev resume` (`cli_context` + `approve_run` / `resume_run`) in a daemon
  thread with its own store connection (SQLite connections are per thread) and event loop.
  `RunLauncher` keeps a per-run `threading.Lock`, held for the whole job, so one dashboard
  never runs two jobs for one run. A cross-process lock (dashboard vs. a terminal) is out of
  scope, as it already is between two terminals. Tests inject the executor.
- **Security model.** Loopback bind by default with a Host-header allowlist (DNS rebinding).
  Non-loopback binds refuse to start without a token (`DashboardConfigError`); `jev dashboard`
  generates one. Token via cookie, `X-Jev-Token` or `?token=` (swapped for an HttpOnly,
  SameSite=Strict cookie by a redirect). CSRF: POST only, same-origin check (`Sec-Fetch-Site`,
  `Origin`, `Referer`) plus a per-session form token, HMAC(session cookie, per-process secret).
  Strict CSP, no framing, `no-store`. Run files are served as `text/plain` only from inside
  `.jev/runs/<id>/` (resolved path must stay inside; symlinks out refused). Settings shows env
  var names and set/unset, never values. Flushing a profile needs a confirmation page.
- **No new config keys.** Options are flags; the token can also come from
  `JEV_DASHBOARD_TOKEN` (documented in config.md's environment variables), so it need not
  appear in the process list.
- **Scheduler start stays in the terminal.** The queue page shows the lock holder and current
  item; starting a long-lived scheduler from a web request was left out.

## 2026-10-12: Rename to Baton Agent Orchestrator

The project is now **Baton Agent Orchestrator** (v0.2.0). Why: the maintainer's choice. Only
the product name, the PyPI name, the long command alias and the repository URL change.

- **Title:** "Baton Agent Orchestrator" in the README, docs, `jev --help`, the package
  docstring and the OpenRouter attribution headers (`X-OpenRouter-Title`, `HTTP-Referer`).
- **PyPI distribution:** `baton-agent-orchestrator` (was `jev-agent-orchestrator`). The bare
  name `baton` is still taken on PyPI (see "Project name" below).
- **Console scripts:** `jev`, plus the long alias `baton-agent-orchestrator`; the
  `jev-agent-orchestrator` alias is gone.
- **Repository:** `https://github.com/Gane2506/baton` (renamed from
  `jev-agent-orchestrator`; GitHub redirects the old URL).
- **Unchanged:** the CLI `jev`, the import package `jev_agent`, `jev.toml`, `.jev/`, `JEV_*`
  variables, `jev/<run_id>/...` branches, and everything named after the Jev model
  (`[dispatcher] primary = "jev"`, `[dispatcher.jev]`, `JevDispatcher`). Older entries below
  keep the names they were written with.

## 2026-10-12: Quality and security gate

Code: `src/jev_agent/quality/` (levels, models, tools, base + checks_basic/medium/production,
policy, review, report, gate), role `code_reviewer`, migration 6, `cli/quality_cmds.py`; tests
in `tests/unit/test_quality.py`, `tests/unit/test_cli_quality.py`,
`tests/integration/test_quality_pipeline.py`. User docs: [quality.md](quality.md).

- **Beyond PLAN.md.** `[quality] level` (`none` default, then `basic`, `medium`, `production`,
  `scalable`, cumulative). Precedence: `--quality` on run/resume (and `/quality` in the chat) >
  the queue item's `--quality` > config. The level is stored on the run.
- **Storage: migration 6.** `runs.quality_level` (NULL reads as `none`), `queue.quality`, and a
  `quality_reports` table (one row per gate, result as JSON). Reports also go to
  `.jev/runs/<run_id>/quality/<T1-2|integration-1>.md` and a rebuilt `review.md`.
- **New role `code_reviewer`, not an extended `reviewer`.** Its output (findings with severity,
  category, location, checklists) and job (report, not approve/revise) differ from the
  reviewer's, and keeping both lets `[review]` and the gate be on independently. Default
  workers `["api", "codex", "claude_code"]`; read-only; no lessons.
- **No new decision point.** The code reviewer is chosen like the task reviewer (first worker
  and model, middle effort, free models kept on api, quota fallback, budget checked). The
  dispatcher sees the outcome through D5/D6 (`quality`, `quality_failed`, top 5 findings).
- **The gate is a hard limit.** A `done` (D5) or `ship` (D6) while it blocks is turned into a
  retry or a fix task, like the budget caps override the dispatcher.
- **Checks are a registry** (`QualityCheck` protocol, `CHECKS` list); contributors add a class.
  Missing tools skip (never fail a run) unless listed in `required_tools`. Tools run through the
  injectable `CommandRunner` with `[quality] timeout_s`; output redacted and clipped.
- **Judge only what changed.** File-list tools run on changed files; whole-tree tools
  (gitleaks, type checkers, trivy, checkov) only count findings in changed files, so
  pre-existing problems never block. Coverage runs only at integration; dependency audits on a
  task only when it changed a dependency file.
- **Semgrep only with local rules.** `--config auto` fetches registry rules (network, metrics),
  so it is not offline-capable; jev runs Semgrep only when `.semgrep.yml`/`.semgrep/` exists.
- **Secret scan fallback:** without gitleaks, a built-in scan of the diff's added lines reuses
  the log redactor's key patterns (`SECRET_PATTERNS`) and secret env values, plus a few
  well-known token formats.
- **Blocking policy:** basic/medium block on failing tests/lint/format and critical findings;
  production adds failing type/coverage/licence checks and major security findings; scalable
  adds major performance findings. Required tools block at every level.

## 2026-10-12: Rename jao → jev

The short name `jao` is replaced by `jev` everywhere users see it. The PyPI distribution stays
`jev-agent-orchestrator`.

- **Import package `jev_agent`, not `jev`.** The PyPI name `jev` belongs to an unrelated library
  (a TypeSafe decorator library) whose import name is `jev`; installing both would make one
  shadow the other. So the code lives in `src/jev_agent/`.
- **CLI:** `jev` (the `jev-agent-orchestrator` alias stays; the `jao` script is gone).
- **Files:** `jao.toml` → `jev.toml`, `.jao/` → `.jev/` (state.db, runs, chats, cache,
  worktrees, scheduler.lock), user config `$XDG_CONFIG_HOME/jev/config.toml` (else
  `~/.config/jev/config.toml`), shared lessons `$XDG_DATA_HOME/jev/learning.db`.
- **Git:** branches `jev/<run_id>/...`, start ref `refs/jev/<run_id>/start`, commit identity
  `jev <jev@localhost>`.
- **Env vars:** `JAO_*` → `JEV_*` (`JEV_FAKE_WORKERS`, `JEV_LIVE_TESTS`, `JEV_REDACT_NAMES`,
  `JEV_LIVE_*_MODEL`, `JEV_TEST_OPENROUTER_KEY`).
- **Coder summary block:** fenced `jev-summary` (was `jao-summary`).
- **The Jev dispatcher is unchanged:** `[dispatcher] primary = "jev"`, `[dispatcher.jev]` and
  `JevDispatcher` name the Jev model, not the tool.
- **Repos set up with v0.1.0.** When a repo has `jao.toml` or `.jao/` but neither `jev.toml` nor
  `.jev/`, every command stops and points to `jev init --migrate`. That command renames both,
  rewrites `.jao/` to `.jev/` in `.gitignore`, renames local branches `jao/*` → `jev/*` and refs
  `refs/jao/*` → `refs/jev/*` (never pushes), and rewrites the absolute log paths stored in
  `state.db` (`attempts.log_path` and its copy in `result_json`; idempotent). Worktrees that lived
  under `.jao/` are kept: after the move it runs `git worktree repair <new paths>`, which
  reconnects them, so nothing needs `git worktree prune`. A branch checked out in a worktree is
  skipped and reported (renaming it would change that worktree's HEAD under it); remove the
  worktree or rename the branch by hand. Other text that mentions old paths (task notes, chat
  transcripts) is left as written.

## 2026-10-12: `jev chat`

Code: `core/chat.py`, `cli/chat_cmds.py`, `roles/chat/prompt.md`; tests in
`tests/unit/test_chat.py` and `tests/unit/test_cli_chat.py`. User docs: [chat.md](chat.md).

- **Beyond PLAN.md**, which starts every run from a one-line idea. The chat turns a rough idea
  into a brief first; the brief's text becomes the run's idea. No new config keys: options are
  flags, and the backend reuses `[workers.api]`, `[workers.claude_code]` and
  `[roles.planner] models`.
- **Same code paths.** `/plan` and `/run` call `run_cmds.start_run` (as `jev run` and the
  scheduler do); `/queue` uses `Store.add_queue_item` (as `jev queue add` does). The approval
  gate and run budgets are unchanged.
- **Two backends behind a small `ChatBackend` protocol** so tests use fakes. The api backend
  sends the whole conversation each turn (multi-turn messages, which `ApiWorker`'s one-prompt
  job does not model). The Claude Code backend needs `--resume <session_id>`, so it builds its
  own argv (the worker's `--no-session-persistence` would make resume impossible) but reuses
  `run_command` (process group, timeout, redacted log) and the worker's result parsing and
  classification. It is read-only: `dontAsk` with Read/Glob/Grep only.
- **Learning choice has three states**: one profile, off, or the repo default (resolved at
  launch exactly as `jev run` without flags). Showing lessons never creates a profile; picking
  "with learning" on the start screen or `/learning on` creates the active profile, as
  `jev learning enable` would. Lessons are selected for the planner role.
- **Workspace profile**: same PROFILE HOOK as the scheduler. Passed to `start_run` only if its
  signature has `profile`; otherwise `/plan` and `/run` refuse rather than drop it.
- **Chat cost is separate**: checked against `--budget-usd` (default $1.00) before each model
  call, shown by `/cost`, never added to a run. A check-before-call budget can end slightly
  above the cap; Claude Code calls also get `--max-budget-usd` with what is left.
- **Persistence in files, not state.db**: `.jev/chats/<id>/{meta.json,transcript.jsonl,
  backend.log}`, redacted before writing. Chats are personal scratch work that does not need
  queries or migrations; the files are easy to read and to delete.

## 2026-10-12: Free OpenRouter models

Code: `workers/openrouter_models.py` (catalog and cache), `[models]` and
`[roles.<role>] prefer_free` in `config/schema.py`, `role_options` and D5 state in
`dispatch/requests.py`, `dispatch/rules.py`, `dispatch/jev.py`, `core/assign.py`
(`quota_fallback`, `fit_free`), `Harness.run_worker_with_fallback`, `classify_api_error`,
`cli/models_cmds.py`, doctor rows. Verified facts: [verification.md](verification.md#7-free-models-openrouter).
User docs: [config.md](config.md#free-models).

- **Free = price 0 or `:free`.** Both are documented ways a free model shows up; price-0
  models without the suffix exist today. The `openrouter/free` router is never added by
  `"free"` expansion because it picks a model at random, which makes attempts unreproducible.
- **Api worker only.** Free slugs are OpenRouter ids; Claude Code and Codex cannot take them.
  Roles without `api` ignore `prefer_free` and drop `"free"`. A dispatcher answer that pairs a
  free model with another worker is corrected by the harness (`fit_free`: move to `api`, else
  use the first paid model), like the existing "never trust the dispatcher" rule.
- **Capability filter.** Structured roles need `structured_outputs` in
  `supported_parameters` (what `response_format: json_schema` with `require_parameters` needs);
  models that list no parameters are kept and marked unknown rather than dropped.
- **Expansion at run start, from a cache.** The model list is fetched only when some role uses
  free models (and never with `JEV_FAKE_WORKERS=1`), cached for 24h, and a failed fetch falls
  back to the stale cache with a warning. Doctor never touches the network.
- **429 on a free model = `quota_exhausted`.** Free-model 429s carry no `limit_source`, and the
  per-minute and per-day limits cannot be told apart reliably, so jev does not wait: it moves
  on. Fallback chain: next free model, first paid model, one other worker. Each try is a
  recorded attempt. Run-level roles (planner, validator, learner) and the reviewer do this in
  `Harness.run_worker_with_fallback` within one step (they had no retry logic to extend);
  tasks do it through D5, where the rules dispatcher answers `retry` when
  `state.quota_fallback_model` is set, and the task runner moves to that model. In practice
  the coder never runs on `api`, so the D5 path is for completeness.
- **Escalate from free goes to paid.** "Try harder" after a free model means a paid model,
  not another free one.
- **No `models` fallback array.** OpenRouter supports it, but one request could then be served
  and billed by a model jev did not choose; explicit attempts keep cost and logs honest.
- **Prefer, not force.** With `prefer_free` free models come first and Jev is told to prefer
  them "unless the task is hard or earlier attempts failed"; Jev may still choose paid ones.

## 2026-10-12: Idea queue, scheduler and kanban board

Code: `core/queue.py`, `core/scheduler.py`, queue methods in `core/store.py` (migration 5),
`cli/{queue_cmds,scheduler_cmds,board_cmds}.py`; tests in `tests/unit/test_queue.py`,
`tests/unit/test_scheduler.py` and `tests/unit/test_cli_queue.py`. User docs:
[queue.md](queue.md).

- **Beyond PLAN.md**, which has one run per `jev run`. A `queue` table (migration 5) holds
  ideas with priority, position, the run flags (`plan_only`, `learning_profile`,
  `no_learning`, workspace `profile`) and a link to the run started for them.
- **Same code path as `jev run`.** The scheduler calls `run_cmds.start_run` with the item's
  flags; there is no second way to start a run. It runs in the foreground, one run at a time,
  with no daemon: easy to reason about, and Ctrl-C behaves like it does for `jev run`.
- **The scheduler never waits for a person.** A run that stops at `AWAITING_APPROVAL` or
  `NEEDS_HUMAN` leaves its item `waiting` and the scheduler moves on. The plan approval gate is
  unchanged (`require_plan_approval` still applies to queued runs).
- **Status follows the run.** Once an item has a run, its shown status is derived from the
  run's state at read time, so approving or resuming a run later is reflected without the
  queue being updated. The stored status is only the scheduler's last word.
- **Interrupts leave runs resumable** rather than finishing the current worker step: the run
  is interrupted where it is (as with `jev run`), its item becomes `waiting` with a
  `jev resume` note, and nothing restarts it automatically. The scheduler finds the run it
  started by idea text and start time when `start_run` did not return.
- **Lock file, not a database row**: `.jev/scheduler.lock` with pid and host, taken with
  `O_CREAT | O_EXCL`; a dead pid on this host (or an unreadable file) is stale and taken over;
  another host's lock is respected because its pid cannot be checked.
- **Daily budget** counts every run created today (UTC), not only queue runs, and is checked
  between runs, so it can be overshot by up to one run's cap.
- **No config schema changes**: scheduler settings are command flags.
- **Workspace profiles**: the item's `profile` is stored and passed to `start_run` as
  `profile=` when that function accepts it (`scheduler_cmds.apply_profile`, the hook for the
  profile feature). Until then an item naming a profile fails with a note instead of running
  with access it did not ask for.
- **Board**: `build_board` is a pure function (tested without rendering); columns side by side
  from 140 columns, stacked panels below. Done shows the last 10 runs unless `--all`.

## 2026-10-12: Recursive learning (decision point D7, learner role)

Code: `core/learning.py` (profiles, lessons, selection, `resolve_learning`), `core/learner.py`
(scoring, D7, applying the learner's answer), `roles/learner/prompt.md`, `cli/learning_cmds.py`;
tests in `tests/unit/test_learning.py`, `tests/unit/test_cli_learning.py` and
`tests/integration/test_learning_pipeline.py`. User docs: [learning.md](learning.md).

- **Beyond PLAN.md.** PLAN.md has four roles and decision points D1-D6. Learning adds a fifth,
  read-only role, `learner`, and **D7** at the end of a run (DONE, FAILED, NEEDS_HUMAN,
  BUDGET_EXCEEDED; not CANCELLED): `learn` in {learn, skip} plus the learner's worker, model
  and effort. The rules dispatcher learns when the run had a plan revision, retry, escalation,
  reassignment, human hand-off, fix task, failure or NEEDS_HUMAN stop, or on a clean DONE
  while the profile has fewer than 3 lessons. Jev gets D7 in its instruction tables.
- **Off by default.** Learning costs an extra model call per run that has something to teach
  and sends lesson text to the workers, so it is opt-in: `[learning] enabled = false`.
  Precedence: run flag (`--learning NAME` / `--no-learning`) > repo settings stored by
  `jev learning` in a new `settings` table of state.db > `jev.toml` > defaults. A run records
  its profile (`runs.learning_profile`), so `resume` keeps it.
- **Profiles and scopes.** Repo-scope profiles live in state.db (migration 4); shared ones in
  `$XDG_DATA_HOME/jev/learning.db` (its own one-step migration list, same table SQL). Lesson
  ids carry the scope (`L12`, `S3`); names are unique across scopes. The shared file is only
  created when a shared profile is written.
- **Selection without embeddings.** Lessons for the role, not retired, ranked by tag overlap
  with the task card (files and title) and the idea, then score, then recency; capped by
  `max_lessons` and `max_chars`. Simple, deterministic and explainable from `jev log`, which
  lists the lesson ids injected into each attempt (`attempts.lesson_ids`).
- **Scoring** is harness-side and deterministic: per finished task, lessons in its coder or
  reviewer attempts get `uses + 1`, `score + 1` when the task was DONE on the first attempt,
  else `- 0.5`; a lesson at score -2 or below is retired. Planner/validator injections are not
  scored (no per-task outcome to attribute). Each task is scored once (`tasks.lessons_scored`).
- **The learner never sees transcripts**: only a compact run card (counts, titles, D5 actions,
  issues, short redacted failure excerpts) and up to 30 relevant lessons with ids. Its answer is
  limited (5 adds, 10 updates, 300 characters, valid roles), redacted, and may only update or
  retire lessons it was shown. The schema is lenient (plain strings) so one sloppy field does
  not void the reply; the limits are enforced when storing.
- **Never changes the run's outcome.** Every problem in the learning step (no learner worker,
  per-run budget spent, invalid reply, worker error) is recorded as `skipped: ...` in
  `runs.learning_outcome`. A resumed NEEDS_HUMAN run learns again when it ends.

## 2026-10-12: Agent tools per role (MCP servers, skills, plugins)

Code: `config/schema.py` (`ToolsConfig`), `workers/tools.py` (`AgentTools`, `tools_for_role`),
`workers/{claude_code,codex}.py`, `cli/doctor_cmd.py`; tests in `tests/unit/test_agent_tools.py`.
Findings and citations: [verification.md](verification.md) section 6.

- **Config, not code.** `[tools.mcp.<name>]`, `[tools.skills]` and `[tools.plugins.<name>]`
  define tools once; `[roles.<role>] mcp/skills/plugins` name them. Unknown names fail config
  validation. Secrets are only ever env var names (`env_from`, `bearer_token_env`,
  `headers_from_env`); literal `env`/`headers` and credential-looking args/URLs are rejected.
  Keys under the name tables (and header names) are exempt from the secret-key-name check.
- **`WorkerJob.tools`** carries the role's resolved tools (paths made absolute against the repo)
  from `Harness.run_worker` to every worker. Empty tools leave argv exactly as before.
- **Claude Code:** per-attempt `--mcp-config <tmp>/mcp.json --strict-mcp-config`, `mcp__<server>`
  added to `--allowedTools` (read-only roles included: the user opted in per role), skills copied
  under `<tmp>/skills/.claude/skills/` and passed with `--add-dir` (no per-skill flag exists),
  `Skill(<name>)` allowed, plugins via `--plugin-dir` plus their configured `allowed_tools`.
  Env values are written into the 0600 temp file because `${VAR}` expansion is only documented
  for `.mcp.json`; switch once verified.
- **Codex:** `-c mcp_servers.<name>.<key>=<TOML>` overrides with variable names only
  (`env_vars`, `bearer_token_env_var`, `env_http_headers`). Skills, plugins and SSE servers have
  no per-run form in Codex and are left out with a log note.
- **API worker:** no tools in this change (future work).
- **Redaction by name:** workers set `JEV_REDACT_NAMES` to the MCP variables, and
  `runlog.redact` scrubs those values (4+ chars) whatever the variables are called.
- **Doctor:** a row per role with tools, notes for tools a role's workers cannot use and for
  read-only roles with MCP servers, a warning per MCP server with an unset variable or a
  `command` not on PATH, a failure for a missing skill or plugin path.

## 2026-10-12: Live smoke test

Run on `examples/sample-repo` (copied to its own git repo) with Jev via native TypeSafe
(`jev-latest` resolving to `jev-1.13.0`), Claude Code 2.1.296 as planner and coder, Codex 0.162.1
as validator, the API worker disabled: `jev run --plan-only "Add a word_frequencies(text)
function with tests"`, then `jev approve`.

- The run reached DONE for $1.11 reported cost: planner $0.62, coder $0.49; the Codex validator
  reported no cost (as documented).
- Jev answered D1 (`clear=plan` 0.98, `effort=low` 0.94), D2 (`effort=low` 0.59), D3 (`approve`
  1.00), D5 (`done` 0.95) and D6 (`ship` 1.00). At D4 Jev's confidence was 0.42 < 0.55
  (`min_confidence`), so the rules dispatcher decided: `complexity=hard`, `effort=high`.
- The result branch had `word_frequencies` plus 5 new tests; all 10 tests passed. The user's
  branch was untouched and the worktrees were cleaned up.
- **Follow-up fixed:** the rules D4 heuristic rated this one-function task "hard" because it
  treated 5+ acceptance criteria as hard on their own, and planners routinely write 5-6
  criteria for small tasks. Now files are the main signal: hard needs 4+ files or 8+ criteria,
  trivial is at most 1 file and 2 criteria, everything else is standard
  (`dispatch/rules.py::complexity`, cases in `tests/unit/test_rules.py`). A two-file task with
  six criteria is now "standard"/medium.

## 2026-10-12: v0.1.0 polish (prompt errors, UTC times, Codex cost)

- **Bad prompt templates stop the run in NEEDS_HUMAN, resumably.** A missing or unreadable
  `prompt_file`, a Jinja2 syntax error, or any error while rendering (e.g. `UndefinedError`) is
  raised as `roles.PromptError` and turned into a NEEDS_HUMAN halt in one place
  (`Harness.prompt` / `Harness.check_prompts`); the reason names the role and the file.
  Previously a raw traceback escaped and left the run in PLANNING. Every custom template is
  loaded and parsed before each step, so missing files and syntax errors cost nothing; the coder
  prompt is rendered before the attempt is counted. Store schema v3 adds `runs.resume_state`:
  the state such a halt came from. `resume` accepts a NEEDS_HUMAN run only when it is set, and
  continues from there; every other NEEDS_HUMAN stop stays final (see "Not resumable yet"
  below). `jev doctor` fails a `prompt_file` that is missing or does not parse (undefined
  variables need a render, so they are only found at run time).
- **Times are shown in UTC.** Run ids were UTC but `jev status`/`jev log` printed local time.
  Both now print UTC with a `Z` suffix (`2026-10-12 15:30:12Z`), matching the run id.
- **Codex and the cost caps.** Codex reports no cost, so its attempts count $0 and the budget
  caps never stop them; they are bounded by `max_task_attempts` and `timeout_s` only.
  docs/security.md and the README say so, and `jev doctor` shows a `note` row when Codex is
  usable by some role.
- **Live tests** read only `JEV_LIVE_TESTS=1` (the old `BATON_LIVE_TESTS` alias is gone).

## 2026-10-12: M6 pipeline (dependent bases, integration, fix tasks, cleanup)

Code: `core/{workspace,integration,pipeline,tasks}.py`, `dispatch/{requests,rules}.py`; tests in
`tests/integration/test_integration_step.py`. Supersedes the M2 entry's "branches from HEAD"
and "fix_task -> NEEDS_HUMAN" points.

- **Start commit.** When a run first enters EXECUTING, HEAD is recorded as the git ref
  `refs/jev/<run_id>/start`, and every branch of the run grows from it, so resume rebuilds
  identical branches even if HEAD moved. The ref is the record (no store change) and is kept
  after cleanup (`git diff refs/jev/<id>/start jev/<id>/result`).
- **Dependent tasks** fork from `jev/<run_id>/base-<task>`: the start commit with the dependency
  branches merged in dependency order, built in a temporary worktree and removed straight after.
  A conflict leaves no base branch, sets the task NEEDS_HUMAN (naming the files) and stops the run
  in NEEDS_HUMAN before an attempt is spent. The reviewer's diff is relative to that base.
- **Integration.** Task branches merge in dependency order into `jev/<run_id>/result`, rebuilt
  from the start commit each time; then each distinct task `test_command`, then
  `[integration] test_command`; the first failure stops the tests.
- **D6** state gains `fix_rounds` and `max_fix_rounds`. Rules: conflicts -> `human`; failed
  tests -> `fix_task` while rounds remain, else `human`; otherwise `ship`. The harness enforces
  the bound and treats `fix_task` without a test failure as `human`. Jev's D6 instruction says
  the same.
- **Fix tasks.** `fix_task` appends `T<max+1>` "Fix integration failures (round k)", depending on
  every earlier task, with the failing command as its `test_command` and the last 2000 chars of
  its output (redacted) in the description. Rounds are counted from those titles, so a crash
  cannot double-count. The plan is written with `set_run_plan` (same `plan_round`) then
  `upsert_tasks`; an unfinished task seen at integration sends the run back to EXECUTING.
- **Cleanup on DONE** removes the run's worktrees (`git worktree remove --force`, then `prune`)
  and base branches; task and result branches stay. Other end states keep everything. Cleanup
  is idempotent, rejects unsafe run ids, only touches `<state_dir>/worktrees/`, and a failure is
  appended to the DONE reason rather than failing the run.
- **Config.** `[integration] test_command` (default none) and `max_fix_rounds` (default 2), and
  `[budget] test_timeout_s` (default 1200, per test command), reach `PipelineSettings` via
  `cli/wiring.py`. `test_timeout_s` sits in `[budget]` with the other limits because it applies
  to task tests too, not only integration.

## 2026-10-12: Jev and API wiring

Folds in the M3 (API worker), M4 (Jev) and M5 (Claude Code and Codex workers) notes. Code:
`dispatch/jev.py`, `workers/{api,claude_code,codex,subprocess_runner,summary}.py`,
`cli/wiring.py`; config: `[dispatcher.jev]`, `[workers.*]` (docs/config.md). Permission advice
and terms of use are in docs/security.md.

**Jev dispatcher**

- **Slug fix.** PLAN.md's `jev_model = "typesafe/jev-latest"` was wrong (missing `~`). The key
  is replaced by a `[dispatcher.jev]` table that maps 1:1 onto `JevSettings`, defined once in
  `config/schema.py`: `base_url`, `model = "~typesafe/jev-latest"`, `api_key_env`,
  `timeout_s = 30`, `min_confidence = 0.55`, optional `instructions`.
- **OpenRouter is the default path**; native TypeSafe (`https://api.typesafe.ai/v1`,
  `jev-latest`, `TYPESAFE_API_KEY`) takes the same request shape (`JevSettings.for_native()`).
- **System One endpoint, not chat completions.** `POST {base_url}/systemone` with
  `{state, model, questions}` (docs/verification.md section 1). `state` is
  `{"decision_point": {id, description}, **request.state}`.
- **Allowed answers as option -> description maps.** Every question is a `choice` whose
  `criteria` keys are exactly the allowed answers, each mapped to a short description for known
  values (efforts, complexity tiers, workers, D3/D5/D6 actions) or `null` (model slugs, anything
  unknown). Instructions: override `"D4.effort"`, override `"effort"`, built-in per point, built-in
  per question, generic sentence. `effort`/`complexity` are `choice`, not `score`; revisit if
  choices on ordered scales look poor. Pools over 255 options would get a 422 (falls back).
- **Single-option questions** are answered locally; if all are, no call is made and no key is
  needed.
- **Every problem raises `DispatchError`** and the guard falls back to rules for that decision:
  key unset; `state` + longest question over 32k tokens (estimated as chars / 3); 401/403 (no
  body echoed); 422 (server message, <= 300 chars); 429/529/5xx after one retry
  (`Retry-After` capped at 10 s, default 1 s); other statuses (incl. 402); timeout or network
  error (no httpx text); missing or disallowed answer; missing confidence. The key never appears
  in messages; error bodies have it replaced by `***`. The error body schema is unverified; the
  parser accepts `{"error": {"message"}}`, `{"error": "..."}`, `{"detail"}` or plain text.
- **Confidence gate.** If any answer's confidence is below `min_confidence`, the whole decision
  falls back (all-or-nothing; a partial merge would need the guard to combine two decisions).
- **`Decision.details`** (new, default `{}`) replaces `JevDispatcher.last_details`, which was not
  safe under concurrency. Jev stores `direct` (locally answered questions), `answers`
  (`{q: {choice, confidence, probabilities}}`) and `usage`. It is persisted inside
  `decision_json`, so no schema migration; `jev log` prints `effort=high (0.91)`. A rejected
  low-confidence decision keeps its confidences only in the fallback reason.
- **Cost** is `usage.cost` (OpenRouter), else `usage.input_tokens` x $0.042/M (Jev 1.13 list
  price, input only; native TypeSafe never returns a cost, so it is an estimate).
  `Decision.model` is the versioned id from the response.
- **Wiring.** `primary = "jev"` builds a `JevDispatcher` even when its key is unset; the guard
  falls back per decision and `jev run`/`approve`/`resume` print one warning. `fallback` is
  always rules for now. `jev doctor` shows the dispatcher, model, URL and whether the key
  variable is set (never its value), warning if not.

**API worker**

- **Endpoint.** `POST {base_url}/chat/completions` (OpenAI-compatible, default OpenRouter), one
  user message with the rendered prompt, key from `api_key_env` sent only as a Bearer header.
  Attribution headers `HTTP-Referer: PROJECT_URL` and `X-OpenRouter-Title`. `PROJECT_URL`
  (`https://github.com/Gane2506/jev-agent-orchestrator`) is a guess; fix it once the repo is public.
- **Roles and models.** Refuses `coder` (status `error`). Model is `job.model`, else
  `[workers.api] default_model` (new, unset by default), else status `error`. New
  `[workers.api] timeout_s = 600` bounds a whole attempt, retries included. Registered in the
  CLI whenever enabled; the M5 CLI workers plug in via `wiring.build_cli_workers` (empty for now).
- **Structured output.** `response_format: json_schema` with `strict: true` plus
  `provider: {require_parameters: true}`. The schema is post-processed: every object gets
  `additionalProperties: false`, `required` lists all properties, `default` is dropped, so
  defaulted fields are required on the wire; pydantic then validates against the real model
  (cross-field validators still apply). Other keywords (`pattern`, `minItems`) are kept; some
  providers may reject them under strict mode (unverified). Replies are parsed as JSON, else
  from the first fenced json block. The verification.md fallback to prompt-and-extract on a
  400 "unsupported parameter" is not implemented.
- **Effort** `low|medium|high` maps to `reasoning: {effort}` (other values not sent). Cost is
  `usage.cost`, tokens are `prompt/completion/total`; cost is kept even when validation fails.
- **Errors** (`classify_api_error`): 402 -> `quota_exhausted`; 429 with `limit_source` or quota
  wording (credit, insufficient, exhaust, spend/key limit, budget, per-day) -> `quota_exhausted`,
  other 429 -> `failure`; 403 with a guardrail/workspace/key budget `limit_source` ->
  `quota_exhausted` (deliberate deviation from "403 -> error"); other 401/403 -> `error`;
  408 -> `timeout`; 5xx -> `failure`; other 4xx -> `error`. Timeouts -> `timeout`, other httpx
  errors and a missing key -> `error`.
- **Retry** once, only for 429/503 classified `failure`, after `Retry-After` (<= 10 s) or 2 s;
  a longer or HTTP-date `Retry-After` means no retry.
- **Logging and redaction.** A sectioned log at `job.log_path` (request metadata, prompt, HTTP
  status per attempt, error bodies, usage, content, result; never headers). The log and every
  result summary are scrubbed of the literal key value (the variable may have any name) and then
  passed through `jev_agent.runlog.redact.redact`, now imported directly (the importlib shim and
  duplicate local regex are gone).
- **Live test** `tests/live/test_api_live.py` needs `JEV_LIVE_TESTS=1` (or `BATON_LIVE_TESTS=1`)
  and `OPENROUTER_API_KEY`; model `JEV_LIVE_API_MODEL`, default `openai/gpt-4o-mini`.

**Claude Code and Codex workers** (flags per docs/verification.md sections 2 and 3; Claude Code
2.1.114, codex-cli 0.162.1)

- **Subprocess runner.** `run_command` uses `create_subprocess_exec`, never a shell; the child
  gets its own process group, which gets SIGTERM then SIGKILL (5 s) on timeout. Missing or
  non-executable binaries return 127/126 instead of raising. The log is appended (agent call and
  `git status` share one file): argv, cwd, stdin, status, stdout, stderr, redacted; never the
  environment. The parent environment is passed through (the CLIs need HOME, PATH, their auth).
  Workers take an injectable `CommandRunner`.
- **Redaction** now calls `jev_agent.runlog.redact.redact` directly with `os.environ` merged with the
  child env; the lazy import and local regex are gone. To keep M5's coverage, runlog's
  secret-name rule widened from `_API_KEY$|TOKEN` to `KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL`.
- **Coder summary.** The coder ends with a fenced `jev-summary` JSON block (last one wins, via
  `roles.schemas.extract_coder_summary`); missing or invalid -> `error`. `files_changed` comes
  from `git status --porcelain --untracked-files=all` (renames, C-quoted paths), falling back to
  the block's list. Coder jobs ignore `output_schema`.
- **Claude Code** (`claude -p --output-format json --no-session-persistence`, prompt on stdin):
  coder `--permission-mode acceptEdits --allowedTools "Read Edit Write Glob Grep Bash(git status *)
  Bash(git diff *)"`; other roles `dontAsk`, read tools only, `--disallowedTools "Edit Write
  NotebookEdit"` and `--json-schema`. `plan` mode was rejected (its exit-plan flow targets
  interactive use). Effort outside low..max is dropped and logged. `structured_output` is
  validated, else the `result` text is parsed. `total_cost_usd` is an estimate (not real spend on
  subscriptions). Classification, in order: timeout; success; quota (`You've hit your ... limit`,
  `Credit balance is too low`, `Request rejected (429)`, `credits_required`, rejected
  `rate_limit_event`, `api_retry` rate_limit/billing_error); auth -> `error`;
  `error_max_structured_output_retries` -> `error`; `error_max_turns`, `error_max_budget_usd`,
  `error_during_execution`, other errors or non-zero exit -> `failure`; exit 0 without a result
  -> `error`. `error_max_budget_usd` is `failure`, not quota: it is our own cap, so reassigning
  would be wrong.
- **Codex** (`codex exec --json --ephemeral -C <workdir> -s <sandbox> -c approval_policy="never"
  ... -o <tmp>/last_message.txt -`): coder uses the configured sandbox, other roles `read-only`
  with `--output-schema` (strict form via `api.strict_json_schema`). `--skip-git-repo-check` only
  outside a git repo. Temp files live outside the workdir. Final text is the `-o` file, else the
  last `agent_message`; tokens are summed over `turn.completed`; `cost_usd` is always None.
  Classification: timeout; exit 0 with `turn.completed` and no `turn.failed` -> success; usage
  limit / `usage_limit_reached` / out of credits / spend cap / `insufficient_quota` -> quota; not
  logged in / 401 / invalid key -> `error`; `turn.failed`, `error` event or non-zero exit ->
  `failure`; exit 0 without a completed turn -> `error`.
- **Config and wiring.** `[workers.claude_code]` gains `permission_mode`,
  `read_only_permission_mode`, `allowed_tools`, `max_budget_usd`, `bare`, `extra_args` and
  `skip_permissions`; `[workers.codex]` gains `sandbox` and `extra_args`. `bypassPermissions`
  is not accepted as a `permission_mode`: the only way to it is the DANGEROUS, opt-in
  `skip_permissions`, which `jev doctor` warns about (as it does for
  `sandbox = "danger-full-access"` and "dangerous" `extra_args`). `max_budget_usd` defaults to
  `[budget] per_task_usd`, since `WorkerJob` has no budget field. Both workers are registered
  when enabled; `timeout_s` reaches `WorkerJob` through the harness.
- **Unverified.** Exit codes on quota/auth/turn failures are undocumented, so classification
  relies on text and events. Where Codex puts limit messages is inferred from source; auth
  strings for both CLIs are best guesses. Before 2.1.205 an invalid `--json-schema` was silently
  ignored (we fall back to parsing text). `--allowedTools` is one space-separated argument and
  relies on the CLI parsing `Bash(git status *)`. Fixtures are synthesized from docs; recapture
  them with `tests/live/test_cli_workers_live.py`.

## 2026-10-12: M2 pipeline (state machine, rules dispatcher, fake worker)

- **Worker interface.** `Worker.run` returns `WorkerOutput(result, parsed)` rather than a bare
  `WorkerResult`: `parsed` carries the validated structured output (a `Plan`, `ValidationResult`,
  `ReviewResult` or `CoderSummary`) so the harness never re-parses text. `WorkerJob` carries
  `log_path` (the harness decides where logs go, `.jev/runs/<run_id>/<task_id|run>/<attempt>.log`)
  plus `task_id` and `attempt` for logging and for scripting fakes. If a JSON role reports
  success without valid parsed output, the harness records it as `error`.
- **Reviewer stage is opt-in** (`[review] enabled = false`), answering PLAN.md section 17. No
  decision point chooses the reviewer, so it uses the role's first worker and model and its middle
  effort. Revisit when the reviewer becomes a standard stage (v0.3).
- **Question names** for D1-D6 are fixed in `dispatch/requests.py`. An empty model pool (or
  effort list) is offered as the single answer `"default"`: let the worker use its default.
- **Planner and validator failures** (no valid plan or verdict) stop the run in NEEDS_HUMAN, as
  do D1 `ask_human`, D3 `escalate_human` and running out of plan rounds. A dependency cycle in a
  plan stops the run in FAILED.
- **Budgets.** No worker is launched once the run or task has spent its cap; work already
  running finishes, so spend can end slightly above a cap. Run cap -> BUDGET_EXCEEDED. Task cap
  -> task FAILED and run FAILED. Task spend is added to the run total in one transaction
  (`Store.add_spend`), and dispatcher costs count too.
- **Attempts.** `max_task_attempts` counts every started attempt, including one interrupted by a
  crash. On `resume`, unfinished attempts are closed as `error` ("interrupted") and the task's
  current attempt is run again as a new attempt. Retry keeps the assignment; escalate raises the
  effort one level, then moves to the next model; reassign moves to the next worker in the role's
  list. Run-level attempts (planning, validation) share one counter per run.
- **Task branches** are created from the repo's HEAD, not from their dependencies' branches, as
  PLAN.md section 10 says. Dependent tasks therefore do not see each other's changes until
  integration. Revisit in M5/M6 (branching from the merged dependencies is the likely fix).
- **Integration (minimal).** After all tasks are DONE, task branches are merged in dependency
  order into a fresh `jev/<run_id>/result` branch (in a worktree under `.jev/worktrees/`), each
  distinct task test command runs there, and D6 is asked. `ship` -> DONE; `fix_task` and
  `human` -> NEEDS_HUMAN for now (automatic fix tasks are M6 work). jev commits with a fixed
  local identity (`jev <jev@localhost>`, unsigned). Nothing is pushed.
- **Store schema v2** adds `runs.reason`, `runs.validation_json`, and `tasks.worker/model/effort/
  note` so status shows why a run stopped and resume knows each task's next assignment.
- **Partial role tables keep role defaults.** Previously `[roles.coder] models = [...]` alone
  silently reset `workers` to `[]`; per-role defaults are now layered under each role table.
- **CLI in M2** uses the rules dispatcher and has no real workers; `JEV_FAKE_WORKERS=1` is a
  documented test hook that registers fakes. `jev approve` without workers only marks the plan
  approved (state EXECUTING); `jev resume` then continues once workers exist.
- **Not resumable yet:** NEEDS_HUMAN, FAILED and BUDGET_EXCEEDED runs are terminal; `resume`
  refuses them (raising a cap and resuming is future work). Exception since v0.1.0: a
  NEEDS_HUMAN stop caused by a bad prompt template (see the v0.1.0 polish entry).

## 2026-10-12: Config precedence and doctor severity

- PLAN.md section 8 says `jev.toml` is "overridable by" the user config, which reads as the user
  file winning. We chose the opposite, the usual convention: built-in defaults < user config
  (`$XDG_CONFIG_HOME/jev/config.toml`, else `~/.config/jev/config.toml`) < repo `jev.toml`.
  The repo file is the most specific, so it wins. Tables are deep-merged; lists and scalars
  are replaced whole.
- Workers are fixed fields (`claude_code`, `codex`, `api`) rather than a free-form table, so a
  misspelled worker table is an error. Roles may only name these workers.
- `jev doctor` reports a missing worker CLI or unset API key as a warning, and fails only when a
  role has no usable worker (or git/config checks fail). Default config enables both CLIs and
  most users will have only some of them.

## 2026-10-12: Project name

*Superseded by the "Rename jao → jev" entry above; kept as written for history.*

- `baton` is taken on PyPI. The project is named **Jev Agent Orchestrator**.
- PyPI distribution: `jev-agent-orchestrator`.
- Import package: `jao`.
- CLI: `jao` (short), with `jev-agent-orchestrator` as an alias pointing to the same entry point.
- Config file `jao.toml`, state folder `.jao/`, user config `~/.config/jao/config.toml`,
  branches `jao/<run_id>/<task_id>`. Everywhere PLAN.md says `baton`, read `jao`.

## 2026-10-12: Run-log package name

PLAN.md's layout has a `logging/` package. It is named `runlog/` instead so it can never be
confused with, or shadow, the standard library `logging` module.

## 2026-10-12: License

Apache-2.0, as PLAN.md proposed. Confirmed with the maintainer.
