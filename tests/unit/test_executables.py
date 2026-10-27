"""`core.executables`: PATH-first CLI resolution with per-platform fallbacks (issue #333)."""

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


def test_resolve_prefers_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _make_executable(bindir / "claude")
    monkeypatch.setenv("PATH", str(bindir))
    assert executables.resolve_executable("claude") == str(bindir / "claude")


def test_resolve_returns_none_when_absent(bare_env: Path) -> None:
    assert executables.resolve_executable("definitely-not-a-cli") is None


def test_resolve_darwin_nvm_version_dir(monkeypatch: pytest.MonkeyPatch, bare_env: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    npx = _make_executable(
        bare_env / "home" / ".nvm" / "versions" / "node" / "v20.10.0" / "bin" / "npx"
    )
    assert executables.resolve_executable("npx") == str(npx)


def test_resolve_darwin_ignores_bare_nvm_root(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path
) -> None:
    """The old darwin list named `~/.nvm/versions/node` itself as a candidate dir, but no
    binary ever lives there — only `<version>/bin` does. A binary parked at the bare root
    must not resolve (it would be a false positive for a real nvm layout)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    _make_executable(bare_env / "home" / ".nvm" / "versions" / "node" / "npx")
    assert executables.resolve_executable("npx") is None


def test_resolve_linux_nvm_version_dir(monkeypatch: pytest.MonkeyPatch, bare_env: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    node = _make_executable(
        bare_env / "home" / ".nvm" / "versions" / "node" / "v18.19.0" / "bin" / "node"
    )
    assert executables.resolve_executable("node") == str(node)


def test_resolve_linux_prefers_newest_nvm_version(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    root = bare_env / "home" / ".nvm" / "versions" / "node"
    _make_executable(root / "v18.19.0" / "bin" / "node")
    newest = _make_executable(root / "v20.10.0" / "bin" / "node")
    assert executables.resolve_executable("node") == str(newest)


def _win32_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "programfiles"))
    # `shutil.which` reads Windows-only internals when `sys.platform` is
    # "win32", so it cannot run on the Linux CI host even faked: stub the
    # PATH half out — these tests cover the fallback dirs, not `which`.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    return tmp_path


def test_resolve_win32_nvm_version_dir(monkeypatch: pytest.MonkeyPatch, bare_env: Path) -> None:
    """nvm-windows keeps node.exe directly in the version dir (no `bin` level)."""
    _win32_env(monkeypatch, bare_env)
    node = _make_executable(bare_env / "appdata" / "nvm" / "v20.10.0" / "node.exe")
    assert executables.resolve_executable("node") == str(node)


def test_resolve_win32_npm_cmd(monkeypatch: pytest.MonkeyPatch, bare_env: Path) -> None:
    """npm tools on Windows are `.cmd` shims; the fallback must try PATHEXT names."""
    _win32_env(monkeypatch, bare_env)
    npx = _make_executable(bare_env / "appdata" / "npm" / "npx.cmd")
    assert executables.resolve_executable("npx") == str(npx)


def test_resolve_win32_name_with_extension_not_doubled(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path
) -> None:
    _win32_env(monkeypatch, bare_env)
    npx = _make_executable(bare_env / "appdata" / "npm" / "npx.cmd")
    assert executables.resolve_executable("npx.cmd") == str(npx)


def test_augment_path_prepends_discovered_dirs(
    monkeypatch: pytest.MonkeyPatch, bare_env: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    bindir = bare_env / "home" / ".nvm" / "versions" / "node" / "v20.10.0" / "bin"
    _make_executable(bindir / "claude")
    added = executables.augment_path_with_known_clis(("claude", "no-such-cli"))
    assert added == [str(bindir)]
    assert os.environ["PATH"].split(os.pathsep)[0] == str(bindir)
    assert executables.resolve_executable("claude") == str(bindir / "claude")
    # Idempotent: a second call adds nothing.
    assert executables.augment_path_with_known_clis(("claude",)) == []


def test_augment_path_skips_dirs_already_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    _make_executable(bindir / "claude")
    monkeypatch.setenv("PATH", str(bindir))
    assert executables.augment_path_with_known_clis(("claude",)) == []
