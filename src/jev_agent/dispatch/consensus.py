"""Consensus dispatcher: Jev votes first, Laya second, and both must be confident (issue #66).

Laya is Jev inside the Laya CDA decision matrix: every allowed answer is scored 1-5 on six
weighted criteria (`[dispatcher.laya] weights`), and the highest weighted total is Laya's
pick. Laya is asked separately from Jev and never sees Jev's vote in the first round.

A question is settled when all of these hold:
- Jev's choice has at least `[dispatcher.jev] min_confidence`;
- Laya ranks the same answer first;
- Laya's lead stays at least `min_margin` after every uncertain score (confidence below
  `min_confidence`) is moved one level against it.

Unsettled questions are asked again, with the earlier rounds in `state.previous_rounds`:
Jev votes again, and Laya re-scores the uncertain cells (a new score replaces the old one
when it is at least as confident). After `follow_up_rounds` extra rounds the dispatcher
raises `NoConsensus`, and the run stops for a human. It never falls back to rules.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from jev_agent.config.schema import LAYA_CRITERIA, LayaSettings
from jev_agent.core.models import Decision, DecisionRequest
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.jev import PROVIDER_KEY, JevDispatcher, cost_from_usage, parse_answers

__all__ = ["LEVELS", "ConsensusDispatcher", "NoConsensus", "Score", "Verdict", "evaluate"]

# Laya's 1-5 scale; Jev's `score` answer is the level index (0..4, may fall between levels).
LEVELS = ["1: very poor", "2: poor", "3: acceptable", "4: good", "5: excellent"]
LOWEST, HIGHEST = 1.0, float(len(LEVELS))

FOLLOW_UP_NOTE = (
    "Earlier rounds did not settle this (see `previous_rounds` and their reasons); "
    "decide again with that in mind."
)

Cell = tuple[str, str]  # (answer option, criterion)


class NoConsensus(DispatchError):
    """Jev and Laya did not agree confidently within the allowed rounds."""


@dataclass(frozen=True)
class Score:
    value: float  # 1..5
    confidence: float


@dataclass(frozen=True)
class Verdict:
    """Both votes on one question, and why it is not settled (empty when it is)."""

    jev_choice: str
    jev_confidence: float
    laya_choice: str
    totals: dict[str, float]  # weighted total per option, 1..5
    margin: float  # Laya's lead after moving uncertain scores against it
    uncertain: tuple[Cell, ...]
    reasons: tuple[str, ...]

    @property
    def settled(self) -> bool:
        return not self.reasons

    def summary(self) -> dict[str, Any]:
        return {
            "jev": {"choice": self.jev_choice, "confidence": round(self.jev_confidence, 3)},
            "laya": {
                "choice": self.laya_choice,
                "totals": {o: round(t, 3) for o, t in self.totals.items()},
                "margin": round(self.margin, 3),
            },
            "reasons": list(self.reasons),
        }


def evaluate(
    options: list[str],
    jev: dict[str, Any],
    scores: dict[Cell, Score],
    weights: dict[str, float],
    *,
    min_confidence: float,
    min_margin: float,
) -> Verdict:
    """Apply the consensus rules to one question's votes. Pure, so the rules are testable."""
    totals = {o: sum(w * scores[(o, c)].value for c, w in weights.items()) for o in options}
    laya_choice = max(options, key=lambda o: totals[o])  # ties go to the earlier option
    uncertain = tuple(
        (o, c) for o in options for c in weights if scores[(o, c)].confidence < min_confidence
    )
    worst = dict(totals)
    for o, c in uncertain:
        value = scores[(o, c)].value
        if o == laya_choice:
            worst[o] -= weights[c] * min(1.0, value - LOWEST)
        else:
            worst[o] += weights[c] * min(1.0, HIGHEST - value)
    margin = min(worst[laya_choice] - worst[o] for o in options if o != laya_choice)

    reasons: list[str] = []
    confidence = float(jev["confidence"])
    if confidence < min_confidence:
        reasons.append(f"Jev's confidence {confidence:.2f} is below {min_confidence:.2f}")
    if jev["choice"] != laya_choice:
        reasons.append(f"Jev picked {jev['choice']}, Laya ranked {laya_choice} first")
    if margin < min_margin:
        reasons.append(
            f"Laya's lead for {laya_choice} is not robust ({margin:.2f} < {min_margin:g})"
        )
    return Verdict(
        jev_choice=jev["choice"],
        jev_confidence=confidence,
        laya_choice=laya_choice,
        totals=totals,
        margin=margin,
        uncertain=uncertain,
        reasons=tuple(reasons),
    )


