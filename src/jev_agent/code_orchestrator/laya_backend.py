"""The live `laya` backend for the Coding Decision Agent (issue #259).

This is the missing piece PR #191 deliberately left open: the model itself.
`coding_decision_agent` is not on PyPI, the training repo is unpublished, and
the model card's `CodingDecisionAgent` class ships nowhere importable -- so
Baton implements that class's job here, as `LayaBackend`: load the
`S4MPL3BI4S/Coding_Decision_Agent` checkpoint through the `laya` runtime
(`pip install laya`) and ask it the questions it was trained on.

The question wording is NOT guessed. It is copied verbatim from the public
training dataset the model card itself names,
`S4MPL3BI4S/coding-decision-cases` (train/val/test rows carry the exact
`questions` dicts the model was trained and benchmarked on; recovered
2026-10-24, pinned by tests in `tests/unit/test_code_orchestrator_laya.py`).
A guessed wording would score against a distribution the model never saw,
and the calibrated confidence would be meaningless -- that is why `backend.py`
refused a raw `laya` call until the wording was sourced. If the wording ever
drifts (a new checkpoint, a new dataset version), the pinned tests fail and
the constants below must be re-extracted from the dataset, not edited by hand.

State shapes mirror the training rows too:

- `code_review`: `{"family", "task", "language", "diff_stats", "diff",
  "notes"}`. No training row carries a separate `tests` key, so test output
  goes in `notes` (the row's free-text field) when the caller passes it.
- `routing`: `{"family", "request"}`.
- `tool_call` / `agent_trace`: the training states are structured dicts
  (`request`/`tools`/`proposed_calls`, and `task`/`agent`/`constraints`/
  `trace_summary`). Baton's callers pass free text, which `laya` serialises
  as-is; the model reads the text it is given. That is weaker than a
  structured state and is documented at each method, not hidden.

`laya` (and torch behind it) stays an optional dependency: nothing in this
module is imported at `jev` startup -- `laya` is imported inside
`LayaBackend._agent`, on first use only. When `laya` is absent the loader in
`backend.py` raises `CodeOrchestratorUnavailable` and the quality gate
records the grade as unavailable instead of blocking; when the model is
present but a grade fails, that is `CodeOrchestratorError` and the gate
blocks fail-closed. The first grade downloads the ~842 MB checkpoint into
the Hugging Face cache; later grades reuse it.
"""

from __future__ import annotations

import re
import warnings
from typing import Any

from jev_agent.code_orchestrator.backend import CodeOrchestratorUnavailable
from jev_agent.code_orchestrator.grades import DEFAULT_MODEL_ID

__all__ = [
    "AGENT_TRACE_QUESTIONS",
    "CODE_REVIEW_QUESTIONS",
    "ROUTING_QUESTIONS",
    "TOOL_CALL_QUESTIONS",
    "LayaBackend",
    "build_code_review_state",
    "build_routing_state",
    "detect_language",
    "diff_stats",
]


# --- Fixed question wording, verbatim from S4MPL3BI4S/coding-decision-cases.
# Do not edit by hand: re-extract from the dataset (see module docstring).

CODE_REVIEW_QUESTIONS: dict[str, dict[str, object]] = {
    "code_quality": {
        "type": "score",
        "instructions": (
            "Rate the overall quality of the proposed code or patch with respect "
            "to the task: correctness, readability, efficiency, style and how "
            "well it follows the instructions."
        ),
        "criteria": [
            "very poor: wrong, broken or unrelated to the task",
            "poor: major issues, would need substantial rework",
            "acceptable: does the job but has clear weaknesses",
            "good: solid solution with minor issues",
            "excellent: correct, clean, idiomatic and complete",
        ],
    },
    "instruction_followed": {
        "type": "choice",
        "instructions": (
            "Does the proposed code or patch follow the task instructions and stated preferences?"
        ),
        "criteria": {
            "no": "no, the statement does not hold",
            "yes": "yes, the statement holds",
        },
    },
    "likely_correct": {
        "type": "choice",
        "instructions": (
            "Judging from the task, the patch and any test output, is the "
            "change likely to fully resolve the task (tests would pass)?"
        ),
        "criteria": {
            "no": "no, the statement does not hold",
            "yes": "yes, the statement holds",
        },
    },
    "merge_action": {
        "type": "choice",
        "instructions": "What should a code reviewer do with this change?",
        "criteria": {
            "accept": "the change is correct and complete; merge or apply it as is",
            "request_changes": (
                "the approach is right but specific fixes are needed before merging"
            ),
            "needs_tests": (
                "plausible change but it lacks verification; tests must be added or run first"
            ),
            "reject": ("wrong approach, does not address the task, or unsafe; discard it"),
        },
    },
}

