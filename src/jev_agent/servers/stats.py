"""Server health stats: one SSH batch, parsed into a summary.

The remote script is POSIX sh plus ubiquitous tools (`hostname`, `uname`,
`uptime`, `df`, `ps`, and either `free`/`nproc` on Linux or `vm_stat`/`sysctl`
on macOS). Sections are delimited by `==NAME==` markers so parsing never
depends on locale-specific prose. Values are numbers where possible; the raw
`uptime` line is kept for display.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jev_agent.config.servers_schema import ServerConfig
    from jev_agent.servers.ssh import SshRunner

__all__ = [
    "STATS_SCRIPT",
    "MountStat",
    "ProcStat",
    "ServerStats",
    "collect_stats",
    "parse_stats",
]

STATS_SCRIPT = """\
echo '==HOST=='; hostname 2>/dev/null || echo unknown
echo '==OS=='; uname -sr 2>/dev/null || echo unknown
echo '==UPTIME=='; uptime 2>/dev/null || echo unknown
echo '==CORES=='; (nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1) | tr -d ' '
echo '==LOAD=='
(cat /proc/loadavg 2>/dev/null | awk '{print $1, $2, $3}'
 sysctl -n vm.loadavg 2>/dev/null | tr -d '{}') | head -1
echo '==MEM=='
if command -v free >/dev/null 2>&1; then
  free -m | awk 'NR==2 {printf "%d %d %d\\n", $2, $3, $7}'
else
  _psz=$(vm_stat 2>/dev/null | awk '/page size of/{print $8}'); _psz=${_psz:-4096}
  _mem_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
  _total_mb=$(( _mem_bytes / 1048576 ))
  _free=$(vm_stat 2>/dev/null | awk '/Pages free:/{gsub(/[.]/,"",$3); print $3}')
  _free=${_free:-0}
  _inact=$(vm_stat 2>/dev/null | awk '/Pages inactive:/{gsub(/[.]/,"",$3); print $3}')
  _inact=${_inact:-0}
  _avail_mb=$(( (_free + _inact) * _psz / 1048576 ))
  _used_mb=$(( _total_mb - _avail_mb ))
  echo "$_total_mb $_used_mb $_avail_mb"
fi
echo '==CPUTOTAL=='
if ps -eo pcpu --no-headers >/dev/null 2>&1; then
  ps -eo pcpu --no-headers 2>/dev/null | awk '{s+=$1} END {printf "%.1f\\n", s+0}'
else
  ps -Ao %cpu 2>/dev/null | awk 'NR>1 {s+=$1} END {printf "%.1f\\n", s+0}'
fi
echo '==DISK=='
df -Pk 2>/dev/null | awk 'NR>1 && $1 !~ /^(tmpfs|devtmpfs|udev)$/ {
  m=$6; for (i=7; i<=NF; i++) m=m " " $i; print $1, $2, $3, $4, $5, m}'
echo '==TOPCPU=='
if ps -eo comm,pcpu,pmem --sort=-pcpu >/dev/null 2>&1; then
  ps -eo comm,pcpu,pmem --sort=-pcpu 2>/dev/null | head -9
else
  ps -Ao comm,%cpu,%mem 2>/dev/null | head -9
fi
echo '==TOPMEM=='
if ps -eo comm,pcpu,pmem --sort=-pmem >/dev/null 2>&1; then
  ps -eo comm,pcpu,pmem --sort=-pmem 2>/dev/null | head -9
else
  ps -Ao comm,%cpu,%mem -m 2>/dev/null | head -9 || ps -Ao comm,%cpu,%mem 2>/dev/null | head -9
