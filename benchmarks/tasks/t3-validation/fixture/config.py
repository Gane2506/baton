"""Minimal config parser for capability benchmark task T3.

Self-contained: stdlib only, no Baton internals. `parse_config` is brittle
today: it lets KeyError/TypeError escape on malformed input.
"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {"host": "localhost", "port": 8080, "debug": False}


def parse_config(data: dict[str, Any]) -> dict[str, Any]:
    """Build a config dict from raw data. Brittle: raises on malformed input."""
    return {
        "host": data["host"],
        "port": int(data["port"]),
        "debug": bool(data.get("debug", False)),
    }
