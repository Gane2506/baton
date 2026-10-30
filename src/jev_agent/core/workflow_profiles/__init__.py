"""Workflow profiles (issue #368): named, versioned pipeline flows.

Phase 2 package: `schema` (section 6.6 field lists), `store` (file store,
repo and user scope, atomic version writes), `resolver` (one resolver
function: validate, apply precedence once, store the snapshot, compute
assurance), `validator` (schema plus the live catalogue subset), and
`assurance` (section 10.3 computation and label text). Phase 3 adds the
full catalogue to the validator, the store-wide re-check, the
snapshot-aware executor lookup (pipeline, harness, gate), the
`default`-preset parameter set (from the design text, no preset files),
and the `[workflows]` configuration section.
"""

from __future__ import annotations

from .assurance import assurance_label_text, compute_assurance
from .resolver import (
    ResolvedProfile,
    build_default_snapshot,
    default_via_profile_enabled,
    pin_workflow_profile,
    resolve_workflow_profile,
    workflows_enabled,
)
from .store import (
    ProfileCorruptError,
    ProfileRefusal,
    ProfileStore,
    ProfileStoreError,
    RecheckFailure,
)
from .validator import ProfileValidationError, ValidationContext, validate_profile

__all__ = [
    "ProfileCorruptError",
    "ProfileRefusal",
    "ProfileStore",
    "ProfileStoreError",
    "ProfileValidationError",
    "RecheckFailure",
    "ResolvedProfile",
    "ValidationContext",
    "assurance_label_text",
    "build_default_snapshot",
    "compute_assurance",
    "default_via_profile_enabled",
    "pin_workflow_profile",
    "resolve_workflow_profile",
    "validate_profile",
    "workflows_enabled",
]
