# Baton: Build Plan

> Working name: **baton** (rename freely; it is only used as the package name, CLI command, and config folder).
> License: Apache-2.0 (default; change before first public commit if preferred).

## 0. Instructions for Claude Code

Read this whole file before writing any code. Then:

1. Build strictly milestone by milestone (Section 13). Stop after each milestone, summarize what was built, show test results, and wait for approval before starting the next one.
2. Do not invent external APIs. Before implementing anything in Section 16 ("Verify first"), check the current official docs and tell the user what you found. If something differs from this plan, follow the docs and note the change in `docs/decisions.md`.
3. Keep the harness dumb. All routing intelligence belongs to the dispatcher. The harness only stores state, asks the dispatcher what to do, and executes the answer.
4. Every external call (dispatcher, worker, model API) must go through an interface so it can be faked in tests. No test may hit a real network or real CLI.
5. Prefer small, readable modules over clever abstractions. This is an open-source project; contributors should understand any file in five minutes.

---

## 1. What we are building

An open-source Python tool that turns an idea into reviewed, tested code by running a pipeline of AI agents. A decision model (Jev, from TypeSafe via OpenRouter) acts as the dispatcher: at every step it decides which agent gets the work next, which model it uses, and how much reasoning effort to spend.

Agents are existing coding tools run headless (Claude Code, Codex CLI) plus plain API models through OpenRouter. Each agent plays a **role**: planner, validator, coder, reviewer.

### Goals

- One command: `baton run "idea"` takes an idea through plan, validation, human approval, execution, and testing.
- Jev dispatches every step. A rules-based dispatcher exists as a fallback and for tests.
- Workers, models, roles, and budgets are all configuration, not code.
- Safe by default: isolated worktrees, no auto-push, hard cost caps, full audit log.
- Every dispatcher decision is logged with its inputs, outputs, and cost, so users can see why things happened.

### Non-goals (for v0.1)

- No web UI.
- No auto-merge to the user's main branch.
- No automatic detection of subscription quota exhaustion (v0.2).
- No parallel execution (v0.2).

---

## 2. Architecture

Four parts:

| Part | Responsibility | Intelligence |
|---|---|---|
| **Harness** (`core/`) | Pipeline state machine, task graph, persistence, budgets, launching workers | None. Deterministic. |
| **Dispatcher** (`dispatch/`) | Answers "what next?" at each decision point with typed decisions | All routing decisions |
| **Workers** (`workers/`) | Adapters that run an agent (CLI or API) with a role prompt and return a structured result | Whatever the underlying model has |
| **Roles** (`roles/`) | Prompt files and output schemas for planner, validator, coder, reviewer | Prompt content only |

Flow of control: the harness reaches a decision point, builds a small `DecisionRequest` (state + questions), calls the dispatcher, validates the typed answer, then executes it (launch a worker, loop back, ask the human, stop).

The dispatcher never sees full transcripts or full plans. It sees compact cards. This keeps Jev well inside its context limit and keeps decisions cheap.

---

## 3. Pipeline

States for a **run** (one idea):

```
INTAKE -> PLANNING -> VALIDATING -> (loop back to PLANNING, max N rounds)
       -> AWAITING_APPROVAL -> EXECUTING -> INTEGRATING -> DONE
any state -> NEEDS_HUMAN | FAILED | BUDGET_EXCEEDED | CANCELLED
```

States for a **task** (one unit of work from the plan):

```
PENDING -> READY (dependencies done) -> DISPATCHED -> RUNNING
        -> TESTING -> REVIEWING -> DONE
RUNNING/TESTING/REVIEWING -> RETRY | ESCALATE | REASSIGN -> DISPATCHED
any -> FAILED (after max attempts) | NEEDS_HUMAN
```

### Decision points (each is one dispatcher call)

| # | When | Questions |
|---|---|---|
| D1 | Intake | Is the idea clear enough to plan, or ask the human? Which planner worker, model, effort? |
| D2 | After plan | Which validator worker, model, effort? (Should be a different vendor than the planner by default.) |
| D3 | After validation | Approve plan, revise (back to planner), or escalate to human? |
| D4 | Per ready task | Complexity tier, worker, model, effort. |
| D5 | After task tests/review | Done, retry same config, escalate (stronger model/effort), reassign (other worker), or human? |
| D6 | After integration | Shippable, create fix task, or human? |

The harness enforces hard limits regardless of dispatcher answers: max plan rounds (default 3), max attempts per task (default 3), per-run and per-task cost caps.

---

## 4. Data models (pydantic)

Define in `core/models.py`. Sketch, adjust as needed:

