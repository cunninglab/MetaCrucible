# MetaCrucible

MetaCrucible is a workbench for improving portable agent capabilities through repeatable optimization, evaluation, and review loops.

## Experimental status

MetaCrucible is **experimental, pre-1.0 MVP software**. The CLI surface, schemas, and behavior may change without notice. Status is grounded in two project artifacts:

- `Development Status :: 3 - Alpha` classifier in `pyproject.toml`.
- MVP scope and five-wave delivery plan in `docs/roadmap.md`.

Pin expectations accordingly: command flags, envelope fields, and benchmark shapes can shift between waves W1–W5.

## Install

Provision Python 3.14 and the project virtualenv, then install MetaCrucible in editable mode with dev dependencies. Both commands are defined in `mise.toml`; `mise install` is the single source of truth for the Python toolchain.

```bash
mise install
mise run install
```

## Quickstart

The MVP exposes five CLI commands (F1–F5 from `docs/prd.md`). Each takes a path to a capability artifact on disk; output is human-readable by default with a `--json` switch.

```bash
metacrucible review <path>
metacrucible bootstrap <path>
metacrucible optimize <path>
metacrucible synthesize "<capability need>"   # or: metacrucible synthesize --from spec.md
metacrucible inspect <path>
```

What each does:

- **F1. review** — one-shot diagnostic. Static Review runs the Darwin 9-dimension rubric; Execution Evaluation runs when a reviewed Benchmark is present.
- **F2. bootstrap** — generate Evaluation Case drafts against an existing artifact. Cases land with `BOOTSTRAP_PENDING_REVIEW` and an envelope status of `generated`.
- **F3. optimize** — improve an artifact against a reviewed benchmark. Records a baseline, runs Optimization Rounds, and writes the best artifact, revision history, and per-round Evidence Bundles under `.metacrucible/`.
- **F4. synthesize** — create a new capability artifact from a capability need (or `--from spec.md`). Generated cases are held pending human review; optimization then runs automatically.
- **F5. inspect** — read prior optimization state: revision history, acceptance decisions, evidence bundle index, and the current best revision id. No files are modified.

## Core concepts

Terms are pinned in `CONTEXT.md`; the short forms below are the working definitions.

- **Agent Runtime** — an environment that can load and execute Skills or subagents (e.g. Claude Code, oh-my-pi).
- **Skill** — a portable capability package that teaches an agent runtime how to perform a bounded task or workflow.
- **Subagent** — a specialized execution unit delegated by a primary agent, with its own role, instructions, and tool boundary.
- **Optimization Round** — one pass of proposing a revision, applying it, evaluating the result, and deciding whether to keep or revert.
- **Acceptance Decision** — the decision to keep, reject, revert, or abort a revision after evaluating it against the baseline. Acceptance requires strict Eval Split improvement and no Held-Out Split regression.
- **Evaluation Case** — a scenario used to evaluate a capability artifact, including the input, expected behavior, and judgment method.
- **Benchmark** — a reviewed collection of evaluation cases used to define and measure capability quality for a capability artifact, split into an Eval Split (for scoring) and a Held-Out Split (for overfit detection).

## Safety model

MetaCrucible is designed to keep humans in the loop and protect the user's environment by default. The boundaries below are enforced in code and tests, not just in documentation.

- **No automatic git commits.** MetaCrucible does not create git commits automatically; the user owns version control. Optimize, synthesize, and bootstrap all leave working-tree state visible and reviewable.
- **Routing surfaces are not silently mutated.** Optimization does not automatically mutate a Skill's routing surface (frontmatter) — edits are bounded to the artifact's `Mutable Range`. Changes to routing require a human-confirmed `Exploratory Rewrite`.
- **Generated Evaluation Cases are held for review.** Cases produced by `bootstrap` or `synthesize` are written with envelope status `generated` and a `BOOTSTRAP_PENDING_REVIEW` sentinel. `optimize` refuses to start while that sentinel is present and points the user to `bootstrap`.
- **Replay fixtures reject secrets.** The recorded replay harness (`src/metacrucible/replay.py`) rejects any fixture containing a high-confidence secret pattern — AWS access key IDs (`AKIA[0-9A-Z]{16}`), GitHub personal access tokens (`ghp_…`), or Stripe live secret keys (`sk_live_…`). The guard is part of the replay contract and is unit-tested.
- **Local-real tests never touch user home or require API keys.** `tests/test_local_real_adapter.py` is opt-in (gated by `METACRUCIBLE_RUN_LOCAL_REAL=1` and `pytest.mark.local_real`). Two cases force `HOME`/`USERPROFILE` to a `tmp_path` fixture to prove the harness does not write under the real `~/.claude/` or `~/.omp/`. The harness uses the developer's existing OS-keychain / Claude / oh-my-pi subscription and never reads `ANTHROPIC_API_KEY` or any provider key from the environment. CI does not require provider API keys.

See `docs/adr/0036-pin-project-metadata-policy.md` for the binding policy, and `CONTRIBUTING.md` for the three-layer test discipline (unit, recorded replay, opt-in local-real) that pins these boundaries.

## Docs

- [`docs/prd.md`](docs/prd.md) — MVP product requirements, F1–F5 acceptance criteria, non-goals.
- [`docs/roadmap.md`](docs/roadmap.md) — five-wave MVP delivery plan (W1 Core skeleton → W5 Testing, documentation, and release).
- [`docs/adr/`](docs/adr/) — architectural decision records, including ADR 0036 (project metadata policy), ADR 0021 (real-LLM local + recorded-replay CI), ADR 0028 (Claude Code adapter contract), and ADR 0003 (shared `.claude/` layout).
- [`CONTEXT.md`](CONTEXT.md) — pinned vocabulary and the "avoid" list for each term.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — environment setup, developer commands, and the three-layer test discipline.

## Reference projects

MetaCrucible is built on three reference projects, each filling a distinct role.

- **GBrain** — engineering flow template. The development workflow and project structure follow the GBrain template.
- **Microsoft SkillOpt** — optimization algorithm basis. The propose → apply → evaluate → Acceptance Decision loop in `metacrucible optimize` and the `metacrucible synthesize` follow-up round are derived from the SkillOpt optimization approach.
- **Darwin 9-dimension SkillLens-derived rubric** — static review rubric. `metacrucible review` runs Static Review against the Darwin 9-dimension rubric (derived from Microsoft Research's SkillLens) and prints per-dimension scores plus the weakest dimensions.

The full mapping lives in `docs/prd.md` § Reference mapping; binding decisions are recorded in `docs/adr/`.

## License

MetaCrucible is released under the **MIT License**. See [`LICENSE`](LICENSE) for the full text. The MIT grant is also reflected in the `License :: OSI Approved :: MIT License` classifier in `pyproject.toml`.
