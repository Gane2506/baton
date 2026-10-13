"""A stand-in for the GitHub CLI in tests: never touches the network.

The tests put a `gh` wrapper that runs this script first on PATH. It appends every call
(argv and working directory) to `$FAKE_GH_DIR/calls.jsonl` and answers from
`$FAKE_GH_DIR/responses.json`: a map from a command prefix ("auth status", "pr create",
"pr view", ...) to {"code": int, "stdout": str, "stderr": str}; the longest matching prefix
wins, unknown commands exit 0 with no output. An answer may also be a list: the Nth call
matching that prefix gets the Nth answer (the last one repeats), counted in
`$FAKE_GH_DIR/counts.json`; and an answer's optional "sleep" (seconds) delays it, to test
timeouts. For `pr create` it copies the --body-file to `$FAKE_GH_DIR/body.md`, so tests can
read the body after jev deleted its temporary file.
"""

import json
import os
import shutil
import sys
import time
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
    if isinstance(answer, list):
        counts_file = state / "counts.json"
        counts = json.loads(counts_file.read_text()) if counts_file.exists() else {}
        seen = counts.get(best, 0)
        counts[best] = seen + 1
        counts_file.write_text(json.dumps(counts))
        answer = answer[min(seen, len(answer) - 1)] if answer else {}
    time.sleep(float(answer.get("sleep", 0)))
    sys.stdout.write(answer.get("stdout", ""))
    sys.stderr.write(answer.get("stderr", ""))
    return int(answer.get("code", 0))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
