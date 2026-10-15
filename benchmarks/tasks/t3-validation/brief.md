# T3: harden `parse_config` against malformed input

The repo contains a minimal config parser (`config.py`). `parse_config(data)`
builds a config dict from raw data — but today it lets `KeyError`,
`TypeError` and friends escape on malformed input.

Harden it:

- It must **never** let `KeyError`, `TypeError`, `AttributeError` (or similar)
  escape. On malformed input, either return a config built from `DEFAULTS`
  for the missing/bad parts, or raise `ValueError` with a clear message —
  pick one and be consistent.
- Valid configs (all three keys present with the right types) must keep
  working **exactly** as before, including a numeric string for `port`
  (`"8080"` → `8080`).
- Extra unknown keys must be ignored, not crash.
- Do not add third-party dependencies (stdlib only).

The hidden grader feeds it `None`, missing keys, wrong types, nested garbage
and empty dicts, checks the valid cases are unchanged, and checks that no
changed file imports anything outside the stdlib.
