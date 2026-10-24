"""Exact-match cache for dispatcher decisions and read-only role outputs (issue #10).

Two interception points in `core/harness.py` — `Harness.decide` (phase 1) and
`Harness.run_worker` (phase 2) — consult this cache. Keys are sha256 over canonical,
normalized request records; volatile state (round counters, spend, timestamps) is
stripped so `jev resume` hits. The config fingerprint is key material for every key,
so any settings change is a different key automatically; role keys additionally
carry the repo fingerprint, so a changed repo invalidates role outputs (decisions
key on content, and the role chain invalidates them transitively when the repo
moves).

Safety rules (enforced in code, asserted in tests):
- `coder` and `learner` roles are never cached (hard deny, not config).
- A role entry is never served or stored when its repo fingerprint is
  `"unknown"` (issue #201): without repo invalidation, caching would serve stale
  role outputs after the repo moves. `repo_fingerprint` still returns
  `"unknown"` (fail-soft, logged) — the refusal lives in `Cache.get`/`put`, so
  the exact-match paths are covered, including entries written before this fix
  (role semantic serving stays off, so no other path can serve a role entry).
- Only `status == "success"` role results are stored; failures are never replayed.
- Decisions are stored only from clean primary answers (no rules-fallback, no D6
  unless `[cache] d6_decisions = true`).
- CLI workers (`claude_code`, `codex`) are excluded unless `[cache] cli_workers`.

Phase 3 (semantic similarity) ships the `Embedder` protocol with the stdlib SimHash
backend, default OFF (`[cache.semantic] enabled = false`), plus `jev cache calibrate`
and a shadow mode that measures agreement without serving. Live semantic serving
stays off until shadow data proves it safe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from jev_agent import __version__

if TYPE_CHECKING:  # avoid a runtime import cycle: store.py imports CacheEntry
    from jev_agent.config.schema import CacheSettings, JevConfig
    from jev_agent.core.models import DecisionRequest
    from jev_agent.core.store import Store

__all__ = [
    "CACHE_MARKER",
    "Cache",
    "CacheEntry",
    "CacheKey",
    "CacheStats",
    "Embedder",
    "OpenRouterEmbedder",
    "SimHashEmbedder",
    "canonical_json",
    "decision_cache_key",
    "decision_normalized_text",
    "normalize_prompt",
    "repo_fingerprint",
    "role_cache_key",
    "role_normalized_text",
    "strip_volatile",
    "tuned_laya_fingerprint",
]

#: Appended to a worker summary when the output was served from the cache.
CACHE_MARKER = "[served from cache]"

#: The repo fingerprint when git could not produce one. `Cache` refuses to serve
#: or store role keys carrying it (see `Cache.get`/`Cache.put`).
UNKNOWN_REPO_FINGERPRINT = "unknown"

log = logging.getLogger(__name__)

#: Decision state keys that are dropped outright (the rest is pattern-matched).
_VOLATILE_KEYS = frozenset({"previous_rounds"})
#: Stable config keys the volatile regex below would otherwise match. The D5
#: retry/escalate ladder is attempt-relative-to-max (`attempt` vs `max_attempts`),
#: and the plan/fix round counters are round-relative-to-max too — all of these are
#: decision-relevant, so they are kept. (`round`/`fix_rounds` never match the regex
#: anyway; they are listed here so a future broadening of `_VOLATILE_RE` cannot
#: silently drop them.)
_KEY_KEEP = frozenset({"max_attempts", "max_rounds", "max_fix_rounds", "round", "fix_rounds"})
#: Matches volatile counters/timestamps anywhere in a state key: `attempt_no`,
#: `budget_usd`, `spend_usd`, `cost_estimate`, `created_at`, `latency_ms`, ...
#: Note: the bare key `attempt` (the D5/D8 attempt number) is deliberately NOT
#: matched — it drives the retry/escalate ladder, so it is decision-relevant.
#: Spend/budget keys ARE stripped: a deliberate, documented freshness tradeoff for
#: hit rate (see core-a.md minor 11) — the hard `check_budget` gate in the harness
#: bounds it, so it can never cause overspend.
_VOLATILE_RE = re.compile(
    r"(^|_)(attempts|attempt_no|budget|budgets|spend|spent|cost|costs|token|tokens|"
    r"timestamp|timestamps|latency|duration)(_|$)|(_at|_ms|_us)$"
)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?\b")
_HEXID_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
# Worktree paths look like `.../jev/a1b2c3/task-name`; the run/task ids differ per run.
_WORKTREE_RE = re.compile(r"jev[/\\][\w.\-]{4,}[/\\][\w.\-]+")

_GIT_TIMEOUT_S = 10
# Bound the repo fingerprint: at most this many untracked files, this big each.
_FP_MAX_FILES = 200
_FP_MAX_BYTES = 1_000_000


# ---- canonical records ---------------------------------------------------------------


def strip_volatile(obj: Any) -> Any:
    """Recursively drop volatile fields from a decision state.

    `previous_rounds` changes every follow-up round while the decision-relevant facts
    don't; budget/spend/attempt counters and timestamps change on every call. Keeping
    them in the key would make every `jev resume` a miss. Stable config such as
    `max_attempts` is kept (`_KEY_KEEP`): the retry/escalate ladder is relative to it.
    """
    if isinstance(obj, dict):
        return {
            key: strip_volatile(value)
            for key, value in obj.items()
            if str(key) in _KEY_KEEP
            or (key not in _VOLATILE_KEYS and not _VOLATILE_RE.search(str(key)))
        }
    if isinstance(obj, (list, tuple)):
        return [strip_volatile(value) for value in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Stable JSON for hashing: sorted keys, no whitespace, no float noise."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _config_fingerprint(
    config: JevConfig, dispatcher_name: str, laya_weights_fp: str | None = None
) -> str:
    """Hash of the settings a cached answer is only valid under.

    `min_confidence` gates answers, Laya weights move votes, the model and base URL
    pick the answerer — a cached answer gated at 0.55 must never serve a run gated
    at 0.9. Profile-tuned Laya weights (`jev laya tune --apply`) are fingerprinted
    separately: they change the vote without changing jev.toml. The jev package
    version is included so upgrades invalidate.
    """
    record = {
        "dispatcher": dispatcher_name,
        "jev": config.dispatcher.jev.model_dump(mode="json"),
        "laya": config.dispatcher.laya.model_dump(mode="json"),
        "laya_tuned_fp": laya_weights_fp,
        "jev_version": __version__,
    }
    return hashlib.sha256(canonical_json(record).encode()).hexdigest()


@dataclass(frozen=True)
class CacheKey:
    """A built cache key: the hash for lookup, the JSON for provenance, the TTL."""

    kind: str  # "decision" | "role"
    key_hash: str
    key_json: str
    ttl_hours: float
    simhash: str  # hex simhash of the normalized text, for phase-3 similarity scans


def _build(kind: str, components: dict[str, Any], text: str, ttl_hours: float) -> CacheKey:
    body = canonical_json(components)
    return CacheKey(
        kind=kind,
        key_hash=hashlib.sha256(body.encode()).hexdigest(),
        key_json=body,
        ttl_hours=ttl_hours,
        simhash=SimHashEmbedder().fingerprint(text),
    )


def decision_normalized_text(req: DecisionRequest) -> str:
    """The normalized text a decision key (and its simhash) is built from."""
    return canonical_json(
        {"point": req.point, "questions": req.questions, "state": strip_volatile(req.state)}
    )


def decision_cache_key(
    req: DecisionRequest,
    *,
    dispatcher_name: str,
    config: JevConfig,
    run_id: str,
    laya_weights_fp: str | None = None,
) -> CacheKey:
    """Exact-match key for a dispatcher decision (phase 1)."""
    text = decision_normalized_text(req)
    components = {
        "v": 1,
        "kind": "decision",
        "point": req.point,
        "questions": req.questions,
        "state": strip_volatile(req.state),
        "config_fp": _config_fingerprint(config, dispatcher_name, laya_weights_fp),
        # run_id/decision_id are provenance, filled in on put — not key material,
        # so a resumed run hits the same entry.
        "simhash": SimHashEmbedder().fingerprint(text),
    }
    return _build("decision", components, text, config.cache.ttl_hours)


def tuned_laya_fingerprint(weights: dict[str, float]) -> str:
    """Fingerprint for profile-tuned Laya weights, so a retuned profile is a new key."""
    return hashlib.sha256(canonical_json(weights).encode()).hexdigest()


def normalize_prompt(prompt: str, repo: Path) -> str:
    """Normalize a role prompt so reruns key identically.

    Absolute worktree paths differ per run (`jev/<run>/<task>`), timestamps, UUIDs
    and long hex ids are regenerated every call — all are replaced with placeholders.
    Everything else stays byte-identical.
    """
    text = prompt.replace(str(repo), "<repo>")
    text = _WORKTREE_RE.sub("jev/<run>/<task>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _TS_RE.sub("<ts>", text)
    return _HEXID_RE.sub("<id>", text)


def role_normalized_text(role: str, prompt: str, repo: Path) -> str:
    """The normalized text a role key (and its simhash) is built from."""
    return canonical_json({"role": role, "prompt": normalize_prompt(prompt, repo)})


def repo_fingerprint(repo: Path) -> str:
    """Fingerprint the repo state a role call sees: HEAD + tracked diff + untracked files.

    A changed repo is a different key — this is the primary defense against serving a
    stale review or plan after the code moved. Fail-soft: when git is unavailable the
    fingerprint is `UNKNOWN_REPO_FINGERPRINT`, and `Cache` then refuses to serve or
    store role keys carrying it (issue #201) — caching without repo invalidation
    would serve stale outputs after the repo moves.
    """
    try:
        head = _git(repo, "rev-parse", "HEAD").strip()
        diff = _git(repo, "diff", "--no-color", "--no-ext-diff", "HEAD")
        digest = hashlib.sha256()
        digest.update(head.encode())
        digest.update(b"\x00")
        digest.update(diff.encode())
        untracked = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        paths = [line[3:] for line in untracked.split("\0") if line.startswith("?? ")][
            :_FP_MAX_FILES
        ]
        for rel in sorted(paths):
            digest.update(b"\x00")
            digest.update(rel.encode())
            target = repo / rel
            try:
                if target.is_file() and target.stat().st_size <= _FP_MAX_BYTES:
                    digest.update(target.read_bytes())
            except OSError:
                continue
        return digest.hexdigest()
    except Exception as exc:
        # Fail-soft here, hard refusal in `Cache.get`/`Cache.put`: without a
        # fingerprint the call still runs, it just never touches the cache.
        # Record the failure's type + message so a broken git setup stays
        # visible instead of silently degrading.
        log.debug(
            "repo fingerprint failed, caching disabled for this call: %s: %s",
            type(exc).__name__,
            exc,
        )
        return UNKNOWN_REPO_FINGERPRINT


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _template_fingerprint(template: Path | None) -> str:
    if template is None:
        return "builtin"
    try:
        stat = template.stat()
        digest = hashlib.sha256(template.read_bytes()).hexdigest()
        return f"{digest}:{stat.st_mtime_ns}"
    except OSError:
        return "missing"


def _key_repo_fingerprint(key: CacheKey) -> str | None:
    """The `repo_fp` recorded in a built key's JSON, or None when it has none.

    Unparseable key JSON (a corrupt or hand-built key) reads as None, which is
    not the unknown fingerprint — the refusal above only triggers on an
    explicit `"unknown"`, so a malformed key can neither bypass nor trip it.
    """
    try:
        value = json.loads(key.key_json).get("repo_fp")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def role_cache_key(
    *,
    role: str,
    worker: str,
    model: str | None,
    effort: str | None,
    prompt: str,
    workdir: Path,
    repo: Path,
    template: Path | None,
    lesson_texts: list[str],
    role_config_json: str,
    config: JevConfig,
    run_id: str,
) -> CacheKey:
    """Exact-match key for a read-only role call (phase 2)."""
    normalized = normalize_prompt(prompt, repo)
    text = canonical_json({"role": role, "prompt": normalized})
    components = {
        "v": 1,
        "kind": "role",
        "role": role,
        "worker": worker,
        "model": model,
        "effort": effort,
        "prompt": normalized,
        "repo_fp": repo_fingerprint(workdir),
        "template_fp": _template_fingerprint(template),
        "lessons_fp": hashlib.sha256(canonical_json(sorted(lesson_texts)).encode()).hexdigest(),
        "role_config_fp": hashlib.sha256(role_config_json.encode()).hexdigest(),
        # The jev version, like decision keys have: an upgrade that changes worker
        # output parsing or ROLE_SCHEMAS invalidates stale {result, parsed} entries.
        "jev_version": __version__,
        # run_id is provenance, filled in on put — not key material.
        "simhash": SimHashEmbedder().fingerprint(text),
    }
    return _build("role", components, text, config.cache.ttl_hours_overrides.roles)


# ---- stored entries ------------------------------------------------------------------


@dataclass
class CacheEntry:
    key_hash: str
    kind: str
    key_json: str
    value_json: str
    cost_usd: float  # the *original* call cost; a hit serves at $0
    created_at: str
    expires_at: str
    hit_count: int = 0
    last_hit_at: str | None = None

    @property
    def saved_usd(self) -> float:
        """Dollars this entry saved by serving hits instead of live calls."""
        return self.cost_usd * self.hit_count


@dataclass
class CacheStats:
    entries: int
    hits: int
    saved_usd: float
    size_bytes: int
    decisions: int
    roles: int
    shadow_agreement: float | None = None  # mean agreement of shadow measurements


# ---- embeddings (phase 3, default off) -----------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """Similarity backend for phase-3 near-duplicate matching."""

    def fingerprint(self, text: str) -> str:
        """A hex fingerprint of the normalized text."""
        ...

    def similarity(self, a: str, b: str) -> float:
        """0..1 similarity between two fingerprints from `fingerprint`."""
        ...


class SimHashEmbedder:
    """Stdlib 64-bit SimHash over character 5-grams: crude but free, deterministic,
    and dependency-free — good enough as the default semantic backend for
    near-duplicate detection (resume, re-runs of a similar idea)."""

    BITS = 64
    SHINGLE = 5

    def fingerprint(self, text: str) -> str:
        vec = [0] * self.BITS
        shingles = {text[i : i + self.SHINGLE] for i in range(max(0, len(text) - self.SHINGLE + 1))}
        if not shingles:
            shingles = {text}
        for shingle in shingles:
            h = int.from_bytes(hashlib.sha256(shingle.encode()).digest()[:8], "big")
            for bit in range(self.BITS):
                vec[bit] += 1 if (h >> bit) & 1 else -1
        out = 0
        for bit, weight in enumerate(vec):
            if weight > 0:
                out |= 1 << bit
        return f"{out:016x}"

    def similarity(self, a: str, b: str) -> float:
        xor = int(a, 16) ^ int(b, 16)
        return 1.0 - bin(xor).count("1") / self.BITS


class OpenRouterEmbedder:
    """Optional embedding-model backend (`[cache.semantic] embedder = "openrouter"`).

    Constructed only when configured; needs `OPENROUTER_API_KEY` in the environment.
    Embedding cost applies per lookup — only worth it for expensive role calls (§5).
    """

    def __init__(self, model: str = "text-embedding-3-small", api_key: str | None = None) -> None:
        import os

        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._vectors: dict[str, list[float]] = {}

    def fingerprint(self, text: str) -> str:
        import httpx

        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest not in self._vectors:
            response = httpx.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": text},
                timeout=60,
            )
            response.raise_for_status()
            self._vectors[digest] = response.json()["data"][0]["embedding"]
        return json.dumps(self._vectors[digest])

    def similarity(self, a: str, b: str) -> float:
        va = json.loads(a)
        vb = json.loads(b)
        dot = sum(x * y for x, y in zip(va, vb, strict=True))
        na = sum(x * x for x in va) ** 0.5
        nb = sum(x * x for x in vb) ** 0.5
        return dot / (na * nb) if na and nb else 0.0


# ---- the cache -----------------------------------------------------------------------


@dataclass
class Cache:
    """Policy wrapper over the `cache_entries` table in the repo's own state DB.

    The table is repo-scoped by construction (one `.jev/state.db` per repo), so
    cross-repo key collisions are structurally near-impossible.
    """

    store: Store
    settings: CacheSettings
    repo: Path
    _embedder: Embedder | None = field(default=None, init=False, repr=False)

    @property
    def usable(self) -> bool:
        """The master kill-switch (`[cache] enabled`, `--no-cache`)."""
        return self.settings.enabled

    def ttl_for(self, kind: str) -> float:
        if kind == "role":
            return self.settings.ttl_hours_overrides.roles
        if kind == "chat":
            return self.settings.ttl_hours_overrides.chat
        return self.settings.ttl_hours

    # -- exact match --

    def get(self, key: CacheKey) -> CacheEntry | None:
        """Return the entry and count the hit, or None on miss/expiry/disabled.

        A role key whose repo fingerprint is unknown is always a miss (issue
        #201): the check runs before the lookup, so a refused key never counts
        as a hit and can never serve a stale output, including one stored
        before this refusal existed.
        """
        if not self.usable:
            return None
        if key.kind == "role" and _key_repo_fingerprint(key) == UNKNOWN_REPO_FINGERPRINT:
            return None
        return self.store.cache_get(key.key_hash)

    def get_entry(self, key_hash: str) -> CacheEntry | None:
        """Fetch one entry by hash, counting the hit.

        The semantic serve path finds its candidate via `similar()` (a table scan,
        no hit counting); re-fetching here makes a served semantic hit account
        exactly like an exact hit in stats, saved dollars, and LRU order.
        """
        if not self.usable:
            return None
        return self.store.cache_get(key_hash)

    def put(
        self,
        key: CacheKey,
        value_json: str,
        cost_usd: float,
        *,
        run_id: str,
        decision_id: int | None = None,
    ) -> None:
        """Store a fresh answer. Callers must have enforced the cacheability rules.

        A role key whose repo fingerprint is unknown is never stored (issue #201).
        """
        if not self.usable:
            return
        if key.kind == "role" and _key_repo_fingerprint(key) == UNKNOWN_REPO_FINGERPRINT:
            return
        key_json = json.loads(key.key_json)
        key_json["decision_id"] = decision_id
        key_json["run_id"] = run_id
        self.store.cache_put(
            key.key_hash,
            key.kind,
            canonical_json(key_json),
            value_json,
            cost_usd,
            key.ttl_hours,
        )
        self.prune()

    # -- maintenance --

    def prune(self) -> None:
        """TTL sweep plus LRU / size eviction per `[cache] max_entries` / `max_mb`."""
        if not self.usable:
            return
        self.store.cache_delete_expired()
        self.store.cache_evict_lru(self.settings.max_entries)
        size_mb = self.store.cache_size_bytes() / (1024 * 1024)
        if size_mb > self.settings.max_mb:
            # Shrink the table proportionally to the size budget: keep the fraction
            # max_mb/size_mb of max_entries (never below 100, so a tiny budget does
            # not wipe a warm cache), evicting least-recently-hit first.
            keep = int(self.settings.max_entries * self.settings.max_mb / size_mb)
            self.store.cache_evict_lru(max(100, keep))

    def stats(self) -> CacheStats:
        return self.store.cache_stats()

    def clear(self, *, kind: str | None = None, older_than_hours: float | None = None) -> int:
        return self.store.cache_clear(kind=kind, older_than_hours=older_than_hours)

    # -- phase 3: semantic similarity (default off) --

    @property
    def semantic_enabled(self) -> bool:
        return self.usable and self.settings.semantic.enabled

    @property
    def semantic_mode(self) -> str:
        return self.settings.semantic.mode

    def embedder(self) -> Embedder:
        if self._embedder is None:
            name = self.settings.semantic.embedder
            if name == "openrouter":
                self._embedder = OpenRouterEmbedder()
            elif name == "simhash":
                self._embedder = SimHashEmbedder()
            else:
                raise ValueError(f"unknown [cache.semantic] embedder: {name!r}")
        return self._embedder

    def similar(self, kind: str, text: str, threshold: float) -> list[tuple[CacheEntry, float]]:
        """Entries of `kind` whose normalized text is at least `threshold`-similar.

        Used by shadow mode (measure) and — only when explicitly enabled — by live
        semantic serving. Scans the table; SimHash comparison is cheap, and the table
        is capped by `max_entries`.
        """
        if not self.semantic_enabled:
            return []
        embedder = self.embedder()
        needle = embedder.fingerprint(text)
        hits: list[tuple[CacheEntry, float]] = []
        for entry in self.store.cache_scan(kind):
            try:
                simhash = json.loads(entry.key_json).get("simhash")
            except (ValueError, AttributeError):
                continue
            if not simhash:
                continue
            try:
                sim = embedder.similarity(needle, simhash)
            except Exception as exc:
                # A stale entry written under another embedder (simhash hex vs
                # OpenRouter JSON vectors) must not crash the scan — skip it,
                # but record type + message (P7: a trust-boundary catch).
                log.debug(
                    "skipping cache entry with an incompatible simhash: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                continue
            if sim >= threshold:
                hits.append((entry, sim))
        hits.sort(key=lambda pair: pair[1], reverse=True)
        return hits

    def log_shadow(self, key_hash: str, kind: str, agreed: bool, agreement: float) -> None:
        self.store.cache_shadow_log(key_hash, kind, agreed, agreement)
