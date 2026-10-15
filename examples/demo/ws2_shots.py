"""Before/after capture for the Tools page redesign (Workstream 2): /tools, dark +
light, plus the open "Add to …" disclosure. Usage:
uv run --frozen --all-extras python examples/demo/ws2_shots.py <outdir>
Reuses screenshots.py's demo-repo + dashboard machinery.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Literal

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture  # noqa: E402
import screenshots  # noqa: E402  (examples/demo/screenshots.py)

Scheme = Literal["dark", "light"]

SHOTS: list[tuple[str, str, int, Scheme]] = [
    ("tools-dark.png", "/tools", 1400, "dark"),
    ("tools-light.png", "/tools", 1400, "light"),
]


def main(argv: list[str]) -> None:
    out = Path(argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    demo = screenshots.make_demo()
    try:
        env = capture.base_env(demo, capture.WIDTH)
        screenshots.seed(demo, env)
        with screenshots.dashboard(demo, env) as base, sync_playwright() as pw:
            browser = screenshots.launch(pw, None)
            try:
                for name, path, height, scheme in SHOTS:
                    page = browser.new_page(
                        viewport={"width": 1440, "height": height},
                        color_scheme=scheme,
                        device_scale_factor=1,
                    )
                    page.goto(base + path, wait_until="networkidle")
                    page.evaluate("document.fonts.ready")
                    page.screenshot(path=out / name)
                    page.close()
                    print(f"{name}: {path}")
                # expanded "Add to …" disclosure + form (dark)
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1400},
                    color_scheme="dark",
                    device_scale_factor=1,
                )
                page.goto(base + "/tools", wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                page.locator("details.disclosure > summary").first.click()
                page.wait_for_timeout(300)
                page.screenshot(path=out / "tools-add-open.png")
                page.close()
                print("tools-add-open.png: /tools (disclosure open)")
            finally:
                browser.close()
    finally:
        shutil.rmtree(demo, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv)