```python
class Idea(BaseModel):
    text: str
    repo_path: Path
    context_files: list[Path] = []

class TaskCard(BaseModel):
    id: str                       # "T1", "T2"...
    title: str
    description: str
    files_hint: list[str]         # files/dirs likely touched
    depends_on: list[str]
    acceptance_criteria: list[str]
    test_command: str | None      # e.g. "pytest tests/test_search.py"

class Plan(BaseModel):
    summary: str
    tasks: list[TaskCard]
    risks: list[str] = []

class ValidationResult(BaseModel):
    verdict: Literal["approve", "revise"]
    issues: list[str]

class WorkerResult(BaseModel):
    status: Literal["success", "failure", "error", "quota_exhausted", "timeout"]
    summary: str                  # short, written by the agent per role contract
    files_changed: list[str]
    cost_usd: float | None
    tokens: dict | None
    raw_log_path: Path

class DecisionRequest(BaseModel):
    point: Literal["D1","D2","D3","D4","D5","D6"]
    state: dict                   # compact, never full transcripts
    questions: dict[str, list[str]]   # question name -> allowed answers

class Decision(BaseModel):
    point: str
    answers: dict[str, str]
    dispatcher: str               # "jev" | "rules"
    model: str | None             # model that made the decision
    cost_usd: float | None
    latency_ms: int
```

Every answer must be one of the allowed values in `questions`. If the dispatcher returns anything else, the harness rejects it, logs it, and falls back to the rules dispatcher for that decision.

---

## 5. Dispatcher

### Interface (`dispatch/base.py`)

```python
class Dispatcher(Protocol):
    name: str
    async def decide(self, req: DecisionRequest) -> Decision: ...
```

### Implementations

- **`JevDispatcher`** (`dispatch/jev.py`): calls Jev through OpenRouter. Builds `state` and `questions` from the `DecisionRequest`. Allowed answer lists come from config (model pools per role, effort levels, worker names). See Section 16 for verifying the exact API.
- **`RulesDispatcher`** (`dispatch/rules.py`): deterministic. Uses config defaults and simple rules (e.g. complexity from task size heuristics, escalate on second failure). Used as fallback and in all tests.
- **`HumanDispatcher`** (later, optional): asks the user at every decision. Useful for debugging.

### Example D4 request (illustrative)

```
state:
  task: {id: T3, title: "Add rate limiting to /api/search", files_hint: [...], criteria_count: 3}
  quota: {claude_code: "ok", codex: "ok"}
  prior_attempts: 0
  run_spend_usd: 0.42, run_budget_usd: 5.00
questions:
  complexity: [trivial, standard, hard]
  worker: [claude_code, codex, api]
  model: [<pool for role "coder" from config>]
  effort: [low, medium, high]
```

---

## 6. Workers

### Interface (`workers/base.py`)

```python
class Worker(Protocol):
    name: str
    async def run(self, job: WorkerJob) -> WorkerResult: ...

class WorkerJob(BaseModel):
    role: Literal["planner","validator","coder","reviewer"]
    prompt: str                   # rendered role prompt + task card
    workdir: Path                 # worktree for coders; read-only copy for others
    model: str | None
    effort: str | None
    timeout_s: int
    output_schema: type[BaseModel] | None   # for planner/validator/reviewer JSON output
```

### Adapters for v0.1

- **`ClaudeCodeWorker`**: runs Claude Code headless as a subprocess. Captures stdout/stderr to a log file. Parses structured output where the role requires JSON.
- **`CodexWorker`**: runs Codex CLI headless (`codex exec` or current equivalent) as a subprocess. Same contract.
- **`ApiWorker`**: calls a model directly through OpenRouter (OpenAI-compatible). Used for planner/validator/reviewer roles where no file editing is needed, and as a cheap fallback.
- **`FakeWorker`** (tests only): returns scripted results.

### Result contract

Each role prompt ends by instructing the agent to write a final summary block (and JSON for planner/validator/reviewer). The adapter extracts it. If extraction fails, status is `error`, not `success`.

### Error classification

Adapters must map failures into `WorkerResult.status`. For v0.1, `quota_exhausted` only needs to be detected from obvious error messages; robust detection is v0.2. Keep classification logic in one function per adapter with tests against recorded error outputs.

---

## 7. Roles

`roles/` holds one folder per role:

```
roles/planner/prompt.md       # instructions + output format
roles/planner/schema.py       # -> Plan
roles/validator/prompt.md     # -> ValidationResult
roles/coder/prompt.md         # free-form work + summary block
roles/reviewer/prompt.md      # -> ValidationResult-style verdict on a diff
```

Prompts are Jinja2 templates with variables like `{{ idea }}`, `{{ task }}`, `{{ plan_summary }}`, `{{ validation_issues }}`. Users can override any role by pointing config at their own prompt file. Contributors should be able to improve a role without touching Python.

