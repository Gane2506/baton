"""Issue #223: close #194 fully — prompt_file containment, trusts() CWD, Laya weights.

PR #212 added the `roles.*.prompt_file` guard key but left the containment half
undone (a trusted repo could still point a prompt outside the repo), Laya
weights were never added to `GUARDED_KEYS`, and `trusts()` resolved relative
trust entries against the working directory instead of the repo.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any

import pytest

from jev_agent.app.settings import save as settings_save
from jev_agent.config import JevConfig
from jev_agent.config.loader import (
    ConfigError,
    changed_guarded_keys,
    load_config,
    trusts,
)
from jev_agent.config.schema import DEFAULT_LAYA_WEIGHTS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def trusted_user_cfg(tmp_path: Path) -> Path:
    user_cfg = tmp_path / "user.toml"
    _write(user_cfg, '[trust]\nrepos = ["*"]\n')
    return user_cfg


@pytest.fixture
def untrusted_user_cfg(tmp_path: Path) -> Path:
    user_cfg = tmp_path / "user.toml"
    _write(user_cfg, "")
    return user_cfg


# ---- prompt_file containment (issue #194 fix 2) -------------------------------------------


def test_trusted_repo_cannot_point_prompt_file_outside(
    tmp_path: Path, trusted_user_cfg: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "jev.toml", '[roles.coder]\nprompt_file = "/etc/passwd"\n')
    with pytest.raises(ConfigError, match=r"roles\.coder\.prompt_file.*outside the repository"):
        load_config(repo, trusted_user_cfg)


def test_repo_prompt_file_dotdot_escape_rejected(tmp_path: Path, trusted_user_cfg: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "jev.toml", '[roles.coder]\nprompt_file = "../outside.md"\n')
    with pytest.raises(ConfigError, match=r"outside the repository"):
        load_config(repo, trusted_user_cfg)


def test_repo_prompt_file_symlink_escape_rejected(tmp_path: Path, trusted_user_cfg: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("escape\n")
    os.symlink(outside, repo / "prompts" / "link.md")
    _write(repo / "jev.toml", '[roles.coder]\nprompt_file = "prompts/link.md"\n')
    with pytest.raises(ConfigError, match=r"outside the repository"):
        load_config(repo, trusted_user_cfg)


def test_repo_prompt_file_inside_allowed(tmp_path: Path, trusted_user_cfg: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    (repo / "prompts" / "coder.md").write_text("custom\n")
    _write(repo / "jev.toml", '[roles.coder]\nprompt_file = "prompts/coder.md"\n')
    assert load_config(repo, trusted_user_cfg).roles.coder.prompt_file == Path("prompts/coder.md")


def test_repo_prompt_file_absolute_inside_allowed(tmp_path: Path, trusted_user_cfg: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    (repo / "prompts" / "coder.md").write_text("custom\n")
    _write(
        repo / "jev.toml",
        f'[roles.coder]\nprompt_file = "{repo / "prompts" / "coder.md"}"\n',
    )
    assert load_config(repo, trusted_user_cfg).roles.coder.prompt_file == (
        repo / "prompts" / "coder.md"
    )


def test_user_config_absolute_prompt_file_still_allowed(
    tmp_path: Path, untrusted_user_cfg: Path
) -> None:
    # An absolute path in the *user's own* config is the user's explicit choice,
    # not a repo pointing outside itself — it keeps working.
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "shared.md"
    outside.write_text("shared\n")
    _write(
        untrusted_user_cfg,
        f'[roles.coder]\nprompt_file = "{outside}"\n',
    )
    assert load_config(repo, untrusted_user_cfg).roles.coder.prompt_file == outside


def test_dashboard_save_rejects_escaping_prompt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dashboard writes through settings.save(), not load_config(): the same
    # containment must hold there, or the save would write a jev.toml that fails
    # its next load.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "jev.toml").write_text("")

    def mutate(layer: dict[str, Any]) -> None:
        layer.setdefault("roles", {}).setdefault("coder", {})["prompt_file"] = "/etc/passwd"

    err = settings_save(root, "repo", mutate)
    assert err is not None and "outside the repository" in err
    assert (root / "jev.toml").read_text() == ""  # nothing was written


# ---- trusts() relative entries (issue #194) -------------------------------------------------


def test_trusts_relative_dot_trusts_repo_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = JevConfig.model_validate({"trust": {"repos": ["."]}})
    monkeypatch.chdir(tmp_path)  # not the repo: the old CWD resolution failed here
    assert trusts(config, repo) is True
    monkeypatch.chdir("/")
    assert trusts(config, repo) is True


def test_trusts_relative_entry_is_not_cwd_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relative entry used to resolve against the working directory: run from
    # the repo's parent with repos = ["<repo-name>"] and the old code trusted it.
    repo = tmp_path / "repo"
    repo.mkdir()
    config = JevConfig.model_validate({"trust": {"repos": ["repo"]}})
    monkeypatch.chdir(tmp_path)
    assert trusts(config, repo) is False


def test_trusts_absolute_and_star_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert trusts(JevConfig.model_validate({"trust": {"repos": [str(repo)]}}), repo) is True
    assert trusts(JevConfig.model_validate({"trust": {"repos": ["*"]}}), repo) is True
    assert (
        trusts(JevConfig.model_validate({"trust": {"repos": [str(tmp_path / "other")]}}), repo)
        is False
    )


# ---- Laya weights: nothing left to guard (issue #194, after #193/#217) ----------------------


def test_repo_laya_weights_table_is_ignored_not_guarded(
    tmp_path: Path, trusted_user_cfg: Path, untrusted_user_cfg: Path
) -> None:
    # `[dispatcher.laya]` no longer exists as a config section (issue #193): a
    # repo cannot pin weights any more — the table is stripped with a warning,
    # the effective weights stay the defaults, and no guarded key changes.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "jev.toml",
        "[dispatcher.laya]\nweights = {correctness = 0.99, safety = 0.002, simplicity = 0.002, "
        "maintainability = 0.002, tool_fit = 0.002, user_preference = 0.002}\n",
    )
    before = JevConfig()
    for user_cfg in (trusted_user_cfg, untrusted_user_cfg):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = load_config(repo, user_cfg)
        assert cfg.dispatcher.primary == "jev"
        assert not hasattr(cfg.dispatcher, "laya")
        assert any("consensus dispatcher was removed" in str(w.message) for w in caught)
        assert changed_guarded_keys(before, cfg) == []
    assert DEFAULT_LAYA_WEIGHTS == {
        "correctness": 0.30,
        "safety": 0.20,
        "simplicity": 0.15,
        "maintainability": 0.15,
        "tool_fit": 0.10,
        "user_preference": 0.10,
    }
