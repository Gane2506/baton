# Contributing

Thanks for helping. This file covers setup, the layout of the code, the rules the design
depends on, and what a pull request needs.

## Setup

You need Python 3.11+, git and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Gane2506/baton
cd baton-agent-orchestrator
uv sync                 # creates .venv with the package and dev tools
uv run jev --version
uv run pytest
```

You do not need Claude Code, Codex or any API key to develop or run the test suite.

To try the CLI end to end without agents, use the sample repo with fake workers (see
[examples/sample-repo/README.md](examples/sample-repo/README.md)):

```bash
cp -R examples/sample-repo /tmp/textstats && cd /tmp/textstats
git init -q && git add . && git commit -qm init
uv run --project /path/to/baton-agent-orchestrator jev init
# set [dispatcher] primary = "rules" in jev.toml
JEV_FAKE_WORKERS=1 uv run --project /path/to/baton-agent-orchestrator jev run "anything"
```

## Project layout

```
src/jev_agent/
  cli/        typer commands
  config/     config schema, layered loading, the `jev init` template
  core/       models, run and task state machines, store, budgets, git worktrees
  dispatch/   dispatcher interface, Jev and rules dispatchers, answer guard
  roles/      prompt templates and output schemas, one folder per role
  runlog/     run log files and secret redaction
  workers/    worker interface and adapters
tests/
  unit/         one module or function at a time
  integration/  whole pipeline with fakes against a temporary git repo
  fixtures/     recorded or synthesized outputs of Claude Code, Codex, Jev and OpenRouter
  live/         real calls, skipped unless JEV_LIVE_TESTS=1
docs/         config, roles, security, decisions, verification of external APIs
examples/     sample config and sample repo
```

Module by module:

| Module | Purpose |
| --- | --- |
| `cli/app.py` | Entry point; registers the commands. |
| `cli/init_cmd.py` | `jev init`: write `jev.toml`, create `.jev/`, update `.gitignore`. |
| `cli/doctor_cmd.py` | `jev doctor`: check git, config, dispatcher, worker CLIs and keys. |
| `cli/run_cmds.py` | `jev run`, `approve`, `resume`, `cancel`. |
| `cli/status_cmds.py` | `jev status` and `jev log` (read-only views of the store). |
| `cli/wiring.py` | Build the dispatcher, workers and pipeline from config. |
| `cli/repo.py` | Find the target git repository. |
| `config/schema.py` | Pydantic config schema. Unknown keys and secret-looking keys are errors. |
| `config/loader.py` | Layering: defaults < user config < repo `jev.toml`. |
| `config/defaults.py` | File names and the commented `jev.toml` template. |
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
| `core/store.py` | SQLite store: runs, tasks, attempts, decisions. |
| `core/migrations.py` | Store schema migrations (and the shared learning database's). |
| `core/learning.py` | Learning profiles and lessons (repo and shared scope), lesson selection, which profile a run uses. |
| `core/learner.py` | The end-of-run learning step: scoring, D7, the learner role, applying its answer. |
| `dispatch/base.py` | The `Dispatcher` protocol. |
| `dispatch/requests.py` | Build the compact `DecisionRequest` for each point D1 to D7. |
| `dispatch/rules.py` | `RulesDispatcher`: deterministic answers; fallback and tests. |
| `dispatch/jev.py` | `JevDispatcher`: Jev via the System One API. |
| `dispatch/guard.py` | Check answers against the allowed values; fall back to rules. |
| `roles/<role>/prompt.md` | Jinja2 prompt template per role. |
| `roles/render.py` | Render a built-in or user template with `StrictUndefined`. |
| `roles/schemas.py` | Output schema per role; coder summary block extraction. |
| `runlog/writer.py` | Where run logs live and how they are written. |
| `runlog/redact.py` | Remove secrets from text before it is logged. |
| `workers/base.py` | The `Worker` protocol, `WorkerJob`, `WorkerOutput`. |
| `workers/registry.py` | Worker name -> worker instance. |
| `workers/claude_code.py` | Claude Code adapter (`claude -p`). |
| `workers/codex.py` | Codex adapter (`codex exec`). |
| `workers/api.py` | OpenAI-compatible API adapter (OpenRouter by default). |
| `workers/subprocess_runner.py` | Run a CLI with a timeout and a redacted log. |
| `workers/summary.py` | Turn a CLI agent's final reply into a `WorkerOutput`. |
| `workers/fake.py` | Scripted worker for tests and dry runs. |

## Design rules

These come from [PLAN.md](PLAN.md) section 0. Pull requests that break them will be asked to
change.

1. **The harness stays dumb.** Routing intelligence belongs to the dispatcher. The harness
   stores state, asks the dispatcher what to do, checks the answer and executes it. It only
   enforces hard limits (rounds, attempts, budgets). If you are adding an `if` in `core/` that
   picks a worker, model or effort, it belongs in `dispatch/` instead.
2. **Every external call goes through an interface and is faked in tests.** Dispatchers,
   workers, model APIs, shell commands and subprocesses each sit behind a protocol or an
   injectable dependency (`Dispatcher`, `Worker`, `CommandRunner`, an `httpx` client or
   transport).
3. **No network in tests.** No test may hit a real network or a real agent CLI. Use recorded
   fixtures, fakes or `httpx.MockTransport`. Real calls go in `tests/live/` behind
   `JEV_LIVE_TESTS=1`.
4. **Small, readable modules.** A contributor should understand any file in five minutes.
   Prefer a plain function over a class hierarchy, and a clear name over a comment.
5. **Do not invent external APIs.** Check the current official docs before relying on a CLI
   flag or API field, and record what you found (see "Recording decisions").

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
3. **Add its config** in `config/schema.py`: a `GeminiConfig(WorkerConfig)` class and a
   `gemini` field on `WorkersConfig`. Add a commented section to the template in
   `config/defaults.py`, and document the keys in `docs/config.md`.
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

Format: a `## YYYY-MM-DD: short title` heading, then short bullets. Say what was decided, why,
and what is unverified or should be revisited. Link the code or doc it affects.

