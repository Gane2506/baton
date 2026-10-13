"""Two-pass decision with Jev (native TypeSafe System One API).

Decision 1: Jev picks one option directly (a `choice` question).
Decision 2: the same Jev call also runs a decision framework on top: must-have gates
(`noul` per option x must criterion) eliminate options, then a weighted matrix (`score`
per option x weighted criterion) ranks the rest. The two results are compared and the
script prints a verdict: AGREE (act on it) or ESCALATE (a human or the agent decides,
with the reasons listed). The rules are in SKILL.md and `evaluate()`.

Both passes go in one request, so a decision costs one call (input-priced, see
docs/verification.md section 1). Standard library only.

Usage: python decide.py decision.json [--log decisions.jsonl] [--json]
Input:  {"context": str or object,
         "options": {name: description},
         "criteria": {name: {"description": str, "weight": float}       # scored
                      | {"description": str, "must": true}}}             # gate
Env:    TYPESAFE_API_KEY (any placeholder works where a proxy injects the key),
        JEV_DECIDE_BASE_URL (default https://api.typesafe.ai/v1),
        JEV_DECIDE_MODEL (default jev-latest).
Exit:   0 AGREE, 3 ESCALATE, 2 bad input, 1 API failure.
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
RETRY_STATUSES = {429, 529}  # the API docs say to retry these; the call has no side effects
MAX_ATTEMPTS = 3


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


def build_questions(d: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """Return the request's questions and (id, option, criterion) for each matrix cell."""
    options, criteria = d["options"], d["criteria"]
    questions: dict[str, Any] = {
        "pick": {
            "type": "choice",
            "instructions": "Which option is the best decision in this context?",
            "criteria": {name: desc or None for name, desc in options.items()},
        }
    }
    cells: list[tuple[str, str, str]] = []
    for opt, opt_desc in options.items():
        for crit, c in criteria.items():
            qid = f"c{len(cells)}"
            cells.append((qid, opt, crit))
            subject = f"Option '{opt}': {opt_desc}"
            if c.get("must") is True:
                questions[qid] = {
                    "type": "noul",
                    "instructions": f"{subject}\nDoes it meet this requirement: {c['description']}",
                }
            else:
                questions[qid] = {
                    "type": "score",
                    "instructions": f"{subject}\nRate it on: {c['description']}",
                    "criteria": LEVELS,
                }
    return questions, cells


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


