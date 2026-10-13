---
name: jev-decide
description: Make a non-trivial engineering decision with Jev (TypeSafe's decision model) - Jev's direct pick plus Jev inside a decision framework (must-have gates + weighted criteria matrix); disagreements go back to Jev for follow-up rounds and, if needed, Jev's final arbitration. Jev decides; act on its result. Use when choosing between designs, keep/refactor/rewrite, ranking review findings, or ordering work on this repo.
---

# jev-decide

Jev makes the decision. You frame it (options, criteria, facts) and act on the result;
you never override it. The only exception is CLAUDE.md section 3: an outward-facing or
hard-to-reverse step still needs the maintainer's approval after Jev has decided.

**Round 1** (one request, two passes):
1. **Decision 1, Jev direct**: a `choice` question, "which option is best?".
2. **Decision 2, Jev + framework**: per option, a yes/no `noul` gate for every
   must-have criterion (failing one eliminates the option), and a 0-4 `score` for
   every weighted criterion; weighted totals rank the survivors.

They **agree** when both pick the same option, decision 1 confidence is at least 0.55,
the matrix winner leads by at least 0.05, and it still leads by 0.05 if every matrix
cell under 0.55 confidence is assumed one level worse for the winner and one level
better for the others. (Score confidence is often low because probability spreads over
neighbouring levels; this asks whether that uncertainty could change the outcome.)

**If they don't agree, the escalation goes back to Jev**, automatically:
- Rounds 2-3 send the previous rounds' picks, totals and escalation reasons as state,
  re-ask the direct pick, and re-score the uncertain cells plus the gates of the disputed
  options. A new answer replaces the old one when it is at least as confident.
- If round 3 still doesn't agree, Jev **arbitrates**: one `choice` among the surviving
  options with all rounds as evidence. That choice is the decision.

Verdicts: **AGREE** or **ARBITRATED** (exit 0, act on `decision`); **NO_VALID_OPTION**
(exit 4) when every option fails a must-have: rethink the options, then decide again.
Before running, put every relevant fact you have in `context` or `clarifications`;
low confidence usually means thin context.

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
- Don't re-run a decided question hoping for a different answer. Re-run only when you
  learn new facts; add them to `clarifications` and say so in the log.

## Cost and failure behavior

- One request per round, at most four (three rounds plus arbitration). Jev is priced on
  input tokens only (see `docs/verification.md` section 1); a full four-call decision is
  about 10k tokens, well under a cent.
- 429/529 are retried up to 3 attempts with backoff (the call has no side effects).
  Other HTTP errors, timeouts and incomplete responses exit 1 with the cause.
  Bad input exits 2. On exit 1 there is no decision: fix the cause and run again.
