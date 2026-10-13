import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jev_agent.config.schema import LAYA_CRITERIA, LayaSettings
from jev_agent.core.models import DecisionRequest
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.consensus import (
    FOLLOW_UP_NOTE,
    LEVELS,
    ConsensusDispatcher,
    NoConsensus,
    Score,
    evaluate,
)
from jev_agent.dispatch.jev import PRICE_PER_INPUT_TOKEN_USD, JevDispatcher, JevSettings

KEY = "sk-or-test-SECRET-1234567890"
MODEL = "typesafe/jev-1.13-20260917"
EFFORT = ["low", "medium", "high"]
WORKER = ["claude_code", "codex"]
JEV_USAGE = {"input_tokens": 100, "output_tokens": 10, "cost": 0.001}
LAYA_USAGE = {"input_tokens": 200, "output_tokens": 20, "cost": 0.002}

Votes = dict[str, tuple[str, float]]  # question -> (choice, confidence)
# (round, question, option, criterion) -> (level 1..5, confidence)
ScoreFn = Callable[[int, str, str, str], tuple[float, float]]
Cell = tuple[str, str, str]  # (question, option, criterion)


def cell_of(question: dict[str, Any]) -> Cell:
    """Which (question, option, criterion) a Laya `score` question asks about."""
    match = re.search(r"Candidate answer to `(\w+)`: (\S+)", question["instructions"])
    assert match, question["instructions"]
    criterion = next(
        c for c, text in LAYA_CRITERIA.items() if question["instructions"].endswith(text)
    )
    return match[1], match[2], criterion


def levels(table: dict[str, float], confidence: float = 0.9) -> ScoreFn:
    """Laya gives every criterion of an option the same level, with one confidence."""
    return lambda _round, _question, option, _criterion: (table[option], confidence)