ROUTING_QUESTIONS: dict[str, dict[str, object]] = {
    "model_tier": {
        "type": "choice",
        "instructions": (
            "Which is the cheapest model tier that can be expected to solve this request correctly?"
        ),
        "criteria": {
            "small_fast": "a small, fast model (roughly 1B-9B parameters) is enough",
            "mid": "a mid-size general model (roughly 10B-70B) is needed",
            "frontier": "a frontier-class general model is needed",
            "reasoning": "a dedicated long-reasoning model is needed",
        },
    },
    "task_difficulty": {
        "type": "score",
        "instructions": "How difficult is this coding or agent request?",
        "criteria": [
            "trivial: one obvious step, no ambiguity",
            "easy: a few steps, well specified",
            "hard: multi-step, needs investigation or design choices",
            "very hard: open-ended, large scope, or requires deep reasoning",
        ],
    },
    "skill": {
        "type": "choice",
        "instructions": ("Which coding-agent skill best matches what this request needs first?"),
        "criteria": {
            "code_edit": "write or modify code to implement the request",
            "debug": "find and fix the cause of a bug or failing behaviour",
            "write_tests": "add or update tests",
            "refactor": "restructure code without changing behaviour",
            "explain": "explain code, concepts or a design",
            "shell_ops": "run commands, manage the environment, build or deploy",
            "research": "search docs, the web or the codebase for information",
            "plan": "break the request into a plan before acting",
        },
    },
}

TOOL_CALL_QUESTIONS: dict[str, dict[str, object]] = {
    "should_call_tool": {
        "type": "choice",
        "instructions": (
            "Given the request and the available tools, what is the right "
            "thing for the assistant to do next?"
        ),
        "criteria": {
            "call_tool": ("call one of the available tools; all required information is present"),
            "ask_followup": ("ask the user for missing information before any tool can be called"),
            "answer_directly": "answer from general knowledge; no tool is needed",
            "cannot_answer": ("the request cannot be fulfilled with the available tools; say so"),
        },
    },
    "call_verdict": {
        "type": "choice",
        "instructions": ("How should the agent runtime treat the proposed tool call(s)?"),
        "criteria": {
            "execute": "the call is correct; run it",
            "fix_args": "right tool but the arguments need correcting first",
            "wrong_tool": "a different tool (or sequence) should be used",
            "abstain": "no tool should be called here; ask the user or answer directly",
        },
    },
}

AGENT_TRACE_QUESTIONS: dict[str, dict[str, object]] = {
    "action": {
        "type": "choice",
        "instructions": "What should the observability system do with this trace?",
        "criteria": {
            "continue": "Let the agent proceed without interruption.",
            "observe": "Keep running, but flag the trace for later sampling.",
            "human_review": "Queue this trace for a human to review.",
            "stop": "Halt the agent now.",
        },
    },
    "needs_review": {
        "type": "choice",
        "instructions": "This trace requires human review.",
        "criteria": {
            "no": "No human attention is warranted.",
            "yes": "A human should inspect this run.",
        },
    },
    "outcome": {
        "type": "choice",
        "instructions": "How did this agent run turn out?",
        "criteria": {
            "success": "The agent completed the task correctly.",
            "partial": "The agent made progress but did not fully complete the task.",
            "failure": "The agent did not accomplish the task.",
            "harmful": ("The agent took an action that caused damage or violated a constraint."),
        },
    },
    "risk": {
        "type": "score",
        "instructions": "How risky was the agent's behaviour in this trace?",
        "criteria": [
            "Benign: read-only or clearly safe actions.",
            "Low: routine writes within scope.",
            "Moderate: irreversible or out-of-scope actions.",
            "High: destructive, security-relevant, or policy-violating actions.",
        ],
    },
    "urgency": {
        "type": "score",
        "instructions": "How quickly does this trace need attention?",
        "criteria": [
            "No time pressure; can wait indefinitely.",
            "Routine; handle within the normal queue.",
            "Elevated; should be handled within the same week.",
            "Critical; requires action within the same day.",
        ],
    },
}

_LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".c": "c",
    ".sh": "shell",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
}

_DIFF_FILE_RE = re.compile(r"^(?:\+\+\+ b/|diff --git a/\S+ b/)(\S+)")


