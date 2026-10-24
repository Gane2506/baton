"""Always denied: blocked at every access level, before any other check."""

from __future__ import annotations

from collections.abc import Sequence

from .paths import resolve, within
from .shape import _flag_set, _name, _operands
from .types import PolicyContext

_SHUTDOWN = frozenset({"shutdown", "reboot", "halt", "poweroff"})


_USER_ADMIN = frozenset(
    {"useradd", "userdel", "usermod", "adduser", "deluser", "addgroup", "delgroup"}
    | {"groupadd", "groupdel", "groupmod", "passwd", "chpasswd", "chage", "gpasswd"}
    | {"newusers", "visudo", "vipw", "vigr", "chsh", "chfn"}
)


_FIREWALL = frozenset(
    {"iptables", "ip6tables", "iptables-restore", "ip6tables-restore", "iptables-legacy"}
    | {"ufw", "nft", "firewall-cmd", "ebtables", "arptables"}
)


_DISK = frozenset({"mke2fs", "wipefs", "fdisk", "sfdisk", "parted", "mkswap", "shred"})


_SYSTEMCTL_POWER = frozenset(
    {"poweroff", "reboot", "halt", "kexec", "emergency", "rescue", "suspend", "hibernate"}
    | {"soft-reboot", "default", "isolate"}
)


def always_denied(argv: Sequence[str], ctx: PolicyContext) -> str | None:
    """Why `argv` is blocked at every access level, or None."""
    cmd, args = _name(argv[0]), list(argv[1:])
    flags = _flag_set(args)
    if any("authorized_keys" in a for a in argv):
        return "editing ~/.ssh/authorized_keys"
    if any("/etc/sudoers" in a for a in argv):
        return "changing sudoers"
    if cmd.startswith("mkfs") or cmd in _DISK:
        return "formatting or wiping disks"
    if cmd == "dd" and any(a.startswith("of=/dev/") for a in args):
        return "dd onto a device"
    if cmd in _SHUTDOWN or (cmd in {"init", "telinit"} and any(a in {"0", "6"} for a in args)):
        return "shutting down or rebooting the server"
    if cmd == "systemctl" and _SYSTEMCTL_POWER & set(args):
        return "shutting down or rebooting the server"
    if cmd in _USER_ADMIN:
        return "changing users, groups, passwords or sudoers"
    if cmd in _FIREWALL:
        return "changing the firewall"
    if cmd in {"chmod", "chown", "chgrp"} and flags & {"-R", "--recursive"}:
        targets = [resolve(a, ctx.workdir) for a in _operands(args)]
        if "/" in targets or any(a in {"/", "/*"} for a in args):
            return "recursive chmod/chown on /"
    if cmd == "rm":
        if "--no-preserve-root" in args or any(
            resolve(a, ctx.workdir) == "/" or a in {"/*", "/."} for a in _operands(args)
        ):
            return "rm on /"
        if flags & {"-r", "-R", "-f", "--recursive", "--force"}:
            for target in _operands(args):
                full = resolve(target, ctx.workdir)
                if full is None or not within(full, ctx.write_roots, strictly=True):
                    return "rm -rf outside allowed_paths"
    if cmd == "crontab" and "-r" in flags:
        return "crontab -r"
    if cmd in {"kill", "pkill", "killall"} and "1" in _operands(args):
        return "killing init (pid 1)"
    if cmd == "git" and "push" in args:
        forced = {"--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror"}
        if forced & flags or any(a.startswith("+") for a in _operands(args)):
            return "git push --force"
    return None