fi
"""

_SECTION = re.compile(r"^==([A-Z]+)==$")


@dataclass(frozen=True)
class MountStat:
    device: str
    mount: str
    total_gb: float
    used_gb: float
    avail_gb: float
    use_pct: float


@dataclass(frozen=True)
class ProcStat:
    name: str
    cpu_pct: float
    mem_pct: float


@dataclass(frozen=True)
class ServerStats:
    """Parsed health summary. Percentages are 0-100; sizes in GiB."""

    hostname: str = "unknown"
    os: str = "unknown"
    uptime: str = "unknown"
    load_1: float = 0.0
    load_5: float = 0.0
    load_15: float = 0.0
    cpu_cores: int = 1
    cpu_pct: float = 0.0
    mem_total_gb: float = 0.0
    mem_used_gb: float = 0.0
    mem_avail_gb: float = 0.0
    mem_pct: float = 0.0
    mounts: tuple[MountStat, ...] = ()
    top_cpu: tuple[ProcStat, ...] = ()
    top_mem: tuple[ProcStat, ...] = ()
    collected_at: float = 0.0
    ok: bool = False
    error: str = ""

    @property
    def disk_pct(self) -> float:
        """Highest use% across mounts (the headline disk number)."""
        return max((m.use_pct for m in self.mounts), default=0.0)

    @property
    def root_mount(self) -> MountStat | None:
        for m in self.mounts:
            if m.mount == "/":
                return m
        return self.mounts[0] if self.mounts else None


def _sections(output: str) -> dict[str, list[str]]:
    current: str | None = None
    sections: dict[str, list[str]] = {}
    for line in output.splitlines():
        marker = _SECTION.match(line.strip())
        if marker:
            current = marker.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _first_line(sections: dict[str, list[str]], name: str) -> str:
    lines = [ln.strip() for ln in sections.get(name, []) if ln.strip()]
    return lines[0] if lines else ""


_PSEUDO_FS = ("tmpfs", "devtmpfs", "udev", "overlay", "squashfs")


def _parse_mounts(lines: list[str]) -> tuple[MountStat, ...]:
    mounts: list[MountStat] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        if parts[0] in _PSEUDO_FS:
            continue
        try:
            total_kb, used_kb, avail_kb = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        use_pct = 0.0
        pct_raw = parts[4].rstrip("%")
        try:
            use_pct = float(pct_raw)
        except ValueError:
            if total_kb > 0:
                use_pct = round(used_kb / total_kb * 100, 1)
        mounts.append(
            MountStat(
                device=parts[0],
                # A mount point may itself contain spaces ("/Volumes/My Drive"):
                # the script joins fields 6+ with spaces, so rejoin them here
                # instead of truncating at the first word (issue #204).
                mount=" ".join(parts[5:]),
                total_gb=round(total_kb / 1048576, 1),
                used_gb=round(used_kb / 1048576, 1),
                avail_gb=round(avail_kb / 1048576, 1),
                use_pct=use_pct,
            )
        )
    # Most interesting first: root, then by usage descending.
    mounts.sort(key=lambda m: (m.mount != "/", -m.use_pct))
    return tuple(mounts)


def _parse_procs(lines: list[str]) -> tuple[ProcStat, ...]:
    procs: list[ProcStat] = []
    for line in lines[1:]:  # skip the header row
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            cpu, mem = float(parts[-2]), float(parts[-1])
        except ValueError:
            continue
        name = " ".join(parts[:-2]).strip() or "?"
        # `ps` comm may be "(name)" or a full path; keep the basename.
        name = name.strip("()").split("/")[-1]
        procs.append(ProcStat(name=name, cpu_pct=cpu, mem_pct=mem))
    return tuple(procs[:8])


def parse_stats(output: str, *, collected_at: float = 0.0) -> ServerStats:
    """Parse one `STATS_SCRIPT` run into a `ServerStats`."""
    sections = _sections(output)
    if not sections.get("HOST"):
        return ServerStats(ok=False, error="empty output from server")

    try:
        cores = max(1, int(_first_line(sections, "CORES") or "1"))
    except ValueError:
        cores = 1

    load_parts = _first_line(sections, "LOAD").split()

    def _load(i: int) -> float:
        try:
            return float(load_parts[i])
        except (IndexError, ValueError):
            return 0.0

    mem_parts = _first_line(sections, "MEM").split()
    mem_total_mb, mem_used_mb, mem_avail_mb = 0.0, 0.0, 0.0
    if len(mem_parts) >= 3:
        with contextlib.suppress(ValueError):
            mem_total_mb, mem_used_mb, mem_avail_mb = (
                float(mem_parts[0]),
                float(mem_parts[1]),
                float(mem_parts[2]),
            )
    mem_total_gb = round(mem_total_mb / 1024, 1)
    mem_used_gb = round(mem_used_mb / 1024, 1)
    mem_avail_gb = round(mem_avail_mb / 1024, 1)
    mem_pct = round(mem_used_mb / mem_total_mb * 100, 1) if mem_total_mb > 0 else 0.0

    try:
        cpu_total = float(_first_line(sections, "CPUTOTAL") or "0")
    except ValueError:
        cpu_total = 0.0
    cpu_pct = round(min(100.0, cpu_total / cores), 1)

    return ServerStats(
        hostname=_first_line(sections, "HOST") or "unknown",
        os=_first_line(sections, "OS") or "unknown",
        uptime=_first_line(sections, "UPTIME") or "unknown",
        load_1=_load(0),
        load_5=_load(1),
        load_15=_load(2),
        cpu_cores=cores,
        cpu_pct=cpu_pct,
        mem_total_gb=mem_total_gb,
        mem_used_gb=mem_used_gb,
        mem_avail_gb=mem_avail_gb,
        mem_pct=mem_pct,
        mounts=_parse_mounts(sections.get("DISK", [])),
        top_cpu=_parse_procs(sections.get("TOPCPU", [])),
        top_mem=_parse_procs(sections.get("TOPMEM", [])),
        collected_at=collected_at or time.time(),
        ok=True,
    )


def collect_stats(
    server: ServerConfig,
    *,
    runner: SshRunner,
    state_dir: Path,
    timeout_s: float = 30.0,
) -> ServerStats:
    """Run the stats script on `server` via SSH and parse the result.

    One timeout wrap only: `SshTransport.run` already wraps the command with
    `time_limited`, which subtracts the kill grace (2s) plus 1s from its timeout —
    wrapping here as well made the script die at 27s for a 30s timeout (issue #204).
    Passing `timeout_s + 3` makes the remote limit exactly `timeout_s`, with the
    local ssh timeout 3s behind it so the remote kill lands first.
    """
    from jev_agent.servers.ssh import REMOTE_KILL_GRACE_S, SshTransport

    transport = SshTransport(server, sockets=state_dir / "ssh", runner=runner)
    result = transport.run(STATS_SCRIPT, timeout_s=timeout_s + REMOTE_KILL_GRACE_S + 1)
    if not result.ok:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:200]
        if result.timed_out:
            detail = "timed out"
        return ServerStats(ok=False, error=detail or f"ssh exited {result.exit_code}")
    stats = parse_stats(result.stdout.decode("utf-8", errors="replace"))
    if not stats.ok:
        return stats
    return stats


# ---- background cache ------------------------------------------------------------


@dataclass
class _CachedEntry:
    stats: ServerStats | None = None
    state: str = "never"  # "never" | "collecting" | "ready" | "failed"


class ServerStatsCache:
    """Per-server stats, collected in background threads and cached as JSON.

    `get(name)` never blocks and never opens an SSH connection. `refresh(...)`
    starts a background collection unless one is already running for that
    server. JSON lives under `<state_dir>/server-stats/<name>.json`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _CachedEntry] = {}

    def _cache_path(self, state_dir: Path, name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        return state_dir / "server-stats" / f"{safe}.json"

    def _read_disk(self, state_dir: Path, name: str) -> ServerStats | None:
        try:
            raw = json.loads(self._cache_path(state_dir, name).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        try:
            mounts = tuple(MountStat(**m) for m in raw.get("mounts", ()))
            top_cpu = tuple(ProcStat(**p) for p in raw.get("top_cpu", ()))
            top_mem = tuple(ProcStat(**p) for p in raw.get("top_mem", ()))
            return ServerStats(
                **{k: v for k, v in raw.items() if k not in ("mounts", "top_cpu", "top_mem")},
                mounts=mounts,
                top_cpu=top_cpu,
                top_mem=top_mem,
            )
        except (TypeError, ValueError):
            return None

    def get(self, state_dir: Path, name: str) -> tuple[ServerStats | None, str]:
        """(stats, state). Loads from disk on first call; never blocks on SSH."""
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                entry = _CachedEntry(stats=self._read_disk(state_dir, name))
                entry.state = "ready" if entry.stats and entry.stats.ok else "never"
                self._entries[name] = entry
            return entry.stats, entry.state

    def refresh(
        self,
        name: str,
        server: ServerConfig,
        state_dir: Path,
        runner: SshRunner,
    ) -> bool:
        """Start a background collection. False when one is already running."""
        with self._lock:
            entry = self._entries.setdefault(name, _CachedEntry())
            if entry.state == "collecting":
                return False
            entry.state = "collecting"

        def _work() -> None:
            # A raising runner must not kill this thread with the entry stuck
            # at "collecting": stats would then never refresh for the process
            # lifetime, and `refresh()` would keep refusing (issue #195). A
            # failed collection records "failed" (keeping the last good stats),
            # so the next `refresh()` can start a new attempt. The exception is
            # recorded in the error field, never silently discarded (P7).
            try:
                stats = collect_stats(server, runner=runner, state_dir=state_dir)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}".strip()[:200]
                stats = ServerStats(ok=False, error=detail or "collection failed")
            if stats.ok:
                path = self._cache_path(state_dir, name)
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    payload = asdict(stats)
                    payload["mounts"] = [asdict(m) for m in stats.mounts]
                    payload["top_cpu"] = [asdict(p) for p in stats.top_cpu]
                    payload["top_mem"] = [asdict(p) for p in stats.top_mem]
                    path.write_text(json.dumps(payload), "utf-8")
                except OSError:
                    pass
            with self._lock:
                self._entries[name] = _CachedEntry(
                    stats=stats if stats.ok else entry.stats,
                    state="ready" if stats.ok else "failed",
                )

        thread = threading.Thread(target=_work, name=f"server-stats-{name}", daemon=True)
        thread.start()
        return True
