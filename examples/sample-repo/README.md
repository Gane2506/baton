# textstats: a sample repo for jev

A tiny Python project for trying [jev](../../README.md) end to end. It has one module,
`textstats/core.py` (`words`, `word_count`, `average_word_length`), and a passing pytest suite in
`tests/`.

## Set it up

jev works on a git repository, and this folder is not one. Copy it out first, so jev's branches
and worktrees never touch the jev source tree:

```bash
cp -R examples/sample-repo ~/textstats
cd ~/textstats
uv run pytest -q          # 5 passed; also creates uv.lock
git init
git add .
git commit -m "Initial commit"
```

You can also run the tests without uv: `python -m pip install pytest && python -m pytest -q`.

## Run jev on it

```bash
jev init                  # writes jev.toml, creates .jev/, ignores .jev/ in git
jev doctor                # checks git, config, worker CLIs and API keys
jev run --plan-only "Add a word_frequencies(text) function with tests"
jev status                # find the run id
jev status <run_id>       # read the plan
jev approve <run_id>      # execute it
jev log <run_id>          # every decision and worker attempt
```

The result is a local branch, `jev/<run_id>/result`. Nothing is pushed. Inspect it with:

```bash
git log --oneline jev/<run_id>/result
git diff HEAD jev/<run_id>/result
```

Tip: to have the full suite run on the merged result, uncomment `test_command` under
`[integration]` in `jev.toml` and set it to `"uv run pytest -q"`.

## Ideas to try

1. "Add a `word_frequencies(text)` function that returns a dict of lowercased word to count,
   with tests." One small task; a good first run.
2. "Add `sentence_count(text)` and `reading_time_minutes(text, wpm=200)`, each with tests." Two
   independent tasks.
3. "Add a command-line entry point `python -m textstats FILE` that prints word count, average
   word length and the five most common words, with tests." Several tasks with dependencies.

## Try it without any agent or key

`JEV_FAKE_WORKERS=1` swaps every worker for a scripted fake, and the rules dispatcher needs no
key. This checks your install and shows the flow, but writes only a placeholder file:

```bash
# in jev.toml: [dispatcher] primary = "rules"
JEV_FAKE_WORKERS=1 jev run "anything"
JEV_FAKE_WORKERS=1 jev approve <run_id>
```

The fake run ends in DONE with a branch `jev/<run_id>/result` holding `jev_fake_T1.txt`.
