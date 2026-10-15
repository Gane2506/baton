"""Before/after capture for WS5 template sweep: connections, settings, queue,
dark + light. Usage: uv run --frozen --all-extras python examples/demo/ws5_shots.py <outdir>
Reuses screenshots.py's demo-repo + dashboard machinery.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture  # noqa: E402
import screenshots  # noqa: E402  (examples/demo/screenshots.py)

SHOTS = [
    ("connections-dark.png", "/connections", 1600, "dark"),
    ("connections-light.png", "/connections", 1600, "light"),
    ("settings-dark.png", "/settings", 1600, "dark"),
    ("settings-light.png", "/settings", 1600, "light"),
    ("queue-dark.png", "/queue", 1400, "dark"),
    ("queue-light.png", "/queue", 1400, "light"),
    ("board-dark.png", "/", 900, "dark"),
    ("chats-dark.png", "/chats", 1200, "dark"),
    ("learning-dark.png", "/learning", 1200, "dark"),
    ("kpis-dark.png", "/kpis", 1200, "dark"),
    ("artifacts-dark.png", "/artifacts", 1200, "dark"),
    ("run-dark.png", "/runs/{run}", 1800, "dark"),
]


def main(argv: list[str]) -> None:
    out = Path(argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    demo = screenshots.make_demo()
    try:
        env = capture.base_env(demo, capture.WIDTH)
        screenshots.seed(demo, env)
        run_id = capture.richest_run(demo)
        with screenshots.dashboard(demo, env) as base, screenshots.sync_playwright() as pw:
            browser = screenshots.launch(pw, None)
            try:
                for name, path, height, scheme in SHOTS:
                    url = base + path.format(run=run_id)
                    page = browser.new_page(
                        viewport={"width": 1440, "height": height},
                        color_scheme=scheme,
                        device_scale_factor=1,
                    )
                    page.goto(url, wait_until="networkidle")
                    page.evaluate("document.fonts.ready")
                    page.screenshot(path=out / name, full_page=False)
                    page.close()
                    print(f"{name}: {url}")
            finally:
                browser.close()
    finally:
        import shutil

        shutil.rmtree(demo, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv)
