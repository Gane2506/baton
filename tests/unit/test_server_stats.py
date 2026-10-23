"""Tests for jev_agent.servers.stats (issue #143)."""

from __future__ import annotations

import json
import time

import pytest

from jev_agent.servers.stats import (
    ServerStatsCache,
    collect_stats,
    parse_stats,
)

LINUX_OUTPUT = """\
==HOST==
web-01
==OS==
Linux 6.8.0
==UPTIME==
 10:23:45 up 12 days,  3:14,  1 user,  load average: 0.42, 0.38, 0.35
==CORES==
4
==LOAD==
0.42 0.38 0.35
==MEM==
7976 2345 5234
==CPUTOTAL==
86.4
==DISK==
/dev/sda1 104857600 52428800 52428800 50% /
/dev/sdb1 209715200 104857600 104857600 50% /data
tmpfs 1024 0 1024 0% /dev/shm
==TOPCPU==
COMMAND         %CPU %MEM
python3         45.2 12.1
nginx           12.0  2.3
postgres         8.1  5.4
redis-server     4.2  1.1
systemd          0.1  0.2
==TOPMEM==
COMMAND         %CPU %MEM
python3         45.2 12.1
postgres         8.1  5.4
java            22.0  8.8
nginx           12.0  2.3
redis-server     4.2  1.1
"""

MACOS_OUTPUT = """\
==HOST==
macbook-pro
==OS==
Darwin 24.1.0
==UPTIME==
10:23  up 3 days,  5:12, 2 users, load averages: 1.84 1.62 1.55
==CORES==
10
==LOAD==
1.84 1.62 1.55
==MEM==
32768 20480 8192
==CPUTOTAL==
231.5
==DISK==
/dev/disk3s1s1 488245288 321000000 150000000 69% /
==TOPCPU==
COMM             %CPU %MEM
kernel_task      12.0  3.1
Google Chrome    45.0  8.2
Code Helper      22.1  4.5
==TOPMEM==
COMM             %CPU %MEM
Google Chrome    45.0  8.2
Code Helper      22.1  4.5
kernel_task      12.0  3.1
"""


def test_parse_linux() -> None:
    stats = parse_stats(LINUX_OUTPUT, collected_at=1234.0)
    assert stats.ok
    assert stats.hostname == "web-01"
    assert stats.os == "Linux 6.8.0"
    assert stats.cpu_cores == 4
    assert stats.cpu_pct == pytest.approx(21.6)  # 86.4 / 4
    assert stats.mem_total_gb == pytest.approx(7.8, abs=0.1)
    assert stats.mem_pct == pytest.approx(29.4, abs=0.2)
    assert stats.load_1 == pytest.approx(0.42)
    # tmpfs excluded
    assert [m.mount for m in stats.mounts] == ["/", "/data"]
    root = stats.root_mount
    assert root is not None and root.mount == "/"
    assert root.use_pct == 50.0
    assert stats.disk_pct == 50.0
    assert stats.top_cpu[0].name == "python3"
    assert stats.top_cpu[0].cpu_pct == pytest.approx(45.2)
    assert len(stats.top_cpu) == 5
    assert stats.top_mem[0].name == "python3"
    assert stats.collected_at == 1234.0


def test_parse_macos() -> None:
    stats = parse_stats(MACOS_OUTPUT)
    assert stats.ok
    assert stats.hostname == "macbook-pro"
    assert stats.cpu_cores == 10
    assert stats.cpu_pct == pytest.approx(23.2, abs=0.1)  # 231.5 / 10
    assert stats.mem_total_gb == pytest.approx(32.0)
    assert len(stats.mounts) == 1
    assert stats.disk_pct == 69.0


def test_parse_empty_is_not_ok() -> None:
    stats = parse_stats("")
    assert not stats.ok
    assert stats.error


def test_parse_missing_sections_degrades() -> None:
    stats = parse_stats("==HOST==\nhost1\n")
    assert stats.ok  # hostname present; everything else defaults
    assert stats.hostname == "host1"
    assert stats.cpu_pct == 0.0
    assert stats.mounts == ()


