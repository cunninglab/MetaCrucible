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

(Further sections — per-command bodies, error and evidence propagation, agent workflow examples, troubleshooting, terminology, and references — will be added by Tasks 2, 3, 4, and 5. This Task 1 ends with the routing surface above.)