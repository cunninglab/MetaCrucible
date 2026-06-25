# Contributing to MetaCrucible

MetaCrucible is a verifier-gated optimization system for evolving agent
skills, subagents, and reusable AI capabilities. The project keeps core
optimization logic and adapter behavior grounded through three test
layers — pure unit tests, deterministic recorded-replay tests in CI,
and opt-in local-real tests on developer machines.

This guide covers how to set up a working environment, which commands
to run before opening a pull request, and where each kind of test
belongs. Project rationale lives in `docs/adr/`; see the linked ADRs
for "why" decisions.

## Python environment

Python and the project virtualenv are managed by
[Mise](https://mise.jdx.dev/) from a single `mise.toml` at the repo
root. No competing environment manager (pyenv, poetry, pipenv, conda,
asdf, ...) is added without a rationale comment inside `mise.toml`.

```bash
# Provision Python 3.14 and the project virtualenv (.venv/).
mise install

# Editable install of MetaCrucible + dev dependencies into .venv.
mise run install
```

After `mise install`, the active shell's `python` and `pytest` resolve
to the project virtualenv, so subsequent commands do not need manual
activation.

## Developer commands

All common workflows run through `mise run <task>` so the same command
string works locally and in CI:

| Command | What it does |
| --- | --- |
| `mise run install` | Editable install via `uv pip install -e ".[dev]"` |
| `mise run test` | Full pytest suite (`pytest`) |
| `mise run test-replay` | Replay-focused subset for issue #45 CI harness |

The replay subset exercises four `metacrucible` subcommands against
recorded JSONL fixtures: `review`, `bootstrap`, `optimize`, and
`synthesize`. The `test-replay` task pins the four test files that
cover this surface so a change to the optimizer or the CLI command
shape is reflected in CI.

## Test layers

MetaCrucible runs three test layers with different purposes and
different cost profiles. Pick the right one for each change.

### 1. Unit tests — `mise run test`

Pure-logic tests. No LLM, no network, no real secrets, no provider
API keys. Most of the suite lives here and runs on every push through
`mise run test`.

### 2. Recorded replay tests — `mise run test-replay`

Deterministic fixtures stand in for live LLM calls so CI can exercise
the full optimizer and CLI surface cheaply and deterministically.
The replay module (`src/metacrucible/replay.py`) loads JSONL
fixtures and rejects any fixture that contains a high-confidence
secret pattern — AWS access key IDs (`AKIA[0-9A-Z]{16}`), GitHub
personal access tokens (`ghp_…`), or Stripe live secret keys
(`sk_live_…`). The guard is part of the replay contract and is
unit-tested.

`mise run test-replay` covers:

- `tests/test_replay_harness.py` — replay loader and contract.
- `tests/test_replay_cli.py` — CLI surface for `--replay` across
  `review`, `bootstrap`, `optimize`, and `synthesize`.
- `tests/test_ci_workflow.py` — CI workflow pins `mise run test-replay`.
- `tests/test_mise_toolchain.py` — `mise.toml` exposes the
  `test-replay` task and references the four files above.

### 3. Local-real tests (opt-in, not run in CI)

The developer can wire a real LLM provider on their local machine to
sanity-check optimizer and adapter behavior end-to-end. These tests
are not part of the suite and are not run in CI; they exist for
reproducing real failures before recording a new replay fixture.

> **CI does not require provider API keys. Recorded replay tests use
> deterministic fixtures; do not put real secrets in fixtures.**

## Working on replay or CLI changes

When you change the optimizer or the CLI command surface, add or
update a recorded replay test in `tests/test_replay_cli.py` so CI
exercises the change through `mise run test-replay`. New fixtures
live alongside the existing JSONL fixtures and must satisfy the
secret-pattern guard described above.

## Further reading

- `docs/adr/0021-test-with-real-llms-locally-and-recorded-replays-in-ci.md`
- `docs/adr/0028-define-claude-code-adapter-contract.md`
- `docs/adr/0036-pin-project-metadata-policy.md`
