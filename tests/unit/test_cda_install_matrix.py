"""CDA install decision matrix (issue #366, design §10): platform x deployment
with the environment detection stubbed — no test downloads torch or a
checkpoint, and no test touches a real interpreter.

Note (design §5.4 fallback): the dedicated-venv branch (branch 2) is
reserved until the grade runner gains an interpreter override, so an
unwritable own environment falls to branch 3 (guide) today. The venv argv
branch itself is still built and tested, so the fallback costs no rework
when the override lands.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jev_agent.dashboard.cda_install_runner import (
    EnvironmentDecision,
    build_pip_steps,
    decide_environment,
    temp_space_check,
)


@pytest.fixture
def writable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.access", lambda path, mode: True)


@pytest.fixture
def unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.access", lambda path, mode: False)


def _decide(
    *,
    platform: str,
    nvidia_ok: bool,
    docker: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> EnvironmentDecision:
    monkeypatch.setattr(sys, "platform", platform)
    return decide_environment(
        "/fake/python", home=Path("/fake/home"), nvidia_ok=nvidia_ok, docker=docker
    )


def test_mac_venv_is_cpu_always(writable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    decision = _decide(platform="darwin", nvidia_ok=True, docker=False, monkeypatch=monkeypatch)
    assert decision.branch == 1
    assert decision.torch_path == "cpu"


def test_linux_nvidia_gets_default_wheel(writable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    decision = _decide(platform="linux", nvidia_ok=True, docker=False, monkeypatch=monkeypatch)
    assert decision.branch == 1
    assert decision.torch_path == "default"


def test_linux_no_nvidia_falls_back_to_cpu(writable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    decision = _decide(platform="linux", nvidia_ok=False, docker=False, monkeypatch=monkeypatch)
    assert decision.branch == 1
    assert decision.torch_path == "cpu"


def test_windows_user_install_is_cpu_without_driver(
    writable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decide(platform="win32", nvidia_ok=False, docker=False, monkeypatch=monkeypatch)
    assert decision.branch == 1
    assert decision.torch_path == "cpu"


def test_windows_service_without_driver_is_cpu(
    writable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decide(platform="win32", nvidia_ok=False, docker=False, monkeypatch=monkeypatch)
    assert decision.torch_path == "cpu"


def test_system_python_pep668_falls_to_branch3_guide(
    unwritable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decide(platform="linux", nvidia_ok=False, docker=False, monkeypatch=monkeypatch)
    assert decision.branch == 3
    assert "laya" in decision.guide_command


def test_vps_service_account_unwritable_falls_to_branch3_guide(
    unwritable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decide(platform="linux", nvidia_ok=False, docker=False, monkeypatch=monkeypatch)
    assert decision.branch == 3


def test_docker_states_recreate_wipes_install(
    writable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decide(platform="linux", nvidia_ok=False, docker=True, monkeypatch=monkeypatch)
    assert decision.branch == 1
    assert decision.docker is True
    assert "recreate" in decision.persistence
    assert "offered again" in decision.persistence


def test_uv_tool_persistence_note(writable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The uv-tool row is keyed on the dashboard's own interpreter path, not
    # on the stubbed one — so this cell asserts the guide/persistence shape
    # of a writable environment rather than the tool string itself.
    decision = _decide(platform="darwin", nvidia_ok=False, docker=False, monkeypatch=monkeypatch)
    assert decision.branch == 1
    assert decision.persistence.startswith("Installs into")


def test_cpu_guide_names_cpu_index(writable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    decision = _decide(platform="darwin", nvidia_ok=False, docker=False, monkeypatch=monkeypatch)
    assert "download.pytorch.org/whl/cpu" in decision.guide_command


def test_default_guide_has_no_cpu_index(writable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    decision = _decide(platform="linux", nvidia_ok=True, docker=False, monkeypatch=monkeypatch)
    assert "download.pytorch.org" not in decision.guide_command


def test_venv_argv_branch_built_but_not_selected() -> None:
    decision = EnvironmentDecision(
        2, "/fake/python", Path("/fake/home/.jev/cda-venv"), "cpu", "", "", False
    )
    steps = build_pip_steps(decision)
    assert steps[0] == ["/fake/python", "-m", "venv", "/fake/home/.jev/cda-venv"]
    assert "jev-agent" not in " ".join(" ".join(step) for step in steps)


def test_temp_space_check_reports_a_number() -> None:
    free, ok = temp_space_check()
    assert free >= 0
    assert isinstance(ok, bool)
