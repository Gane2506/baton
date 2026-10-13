"""A stand-in for the GitHub CLI in tests: never touches the network.

The tests put a `gh` wrapper that runs this script first on PATH. It appends every call
(argv and working directory) to `$FAKE_GH_DIR/calls.jsonl` and answers from
`$FAKE_GH_DIR/responses.json`: a map from a command prefix ("auth status", "pr create",
"pr view", ...) to {"code": int, "stdout": str, "stderr": str}; the longest matching prefix
wins, unknown commands exit 0 with no output. For `pr create` it copies the --body-file to
`$FAKE_GH_DIR/body.md`, so tests can read the body after jev deleted its temporary file.
"""

import json
import os
import shutil
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    state = Path(os.environ["FAKE_GH_DIR"])
    with (state / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv[:2] == ["pr", "create"] and "--body-file" in argv:
        shutil.copy(argv[argv.index("--body-file") + 1], state / "body.md")
    responses_file = state / "responses.json"
    responses = json.loads(responses_file.read_text()) if responses_file.exists() else {}
    command = " ".join(argv)
    best = max((key for key in responses if command.startswith(key)), key=len, default=None)
    answer = responses.get(best, {}) if best is not None else {}
    sys.stdout.write(answer.get("stdout", ""))
    sys.stderr.write(answer.get("stderr", ""))
    return int(answer.get("code", 0))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
