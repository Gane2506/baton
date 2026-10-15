# Benchmark tasks

Each task is a directory `benchmarks/tasks/<id>/` with:

- `brief.md` — the natural-language idea fed to `jev run`. **This is the only
  thing the agent sees**, so the brief is the spec: pin every contract the
  grader checks (exact JSON keys, exit codes, what "malformed" means). If the
  grader and the brief disagree, fix the brief first.
- `fixture/` — a tiny sandbox repo the run builds *in*. Self-contained:
  stdlib only, must not import `jev_agent` or depend on Baton's internals, so
  tasks cannot rot when Baton refactors. Keep it tiny (a handful of files).
- `grade.py` — the deterministic grader. It speaks this protocol:

  ```
  python grade.py --result DIR --run-json FILE --diff FILE
  ```

  - `--result`: a clean checkout of the merged result branch (`jev/<run>/result`).
  - `--run-json`: `{"run_id": ..., "ship_score": int|null, "cost_usd": ..., "state": ...}`.
  - `--diff`: the unified diff `fixture-base..result-branch`.
  - stdout: one JSON object matching `jev_agent.capbench.models.GradeReport`.
  - exit `0` = passed, `1` = graded failure, `2` = harness error (bad inputs,
    pytest missing, …).

Use `jev_agent.capbench.grading.grade_main` — it implements the argv parsing,
the hidden-pytest run, the diff-size band check and the JSON/exit-code
protocol, so a grader only declares `HIDDEN_TESTS`, `max_added`/`max_files`
and any extra checks. Hidden tests are embedded in `grade.py` (written to a
temp dir at grade time, never into the fixture the agent sees); the fixture's
own tests can additionally be collected via `extra_collect` (see T2).

Grading rules (design §2): deterministic only — hidden pytest executed by the
harness, diff-size bands, the gate's ship score from the run record. No LLM
judge. T8-style semi-deterministic graders are out of scope for Phase 1.

A new task is done when its `grade.py` **fails on the unmodified fixture**
(the task is not trivially green) and **passes on a hand-written reference
solution** (the grader is satisfiable) — see `tests/capbench/test_tasks.py`.
