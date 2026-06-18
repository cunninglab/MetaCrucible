---
name: metacrucible
description: Use MetaCrucible to review, bootstrap, optimize, synthesize, and inspect portable Skills and Subagents through the `python -m metacrucible` CLI while preserving CLI errors, exit codes, and Evidence Bundle paths.
---

# MetaCrucible Skill

MetaCrucible is a workbench for improving portable Skills and Subagents through repeatable Static Review, Execution Evaluation, Revision, Synthesis, and Acceptance Decision loops. This Skill is the agent-facing Routing Surface for the `metacrucible` CLI; it teaches an Agent Runtime when to invoke public commands and how to surface the resulting Receipt, Evidence Bundle, and diagnostic output without reinterpreting the CLI contract.

## When to use

Use this Skill when the user wants to work with a Capability Artifact as a Canonical Source:

- run a one-shot Static Review or optional Execution Evaluation with `review`;
- generate reviewed Evaluation Case drafts with `bootstrap`;
- run optimization rounds against a reviewed Benchmark with `optimize`;
- create a new Capability Artifact from a capability need or source spec with `synthesize`;
- inspect Revision History, Acceptance Decisions, and Evidence Bundle indexes with `inspect`.

## When not to use

Do not use this Skill to edit arbitrary project code, invent Evaluation Cases outside MetaCrucible, bypass an Acceptance Gate, or hide an `EXIT_BLOCKED` result. If the user asks for unrelated coding work, use the repository's normal engineering workflow instead.

## Invocation rule

Prefer the module form from the active repository or worktree:

```sh
python -m metacrucible --help
```

Use `metacrucible ...` only when the console script is already on `PATH`. Pass user-provided paths and flags through to the CLI; this Skill documents routing and evidence propagation but does not reimplement command behavior.

## Public command overview

| Command | Use when | Primary artifact effect |
| --- | --- | --- |
| `review` | The user wants diagnostics for an existing Capability Artifact. | Reads the artifact and writes Evidence Bundles only when execution-requested review is blocked. |
| `bootstrap` | The user needs Generated Evaluation Cases for an existing artifact. | Writes generated benchmark records and pending-review state. |
| `optimize` | The user has a reviewed Benchmark and wants Revisions. | Writes Baseline, Revision History, per-round Evidence Bundles, and Acceptance Decisions. |
| `synthesize` | The user wants a new Capability Artifact from a need or spec. | Writes draft Canonical Source, Baseline, generated cases, and later optimization evidence. |
| `inspect` | The user wants prior state without mutation. | Reads Revision History, Acceptance Decisions, and Evidence Bundle index. |

## Command reference

### `review`

Purpose: Run a Static Review and, when requested and supported by a reviewed Benchmark, an Execution Evaluation against an existing Capability Artifact.

Use when: The user asks for a one-shot diagnostic, rubric scores, weakest dimensions, or a non-mutating review of a Skill or Subagent.

Required inputs: A path to an existing Capability Artifact whose Routing Surface and frontmatter can be parsed.

Key flags: Use `--json` when the caller needs a stable machine-readable result. Use execution-related flags only when the user explicitly wants Execution Evaluation and accepts its Execution Boundary.

Example:

```sh
python -m metacrucible review path/to/SKILL.md --json
```

Output and evidence: Human output includes Static Review results and skipped-execution warnings when no reviewed Benchmark is present. JSON output carries the same content. Execution-requested blocked review returns `EXIT_BLOCKED` and points to the Evidence Bundle.

### `bootstrap`

Purpose: Generate draft Evaluation Cases for an existing Capability Artifact without entering optimization.

Use when: The user needs Generated Evaluation Cases before a Benchmark is reviewed.

Required inputs: A path to an existing Capability Artifact.

Key flags: Use command help for available generation, diagnostics, and JSON options in the active CLI version.

Example:

```sh
python -m metacrucible bootstrap path/to/SKILL.md --json
```

Output and evidence: The command writes generated benchmark records and a pending-review marker. The agent must tell the user that human review is required before `optimize` can proceed.

### `optimize`

Purpose: Run Optimization Rounds that propose Revisions, evaluate them, and record Acceptance Decisions.

Use when: The artifact has a reviewed Benchmark with no generated-case sentinel and the user wants MetaCrucible to improve it.

Required inputs: A path to an existing Capability Artifact with an Artifact Envelope and reviewed Benchmark.