class ConsensusDispatcher:
    """`Dispatcher` that needs both Jev and Laya to agree confidently on every answer.

    `Decision.details` holds `direct`, Jev's final `answers` (as `JevDispatcher` stores them),
    Laya's final vote per question (`laya`, with the final 1-5 `scores` per answer and
    criterion, which `jev laya tune` learns from), the `weights` used, the reasons of every
    unsettled round (`rounds`), the summed `usage`, and `provider`: which Jev provider
    served the decision (`"native"`, `"openrouter"`, or `"mixed"` when both served calls).
    Pass a `MultiProviderJevDispatcher` as `jev` so neither vote depends on one provider.
    """

    name = "consensus"

    def __init__(self, jev: JevDispatcher, laya: LayaSettings | None = None) -> None:
        self.jev = jev
        self.laya = laya or LayaSettings()

    def with_weights(self, weights: dict[str, float]) -> ConsensusDispatcher:
        """The same dispatcher with other matrix weights (a learning profile's tuned ones)."""
        laya = LayaSettings.model_validate({**self.laya.model_dump(), "weights": weights})
        return type(self)(self.jev, laya)

    async def decide(self, req: DecisionRequest) -> Decision:
        started = time.monotonic()
        direct = {q: allowed[0] for q, allowed in req.questions.items() if len(allowed) == 1}
        asked = {q: allowed for q, allowed in req.questions.items() if len(allowed) > 1}
        details: dict[str, Any] = {
            "direct": sorted(direct),
            "answers": {},
            "laya": {},
            "rounds": [],
            "usage": None,
        }
        if not asked:
            return Decision(
                point=req.point,
                answers=direct,
                dispatcher=self.name,
                cost_usd=0.0,
                latency_ms=0,
                details=details,
            )

        min_confidence = self.jev.settings.min_confidence
        jev_votes: dict[str, dict[str, Any]] = {}
        scores: dict[str, dict[Cell, Score]] = {q: {} for q in asked}
        verdicts: dict[str, Verdict] = {}
        usage = _Usage()
        providers: set[str] = set()  # every Jev provider that served a call below
        model: str | None = None
        unsettled = list(asked)
        for round_no in range(1, self.laya.follow_up_rounds + 2):
            state = dict(req.state)
            if details["rounds"]:
                state["previous_rounds"] = details["rounds"]
            questions = {q: asked[q] for q in unsettled}

            # Jev votes first.
            payload = self.jev.build_payload(req.point, state, questions)
            if round_no > 1:
                for question in payload["questions"].values():
                    question["instructions"] = f"{question['instructions']} {FOLLOW_UP_NOTE}"
            body = await self.jev.post(payload)
            _note_provider(body, providers)
            usage.add(body)
            model = body.get("model") if isinstance(body.get("model"), str) else model
            jev_votes.update(parse_answers(body, questions)[1])

            # Laya second: score the cells that are new or still uncertain.
            cells = {
                q: _cells_to_score(q, asked[q], verdicts.get(q), self.laya.weights)
                for q in unsettled
            }
            laya_payload = _laya_payload(payload, cells)
            body = await self.jev.post(laya_payload)
            _note_provider(body, providers)
            usage.add(body)
            for q, new in _parse_scores(body, laya_payload["questions"], cells).items():
                for cell, score in new.items():
                    old = scores[q].get(cell)
                    if old is None or score.confidence >= old.confidence:
                        scores[q][cell] = score

            for q in unsettled:
                verdicts[q] = evaluate(
                    asked[q],
                    jev_votes[q],
                    scores[q],
                    self.laya.weights,
                    min_confidence=min_confidence,
                    min_margin=self.laya.min_margin,
                )
            unsettled = [q for q in unsettled if not verdicts[q].settled]
            if unsettled:
                details["rounds"].append(
                    {"round": round_no, **{q: verdicts[q].summary() for q in unsettled}}
                )
            if not unsettled:
                break
        else:
            raise NoConsensus(_no_consensus_message(verdicts, round_no))

        details["answers"] = jev_votes
        details["laya"] = {
            q: {**v.summary()["laya"], "scores": _score_table(scores[q])}
            for q, v in verdicts.items()
        }
        details["weights"] = dict(self.laya.weights)
        details["usage"] = usage.as_dict()
        if providers:
            # Usually one provider served every call; "mixed" when the votes split.
            details["provider"] = next(iter(providers)) if len(providers) == 1 else "mixed"
        return Decision(
            point=req.point,
            answers={**direct, **{q: v.jev_choice for q, v in verdicts.items()}},
            dispatcher=self.name,
            model=model or self.jev.settings.model,
            cost_usd=cost_from_usage(details["usage"]),
            latency_ms=int((time.monotonic() - started) * 1000),
            details=details,
        )


