"""Demo CLI for capability benchmark task T1.

Self-contained: stdlib only, no Baton internals.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo-cli", description="Greet somebody.")
    parser.add_argument("--name", default="world", help="Who to greet.")
    args = parser.parse_args(argv)
    print(f"Hello, {args.name}!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
