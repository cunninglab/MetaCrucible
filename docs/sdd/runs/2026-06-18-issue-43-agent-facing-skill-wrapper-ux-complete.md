---
type: SDD Run Record
title: "Agent-facing Skill wrapper UX complete (issue #43)"
description: "Replaces the SKELETON agent-facing Skill wrapper at skills/metacrucible/SKILL.md with the complete UX for public commands (review/bootstrap/optimize/synthesize/inspect), error/evidence propagation contract, agent workflow examples, troubleshooting, terminology, and references; retires the Issue #3 skeleton boundary."
sdd_version: "0.1"
status: ready-for-pr
source_type: issue
source_ref: "#43 https://github.com/Cunning-Kang/MetaCrucible/issues/43"
branch: "sdd/issue-43-agent-facing-skill-wrapper-ux-complete"
base_sha: "f7518b2a3ab99facdc707bb6572804e384c098e2"
head_sha: "0d7954a78ca8892b257d46be98b4698b9883c1f1"
created_at: "2026-06-18T23:33:54Z"
tags: [sdd, run-record, issue-43, agent-wrapper, skill-md]
---

## Summary

Implements Issue #43 — replaces the SKELETON agent-facing Skill wrapper at `skills/metacrucible/SKILL.md` with the complete UX for the public CLI commands (`review`, `bootstrap`, `optimize`, `synthesize`, `inspect`), propagates CLI errors and evidence bundles without reclassifying them, includes agent-facing documentation, and retires the Issue #3 skeleton boundary.

The slice ships 5 task commits plus 1 integration-fix commit:

- `ca2d9fe` — Task 1: Replace metacrucible Skill stub header with complete Routing Surface. New frontmatter, When to use, When not to use, Invocation rule, Public command overview. Created `tests/test_metacrucible_skill.py`. Retired the two obsolete Issue #3 assertions in `tests/test_skill_wrapper_skeleton.py` via `pytest.skip(...)`.
- `cf6d547` — Task 2: Document public command reference in metacrucible Skill wrapper. New `## Command reference` with five `### <command>` subsections (each with Purpose, Use when, Required inputs, Key flags, Example, Output and evidence) plus `## Support command boundary` (`init`, `baseline create`, `evaluate` marked as ADR 0035 support-only).
- `40635f1` — Task 3: Document error and evidence propagation in metacrucible Skill wrapper. New `## Error and evidence propagation` section with exit-code table (matching `src/metacrucible/exit_codes.py`: EXIT_OK=0, EXIT_USER_ERROR=1, EXIT_BLOCKED=2, EXIT_INTERNAL_ERROR=3), `### BLOCKED Evidence Bundles` (ADR 0035 command classes + receipt.json/summary.json/trajectory-digest.json), and `### Common blocked or error states`.
- `55428e0` — Task 4: Add agent workflow examples, troubleshooting, terminology, and references. New `## Agent workflow examples` (five copyable `python -m metacrucible <command> --json` invocations), `## Troubleshooting` (five exit-code/support-command bullets), `## Terminology for agent responses` (CONTEXT.md vocabulary subset), `## References` (CONTEXT.md, docs/prd.md, ADR 0035).
- `1301d10` — Task 5: Add Issue #43 acceptance integration test for Skill wrapper. `test_issue_43_acceptance_is_covered_by_skill_wrapper` maps each acceptance criterion to observable markdown facts (five command headings + invocations, three evidence phrases, five doc headings, five retired carve-out phrases absent).
- `0d7954a` — Integration fix (attempt-01): Remove non-existent `--runtime-adapter` flag reference from line 166 of `skills/metacrucible/SKILL.md` troubleshooting bullet, replacing it with a pointer to the envelope update mechanism + ADR 0035. The flag did not exist on any subcommand (verified via grep of `src/metacrucible/__main__.py` and live `--help` for all 10 subcommands); an agent following the original text would have hit `EXIT_USER_ERROR` on an unknown flag.

PRD F-Issue acceptance criteria coverage (test surface):