def _cells_to_score(
    question: str, options: list[str], verdict: Verdict | None, weights: dict[str, float]
) -> list[Cell]:
    """Every cell in the first round; afterwards the uncertain ones, or (when every score was
    confident) every cell of Jev's pick, Laya's pick and Laya's runner-up."""
    if verdict is None:
        return [(o, c) for o in options for c in weights]
    if verdict.uncertain:
        return list(verdict.uncertain)
    ranked = sorted(options, key=lambda o: -verdict.totals[o])
    disputed = dict.fromkeys([verdict.jev_choice, *ranked[:2]])
    return [(o, c) for o in disputed for c in weights]


def _laya_payload(jev_payload: dict[str, Any], cells: dict[str, list[Cell]]) -> dict[str, Any]:
    """One `score` question per cell, built from the question Jev was asked."""
    questions: dict[str, Any] = {}
    for q, q_cells in cells.items():
        asked = jev_payload["questions"][q]
        for option, criterion in q_cells:
            description = asked["criteria"].get(option)
            candidate = f"{option} ({description})" if description else option
            questions[f"laya.{len(questions)}"] = {
                "type": "score",
                "instructions": (
                    f"{asked['instructions']}\nCandidate answer to `{q}`: {candidate}\n"
                    f"Rate this candidate on {LAYA_CRITERIA[criterion]}"
                ),
                "criteria": LEVELS,
            }
    return {"state": jev_payload["state"], "model": jev_payload["model"], "questions": questions}


def _parse_scores(
    body: dict[str, Any], questions: dict[str, Any], cells: dict[str, list[Cell]]
) -> dict[str, dict[Cell, Score]]:
    raw = body.get("answers")
    if not isinstance(raw, dict):
        raise DispatchError("laya: response has no answers")
    ids = iter(questions)
    parsed: dict[str, dict[Cell, Score]] = {}
    for q, q_cells in cells.items():
        for cell in q_cells:
            qid = next(ids)
            answer = raw.get(qid)
            score = answer.get("score") if isinstance(answer, dict) else None
            confidence = answer.get("confidence") if isinstance(answer, dict) else None
            if not _is_number(score) or not _is_number(confidence):
                raise DispatchError(f"laya: no score for {q}={cell[0]} on {cell[1]}")
            assert isinstance(score, int | float) and isinstance(confidence, int | float)
            value = min(max(float(score), 0.0), HIGHEST - LOWEST) + LOWEST
            parsed.setdefault(q, {})[cell] = Score(value, float(confidence))
    return parsed


def _score_table(scores: dict[Cell, Score]) -> dict[str, dict[str, float]]:
    """{answer: {criterion: 1-5 score}}, the final values of every scored cell."""
    table: dict[str, dict[str, float]] = {}
    for (option, criterion), score in scores.items():
        table.setdefault(option, {})[criterion] = round(score.value, 3)
    return table


def _is_number(value: object) -> bool:
    """A finite int or float (not a bool, NaN or infinity)."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _no_consensus_message(verdicts: dict[str, Verdict], rounds: int) -> str:
    # A reason may contain a comma ("Jev picked a, Laya ranked b first"), so reasons are
    # joined with "; " and questions with ". ".
    unsettled = ". ".join(
        f"{q}: {'; '.join(v.reasons)}" for q, v in verdicts.items() if not v.settled
    )
    return f"Jev and Laya did not agree after {rounds} round(s): {unsettled}"


def _note_provider(body: dict[str, Any], providers: set[str]) -> None:
    """Record which Jev provider answered one `post` call (`MultiProviderJevDispatcher`
    marks the body; a plain `JevDispatcher` marks nothing)."""
    provider = body.get(PROVIDER_KEY)
    if isinstance(provider, str):
        providers.add(provider)


class _Usage:
    """Token counts (and OpenRouter's cost, when given) summed over every call."""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost: float | None = None

    def add(self, body: dict[str, Any]) -> None:
        self.calls += 1
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return
        for name in ("input_tokens", "output_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, name, getattr(self, name) + value)
        cost = usage.get("cost")
        if _is_number(cost):
            assert isinstance(cost, int | float)
            self.cost = (self.cost or 0.0) + float(cost)

    def as_dict(self) -> dict[str, Any]:
        usage: dict[str, Any] = {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
        if self.cost is not None:
            usage["cost"] = self.cost
        return usage
