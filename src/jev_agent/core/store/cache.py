"""Semantic cache table persistence (issue #10: `core/cache.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta

from jev_agent.core.cache import CacheEntry, CacheStats

from .base import StoreBase
from .rows import _cache_entry, _iso


class CacheMixin(StoreBase):
    """Cache entries and shadow measurements."""

    def cache_get(self, key_hash: str) -> CacheEntry | None:
        """Return the entry for `key_hash` and count the hit; None on miss or expiry.

        Expired entries are deleted on read (the TTL sweep in `Cache.prune` handles
        the rest).
        """
        now = self._clock()
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM cache_entries WHERE key_hash = ?", (key_hash,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                db.execute("DELETE FROM cache_entries WHERE key_hash = ?", (key_hash,))
                return None
            db.execute(
                "UPDATE cache_entries SET hit_count = hit_count + 1, last_hit_at = ?"
                " WHERE key_hash = ?",
                (now, key_hash),
            )
            return _cache_entry(row, hit_count=row["hit_count"] + 1, last_hit_at=now)

    def cache_put(
        self,
        key_hash: str,
        kind: str,
        key_json: str,
        value_json: str,
        cost_usd: float,
        ttl_hours: float,
    ) -> None:
        """Store (or replace) one entry; `cost_usd` is the original call cost."""
        now = self._clock()
        expires = _iso(datetime.fromisoformat(now) + timedelta(hours=ttl_hours))
        with self._tx() as db:
            db.execute(
                "INSERT INTO cache_entries (key_hash, kind, key_json, value_json,"
                " cost_usd, created_at, expires_at, hit_count, last_hit_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)"
                " ON CONFLICT (key_hash) DO UPDATE SET"
                " kind = excluded.kind, key_json = excluded.key_json,"
                " value_json = excluded.value_json, cost_usd = excluded.cost_usd,"
                " created_at = excluded.created_at, expires_at = excluded.expires_at,"
                " hit_count = 0, last_hit_at = NULL",
                (key_hash, kind, key_json, value_json, cost_usd, now, expires),
            )

    def cache_scan(self, kind: str) -> list[CacheEntry]:
        """All live entries of one kind, for phase-3 similarity scans."""
        now = self._clock()
        rows = self._conn.execute(
            "SELECT * FROM cache_entries WHERE kind = ? AND expires_at > ?",
            (kind, now),
        ).fetchall()
        return [_cache_entry(row) for row in rows]

    def cache_delete_expired(self) -> int:
        with self._tx() as db:
            cur = db.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (self._clock(),))
            return cur.rowcount

    def cache_evict_lru(self, keep: int) -> int:
        """Drop the least-recently-hit entries past `keep` (LRU on `last_hit_at`)."""
        with self._tx() as db:
            cur = db.execute(
                "DELETE FROM cache_entries WHERE key_hash NOT IN ("
                " SELECT key_hash FROM cache_entries"
                " ORDER BY COALESCE(last_hit_at, created_at) DESC LIMIT ?)",
                (keep,),
            )
            return cur.rowcount

    def cache_size_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(key_json) + LENGTH(value_json)), 0) FROM cache_entries"
        ).fetchone()
        return int(row[0])

    def cache_stats(self) -> CacheStats:
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hit_count), 0),"
            " COALESCE(SUM(cost_usd * hit_count), 0),"
            " COALESCE(SUM(CASE WHEN kind = 'decision' THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN kind = 'role' THEN 1 ELSE 0 END), 0)"
            " FROM cache_entries"
        ).fetchone()
        shadow = self._conn.execute("SELECT COUNT(*), AVG(agreement) FROM cache_shadow").fetchone()
        return CacheStats(
            entries=int(row[0]),
            hits=int(row[1]),
            saved_usd=float(row[2]),
            size_bytes=self.cache_size_bytes(),
            decisions=int(row[3]),
            roles=int(row[4]),
            shadow_agreement=None if shadow[0] == 0 else float(shadow[1] or 0.0),
        )

    def cache_saved_by_run(self) -> dict[str, float]:
        """Dollars saved per run (`cost_usd * hit_count` of its entries)."""
        rows = self._conn.execute(
            "SELECT json_extract(key_json, '$.run_id') AS run_id,"
            " COALESCE(SUM(cost_usd * hit_count), 0) FROM cache_entries"
            " GROUP BY run_id HAVING SUM(cost_usd * hit_count) > 0"
        ).fetchall()
        return {row[0]: float(row[1]) for row in rows if row[0]}

    def cache_clear(self, *, kind: str | None = None, older_than_hours: float | None = None) -> int:
        """Delete entries, optionally filtered by kind and/or age. Returns the count."""
        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if older_than_hours is not None:
            cutoff = _iso(datetime.fromisoformat(self._clock()) - timedelta(hours=older_than_hours))
            clauses.append("created_at <= ?")
            params.append(cutoff)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._tx() as db:
            cur = db.execute(f"DELETE FROM cache_entries{where}", params)
            return cur.rowcount

    def cache_shadow_log(self, key_hash: str, kind: str, agreed: bool, agreement: float) -> None:
        """Record one shadow-mode measurement (phase-3 gate, issue #10 §6)."""
        with self._tx() as db:
            db.execute(
                "INSERT INTO cache_shadow (key_hash, kind, agreed, agreement, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key_hash, kind, int(agreed), agreement, self._clock()),
            )
