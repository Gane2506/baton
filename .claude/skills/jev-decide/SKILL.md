---
name: jev-decide
description: Make a non-trivial engineering decision with Jev (TypeSafe's decision model) in two passes - Jev's direct pick, then Jev inside a decision framework (must-have gates + weighted criteria matrix) - and act only when both agree. Use when choosing between designs, keep/refactor/rewrite, ranking review findings, or ordering work on this repo.
---

# jev-decide

One TypeSafe request answers two decisions about the same options:

1. **Decision 1, Jev direct**: a `choice` question, "which option is best?".
2. **Decision 2, Jev + framework**: per option, a yes/no `noul` gate for every
   must-have criterion (failing one eliminates the option), and a 0-4 `score` for
   every weighted criterion; weighted totals rank the survivors.

Verdict **AGREE** (exit 0) when both pick the same option, decision 1 confidence is at
least 0.55, the matrix winner leads by at least 0.05, and it still leads by 0.05 if every
matrix cell under 0.55 confidence is assumed one level worse for the winner and one
level better for the others. (Score confidence is often low because probability
spreads over neighbouring levels; this asks whether that uncertainty could change the
outcome.)
Anything else is **ESCALATE** (exit 3) with the reasons. Then decide yourself and write
down why, or ask the maintainer if the choice is outward-facing or hard to reverse
(see CLAUDE.md section 3). Jev is an input to the decision, not the decision maker.

## Use it

1. Write the decision as JSON (keep it small: options x criteria is the number of
   questions; under ~40 keeps the request well inside the 32k-token state limit):

```json
{
  "context": "What the code does today, the problem, constraints. Facts, not opinions.",
  "options": {
    "keep": "Leave X as is",
    "refactor": "Split X into A and B, same behavior",
    "rewrite": "Replace X with Y"
  },
  "criteria": {
    "no_breaking_change": {"must": true, "description": "keeps jev.toml, CLI and state DB compatible"},
    "traceability": {"weight": 3, "description": "a contributor can follow the flow end to end"},
    "failure_semantics": {"weight": 3, "description": "timeouts, empty results and errors stay distinct"},
    "change_risk": {"weight": 2, "description": "low risk of regressions given current tests"},
    "effort": {"weight": 1, "description": "small enough to review in one sitting"}
  }
}
```

2. Run it (stdlib only, no install):

```bash
python .claude/skills/jev-decide/decide.py decision.json --log <scratchpad>/decisions.jsonl
```

`--json` prints the full result. `TYPESAFE_API_KEY` must be set; in a cloud session
whose proxy injects the key, any placeholder works.

## Writing good decisions

- Describe options neutrally; Jev scores what you write.
- Gates are for real must-haves (compatibility, safety rules from CLAUDE.md), not
  preferences. Weighted criteria come from the 16 principles in the review brief plus
  risk and effort.
- Treat low-confidence cells as "Jev can't tell from this context": add facts to
  `context` once, or decide yourself. Don't re-run until it agrees.

## Cost and failure behavior

- One request per decision. Jev is priced on input tokens only (see
  `docs/verification.md` section 1); a typical decision is a few thousand tokens.
- 429/529 are retried up to 3 attempts with backoff (the call has no side effects).
  Other HTTP errors, timeouts and incomplete responses exit 1 with the cause.
  Bad input exits 2.
