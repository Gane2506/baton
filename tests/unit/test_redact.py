from pathlib import Path

import pytest

from jev_agent.runlog import REDACTED, append_log, log_path, redact, write_log


@pytest.mark.parametrize(
    "secret",
    [
        "sk-or-v1-0123456789abcdef0123",
        "sk-proj-ABCDEFGHijklmnop1234",
        "sk-ant-api03-abcdefghijk_lmn-op",
    ],
)
def test_key_patterns_are_redacted(secret: str) -> None:
    out = redact(f"key={secret} done", env={})
    assert secret not in out
    assert out == f"key={REDACTED} done"


def test_bearer_token_keeps_the_scheme() -> None:
    out = redact("Authorization: Bearer abc.DEF-123_456~xyz", env={})
    assert out == f"Authorization: Bearer {REDACTED}"


def test_secret_env_values_are_redacted() -> None:
    env = {
        "OPENROUTER_API_KEY": "plainvalue12345",
        "GITHUB_TOKEN": "ghp_tokenvalue999",
        "MY_TOKEN_FILE": "short",  # too short to redact
        "HOME": "/Users/someone",  # not a secret name
    }
    out = redact("a plainvalue12345 b ghp_tokenvalue999 c short d /Users/someone", env=env)
    assert out == f"a {REDACTED} b {REDACTED} c short d /Users/someone"


def test_uses_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_API_KEY", "fromtheenvironment")
    assert redact("x fromtheenvironment") == f"x {REDACTED}"


def test_ordinary_text_is_untouched() -> None:
    text = "risk-free task-list; skip sk-1 (short)"
    assert redact(text, env={}) == text


def test_log_writes_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-supersecretvalue")
    path = log_path(tmp_path, "r1", None, 1)
    assert path == tmp_path / "runs" / "r1" / "run" / "1.log"
    write_log(path, "key sk-or-v1-supersecretvalue\n")
    append_log(path, "Bearer abcdefghijklmnop\n")
    text = path.read_text()
    assert "supersecret" not in text and "abcdefghijklmnop" not in text
    assert log_path(tmp_path, "r1", "T2", "3.test").name == "3.test.log"
