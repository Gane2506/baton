# The 16 coding principles — review checklist

How every change to this repo is judged. Each principle has concrete pass/fail
criteria with a small `jev` example. A subset is enforced mechanically by
`.github/workflows/principles.yml` (see "Mechanical enforcement" at the end);
the rest are review criteria — a reviewer checks them by reading the diff.

## 1. Make execution easy to trace

Follow a request from entry point to execution, result, cleanup without getting
lost. Prefer explicit construction and direct calls over factories, registries
and wrapper layers.

- **Passes:** `cli/wiring.py` builds the dispatcher, workers and pipeline in one
  readable function; `WorkerRegistry` is a plain name→worker map with a
  docstring saying so (`workers/registry.py`).
- **Fails:** a `get_worker(kind)` that consults a registry populated by
  decorators in five modules, with the real class only discoverable at runtime.
- Tiny example — prefer `WorkerRegistry({"claude_code": ClaudeCodeWorker(...)})`
  over `@register_worker("claude_code")` magic.

## 2. Every resource has a clear owner

For each process, connection, temp file and background task, one component
creates it and one component closes it — after success, failure *and*
cancellation.

- **Passes:** `core/workspace.py::run_git` — the `Popen` is `communicate()`d in
  the same function; on timeout the whole process group is SIGTERM/SIGKILLed.
  Temp dirs via `with tempfile.TemporaryDirectory(...)`.
- **Fails:** spawning a child in one module and hoping another module reaps it;
  a `TemporaryDirectory` whose cleanup depends on the caller remembering to call
  `.cleanup()`.
- Ask for every resource: "if this raises here, who closes it?" The answer must
  be a named component, not "whoever".

## 3. Explicit state transitions

Model lifecycle as an enum with legal transitions, not a bag of booleans.
Impossible combinations must be unrepresentable.

- **Passes:** `RunState` / `TaskState` (`core/models.py`) — one field, one
  value; the pipeline dispatches on it (`core/pipeline.py`).
- **Fails:** `is_done: bool` next to `is_failed: bool` next to
  `is_cancelled: bool`, where `is_done and is_failed` compiles fine.
- New lifecycle? New `StrEnum`, stored in the state DB, transitions in one
  place.

## 4. Separate decisions from side effects

Scheduling, validation and interpretation live apart from filesystem, network
and process work. A decision function should be testable without touching the
world.

- **Passes:** `dispatch/rules.py` and `dispatch/consensus.py` are pure
  functions over requests and config; workers do the I/O.
- **Fails:** a dispatcher that `os.remove()`s a worktree while choosing the next
  worker.
- Boundary case: `dispatch/jev.py` POSTs to the System One API — the decision
  *model* is remote, so the call is inherent, but it stays isolated in
  `_post_with_retry` with an injectable client and clock. New network calls in
  `dispatch/` need the same isolation and a justification.

## 5. Abstractions must remove real complexity and justify their code

Every layer, base class and helper must delete more complexity than it adds.
A wrapper around one call site is not an abstraction.

