"""`core.executables`: PATH-first CLI resolution with per-platform fallbacks (issue #333).

Two halves, tested separately: `_platform_search_dirs` (which directories per
platform, with a fake HOME/APPDATA) and `resolve_executable` (PATH first, then
the fallback list — the list itself is patched out there, so a real `node` in
`/usr/local/bin` on the CI runner cannot leak into the test).
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

from jev_agent.core import executables


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def bare_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A PATH with no CLIs and a fake HOME, so only the fallback dirs can answer."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def fake_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """`_platform_search_dirs` patched to one fake dir: `resolve_executable`'s
    fallback behaviour, without any real install location leaking in."""
    fallback = tmp_path / "fallback-bin"
    fallback.mkdir()
    monkeypatch.setattr(executables, "_platform_search_dirs", lambda: [fallback])
    return fallback


def test_resolve_prefers_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _make_executable(bindir / "claude")
    monkeypatch.setenv("PATH", str(bindir))
    assert executables.resolve_executable("claude") == str(bindir / "claude")


def test_resolve_returns_none_when_absent(bare_env: Path, fake_fallback: Path) -> None:
    assert executables.resolve_executable("definitely-not-a-cli") is None


def test_resolve_falls_back_to_search_dirs(bare_env: Path, fake_fallback: Path) -> None:
    npx = _make_executable(fake_fallback / "npx")
    assert executables.resolve_executable("npx") == str(npx)


def test_resolve_win32_npm_cmd(monkeypatch: pytest.MonkeyPatch, bare_env: Path) -> None:
    """npm tools on Windows are `.cmd` shims; the fallback must try PATHEXT names."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(bare_env / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(bare_env / "localappdata"))
    monkeypatch.setenv("PROGRAMFILES", str(bare_env / "programfiles"))
    # `shutil.which` reads Windows-only internals when `sys.platform` is
    # "win32", so it cannot run on the Linux CI host even faked: stub the
    # PATH half out — these tests cover the fallback dirs, not `which`.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    npx = _make_executable(bare_env / "appdata" / "npm" / "npx.cmd")
    assert executables.resolve_executable("npx") == str(npx)


def test_resolve_win32_name_with_extension_not_doubled(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(bare_env / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(bare_env / "localappdata"))
    monkeypatch.setenv("PROGRAMFILES", str(bare_env / "programfiles"))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    npx = _make_executable(bare_env / "appdata" / "npm" / "npx.cmd")
    assert executables.resolve_executable("npx.cmd") == str(npx)


def test_platform_search_dirs_darwin_enumerates_nvm(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    home = bare_env / "home"
    nvm = home / ".nvm" / "versions" / "node"
    (nvm / "v20.10.0" / "bin").mkdir(parents=True)
    (nvm / "v18.19.0" / "bin").mkdir(parents=True)
    dirs = executables._platform_search_dirs()
    assert dirs[0] == Path("/opt/homebrew/bin")
    assert dirs[1] == Path("/usr/local/bin")
    # The version dirs are enumerated (newest first); the bare nvm root itself
    # is NOT a candidate — no binary ever lives there.
    assert nvm / "v20.10.0" / "bin" in dirs
    assert nvm / "v18.19.0" / "bin" in dirs
    assert nvm not in dirs
    assert dirs.index(nvm / "v20.10.0" / "bin") < dirs.index(nvm / "v18.19.0" / "bin")


def test_platform_search_dirs_linux_enumerates_nvm(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    home = bare_env / "home"
    nvm = home / ".nvm" / "versions" / "node"
    (nvm / "v20.10.0" / "bin").mkdir(parents=True)
    dirs = executables._platform_search_dirs()
    assert Path("/usr/local/bin") in dirs
    assert home / ".local" / "bin" in dirs
    assert nvm / "v20.10.0" / "bin" in dirs


def test_platform_search_dirs_win32_enumerates_nvm(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path
) -> None:
    """nvm-windows keeps node.exe directly in the version dir (no `bin` level)."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(bare_env / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(bare_env / "localappdata"))
    monkeypatch.setenv("PROGRAMFILES", str(bare_env / "programfiles"))
    nvm = bare_env / "appdata" / "nvm"
    (nvm / "v20.10.0").mkdir(parents=True)
    dirs = executables._platform_search_dirs()
    assert bare_env / "appdata" / "npm" in dirs
    assert bare_env / "programfiles" / "nodejs" in dirs
    assert nvm / "v20.10.0" in dirs


def test_augment_path_prepends_discovered_dirs(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path, fake_fallback: Path
) -> None:
    _make_executable(fake_fallback / "claude")
    added = executables.augment_path_with_known_clis(("claude", "no-such-cli"))
    assert added == [str(fake_fallback)]
    assert os.environ["PATH"].split(os.pathsep)[0] == str(fake_fallback)
    assert executables.resolve_executable("claude") == str(fake_fallback / "claude")
    # Idempotent: a second call adds nothing.
    assert executables.augment_path_with_known_clis(("claude",)) == []


def test_augment_path_skips_dirs_already_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    _make_executable(bindir / "claude")
    monkeypatch.setenv("PATH", str(bindir))
    assert executables.augment_path_with_known_clis(("claude",)) == []
