# Plan: metacrucible synthesize

## Source
Type: issue
Ref: #41 + PRD F4 `metacrucible synthesize`
Issue close mode: auto-on-merge

## Pipeline status
Status: complete
Branch: sdd/issue-41-metacrucible-synthesize
Worktree: /Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize
Base: b7a2da3e3da6f7d81b79568a1630001f43ea3913

## Goal
Implement PRD F4 `metacrucible synthesize` as the public CLI path that creates a new capability artifact workspace from either an inline capability need or a source spec file, records a baseline, produces generated evaluation cases held pending review with the same sentinel/envelope mechanism as `bootstrap`, then resumes into the existing optimizer loop after human-reviewed cases are available.

## Acceptance criteria
- [ ] `metacrucible synthesize "<capability need>"` creates a workspace, writes a draft canonical source, writes `.metacrucible/envelope.json`, `.metacrucible/state.json`, `benchmark.jsonl`, and records baseline/synthesis history without requiring an existing artifact.
- [ ] `metacrucible synthesize --from spec.md` reads the spec file as the capability need and produces the same durable synthesis outputs; missing, empty, or conflicting input modes are rejected with stable blockers and no partial workspace.
- [ ] Generated Evaluation Cases are written with `status: "generated"` and `BOOTSTRAP_PENDING_REVIEW_FIELD` set to `true`, so existing benchmark loading and `optimize` preconditions hold them pending human review.
- [ ] When reviewed eval and held-out cases are already present and no generated/sentinel cases remain, `synthesize` automatically invokes the existing optimizer loop using the same bounded settings as `optimize` and reports the optimizer acceptance decision.
- [ ] The synthesized artifact is reported as accepted only when the optimizer returns an accepted Acceptance Decision; otherwise the outcome is `aborted` with diagnostic evidence after configured stopping conditions.
- [ ] Evaluation-stage blockers in `synthesize` write a minimal BLOCKED evidence bundle using the existing `synthesize_evaluation_stage` matrix slot from `blocked_bundles.py`.
- [ ] CLI output is human-readable by default, `--json` emits a stable machine-readable payload, and the command returns existing stable exit codes: `EXIT_OK` for accepted or draft-pending-review success, `EXIT_BLOCKED` for blocked/aborted preconditions or evaluation-stage blockers, and `EXIT_USER_ERROR` for argparse input errors.

## Assumptions
- The implementation may choose a deterministic default output workspace for inline needs when `--output` is not provided, but tests should prefer explicit `--output` to avoid current-working-directory ambiguity.
- Draft canonical source may be a minimal Skill-style Markdown capability artifact because PRD F4 requires a draft canonical source, not provider-quality content.
- Baseline recording for synthesis means durable baseline facts in `.metacrucible/state.json` and `.metacrucible/history.jsonl`; do not require the `baseline create` CLI to run as a subprocess.
- Deterministic draft source and generated cases satisfy this MVP slice; provider-backed synthesis is not part of this issue.

## Task 1: Add synthesize parser and command dispatch

### Goal
Expose `metacrucible synthesize` through argparse and dispatch it to a real command wrapper without creating synthesis outputs yet.

### Files

Allowed to edit:
- `src/metacrucible/__main__.py` — add synthesize parser flags, input-mode argparse validation, `cmd_synthesize(args)` thin wrapper, and `main()` dispatch.

Allowed to create:
- `tests/test_synthesize_command.py` — add parser and dispatch tests for the new public command.

Read-only context:
- `docs/prd.md` — PRD F4 acceptance text.
- `docs/adr/0035-pin-mvp-cli-surface-and-operational-behavior.md` — public command and BLOCKED bundle behavior.
- `tests/test_optimize_command.py` — parser and `cmd_optimize` wrapper test patterns.
- `tests/test_init_command.py` — CLI output and workspace assertion patterns.

