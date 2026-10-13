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


# ---- credential shapes beyond `sk-` keys (issue #27) ---------------------------------------

GITHUB_PAT = "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"
# Built at runtime so secret scanners don't flag the source as a leaked Google key.
GOOGLE_KEY = "AI" + "zaSyA-abcdefghijklmnopqrstuvwxyz01234"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (
            "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 used",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
        ),
        ("gh auth: ghs_abcdefghijklmnopqrstuvwxyz012345", "ghs_abcdefghijklmnop"),
        (f"pat {GITHUB_PAT}", GITHUB_PAT),
        ("slack xoxb-1234567890-abcdefghijkl", "xoxb-1234567890"),
        ("aws AKIAIOSFODNN7EXAMPLE key", "AKIAIOSFODNN7EXAMPLE"),
        (f"google {GOOGLE_KEY}", GOOGLE_KEY),
        (f"jwt {JWT}", JWT),
        ("remote https://x-access-token:ghs_secretvalue@github.com/o/r.git", "ghs_secretvalue"),
        ("remote https://deploy:hunter2hunter2@git.example.com/r.git", "hunter2hunter2"),
        ("Authorization: token ghp_short123", "ghp_short123"),
        ("authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    ],
)
def test_common_credential_formats_are_redacted(text: str, secret: str) -> None:
    out = redact(text, env={})
    assert secret not in out and REDACTED in out


def test_url_credentials_keep_the_host() -> None:
    out = redact("https://user:longpassword@example.com/repo.git", env={})
    assert out == f"https://{REDACTED}@example.com/repo.git"


def test_private_key_block_is_redacted_whole_even_when_cut_off() -> None:
    body = "MIIEowIBAAKCAQEAabcdefghijklmnopqrstuvwxyz0123456789"
    block = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body}\n-----END OPENSSH PRIVATE KEY-----"
    out = redact(f"before\n{block}\nafter", env={})
    assert body not in out and out == f"before\n{REDACTED}\nafter"
    cut = redact(f"-----BEGIN RSA PRIVATE KEY-----\n{body}", env={})
    assert body not in cut


def test_ordinary_prose_is_left_alone() -> None:
    text = "the token generation step and Basic authentication are documented"
    assert redact(text, env={}) == text


def test_summary_is_redacted_before_it_is_cut(tmp_path: Path) -> None:
    """A secret straddling the 500-character cut must not leave its prefix (issue #27)."""
    from jev_agent.workers.base import WorkerJob
    from jev_agent.workers.summary import SUMMARY_CHARS, make_output

    token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    text = "x" * (SUMMARY_CHARS - 10) + " " + token
    job = WorkerJob(role="coder", prompt="p", workdir=tmp_path, log_path=tmp_path / "w.log")
    summary = make_output(job, "success", text, env={}).result.summary
    assert "ghp_" not in summary and len(summary) <= SUMMARY_CHARS
