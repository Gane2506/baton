"""Fill a repo's `.jev/` with illustrative demo state for the dashboard screenshots.

Thin wrapper around `jev_agent.demo.seeder` (which `jev init --demo` also uses). It stays at
this path because `examples/demo/screenshots.py` runs it as a script, `capture.py` imports
`DemoChatBackend` from it, and `tests/integration/test_demo_seed.py` imports `seed`.
"""

from __future__ import annotations

import sys

from jev_agent.demo.seeder import DemoChatBackend, main, seed

__all__ = ["DemoChatBackend", "main", "seed"]


if __name__ == "__main__":
    main(sys.argv)