### Steps
- [ ] In `tests/test_synthesize_command.py`, add `test_synthesize_parser_accepts_inline_need` that imports `_build_parser`, parses `['synthesize', 'write a skill', '--output', str(tmp_path / 'skill')]`, and asserts `args.command == 'synthesize'`, `args.capability_need == 'write a skill'`, `args.from_spec is None`, `args.output` matches the supplied path, `args.max_rounds` is present, and `args.json is False`.
- [ ] Add `test_synthesize_parser_accepts_from_spec` that parses `['synthesize', '--from', str(spec_path), '--output', str(tmp_path / 'skill'), '--json']` and asserts `args.capability_need is None`, `args.from_spec` matches the spec path, and `args.json is True`.
- [ ] Add `test_synthesize_parser_rejects_missing_input` and `test_synthesize_parser_rejects_conflicting_input` using `pytest.raises(SystemExit)`; call `parser.parse_args(...)` and assert the exit code is `2`.
- [ ] In `src/metacrucible/__main__.py`, add `synthesize_parser = subparsers.add_parser('synthesize', ...)` beside the other public PRD commands.
- [ ] Add optional positional `capability_need`, `--from` stored as `from_spec`, `--output`, `--max-rounds` defaulting to `ROUND_BUDGET_DEFAULT`, `--allow-routing-revision`, `--allow-dirty-unrelated`, `--confirm-resume`, and `--json`. Keep shared flag help text consistent with `optimize` where the meaning is identical.
- [ ] Enforce exactly one input mode at parse time. If current parser structure cannot use an argparse mutually exclusive group for a positional plus `--from`, subclass or wrap parser parsing so `_build_parser().parse_args(...)` raises `SystemExit(2)` for missing and conflicting synthesize input.
- [ ] Add `cmd_synthesize(args)` as a thin wrapper that imports `run_synthesize_command` from `metacrucible.synthesize`, calls `run_synthesize_command(args, emit=_emit, now=_now_iso)`, and returns the result.
- [ ] Dispatch `args.command == 'synthesize'` to `cmd_synthesize(args)` in `main()`.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest tests/test_synthesize_command.py -k "parser" -v`

Expected:
- parser tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 1/3
Commit: b301e6634033488880893c5207ed97638c81830b

## Task 2: Create draft workspace and pending generated cases

### Goal
Implement the non-optimizing synthesis path: a valid need or spec creates the draft canonical source, envelope/state, baseline/history records, and pending generated evaluation cases, then exits successfully with a draft-pending-review outcome.

### Files

Allowed to edit:
- `src/metacrucible/__main__.py` — keep `cmd_synthesize` a thin wrapper and add imports only if the wrapper requires them.
- `tests/test_synthesize_command.py` — add command tests for draft workspace creation, `--from`, and precondition blockers.

Allowed to create:
- `src/metacrucible/synthesize.py` — implement synthesis input resolution, draft source, generated cases, workspace writes, and command payloads.

Read-only context:
- `src/metacrucible/__main__.py` — `_emit`, `_now_iso`, `BENCHMARK_FILE_NAME`, `_default_state`, `_default_metadata_record`, `BOOTSTRAP_PENDING_REVIEW_FIELD`, `STATUS_GENERATED`, and command style.
- `src/metacrucible/storage.py` — `RepositoryStorage` envelope/state/history helpers.
- `src/metacrucible/promote.py` — `_atomic_write_jsonl` helper for benchmark writes.
- `src/metacrucible/benchmark.py` — generated case partition behavior.
- `tests/test_init_command.py` and `tests/test_optimize_command.py` — workspace and JSON output assertions.

### Steps
- [ ] In `src/metacrucible/synthesize.py`, define stable constants: `SYNTHESIZE_INPUT_MISSING_BLOCKER = 'synthesize-input-missing'`, `SYNTHESIZE_INPUT_CONFLICT_BLOCKER = 'synthesize-input-conflict'`, `SYNTHESIZE_SPEC_MISSING_BLOCKER = 'synthesize-spec-missing'`, `SYNTHESIZE_SPEC_EMPTY_BLOCKER = 'synthesize-spec-empty'`, `SYNTHESIZE_OUTPUT_EXISTS_BLOCKER = 'synthesize-output-exists'`, `SYNTHESIZE_DRAFT_PENDING_REVIEW = 'draft_pending_review'`, and `SYNTHESIZE_ABORTED = 'aborted'`.
- [ ] Implement `resolve_synthesize_input(capability_need: str | None, from_spec: str | None) -> tuple[str | None, list[dict[str, str]]]` that returns stripped need text or blockers. It must reject no input, both inputs, missing spec path, and empty spec content. It must read the spec with UTF-8 only after confirming the path is a file.
- [ ] Implement `default_artifact_filename(need: str) -> str` that returns `synthesized-skill.md` for empty/non-sluggable text and otherwise lowercases ASCII alphanumeric runs, joins them with `-`, truncates to a bounded prefix, and appends `.md`.
- [ ] Implement `build_draft_canonical_source(need: str) -> str` returning deterministic Markdown Skill text with YAML frontmatter fields `name` and `description`, a `# Capability Need` section containing the capability need verbatim, and a `# Operating Instructions` section that tells the future maintainer to replace generated draft guidance after review. Ensure the string ends with exactly one newline.
- [ ] Implement `build_generated_cases(need: str, *, now: str) -> list[dict[str, object]]` returning at least two records: one `record_type: 'case_eval'` with `split: 'eval'` and one `record_type: 'case_held_out'` with `split: 'held_out'`. Each record must have unique `case_id` prefixed with `synthesize-`, `status: STATUS_GENERATED`, `reviewed: False`, `input`, `expected_behavior`, `checks: []`, `judgment: None`, `created_at: now`, and `BOOTSTRAP_PENDING_REVIEW_FIELD: True`.
- [ ] Implement `create_synthesis_workspace(output: Path, need: str, *, now: str) -> dict[str, object]` that refuses an existing output path, creates the output directory, writes the draft artifact under it, writes `.metacrucible/envelope.json` with at least `artifact_workspace`, `artifact_path`, `source: 'synthesize'`, and `capability_need_hash`, writes `.metacrucible/state.json` with the default state plus a `baseline` mapping containing hashes for the draft artifact and benchmark container, writes `benchmark.jsonl` containing metadata then generated case records via `_atomic_write_jsonl`, and appends history records for `synthesis_started`, `baseline_recorded`, `generated_cases_created`, and `synthesis_pending_review`.
- [ ] Implement `run_synthesize_command(args, *, emit, now) -> int`. If any precondition blocker exists before workspace creation, emit payload with `status: 'BLOCKED'`, `outcome: 'blocked'`, `blockers`, `workspace`, `generated_case_ids: []`, and return `EXIT_BLOCKED` without creating the output path.
- [ ] On success with generated pending cases, emit payload with `status: 'OK'`, `outcome: 'draft_pending_review'`, `workspace`, `artifact_path`, `benchmark`, `generated_case_ids`, `sentinel: BOOTSTRAP_PENDING_REVIEW_FIELD`, `baseline`, `blockers: []`, and return `EXIT_OK`.
- [ ] Add `test_synthesize_inline_need_creates_draft_pending_review_workspace` that calls `cmd_synthesize` with an `argparse.Namespace` and `json=True`, then asserts return code `EXIT_OK`, JSON outcome `draft_pending_review`, artifact file exists, envelope points at that artifact, benchmark contains generated eval and held-out records with the sentinel, state contains baseline hashes, and history contains the four synthesis events.
- [ ] Add `test_synthesize_from_spec_creates_same_pending_review_shape` by writing a spec file, invoking `cmd_synthesize`, and asserting the draft source contains the spec text and the same generated-case/sentinel contract holds.
- [ ] Add precondition tests for missing spec, empty spec, and existing output directory. Assert return code `EXIT_BLOCKED`, stable blocker ids, and no new artifact is written for blocker paths that fail before creation.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest tests/test_synthesize_command.py -k "draft_pending_review or from_spec or spec_missing or spec_empty or output_exists" -v`

Expected:
- selected tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 1/3
Commit: 0e74786ad99f44964d4ec7e66fdbd7a376de16c9

## Task 3: Resume reviewed synthesis into optimizer loop

### Goal
When a synthesis workspace already contains reviewed eval and held-out cases and no pending generated/sentinel cases, run the existing optimizer loop automatically and report acceptance, rejection, or aborted outcome correctly.

### Files

Allowed to edit:
- `src/metacrucible/synthesize.py` — add existing-workspace loading, benchmark readiness detection, optimizer invocation, and accepted/aborted payload mapping.
- `tests/test_synthesize_command.py` — add reviewed-workspace optimizer resume tests.

Allowed to create:
- (none)

Read-only context:
- `src/metacrucible/__main__.py` — `cmd_optimize` precondition flow, routing/resume flag behavior, and optimizer payload shape.
- `src/metacrucible/optimizer.py` — `run_optimizer_pipeline` result fields and accepted/rejected/blocked statuses.
- `src/metacrucible/benchmark.py` — reviewed eval and held-out eligibility rules.
- `tests/test_optimize_command.py` — monkeypatch patterns for `run_optimizer_pipeline` and `OptimizerPipelineResult`-compatible stubs.

### Steps
- [ ] Add a helper in `tests/test_synthesize_command.py` that creates a synthesis workspace from Task 2, rewrites generated case records into reviewed eval and reviewed held-out records by setting `status: 'reviewed'`, `reviewed: True`, and removing or setting `BOOTSTRAP_PENDING_REVIEW_FIELD` to `False`.
- [ ] Add `test_synthesize_reviewed_workspace_runs_optimizer_and_accepts`: monkeypatch the optimizer entrypoint used by synthesize to return a stub result with `status='ACCEPTED'`, `rounds=1`, `acceptance_decision={'accepted': True, 'reason': 'accepted'}`, evidence refs, record counts, and selected candidate ids. Invoke `cmd_synthesize` against the same output workspace and assert return `EXIT_OK`, outcome `accepted`, optimizer called once with workspace, benchmark path, artifact path, `max_rounds`, `human_confirmed=False`, and `routing_confirmation_preview=True`, and payload includes acceptance decision.
- [ ] Add `test_synthesize_reviewed_workspace_aborts_when_optimizer_rejects`: stub result `status='REJECTED'`, `rounds` equal to max rounds, `stop_reason='max_rounds'`, `acceptance_decision={'accepted': False, 'reason': 'no_eval_improvement'}`. Assert return `EXIT_BLOCKED`, outcome `aborted`, payload includes diagnostic evidence refs, stop reason, and no accepted flag.
- [ ] Add `test_synthesize_keeps_pending_review_without_optimizer_call`: leave generated sentinel records in place, monkeypatch optimizer to fail if called, invoke `cmd_synthesize` for the existing workspace, and assert outcome remains `draft_pending_review` with `EXIT_OK`.
- [ ] Implement `load_synthesis_workspace(output: Path) -> dict[str, object]` that reads envelope/state/benchmark and resolves `artifact_path`; return blockers when required files are absent.
- [ ] Implement `benchmark_ready_for_optimization(benchmark_path: Path) -> tuple[bool, list[dict[str, object]]]` using `load_benchmark`. It must return not ready with no blocker when pending generated cases are present, and ready only when eligible eval and held-out counts are both non-zero and loader blockers are empty.
- [ ] Implement `run_synthesis_optimizer(...)` that calls `run_optimizer_pipeline` with `call_fn=None`, `max_rounds`, `human_confirmed=False`, and `routing_confirmation_preview=True`; pass through shared flags only when they are already supported and tested.
- [ ] In `run_synthesize_command`, if output exists and is a synthesis workspace, do not recreate files. Instead inspect benchmark readiness. If pending generated cases exist, emit the existing pending-review payload and return `EXIT_OK`. If ready, call the optimizer. Map `status == 'ACCEPTED'` and `acceptance_decision.accepted is True` to `outcome: 'accepted'`, `status: 'OK'`, `EXIT_OK`. Map every other optimizer completion to `outcome: 'aborted'`, `status: 'BLOCKED'`, `EXIT_BLOCKED`, preserving `blockers`, `warnings`, `evidence_refs`, `record_counts`, `rounds`, `stop_reason`, and `acceptance_decision`.
- [ ] Append `synthesis_optimizer_started` before the optimizer call and `synthesis_finished` after it, with the final outcome and stop reason.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest tests/test_synthesize_command.py -k "reviewed_workspace_runs_optimizer or reviewed_workspace_aborts or keeps_pending_review" -v`

