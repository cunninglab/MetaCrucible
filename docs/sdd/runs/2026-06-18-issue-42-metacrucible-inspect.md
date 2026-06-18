---
type: SDD Run Record
title: "metacrucible inspect (PRD F5)"
description: "Implements metacrucible inspect as a read-only diagnostic command that surfaces revision history, acceptance decisions, evidence bundle index, and current best revision id without emitting BLOCKED bundles."
sdd_version: "0.1"
status: ready-for-pr
source_type: issue
source_ref: "#42 https://github.com/Cunning-Kang/MetaCrucible/issues/42"
branch: "sdd/issue-42-metacrucible-inspect"
base_sha: "d844487a6881cbb2de2d05cf0df4a2938cd31489"
head_sha: "1e249d8441ce6b70b6635eb81d1dd4a2aab9721a"
created_at: "2026-06-18T13:00:00Z"
tags: [sdd, run-record, issue-42, prd-f5, inspect]
---

## Summary

Implements PRD F5 `metacrucible inspect` (issue #42) — a read-only diagnostic
command that exposes the prior MetaCrucible optimization state for a given
artifact workspace, including revision history, acceptance decisions, evidence
bundle index, and the current best revision id. Inspect never writes a BLOCKED
bundle or any other state.

The slice ships 4 task commits plus 1 integration-fix commit:

- `2c23af3` — Task 1: parser shell + thin `cmd_inspect` wrapper. Adds the
  `inspect` subparser (positional `path` + `--json`), dispatches to
  `cmd_inspect`, and proves via a static guard and a runtime tripwire that
  the inspect path never calls `write_blocked_bundle`. Five tests cover the
  parser, --json, missing-path failure (with HOME monkeypatch and
  `~/.metacrucible/evidence/` absence assertion), dispatch smoke, and the
  static BLOCKED-bundle guard.
- `d967019` — Task 2: real read-side reader. `src/metacrucible/inspect.py`
  with `resolve_inspect_paths`, `_load_json`, `_load_history`,
  `_project_revision_event`, `_project_acceptance_decision`,
  `build_inspect_payload`. State keys are `{schema_version,
  current_best_revision, last_run_id, baseline}` per `_default_state`;
  history.jsonl records are event-shaped with nested `decision` dicts
  carrying `{accepted, reason, baseline_eval_fail_blocked_count,
  candidate_eval_fail_blocked_count, new_held_out_fail_blocked_case_ids,
  held_out_pass_to_fail_case_ids, eval_fail_to_pass_case_ids,
  eval_pass_to_fail_case_ids}`. JSON output keys are exactly the seven
  pinned in `INSPECT_OUTPUT_KEYS`: `artifact_path, workspace_path,
  envelope_status, current_best_revision_id, revision_history,
  acceptance_decisions, evidence_bundles`. cmd_inspect delegates to
  `build_inspect_payload` and renders the human table with the required
  column header `revision_id | status | accepted_at | eval_score |
  held_out_delta`.
- `2ce133c` — Task 3: user-global evidence bundle index. `_load_evidence_bundles`
  scans `$HOME/.metacrucible/evidence/<run_id>/receipt.json` via
  `Path.home()` only (no `UserGlobalStorage` instantiation, preserving the
  read-only contract). Per-receipt loading is wrapped in `try/except` so a
  single corrupt receipt is skipped silently. Adds tests for empty
  workspace, user-global evidence index, malformed receipt skip, and no
  mutation across both JSON and human modes.
- `66e5d2b` — Task 4: full PRD F5 acceptance gate. End-to-end `main()`
  test pins every public surface (human + JSON, all six required sections,
  non-empty `revision_history`/`acceptance_decisions`/`evidence_bundles`,
  no-mutation via `snapshot_tree`). Negative BLOCKED-bundle tests:
  `test_inspect_never_writes_blocked_bundle_on_bad_input` monkeypatches
  `write_blocked_bundle` to fail on any call; `test_inspect_is_not_blocked_bundle_emitter`
  asserts `HOME/.metacrucible/evidence/` is never created. Static-source
  check confirms no `inspect-not-implemented` placeholders remain.
- `1e249d8` — Integration fix: re-anchor reader and tests to the REAL
  optimizer schema. The global quality review (attempt-00) verified that
  the real comparator decision dict (optimizer.py:2092) carries ONLY
  `accepted, reason, baseline_eval_fail_blocked_count,
  candidate_eval_fail_blocked_count, new_held_out_fail_blocked_case_ids,
  held_out_pass_to_fail_case_ids, eval_fail_to_pass_case_ids,
  eval_pass_to_fail_case_ids` and that `state.current_best_revision` is
  only written at `init` (as None) and never updated post-optimize. The
  reader now projects only fields the real optimizer writes: revision_id
  derived from `run_id/round_id`, status mapped from event,
  accepted_at from event timestamp, eval_score/held_out_delta as None,
  plus raw passthrough of event/run_id/round_id/reason/eval-counts/
  case-id-sets/timestamp. `current_best_revision_id` is hardcoded to None
  on real workspaces (documented producer limitation; `init` is the only
  writer, and it writes None). Tests use `_real_decision` helper matching
  the real schema; obsolete fallback-resolver tests are removed;
  `test_inspect_current_best_revision_is_none_on_real_workspace` pins
  the limitation. `_load_history` now wraps per-line parsing in
  `try/except` to mirror the per-receipt isolation in
  `_load_evidence_bundles`.

PRD F5 acceptance criteria coverage (test surface):

- Read-only: `test_inspect_missing_path_does_not_write_evidence`,
  `test_inspect_does_not_modify_workspace_or_home`,
  `test_inspect_does_not_reference_write_blocked_bundle`,
  `test_inspect_never_writes_blocked_bundle_on_bad_input`.
- Shows history/acceptance/evidence index/best revision id:
  `test_inspect_public_command_full_prd_f5_acceptance` and the per-section
  tests in `test_inspect_json_reads_real_state_and_event_history` +
  `test_inspect_human_output_shows_required_sections`.
- Does not emit BLOCKED bundle: see read-only tests above.
- Input artifact exists: `test_inspect_missing_path_does_not_write_evidence`
  (returns EXIT_USER_ERROR) and the `resolve_inspect_paths` precondition
  in `test_inspect_missing_state_returns_clean_error_without_blocked_bundle`.
- `--json` shape stable: `test_inspect_json_reads_real_state_and_event_history`
  asserts the exact 7-key set via `set(payload) == {...}`.
- No files modified by inspect: `test_inspect_does_not_modify_workspace_or_home`
  via `snapshot_tree` byte-for-byte comparison across both modes.

## Evidence

- `git log d844487..HEAD --oneline`:
  ```
  1e249d8 Integration repair: re-anchor inspect reader to real optimizer schema
  66e5d2b Add full PRD F5 inspect acceptance and BLOCKED-bundle negative tests
  2ce133c Index user-global evidence and add best revision fallback for inspect
  d967019 Read real state and event-shaped history for metacrucible inspect
  2c23af3 Add metacrucible inspect parser and cmd_inspect thin wrapper
  ```
- `git diff --stat d844487..HEAD`:
  ```
   src/metacrucible/__main__.py  |  85 ++++++++++--
   src/metacrucible/inspect.py   | 304 +++++++++++++++++++++++++++++++++
   tests/test_inspect_command.py | 754 ++++++++++++++++++++++++++++++++++++++++++++++
   3 files changed, 1142 insertions(+), 1 deletion(-)
  ```
- Final verification: `pytest tests/test_inspect_command.py
  tests/test_review_command.py tests/test_optimize_command.py
  tests/test_synthesize_command.py tests/test_init_command.py
  tests/test_baseline_command.py tests/test_blocked_bundle_policy.py` =
  181 passed, 1 skipped in 9.13s (the skip is the pre-existing
  `optimize/test_optimize_command.py` skip unrelated to inspect).

## Runtime Plan snapshot

The canonical SDD Runtime Plan for this slice lives at:
`.sdd/plans/issue-42-metacrucible-inspect.md`

It carries the full header (Source, Pipeline status, Goal, Acceptance
criteria, Assumptions), the four task blocks with their Files / Steps /
Verification / Status sections, the Finalization block, and the Resume
notes. The plan was revised twice mid-execution (Plan revision 1
corrected Tasks 2/3 to the event-shaped history schema; Plan revision 2
corrected Tasks 2/3/4 to the real optimizer decision dict shape after the
global quality review verified the real producer schema). The Plan's
Finalization block records the final statuses of all reviews and the
integration-fix commit SHA.

## Citations

- Issue: <https://github.com/Cunning-Kang/MetaCrucible/issues/42>
- PRD F5: <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/prd.md#f5-inspect>
- ADR 0030 (receipt/evidence-bundle v1 schema):
  <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/adr/0030-pin-receipt-and-evidence-bundle-v1-schema.md>
- ADR 0032 (optimizer pipeline contract):
  <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/adr/0032-pin-optimizer-pipeline-contract.md>
- ADR 0035 (MVP CLI surface and operational behavior):
  <https://github.com/Cunning-Kang/MetaCrucible/blob/main/docs/adr/0035-pin-mvp-cli-surface-and-operational-behavior.md>