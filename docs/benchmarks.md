# Capability benchmark scoreboard

> **Capability benchmark** - fixed tasks run through Baton (plan → validate → build → review → quality gate), published as a public scoreboard. For the **KPI benchmarks** (regression detection over shipped PRs: cost per PR, wall time, first-pass rate), see [docs/ship.md](ship.md).

> **Status: unevaluated.** No benchmark run has been performed yet - this page is a template describing the suite and the rubric. Run `jev capbench run --all` to produce real scores. Nothing below is measured.

## Tasks

| task | what | verdict | hidden tests | correctness | ship score | cost | time | composite |
|---|---|---|---|---|---|---|---|---|
| `t1-cli-flag` | T1: add `--format` output to the demo CLI | not run | - | - | - | - | - | - |
| `t2-seeded-bug` | T2: make the failing test suite green | not run | - | - | - | - | - | - |
| `t3-validation` | T3: harden `parse_config` against malformed input | not run | - | - | - | - | - | - |

## The tasks in detail

### `t1-cli-flag` - T1: add `--format` output to the demo CLI

Grading: hidden pytest + diff within 150 added lines / 6 files + the gate's ship score.

<details>
<summary>brief.md (the idea the agent sees)</summary>

# T1: add `--format` output to the demo CLI

The repo contains a tiny demo CLI (`demo_cli.py`) that greets somebody:

```
$ python demo_cli.py --name Ada
Hello, Ada!
```

Add a `--format` option to it:

- `--format text` (the default): print `Hello, <name>!` exactly as today.
- `--format json`: print exactly one line of JSON with two keys —
  `{"greeting": "Hello, <name>!", "name": "<name>"}` — the same greeting text,
  wrapped together with the name.
- Any other value must exit non-zero with a usage error (argparse's built-in
  invalid-choice error is fine).

Keep `--name` working exactly as before, and do not add any third-party
dependencies. The hidden grader checks the default, both formats, the exact
JSON keys, and the non-zero exit on a bad value.

</details>

### `t2-seeded-bug` - T2: make the failing test suite green

Grading: hidden pytest + diff within 150 added lines / 6 files + the gate's ship score.

<details>
<summary>brief.md (the idea the agent sees)</summary>

# T2: make the failing test suite green

The repo contains a tiny calculator (`calc.py`) with a test suite under
`tests/` (`pytest`). One of the tests fails — there is a bug in the source.

Find and fix the bug so the whole suite passes:

```
$ python -m pytest tests/ -q
```

Do **not** modify, move or delete anything under `tests/` — fix the source,
not the tests. The grader re-runs the suite itself, checks that the diff does
not touch `tests/`, and runs extra hidden edge-case tests against the fixed
functions.

</details>

### `t3-validation` - T3: harden `parse_config` against malformed input

Grading: hidden pytest + diff within 150 added lines / 6 files + the gate's ship score.

<details>
<summary>brief.md (the idea the agent sees)</summary>

# T3: harden `parse_config` against malformed input

The repo contains a minimal config parser (`config.py`). `parse_config(data)`
builds a config dict from raw data — but today it lets `KeyError`,
`TypeError` and friends escape on malformed input.

Harden it:

- It must **never** let `KeyError`, `TypeError`, `AttributeError` (or similar)
  escape. On malformed input, either return a config built from `DEFAULTS`
  for the missing/bad parts, or raise `ValueError` with a clear message —
  pick one and be consistent.
- Valid configs (all three keys present with the right types) must keep
  working **exactly** as before, including a numeric string for `port`
  (`"8080"` → `8080`).
- Extra unknown keys must be ignored, not crash.
- Do not add third-party dependencies (stdlib only).

The hidden grader feeds it `None`, missing keys, wrong types, nested garbage
and empty dicts, checks the valid cases are unchanged, and checks that no
changed file imports anything outside the stdlib.

</details>

## Rubric

Per task, four dimensions normalized to 0-100 (design §3):

- **Correctness** (50%): hidden-test fraction - the grader's pytest, executed by the harness, never by the agent.
- **Ship score** (25%): the quality gate's deterministic 0-100 ship score at the `basic` level on the merged result branch.
- **Cost** (15%): 100 at ≤ $0.50, 0 at ≥ $2.00, linear between.
- **Time** (10%): 100 at ≤ 5 min, 0 at ≥ 30 min, linear between.

Suite score = mean of the task composites. The table above publishes the raw dimensions, not just the composite.

## How it runs

- Manual trigger only: `jev capbench run --task <id>` or `jev capbench run --all`.
- Each task gets a throwaway sandbox repo copied from its fixture, a fixed `[budget] per_run_usd = $2` cap, `basic` quality level, and the pinned worker profile; grading happens on the merged result branch.
- **No nightly schedule exists.** A nightly run would cost ~$3-7/night in model spend and needs the maintainer's explicit go-ahead before it is built.
- Grading is deterministic only (hidden pytest, diff-size bands, ship score). No LLM judge.
- Anti-gaming: hidden tests live outside the fixture the agent sees; raw dimensions are published; tasks at 100% for 90 days get replaced or hardened.

_Generated by `jev capbench report`; do not edit by hand._
