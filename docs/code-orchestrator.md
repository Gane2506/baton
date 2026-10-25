# Code orchestrator: the Coding Decision Agent (issue #187)

## Roles, in one paragraph

**Jev is the main orchestrator.** It decides what kind of work a run is,
routes it to subsystems, and manages the pipeline
(INTAKE → PLANNING → VALIDATING → AWAITING_APPROVAL → EXECUTING →
INTEGRATING → DONE) and every decision point in it (D1–D8). Nothing in this
document changes that.

**The Coding Decision Agent is the orchestrator for code-writing agents.**
When the work is code, it decides the coding strategy: it routes a coding
task to a model tier and skill, grades a finished diff, and proposes a merge
action. Jev and the quality gate still decide what happens with that
proposal — the code orchestrator never merges, ships, or approves a plan on
its own.

## The model

`S4MPL3BI4S/Coding_Decision_Agent` on Hugging Face
(<https://huggingface.co/S4MPL3BI4S/Coding_Decision_Agent>), a Laya
derivative fine-tuned to grade coding agent work, Apache-2.0. It does not
write code and does not emit prose: given a state (a diff, a tool call, an
agent trace, or a user request) and a fixed set of typed questions, it
returns a label and a calibrated probability for every option in one forward
pass.

What it grades (from the model card):

| Family | Questions |
| --- | --- |
| `code_review` | `code_quality` (0–4), `instruction_followed` (no/yes), `likely_correct` (no/yes), `merge_action` (accept / request_changes / needs_tests / reject) |
| `tool_call` | `tool_selection`, `parameter_structure`, `sequence_accuracy`, `should_call_tool`, `call_verdict` |
| `agent_trace` | `action` (continue / observe / human_review / stop), `needs_review`, `outcome`, `risk` (0–3), `urgency` (0–3) |
| `routing` | `model_tier` (small_fast / mid / frontier / reasoning), `task_difficulty` (0–3), `skill` (code_edit / debug / write_tests / refactor / explain / shell_ops / research / plan) |

## The gate: calibrated confidence, not labels

Gate on `answer_confidence` — the calibrated probability — never on the
entropy-style `confidence` field and never on a label alone. Starting
threshold: **0.7** (the model card's starting point; measure it on your own
traces before trusting it in production).

`jev_agent/code_orchestrator/grades.py:gate_code_review` applies it in this
order, deliberately:

1. `answer_confidence[merge_action]` below the threshold → **human_review**,
   whatever the label says. An unconfident "accept" is not an accept.
2. Confident `accept` → **accept**.
3. Confident `request_changes` / `needs_tests` → **rework**.
4. Confident `reject` → **block**.

## Where it is wired

- **Quality gate (merge decisions).** When `[code_orchestrator] enabled`
  is true, `run_gate` grades the same diff the gate just checked, after the
  tool checks and the AI review. The grade is stored on the gate result
  (`code_grade`) and shown in the gate report ("Code orchestrator" section).
  A blocking grade blocks the gate, so D5/D6 see the work as failed and send
  it back or to a human. The gate's checks, the AI review, and the 0–100
  ship score are unchanged and still run.
- **D4 routing (coding strategy).** When enabled, the task runner adds the
  routing hint (`model_tier`, `skill`, `task_difficulty`,
  `suggested_effort`) to the D4 request state as `code_routing` — but only
  when the hint clears the configured confidence threshold (0.7 by default):
  a low-confidence hint is no hint at all (`routing_hint` returns `{}`),
  never a weak tier Jev might follow as if it were trusted. When the hint
  is present, D4's worker/model/effort instructions name
  `state.code_routing` and tell Jev to prefer it when it fits the allowed
  answers (`dispatch.jev.CODE_ROUTING_INSTRUCTION`). Jev still
  picks worker/model/effort at D4 — the hint informs that decision, it does
  not replace it. A missing model, a failed inference, or a mis-shaped
  verdict never crashes D4: it proceeds without a hint, exactly as before
  the feature (issue #203, C3).
- **CLI.** `jev code status` (enabled? model? backend present?),
  `jev code grade --task "..." --diff-file change.diff [--tests-file ...]`
  (the same gate the quality gate applies, printed as a table), and
  `jev code route "Refactor …"` (tier + skill + suggested effort).

## Install

```bash
pip install laya
```

That is the whole install: no `coding_decision_agent` package is needed.
Baton ships the model's fixed question wording itself (recovered verbatim
from the public training dataset `S4MPL3BI4S/coding-decision-cases`; see
`src/jev_agent/code_orchestrator/laya_backend.py`) and runs the
`S4MPL3BI4S/Coding_Decision_Agent` checkpoint through the `laya` runtime.
If the `coding_decision_agent` package ever becomes installable, Baton
prefers it; until then the built-in backend is the real one.

```bash
# Verify it works (grade a diff the way the quality gate does):
jev code status    # backend_available: yes
jev code grade --task "Fix the off-by-one in paginate()" --diff-file change.diff
```

### What the first grade downloads, and what it needs

- **Weights:** the first grade downloads the ~842 MB checkpoint
  (`model.safetensors`, bf16) into the Hugging Face cache
  (`~/.cache/huggingface/hub`, or `$HF_HOME`). Later grades reuse it;
  set `HF_HUB_OFFLINE=1` to run fully offline after the first download.
- **Torch:** `pip install laya` pulls the default torch wheel, which
  includes CUDA libraries (~2.5 GB). On a CPU-only machine install the CPU
  wheel first, then `laya` without its torch dependency resolution
  fighting you:
  ```bash
  pip install torch --index-url https://download.pytorch.org/whl/cpu
  pip install laya
  ```
- **Hardware:** a GPU is used automatically when CUDA is available, but
  none is required — the model runs on CPU. On a 2-core CPU VM a grade
  takes on the order of a minute (measured 2026-10-24: load ~Xs, one
  `code_review` predict ~Ys; see the issue #259 notes for the numbers).
  Memory: roughly 2–3 GB RSS for torch + the bf16 weights. If that does
  not fit, keep the feature off — the gate records `unavailable` and the
  run proceeds exactly as before.
- **Never a hard dependency:** Baton deliberately does **not** add the
  model as a dependency: it is a 421M model behind `laya` + torch, and CI
  must not download weights. The backend loads lazily, only when grading
  is enabled and a grade is actually requested, and importing Baton never
  imports torch. If the package is absent, `jev code grade`/`route` fail
  with a clear message, and the quality gate records the grade as
  `unavailable` instead of blocking the run. A grade that did not run
  must never look like a grade.

> **History (2026-10-24):** `coding_decision_agent` is not on PyPI (its
> PyPI JSON endpoint 404s; the model card's install block only lists
> `pip install laya`), and the training repo is unpublished, so the model
> card's `CodingDecisionAgent` class ships nowhere importable. Baton
> therefore does not call `laya.load(...)` with invented question wording:
> the fixed wording ships in the `coding_decision_agent` package the card
> names, and scoring against guessed wording would make the calibrated
> confidence meaningless. The wording Baton uses is the dataset's own
> (`S4MPL3BI4S/coding-decision-cases`), pinned by a fixture test.

## Configuration

See `docs/config.md` (`[code_orchestrator]`):

```toml
[code_orchestrator]
enabled = true
model_id = "S4MPL3BI4S/Coding_Decision_Agent"
confidence_threshold = 0.7
max_diff_chars = 7000
```

Off by default, so existing runs are byte-for-byte unchanged. Enabling it
without the model installed changes nothing except a recorded
"unavailable" grade and a note from `jev code status`.

## Context: 2048 tokens

The model's context is 2048 tokens (`head_max_len` 320 for the options, the
rest for the state). Diffs are compacted before scoring
(`grades.compact_diff`, default 7000 characters): short diffs pass through
untouched; a long diff keeps its head and tail with an explicit marker
naming how much was dropped, so a grader never silently scores a
partial diff as if it were whole. The character budget is an approximation
(≈4 characters per token), not a token count — no tokenizer is a dependency.
Compaction is `CodeOrchestrator.grade_code`'s job, not the caller's: any
caller that forgot used to send an over-context diff to the model, and a
2-line diff of very long lines used to be sliced with no marker at all
(issue #203, C2/I10). The marker's count is computed from the head/tail
actually returned, and the result is never sliced after the marker is built.

## Error taxonomy (decided, not accidental)

- Missing backend → `CodeOrchestratorUnavailable`. The quality gate records
  "unavailable" and does not block; D4 proceeds without a hint; the CLI
  reports it and stops.
- Backend failed during inference → `CodeOrchestratorError`. The quality
  gate blocks fail-closed: a grade was attempted and cannot be trusted,
  which is not the same as no grade being configured. D4 still proceeds
  without a hint (a hint is advisory); the CLI reports it and stops.
- Mis-shaped verdict (unknown label, out-of-range score, a yes/no field
  that is neither — "maybe" is not a "no") → `ValueError`. Same handling
  as an inference failure: fail-closed at the gate, no hint at D4, a clear
  CLI error instead of a traceback.

## Two confidence bars, on purpose

`[dispatcher.jev] min_confidence` (0.55) gates Jev's own decision answers:
below it the decision falls back to the rules dispatcher. The code gate's
0.7 gates the Coding Decision Agent's *calibrated* `answer_confidence` on a
merge action (the model card's starting point), and the same 0.7 gates
routing hints. They are different models' confidences on different
decisions, not one bar with two values (issue #203, I7).

## Laya removal (issue #193)

- The Laya second vote is removed. `[dispatcher.laya]` is no longer documented or
  supported, and `jev laya tune` / `jev laya reset` are removed. Delete any
  `[dispatcher.laya]` table (including `[dispatcher.laya.weights]`) from `jev.toml`
  and your user config. Jev alone decides as main orchestrator; code grading and
  routing belong to the Coding Decision Agent above, gated on calibrated confidence.
- What remains of "Laya" is lineage, not a vote: the Coding Decision Agent model is a
  Laya derivative (see The model above), and `laya` is still the PyPI package name
  for that model's runtime (see Install above). Neither is a Baton second vote.
- The deterministic 0–100 ship score in `quality/scoring.py` is a static score from
  check results, not a model vote; its weights source is owned by the core/quality
  track of this removal.

## Not yet wired (honest gaps)

- `tool_call` and `agent_trace` grading are wired through the live
  `laya` backend (`LayaBackend.grade_trace` / `grade_tool_call`,
  `jev code trace`) with the trained question wording from the case
  dataset. The caveat is the state shape, not the wording: the training
  states are structured dicts, and Baton's callers pass free text, which
  the model reads as given. A caller with a structured trace or tool call
  should build the dict state instead of passing text.
- The quality gate grades the diff and task title only (`tests=""`): the
  test outcome is already a gate input (a failed task test never reaches
  the gate), but the model does not currently see the test log text.
  `jev code grade --tests-file` passes test output through as the state's
  `notes` field when a human asks for it.
- Live-model evaluation (issue #259): the model now runs, and
  `tests/live/test_code_orchestrator_live.py` is the smoke test for a run
  that has it installed (`JEV_LIVE_TESTS=1`). The 0.7 threshold was
  measured on real Baton diffs during that issue; see its report for the
  numbers before trusting the default in production.
