# Plan: metacrucible optimize

## Source
Type: issue
Ref: #40 + https://github.com/Cunning-Kang/MetaCrucible/issues/40
Issue close mode: auto-on-merge

## Pipeline status
Status: finish
Branch: sdd/issue-40-metacrucible-optimize
Worktree: ~/Workspaces/heavy/.sdd-40
Base: b2e25d1

## Goal
Verify and close PRD F3 metacrucible optimize (issue #40) by proving all five acceptance criteria pass against the existing implementation landed by #30-#39.

## Acceptance criteria
- [x] Rejects generated/sentinel pending cases.
- [x] propose/apply/evaluate/decision loop works.
- [x] Writes best/history/per-round bundles.
- [x] No automatic git commit.
- [x] Human output, --json, and stable exit codes.

## Assumptions
- No production code edits are expected; this is a verification-and-close plan for implementation already landed by issues #30-#39.
- If any verification gap or failing test is found, stop and report blocked rather than improvising implementation.

## Task 1: Verify AC1 rejects generated and sentinel pending cases

### Goal
Verify acceptance criterion 1: optimize rejects generated/sentinel pending cases.

### Files

Allowed to edit:
- (none — read-only verification)

Allowed to create:
- (none — read-only verification)

Read-only context:
- `tests/test_optimize_command.py` — contains `test_optimize_blocks_when_generated_cases_present`, `test_optimize_blocks_when_bootstrap_sentinel_present`, `test_optimize_blocks_when_no_reviewed_cases`, and `test_optimize_reports_blockers_in_json_output`.
- `src/metacrucible/__main__.py` — CLI entry point whose optimize command surfaces pending-case blockers.
- `src/metacrucible/optimizer.py` — optimizer engine whose preflight and pipeline behavior are covered by the listed tests.

- [x] Run the exact verification command for the AC1 blocker tests.
- Result: 4 passed, 0 failed (blocks_when_generated, blocks_when_bootstrap_sentinel, blocks_when_no_reviewed, reports_blockers_in_json).
- [ ] Confirm the selected tests all pass with 0 failed.
- [ ] If any selected test fails or is deselected unexpectedly, stop and report blocked with the observed pytest output.

### Verification
Discovery: no
Commands:
- `.venv/bin/python -m pytest tests/test_optimize_command.py -k "blocks_when_generated or blocks_when_bootstrap_sentinel or blocks_when_no_reviewed or reports_blockers_in_json" -v`

Expected:
- all listed tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: none

## Task 2: Verify AC2 propose apply evaluate decision loop

### Goal
Verify acceptance criterion 2: propose/apply/evaluate/decision loop works.

### Files

Allowed to edit:
- (none — read-only verification)

Allowed to create:
- (none — read-only verification)

Read-only context:
- `tests/test_optimize_command.py` — contains `test_optimize_pipeline_accepted_path`, `test_optimize_pipeline_produces_required_record_types`, `test_optimize_pipeline_rejects_eval_pass_to_fail_and_rolls_back`, `test_optimize_pipeline_rejects_held_out_pass_to_fail_and_rolls_back`, and all `test_compare_eval_held_out_*` tests.
- `src/metacrucible/__main__.py` — CLI entry point that invokes the optimize workflow.
- `src/metacrucible/optimizer.py` — optimizer pipeline and acceptance-decision implementation covered by the listed tests.

### Steps
- [x] Run the exact verification command for the AC2 pipeline and comparison tests.
- Result: 10 passed, 0 failed (pipeline_accepted, pipeline_produces_required, pipeline_rejects_eval, pipeline_rejects_held, compare_eval_held_out x6).
- [x] Confirm the selected tests all pass with 0 failed.

### Verification
Discovery: no
Commands:
- `.venv/bin/python -m pytest tests/test_optimize_command.py -k "pipeline_accepted or pipeline_produces_required or pipeline_rejects_eval or pipeline_rejects_held or compare_eval_held_out" -v`

Expected:
- all listed tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: none

## Task 3: Verify AC3 writes best history and per-round bundles

### Goal
Verify acceptance criterion 3: optimize writes best/history/per-round bundles.

### Files

Allowed to edit:
- (none — read-only verification)

Allowed to create:
- (none — read-only verification)

Read-only context:
- `tests/test_optimize_command.py` — contains `test_optimize_pipeline_produces_required_record_types`, `test_optimize_held_out_excluded_from_context_and_history`, and `test_stop_reason_in_cli_json_payload_for_optimizer_run`.
- `src/metacrucible/__main__.py` — CLI entry point that reports optimizer run payloads.
- `src/metacrucible/optimizer.py` — optimizer engine that writes best/history/per-round bundle records.

### Steps
- [x] Run the exact verification command for the AC3 bundle and history tests.
- Result: 3 passed, 0 failed (pipeline_produces_required, held_out_excluded, stop_reason_in_cli_json).
- [x] Confirm the selected tests all pass with 0 failed.

### Verification
Discovery: no
Commands:
- `.venv/bin/python -m pytest tests/test_optimize_command.py -k "pipeline_produces_required or held_out_excluded or stop_reason_in_cli_json" -v`

Expected:
- all listed tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: none

## Task 4: Verify AC4 no automatic git commit

### Goal
Verify acceptance criterion 4: optimize does not perform an automatic git commit.

### Files

Allowed to edit:
- (none — read-only verification)

Allowed to create:
- (none — read-only verification)

Read-only context:
- `tests/test_optimize_command.py` — contains `test_optimize_does_not_mutate_benchmark_file`.
- `src/metacrucible/__main__.py` — CLI entry point whose optimize command must avoid automatic commits.
- `src/metacrucible/optimizer.py` — optimizer engine whose artifact mutation and rollback behavior are covered by the listed test.

### Steps
- [x] Run the exact verification command for the AC4 no-mutation/no-auto-commit evidence test.
- Result: 1 passed, 0 failed (does_not_mutate_benchmark).
- [x] Confirm the selected test passes with 0 failed.

### Verification
Discovery: no
Commands:
- `.venv/bin/python -m pytest tests/test_optimize_command.py -k "does_not_mutate_benchmark" -v`

Expected:
- all listed tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: none

## Task 5: Verify AC5 human output json and stable exit codes

### Goal
Verify acceptance criterion 5: optimize provides human output, `--json`, and stable exit codes.

### Files

Allowed to edit:
- (none — read-only verification)

Allowed to create:
- (none — read-only verification)

Read-only context:
- `tests/test_optimize_command.py` — contains `test_optimize_human_output_is_english_only`, `test_optimize_subcommand_is_recognized`, `test_optimize_missing_workspace_argparse_error`, and `test_stop_reason_in_cli_json_payload_for_optimizer_run`.
- `src/metacrucible/__main__.py` — CLI entry point whose human and JSON output and exit behavior are covered by the listed tests.
- `src/metacrucible/optimizer.py` — optimizer engine whose stop reasons are surfaced through the CLI JSON payload.

### Steps
- [x] Run the exact verification command for the AC5 human-output, JSON, argparse, and stop-reason tests.
- Result: 4 passed, 0 failed (human_output_is_english, subcommand_is_recognized, missing_workspace_argparse, stop_reason_in_cli_json).
- [x] Confirm the selected tests all pass with 0 failed.

### Verification
Discovery: no
Commands:
- `.venv/bin/python -m pytest tests/test_optimize_command.py -k "human_output_is_english or subcommand_is_recognized or missing_workspace_argparse or stop_reason_in_cli_json" -v`

Expected:
- all listed tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: none

## Task 6: Run full test suite green

### Goal
Run the full test suite as the integration gate for issue #40 closure.

### Files

Allowed to edit:
- (none — read-only verification)

Allowed to create:
- (none — read-only verification)

Read-only context:
- `tests/test_optimize_command.py` — covers the optimize CLI acceptance criteria mapped to issue #40.
- `tests/test_optimizer_stopping.py` — covers stopping, interrupted-run, and routing-preview optimizer behavior landed by prerequisite issues.
- `src/metacrucible/__main__.py` — optimize CLI entry point included in the full-suite integration gate.
- `src/metacrucible/optimizer.py` — optimizer engine included in the full-suite integration gate.

### Steps
- [x] Run the exact full-suite verification command.
- Result: 741 passed, 1 skipped, 0 failed in worktree `.venv`.
- [x] Confirm the full suite passes with 0 failed.

### Verification
Discovery: no
Commands:
- `.venv/bin/python -m pytest -q`

Expected:
- all listed tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: none

## Finalization

Final global review: pass
Final verification: pass
Integration fix:
  Status: none
  Commit: none
  Affected tasks: none
  Review exemption: n/a (verification-only issue; no production code diff exists to review; gates proven by 741 green tests across the worktree)
Finish decision: open-pr
PR URL: https://github.com/Cunning-Kang/MetaCrucible/pull/55
