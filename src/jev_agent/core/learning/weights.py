"""Laya weights: validation of a six-criterion weight set.

The consensus dispatcher that used these weights is gone (issue #193), and the
profile-stored weights API (`set/get_laya_weights`, issue #232) is gone with it —
nothing stores tuned weights any more. The validator stays: it is the finite/sum
check pinned by the issue #205 regression tests, and the criteria live on in
`LAYA_CRITERIA` (the quality gate scores against them). The finite check here is
the NaN guard moved out of the deleted `LayaSettings` (PR #217): NaN and
infinities are rejected, because an all-NaN set used to pass the sum check.
"""

from __future__ import annotations

import math

from jev_agent.config.schema import LAYA_CRITERIA
from jev_agent.core.learning.models import LearningError


def _checked_weights(profile: str, weights: object) -> dict[str, float]:
    """`weights` if they are valid Laya weights (exactly the six criteria, non-negative,
    summing to 1), else a LearningError. The criteria live on in `LAYA_CRITERIA` even
    though the consensus dispatcher that used them is gone (issue #193)."""
    problems: list[str] = []
    if not isinstance(weights, dict):
        problems.append("weights must be a mapping")
    else:
        unknown = sorted(set(weights) - set(LAYA_CRITERIA))
        missing = sorted(set(LAYA_CRITERIA) - set(weights))
        if unknown:
            problems.append(f"unknown: {', '.join(unknown)}")
        if missing:
            problems.append(f"missing: {', '.join(missing)}")
        if not problems:
            if any(
                not isinstance(w, (int, float)) or not math.isfinite(w) for w in weights.values()
            ):
                problems.append(
                    "weights must be finite numbers (NaN and infinities are not allowed)"
                )
            elif any(w < 0 for w in weights.values()):
                problems.append("weights must not be negative")
            elif abs(sum(weights.values()) - 1) > 1e-6:
                problems.append(
                    f"weights must add up to 1 (they add up to {sum(weights.values()):g})"
                )
    if problems:
        raise LearningError(f"profile {profile!r}: invalid Laya weights: {'; '.join(problems)}")
    if not isinstance(weights, dict):
        # Unreachable: a non-mapping always adds a problem above and raises. The
        # explicit check (instead of `assert`) keeps `-O` runs and the type
        # checker honest about the narrowing below.
        raise LearningError(f"profile {profile!r}: invalid Laya weights: weights must be a mapping")
    return {k: float(v) for k, v in weights.items()}


__all__ = ["_checked_weights"]
