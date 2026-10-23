"""Regenerate every screenshot in `docs/images/` from the running app.

One command builds a scratch demo repo from `examples/sample-repo`, runs `jev init`, seeds it
with `seed_demo.py`, starts the real `jev dashboard` on a free loopback port and captures each
`dashboard-*.png` with headless Chrome, then captures the `cli-*.svg` terminal images with
`capture.py`. Nothing is guessed or drawn by hand: what the images show is what the dashboard
and the CLI on this checkout serve.

    uv run --all-extras python examples/demo/screenshots.py
    uv run --all-extras python examples/demo/screenshots.py --out preview/

Everything runs under a scratch HOME and XDG directories inside the demo repo with fake
workers and proxy variables pointing nowhere (`capture.base_env`), so your own config, keys and
browser profile are never read. Chrome gets a fresh profile and is closed at the end; the demo
repo is deleted unless `--keep` is given. Chrome is Playwright's own Chromium when installed,
otherwise the system Chrome, or the binary given with `--chrome`.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import capture  # examples/demo/capture.py: scrubbed environment and the cli-*.svg images
from playwright.sync_api import Browser, Error, FloatRect, Page, Playwright, sync_playwright

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SAMPLE = REPO / "examples" / "sample-repo"
WIDTH = 1440
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GIT = ["git", "-c", "user.name=demo", "-c", "user.email=demo@example.com"]


@dataclass(frozen=True)
class Shot:
    file: str
    path: str  # "{run}" is replaced with the richest run's id
    height: int = 900
    dark: bool = False


SHOTS = [
    Shot("dashboard-board.png", "/"),
    Shot("dashboard-board-dark.png", "/", dark=True),
    Shot("dashboard-run.png", "/runs/{run}", height=1400),
    Shot("dashboard-queue.png", "/queue"),
    Shot("dashboard-learning.png", "/learning"),
    Shot("dashboard-spend.png", "/spend", height=1580),
    Shot("dashboard-kpis.png", "/kpis", height=1700),
    Shot("dashboard-settings.png", "/settings", height=1400),
    Shot("dashboard-fleet.png", "/fleet"),
    Shot("dashboard-agents.png", "/agents", height=1400),
    Shot("dashboard-chats.png", "/chats"),
    Shot("dashboard-tools.png", "/tools", height=1400),
    Shot("dashboard-connections.png", "/connections", height=1600),
    Shot("dashboard-design-systems.png", "/design-systems"),
    Shot("dashboard-design-system.png", "/design-systems/acme-brand", height=1400),
    Shot("design-studio.png", "/design-studio"),
]
QUALITY = "dashboard-quality.png"  # the run page's "Quality review" section only
NO_BACKEND_CHAT = "dashboard-chats-no-backend.png"  # the New chat form's guiding empty state


# ---- the demo repo ---------------------------------------------------------------------------


def make_demo() -> Path:
    """A fresh repo under /tmp (shown in the dashboard header, so not a home path)."""
    parent = "/tmp" if Path("/tmp").is_dir() else None
    demo = Path(tempfile.mkdtemp(prefix="baton-demo.", dir=parent))
    shutil.copytree(SAMPLE, demo, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=demo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=demo, check=True)
    subprocess.run([*GIT, "commit", "-qm", "sample"], cwd=demo, check=True)
    return demo


def run(args: list[str], demo: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        args, cwd=demo, env=env, capture_output=True, text=True, timeout=300, check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def seed(demo: Path, env: dict[str, str]) -> None:
    run([sys.executable, "-c", capture.JEV, "init"], demo, env)
    print(run([sys.executable, str(HERE / "seed_demo.py"), str(demo)], demo, env).strip())
    seed_library(demo, env)


def seed_library(demo: Path, env: dict[str, str]) -> None:
    """One design system in the scratch library (`$HOME/.jev/design-systems/`), so the
    design-systems page and its detail view have something to show."""
    snippet = (
        "from jev_agent.core.design_library import save_library_system\n"
        "save_library_system(\n"
        "    'acme-brand',\n"
        "    '# Acme Brand\\n\\nA warm, confident marketing system.\\n\\n"
        "## Components\\n\\nButtons, cards and the marketing nav.\\n',\n"
        "    {'colors': {'primary': '#e8590c', 'ink': '#1a1b1e'},\n"
        "     'type_scale': {'base': 16, 'lg': 20},\n"
        "     'spacing': {'sm': 8, 'md': 16},\n"
        "     'radii': {'md': 8}},\n"
        "    {'name': 'acme-brand', 'source': 'wizard',\n"
        "     'app_type': 'marketing', 'stack': 'Next.js'},\n"
        ")\n"
    )
    run([sys.executable, "-c", snippet], demo, env)


# ---- the dashboard ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def dashboard(demo: Path, env: dict[str, str]) -> Iterator[str]:
    """Serve `jev dashboard` for the demo repo until the block ends; yields its base URL."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    args = [sys.executable, "-c", capture.JEV, "dashboard", "--no-open", "--port", str(port)]
    proc = subprocess.Popen(
        args, cwd=demo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                raise SystemExit(f"jev dashboard exited with {proc.returncode}:\n{output}")
            try:
                with NO_PROXY.open(base + "/", timeout=2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise SystemExit(
                        f"jev dashboard did not answer on {base} within 30 s"
                    ) from None
                time.sleep(0.2)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---- the browser -----------------------------------------------------------------------------


def launch(pw: Playwright, chrome: Path | None) -> Browser:
    """Headless, with a throwaway profile and no proxy: the dashboard is on loopback."""
    args = ["--no-proxy-server"]
    if chrome is not None:
        return pw.chromium.launch(executable_path=chrome, args=args)
    try:
        return pw.chromium.launch(args=args)
    except Error:
        return pw.chromium.launch(channel="chrome", args=args)


def open_page(browser: Browser, url: str, height: int, dark: bool) -> Page:
    context = browser.new_context(
        viewport={"width": WIDTH, "height": height},
        color_scheme="dark" if dark else "light",
        device_scale_factor=1,
    )
    page = context.new_page()
    page.goto(url, wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    return page


def shoot_quality(browser: Browser, url: str, out: Path) -> None:
    """The heading and card of the run page's quality review, with the page's own margin."""
    page = open_page(browser, url, 900, dark=False)
    heading = page.locator("#quality-review").bounding_box()
    card = page.locator("#quality-review + section").bounding_box()
    if heading is None or card is None:
        raise SystemExit(f"{url} has no quality review to capture")
    top = heading["y"] - 16
    bottom = card["y"] + card["height"] + 16
    clip = FloatRect(x=0, y=top, width=WIDTH, height=bottom - top)
    page.screenshot(path=out, clip=clip, full_page=True)
    page.context.close()


def shoot_studio_wizard(browser: Browser, base: str, out: Path) -> None:
    """Drive the studio wizard to its Stack step, then capture that step.

    The wizard keeps its answers server-side per session, so the shots script
    walks the real flow like a visitor would: Purpose -> App type -> Stack.
    """
    page = open_page(browser, base + "/design-studio/new", 900, dark=False)
    page.fill("#ws-product", "Acme Analytics")
    page.click('form[action="/design-studio/new/purpose"] button[type="submit"]')
    page.wait_for_selector('input[name="app_type"]')
    page.check('input[name="app_type"][value="dashboard"]')
    page.click('form[action="/design-studio/new/app_type"] button[type="submit"]')
    page.wait_for_selector('input[name="stack"]')
    page.screenshot(path=out)
    page.context.close()


def shoot_no_backend_chat(base: str, out_dir: Path, chrome: Path | None) -> None:
    """The /chats New chat form with no backend ready: the guiding empty state.

    Served with the `claude` stub off PATH and no API key in the environment, so
    `backend_readiness` reports nothing usable — the page the scrubbed demo
    environment would otherwise show (Claude Code "on PATH" via the stub).
    """
    with sync_playwright() as pw:
        browser = launch(pw, chrome)
        try:
            page = open_page(browser, base + "/chats", 900, dark=False)
            page.screenshot(path=out_dir / NO_BACKEND_CHAT)
            page.context.close()
            print(f"{NO_BACKEND_CHAT}: /chats (no chat backend ready)")
        finally:
            browser.close()


def shoot_dashboard(base: str, run_id: str, out_dir: Path, chrome: Path | None) -> None:
    with sync_playwright() as pw:
        browser = launch(pw, chrome)
        try:
            for shot in SHOTS:
                page = open_page(
                    browser, base + shot.path.format(run=run_id), shot.height, shot.dark
                )
                page.screenshot(path=out_dir / shot.file)
                page.context.close()
                print(f"{shot.file}: {shot.path.format(run=run_id)}")
            shoot_studio_wizard(browser, base, out_dir / "design-studio-wizard.png")
            print("design-studio-wizard.png: /design-studio/new (walked to the Stack step)")
            shoot_quality(browser, f"{base}/runs/{run_id}", out_dir / QUALITY)
            print(f"{QUALITY}: /runs/{run_id} (quality review)")
        finally:
            browser.close()


# ---- main ------------------------------------------------------------------------------------


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=capture.DEFAULT_OUT, help="output directory")
    parser.add_argument("--chrome", type=Path, default=None, help="Chrome or Chromium binary")
    parser.add_argument("--no-cli", action="store_true", help="skip the cli-*.svg images")
    parser.add_argument("--keep", action="store_true", help="keep the demo repo afterwards")
    args = parser.parse_args(argv)
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    demo = make_demo()
    try:
        env = capture.base_env(demo, capture.WIDTH)
        seed(demo, env)
        run_id = capture.richest_run(demo)
        with dashboard(demo, env) as base:
            shoot_dashboard(base, run_id, out, args.chrome)
        # The New chat form with no backend ready: same seeded demo repo, but the
        # `claude` stub off PATH and no API key, so nothing is usable.
        env_no_backends = dict(env)
        env_no_backends["PATH"] = "/usr/bin:/bin"
        with dashboard(demo, env_no_backends) as base:
            shoot_no_backend_chat(base, out, args.chrome)
        if not args.no_cli:
            capture.main([str(demo), "--out", str(out)])  # last: the chat adds a run
    finally:
        if args.keep:
            print(f"Demo repo kept at {demo}")
        else:
            shutil.rmtree(demo, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