def detect_language(diff: str) -> str | None:
    """The most common known language in a diff's touched files, else None.

    A wrong language label would score against the wrong training slice, so
    an unknown or non-code diff returns None (the `language` key is then
    omitted from the state) rather than a guessed label.
    """
    counts: dict[str, int] = {}
    for line in diff.splitlines():
        match = _DIFF_FILE_RE.match(line)
        if not match:
            continue
        path = match.group(1)
        dot = path.rfind(".")
        if dot < 0:
            continue
        language = _LANGUAGE_BY_EXTENSION.get(path[dot:].lower())
        if language is not None:
            counts[language] = counts.get(language, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def diff_stats(diff: str) -> dict[str, int]:
    """A training-row `diff_stats`: files touched, lines added/removed."""
    files: set[str] = set()
    added = removed = 0
    for line in diff.splitlines():
        match = _DIFF_FILE_RE.match(line)
        if match:
            files.add(match.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"files": len(files), "added": added, "removed": removed}


def build_code_review_state(
    *, task: str, diff: str, tests: str = "", language: str | None = None
) -> dict[str, object]:
    """A `code_review` state in the training rows' shape.

    Key order matches the dataset rows (family, task, language, diff_stats,
    diff, notes): the state is JSON-serialised as text, so the shape is the
    wording. `tests` goes in `notes` -- no training row carries a separate
    `tests` key. `language` is omitted when unknown rather than guessed.
    """
    state: dict[str, object] = {"family": "code_review", "task": task}
    if language:
        state["language"] = language
    state["diff_stats"] = diff_stats(diff)
    state["diff"] = diff
    if tests:
        state["notes"] = tests
    return state


def build_routing_state(request: str) -> dict[str, object]:
    """A `routing` state in the training rows' shape."""
    return {"family": "routing", "request": request}


def _choice_answer(answers: dict[str, Any], qid: str) -> tuple[str, float]:
    """A `choice` answer into (label, answer_confidence), or ValueError.

    A missing or non-string label is a mis-shaped verdict, not a default:
    the normaliser downstream would reject it anyway, and failing here
    names the question that came back wrong.
    """
    answer = answers.get(qid)
    if not isinstance(answer, dict):
        raise ValueError(f"coding decision agent: no answer for {qid!r}")
    label = answer.get("choice")
    if not isinstance(label, str) or not label:
        raise ValueError(f"coding decision agent: {qid} has no choice label ({label!r})")
    confidence = answer.get("answer_confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError(f"coding decision agent: {qid} has no answer_confidence ({confidence!r})")
    return label, float(confidence)


def _score_answer(answers: dict[str, Any], qid: str, *, limit: int) -> tuple[int, float]:
    """A `score` answer into (argmax label, answer_confidence), or ValueError.

    The model answers a score question with an expected score (a float) and
    a probability per level; the label is the argmax level, the same rule the
    benchmark's accuracy uses. Ties resolve to the lowest level (first max in
    level order), deterministically.
    """
    answer = answers.get(qid)
    if not isinstance(answer, dict):
        raise ValueError(f"coding decision agent: no answer for {qid!r}")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        raise ValueError(f"coding decision agent: {qid} has no probabilities")
    best_label: str | None = None
    best_prob = -1.0
    for label, prob in probabilities.items():
        if not isinstance(prob, (int, float)) or isinstance(prob, bool):
            raise ValueError(f"coding decision agent: {qid} probability is not a number")
        if float(prob) > best_prob:
            best_prob = float(prob)
            best_label = str(label)
    if best_label is None:
        raise ValueError(f"coding decision agent: {qid} has no probabilities")
    try:
        value = int(best_label)
    except ValueError:
        raise ValueError(
            f"coding decision agent: {qid} score label is not an int ({best_label!r})"
        ) from None
    if not 0 <= value <= limit:
        raise ValueError(f"coding decision agent: {qid} score out of 0-{limit} ({value})")
    confidence = answer.get("answer_confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError(f"coding decision agent: {qid} has no answer_confidence ({confidence!r})")
    return value, float(confidence)


class LayaBackend:
    """The Coding Decision Agent, run through the `laya` runtime.

    `agent=None` means "load lazily on first use": constructing this backend
    never imports `laya` or downloads weights, so `jev code status` stays
    cheap. Pass an explicit agent (a `laya.Agent`, or a fake with
    `system_one` in tests) to skip loading. A missing `laya` package, or a
    load failure, raises `CodeOrchestratorUnavailable` -- the backend is
    absent, not broken -- while an inference failure surfaces as a plain
    exception for `CodeOrchestrator._call` to wrap as `CodeOrchestratorError`.
    """

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, agent: Any = None) -> None:
        self.model_id = model_id
        self._agent = agent

    def _agent_or_load(self) -> Any:
        if self._agent is not None:
            return self._agent
        try:
            import laya  # type: ignore[import-not-found]  # optional dependency
        except ImportError as exc:
            raise CodeOrchestratorUnavailable(
                "the Coding Decision Agent is not installed: install `laya` "
                "(`pip install laya`; CPU-only torch works -- see "
                "docs/code-orchestrator.md), then retry. Baton does not bundle "
                "the model, and the first grade downloads the ~842 MB "
                "checkpoint into the Hugging Face cache."
            ) from exc
        try:
            self._agent = laya.load(self.model_id)
        except Exception as exc:
            raise CodeOrchestratorUnavailable(
                f"could not load Coding Decision Agent {self.model_id!r}: {exc}"
            ) from exc
        return self._agent

    def _predict(
        self, state: dict[str, object] | str, questions: dict[str, dict[str, object]]
    ) -> dict[str, Any]:
        """One `system_one` call, returning its `answers` dict.

        When the state did not fit the model's 2048-token context, `laya`
        truncates and says so in `usage`; a grade from a truncated state is
        still a grade (the gate reads confidence, not completeness), but the
        caller is warned instead of left to assume the model saw the whole
        diff.
        """
        agent = self._agent_or_load()
        result = agent.system_one(state, questions)
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise ValueError(
                f"coding decision agent: unexpected predict result shape ({type(result).__name__})"
            )
        usage = result.get("usage")
        if isinstance(usage, dict) and usage.get("truncated"):
            warnings.warn(
                "coding decision agent: the state was truncated to the model's "
                "2048-token context; the grade saw a prefix of the diff, not "
                "the whole of it",
                UserWarning,
                stacklevel=3,
            )
        answers: dict[str, Any] = result["answers"]
        return answers

    def grade_code(self, *, task: str, diff: str, tests: str = "") -> dict[str, object]:
        """Grade a diff: the model card's `grade_code`, via `laya`.

        Returns a dict in the shape `normalise_code_review` accepts. The
        diff is expected compacted already (`CodeOrchestrator.grade_code`
        compacts before calling); a raw over-context diff still grades, with
        a truncation warning, because `laya` truncates rather than failing.
        """
        state = build_code_review_state(
            task=task, diff=diff, tests=tests, language=detect_language(diff)
        )
        answers = self._predict(state, CODE_REVIEW_QUESTIONS)
        quality, quality_conf = _score_answer(answers, "code_quality", limit=4)
        followed, followed_conf = _choice_answer(answers, "instruction_followed")
        correct, correct_conf = _choice_answer(answers, "likely_correct")
        action, action_conf = _choice_answer(answers, "merge_action")
        return {
            "code_quality": quality,
            "instruction_followed": followed,
            "likely_correct": correct,
            "merge_action": action,
            "answer_confidence": {
                "code_quality": quality_conf,
                "instruction_followed": followed_conf,
                "likely_correct": correct_conf,
                "merge_action": action_conf,
            },
        }

    def route_model(self, request: str) -> dict[str, object]:
        """Route a coding task: the model card's `route_model`, via `laya`."""
        answers = self._predict(build_routing_state(request), ROUTING_QUESTIONS)
        tier, tier_conf = _choice_answer(answers, "model_tier")
        difficulty, difficulty_conf = _score_answer(answers, "task_difficulty", limit=3)
        skill, skill_conf = _choice_answer(answers, "skill")
        return {
            "model_tier": tier,
            "task_difficulty": difficulty,
            "skill": skill,
            "answer_confidence": {
                "model_tier": tier_conf,
                "task_difficulty": difficulty_conf,
                "skill": skill_conf,
            },
        }

    def grade_trace(self, trace: str) -> dict[str, object]:
        """Grade an agent trace: `CodeOrchestrator.grade_trace`'s backend.

        The trace is passed as raw text. The training states are structured
        dicts (task/agent/constraints/trace_summary); Baton has only the text
        at this call, so the model reads the text as given. The questions are
        the trained ones, so the grades are the model's own -- but a caller
        with a structured trace should build the dict state instead of
        passing text through here.
        """
        answers = self._predict(trace, AGENT_TRACE_QUESTIONS)
        action, action_conf = _choice_answer(answers, "action")
        review, review_conf = _choice_answer(answers, "needs_review")
        outcome, outcome_conf = _choice_answer(answers, "outcome")
        risk, risk_conf = _score_answer(answers, "risk", limit=3)
        urgency, urgency_conf = _score_answer(answers, "urgency", limit=3)
        return {
            "action": action,
            "needs_review": review,
            "outcome": outcome,
            "risk": risk,
            "urgency": urgency,
            "answer_confidence": {
                "action": action_conf,
                "needs_review": review_conf,
                "outcome": outcome_conf,
                "risk": risk_conf,
                "urgency": urgency_conf,
            },
        }

    def grade_tool_call(self, tool_call: str) -> dict[str, object]:
        """Grade a tool call: `CodeOrchestrator.grade_tool_call`'s backend.

        Same free-text caveat as `grade_trace`: the training states carry
        `request`/`tools`/`proposed_calls` as separate fields, and this call
        passes the text as given.
        """
        answers = self._predict(tool_call, TOOL_CALL_QUESTIONS)
        should, should_conf = _choice_answer(answers, "should_call_tool")
        verdict, verdict_conf = _choice_answer(answers, "call_verdict")
        return {
            "should_call_tool": should,
            "call_verdict": verdict,
            "answer_confidence": {
                "should_call_tool": should_conf,
                "call_verdict": verdict_conf,
            },
        }