class Server:
    """A MockTransport handler playing System One: `choice` requests are Jev's vote (one
    `Votes` per round), `score` requests are Laya's matrix (from `score`)."""

    def __init__(
        self,
        votes: list[Votes],
        score: ScoreFn,
        *,
        mangle: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.votes = votes
        self.score = score
        self.mangle = mangle
        self.jev_usage: Any = JEV_USAGE
        self.laya_usage: Any = LAYA_USAGE
        self.requests: list[dict[str, Any]] = []

    @property
    def jev(self) -> list[dict[str, Any]]:
        return [p for p in self.requests if _kind(p) == "choice"]

    @property
    def laya(self) -> list[dict[str, Any]]:
        return [p for p in self.requests if _kind(p) == "score"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        round_no = len(self.jev)
        answers: Any
        if _kind(payload) == "choice":
            vote = self.votes[round_no - 1]
            answers = {
                name: {
                    "type": "choice",
                    "choice": vote[name][0],
                    "confidence": vote[name][1],
                    "probabilities": {vote[name][0]: vote[name][1]},
                }
                for name in payload["questions"]
            }
            usage = self.jev_usage
        else:
            answers = {}
            for qid, question in payload["questions"].items():
                level, confidence = self.score(round_no, *cell_of(question))
                answers[qid] = {"type": "score", "score": level - 1, "confidence": confidence}
            if self.mangle:
                answers = self.mangle(answers)
            usage = self.laya_usage
        return httpx.Response(200, json={"model": MODEL, "answers": answers, "usage": usage})


def _kind(payload: dict[str, Any]) -> str:
    kinds = {q["type"] for q in payload["questions"].values()}
    assert len(kinds) == 1, kinds
    return str(kinds.pop())


def _consensus(server: Server, laya: LayaSettings | None = None) -> ConsensusDispatcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return ConsensusDispatcher(JevDispatcher(JevSettings(), client=client), laya)


def _request(**questions: list[str]) -> DecisionRequest:
    return DecisionRequest(point="D4", state={"task": {"id": "T1"}}, questions=questions)


def _cells(payload: dict[str, Any]) -> set[Cell]:
    return {cell_of(q) for q in payload["questions"].values()}


def _all_cells(question: str, options: list[str]) -> set[Cell]:
    return {(question, o, c) for o in options for c in LAYA_CRITERIA}


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)


# --- evaluate: the consensus rules on one question ---------------------------------------

TWO = {"correctness": 0.5, "safety": 0.5}


def _grid(rows: dict[str, list[tuple[float, float]]]) -> dict[tuple[str, str], Score]:
    """(value, confidence) per criterion of `TWO`, for each option."""
    return {
        (option, criterion): Score(value, confidence)
        for option, row in rows.items()
        for criterion, (value, confidence) in zip(TWO, row, strict=True)
    }


def _evaluate(
    rows: dict[str, list[tuple[float, float]]],
    choice: str,
    confidence: float = 0.9,
    *,
    min_margin: float = 0.2,
) -> Any:
    return evaluate(
        list(rows),
        {"choice": choice, "confidence": confidence},
        _grid(rows),
        TWO,
        min_confidence=0.55,
        min_margin=min_margin,
    )


def test_evaluate_settles_when_both_agree_confidently() -> None:
    verdict = _evaluate({"a": [(4, 0.9), (4, 0.9)], "b": [(2, 0.9), (3, 0.9)]}, "a")
    assert verdict.settled
    assert verdict.reasons == ()
    assert verdict.laya_choice == "a"
    assert verdict.totals == {"a": 4.0, "b": 2.5}
    assert verdict.margin == pytest.approx(1.5)
    assert verdict.uncertain == ()


def test_evaluate_rejects_low_jev_confidence() -> None:
    verdict = _evaluate({"a": [(4, 0.9), (4, 0.9)], "b": [(2, 0.9), (3, 0.9)]}, "a", 0.5)
    assert not verdict.settled
    assert verdict.reasons == ("Jev's confidence 0.50 is below 0.55",)


def test_evaluate_rejects_disagreement() -> None:
    verdict = _evaluate({"a": [(4, 0.9), (4, 0.9)], "b": [(2, 0.9), (3, 0.9)]}, "b")
    assert verdict.reasons == ("Jev picked b, Laya ranked a first",)
    assert verdict.jev_choice == "b"
    assert verdict.laya_choice == "a"


def test_evaluate_tie_goes_to_the_earlier_option() -> None:
    tied = [(3.0, 0.9), (3.0, 0.9)]
    assert _evaluate({"a": tied, "b": tied}, "a", min_margin=0).laya_choice == "a"
    assert _evaluate({"b": tied, "a": tied}, "b", min_margin=0).laya_choice == "b"
    # A tie leaves no lead, so it settles only when no margin is required.
    assert _evaluate({"a": tied, "b": tied}, "a", min_margin=0).settled
    assert _evaluate({"a": tied, "b": tied}, "a").reasons == (
        "Laya's lead for a is not robust (0.00 < 0.2)",
    )


def test_evaluate_moves_uncertain_scores_one_level_against_laya() -> None:
    # Totals a 3.5, b 2.5; the uncertain a/safety drops to 1, b/correctness rises to 4.
    verdict = _evaluate({"a": [(5, 0.9), (2, 0.3)], "b": [(3, 0.3), (2, 0.9)]}, "a")
    assert verdict.uncertain == (("a", "safety"), ("b", "correctness"))
    assert verdict.margin == pytest.approx(0.0)
    assert verdict.reasons == ("Laya's lead for a is not robust (0.00 < 0.2)",)


def test_evaluate_clamps_the_shift_at_1_and_5() -> None:
    # a/safety 1.4 can drop only to 1 (not 0.4); b/correctness 4.6 can rise only to 5 (not 5.6).
    verdict = _evaluate({"a": [(5, 0.9), (1.4, 0.3)], "b": [(4.6, 0.3), (1.6, 0.9)]}, "a")
    assert verdict.totals == {"a": pytest.approx(3.2), "b": pytest.approx(3.1)}
    assert verdict.margin == pytest.approx(3.0 - 3.3)
    # Already at the bounds: an uncertain 1 on the winner and 5 on the loser do not move.
    edge = _evaluate({"a": [(5, 0.9), (1, 0.3)], "b": [(5, 0.3), (1, 0.9)]}, "a", min_margin=0)
    assert edge.margin == pytest.approx(0.0)
    assert edge.settled


def test_evaluate_rejects_a_margin_below_min_margin() -> None:
    verdict = _evaluate({"a": [(4, 0.9), (3, 0.9)], "b": [(4, 0.9), (2.8, 0.9)]}, "a")
    assert verdict.uncertain == ()
    assert verdict.margin == pytest.approx(0.1)
    assert verdict.reasons == ("Laya's lead for a is not robust (0.10 < 0.2)",)


# --- ConsensusDispatcher.decide ----------------------------------------------------------

EFFORT_LEVELS = {"low": 2, "medium": 4, "high": 3}
WORKER_LEVELS = {"claude_code": 4, "codex": 2}


def _both(round_no: int, question: str, option: str, criterion: str) -> tuple[float, float]:
    table = EFFORT_LEVELS if question == "effort" else WORKER_LEVELS
    return table[option], 0.9


async def test_questions_with_one_allowed_answer_need_no_request() -> None:
    server = Server([], _both)
    decision = await _consensus(server).decide(_request(worker=["codex"], effort=["low"]))
    assert server.requests == []
    assert decision.answers == {"worker": "codex", "effort": "low"}
    assert decision.dispatcher == "consensus"
    assert decision.cost_usd == 0.0
    assert decision.details == {
        "direct": ["effort", "worker"],
        "answers": {},
        "laya": {},
        "rounds": [],
        "usage": None,
    }


async def test_jev_votes_first_then_laya_scores_every_cell_without_jevs_vote() -> None:
    server = Server([{"effort": ("medium", 0.8)}], _both)
    decision = await _consensus(server).decide(_request(effort=EFFORT))
    assert decision.answers == {"effort": "medium"}

    assert [_kind(p) for p in server.requests] == ["choice", "score"]
    jev, laya = server.requests
    assert set(jev["questions"]) == {"effort"}
    assert len(laya["questions"]) == len(EFFORT) * len(LAYA_CRITERIA)
    assert _cells(laya) == _all_cells("effort", EFFORT)
    for question in laya["questions"].values():
        assert question["criteria"] == LEVELS
        assert FOLLOW_UP_NOTE not in question["instructions"]
    # Laya sees what Jev was asked, never what Jev answered.
    assert laya["state"] == jev["state"]
    assert "previous_rounds" not in laya["state"]
    assert laya["model"] == jev["model"]


async def test_follow_up_reasks_only_unsettled_questions_with_previous_rounds() -> None:
    server = Server(
        [
            {"effort": ("medium", 0.8), "worker": ("codex", 0.9)},
            {"worker": ("claude_code", 0.9)},
        ],
        _both,
    )
    decision = await _consensus(server).decide(_request(effort=EFFORT, worker=WORKER))
    assert decision.answers == {"effort": "medium", "worker": "claude_code"}

    assert [_kind(p) for p in server.requests] == ["choice", "score"] * 2
    jev_1, jev_2 = server.jev
    assert set(jev_1["questions"]) == {"effort", "worker"}
    assert set(jev_2["questions"]) == {"worker"}
    assert {q for q, _, _ in _cells(server.laya[1])} == {"worker"}

    # The earlier round goes into the state, and the note points both voters at it.
    rounds = decision.details["rounds"]
    assert rounds == [{"round": 1, "worker": rounds[0]["worker"]}]
    assert rounds[0]["worker"]["jev"] == {"choice": "codex", "confidence": 0.9}
    assert rounds[0]["worker"]["reasons"] == ["Jev picked codex, Laya ranked claude_code first"]
    assert "previous_rounds" not in jev_1["state"]
    for payload in server.requests[2:]:
        assert payload["state"]["previous_rounds"] == rounds
        for question in payload["questions"].values():
            assert FOLLOW_UP_NOTE in question["instructions"]
    assert jev_2["questions"]["worker"]["instructions"].endswith(FOLLOW_UP_NOTE)


def _uncertain_round_one(
    round_no: int, question: str, option: str, criterion: str
) -> tuple[float, float]:
    """Round 1: three uncertain cells. Round 2: one less, one equally and one more confident."""
    second = {
        ("high", "safety"): (5, 0.2),  # less confident than 0.3: the old 3 stays
        ("medium", "tool_fit"): (3, 0.3),  # as confident: the new 3 replaces 4
        ("low", "simplicity"): (1, 0.8),  # more confident: the new 1 replaces 2
    }
    if round_no == 2:
        return second[(option, criterion)]
    return EFFORT_LEVELS[option], 0.3 if (option, criterion) in second else 0.9


async def test_follow_up_rescores_only_the_uncertain_cells() -> None:
    server = Server(
        [{"effort": ("medium", 0.4)}, {"effort": ("medium", 0.8)}], _uncertain_round_one
    )
    decision = await _consensus(server).decide(_request(effort=EFFORT))
    assert decision.answers == {"effort": "medium"}
    assert decision.details["rounds"][0]["effort"]["reasons"] == [
        "Jev's confidence 0.40 is below 0.55"
    ]
    assert _cells(server.laya[1]) == {
        ("effort", "low", "simplicity"),
        ("effort", "medium", "tool_fit"),
        ("effort", "high", "safety"),
    }


async def test_follow_up_keeps_the_more_confident_score() -> None:
    server = Server(
        [{"effort": ("medium", 0.4)}, {"effort": ("medium", 0.8)}], _uncertain_round_one
    )
    decision = await _consensus(server).decide(_request(effort=EFFORT))
    totals = decision.details["laya"]["effort"]["totals"]
    assert totals["high"] == pytest.approx(3.0)  # kept the old, more confident score
    assert totals["medium"] == pytest.approx(4.0 - 0.10)  # tool_fit 4 -> 3
    assert totals["low"] == pytest.approx(2.0 - 0.15)  # simplicity 2 -> 1


async def test_follow_up_without_uncertain_cells_rescores_the_disputed_options() -> None:
    models = ["m/a", "m/b", "m/c", "m/d"]
    table = {"m/a": 3, "m/b": 5, "m/c": 4, "m/d": 1}  # Laya: b, then runner-up c
    server = Server(
        [{"model": ("m/d", 0.9)}, {"model": ("m/b", 0.9)}],
        lambda _r, _q, option, _c: (table[option], 0.9),
    )
    decision = await _consensus(server).decide(_request(model=models))
    assert decision.answers == {"model": "m/b"}
    # Jev's pick d, Laya's pick b and the runner-up c; never a.
    assert _cells(server.laya[1]) == _all_cells("model", ["m/d", "m/b", "m/c"])


@pytest.mark.parametrize("follow_up_rounds", [0, 1, 2])
async def test_no_consensus_after_the_last_follow_up_round(follow_up_rounds: int) -> None:
    rounds = follow_up_rounds + 1
    server = Server(
        [{"effort": ("high", 0.4), "worker": ("codex", 0.9)}]
        + [{"effort": ("high", 0.4), "worker": ("codex", 0.9)}] * follow_up_rounds,
        _both,
    )
    laya = LayaSettings(follow_up_rounds=follow_up_rounds)
    with pytest.raises(NoConsensus) as exc:
        await _consensus(server, laya).decide(_request(effort=EFFORT, worker=WORKER))
    assert len(server.requests) == 2 * rounds
    assert str(exc.value) == (
        f"Jev and Laya did not agree after {rounds} round(s): "
        "effort: Jev's confidence 0.40 is below 0.55; Jev picked high, Laya ranked medium first. "
        "worker: Jev picked codex, Laya ranked claude_code first"
    )
    assert isinstance(exc.value, DispatchError)


def _drop_first(answers: dict[str, Any]) -> dict[str, Any]:
    return dict(list(answers.items())[1:])


def _set_first(**fields: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def mangle(answers: dict[str, Any]) -> dict[str, Any]:
        first = next(iter(answers))
        answers[first] = {**answers[first], **fields}
        return answers

    return mangle


def _first_is(value: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def mangle(answers: dict[str, Any]) -> dict[str, Any]:
        answers[next(iter(answers))] = value
        return answers

    return mangle


@pytest.mark.parametrize(
    ("mangle", "message"),
    [
        (lambda _answers: None, "laya: response has no answers"),
        (lambda _answers: ["not", "a", "map"], "laya: response has no answers"),
        (_drop_first, "laya: no score for effort=low on correctness"),
        (_first_is("4"), "laya: no score for effort=low on correctness"),
        (_set_first(score=True), "laya: no score for effort=low on correctness"),
        (_set_first(score="3"), "laya: no score for effort=low on correctness"),
        (_set_first(confidence=None), "laya: no score for effort=low on correctness"),
    ],
)
async def test_bad_laya_answers_raise(
    mangle: Callable[[dict[str, Any]], Any], message: str
) -> None:
    server = Server([{"effort": ("medium", 0.8)}], _both, mangle=mangle)
    with pytest.raises(DispatchError) as exc:
        await _consensus(server).decide(_request(effort=EFFORT))
    assert type(exc.value) is DispatchError
    assert str(exc.value) == message


async def test_usage_and_cost_are_summed_over_every_call() -> None:
    server = Server([{"effort": ("medium", 0.4)}, {"effort": ("medium", 0.8)}], _both)
    decision = await _consensus(server).decide(_request(effort=EFFORT))
    usage = decision.details["usage"]
    assert usage == {
        "calls": 4,
        "input_tokens": 600,
        "output_tokens": 60,
        "cost": pytest.approx(0.006),
    }
    assert decision.cost_usd == pytest.approx(0.006)


async def test_cost_falls_back_to_summed_input_tokens() -> None:
    server = Server([{"effort": ("medium", 0.8)}], _both)
    server.jev_usage = {"input_tokens": 100, "output_tokens": 10}
    server.laya_usage = {"input_tokens": 200, "output_tokens": 20}
    decision = await _consensus(server).decide(_request(effort=EFFORT))
    assert decision.details["usage"] == {"calls": 2, "input_tokens": 300, "output_tokens": 30}
    assert decision.cost_usd == pytest.approx(300 * PRICE_PER_INPUT_TOKEN_USD)


async def test_details_shape() -> None:
    server = Server([{"effort": ("medium", 0.8)}], _both)
    decision = await _consensus(server).decide(_request(effort=EFFORT, worker=["codex"]))
    assert "worker" not in server.jev[0]["questions"]
    assert decision.answers == {"worker": "codex", "effort": "medium"}
    assert decision.model == MODEL
    assert decision.dispatcher == "consensus"
    assert decision.latency_ms >= 0
    details = decision.details
    assert set(details) == {"direct", "answers", "laya", "weights", "rounds", "usage"}
    assert details["direct"] == ["worker"]
    assert details["answers"] == {
        "effort": {"choice": "medium", "confidence": 0.8, "probabilities": {"medium": 0.8}}
    }
    assert details["laya"] == {
        "effort": {
            "choice": "medium",
            "totals": {"low": 2.0, "medium": 4.0, "high": 3.0},
            "margin": 1.0,
            "scores": {
                option: dict.fromkeys(LAYA_CRITERIA, level)
                for option, level in {"low": 2.0, "medium": 4.0, "high": 3.0}.items()
            },
        }
    }
    assert details["weights"] == LayaSettings().weights
    assert details["rounds"] == []
    assert details["usage"] == {
        "calls": 2,
        "input_tokens": 300,
        "output_tokens": 30,
        "cost": pytest.approx(0.003),
    }


async def test_with_weights_scores_with_the_given_weights() -> None:
    # Two criteria disagree: "low" is safest, "high" most correct. Weights decide the winner.
    def split(_round: int, _question: str, option: str, criterion: str) -> tuple[float, float]:
        if criterion == "safety":
            return {"low": 5.0, "medium": 3.0, "high": 1.0}[option], 0.9
        if criterion == "correctness":
            return {"low": 1.0, "medium": 3.0, "high": 5.0}[option], 0.9
        return 3.0, 0.9

    weights = dict.fromkeys(LAYA_CRITERIA, 0.0) | {"safety": 0.6, "correctness": 0.4}
    dispatcher = _consensus(Server([{"effort": ("low", 0.9)}], split)).with_weights(weights)
    decision = await dispatcher.decide(_request(effort=EFFORT))
    assert decision.answers == {"effort": "low"}
    assert decision.details["weights"] == weights
    assert decision.details["laya"]["effort"]["scores"]["high"]["safety"] == 1.0


def test_with_weights_validates_the_weights() -> None:
    dispatcher = _consensus(Server([], _both))
    with pytest.raises(ValueError, match="add up to 1"):
        dispatcher.with_weights(dict.fromkeys(LAYA_CRITERIA, 0.5))