- **Passes:** `core/commands.py` — one seam ("run shell commands behind an
  interface tests can fake") used by every task test command.
- **Fails:** a `BaseManager`/`AbstractHandler` hierarchy with a single
  implementation, or a helper used once that only renames a stdlib call.
- Test: delete the abstraction mentally — if the call sites get *simpler*, the
  abstraction was negative value.

## 6. Validate inputs at boundaries into dependable internal types

Parse and validate once, at the edge (CLI args, config files, env vars, worker
JSON, HTTP bodies), into typed models. Inside the codebase, trust the types.

- **Passes:** config through pydantic (`config/schema.py`); worker replies
  through `roles/schemas.py`; `TaskCard.id` has `pattern=r"^T\d+$"`.
- **Fails:** `int(os.environ["JEV_TIMEOUT"])` deep in a worker (unvalidated,
  raises `KeyError`/`ValueError` far from the user); re-checking
  `if plan is not None` three layers past parsing.
- Env vars are strings: compare or validate them at startup, never cast
  mid-flight.

## 7. Preserve failure meaning

Empty, timeout, quota-exhausted and failure are different facts — never merge
them. Keep the error's context (type, message, where) all the way to the log.

- **Passes:** `classify_api_error` (`workers/api.py`) maps 402/429/403/408/5xx
  to `quota_exhausted`/`failure`/`error`/`timeout` distinctly; the six
  `except Exception` sites (harness, scheduler, dashboard runner, guard,
  quality gate) all record `type(exc).__name__` and `str(exc)`.
- **Fails:** `except Exception: return None`; logging only "request failed"
  without status code, body or which attempt.
- A broad `except` is allowed at a trust boundary (a buggy worker/check must
  not crash a run) only if it records the exception and converts it to a typed
  outcome.

## 8. Retries are a correctness problem

Every retry is bounded, and the retried operation is idempotent — no quiet
duplication of side effects (double PRs, double charges, double merges).

- **Passes:** API worker and Jev dispatcher retry *once*, only for 429/5xx
  classified `failure`, honoring `Retry-After` (≤ 10 s); migrations retry WAL
  setup against a monotonic deadline.
- **Fails:** `while True: try: ... except: continue` with no counter, deadline
  or jitter; retrying a non-idempotent POST because "it usually works".
- `while True` in this codebase is a state-machine or poll loop — each one must
  show its exit: a state check, a counter, a deadline, or a user interrupt.

## 9. Isolated concurrent work

No mutable global run state. Concurrent work uses visible synchronization
(locks, leases, queues) that a reader can see at the use site.

- **Passes:** the dashboard's per-run `threading.Lock`s (`dashboard/runner.py`);
  the SQLite-backed run lease with `flock` on `chat.lock` (`core/lease.py`,
  `core/chat.py::hold_chat`); per-thread SQLite connections.
- **Fails:** a module-level `_ACTIVE_RUNS = {}` mutated by the pipeline and the
  dashboard; a `global` statement anywhere.
- Two drivers, one run (dashboard + terminal `jev resume`) is the sharp edge —
  the lock must cover both or the gap must be documented as accepted.

## 10. Test realistic breakage

Tests cover contracts, failure paths, cleanup and component interactions —
not just the happy path. Integration tests sit at module boundaries.

- **Passes:** `tests/integration/` exercises pipeline × store × workspace;
  unit tests cover timeouts, cancellation, corrupt state, locked DBs,
  quota/auth classification.
- **Fails:** a new retry path with tests only for "succeeds first try"; a
  cleanup function with no test that kills the process mid-way.
- New failure mode → new test that triggers it. New boundary → an integration
  test across it.

## 11. Repeatable tests

Control time, randomness, environment and temp dirs. A test that sleeps on
real time or depends on run order is a flaky test waiting to happen.

- **Passes:** `scheduler.py` takes injectable `now`/`sleep`; `openrouter_models`
  takes `now`; tests use `tmp_path` and `monkeypatch`.
- **Fails:** `time.sleep(0.3)` in a test to "wait for the thread"; reading the
  real `$HOME`; depending on wall-clock dates.
- If the code needs the clock, inject it; the test then owns time.

## 12. Deliberate dependencies; reproducible installs

Every dependency is chosen, minimal, and pinned via `uv.lock`. No drive-by
adds, no unpinned ranges that resolve differently next month.

- **Passes:** six core deps (`typer`, `rich`, `pydantic`, `jinja2`,
  `markupsafe`, `httpx`) + a `dashboard` extra; `uv lock --check` is green.
- **Fails:** adding `requests` "because the snippet used it" when `httpx` is
  already there; a new import without `uv lock` updated.
- New dependency? Say in the PR why the existing set can't do it.

## 13. Names explain work

Functions and modules are named for what they do (`terminate_worker`, not
`handle_action`). Each module has one coherent purpose, stated in its
docstring.

- **Passes:** `run_git`, `hold_chat`, `merge_check`, `classify_api_error`;
  `app/`'s docstring: "use cases shared by the CLI and the web UI".
- **Fails:** `def process(data)`, `def do_stuff()`, `utils.py` /
  `helpers.py` / `misc.py` catch-alls.
- Watch for near-duplicate module names (`learner.py` vs `learning.py`):
  if two names are confusable, the docstrings must disambiguate in one line.

## 14. Comments explain reasoning, constraints and tradeoffs — not code

Comment *why*, and what you decided against. Update the docs when behavior
changes; `docs/decisions.md` gets a newest-first entry for every deviation or
open choice.

- **Passes:** "Switching to WAL needs an exclusive lock and SQLite reports a
  busy database at once, without waiting, so the switch is retried… (issue
  #23)" (`core/migrations.py`).
- **Fails:** `i += 1  # increment i`; a behavior change with no
  `docs/decisions.md` entry and no CHANGELOG line.
- If the comment restates the code, delete it. If the code is surprising, the
  comment must say why it has to be that way.

## 15. Dependable public contracts; document breaking changes

The CLI surface, config schema, state DB schema and decision cards are
contracts. Breaking one is a deliberate, documented act.

- **Passes:** `docs/config.md` documents every schema field (a test fails
  otherwise); migrations carry the state DB forward (`core/migrations.py`);
  CHANGELOG follows Keep a Changelog.
- **Fails:** renaming a `jev.toml` key without a migration path and a
  CHANGELOG entry; changing a decision card's question names silently.
- "Will this break someone's `jev.toml`, script, or saved `.jev/`?" — if yes,
  it needs a migration, docs, and a changelog entry marked breaking.

## 16. Fresh checkout has clear setup/run/check commands; focused changes

A new clone goes from zero to green with documented commands, and a change
touches only what it needs.

- **Passes:** `CONTRIBUTING.md` / `README.md`: `uv sync --all-extras`,
  `uv run pytest -q`, `uv run ruff format . && uv run ruff check . && uv run mypy`.
- **Fails:** setup steps that only exist in someone's shell history; a bugfix PR
  that also reformats unrelated modules.
- Before pushing: the finish command above, plus the PII scan for the home
  directory path and the Gmail address (the exact command lives in
  `CLAUDE.local.md` — only that file may match).

## Mechanical enforcement

`.github/workflows/principles.yml` checks the subset below on every push to
`main` and every pull request. It is fast (no dependency sync) and
deterministic. Everything else in this checklist is enforced by review.

| Principle | Mechanical check |
|---|---|
| 9 | ruff `PLW0603`: no `global` statements, anywhere. AST: no mutable module-level run state (module-scope `dict`/`list`/`set` not `UPPER_CASE` or dunder). |
| 4 | `dispatch/` modules may not import `subprocess`, `shutil`, `tempfile`, `multiprocessing`, `pathlib` or `socket` — decisions stay apart from filesystem/process work. |
| 7 | every `except Exception` / `except BaseException` handler must reference the caught exception or re-raise — failures keep their meaning. |
| 8 | `time.sleep` in `src/` only in the allowlisted bounded waits (`core/scheduler.py`, `core/migrations.py`, `cli/board_cmds.py`); anything new needs a justification comment in the workflow. |
| 2 | a file using `subprocess.Popen` must also `communicate()` — the creator owns the child's lifetime. |
| 1 | no module-level `__getattr__` / `__dir__` dynamic dispatch in `src/`. |
| 6 | no unvalidated `int(os.environ…)` / `float(os.getenv…)` casts in `src/`. |
| 12 | `uv lock --check` — the lockfile matches `pyproject.toml`. |
| 16 | `README.md` and `CONTRIBUTING.md` document `uv sync`, `pytest` and `ruff`. |
| 10 | `tests/integration/` is non-empty; live tests stay opt-in behind `JEV_LIVE_TESTS`. |
| — | Dashboard spacing (issue #332): no literal `gap`/`margin`/`padding` value in the dashboard stylesheets or templates — every value resolves to the `--s*` scale in `style.css` (`scripts/check_spacing_tokens.py`). |

Review-only (need a human reading the diff): the rest of 1, 2, 3, 4, 5, 6, 7,
8, 10, 11, 13, 14, 15, 16 — plus the judgment calls this checklist names
(e.g. the `dispatch/jev.py` remote-model call, the dashboard/terminal lock
gap).
