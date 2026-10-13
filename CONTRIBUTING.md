# Contributing

Thanks for helping. This file covers how to find something to work on, how to set up, the
layout of the code, the rules the design depends on, the checks CI runs, and what a pull
request needs to be merged.

If you only read one section, read [Before you open a pull request](#before-you-open-a-pull-request):
most failed pull requests so far skipped `uv run ruff format .` or `uv run mypy`.

## Contents

1. [Ways to contribute](#ways-to-contribute)
2. [Finding work](#finding-work)
3. [Setup](#setup)
4. [Project layout](#project-layout)
5. [Code-quality bar](#code-quality-bar)
6. [Changes written with a coding agent](#changes-written-with-a-coding-agent)
7. [Code style](#code-style)
8. [Testing](#testing)
9. [Docs that change with the code](#docs-that-change-with-the-code)
10. [Adding a worker adapter](#adding-a-worker-adapter)
11. [Changing a role prompt](#changing-a-role-prompt)
12. [Recording decisions](#recording-decisions)
13. [Branches and commits](#branches-and-commits)
14. [Before you open a pull request](#before-you-open-a-pull-request)
15. [Pull requests and review](#pull-requests-and-review)
16. [Security](#security)
17. [License](#license)
18. [Code of conduct](#code-of-conduct)

## Ways to contribute

- **Report a bug** with the bug report form. Include `jev --version`, your OS and Python
  version, the command you ran, the relevant part of `jev.toml` and the output or run log
  (`jev log <run_id>`). Run logs are redacted, but read them before you paste them.
- **Suggest a feature** with the feature request form. Describe the problem first; a proposed
  design is welcome but optional.
- **Ask a question** in [GitHub Discussions](https://github.com/Gane2506/baton/discussions)
  rather than in an issue.
- **Fix a bug or build a feature** from an open issue (see [Finding work](#finding-work)).
- **Improve the docs**: `README.md`, `docs/`, this file, or a role prompt
  ([Changing a role prompt](#changing-a-role-prompt) needs no Python).
- **Try jev on a real repository** and report what broke or confused you. Several features
  have never run against real services yet (the OpenRouter API worker and free models, MCP
  tools in real Claude Code or Codex runs, `jev chat` with real backends, the quality gate with
  real scanners, server tools over real SSH, `jev ship` against a real GitHub repository), so
  a careful report from a real run is valuable.

Security problems are the exception: do not open a public issue. See [Security](#security).

## Finding work

Open issues are at https://github.com/Gane2506/baton/issues. The labels that matter for
choosing work:

| Label | Meaning |
| --- | --- |
| `good first issue` | Small and well described; a good way to learn the codebase. |
| `help wanted` | The maintainer would welcome an outside pull request. |
| `priority: next` | Planned for the near term. |
| `priority: medium` | Wanted, not urgent. |
| `priority: low` | Nice to have. |
| `needs decision` | The design is not settled. Comment with your view; do not start code yet. |
| `in progress` | Someone is working on it. Pick another issue, or ask on it first. |
| `bug`, `enhancement`, `documentation`, `security` | The kind of change. |

How to take an issue:

1. **Comment on the issue** to say you want to work on it, with a short plan if the change is
   more than a few lines. Wait for a reply on anything labelled `needs decision` or anything
   that changes config keys, the state database, the CLI or a role's output schema.
2. **One issue per pull request.** If you find a second problem on the way, open an issue for
   it instead of folding it in.
3. **No issue yet?** For a typo or a one-line fix, just open the pull request. For anything
   larger, open an issue first so nobody spends a weekend on a change that will not be merged.
4. If you stop working on an issue, say so on it so someone else can pick it up.

## Setup

You need Python 3.11+, git and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/<you>/baton    # your fork
cd baton
uv sync --all-extras    # creates .venv with the package, the dashboard extra and dev tools
uv run jev --version
uv run pytest -q
```

You do not need Claude Code, Codex or any API key to develop or run the test suite. The
`--all-extras` flag matters: without the `dashboard` extra, the dashboard tests are skipped
locally but still run in CI.

To try the CLI end to end without agents, use the sample repo with fake workers (see
[examples/sample-repo/README.md](examples/sample-repo/README.md)). Copy it outside the source
tree, so jev's branches and worktrees never touch it:

```bash
cp -R examples/sample-repo /tmp/textstats && cd /tmp/textstats
git init -q && git add . && git commit -qm init
uv run --project /path/to/baton jev init
# optional: set [dispatcher] primary = "rules" in jev.toml to skip the Jev dispatcher's
# "API key is not set" warnings (without a key, jev falls back to the rules dispatcher anyway)
JEV_FAKE_WORKERS=1 uv run --project /path/to/baton jev run --plan-only "Add a word_frequencies() function"
```

`JEV_FAKE_WORKERS=1` replaces every enabled worker with a scripted fake, so nothing calls a
model or an agent CLI.

## Project layout

```
src/jev_agent/
  cli/        typer commands, one module per command group; wiring.py builds the pipeline
  config/     config schema, layered loading, the `jev init` template
  core/       models, run and task state machines, store, budgets, git worktrees, learning,
              queue and scheduler, chat, ship and KPIs
  dashboard/  `jev dashboard`: Starlette app, Jinja2 templates, static CSS/JS (optional extra)
  dispatch/   dispatcher interface, Jev and rules dispatchers, answer guard
  quality/    the optional quality and security gate: levels, checks, AI review, reports
  roles/      prompt templates and output schemas, one folder per role
  runlog/     run log files and secret redaction
  servers/    servers over SSH, access policy, audit log, the stdio MCP server, browser, profiles
  workers/    worker interface and adapters (Claude Code, Codex, OpenRouter API, fake)
tests/
  unit/         one module or function at a time
  integration/  whole pipeline with fakes against a temporary git repo
  fixtures/     recorded or synthesized outputs of Claude Code, Codex, gh, Jev and OpenRouter
  live/         real calls, skipped unless JEV_LIVE_TESTS=1
docs/         user docs per feature, config reference, security, decisions, verified API facts
examples/     sample config, sample repo, demo data and screenshot scripts (examples/demo/)
```

Module by module (every file under `src/jev_agent/`, except package `__init__.py` files):

| Module | Purpose |
| --- | --- |
| `__main__.py` | `python -m jev_agent`: the same CLI as `jev` (used to start `jev mcp servers` for agents). |
| **cli/** | |
| `cli/app.py` | Entry point; registers the commands. |
| `cli/wiring.py` | Build the dispatcher, workers and pipeline from config; the `JEV_FAKE_WORKERS` hook. |
| `cli/repo.py` | Find the target git repository. |
| `cli/text.py` | Small text helpers shared by the terminal commands and the dashboard. |
| `cli/init_cmd.py` | `jev init`: write `jev.toml`, create `.jev/`, update `.gitignore`. |
| `cli/migrate.py` | `jev init --migrate`: move a repo set up with jao v0.1 to the jev names. |
| `cli/doctor_cmd.py` | `jev doctor`: check git, config, dispatcher, worker CLIs, keys and agent tools. |
| `cli/run_cmds.py` | `jev run`, `approve`, `resume`, `cancel`. |
| `cli/status_cmds.py` | `jev status` and `jev log` (read-only views of the store). |
| `cli/chat_cmds.py` | `jev chat`: talk an idea through, then plan, run or queue it. |
| `cli/queue_cmds.py` | `jev queue`: add, list, reorder and remove queued ideas. |
| `cli/scheduler_cmds.py` | `jev scheduler start\|status`: work through the queue unattended. |
| `cli/board_cmds.py` | `jev board`: a kanban view of the queue and every run. |
| `cli/learning_cmds.py` | `jev learning`: manage learning profiles and their lessons. |
| `cli/quality_cmds.py` | `jev quality show` and `jev quality checks`. |
| `cli/models_cmds.py` | `jev models`: the OpenRouter model catalog, including free models. |
| `cli/server_cmds.py` | `jev mcp servers`, `jev browser install`, `jev profile list\|show\|use`. |
| `cli/ship_cmds.py` | `jev ship`, `jev ship sync` and `jev kpi`. |
| `cli/dashboard_cmd.py` | `jev dashboard`: check the extra is installed, pick a token, start uvicorn. |
| **config/** | |
| `config/schema.py` | Pydantic config schema. Unknown keys and secret-looking keys are errors. |
| `config/servers_schema.py` | Config models for servers, the virtual browser and workspace profiles. |
| `config/loader.py` | Layering: defaults < user config < repo `jev.toml`. |
| `config/defaults.py` | File names and the commented `jev.toml` template that `jev init` writes. |
| **core/** | |
| `core/models.py` | Shared data models: `Idea`, `TaskCard`, `Plan`, `WorkerResult`, `DecisionRequest`, `Decision`, states. |
| `core/pipeline.py` | The run state machine, from intake to DONE. |
| `core/tasks.py` | The per-task state machine: code, test, review, D5. |
| `core/integration.py` | Merge task branches, run tests, D6, fix tasks. |
| `core/harness.py` | Shared plumbing: ask the dispatcher, launch workers, render prompts, charge costs. |
| `core/assign.py` | Worker assignments and how retry, escalate and reassign change them. |
| `core/budget.py` | Hard cost caps. |
| `core/taskgraph.py` | Dependency order of tasks. |
| `core/workspace.py` | Git worktrees and branches for tasks and integration. |
| `core/commands.py` | Run shell commands such as test commands, behind a fakeable interface. |
| `core/store.py` | SQLite store (`.jev/state.db`): runs, tasks, attempts, decisions, learning, queue, quality reports, shipped PRs. |
| `core/migrations.py` | Store schema migrations (and the shared learning database's). |
| `core/learning.py` | Learning profiles and lessons (repo and shared scope), lesson selection, which profile a run uses. |
| `core/learner.py` | The end-of-run learning step: scoring, D7, the learner role, applying its answer. |
| `core/queue.py` | The idea queue: ordering, moves and the status of an item. |
| `core/scheduler.py` | The scheduler: work through the queue one run at a time. |
| `core/lease.py` | The run lease: at most one process drives a run at a time. |
| `core/chat.py` | `jev chat` sessions: talk an idea through with a model, then launch a run from the brief. |
| `core/ship.py` | `jev ship`: open a GitHub pull request for a finished run. |
| `core/kpi.py` | KPIs of runs and of the pull requests `jev ship` opened. |
| `core/benchmarks.py` | Each KPI as a series over recent shipped PRs, its rolling median and regressions. |
| **dashboard/** | |
| `dashboard/app.py` | The dashboard's routes and actions; actions call the same functions as the CLI. |
| `dashboard/views.py` | What each page shows, read from the store with the CLI's helpers. |
| `dashboard/render.py` | Safe Markdown and the server-side SVG charts (Spend, KPIs). |
| `dashboard/security.py` | Host check, token, CSRF, security headers ([docs/dashboard.md](docs/dashboard.md)). |
| `dashboard/runner.py` | Approve, resume and ship in a background thread, one job per run. |
| `dashboard/templates/`, `dashboard/static/` | Jinja2 templates; `style.css` and the vanilla `live.js`. |
| **dispatch/** | |
| `dispatch/base.py` | The `Dispatcher` protocol. |
| `dispatch/requests.py` | Build the compact `DecisionRequest` for each point D1 to D7. |
| `dispatch/rules.py` | `RulesDispatcher`: deterministic answers; fallback and tests. |
| `dispatch/jev.py` | `JevDispatcher`: Jev via the System One API. |
| `dispatch/guard.py` | Check answers against the allowed values; fall back to rules. |
| **quality/** | |
| `quality/levels.py` | The cumulative levels `none` to `scalable`. |
| `quality/base.py` | The `QualityCheck` protocol, `BaseCheck` defaults, project detection and output parsing helpers. |
| `quality/checks.py` | The registry of every check (`CHECKS`); start here to add one. |
| `quality/checks_basic.py` | Lint, format and the full test suite. |
| `quality/checks_medium.py` | Secret scan, SAST, dependency audits, coverage. |
| `quality/checks_production.py` | Type checkers, licences of new dependencies, container and IaC scans. |
| `quality/tools.py` | `ToolRunner`: how a check runs its tool, plus diff helpers. |
| `quality/review.py` | The AI code review (role `code_reviewer`). |
| `quality/policy.py` | Which gate results block a task or the integration at which level. |
| `quality/gate.py` | Run one gate: checks, AI review, policy, reports. |
| `quality/models.py` | Findings, check results and gate results. |
| `quality/report.py` | Markdown reports of a gate. |
| **roles/** | |
| `roles/<role>/prompt.md` | Jinja2 prompt template per role: planner, validator, coder, reviewer, code_reviewer, learner, chat. |
| `roles/render.py` | Render a built-in or user template with `StrictUndefined`. |
| `roles/schemas.py` | Output schema per role; coder summary block extraction. |
| **runlog/** | |
| `runlog/writer.py` | Where run logs live and how they are written. |
| `runlog/redact.py` | Remove secrets from text before it is logged. |
| **servers/** | |
| `servers/access.py` | Which servers, at which access level, and whether the browser each role gets for a run. |
| `servers/policy.py` | Access policy for server commands and file operations. |
| `servers/service.py` | The server tools agents call: policy check, audit line, SSH, result. |
| `servers/ssh.py` | The SSH transport: system `ssh`, non-interactive, strict host-key checking. |
| `servers/audit.py` | `.jev/audit/servers.log`: one JSON line per server operation, allowed or not. |
| `servers/mcp.py` | A minimal MCP server over stdio for the server tools (`jev mcp servers`). |
| `servers/browser.py` | The virtual browser: the pinned Playwright MCP server and its install command. |
| `servers/profiles.py` | Workspace profiles: picking one for a run, the repo default, the run's own copy. |
| **workers/** | |
| `workers/base.py` | The `Worker` protocol, `WorkerJob`, `WorkerOutput`. |
| `workers/registry.py` | Worker name -> worker instance. |
| `workers/claude_code.py` | Claude Code adapter (`claude -p`). |
| `workers/codex.py` | Codex adapter (`codex exec`). |
| `workers/api.py` | OpenAI-compatible API adapter (OpenRouter by default). |
| `workers/openrouter_models.py` | The OpenRouter model catalog, parsed and cached on disk. |
| `workers/tools.py` | The MCP servers, skills and plugins a role was given in config, for one job. |
| `workers/subprocess_runner.py` | Run a CLI with a timeout and a redacted log. |
| `workers/summary.py` | Turn a CLI agent's final reply into a `WorkerOutput`. |
| `workers/fake.py` | Scripted worker for tests and dry runs. |

If you add a module, add a row here in the same pull request.

## Code-quality bar

Well-written code here is code another contributor can understand, change safely and debug
without its original author. Review checks every pull request against the points below. Each
says what it means in this codebase and where to see it done; follow the existing example
rather than inventing a new pattern.

The review question behind all of them: **can a contributor trace what this change does,
understand how it fails, and verify it without guessing?**

1. **Execution is easy to trace.** A reader can follow a command from `cli/` through
   `cli/wiring.py` to `core/pipeline.py` and the worker that runs, and back to the result and
   cleanup. Wire things with plain function calls and constructor arguments, not factories,
   registries or wrappers (`workers/registry.py` is a plain map from name to worker).
   **The harness stays dumb:** it stores state, asks the dispatcher, checks the answer and
   executes it, enforcing only hard limits (rounds, attempts, budgets). An `if` in `core/` that
   picks a worker, model or effort belongs in `dispatch/`.
2. **Every resource has an owner.** Know which component creates and closes each process,
   connection, temporary file, worktree and background task, and make cleanup run after
   success, failure and cancellation (`try`/`finally`, `async with`). Examples:
   `workers/subprocess_runner.py` runs each CLI in its own process group and kills the group on
   timeout or cancellation; `core/lease.py` releases the run lease in a `finally`, even when
   the block fails.
3. **States are explicit.** Runs and tasks are `StrEnum` state machines (`RunState`,
   `TaskState` in `core/models.py`); worker outcomes are one `WorkerStatus` value. Add a state
   or a status value rather than a second boolean that can contradict the first.
4. **Decisions are separate from side effects.** Choosing (dispatch, validation, policy,
   result interpretation) is plain code over data; doing (filesystem, network, processes, git)
   sits behind an injectable interface: `Dispatcher`, `Worker`, `CommandRunner`, an `httpx`
   client. `quality/policy.py` decides what blocks; `quality/gate.py` runs the checks.
5. **Abstractions earn their place.** Add an interface when it marks a real boundary or has
   more than one implementation you actually ship or test with (`Worker` has Claude Code,
   Codex, API and fake). No speculative extension points, base classes with one subclass, or
   options nobody sets. Prefer a plain function to a class hierarchy.
6. **Inputs are validated at the boundary.** Config is checked once by the pydantic schema in
   `config/schema.py` (unknown and secret-looking keys are errors); Jev's answers by
   `dispatch/guard.py`; agent replies against the role's output schema (`roles/schemas.py`).
   Past the boundary, code trusts the typed value instead of re-checking it.
7. **Failures keep their meaning.** An empty result, a timeout and an execution failure are
   different outcomes: workers map them to `success`, `failure`, `error`, `quota_exhausted` or
   `timeout`, and never raise for an agent's failure. Errors name the operation that failed and
   its cause, and user-facing ones say what to do next. Catch an exception only where you
   recover, translate it or add context; never swallow it.
8. **Retries are a correctness question.** Retry only failures that are safe to repeat, with a
   bound (the Jev dispatcher posts once and retries once, inside one overall timeout). Before
   adding a retry, decide what happens if the first attempt succeeded and only its response was
   lost: `jev ship` looks for a PR already open for the branch before opening another.
9. **Concurrent work is isolated.** No mutable module-level state for a run. Shared state lives
   in the store, behind visible synchronization (the run lease, SQLite transactions). Tests
   cover two executions overlapping and one being cancelled (`test_lease.py`,
   `test_migrations_concurrency.py`).
10. **Tests cover behaviour that could break.** Test contracts, failure paths, cleanup and the
    interactions between components, not private helpers; a harmless refactor should not break
    a test. Integration tests run the whole pipeline against a real temporary git repo with
    fakes for agents. **Every external call is faked** and **no test touches the network or a
    real CLI**, except the opt-in live tests in `tests/live/` (see [Testing](#testing)).
11. **Tests are repeatable.** Control time, randomness, environment variables and temporary
    directories (`tmp_path`, `monkeypatch`; `tests/conftest.py` already isolates XDG dirs, keys
    and git config). A failure should point at a defect, never at test order or the machine.
12. **Dependencies are deliberate.** Each runtime dependency has a reason in its issue, sits
    behind the module that needs it (the dashboard's Starlette stays in `dashboard/`, behind the
    optional extra), and is locked in `uv.lock`. Remove what is no longer used.
13. **Names explain the work.** `terminate_worker`, not `handle_action`. A module has one
    purpose you can state in its first-line docstring; a growing `utils` module means ownership
    needs sorting out.
14. **Comments give reasons.** Explain constraints, verified API quirks, unusual behaviour and
    trade-offs, with a link to the issue or `docs/decisions.md` entry. Do not restate the code.
    When behaviour changes, update the docs that describe it in the same pull request.
15. **Public contracts are dependable.** `jev.toml` keys, CLI commands and flags, the state
    database, role output schemas and the worker interface are public. Keep them compatible
    unless an issue says otherwise; a database change is a new migration in
    `core/migrations.py`, never an edit to an old one. Document observable behaviour, and call
    out breaking changes in the pull request and `CHANGELOG.md`. **Do not invent external
    APIs:** check the official docs before relying on a CLI flag or API field and record what
    you found in `docs/verification.md`.
16. **Contributions stay straightforward.** A fresh checkout works with the commands in
    [Setup](#setup) and [Before you open a pull request](#before-you-open-a-pull-request). Keep
    each pull request to one coherent change a reviewer can understand in one sitting.

## Changes written with a coding agent

Using Claude Code, Codex or another agent to write a contribution is welcome; jev itself is
built that way. The author who opens the pull request is responsible for every line of it,
and review holds it to the same bar. Agent-written changes most often fail review for clutter,
so make sure the agent, and you before you push:

- **Inspects before editing.** Read the module, its tests and the newest `docs/decisions.md`
  entries, and follow the conventions already there.
- **Makes the smallest coherent change** that solves the issue. No drive-by refactors,
  reformatting of untouched code, or "while I was here" fixes (open an issue for those).
- **Leaves no clutter.** Remove unused helpers, speculative parameters and extension points,
  abandoned code paths, commented-out code, debug output and comments that narrate the code.
- **Never weakens a check to pass.** No new `# type: ignore`, `# noqa`, `cast` or `Any` to
  silence a real error, no skipped or loosened tests, no edits to lint or mypy settings. Fix the
  cause.
- **Verifies and reports exactly what was checked.** The pull request says which commands ran
  and what passed, and which behaviour was not verified. "Should work" is not a test result.
- **Justifies each new abstraction or dependency** in the pull request description: what
  complexity it removes, or why the standard library and the existing code cannot do it.

Say in the pull request that an agent helped, and which parts it wrote. Agents read
`CLAUDE.md` for project context, but the rules for contributions are the ones in this file.

## Code style

The tools decide most of it; CI fails if they disagree with your code.

- **Formatting:** `ruff format`, line length 100 (`[tool.ruff]` in `pyproject.toml`). Do not
  format by hand and do not reformat unrelated code.
- **Linting:** `ruff check` with the rule sets `E`, `F`, `W`, `I` (import order), `B`
  (bugbear), `UP` (pyupgrade, target Python 3.11), `SIM` and `RUF`. Fix the cause rather than
  adding `# noqa`; the codebase has none.
- **Types:** `mypy` in **strict** mode over `src`, `tests` and `examples/demo`. Every function,
  including tests and fixtures, needs parameter and return annotations (`-> None` too). When
  an ignore is unavoidable, scope it to the error code (`# type: ignore[arg-type]`), never a
  bare `# type: ignore`.
- **Python version:** code must run on 3.11, 3.12 and 3.13. Most modules start with
  `from __future__ import annotations`; follow the file you are editing.
- **Names:** the import package is `jev_agent` (never `jev`, which is an unrelated package on
  PyPI). Config is `jev.toml`, state lives in `.jev/`, branches are `jev/<run_id>/...`, and
  environment variables start with `JEV_`. "Jev" in `JevDispatcher` or
  `[dispatcher] primary = "jev"` means the Jev model, not the tool.
- **Docstrings and comments:** every module starts with a one-line docstring saying what it
  is for, often with a pointer to the doc or PLAN.md section it implements. Comments explain
  *why* (a constraint, a verified API quirk, a decision), not what the next line does.
- **Errors:** worker adapters never raise for agent failures; they map them to a status. User
  facing errors say what went wrong and what to do next.
- **Dependencies:** adding a runtime dependency needs a reason in the issue. If you change
  dependencies in `pyproject.toml`, run `uv lock` and commit `uv.lock`: CI installs with
  `uv sync --locked` and fails on a stale lock file.

## Testing

```bash
uv run pytest -q                          # unit + integration; live tests are skipped
uv run pytest tests/unit/test_rules.py    # one file
uv run pytest -k jev                      # by name
```

**Write the test first.** A bug fix comes with a test that fails before the fix and passes
after it; say in the pull request that you saw it fail. A new feature comes with tests for
the behaviour and its failure modes (bad config, missing CLI, timeout, empty output), not
only the happy path.

- **Unit tests** (`tests/unit/`) cover one module: state transitions, budgets, config
  validation, answer validation, error classification, redaction, parsing.
- **Integration tests** (`tests/integration/`) run the whole pipeline with `RulesDispatcher`
  and `FakeWorker` against a temporary git repo (`fixture_repo` in `tests/conftest.py`).
- **Fixtures** (`tests/fixtures/`) hold recorded or synthesized outputs per external system,
  including a fake `gh` (`tests/fixtures/gh/fake_gh.py`). When you add one, note its source in
  that folder's README: captured live, or synthesized from the docs.
- **Live tests** (`tests/live/`) call real APIs or CLIs and cost money. They never run in CI,
  and you do not need to run them for a pull request.

  ```bash
  JEV_LIVE_TESTS=1 OPENROUTER_API_KEY=... uv run pytest tests/live/test_api_live.py
  JEV_LIVE_TESTS=1 OPENROUTER_API_KEY=... uv run pytest tests/live/test_jev_live.py
  JEV_LIVE_TESTS=1 uv run pytest tests/live/test_cli_workers_live.py   # needs claude / codex
  ```

  See the docstring at the top of each file for optional variables.

Notes:

- Tests use `pytest-asyncio` in auto mode, so `async def test_...` just works.
- `tests/conftest.py` keeps your own setup out of the tests: it points `XDG_CONFIG_HOME`,
  `XDG_DATA_HOME` and `XDG_CACHE_HOME` at a temporary directory, unsets `OPENROUTER_API_KEY`,
  `TYPESAFE_API_KEY` and `JEV_FAKE_WORKERS`, and makes git ignore your global config and
  commit as a test identity. A test that needs a variable sets it with `monkeypatch.setenv`.
- The suite pins `COLUMNS=200`, because rich tables cut cells in a narrow terminal. Narrow
  output is checked on purpose in `tests/unit/test_narrow_terminal.py`: if you add a run
  outcome, next-step hint or error message to the CLI, add it there too.
- Secrets in tests are obvious fakes (for example `sk-or-v1-fake`), never real keys.

## Docs that change with the code

A pull request that changes behaviour updates the docs in the same pull request. Two of these
are enforced by tests in `tests/unit/test_config.py`:

| When you change | Update | Enforced |
| --- | --- | --- |
| A config field in `config/schema.py` or `config/servers_schema.py` | `docs/config.md`, under the `## ... [table]` section for its table | `test_config_docs_cover_every_schema_field`, `test_init_template_keys_are_documented` |
| The `jev init` template in `config/defaults.py` | `examples/jev.toml` must stay a `#` comment header followed by the template verbatim | `test_example_config_is_header_plus_init_template` |
| Anything a user would notice | `CHANGELOG.md`, a bullet under `## [Unreleased]` ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/) sections: Added, Changed, Fixed, ...) | review |
| A design choice or a deviation from PLAN.md | `docs/decisions.md` (see [Recording decisions](#recording-decisions)) | review |
| A role prompt or its variables | `docs/roles.md` | review |
| What an agent, server tool or the dashboard may do | `docs/security.md` | review |
| An external CLI flag or API field you rely on | `docs/verification.md`, with the source and the date you checked | review |
| A feature with its own doc (queue, chat, learning, quality, dashboard, servers, ship) | that file in `docs/` | review |
| A CLI command or flag | `README.md` if it is shown there, and the feature's doc | review |

## Adding a worker adapter

A worker runs one agent with a rendered prompt and returns a `WorkerOutput`
(`workers/base.py`). Steps, using a hypothetical `gemini` CLI as the example:

1. **Verify the agent's interface first.** Headless flags, output format, model and effort
   selection, permission or sandbox options, how it reports cost, how errors and usage limits
   appear. Write the findings, with sources, into `docs/verification.md`.
2. **Write the adapter** in `src/jev_agent/workers/gemini.py`, a class with `name = "gemini"` and
   `async def run(self, job: WorkerJob) -> WorkerOutput`.
   - Run the CLI with `workers/subprocess_runner.run_command` (no shell, timeout, redacted
     log at `job.log_path`). Take the runner as a constructor argument so tests can fake it.
   - Never raise for agent failures. Map them to a status in one classification function:
     `success`, `failure`, `error`, `quota_exhausted` or `timeout`.
   - For `planner`, `validator` and `reviewer`, the agent must not be able to write files, and
     the reply must validate against `job.output_schema`. For `coder`, parse the summary block
     with `roles.schemas.extract_coder_summary` (see `workers/summary.py`, shared by the CLI
     adapters).
   - Keys reach the agent only through the environment, never argv or the prompt.
   - If the agent can load MCP servers, skills or plugins, take them from `workers/tools.py`
     like the Claude Code and Codex adapters do.
3. **Add its config** in `config/schema.py`: a `GeminiConfig(WorkerConfig)` class and a
   `gemini` field on `WorkersConfig`. Add a commented section to the template in
   `config/defaults.py`, copy the template into `examples/jev.toml`, and document the keys in
   `docs/config.md`.
4. **Wire it** in `cli/wiring.py` (`build_cli_workers`), and teach `cli/doctor_cmd.py` to
   check its command. Add its vendor to `_WORKER_VENDORS` in `dispatch/requests.py` and a
   one-line description for Jev in `dispatch/jev.py`.
5. **Test it** against recorded outputs in `tests/fixtures/gemini/` (success, structured
   output, malformed output, auth error, quota error, timeout). Say in a fixture README whether
   they were captured live or synthesized from docs. Add an optional live test in
   `tests/live/`.
6. **Document** the permission model in `docs/security.md` and add a dated entry to
   `docs/decisions.md`.

## Changing a role prompt

Role prompts are Jinja2 templates in `src/jev_agent/roles/<role>/prompt.md`. You can improve one
without touching Python.

- Use only the variables listed for that role in [docs/roles.md](docs/roles.md). Rendering
  uses `StrictUndefined`, so an unknown variable is an error.
- Keep the "Output" section in line with the role's schema (`roles/schemas.py`,
  `core/models.py`). The schema is enforced in code; the prompt only tells the agent about it.
- For the coder, keep the closing block with `{{ summary_marker }}`.
- Run `uv run pytest tests/unit/test_roles.py`. It renders every template, checks key content
  and checks that missing variables fail.
- If you need a new variable, that is a Python change: pass it where the harness renders the
  prompt (`core/pipeline.py` for planner and validator, `core/tasks.py` for coder and
  reviewer), add it to the test variables, and document it in `docs/roles.md`.

Users can override prompts in their own repo with `[roles.<role>] prompt_file`; changes here
change the defaults for everyone.

## Recording decisions

`docs/decisions.md` lists every deviation from PLAN.md and every choice the plan left open,
newest first. Add an entry when you:

- do something PLAN.md says differently,
- pick between reasonable options (a default, a status mapping, a file layout),
- find that an external API differs from what the plan or the docs assumed.

Format: a `## YYYY-MM-DD: short title` heading at the top of the file, then short bullets. Say
what was decided, why, and what is unverified or should be revisited. Link the code or doc it
affects. Read the newest entries before you change an existing feature; the reason for odd
looking code is often there.

## Branches and commits

- **Fork** the repository and work on a branch of your fork, not on `main`.
- **Branch names:** `<kind>/<issue>-<short-description>`, for example
  `fix/35-jev-option-descriptions` or `feat/9-settings-sidebar`. Kinds: `fix`, `feat`, `docs`,
  `test`, `refactor`, `ci`.
- **Commit subjects** are short (about 72 characters at most), in the imperative, with a
  capital letter and no trailing period: "Validate Jev option descriptions", not "validated
  option descriptions." or "fix stuff". No `feat:` / `fix:` prefixes are needed in commits.
- **Commit bodies** explain why the change is needed and anything a reviewer would otherwise
  have to work out, wrapped at about 72 characters.
- **Reference the issue** with `Closes #N` (or `Fixes #N`) in the pull request description
  or the last commit, so the issue closes when the pull request is merged. Use `Refs #N` for
  related issues that should stay open.
- Pull requests are merged with a **merge commit**, so your branch's commits end up in the
  history of `main`. Before you ask for review, squash "fix lint" and "address review"
  commits into the commits they fix (`git rebase` on your branch, then force-push your
  branch). A focused pull request is often a single commit.
- Rebase on the latest `main` if your branch is behind or conflicts; do not merge `main` into
  your branch.

## Before you open a pull request

Run the full local check. It is what CI runs, in one line (CI runs
`ruff format --check`; locally `ruff format .` fixes the formatting for you):

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q
```

If `ruff format .` changed files, commit them. If `ruff check .` reports something it can fix,
`uv run ruff check --fix .` usually does it; review the result.

Checklist:

- [ ] The pull request addresses one issue and says which (`Closes #N`).
- [ ] `uv run ruff format .` left no changes to commit.
- [ ] `uv run ruff check .` passes.
- [ ] `uv run mypy` passes (strict, covers `src`, `tests` and `examples/demo`).
- [ ] `uv run pytest -q` passes.
- [ ] A bug fix has a test that failed before the fix; new behaviour has tests.
- [ ] Nothing in the tests touches the network or a real CLI, and there are no real secrets,
      home-directory paths or personal email addresses anywhere in the diff.
- [ ] Docs updated as listed in [Docs that change with the code](#docs-that-change-with-the-code),
      including `CHANGELOG.md` under "Unreleased" for user-visible changes.
- [ ] `uv.lock` updated (`uv lock`) if dependencies changed.
- [ ] Commits are tidy (no "fix lint" commits) and the branch is up to date with `main`.

## Pull requests and review

**The description** (the template asks for this) says:

- what the change does and why, in a few sentences, with `Closes #N`;
- how you tested it: which tests you added, that the new test failed before the fix, and any
  manual check (for example a run on the sample repo with `JEV_FAKE_WORKERS=1`);
- anything that changes behaviour for existing users: config keys, defaults, CLI output, the
  state database, what an agent is allowed to do;
- screenshots for dashboard changes;
- anything you are unsure about. Draft pull requests are welcome for early feedback.

**What happens next:**

1. **CI runs** on every pull request: lint (`ruff check`, `ruff format --check`, `mypy`), the
   tests on Python 3.11, 3.12 and 3.13, and a build of the wheel that is installed and run on
   its own. For a first-time contributor, GitHub may wait for the maintainer to approve the
   workflow run before CI starts. A pull request is reviewed only once CI is green; if a job
   fails, open its log, fix the cause and push again.
2. **The maintainer reviews.** This is a project with one maintainer, so a review can take a
   few days; a polite ping on the pull request after a week is fine. Review checks the
   [code-quality bar](#code-quality-bar), tests, docs and scope.
3. **You address the comments** by pushing to the same branch. Reply to each comment or
   resolve it; say if you disagree, with a reason. Squash fix-up commits before the final
   merge (see [Branches and commits](#branches-and-commits)).
4. **The maintainer merges** with a merge commit once CI is green and the review is approved.
   The issue closes through `Closes #N`. The change ships in the next release and appears in
   `CHANGELOG.md` under that version.

A pull request may be closed without merging if it is out of scope, duplicates other work,
falls short of the code-quality bar and the author does not want to change it, or has had no
response to review for about a month. You can always reopen it or start again later.

## Security

- **Never commit secrets.** No API keys, tokens, SSH keys or `.env` files, not even expired
  ones. jev's config rejects secret-looking keys in `jev.toml` on purpose: keys come from
  environment variables. Tests use obvious fakes.
- **No personal data:** no home-directory paths (`/Users/<name>`, `/home/<name>`) or personal
  email addresses in code, docs, fixtures or screenshots. Docs and images use the demo data
  from `examples/demo/seed_demo.py` in a temporary directory.
- **Tests never reach the network or a real CLI** (claude, codex, gh, ssh, npx, a browser).
  Everything goes through injectable runners and fakes (`JEV_FAKE_WORKERS=1`, the fake `gh` in
  `tests/fixtures/gh/fake_gh.py`, local bare git remotes).
- **Changes that widen what an agent can do** (permissions, sandbox flags, allowed tools,
  server access levels, the dashboard's security model) need an issue first and an update to
  [docs/security.md](docs/security.md). Server access `"full"` is deliberately not
  implemented; see issue #3.
- **Report vulnerabilities privately**, never in a public issue, pull request or discussion.
  See [SECURITY.md](SECURITY.md).

## License

jev is licensed under Apache-2.0 ([LICENSE](LICENSE)). By submitting a contribution you agree
that it is licensed under the same terms, as described in section 5 of the license
(inbound = outbound). You keep the copyright to your work. No CLA or DCO sign-off is required.
Only submit code you wrote or have the right to submit under Apache-2.0, and say so in the
pull request if any part comes from another project, with its license.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By taking part you agree
to it. Report unacceptable behaviour privately to the maintainer as described there.