Planner prompt must require: tasks split along file/module boundaries where possible, explicit dependencies, testable acceptance criteria, and a test command per task when the repo has tests.

---

## 8. Configuration

`baton.toml` in the target repo (with `baton init` generating it), overridable by `~/.config/baton/config.toml`.

```toml
[dispatcher]
primary = "jev"          # "jev" | "rules"
fallback = "rules"
jev_model = "typesafe/jev-latest"   # verify slug

[budget]
per_run_usd = 5.00
per_task_usd = 1.50
max_plan_rounds = 3
max_task_attempts = 3

[approval]
require_plan_approval = true      # human gate after validation

[workers.claude_code]
enabled = true
command = "claude"                # path override
timeout_s = 1200

[workers.codex]
enabled = true
command = "codex"
timeout_s = 1200

[workers.api]
enabled = true
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

[roles.planner]
workers = ["claude_code", "api"]
models = ["anthropic/claude-opus-x", "openai/gpt-x"]     # placeholders, user fills
selection = "dispatcher"          # "dispatcher" | "fixed"

[roles.validator]
workers = ["codex", "api"]
models = ["..."]
prefer_different_vendor_than = "planner"

[roles.coder]
workers = ["claude_code", "codex"]
models = ["..."]
efforts = ["low", "medium", "high"]

[roles.reviewer]
workers = ["api", "codex"]
models = ["..."]
```

Secrets only come from environment variables. Config files never contain keys. `baton doctor` checks which CLIs are installed and which keys are set.

---

## 9. Storage and audit log

- SQLite at `.baton/state.db` in the target repo: runs, tasks, attempts, decisions, costs.
- Per-attempt raw logs at `.baton/runs/<run_id>/<task_id>/<attempt>.log`.
- `decisions` table stores the full `DecisionRequest` and `Decision` JSON for every call.
- `baton log <run_id>` prints a readable timeline: each decision, its answers, which worker ran, result, cost.
- `.baton/` is added to the target repo's `.gitignore` by `baton init`.

Runs must be resumable: if the process dies, `baton resume <run_id>` continues from the last persisted state.

---

## 10. Security defaults

- Every coder task runs in its own git worktree on a branch `baton/<run_id>/<task_id>`. Never on the user's current branch.
- No `git push`, ever, in v0.1. Integration produces a local branch `baton/<run_id>/result` for the user to review.
- Planner, validator, and reviewer get read-only access (or a disposable copy) of the repo.
- API keys are passed to workers only through the environment, never rendered into prompts or logs. Log writer redacts known key patterns.
- Hard cost caps enforced by the harness, independent of dispatcher answers.
- Worker CLIs run with their own permission systems; baton documents recommended settings rather than disabling them. Any flag that skips permission prompts must be opt-in in config and clearly labelled in docs.
- `baton` never sends repo contents anywhere except to the workers the user configured.

---

## 11. CLI

```
baton init                 # create baton.toml, .baton/, update .gitignore
baton doctor               # check CLIs, keys, git state, config validity
baton run "idea"           # full pipeline
baton run --plan-only "idea"
baton approve <run_id>     # approve plan at the human gate
baton resume <run_id>
baton status [run_id]
baton log <run_id>         # decision + execution timeline
baton cancel <run_id>
```

Use `typer` with `rich` for readable output.

---

## 12. Repository layout

```
baton/
  pyproject.toml           # uv-managed; console script "baton"
  LICENSE                  # Apache-2.0
  README.md
  CONTRIBUTING.md
  PLAN.md                  # this file
  docs/
    decisions.md           # design decisions and deviations from this plan
    config.md
    security.md
    roles.md
  src/baton/
    cli/                   # typer app
    core/                  # models, pipeline state machine, task graph, budget, store
    dispatch/              # base, jev, rules
    workers/               # base, claude_code, codex, api, fake
    roles/                 # prompt templates + schemas
    config/                # loading, validation, defaults
    logging/               # run logs, redaction
  tests/
    unit/
    integration/           # full pipeline with FakeWorker + RulesDispatcher
    fixtures/              # recorded CLI outputs, error samples, sample repos
  examples/
    baton.toml
```

Tooling: `uv`, `typer`, `rich`, `pydantic` v2, `httpx`, `jinja2`, `asyncio` subprocesses, SQLite (stdlib `sqlite3` is fine), `pytest`, `pytest-asyncio`, `ruff`, `mypy` (or `pyright`). GitHub Actions running lint, types, and tests on 3.11+.

---

## 13. Milestones for v0.1

Stop and report after each one.

### M0: Skeleton
- `uv` project, package layout, CLI entry point with `--version`, ruff/mypy/pytest configured, CI workflow, LICENSE, minimal README.
- **Done when:** `uv run baton --version` works and CI passes.

