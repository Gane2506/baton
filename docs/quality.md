# Quality and security gate

An optional, CodeRabbit-style gate between "the tests pass" and "done". At each level it runs
more checks (linters, the full test suite, scanners) plus an AI code review of the diff, writes
a report, and blocks work that fails. Off by default: `[quality] level = "none"` keeps the
behaviour of earlier versions (each task's own test command only).

```bash
jev run --quality production "add password reset"   # this run only
jev quality checks                                  # which checks exist, which tools you have
jev quality show RUN                                # the run's review (review.md)
```

Code: `src/jev_agent/quality/` (checks, policy, review, reports, gate), the `code_reviewer`
role in `src/jev_agent/roles/code_reviewer/`. Config: [config.md](config.md#quality).

## Levels

Levels are cumulative: each one runs everything below it.

| Level | Adds | Blocks on |
| --- | --- | --- |
| `none` | nothing (the task's own `test_command` only) | failing task tests, as before |
| `basic` | lint and format check (ruff for Python; ESLint and Prettier for JS/TS when the repo configures them), the full test suite, an AI code review of the diff | failing tests, lint or format; any **critical** finding |
| `medium` | secret scan (gitleaks, else a built-in scan), SAST (bandit; Semgrep with local rules), dependency audit (pip-audit, `npm audit --omit=dev`), coverage (pytest-cov, minimum 60%) | same as basic |
| `production` | type check (mypy, pyright or tsc when the repo configures it), coverage minimum 80%, licences of new Python dependencies (pip-licenses), a security checklist and API/migration compatibility in the AI review | basic, plus failing production checks (types, coverage, licences) and **major or critical security** findings |
| `scalable` | AI review of performance, concurrency, scalability and observability; container and IaC scans (trivy, hadolint, checkov); a threat model | production, plus **major performance** findings |

At every level a check whose tool is listed in `[quality] required_tools` blocks when it fails,
and a missing required tool counts as a failure. Everything else is **reported, never
blocking**: minor findings, a failing advisory check (for example coverage at `medium`), a
skipped check.

Setting the level, highest precedence first:

1. `jev run --quality LEVEL` / `jev resume --quality LEVEL` (changes a run's level from now on),
   `/quality LEVEL` inside `jev chat` (for runs launched from that chat);
2. `jev queue add --quality LEVEL` (stored with the item; the scheduler passes it on);
3. `[quality] level` in `jev.toml` (or your user config).

The level a run uses is stored with it, so `jev resume` keeps it.

## Where the gate runs

- **After each task**, once its coder finished and its own `test_command` passed: on the task's
  diff against its base, in the task's worktree. Tools look at the files the task changed where
  they can (ruff, ESLint, Prettier, bandit, Semgrep, hadolint take file lists); tools that scan
  the whole tree (gitleaks, mypy, pyright, tsc, trivy, checkov) only count findings in changed
  files, so problems that were already there never block new work. Dependency audits run on a
  task only when it changed a dependency file. Coverage runs only at integration.
- **At integration**, after the task branches merged into `jev/<run_id>/result` and the
  integration tests passed: on the run's whole diff (result branch against the run's start),
  with every check of the level, including coverage and dependency audits.

The full test suite is `[quality] test_command`, else `[integration] test_command`, else
detected: `uv run pytest -q` (with `uv.lock`) or `pytest -q` for a Python project with tests,
`npm test` when `package.json` has a real `test` script, `go test ./...`, `cargo test`. A suite
command that already passed in the same step (the task's own test command, the integration
command) is not run twice.

### What happens when it blocks

- **A task**: D5 gets `quality: failed`, `quality_failed: true` and up to five compact findings
  (`critical bug src/a.py:12: divides by zero (ai-review)`). The rules dispatcher retries with
  the findings in the coder's prompt (the `quality_findings` template variable, plus the usual
  `feedback`), escalates on the second block, then reassigns or asks a human, within
  `[budget] max_task_attempts`.
- **Integration**: D6 gets the same fields and answers `fix_task`: a new task, after all the
  others, lists the findings to fix. Fix rounds are limited by `[integration] max_fix_rounds`;
  after that the run stops in `NEEDS_HUMAN`.
- **The gate is a hard limit**, like the budget: if the dispatcher answers `done` (D5) or
  `ship` (D6) while the gate blocks, the harness turns it into a retry or a fix task.

## Reports

- `.jev/runs/<run_id>/quality/<T1-2|integration-1>.md`: one per gate (task id and attempt, or
  integration round).
- `.jev/runs/<run_id>/quality/review.md`: the run's review, rebuilt after every gate from the
  latest gate of each task and of the integration: Summary (one row per gate), Walkthrough
  (one row per file), Findings grouped by severity with `file:line` and suggestions, a Checks
  table (pass / fail / skipped and why), and from `production` a Security checklist, from
  `scalable` a Scalability checklist and a Threat model.
- Tool output is in the run's logs (`.jev/runs/<run_id>/<task>/<n>.quality.<check>.log`),
  redacted. Reports are redacted too.

`jev status RUN` shows one quality line, `jev log RUN` one line per gate with each check's
status, `jev quality show RUN` prints `review.md`.

## The AI code review

A dedicated role, `code_reviewer`, with its own prompt
(`src/jev_agent/roles/code_reviewer/prompt.md`, Jinja2; sections are switched on by level) and
output schema `QualityReview` (summary, walkthrough, findings with severity
`info|minor|major|critical` and category `bug|security|performance|maintainability|tests|docs`,
file, line range and suggestion, plus checklists and a threat model). It is a separate role,
not a mode of `reviewer`, because its output and its job (report findings, not approve or
revise) differ; both can be on at once. Override the prompt with
`[roles.code_reviewer] prompt_file`.

Worker, model and effort: like the task reviewer, the role's first worker and first model
(free models first with `prefer_free`) at its middle effort, with no extra decision point. A
free model that runs out of quota falls back like other roles. The budget is checked before
each review. With no usable worker the review is skipped and the tool checks still run.

## Installing the tools

Nothing is required: a tool that is not installed skips its check (the report says so). jev
finds a tool on `PATH`, in the project's `node_modules/.bin`, or in the repo's `.venv/bin`.
`jev quality checks` shows what is installed and what applies to the repo; `jev doctor` warns
about missing tools for the configured level.

| Tool | Install | Notes |
| --- | --- | --- |
| ruff | `pip install ruff` / a dev dependency | |
| ESLint, Prettier, tsc | `npm install --save-dev eslint prettier typescript` | only with a config in the repo |
| pytest-cov | a dev dependency of the project | coverage needs it importable by the suite |
| gitleaks | <https://github.com/gitleaks/gitleaks> | without it, a built-in scan of added lines runs (key patterns from the log redactor, private keys, cloud tokens, values of secret environment variables) |
| bandit | `pip install bandit` | |
| Semgrep | `pip install semgrep` | only with local rules (`.semgrep.yml` or `.semgrep/`): `--config auto` downloads rules from the registry, which jev does not do on its own |
| pip-audit | `pip install pip-audit` | queries a vulnerability database over the network |
| npm | Node.js | `npm audit` queries the npm registry |
| mypy, pyright | `pip install mypy` / `pip install pyright` | only with a config (`[tool.mypy]`, `mypy.ini`, `pyrightconfig.json`, `[tool.pyright]`) |
| pip-licenses | `pip install pip-licenses` in the project's environment | |
| trivy, hadolint, checkov | `brew install trivy hadolint`, `pip install checkov` | only with Dockerfiles or IaC files; trivy downloads its databases on first use |

Each tool command runs through jev's command runner in the worktree (or on the result branch)
with `[quality] timeout_s` (default 600 s).

## Cost

The tool checks cost nothing but time (the full suite runs once per task at `basic` and up).
The AI review is one extra model call per task attempt and one per integration round; it sees
the diff (up to 40,000 characters), so its cost grows with the size of the change. It counts
toward `[budget] per_run_usd` and `per_task_usd` like every worker. Use a cheaper or free model
for `[roles.code_reviewer]` to keep it small.

## Limitations

- **The AI review can miss real problems and report problems that are not there.** Treat its
  findings as a second opinion. A false positive can block a task; the retry gets the finding
  and the coder may push back by fixing nothing, after which D5 escalates and finally asks a
  human. Lower the level or remove the cause if that happens often.
- **Tools only check what they check.** Parsing of tool output is best-effort; when a tool's
  output cannot be parsed, the check reports the tail of its output instead of findings.
- **Not a substitute for a human security review.** Treat `production` and `scalable` as a
  safety net for automated work, not as a sign-off.
- Licence checks only cover new Python dependencies. Coverage is only measured for Python with
  pytest-cov.
- Tools from the repo (`node_modules/.bin`, `.venv/bin`) are run as they are: only use the
  gate on repositories you trust (see [security.md](security.md)).

## Adding a check

Write a small class with the `QualityCheck` attributes (`name`, `tool`, `levels`, `gate_from`,
`scopes`, `needs`, `hint`, `whole_repo`) and two methods, `applies(repo)` and
`async run(workdir, runner) -> CheckResult`, and append an instance to `CHECKS` in
`src/jev_agent/quality/checks.py`. The gate, the reports, `jev quality checks` and `jev doctor`
pick it up; nothing else changes. Use `runner.resolve(tool)` and `runner.missing(tool)` for the
tool, `runner.run(name, command)` to run it (fake it in tests with `FakeCommandRunner`), and
`runner.changed(suffixes)` for the files to look at.