Key flags: Use `--json` for machine-readable progress, model/runtime flags when supplied by the user, and high-risk isolation bypass flags only after explicit user confirmation.

Example:

```sh
python -m metacrucible optimize path/to/SKILL.md --json
```

Output and evidence: The command records Baseline, Revision History, per-round Evidence Bundles, and final Acceptance Decision state. Blocked optimization returns `EXIT_BLOCKED` and includes Evidence Bundle references.

### `synthesize`

Purpose: Create a new Capability Artifact from a capability need or source spec, then hold generated cases for review before optimization resumes.

Use when: The user asks to create a new Skill or Subagent from a need rather than revise an existing artifact.

Required inputs: Either an inline capability need or a `--from` spec path, plus any required output path for the active CLI mode.

Key flags: Use `--json` for machine-readable output and pass model/runtime flags through unchanged when the user supplies them.

Example:

```sh
python -m metacrucible synthesize "write a focused database migration Skill" --json
```

Output and evidence: Initial synthesis writes a draft Canonical Source, Baseline, and Generated Evaluation Cases held pending review. Evaluation-stage blockers return `EXIT_BLOCKED` with Evidence Bundle references. Failure after stopping conditions surfaces an `aborted` outcome with diagnostic evidence.

### `inspect`

Purpose: Read existing MetaCrucible state without modifying files.

Use when: The user wants Revision History, Acceptance Decisions, current best revision id, or Evidence Bundle index for an artifact.

Required inputs: A path to an existing Capability Artifact or workspace accepted by the active CLI.

Key flags: Use `--json` when another tool or agent needs stable structured state.

Example:

```sh
python -m metacrucible inspect path/to/SKILL.md --json
```

Output and evidence: The command reports prior state and must not modify the artifact. Ordinary inspect errors are surfaced as CLI errors and do not create BLOCKED bundles.

## Support command boundary

ADR 0035 also defines `init`, `baseline create`, and `evaluate` as support commands. Use them only when a public command or maintainer instruction requires that lower-level operation; do not present them as the primary agent-facing Routing Surface.

## Error and evidence propagation

The CLI is the Canonical Source for command success, failure, and blocked state. The Agent Runtime must preserve the observed exit code, human output, JSON output when requested, and any Evidence Bundle path. Do not convert `EXIT_BLOCKED` into success, do not hide stderr, and do not retry automatically unless the user explicitly asks for a corrected invocation.

| Exit constant | Code | Agent behavior |
| --- | ---: | --- |
| `EXIT_OK` | 0 | Report success and summarize the CLI payload or human output. |
| `EXIT_USER_ERROR` | 1 | Surface the user-correctable input problem and ask only for missing information that tools cannot determine. |
| `EXIT_BLOCKED` | 2 | Report `BLOCKED`, preserve blocker ids and Evidence Bundle references, and stop before optimization or evaluation continues. |
| `EXIT_INTERNAL_ERROR` | 3 | Surface the internal failure with the command, exit code, and captured output; do not invent recovery evidence. |

### BLOCKED Evidence Bundles

ADR 0035 requires minimal BLOCKED Evidence Bundles for `baseline create`, `evaluate`, `optimize`, evaluation-stage `synthesize`, and execution-requested `review`. Ordinary `init`, `inspect`, and non-evaluation `bootstrap` failures surface normal CLI errors and do not create BLOCKED bundles.

When the CLI reports a blocked bundle, preserve these file references exactly as returned:

- `receipt.json` — stable Receipt entrypoint binding run result, artifact, benchmark, envelope, adapter, and model identities;
- `summary.json` — bounded machine-readable blocked summary;
- `trajectory-digest.json` — redacted Trajectory Digest for reviewers and optimizers.

If the CLI prints a local evidence path, include it in the user-facing answer. If JSON output includes relative bundle references, preserve those references in structured downstream messages. Never treat JSONL logs as the Evidence Bundle source of truth.

### Common blocked or error states

- Generated Evaluation Cases still pending review: tell the user to review the benchmark records before `optimize`.
- Runtime Adapter ambiguity: ask for an envelope update or a `--runtime-adapter` value instead of guessing.
- Dirty unrelated files: surface the dirty-guard message and wait for the user to clean, stash, or explicitly allow the requested behavior.
- Missing artifact or malformed frontmatter: surface the path and parse error from the CLI.
- High-risk `--no-isolation`: require explicit user confirmation before passing the flag.