def test_cpu_pct_capped_at_100() -> None:
    out = LINUX_OUTPUT.replace("==CPUTOTAL==\n86.4", "==CPUTOTAL==\n800.0")
    stats = parse_stats(out)
    assert stats.cpu_pct == 100.0


class _FakeRunner:
    def __init__(self, stdout: bytes, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.calls = 0

    def __call__(self, argv, *, stdin, timeout_s):
        from jev_agent.servers.ssh import SshResult

        self.calls += 1
        return SshResult(self.exit_code, self.stdout, b"", False, 0.1)


def _server_config() -> object:
    from jev_agent.config.servers_schema import ServerConfig

    return ServerConfig(host="example.com", user="u")


def test_collect_stats_ok(tmp_path) -> None:
    runner = _FakeRunner(LINUX_OUTPUT.encode())
    stats = collect_stats(
        _server_config(),
        runner=runner,
        state_dir=tmp_path,  # type: ignore[arg-type]
    )
    assert stats.ok
    assert stats.hostname == "web-01"
    assert runner.calls == 1


def test_collect_stats_ssh_failure(tmp_path) -> None:
    runner = _FakeRunner(b"ssh: connect timeout", exit_code=255)
    stats = collect_stats(
        _server_config(),
        runner=runner,
        state_dir=tmp_path,  # type: ignore[arg-type]
    )
    assert not stats.ok
    assert stats.error


def test_cache_refresh_and_get(tmp_path) -> None:
    cache = ServerStatsCache()
    runner = _FakeRunner(LINUX_OUTPUT.encode())
    stats, state = cache.get(tmp_path, "web")
    assert state == "never"
    assert stats is None

    assert cache.refresh("web", _server_config(), tmp_path, runner)  # type: ignore[arg-type]
    # Second refresh while collecting is a no-op.
    assert not cache.refresh("web", _server_config(), tmp_path, runner)  # type: ignore[arg-type]

    deadline = time.time() + 5
    while time.time() < deadline:
        stats, state = cache.get(tmp_path, "web")
        if state in ("ready", "failed"):
            break
        time.sleep(0.05)
    assert state == "ready"
    assert stats is not None and stats.ok
    assert stats.hostname == "web-01"

    # Persisted to disk: a fresh cache instance loads it without SSH.
    cache2 = ServerStatsCache()
    stats2, state2 = cache2.get(tmp_path, "web")
    assert state2 == "ready"
    assert stats2 is not None and stats2.hostname == "web-01"
    assert runner.calls == 1  # no new SSH call


def test_cache_failed_keeps_old_stats(tmp_path) -> None:
    cache = ServerStatsCache()
    good = _FakeRunner(LINUX_OUTPUT.encode())
    assert cache.refresh("web", _server_config(), tmp_path, good)  # type: ignore[arg-type]
    deadline = time.time() + 5
    while cache.get(tmp_path, "web")[1] not in ("ready", "failed") and time.time() < deadline:
        time.sleep(0.05)

    bad = _FakeRunner(b"nope", exit_code=255)
    assert cache.refresh("web", _server_config(), tmp_path, bad)  # type: ignore[arg-type]
    deadline = time.time() + 5
    while cache.get(tmp_path, "web")[1] not in ("ready", "failed") and time.time() < deadline:
        time.sleep(0.05)
    stats, state = cache.get(tmp_path, "web")
    assert state == "failed"
    # Old good stats are still served.
    assert stats is not None and stats.ok


def test_cache_json_roundtrip(tmp_path) -> None:
    stats = parse_stats(LINUX_OUTPUT, collected_at=999.0)
    assert stats.disk_pct == 50.0
    assert isinstance(stats.mounts[0].total_gb, float)
    # asdict must survive JSON.
    from dataclasses import asdict

    payload = asdict(stats)
    payload["mounts"] = [asdict(m) for m in stats.mounts]
    raw = json.loads(json.dumps(payload))
    assert raw["hostname"] == "web-01"
    assert raw["mounts"][0]["mount"] == "/"
