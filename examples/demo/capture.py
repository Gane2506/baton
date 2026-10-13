"""Capture the terminal screenshots in `docs/images/cli-*.svg` from a seeded demo repo.

Each image is the real output of a `jev` command, run in the demo repo made by
`examples/demo/seed_demo.py` and exported as an SVG with Rich. No agent, model or network is
called: commands run with `JEV_FAKE_WORKERS=1`, the demo's rules dispatcher and its cached
model list, under a scratch HOME/XDG directory inside the demo repo, with proxy variables
pointing nowhere so an accidental request fails instead of leaving the machine.

Usage (from a clone of this repo, after seeding; see docs/dashboard.md "Screenshots"):

    uv run --all-extras python examples/demo/capture.py "$DEMO"            # docs/images/
    uv run --all-extras python examples/demo/capture.py "$DEMO" --out DIR

`jev doctor` gets placeholder values for the environment variables the demo config names
(so its tool rows show them as set); the values are never printed and no other command sees
them. `jev chat` is driven in a child process: the start screen and the typed lines come from
a script, and the model is `seed_demo.DemoChatBackend`, a canned stand-in. Its `/plan` starts
a real plan-only run with fake workers, so run the chat capture last (this script does).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parents[1] / "docs" / "images"
WIDTH = 110
MAX_SVG_BYTES = 150_000
JEV = "from jev_agent.cli.app import main; main()"

# Names from the demo jev.toml; doctor sees them set. Placeholders, never real keys.
DOCTOR_ENV = {
    "OPENROUTER_API_KEY": "demo-placeholder-not-a-key",
    "GITHUB_PERSONAL_ACCESS_TOKEN": "demo-placeholder-not-a-token",
    "DOCS_MCP_TOKEN": "demo-placeholder-not-a-token",
}
FORBIDDEN = re.compile(r"/Users/|/home/|demo-placeholder|sk-or-|ghp_")

CHAT_INPUT = [
    "1",  # start screen: the active learning profile
    "Let callers ask word_frequencies() for just the most common words",
    "An optional top_n argument; keep alphabetical ties; top_n <= 0 raises ValueError",
    "/brief",
    "/plan",
    "/quit",
]


# ---- environment -----------------------------------------------------------------------------


def stub_bin(demo: Path) -> Path:
    """Stand-ins for `claude`, `codex` and `npx`, so doctor finds them on PATH. They are never
    run (workers are fake); if one were, it would only fail."""
    bin_dir = demo / ".xdg" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "codex", "npx"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\necho 'demo stub: not a real CLI' >&2\nexit 1\n")
        stub.chmod(0o755)
    return bin_dir


def base_env(demo: Path, width: int) -> dict[str, str]:
    """A small, scrubbed environment: none of the caller's keys, home or config."""
    xdg = demo / ".xdg"
    home = xdg / "home"
    home.mkdir(parents=True, exist_ok=True)
    nowhere = "http://127.0.0.1:9"  # nothing listens there: any request fails at once
    return {
        "PATH": f"{stub_bin(demo)}:/usr/bin:/bin",
        "HOME": str(home),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "LANG": "en_US.UTF-8",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "FORCE_COLOR": "1",
        "COLUMNS": str(width),
        "LINES": "60",
        "JEV_FAKE_WORKERS": "1",
        "HTTP_PROXY": nowhere,
        "HTTPS_PROXY": nowhere,
        "ALL_PROXY": nowhere,
        "NO_PROXY": "",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


# ---- running and exporting -------------------------------------------------------------------


@dataclass
class Shot:
    file: str
    title: str
    commands: list[list[str]]  # each: the jev arguments
    width: int = WIDTH
    extra_env: dict[str, str] | None = None


def run_jev(demo: Path, args: Sequence[str], env: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", JEV, *args],
        cwd=demo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode not in (0, 1):  # doctor exits 1 when a check fails
        raise SystemExit(f"jev {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def recorder(width: int) -> Console:
    return Console(
        record=True,
        width=width,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    )


def prompt_line(args: Sequence[str]) -> Text:
    line = Text("$ ", style="bold green")
    line.append("jev " + " ".join(args), style="bold")
    return line


def save_svg(console: Console, out: Path, title: str) -> int:
    svg = console.export_svg(title=title)
    if FORBIDDEN.search(svg):
        raise SystemExit(f"{out.name}: output contains a home path or a placeholder value")
    out.write_text(svg, encoding="utf-8")
    size = out.stat().st_size
    if size > MAX_SVG_BYTES:
        print(f"warning: {out.name} is {size // 1000} KB (limit {MAX_SVG_BYTES // 1000} KB)")
    return size


def capture(demo: Path, out_dir: Path, shot: Shot) -> None:
    env = base_env(demo, shot.width) | (shot.extra_env or {})
    console = recorder(shot.width)
    for n, args in enumerate(shot.commands):
        if n:
            console.print()
        console.print(prompt_line(args))
        console.print(Text.from_ansi(run_jev(demo, args, env).rstrip("\n")), crop=False)
    size = save_svg(console, out_dir / shot.file, shot.title)
    print(f"{shot.file}: {size // 1000} KB")


# ---- picking runs ----------------------------------------------------------------------------


def richest_run(demo: Path) -> str:
    """The run with a quality review and the most decisions and attempts."""
    from jev_agent.config import STATE_DIRNAME
    from jev_agent.core.store import Store

    with Store.open(demo / STATE_DIRNAME / "state.db") as store:
        runs = store.list_runs()
        if not runs:
            raise SystemExit(f"{demo} has no runs: seed it with examples/demo/seed_demo.py")

        def score(run_id: str) -> tuple[bool, bool, int]:
            decisions = store.list_decisions(run_id)
            fallback = any(d.fallback_reason for d in decisions)
            reviewed = bool(store.list_quality_reports(run_id))
            return reviewed, fallback, len(decisions) + len(store.list_attempts(run_id))

        return max((r.id for r in runs), key=score)


# ---- the chat, in a child process ------------------------------------------------------------


class ScriptedConsole(Console):
    """A recording console that answers `input()` from a script and shows each answer after
    its prompt, as a terminal would. Spinners are left out of the recording."""

    def __init__(self, lines: Sequence[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.lines = list(lines)

    def input(self, prompt: Any = "", **kwargs: Any) -> str:
        if not self.lines:
            raise EOFError
        line = self.lines.pop(0)
        self.print(prompt, end="")
        self.print(Text(line))
        return line

    @contextlib.contextmanager
    def status(self, *args: Any, **kwargs: Any) -> Iterator[None]:  # type: ignore[override]
        yield


def chat_child(demo: Path, out: Path) -> None:
    """Runs inside the child process, whose environment `capture_chat` set."""
    sys.path.insert(0, str(HERE))
    from seed_demo import DemoChatBackend  # the demo dir is not a package

    from jev_agent.cli import chat_cmds, run_cmds, wiring

    console = ScriptedConsole(
        CHAT_INPUT,
        record=True,
        width=WIDTH,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    )
    console.print(prompt_line(["chat"]))
    for module in (chat_cmds, run_cmds, wiring):
        module.console = console  # type: ignore[attr-defined]
    chat_cmds.is_interactive = lambda: True
    chat_cmds.make_backend = lambda ctx, backend, model: DemoChatBackend(backend, model)
    chat_cmds.chat(
        learning=None,
        no_learning=False,
        profile=None,
        worker=None,
        model=None,
        resume=None,
        list_=False,
        budget_usd=None,
        repo=demo,
    )
    save_svg(console, out, "jev chat")


def capture_chat(demo: Path, out_dir: Path) -> None:
    # The api worker looks usable (placeholder key), so the chat picks it like a real setup
    # would; the backend is the canned DemoChatBackend, so the key is never sent anywhere.
    env = base_env(demo, WIDTH) | {"OPENROUTER_API_KEY": DOCTOR_ENV["OPENROUTER_API_KEY"]}
    env["PYTHONPATH"] = str(HERE)
    out = out_dir / "cli-chat.svg"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--chat-child", str(demo), str(out)],
        cwd=demo,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"chat capture failed:\n{result.stdout}\n{result.stderr}")
    print(f"{out.name}: {out.stat().st_size // 1000} KB")


# ---- main ------------------------------------------------------------------------------------


def shots(run_id: str) -> list[Shot]:
    return [
        Shot("cli-status-run.svg", f"jev status {run_id}", [["status", run_id]]),
        Shot("cli-log.svg", f"jev log {run_id}", [["log", run_id]], width=176),
        Shot("cli-queue.svg", "jev queue list", [["queue", "list"], ["scheduler", "status"]]),
        Shot("cli-board.svg", "jev board", [["board"]], width=150),
        Shot("cli-models-free.svg", "jev models list --free", [["models", "list", "--free"]]),
        Shot("cli-doctor-tools.svg", "jev doctor", [["doctor"]], extra_env=DOCTOR_ENV),
        Shot("cli-learning.svg", "jev learning show", [["learning", "show"]]),
        Shot("cli-quality-checks.svg", "jev quality checks", [["quality", "checks"]]),
        Shot("cli-quality.svg", f"jev quality show {run_id}", [["quality", "show", run_id]]),
    ]


def main(argv: Sequence[str]) -> None:
    if len(argv) == 3 and argv[0] == "--chat-child":
        chat_child(Path(argv[1]), Path(argv[2]))
        return
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("demo", type=Path, help="a repo seeded by examples/demo/seed_demo.py")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--only", nargs="*", default=None, help="file names to capture")
    args = parser.parse_args(argv)
    demo: Path = args.demo.resolve()
    if not (demo / "jev.toml").is_file():
        raise SystemExit(f"{demo} has no jev.toml: run jev init and seed_demo.py first")
    args.out.mkdir(parents=True, exist_ok=True)
    run_id = richest_run(demo)
    for shot in shots(run_id):
        if args.only is None or shot.file in args.only:
            capture(demo, args.out, shot)
    if args.only is None or "cli-chat.svg" in args.only:
        capture_chat(demo, args.out)  # last: its /plan adds a run to the demo


if __name__ == "__main__":
    main(sys.argv[1:])