### M1: Models, config, storage
- Pydantic models from Section 4. Config loading/validation with clear error messages. SQLite store with migrations for runs, tasks, attempts, decisions.
- `baton init` and `baton doctor` (doctor can stub CLI checks for now).
- **Done when:** unit tests cover config validation and store round-trips.

### M2: Rules dispatcher + fake worker + pipeline
- Pipeline state machine for runs and tasks (Section 3) with hard limits.
- `RulesDispatcher`, `FakeWorker`, role prompt rendering.
- Sequential execution of tasks in dependency order.
- Human approval gate (`baton approve`), `status`, `log`, `resume`, `cancel`.
- **Done when:** an integration test runs a full pipeline end to end with fakes, including a plan revision loop, a task retry, an escalation, and a budget-exceeded stop. Resume works after a simulated crash.

### M3: API worker
- `ApiWorker` via OpenRouter (OpenAI-compatible) for planner, validator, reviewer roles with structured JSON output and schema validation.
- Cost capture from API responses.
- **Done when:** tests pass using recorded HTTP responses; optional manual smoke test with a real key behind an env flag.

### M4: Jev dispatcher
- Verify the current Jev API first (Section 16) and report findings.
- `JevDispatcher` for D1 through D6, with answer validation and automatic fallback to `RulesDispatcher` on invalid answers or errors.
- **Done when:** tests pass with recorded Jev responses, including invalid-answer fallback. Decision log shows dispatcher name, model, cost, latency.

### M5: Claude Code and Codex workers
- Verify current headless flags and output formats for both CLIs first (Section 16) and report findings.
- Worktree creation and cleanup, subprocess management with timeouts, log capture, summary extraction, error classification (including basic `quota_exhausted`).
- Per-task test command execution after the coder finishes.
- **Done when:** adapter tests pass against recorded outputs, and a manual end-to-end run on `examples/` sample repo succeeds behind an env flag.

### M6: Integration step, docs, release
- Integration: merge task branches into `baton/<run_id>/result` in dependency order, run full test suite, D6 decision.
- Docs: README with quickstart and demo, config reference, security notes, roles guide, CONTRIBUTING.
- Tag `v0.1.0`, publish to PyPI.
- **Done when:** a new user can `uv tool install baton`, run `baton init`, `baton doctor`, and `baton run` on the sample repo following only the README.

---

## 14. Roadmap after v0.1

- **v0.2:** parallel task execution (asyncio, one worktree per task, concurrency limit in config); robust quota/rate-limit detection; subscription-to-API fallback chains per worker; checkpointing (commit + progress note) so a task can be handed from one worker to another mid-way.
- **v0.3:** reviewer agent on diffs as a standard stage; smarter integration with conflict detection and a fix-task loop; cost reports per run.
- **v0.4:** small FastAPI server + web dashboard for runs, decisions, and logs; more worker adapters (e.g. Gemini CLI, local models).
- **Later:** plugin system for third-party workers and dispatchers.

---

## 15. Testing strategy

- Unit tests for state transitions, budget enforcement, config validation, answer validation, error classification, log redaction.
- Integration tests run the whole pipeline with `RulesDispatcher` + `FakeWorker` against a tiny fixture git repo.
- Recorded fixtures for Jev, OpenRouter, Claude Code, and Codex outputs (including error and quota messages). No live calls in CI.
- Optional live smoke tests guarded by `BATON_LIVE_TESTS=1` and real credentials, never run in CI.

---

## 16. Verify first (do not assume)

Check current official docs for each before implementing, and record findings in `docs/decisions.md`:

1. **Jev API on OpenRouter:** endpoint (System One / Decisions API vs chat completions), request shape for state and questions, how allowed answers are constrained, response shape, model slugs (`typesafe/jev-1.13`, `~typesafe/jev-latest`), pricing, context limit. Also look at the Jev Router (`typesafe/jev-router`) as a possible simple option for `ApiWorker` model selection.
2. **Claude Code headless mode:** flags for non-interactive runs, JSON/stream output, model selection, permission handling, working directory, how cost/usage is reported, how rate-limit or usage-limit errors appear.
3. **Codex CLI headless mode:** `codex exec` (or current equivalent) flags, JSON output, model and reasoning effort selection, custom providers config, sandbox/approval settings, how limit errors appear.
4. **OpenRouter chat completions:** structured output / JSON schema support per model, usage and cost fields in responses.
5. **Terms of use:** note in `docs/security.md` that users must check whether their plan permits automated use of the CLIs; API keys are the supported path.

---

## 17. Open decisions (confirm with the user when reached)

- Final project name and PyPI availability (before M6).
- License confirmation (before first public commit).
- Whether planner/validator models are dispatcher-selected or fixed by default (config supports both; pick the default).
- Whether the reviewer stage is on by default in v0.1 or opt-in.