def evaluate(d: dict[str, Any], resp: dict[str, Any], cells: list[tuple[str, str, str]]) -> dict:
    """Interpret the answers: no I/O, so the decision rules are easy to read and test."""
    answers, criteria = resp["answers"], d["criteria"]
    pick = answers["pick"]
    reasons: list[str] = []
    if pick.get("confidence", 0) < MIN_CONFIDENCE:
        reasons.append(f"decision 1 confidence {pick.get('confidence', 0):.2f} < {MIN_CONFIDENCE}")

    total_weight = sum(c["weight"] for c in criteria.values() if c.get("must") is not True)
    totals = {opt: 0.0 for opt in d["options"]}
    failed_gates: dict[str, list[str]] = {}
    low_conf: list[str] = []
    rows = []
    for qid, opt, crit in cells:
        a = answers[qid]
        if criteria[crit].get("must") is True:
            passed = a["noul"] >= GATE_PASS
            rows.append((opt, crit, "gate", a["noul"], None, passed))
            if not passed:
                failed_gates.setdefault(opt, []).append(crit)
            continue
        norm = a["score"] / (len(LEVELS) - 1)
        totals[opt] += norm * criteria[crit]["weight"] / total_weight
        conf = a.get("confidence", 0)
        rows.append((opt, crit, "score", a["score"], conf, None))
        if conf < MIN_CONFIDENCE:
            low_conf.append(f"{opt}/{crit}")

    survivors = {o: t for o, t in totals.items() if o not in failed_gates}
    ranked = sorted(survivors.items(), key=lambda kv: kv[1], reverse=True)
    matrix_pick = ranked[0][0] if ranked else None
    if not ranked:
        reasons.append("every option failed a must-have gate")
    elif len(ranked) > 1 and ranked[0][1] - ranked[1][1] < MIN_MARGIN:
        reasons.append(f"matrix tie: {ranked[0][0]} vs {ranked[1][0]}")
    elif len(ranked) > 1:
        # Score confidence is often low because probability spreads over neighbouring
        # levels, so a low-confidence cell is treated as off by up to one level against
        # the winner. Escalate only if that could change the outcome.
        step = 1 / (len(LEVELS) - 1)
        worst = dict(survivors)
        for cell in low_conf:
            opt, crit = cell.split("/", 1)
            if opt not in worst:
                continue
            shift = step * criteria[crit]["weight"] / total_weight
            worst[opt] += -shift if opt == matrix_pick else shift
        rival = max((o for o in worst if o != matrix_pick), key=worst.__getitem__)
        if worst[matrix_pick] - worst[rival] < MIN_MARGIN:
            reasons.append(
                f"not robust: with low-confidence cells ({', '.join(low_conf)}) one level "
                f"worse for {matrix_pick}, {rival} comes within {MIN_MARGIN}"
            )
    if pick["choice"] in failed_gates:
        reasons.append(
            f"decision 1 picked {pick['choice']}, which fails {failed_gates[pick['choice']]}"
        )
    if matrix_pick is not None and pick["choice"] != matrix_pick:
        reasons.append(f"disagreement: decision 1 = {pick['choice']}, decision 2 = {matrix_pick}")

    return {
        "verdict": "ESCALATE" if reasons else "AGREE",
        "decision": matrix_pick if not reasons else None,
        "decision_1": {
            "choice": pick["choice"],
            "confidence": pick.get("confidence"),
            "probabilities": pick.get("probabilities"),
        },
        "decision_2": {"choice": matrix_pick, "ranking": ranked, "failed_gates": failed_gates},
        "reasons": reasons,
        "low_confidence_cells": low_conf,
        "cells": rows,
        "model": resp.get("model"),
        "usage": resp.get("usage"),
    }


def print_report(r: dict[str, Any]) -> None:
    d1, d2 = r["decision_1"], r["decision_2"]
    print(f"decision 1 (Jev direct): {d1['choice']}  confidence {d1['confidence']:.2f}")
    print("decision 2 (gates + weighted matrix):")
    for opt, crit, kind, value, conf, passed in r["cells"]:
        if kind == "gate":
            print(f"  {opt:<24} {crit:<26} gate  {value:.2f} {'pass' if passed else 'FAIL'}")
        else:
            print(f"  {opt:<24} {crit:<26} score {value:.2f}  conf {conf:.2f}")
    for opt, total in d2["ranking"]:
        print(f"  total {opt:<18} {total:.3f}")
    for opt, gates in d2["failed_gates"].items():
        print(f"  eliminated {opt}: fails {', '.join(gates)}")
    print(f"verdict: {r['verdict']}" + (f" -> {r['decision']}" if r["decision"] else ""))
    for reason in r["reasons"]:
        print(f"  - {reason}")
    if r["low_confidence_cells"] and r["verdict"] == "AGREE":
        print(f"  (robust despite low-confidence cells: {', '.join(r['low_confidence_cells'])})")
    print(f"model {r['model']}, usage {r['usage']}")


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
    questions, cells = build_questions(d)
    try:
        resp = call_jev(d["context"], questions)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    result = evaluate(d, resp, cells)
    if args.log:
        with args.log.open("a") as fh:
            fh.write(json.dumps({"input": d, "result": result}) + "\n")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)
    return 0 if result["verdict"] == "AGREE" else 3


if __name__ == "__main__":
    sys.exit(main())