- AC1 "Wrapper exposes review/bootstrap/optimize/synthesize/inspect" — `test_skill_frontmatter_is_complete_wrapper` (description contains all five), `test_public_command_overview_sections` (`## Command reference` with five `### <command>` subsections each with required labels and `python -m metacrucible <command>` example), `test_issue_43_acceptance_is_covered_by_skill_wrapper` (heading + invocation per command).
- AC2 "Errors/evidence propagate from CLI" — `test_exit_code_table_matches_cli_contract` (exit-code labels and table rows match `src/metacrucible/exit_codes.py`), `test_blocked_bundle_propagation_is_documented` (the five ADR 0035 BLOCKED command classes, three bundle file names, EXIT_BLOCKED, "do not retry automatically" all present in the wrapper text).
- AC3 "Agent-facing docs included" — `test_agent_docs_include_examples_troubleshooting_and_references` (four required headings, nine vocabulary terms, three required links).
- AC4 "Skeleton boundary from initial wrapper is retired/replaced" — `test_retired_wrapper_boundary_is_absent` (SKELETON, Issue #3, complete UX is tracked separately, out of scope, not implemented yet all absent), the two obsolete Issue #3 assertions in `tests/test_skill_wrapper_skeleton.py` retired via `pytest.skip`, `test_issue_43_acceptance_is_covered_by_skill_wrapper` (same retired-phrase check at acceptance level).

Implementation boundary: only `skills/metacrucible/SKILL.md` (the Skill markdown), `tests/test_metacrucible_skill.py` (new deterministic markdown tests), and `tests/test_skill_wrapper_skeleton.py` (one-time Issue #3 retirement). No production code under `src/metacrucible/` was touched.

## Evidence

### Commits (chronological, base → head)

| SHA | Subject |
|-----|---------|
| ca2d9fe | Replace metacrucible Skill stub header with complete Routing Surface |
| cf6d547 | Document public command reference in metacrucible Skill wrapper |
| 40635f1 | Document error and evidence propagation in metacrucible Skill wrapper |
| 55428e0 | Add agent workflow examples, troubleshooting, terminology, and references |
| 1301d10 | Add Issue #43 acceptance integration test for Skill wrapper |
| 0d7954a | Remove non-existent --runtime-adapter flag reference from Skill troubleshooting |

### Diff stat (base → head)

```
skills/metacrucible/SKILL.md         | 248 ++++++++++++++++++++++++++++++-----
tests/test_metacrucible_skill.py     | 177 +++++++++++++++++++++++++
tests/test_skill_wrapper_skeleton.py |  38 +-----
```

3 files changed, +392 -71.

### Test results (final verification, run in the worktree venv)

Targeted (wrapper only):
- `tests/test_metacrucible_skill.py`: 7 passed, 0 failed
- `tests/test_skill_wrapper_skeleton.py`: 6 passed, 2 skipped (the retired Issue #3 assertions)
- combined: 13 passed, 2 skipped

Cross-cutting regression sweep (full command-behavior test files + wrapper):
- `tests/test_metacrucible_skill.py`, `tests/test_skill_wrapper_skeleton.py`, `tests/test_review_command.py`, `tests/test_bootstrap_command.py`, `tests/test_optimize_command.py`, `tests/test_synthesize_command.py`, `tests/test_inspect_command.py`: 134 passed, 3 skipped in 6.30s (no regressions on the five public-command test files)

Post-integration-fix targeted: 13 passed, 2 skipped (matches pre-fix baseline; no regressions).

### Branch diff scope

All changed files are within the Plan's allowed boundary:
- `skills/metacrucible/SKILL.md` — touched by Tasks 1, 2, 3, 4, 5 (via integration repair). Within `Allowed to edit` for Tasks 1-5.
- `tests/test_metacrucible_skill.py` — new file created in Task 1, appended to in Tasks 2, 3, 4, 5. Within `Allowed to create` for Task 1 and `Allowed to edit` for Tasks 2-5.
- `tests/test_skill_wrapper_skeleton.py` — modified once in Task 1 (retire two obsolete Issue #3 assertions). Within `Allowed to edit` for Task 1 only.

No out-of-scope changes. No `src/` modifications. No primary-checkout contamination (the primary checkout at `/Users/cunning/Workspaces/heavy/MetaCrucible/` was confirmed clean at every verification checkpoint despite one self-reported working-directory hazard during Task 2 that the implementer reverted before the controller ran verification).

### Repair history (per task)

- **Task 1**: spec PASS, quality PASS (Minor only). 0/3 spec repair rounds, 0/3 quality repair rounds.
- **Task 2**: spec PASS, quality PASS (Minor only). 0/3 spec repair rounds, 0/3 quality repair rounds. One implementer self-reported working-directory hazard reverted before controller verification.
- **Task 3**: spec PASS, quality PASS (Minor only — PEP 8 E402 mid-file import). 0/3 spec repair rounds, 0/3 quality repair rounds.
- **Task 4**: spec PASS (after one re-dispatch because the initial spec-reviewer yield returned a malformed `{"verdict": "PASS"}` JSON, and one IRC follow-up from the spec-reviewer with the proper report content), quality PASS (Minor only). 0/3 spec repair rounds, 0/3 quality repair rounds.
- **Task 5**: spec PASS (after one retry because the first spec-reviewer subagent exhausted its yield-reminder budget without responding; the second dispatch produced a properly shaped report), quality PASS (Minor only). 0/3 spec repair rounds, 0/3 quality repair rounds.

### Integration repair (attempt-01, commit `0d7954a`)

Triggered by global code-quality review attempt-00 returning FAIL with one Important finding:

> `skills/metacrucible/SKILL.md:166` — The troubleshooting bullet "ask for an envelope update or a `--runtime-adapter` value instead of guessing" directs agents to a CLI flag that does NOT exist. Verified via grep across `src/metacrucible/__main__.py` and live `--help` for all 10 subcommands.

Repair:
- One-line replacement at `skills/metacrucible/SKILL.md:166`. The non-existent flag reference was dropped; the principle ("do not guess") and actionable remedy ("envelope update") were preserved; the line now points to ADR 0035 for the broader resolution rule.

After repair:
- Spec reviews for Tasks 1, 2, 3, 4 re-ran (per SDD checkpoint: integration repair touches an acceptance-critical file of all four tasks). All four returned STATUS: PASS with all five Findings categories set to None and Blocking: None.
- Global code-quality review attempt-01 returned STATUS: PASS with only Minor findings (same three Minor carries from attempt-00; integration repair introduced no new Critical or Important findings).
- Verification: pytest wrapper subset 13 passed, 2 skipped (no regressions).

### Per-task review verdicts (final, post-integration-repair)

| Task | Spec (initial) | Quality (initial) | Spec (post-repair) |
|------|----------------|-------------------|---------------------|
| 1    | PASS           | PASS (Minor only) | PASS                |
| 2    | PASS           | PASS (Minor only) | PASS                |
| 3    | PASS           | PASS (Minor only) | PASS                |
| 4    | PASS (after one re-dispatch) | PASS (Minor only) | PASS |
| 5    | PASS (after one retry) | PASS (Minor only) | n/a (not affected) |

Global:
| Scope   | Attempt | Status | Notable findings |
|---------|---------|--------|------------------|
| Global  | attempt-00 | FAIL | 1 Important: non-existent --runtime-adapter flag |
| Global  | attempt-01 | PASS | 3 Minor only (test overlap, PEP 8 E402, generic isolation wording) |

### ADRs and contracts bound by this slice

- **ADR 0014** (ship CLI first with skill wrapper) — Issue #43 is the complete-UX fulfillment of this ADR's skill wrapper half.
- **ADR 0035** (MVP CLI surface and operational behavior) — public command set, support command boundary, exit behavior, and BLOCKED Evidence Bundle policy. The wrapper's `## Error and evidence propagation` and `## Command reference` sections preserve ADR 0035's contract.
- **ADR 0030** (receipt/evidence bundle v1 schema) — bundle file names (receipt.json, summary.json, trajectory-digest.json) referenced in the wrapper match this ADR's schema.
- **CONTEXT.md** vocabulary (Skill, Subagent, Capability Artifact, Revision, Baseline, Evidence Bundle, Receipt, Acceptance Gate, Execution Boundary, Runtime Adapter, Canonical Source, Artifact Envelope, Mutable Range, Routing Surface, Static Review, Execution Evaluation, Rubric, Static Review Profile, Model Provider, Adapter Preflight, Acceptance Decision, Revision History) — cross-referenced from the wrapper's `## Terminology for agent responses` section.

## Runtime Plan snapshot

The full final canonical Plan is reproduced below. It records each task's status, review verdicts, repair-round counts, and commit SHA at the time the slice was completed.

```md
# Plan: issue 43 agent-facing Skill wrapper UX complete

## Source
Type: issue
Ref: #43 https://github.com/Cunning-Kang/MetaCrucible/issues/43
Issue close mode: auto-on-merge

## Pipeline status
Status: planning (set at draft; updated through per-task loop; integration repair applied; final review PASS)
Branch: sdd/issue-43-agent-facing-skill-wrapper-ux-complete
Worktree: /Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-43-agent-facing-skill-wrapper-ux-complete
Base: f7518b2a3ab99facdc707bb6572804e384c098e2

## Goal
Replace the skeleton agent-facing `metacrucible` Skill wrapper with complete runtime guidance for the public `review`, `bootstrap`, `optimize`, `synthesize`, and `inspect` commands, including deterministic tests that prove the wrapper exposes the command Routing Surface, propagates CLI errors and Evidence Bundle locations, includes agent-facing documentation, and removes the original skeleton boundary.

## Acceptance criteria
- [x] Wrapper exposes review/bootstrap/optimize/synthesize/inspect.
- [x] Errors/evidence propagate from CLI.
- [x] Agent-facing docs included.
- [x] Skeleton boundary from initial wrapper is retired/replaced.

## Assumptions
- This slice changes only the portable Skill documentation at `skills/metacrucible/SKILL.md` and pure deterministic tests in `tests/test_metacrucible_skill.py`; CLI behavior and exit-code constants are already complete in prior issues.
- Tests parse the Skill markdown directly and may import `src/metacrucible/exit_codes.py` constants, but they must not execute the full CLI, call an Agent Runtime, or perform network I/O.
- The Skill should document `python -m metacrucible` as the reliable invocation form, while allowing the console script form when available in an Agent Runtime.
- The stable CLI automation contract is `EXIT_OK=0`, `EXIT_USER_ERROR=1`, `EXIT_BLOCKED=2`, and `EXIT_INTERNAL_ERROR=3`.
- The obsolete Issue #3 tests at `tests/test_skill_wrapper_skeleton.py` are retired in Task 1 by removing or skipping the two assertions that contradict Issue #43 acceptance criterion #4; the file-existence, frontmatter-shape, name, and CLI-invocation assertions remain in force because Issue #43 still requires those contracts.

## Task 1: Retire the stub header and define the Routing Surface

### Goal
Replace the initial stub header and limited usage text with complete frontmatter, when-to-use guidance, command overview, and evidence index so the Skill is no longer presented as an unfinished wrapper.

### Files

Allowed to edit:
- `skills/metacrucible/SKILL.md` — replace initial stub frontmatter, status warning, limited usage, and scope carve-out with complete agent-facing Skill wrapper guidance.
- `tests/test_skill_wrapper_skeleton.py` — retire the obsolete Issue #3 skeleton assertions (`test_skill_is_explicitly_skeletal`, `test_skill_documents_complete_ux_tracked_separately`) by deleting those two test functions or replacing them with `pytest.skip(...)` calls; keep file-existence, frontmatter-shape, name, and CLI-invocation tests intact because Issue #43 still requires those contracts. The new wrapper tests live in `tests/test_metacrucible_skill.py`.

Allowed to create:
- `tests/test_metacrucible_skill.py` — add deterministic markdown tests that prove the retired boundary is gone and the Skill frontmatter exposes the runtime-facing wrapper.

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: ca2d9fe

## Task 2: Add complete per-command agent guidance

### Goal
Document each public command in its own section with purpose, when to use, required inputs, key flags, example invocation, output expectations, and evidence behavior so the wrapper exposes `review`, `bootstrap`, `optimize`, `synthesize`, and `inspect` as first-class agent-facing paths.

### Files

Allowed to edit:
- `skills/metacrucible/SKILL.md`
- `tests/test_metacrucible_skill.py`

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: cf6d547

## Task 3: Document exit codes and Evidence Bundle propagation

### Goal
Add the error propagation contract that tells an Agent Runtime to surface CLI exit codes, stderr/stdout, JSON payloads, `BLOCKED` outcomes, and Evidence Bundle paths without masking or reclassifying them.

### Files

Allowed to edit:
- `skills/metacrucible/SKILL.md`
- `tests/test_metacrucible_skill.py`

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: 40635f1

## Task 4: Add examples, troubleshooting, and terminology cross-references

### Goal
Complete the agent-facing documentation with copyable examples, routing examples, troubleshooting rules, domain terminology cross-references, and links to the relevant PRD and ADR so another agent can use the Skill without guessing.

### Files

Allowed to edit:
- `skills/metacrucible/SKILL.md`
- `tests/test_metacrucible_skill.py`

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: 55428e0

## Task 5: Gate the complete wrapper and retired-boundary removal

### Goal
Run the complete Skill wrapper test file and enforce that no retired carve-out text remains in the Skill or tests while keeping the implementation boundary limited to the Skill markdown and its deterministic tests.

### Files

Allowed to edit:
- `skills/metacrucible/SKILL.md`
- `tests/test_metacrucible_skill.py`
- `tests/test_skill_wrapper_skeleton.py` — only if the final integration pass leaves obsolete assertions; the primary cleanup already happened in Task 1.

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: 1301d10

## Finalization

Final global review: pass (attempt-00 FAIL with one Important finding; attempt-01 PASS after integration repair)
Final verification: pass (pytest tests/test_metacrucible_skill.py tests/test_skill_wrapper_skeleton.py tests/test_review_command.py tests/test_bootstrap_command.py tests/test_optimize_command.py tests/test_synthesize_command.py tests/test_inspect_command.py -> 134 passed, 3 skipped; post-integration-fix: 13 passed, 2 skipped on the wrapper subset)
Integration fix:
  Status: pass
  Commit: 0d7954a
  Affected tasks: 1, 2, 3, 4 (integration repair touches Task 3's line 166 troubleshooting bullet; spec reviews re-ran for Tasks 1-4 per checkpoint rule; all PASS)
  Review exemption: global quality-review attempt-00 reported one Important finding (non-existent --runtime-adapter flag reference); integration repair applied as one-line swap; spec reviews for Tasks 1-4 re-ran PASS; global quality-review attempt-01 PASS
Run Record: ready-for-pr
Finish decision: open-pr
PR URL: pending
```

## Citations

- Issue: <https://github.com/Cunning-Kang/MetaCrucible/issues/43>
- Run Record reference (Issue #42 inspect, latest precedent): <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/sdd/runs/2026-06-18-issue-42-metacrucible-inspect.md>
- PRD: <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/prd.md>
- ADRs:
  - 0014 ship CLI first with skill wrapper: <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/adr/0014-ship-cli-first-with-skill-wrapper.md>
  - 0035 pin MVP CLI surface and operational behavior: <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/adr/0035-pin-mvp-cli-surface-and-operational-behavior.md>
  - 0030 pin receipt and evidence bundle v1 schema: <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/adr/0030-pin-receipt-and-evidence-bundle-v1-schema.md>
- CONTEXT (domain vocabulary): <https://github.com/Cunning-Kang/MetaCrucible/blob/main/CONTEXT.md>
- Skill: <https://github.com/Cunning-Kang/MetaCrucible/blob/main/skills/metacrucible/SKILL.md>
- SDD skill: `/Users/cunning/.claude/skills/subagent-driven-development/SKILL.md`