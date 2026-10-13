"""Decide with Jev (native TypeSafe System One API); Jev makes the final call.

Round 1, two passes in one request:
  Decision 1: Jev picks one option directly (a `choice` question).
  Decision 2: Jev inside a framework: must-have gates (`noul` per option x must
  criterion) eliminate options, a weighted matrix (`score` per option x weighted
  criterion) ranks the rest.
If both agree robustly the verdict is AGREE. Otherwise the escalation goes back to Jev:
follow-up rounds send the previous round's results and the escalation reasons as state,
re-ask Jev's pick and re-score the uncertain cells (a new answer replaces the old one
when it is at least as confident). After MAX_ROUNDS without agreement Jev arbitrates:
one `choice` among the surviving options with all the evidence, and that is the
decision (verdict ARBITRATED). Only when every option fails a must-have is there no
decision (NO_VALID_OPTION): the options themselves need rethinking.

Usage: python decide.py decision.json [--log decisions.jsonl] [--json]
Input:  {"context": str or object,
         "clarifications": [str, ...],                                   # optional facts
         "options": {name: description},
         "criteria": {name: {"description": str, "weight": float}       # scored
                      | {"description": str, "must": true}}}             # gate
Env:    TYPESAFE_API_KEY (any placeholder works where a proxy injects the key),
        JEV_DECIDE_BASE_URL (default https://api.typesafe.ai/v1),
        JEV_DECIDE_MODEL (default jev-latest).
Exit:   0 decided (AGREE or ARBITRATED), 4 NO_VALID_OPTION, 2 bad input, 1 API failure.
Standard library only; each round is one request (input-priced, docs/verification.md 1).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("JEV_DECIDE_BASE_URL", "https://api.typesafe.ai/v1")
MODEL = os.environ.get("JEV_DECIDE_MODEL", "jev-latest")
LEVELS = ["very poor", "poor", "acceptable", "good", "excellent"]  # score 0..4
MIN_CONFIDENCE = 0.55  # the same gate jev's own dispatcher uses
MIN_MARGIN = 0.05  # weighted-total gap below which the matrix counts as a tie
GATE_PASS = 0.5  # noul at or above this passes a must-have gate
MAX_ROUNDS = 3  # round 1 plus up to two follow-ups before Jev arbitrates
RETRY_STATUSES = {429, 529}  # the API docs say to retry these; the call has no side effects
MAX_ATTEMPTS = 3

Cell = tuple[str, str]  # (option, criterion)


class InputError(ValueError):
    pass


def load_decision(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    options, criteria = d.get("options"), d.get("criteria")
    if "context" not in d:
        raise InputError("missing 'context'")
    if not isinstance(d.get("clarifications", []), list):
        raise InputError("'clarifications' must be a list of strings")
    if not isinstance(options, dict) or len(options) < 2:
        raise InputError("'options' must map at least two names to descriptions")
    if len(options) > 255:
        raise InputError("a choice allows at most 255 options")
    if not isinstance(criteria, dict) or not criteria:
        raise InputError("'criteria' must be a non-empty map")
    for name, c in criteria.items():
        if not isinstance(c, dict) or not c.get("description"):
            raise InputError(f"criterion {name!r} needs a 'description'")
        if c.get("must") is True:
            continue
        weight = c.get("weight")
        if not isinstance(weight, (int, float)) or weight <= 0:
            raise InputError(f"criterion {name!r} needs a positive 'weight' or 'must': true")
    if not any(c.get("must") is not True for c in criteria.values()):
        raise InputError("at least one criterion must be weighted (not only gates)")
    return d


def is_gate(d: dict[str, Any], crit: str) -> bool:
    return d["criteria"][crit].get("must") is True


def pick_question(options: dict[str, str], instructions: str) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {name: desc or None for name, desc in options.items()},
    }


def cell_question(d: dict[str, Any], cell: Cell, follow_up: bool) -> dict[str, Any]:
    opt, crit = cell
    subject = f"Option '{opt}': {d['options'][opt]}"
    if follow_up:
        subject += (
            "\nThe previous rating of this was uncertain; use the clarifications and evidence."
        )
    desc = d["criteria"][crit]["description"]
    if is_gate(d, crit):
        return {"type": "noul", "instructions": f"{subject}\nDoes it meet this requirement: {desc}"}
    return {"type": "score", "instructions": f"{subject}\nRate it on: {desc}", "criteria": LEVELS}


def call_jev(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    key = os.environ.get("TYPESAFE_API_KEY") or "proxy-injected"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = BASE_URL.rstrip("/") + "/systemone"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                time.sleep(2**attempt)
                continue
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"POST {url} failed: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"POST {url} failed: {exc}") from exc
        missing = [q for q in questions if q not in data.get("answers", {})]
        if missing:
            raise RuntimeError(f"response is missing answers for {missing[:5]}")
        return data
    raise AssertionError("unreachable")


def evaluate(d: dict[str, Any], pick: dict[str, Any], cells: dict[Cell, dict]) -> dict[str, Any]:
    """Apply the decision rules to the current answers. No I/O, so the rules are testable."""
    criteria = d["criteria"]
    total_weight = sum(c["weight"] for c in criteria.values() if c.get("must") is not True)
    totals = {opt: 0.0 for opt in d["options"]}
    failed_gates: dict[str, list[str]] = {}
    uncertain: list[Cell] = []
    for (opt, crit), a in cells.items():
        if is_gate(d, crit):
            if a["noul"] < GATE_PASS:
                failed_gates.setdefault(opt, []).append(crit)
            continue
        totals[opt] += a["score"] / (len(LEVELS) - 1) * criteria[crit]["weight"] / total_weight
        if a.get("confidence", 0) < MIN_CONFIDENCE:
            uncertain.append((opt, crit))

    reasons: list[str] = []
    if pick.get("confidence", 0) < MIN_CONFIDENCE:
        reasons.append(f"decision 1 confidence {pick.get('confidence', 0):.2f} < {MIN_CONFIDENCE}")
    survivors = {o: t for o, t in totals.items() if o not in failed_gates}
    ranked = sorted(survivors.items(), key=lambda kv: kv[1], reverse=True)
    matrix_pick = ranked[0][0] if ranked else None
    if matrix_pick is None:
        reasons.append("every option failed a must-have gate")
    elif len(ranked) > 1 and ranked[0][1] - ranked[1][1] < MIN_MARGIN:
        reasons.append(f"matrix tie: {ranked[0][0]} vs {ranked[1][0]}")
    elif len(ranked) > 1:
        # Score confidence is often low because probability spreads over neighbouring
        # levels, so an uncertain cell is treated as off by up to one level against the
        # winner. It only matters if that could change the outcome.
        step = 1 / (len(LEVELS) - 1)
        worst = dict(survivors)
        for opt, crit in uncertain:
            if opt in worst:
                shift = step * criteria[crit]["weight"] / total_weight
                worst[opt] += -shift if opt == matrix_pick else shift
        rival = max((o for o in worst if o != matrix_pick), key=worst.__getitem__)
        if worst[matrix_pick] - worst[rival] < MIN_MARGIN:
            reasons.append(f"not robust: uncertain cells could let {rival} catch {matrix_pick}")
    if pick["choice"] in failed_gates:
        reasons.append(
            f"decision 1 picked {pick['choice']}, which fails {failed_gates[pick['choice']]}"
        )
    if matrix_pick is not None and pick["choice"] != matrix_pick:
        reasons.append(f"disagreement: decision 1 = {pick['choice']}, decision 2 = {matrix_pick}")
    return {
        "agree": not reasons,
        "decision_1": {"choice": pick["choice"], "confidence": pick.get("confidence")},
        "decision_2": {"choice": matrix_pick, "ranking": ranked, "failed_gates": failed_gates},
        "uncertain": [f"{o}/{c}" for o, c in uncertain],
        "reasons": reasons,
    }


def follow_up_cells(d: dict[str, Any], ev: dict[str, Any]) -> list[Cell]:
    """The cells worth re-asking: uncertain scores, and the gates of the disputed options."""
    cells: list[Cell] = [(opt, crit) for opt, crit in (c.split("/", 1) for c in ev["uncertain"])]
    disputed = {ev["decision_1"]["choice"], ev["decision_2"]["choice"]} - {None}
    for opt in disputed:
        cells += [(opt, crit) for crit in d["criteria"] if is_gate(d, crit)]
    return list(dict.fromkeys(cells))


def round_state(d: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {"context": d["context"]}
    if d.get("clarifications"):
        state["clarifications"] = d["clarifications"]
    if history:
        state["previous_rounds"] = [
            {k: r[k] for k in ("decision_1", "decision_2", "reasons")} for r in history
        ]
    return state


def decide(d: dict[str, Any], ask=call_jev) -> dict[str, Any]:
    """Run rounds until Jev's passes agree; after MAX_ROUNDS, Jev arbitrates."""
    all_cells: list[Cell] = [(o, c) for o in d["options"] for c in d["criteria"]]
    answers: dict[Cell, dict] = {}
    history: list[dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def run(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        resp = ask(state, questions)
        usage["calls"] += 1
        for k in ("input_tokens", "output_tokens"):
            usage[k] += resp.get("usage", {}).get(k, 0)
        return resp["answers"]

    ev: dict[str, Any] = {}
    for rnd in range(1, MAX_ROUNDS + 1):
        targets = all_cells if rnd == 1 else follow_up_cells(d, ev)
        questions = {
            "pick": pick_question(
                d["options"],
                "Which option is the best decision in this context?"
                if rnd == 1
                else "Earlier rounds did not settle this (see previous_rounds and their reasons). "
                "Which option is the best decision?",
            )
        }
        ids = {f"c{i}": cell for i, cell in enumerate(targets)}
        questions.update({qid: cell_question(d, cell, rnd > 1) for qid, cell in ids.items()})
        got = run(round_state(d, history), questions)
        for qid, cell in ids.items():
            new, old = got[qid], answers.get(cell)
            if old is None or new.get("confidence", 1) >= old.get("confidence", 1):
                answers[cell] = new
        ev = evaluate(d, got["pick"], answers)
        ev["round"] = rnd
        history.append(ev)
        if ev["agree"]:
            return finish("AGREE", ev["decision_2"]["choice"], None, history, answers, usage)
        if ev["decision_2"]["choice"] is None:
            return finish("NO_VALID_OPTION", None, None, history, answers, usage)

    survivors = [o for o, _ in ev["decision_2"]["ranking"]]
    arb_options = {o: d["options"][o] for o in survivors}
    if len(arb_options) == 1:
        only = survivors[0]
        return finish(
            "ARBITRATED", only, {"choice": only, "confidence": 1.0}, history, answers, usage
        )
    got = run(
        round_state(d, history),
        {
            "arbitrate": pick_question(
                arb_options,
                "Final decision. The direct pick and the weighted matrix did not settle this "
                "over several rounds (see previous_rounds). Weigh all of it and choose.",
            )
        },
    )
    arb = got["arbitrate"]
    return finish("ARBITRATED", arb["choice"], arb, history, answers, usage)


def finish(verdict, decision, arbitration, history, answers, usage) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "decision": decision,
        "arbitration": arbitration,
        "rounds": history,
        "cells": {f"{o}/{c}": a for (o, c), a in answers.items()},
        "usage": usage,
    }


def print_report(d: dict[str, Any], r: dict[str, Any]) -> None:
    for ev in r["rounds"]:
        d1, d2 = ev["decision_1"], ev["decision_2"]
        totals = ", ".join(f"{o} {t:.3f}" for o, t in d2["ranking"])
        print(
            f"round {ev['round']}: decision 1 = {d1['choice']} ({d1['confidence']:.2f}); "
            f"decision 2 = {d2['choice']} [{totals}]"
        )
        for opt, gates in d2["failed_gates"].items():
            print(f"  eliminated {opt}: fails {', '.join(gates)}")
        for reason in ev["reasons"]:
            print(f"  - {reason}")
    print("final cells:")
    for key, a in r["cells"].items():
        crit = key.split("/", 1)[1]
        if is_gate(d, crit):
            print(f"  {key:<52} gate  {a['noul']:.2f}")
        else:
            print(f"  {key:<52} score {a['score']:.2f}  conf {a.get('confidence', 0):.2f}")
    line = f"verdict: {r['verdict']}"
    if r["decision"]:
        line += f" -> {r['decision']}"
    if r["arbitration"] and r["verdict"] == "ARBITRATED":
        line += f" (Jev arbitration, confidence {r['arbitration'].get('confidence', 0):.2f})"
    print(line)
    u = r["usage"]
    print(f"{u['calls']} call(s), {u['input_tokens']} input / {u['output_tokens']} output tokens")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("decision", type=Path)
    parser.add_argument("--log", type=Path, help="append the full record as one JSON line")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args()
    try:
        d = load_decision(args.decision)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        result = decide(d)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.log:
        with args.log.open("a") as fh:
            fh.write(json.dumps({"input": d, "result": result}) + "\n")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(d, result)
    return 4 if result["verdict"] == "NO_VALID_OPTION" else 0


if __name__ == "__main__":
    sys.exit(main())
