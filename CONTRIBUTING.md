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

### 3. Local-real tests — `mise run test-local-real` (opt-in, not run in CI)

Layer 3 wires a real Claude Code (`claude`) and/or oh-my-pi (`omp`)
binary against a materialized Skill or injected subagent to prove the
adapter contract end-to-end on the developer's machine. The tests are
opt-in, never run in CI, and exist to reproduce real failures before
recording a new replay fixture or cutting a release.

#### How to invoke

```bash
# Runs the full local-real smoke suite (claude + omp tests).
METACRUCIBLE_RUN_LOCAL_REAL=1 mise run test-local-real
```

Under the hood `mise run test-local-real` calls `pytest -m local_real`,
which selects the eight `tests/test_local_real_adapter.py` cases that
carry `@pytest.mark.local_real`. The `mise run test` task excludes
these by design (`pytest -m 'not local_real'`).

#### Skip rules

A local-real test skips cleanly (not fails) when any of its gates are
closed. There is no "skipped is red" surface; a developer who has not
opted in sees a clean green skip summary.

- **Env gate** — `METACRUCIBLE_RUN_LOCAL_REAL=1` must be set. Without
  it, every binary-spawning test skips with
  `METACRUCIBLE_RUN_LOCAL_REAL=1 is required to run local-real smoke tests`.
- **Binary on `$PATH`** — The relevant runtime binary must be
  installed:
  - `claude` (Claude Code) for the four claude-path tests; absent
    binaries skip with `claude binary not found on $PATH`.
  - `omp` (oh-my-pi) for the two omp-path tests; absent binaries
    skip with `omp binary not found on $PATH`.
- **Marker sanity** — `test_local_real_marker_is_registered` runs
  unconditionally so the marker discipline itself cannot silently rot.

#### No provider API-key requirement

The local-real harness uses the developer's existing OS-keychain /
Claude / oh-my-pi subscription. It never reads `ANTHROPIC_API_KEY` or
any provider key from the environment, and no test fixture holds a
real key. CI does not require provider API keys either — recorded
replay tests (layer 2) use deterministic JSONL fixtures.

#### Does NOT mutate the user home

The local-real smoke must not write to the user's real
`~/.claude/` or `~/.omp/`. Two tests pin this contract by forcing
`HOME`/`USERPROFILE` to a `tmp_path` fixture:

- `test_local_real_skill_discovery_never_touches_user_home`
  (`tests/test_local_real_adapter.py`)
- `test_local_real_subagent_injection_never_touches_user_home`
  (`tests/test_local_real_adapter.py`)

If a future regression causes the harness to write to user-home
layout under `fake-home`, one of those tests fails loudly. Do not
relax those assertions to "make CI green" — they are the boundary
between the smoke suite and the user's real environment.

#### The eight local-real tests

`tests/test_local_real_adapter.py` is the only file in layer 3. Each
case proves one behavior:

| Test | Proves |
| --- | --- |
| `test_local_real_marker_is_registered` | The `local_real` marker resolves; `pytest -m local_real` is well-formed. Always runs. |
| `test_local_real_skill_discovery_via_claude` | End-to-end Skill discoverability under `claude --bare` with `--add-dir`; writes `tmp_path/evidence/` (raw stream-json, stderr, evidence.json, preflight.json). |
| `test_local_real_skill_discovery_never_touches_user_home` | The skill smoke does not write under the (monkey-patched) user home. |
| `test_local_real_subagent_injection_via_claude` | End-to-end subagent discoverability under `claude --bare` with `--agents`; writes `tmp_path/evidence-subagent/` (raw stream-json, stderr, evidence.json, preflight.json, argv.json). Uses the terse `local_real=True` confirm-prompt. |
| `test_local_real_subagent_injection_never_touches_user_home` | The subagent smoke does not write under the (monkey-patched) user home. |
| `test_local_real_skill_discovery_via_omp` | ADR 0003 shared-layout contract: a Skill materialized into `.claude/skills/<name>/SKILL.md` is discoverable under `omp --cwd <isolated_root>`; writes `tmp_path/evidence-omp-skill/`. |
| `test_local_real_subagent_injection_via_omp` | ADR 0003 shared-layout contract: the same `agents.json` is discoverable under `omp --cwd <isolated_root>`; writes `tmp_path/evidence-omp-subagent/` (includes `agents_layout.json`). |
| `test_local_real_omp_tests_skip_without_local_real_env` | The omp path is gated by `METACRUCIBLE_RUN_LOCAL_REAL=1` AND `shutil.which('omp')`; the contract is pinned in a unit-style assertion that always runs. |

All eight tests carry `@pytest.mark.local_real` and live behind the
single module-level `pytestmark = pytest.mark.local_real` at the top
of the file.

#### Evidence-ref discipline (for release audits)

Each binary-spawning test writes its evidence bundle to a per-test
directory under `tmp_path/evidence*/`. The bundle always contains the
`run.evidence` dict (parsed by the harness) plus the raw stdout /
stderr captured from the runtime, and may include `argv.json` (the
exact argv the harness invoked) and `agents_layout.json` (the omp
shared-layout copy path). The bundle is the audit trail for "what
did the runtime actually do on this developer's box?".

The four evidence-writing tests now call
`_assert_evidence_present(evidence_dir, [...])` after writing, which
guarantees the directory exists, every expected file is present and
non-empty, and `evidence.json` / `preflight.json` parse as JSON. A
regression that drops an evidence file fails the test loudly instead
of silently shipping an incomplete bundle.

#### How to capture evidence for a release

When cutting a release, point pytest's per-test `tmp_path` at a
persistent directory so the evidence bundles survive after the run:

```bash
# Capture release evidence for issue #46 (or any release tag).
mkdir -p ./release-evidence/issue-46
METACRUCIBLE_RUN_LOCAL_REAL=1 \
    .venv/bin/python -m pytest tests/test_local_real_adapter.py \
    --basetemp ./release-evidence/issue-46/ \
    -v
```

After the run, every per-test scratch lives under
`./release-evidence/issue-46/test_local_real_adapter*/evidence*/`.
Cite the resulting `evidence.json` and `preflight.json` paths in the
release notes under a "Local-real evidence" section. The bundles are
test-owned scratch — they are NOT committed to the repo and they
must NEVER contain a real provider API key.

#### When to run layer 3

- You are about to record a new replay fixture (record the real run
  first, then commit the JSONL).
- You are about to cut a release that touches the adapter or the
  shared-layout contract (ADR 0003).
- You are debugging a CI failure whose root cause is suspected to be
  in the runtime harness rather than the orchestrator.

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