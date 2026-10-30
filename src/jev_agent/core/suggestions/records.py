"""Suggestion record storage (issue #368, phase 6, design sections 6.6, 12.4).

One JSON file per suggestion record, under the profile's directory in
``suggestions/<suggestion-id>.json`` — next to the profile's version
files, never inside a version file, so a suggestion record is never a
runnable flow: it appears in no picker and changes no run until approved
(section 12.4). The record fields are the section 6.6 list (``id``,
``from_workflow_profile_id``, ``from_workflow_profile_version``, ``goal``,
``draft``, ``reasoning_lines``, ``thresholds_met``, ``status``,
``dedup_key``, ``evidence_window``), plus the mode (``preset-only``,
``usage-based``), the cross-repo flag and contributing repos (so the
cross-repo label is stored with the record, not recomputed), the
evidence fingerprint (the re-offer check), and the approval outcome
(``approved_version``, set only by approval).

Lifecycle (section 12.4), and only this lifecycle:

* ``pending`` — stored at request time, visible on the result page and in
  the profile's suggestion history, pickable nowhere.
* ``approved`` — the draft was saved as a new profile version (with
  ``origin: suggested``), or, when the draft is identical to the current
  version, approval of the existing version was recorded and no new
  version created. Either way the record keeps its ``approved_version``.
* ``discarded`` — the record's status changed and nothing else: no new
  version, no picker entry, no run change. The record stays visible in
  the history as discarded.

Re-offer rule (section 12.4): a discarded suggestion is not re-offered
unless new evidence exists. A request whose dedup key matches a discarded
record with an unchanged evidence fingerprint returns that discarded
record, marked as such — not a fresh suggestion. A changed fingerprint
(new runs in the window, a threshold crossed, a new lesson) allows a
fresh suggestion.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from .engine import (
    ReasoningLine,
    SuggestionOutcome,
    change_set_key,
    dedup_key,
)

SUGGESTIONS_DIRNAME = "suggestions"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def reasoning_line_to_json(line: ReasoningLine) -> dict[str, Any]:
    return {
        "change": line.change,
        "evidence": line.evidence,
        "sample_size": line.sample_size,
        "window": line.window,
        "completeness": line.completeness,
        "falsifier": line.falsifier,
        "kind": line.kind,
    }


def reasoning_line_from_json(data: dict[str, Any]) -> ReasoningLine:
    return ReasoningLine(
        change=str(data.get("change", "")),
        evidence=str(data.get("evidence", "")),
        sample_size=int(data.get("sample_size", 0)),
        window=str(data.get("window", "")),
        completeness=str(data.get("completeness", "")),
        falsifier=str(data.get("falsifier", "")),
        kind=str(data.get("kind", "measured")),
    )


class SuggestionStore:
    """Suggestion records for one profile directory."""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir

    @property
    def directory(self) -> Path:
        return self.profile_dir / SUGGESTIONS_DIRNAME

    def create(self, outcome: SuggestionOutcome, *, record_id: str | None = None) -> dict[str, Any]:
        """Store a pending record for a usage-based or preset-only outcome.
        A no-change outcome has no draft and stores nothing — there is
        nothing to approve, so the caller shows its message directly."""
        if outcome.draft is None:
            raise ValueError("a suggestion with no draft stores no record")
        record_id = record_id or f"sugg-{secrets.token_hex(6)}"
        record: dict[str, Any] = {
            "id": record_id,
            "from_workflow_profile_id": outcome.from_profile_id,
            "from_workflow_profile_version": outcome.from_profile_version,
            "goal": outcome.goal,
            "draft": outcome.draft,
            "reasoning_lines": [reasoning_line_to_json(line) for line in outcome.reasoning_lines],
            "thresholds_met": outcome.thresholds_met,
            "status": "pending",
            "dedup_key": dedup_key(
                outcome.from_profile_id, outcome.from_profile_version, outcome.changes
            ),
            "change_set": change_set_key(outcome.changes),
            "evidence_window": outcome.evidence_window,
            "evidence_fingerprint": outcome.evidence_fingerprint,
            "mode": outcome.mode,
            "cross_repo": outcome.cross_repo,
            "repos": list(outcome.repos),
            "approved_version": None,
        }
        _atomic_write(
            self.directory / f"{record_id}.json",
            json.dumps(record, indent=2, sort_keys=True) + "\n",
        )
        return record

    def get(self, record_id: str) -> dict[str, Any]:
        """One record by id. Raises ValueError naming the id when missing
        or unreadable — a corrupt record is reported, never executed."""
        path = self.directory / f"{record_id}.json"
        if not path.exists():
            raise ValueError(f"no suggestion record {record_id!r} for this profile")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"suggestion record {record_id!r} cannot be read: {exc}") from exc
        if not isinstance(data, dict) or data.get("id") != record_id:
            raise ValueError(f"suggestion record {record_id!r} cannot be read: id mismatch")
        return data

    def list_records(self) -> list[dict[str, Any]]:
        """Every record for the profile, newest first by file order (ids
        carry no time order, so creation order is the directory's
        insertion order reversed — records are listed, not ranked)."""
        if not self.directory.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(self.get(path.stem))
            except ValueError:
                continue
        records.reverse()
        return records

    def set_status(
        self, record_id: str, status: str, *, approved_version: int | None = None
    ) -> dict[str, Any]:
        """Change only the record's status (and, on approval, the approved
        version). A discard changes nothing else — no new profile
        version, no picker entry, no run change — because this method
        touches only this file."""
        if status not in ("pending", "approved", "discarded"):
            raise ValueError(f"unknown suggestion status {status!r}")
        record = self.get(record_id)
        record["status"] = status
        if approved_version is not None:
            record["approved_version"] = approved_version
        _atomic_write(
            self.directory / f"{record_id}.json",
            json.dumps(record, indent=2, sort_keys=True) + "\n",
        )
        return record

    def find_discarded_match(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        """The re-offer check (section 12.4): a discarded record with the
        same dedup key and an unchanged evidence fingerprint. None when
        no such record exists, or when the evidence moved."""
        for record in self.list_records():
            if (
                record.get("status") == "discarded"
                and record.get("dedup_key") == key
                and record.get("evidence_fingerprint") == fingerprint
            ):
                return record
        return None


__all__ = [
    "SUGGESTIONS_DIRNAME",
    "SuggestionStore",
    "reasoning_line_from_json",
    "reasoning_line_to_json",
]
