"""Workflow profiles (issue #368): named, versioned pipeline flows.

Phase 2 package: `schema` (section 6.6 field lists), `store` (file store,
repo and user scope, atomic version writes), `resolver` (one resolver
function: validate, apply precedence once, store the snapshot, compute
assurance), `validator` (schema plus the live catalogue subset), and
`assurance` (section 10.3 computation and label text). The full catalogue
and the snapshot-aware executor lookup land in phase 3.
"""

from __future__ import annotations

from .assurance import assurance_label_text, compute_assurance
from .resolver import ResolvedProfile, pin_workflow_profile, resolve_workflow_profile
from .store import (
    ProfileCorruptError,
    ProfileRefusal,
    ProfileStore,
    ProfileStoreError,
)
from .validator import ProfileValidationError, ValidationContext, validate_profile

__all__ = [
    "ProfileCorruptError",
    "ProfileRefusal",
    "ProfileStore",
    "ProfileStoreError",
    "ProfileValidationError",
    "ResolvedProfile",
    "ValidationContext",
    "assurance_label_text",
    "compute_assurance",
    "pin_workflow_profile",
    "resolve_workflow_profile",
    "validate_profile",
]