## Testing

```bash
uv run pytest                          # unit + integration; live tests are skipped
uv run pytest tests/unit/test_rules.py # one file
uv run pytest -k jev                   # by name
```

- **Unit tests** (`tests/unit/`) cover one module: state transitions, budgets, config
  validation, answer validation, error classification, redaction, parsing.
- **Integration tests** (`tests/integration/`) run the whole pipeline with `RulesDispatcher`
  and `FakeWorker` against a temporary git repo (`fixture_repo` in `tests/conftest.py`).
- **Fixtures** (`tests/fixtures/`) hold recorded or synthesized outputs per external system.
  When you add one, note its source in that folder's README.
- **Live tests** (`tests/live/`) call real APIs or CLIs and cost money. They never run in CI.

  ```bash
  JEV_LIVE_TESTS=1 OPENROUTER_API_KEY=... uv run pytest tests/live/test_api_live.py
  JEV_LIVE_TESTS=1 OPENROUTER_API_KEY=... uv run pytest tests/live/test_jev_live.py
  JEV_LIVE_TESTS=1 uv run pytest tests/live/test_cli_workers_live.py   # needs claude / codex
  ```

  See the docstring at the top of each file for optional variables.

Tests use `pytest-asyncio` in auto mode, so `async def test_...` just works.

## Pull request checklist

- [ ] `uv run ruff format .`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy` (strict, covers `src` and `tests`)
- [ ] `uv run pytest`
- [ ] New behaviour has tests; nothing in the tests touches the network or a real CLI.
- [ ] Docs updated: `docs/config.md` for config keys, `docs/roles.md` for prompts or
      variables, `docs/security.md` for anything that changes what an agent may do.
- [ ] A `docs/decisions.md` entry if you deviated from PLAN.md or made a design choice.

CI runs the lint and type checks once and the tests on Python 3.11, 3.12 and 3.13. Keep pull
requests small and focused; one change per PR is easiest to review.

## License

jev is licensed under Apache-2.0 ([LICENSE](LICENSE)). By submitting a contribution you agree
that it is licensed under the same terms, as described in section 5 of the license. No CLA or
DCO sign-off is required.
