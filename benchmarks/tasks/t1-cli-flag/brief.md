# T1: add `--format` output to the demo CLI

The repo contains a tiny demo CLI (`demo_cli.py`) that greets somebody:

```
$ python demo_cli.py --name Ada
Hello, Ada!
```

Add a `--format` option to it:

- `--format text` (the default): print `Hello, <name>!` exactly as today.
- `--format json`: print exactly one line of JSON with two keys —
  `{"greeting": "Hello, <name>!", "name": "<name>"}` — the same greeting text,
  wrapped together with the name.
- Any other value must exit non-zero with a usage error (argparse's built-in
  invalid-choice error is fine).

Keep `--name` working exactly as before, and do not add any third-party
dependencies. The hidden grader checks the default, both formats, the exact
JSON keys, and the non-zero exit on a bad value.