Expected:
- selected tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: 7e8134d617ef83f4ceace5d11dd2b908d6614c63

## Task 4: Write synthesize evaluation-stage BLOCKED bundles

### Goal
Ensure synthesize evaluation-stage blockers produce the ADR 0035 minimal BLOCKED evidence bundle under run type `synthesize_evaluation_stage`.

### Files

Allowed to edit:
- `src/metacrucible/synthesize.py` — add synthesize evaluation-stage BLOCKED bundle writer and attach evidence refs to aborted optimizer payloads.
- `tests/test_synthesize_command.py` — add BLOCKED evidence bundle coverage for optimizer-stage failure.

Allowed to create:
- (none)

Read-only context:
- `src/metacrucible/blocked_bundles.py` — existing `REQUIRES_BLOCKED_BUNDLE_CATEGORIES` includes `synthesize_evaluation_stage`.
- `src/metacrucible/storage.py` — `UserGlobalStorage` evidence paths.
- `src/metacrucible/__main__.py` — `_write_optimize_blocked_bundle` and `_write_evaluate_blocked_bundle` best-effort pattern.
- `tests/test_optimize_command.py` — BLOCKED bundle assertions.

### Steps
- [ ] Add `test_synthesize_evaluation_stage_block_writes_blocked_bundle`: set `HOME` to `tmp_path`, create a reviewed synthesis workspace, monkeypatch optimizer to return a `BLOCKED` result with blockers, invoke `cmd_synthesize` with `json=True`, and assert return `EXIT_BLOCKED`, outcome `aborted`, JSON blockers include the stub blocker id, and `~/.metacrucible/evidence/<run_id>/receipt.json`, `summary.json`, and `trajectory-digest.json` exist with receipt `run_type == 'synthesize_evaluation_stage'`.
- [ ] Define `SYNTHESIZE_EVALUATION_BLOCKED_BUNDLE_RUN_TYPE = 'synthesize_evaluation_stage'` and `SYNTHESIZE_BLOCKED_BUNDLE_RUN_ID_PREFIX = 'synthesize'`.
- [ ] Implement `_write_synthesize_blocked_bundle(blockers: list[dict[str, object]]) -> dict[str, str]` using `UserGlobalStorage` and `write_blocked_bundle`. Follow the same best-effort behavior as `_write_optimize_blocked_bundle`: if bundle writing raises, print an English diagnostic to stderr and keep the in-memory payload authoritative.
- [ ] Call `_write_synthesize_blocked_bundle` only for evaluation-stage aborted/BLOCKED outcomes after the optimizer stage has been reached. Do not write a blocked bundle for input validation, missing spec, empty spec, or ordinary pending-review draft creation.
- [ ] Add returned evidence refs from the bundle writer to the synthesize payload when available.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest tests/test_synthesize_command.py -k "blocked_bundle" -v`

Expected:
- selected tests pass, 0 failed

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 1/3
Quality repair rounds: 1/3
Commit: e471b1fa4e2ec0b91bbc6d7401c45e4e49bf5e23

## Task 5: Full synthesize command integration and regression gate

### Goal
Prove the completed synthesize command satisfies all PRD F4 acceptance criteria through the public command test file and does not regress existing init/optimize command behavior touched by parser and shared helper changes.

### Files

Allowed to edit:
- `src/metacrucible/__main__.py` — final parser/dispatcher fixes only if integration tests expose a synthesize dispatch regression.
- `src/metacrucible/synthesize.py` — final synthesize behavior fixes only if integration tests expose a PRD F4 failure.
- `tests/test_synthesize_command.py` — final test corrections only for assertions that conflict with implemented PRD F4 behavior.

Allowed to create:
- (none)

Read-only context:
- `tests/test_init_command.py` — parser/init regression surface.
- `tests/test_optimize_command.py` — optimize parser and wrapper regression surface.
- `docs/prd.md` and `docs/adr/0035-pin-mvp-cli-surface-and-operational-behavior.md` — final acceptance mapping.

### Steps
- [ ] Confirm production code and tests contain no `synthesize-not-implemented` blocker id or message.
- [ ] Confirm every command in `tests/test_synthesize_command.py` uses the worktree venv command path when it shells out; prefer direct function calls for command behavior tests.
- [ ] Run the synthesize command test file and fix only failures inside the allowed files.
- [ ] Run focused parser/command regressions from init and optimize that cover touched dispatch surfaces.
- [ ] If regressions require changing existing test expectations, stop and request Plan revision instead of weakening tests.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest tests/test_synthesize_command.py -v`
- `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest tests/test_init_command.py tests/test_optimize_command.py -k "parser or subcommand_is_recognized or missing_workspace_argparse" -v`

Expected:
- all listed tests pass, 0 failed
- no production placeholder blockers remain for implemented `synthesize`

### Status
Status: pass
Spec review: pass
Quality review: pass
Spec repair rounds: 0/3
Quality repair rounds: 0/3
Commit: b50bd71f01fe2e63e31b911884ae647071f73b07

## Finalization

Final global review: pass
Final verification: pass
Integration fix:
Status: pass
Commit: a558d8d8972f472210ac7e8a6fff8166efcff319
Affected tasks: 1, 2, 3, 4, 5 (integration fix; re-spec-reviewed)
Review exemption: integration repair touches Task 1 (parser help + flag dests) and Task 3 (run_synthesis_optimizer signature) acceptance-critical files; re-run spec on Tasks 1 and 3 + global review after repair
Run Record: ready-for-pr
Finish decision: open-pr
PR URL: https://github.com/Cunning-Kang/MetaCrucible/pull/56

## Final verification commands
- `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest tests/test_synthesize_command.py -v`
- `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest tests/test_init_command.py tests/test_optimize_command.py -k "parser or subcommand_is_recognized or missing_workspace_argparse" -v`

## Resume notes
- Start with Task 1.
- Use only named SDD roles for execution and review: implementer, spec-reviewer, code-quality-reviewer.
- Run tests with `/Users/cunning/Workspaces/heavy/.sdd-worktrees/MetaCrucible/issue-41-metacrucible-synthesize/.venv/bin/python -m pytest`, not bare `python3`.
- Reviews may need inline diffs if `.py` reads are gated in this worktree.
