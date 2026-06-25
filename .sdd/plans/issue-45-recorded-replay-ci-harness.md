# Plan: Recorded replay CI harness

## Source
Type: issue
Status: implementing
Issue close mode: auto-on-merge

## Repo
Origin: https://github.com/cunninglab/MetaCrucible.git
Primary root: /Users/cunning/Workspace/repos/cunninglab/MetaCrucible
Repo key: cunninglab/MetaCrucible

## Pipeline status
Status: planning
Branch: sdd/issue-45-recorded-replay-ci-harness
Workspace: /Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45
Workspace root: n/a
Workspace strategy: sdd-fallback
Workspace owner: sdd
Workspace cleanup: sdd-may-remove
Workspace isolation proof: git-linked-worktree
Workspace created by SDD: yes
Workspace creation proof: git-worktree-add-success
Workspace collision status: none
Workspace reason: sdd-fallback worktree under repo parent because no SDD-configured root or host-native strategy is provided
Base ref: origin/main
Base: 3f19628dbf0300b6bc4fef91a0589d3f30f03e19

## Goal
Add a recorded LLM replay harness that lets CI exercise MetaCrucible review, bootstrap, optimize, and synthesize flows without live provider calls or secrets. The implementation must keep local default CLI behavior unchanged unless a replay flag is supplied, define a stable replay fixture loader public surface, add CI that installs and runs through Mise, and document unit, replay, and local-real test layers per ADR 0021 and ADR 0036.

## Acceptance criteria
- [ ] CI uses Mise: `.github/workflows/ci.yml` uses `jdx/mise-action`, runs `mise install`, `mise run install`, `mise run test`, and `mise run test-replay`; `tests/test_ci_workflow.py` pins the Mise action and test commands.
- [ ] CI does not call live LLM: replay fixture loading rejects high-confidence secret patterns, CLI replay paths use deterministic callables, workflow tests assert no `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` environment references and no provider-secret env block is present.
- [ ] Recorded replay coverage includes review, bootstrap, optimize, and synthesize: `tests/test_replay_cli.py` exercises replay-backed results for all four commands and default no-replay behavior remains unchanged for review and optimize.

## Assumptions
- Issue blockers #29, #40, #41, and #44 are complete on `origin/main`; this Plan starts from base `3f19628dbf0300b6bc4fef91a0589d3f30f03e19`.
- Replay fixture schema version is introduced as a replay-specific stable string or integer inside `src/metacrucible/replay.py`; it must not alter ADR 0029 benchmark JSONL v1 or ADR 0030 receipt/evidence bundle v1 schemas.
- The replay module may reuse the high-confidence secret regex library from `src/metacrucible/profiles.py` even if that requires exposing a small public helper or importing the private tuple with tests pinning the behavior.
- CI may install packages from Mise and PyPI through the configured toolchain, but must not require provider API keys or call Anthropic, OpenAI, or other live LLM endpoints.
- Ruff is not declared in `pyproject.toml` at base; the CI lint step is conditional and must be omitted or skipped unless the implementer also adds a declared Ruff dev dependency in scope.

## Task 1: Add replay loader and callable builders

### Status
Status: pass
Task review: pass
Spec verdict: pass
Quality verdict: pass
Task review rounds: 0/3
Task review report: .sdd/work/issue-45-recorded-replay-ci-harness/task-001/task-review-report.json
Commit: 2a5dfd801a62adecf9ed81a50ebf3b5543d51f30

### Steps
- Define `src/metacrucible/replay.py` with public surface `load_replay(path: Path) -> Replay`, `build_judge_call_fns(replay: Replay) -> list[Callable]`, and `build_optimizer_call_fn(replay: Replay) -> Callable`.
- Define a stable JSONL fixture format: one JSON object per line, every record carrying `schema_version` and `name`, plus either `response` for a single call or `responses` for an ordered multi-call sequence. Reject missing or mismatched `schema_version`, duplicate names, malformed JSON, empty response queues, and records carrying both `response` and `responses`.
- Implement `Replay` so named entries are consumed in deterministic order and excess calls raise an assertion-style exception with the entry name and call index. Provide an explicit exhaustion assertion so tests and CLI can fail when a fixture leaves unexpected unused responses.
- Scan the raw fixture text and decoded string fields for high-confidence secret patterns from `profiles.py` (`aws-access-key-id`, `github-personal-access-token`, `stripe-live-secret-key`) and reject matching fixtures before any callable is returned.
- Make `build_judge_call_fns` return two distinct callables backed by judge entry names that tests pin, and make `build_optimizer_call_fn` return one callable consuming optimizer responses in order. Do not import provider clients or read API-key environment variables.
- Add `tests/test_replay_harness.py` for secret rejection, schema/version validation, round-trip loading, two distinct judge callables, ordered optimizer calls, excess-call failures, and unused-response assertion failures.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_replay_harness.py -v`
Expected:
- replay harness tests pass, including secret-pattern rejection and excess-call failure coverage
- no live provider modules or API-key environment variables are needed by the replay loader tests

### Dispatch facts
Implementer allowed edit scope:
- `src/metacrucible/profiles.py` — expose or reuse the high-confidence secret pattern library only if `replay.py` cannot safely import the existing constant without weakening encapsulation
Implementer allowed create scope:
- `src/metacrucible/replay.py` — stable replay fixture loader, replay state object, and callable builders
- `tests/test_replay_harness.py` — pure replay loader and callable-builder tests
Implementer required read context:
- `src/metacrucible/profiles.py` — existing `secret-privacy-risk` high-confidence regex pattern source
- `src/metacrucible/provider_config.py` — callable response shape consumed by `call_structured` and `run_judge_evaluator`
- `src/metacrucible/optimizer.py` — `run_optimizer_pipeline` `call_fn` contract and `call_fn=None` behavior
- `docs/adr/0021-test-with-real-llms-locally-and-recorded-replays-in-ci.md` — recorded replay CI intent
- `docs/adr/0036-pin-project-metadata-policy.md` — fixture secret prohibition
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_replay_harness.py -v`
Expected results:
- `tests/test_replay_harness.py` passes with secret rejection, schema pinning, ordered responses, exhaustion, and distinct judge callable assertions
Known reviewer focus:
- Confirm replay fixture scanning cannot leak real secrets and does not silently allow unused or excess recorded responses.
- Confirm public replay surface is small and does not change behavior for callers that do not import it.
Non-goals:
- Do not add live provider configuration, network calls, or provider SDK dependencies.
- Do not change benchmark JSONL v1 or receipt/evidence bundle v1 schemas.
## Task 2: Wire replay flags through review, bootstrap, optimize, and synthesize

### Status
Status: pass
Task review: pass
Spec verdict: pass
Quality verdict: pass
Task review rounds: 0/3
Task review report: .sdd/work/issue-45-recorded-replay-ci-harness/task-002/task-review-report.json
Commit: d036f70ee38a7cf694ef035a8ab229ab7f050437

### Steps
- Add parser help and dispatch support for replay inputs on `review`, `bootstrap`, `optimize`, and `synthesize`. `review` must accept `--replay PATH` and the compatibility aliases `--judge-replay PATH` and `--judge-replay-2 PATH`; `optimize`, `bootstrap`, and `synthesize` must accept `--replay PATH`.
- For `review`, thread replay-backed judge callables into the F1 judgment path so benchmark cases with `judgment` can produce recorded judge verdicts. Default no-replay behavior must still return BLOCKED with `review-case-judge-provider-unavailable` for judgment cases.
- For `optimize`, pass `build_optimizer_call_fn(load_replay(path))` as `call_fn` to both preview and mutating `run_optimizer_pipeline` invocations when `--replay` is present; keep `call_fn=None` when no replay is supplied. Preserve existing `--max-rounds`, resume, dirty-file, and routing-confirmation behavior.
- For `bootstrap`, use replay responses only to replace deterministic draft payloads where the command currently emits generated cases; fixture-backed generated records must still carry `status: generated` and `BOOTSTRAP_PENDING_REVIEW` so human-review gates remain intact.
- For `synthesize`, pass replay through to the optimizer invocation in `run_synthesize_command` / `run_synthesis_optimizer` so a recorded ACCEPTED optimizer response can yield `outcome: accepted`; default no-replay behavior remains unchanged.
- Add `tests/test_replay_cli.py` covering parser help and behavior: review replay returns recorded judge verdicts, bootstrap replay writes generated cases with recorded payloads, optimize replay with recorded ACCEPTED response yields ACCEPTED acceptance decision, synthesize replay with recorded ACCEPTED optimizer response yields `outcome: accepted`, no-replay review remains BLOCKED with `review-case-judge-provider-unavailable`, and no-replay optimize remains REJECTED with the existing no-LLM-call rationale.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_replay_cli.py -v`
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_review_command.py tests/test_optimize_command.py -k "judgment_unavailable or clean_benchmark_enters_pipeline or stop_reason_in_cli_json_payload" -v`
Expected:
- replay CLI tests pass for review, bootstrap, optimize, and synthesize
- focused default-behavior regressions pass, proving no-replay paths are unchanged

### Dispatch facts
Implementer allowed edit scope:
- `src/metacrucible/__main__.py` — parser flags, review/bootstrap/optimize replay dispatch, default behavior preservation
- `src/metacrucible/synthesize.py` — synthesize replay parameter threading into optimizer invocation
- `src/metacrucible/replay.py` — small builder adjustments only if CLI call signatures expose a mismatch
- `tests/test_review_command.py` — focused compatibility assertions only if existing helper setup needs replay-aware optional fields
- `tests/test_optimize_command.py` — focused compatibility assertions only if existing helper namespace needs replay-aware optional fields
Implementer allowed create scope:
- `tests/test_replay_cli.py` — end-to-end CLI replay tests for review, bootstrap, optimize, synthesize, and no-replay defaults
Implementer required read context:
- `src/metacrucible/__main__.py` — `cmd_review`, `_evaluate_single_case`, `_evaluate_case_with_judgment`, `_stub_judge_call`, `cmd_bootstrap`, `cmd_optimize`, `_build_parser`
- `src/metacrucible/synthesize.py` — `run_synthesize_command` and `run_synthesis_optimizer` replay threading point
- `src/metacrucible/provider_config.py` — `run_judge_evaluator` two-callable contract and structured call expectations
- `src/metacrucible/optimizer.py` — `run_optimizer_pipeline` call_fn contract and `OptimizerPipelineResult` payload fields
- `tests/test_review_command.py` — subprocess/helper pattern and `review-case-judge-provider-unavailable` assertions
- `tests/test_optimize_command.py` — monkeypatch pattern around `run_optimizer_pipeline` and exit-code assertions
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_replay_cli.py -v`
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_review_command.py tests/test_optimize_command.py -k "judgment_unavailable or clean_benchmark_enters_pipeline or stop_reason_in_cli_json_payload" -v`
Expected results:
- `tests/test_replay_cli.py` passes and covers all four issue #45 command families
- no-replay review and optimize focused regression tests pass with existing blockers and no-op rationale intact
Known reviewer focus:
- Confirm replay flags do not become hidden default provider configuration and do not trigger live provider calls.
- Confirm synthesize and optimize use the same replay call_fn consistently across preview and apply paths when replay is supplied.
Non-goals:
- Do not add real LLM provider setup or API-key configuration.
- Do not weaken dirty-file, resume, routing-confirmation, or pending-review gates.

## Task 3: Add Mise-based CI workflow and workflow pinning tests

### Status
Status: pass
Task review: pass
Spec verdict: pass
Quality verdict: pass
Task review rounds: 0/3
Task review report: .sdd/work/issue-45-recorded-replay-ci-harness/task-003/task-review-report.json
Commit: 29a007694581df5d68e344cad855a542e70bbf02

### Steps
- Add `.github/workflows/ci.yml` for Linux x86_64 only. The workflow must check out code, install Python 3.14 through Mise using `jdx/mise-action`, run `mise install`, run `mise run install`, run `mise run test`, and run `mise run test-replay` or an equivalent `mise run ci` task that includes replay tests.
- Keep CI safe for public forks: do not require repository secrets, do not set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or provider-secret environment variables, and do not add provider SDK smoke commands.
- Cache only standard setup artifacts where reasonable; avoid custom cache key complexity beyond setup defaults.
- Add `tests/test_ci_workflow.py` that reads `.github/workflows/ci.yml` and asserts the workflow uses `jdx/mise-action`, includes `mise install`, `mise run install`, `mise run test`, and `mise run test-replay`, does not reference `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, and includes replay coverage markers for review, bootstrap, optimize, and synthesize via the replay test command or test file names.
- If `pyproject.toml` still lacks Ruff in dev dependencies, do not add a failing lint step. If Ruff is added in scope, tests must pin the declared dependency and workflow command together.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_ci_workflow.py tests/test_mise_toolchain.py -v`
Expected:
- workflow pinning tests pass and prove AC1 through Mise action and Mise commands
- workflow pinning tests prove AC2 by absence of provider secret references
- workflow pinning tests prove AC3 by requiring replay tests for review, bootstrap, optimize, and synthesize in CI

### Dispatch facts
Implementer allowed edit scope:
- `mise.toml` — add or reference a replay test task only when needed by CI command wiring
- `pyproject.toml` — add CI-only dev dependency only if required for workflow tests and justified by repository conventions
Implementer allowed create scope:
- `.github/workflows/ci.yml` — GitHub Actions workflow using Mise and replay tests without provider secrets
- `tests/test_ci_workflow.py` — tests pinning Mise usage, no live LLM secrets, and replay command coverage markers
Implementer required read context:
- `mise.toml` — canonical toolchain and task definitions from issue #1
- `tests/test_mise_toolchain.py` — pattern for testing Mise contract from repository files
- `pyproject.toml` — dev dependency source and Ruff availability decision
- `docs/adr/0021-test-with-real-llms-locally-and-recorded-replays-in-ci.md` — CI recorded replay requirement
- `docs/adr/0028-define-claude-code-adapter-contract.md` — local-real adapter tests versus CI replay fixture boundary
- `docs/adr/0036-pin-project-metadata-policy.md` — command documentation and fixture secret policy
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_ci_workflow.py tests/test_mise_toolchain.py -v`
Expected results:
- tests prove `.github/workflows/ci.yml` installs via `jdx/mise-action`, runs Mise tasks, contains no provider API-key references, and includes replay coverage for all four target commands
Known reviewer focus:
- Confirm CI cannot call live LLMs and can run on public forks without configured secrets.
- Confirm workflow uses Mise as canonical toolchain rather than direct bare `python`, `pip`, or `pytest` command steps.
Non-goals:
- Do not add macOS or Windows matrix jobs.
- Do not design complex cache keys or release automation.

## Task 4: Document test layers and add replay test task

### Status
Status: pass
Task review: pass
Spec verdict: pass
Quality verdict: pass
Task review rounds: 0/3
Task review report: .sdd/work/issue-45-recorded-replay-ci-harness/task-004/task-review-report.json
Commit: bba8e21222e33845e4cbed7b4e621bacf80b089d

### Steps
- Add `CONTRIBUTING.md` per ADR 0036. Document Python environment setup through Mise, `mise install`, `mise run install`, `mise run test`, and `mise run test-replay`.
- Document the three test layers: unit tests without LLM calls, recorded replay tests for CI, and opt-in local-real tests with real LLMs on developer machines only. State that replay fixtures must not contain real secrets and CI must not require provider API keys.
- Update `mise.toml` with `[tasks.test-replay]` running the replay-focused pytest subset. Use pytest markers if Task 2 adds `@pytest.mark.replay`; otherwise use explicit replay test file paths such as `tests/test_replay_harness.py tests/test_replay_cli.py tests/test_ci_workflow.py`.
- If pytest markers are introduced, add the marker declaration in `pyproject.toml` and keep `tests/test_ci_workflow.py` aligned with the command actually used by `mise run test-replay`.
- Add or extend tests so `mise run test-replay` is pinned by repository tests and CONTRIBUTING references the same command string.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_mise_toolchain.py tests/test_ci_workflow.py -v`
Expected:
- Mise tests pass with `test-replay` task present
- CI workflow tests pass and reference the same replay command documented for contributors
- `CONTRIBUTING.md` exists and documents unit, replay, and local-real layers without suggesting CI live LLM keys

### Dispatch facts
Implementer allowed edit scope:
- `mise.toml` — add `test-replay` task using project venv and pytest replay subset
- `pyproject.toml` — add pytest marker declaration only if replay tests use `@pytest.mark.replay`
- `tests/test_mise_toolchain.py` — pin the new `test-replay` task and command shape
- `tests/test_ci_workflow.py` — align workflow expectations with the final replay task name
Implementer allowed create scope:
- `CONTRIBUTING.md` — contributor guide documenting environment setup and unit/replay/local-real test layers
Implementer required read context:
- `docs/adr/0036-pin-project-metadata-policy.md` — CONTRIBUTING content requirements
- `docs/adr/0021-test-with-real-llms-locally-and-recorded-replays-in-ci.md` — recorded replay versus local-real distinction
- `docs/adr/0028-define-claude-code-adapter-contract.md` — adapter local-real evidence and CI replay fixture distinction
- `mise.toml` — current task syntax and install/test task pattern
- `tests/test_mise_toolchain.py` — TOML parsing and toolchain assertion style
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_mise_toolchain.py tests/test_ci_workflow.py -v`
Expected results:
- tests prove `mise.toml` exposes `test-replay`, workflow invokes it, and contributor docs name the correct commands and test-layer boundaries
Known reviewer focus:
- Confirm CONTRIBUTING does not imply real provider credentials are needed for CI.
- Confirm `test-replay` command covers replay tests and does not silently duplicate only pure unit tests.
Non-goals:
- Do not rewrite README, changelog, license, or existing pure-logic test files.
- Do not add release or PyPI process documentation.

## Task 5: Final integration and regression gate

### Status
Status: pending
Task review: pending
Spec verdict: pending
Quality verdict: pending
Task review rounds: 0/3
Task review report: none
Commit: none

### Steps
- Run the replay harness, replay CLI, CI workflow, Mise toolchain, and existing focused review/optimize regression tests together from the worktree venv.
- Run the full project test suite through the worktree venv to catch integration regressions from parser, command, and optimizer replay threading.
- Run a lightweight workflow safety check from pytest or a direct Python command that reads `.github/workflows/ci.yml` and asserts no `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `live-llm`, or provider-secret env markers are present.
- If failures are limited to files touched by Tasks 1 through 4, repair within those task scopes and rerun the failing command plus the final gate. If failures require changing acceptance semantics, stop for Plan revision rather than weakening tests.
- Leave one canonical commit per completed task; do not squash task commits during final integration.

### Verification
Discovery: no
Commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_replay_harness.py tests/test_replay_cli.py tests/test_ci_workflow.py tests/test_mise_toolchain.py -v`
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_review_command.py tests/test_optimize_command.py -k "judgment_unavailable or clean_benchmark_enters_pipeline or stop_reason_in_cli_json_payload" -v`
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest -v`
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -c "from pathlib import Path; text=Path('.github/workflows/ci.yml').read_text(encoding='utf-8'); forbidden=('ANTHROPIC_API_KEY','OPENAI_API_KEY','live-llm','provider-secret'); missing=[s for s in forbidden if s in text]; assert not missing, missing"`
Expected:
- focused replay, CI, Mise, review, and optimize tests pass
- full pytest suite passes
- workflow safety command exits 0 with no forbidden live-LLM or provider-secret markers

### Dispatch facts
Implementer allowed edit scope:
- `src/metacrucible/replay.py` — final integration fixes for replay loader or callable behavior exposed by full tests
- `src/metacrucible/__main__.py` — final integration fixes for replay CLI dispatch exposed by full tests
- `src/metacrucible/synthesize.py` — final integration fixes for synthesize replay threading exposed by full tests
- `mise.toml` — final replay task command correction exposed by CI/toolchain tests
- `tests/test_replay_harness.py` — assertion correction only if it conflicts with stable replay behavior already implemented
- `tests/test_replay_cli.py` — assertion correction only if it conflicts with stable CLI replay behavior already implemented
- `tests/test_ci_workflow.py` — assertion correction only if workflow safety requirement is still preserved
- `tests/test_mise_toolchain.py` — assertion correction only if Mise contract requirement is still preserved
Implementer allowed create scope:
- `.sdd/work/issue-45-recorded-replay-ci-harness/` — implementer report and final gate evidence only
Implementer required read context:
- `tests/test_replay_harness.py` — replay loader and callable-builder expected behavior
- `tests/test_replay_cli.py` — command-level replay acceptance coverage
- `tests/test_ci_workflow.py` — workflow safety and Mise assertions
- `tests/test_mise_toolchain.py` — toolchain task assertions
- `.github/workflows/ci.yml` — final CI workflow safety check target
- `mise.toml` — final replay task command source
Verification owner: controller
Verification commands:
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_replay_harness.py tests/test_replay_cli.py tests/test_ci_workflow.py tests/test_mise_toolchain.py -v`
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest tests/test_review_command.py tests/test_optimize_command.py -k "judgment_unavailable or clean_benchmark_enters_pipeline or stop_reason_in_cli_json_payload" -v`
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -m pytest -v`
- `/Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45/.venv/bin/python -c "from pathlib import Path; text=Path('.github/workflows/ci.yml').read_text(encoding='utf-8'); forbidden=('ANTHROPIC_API_KEY','OPENAI_API_KEY','live-llm','provider-secret'); missing=[s for s in forbidden if s in text]; assert not missing, missing"`
Expected results:
- every command exits 0
- final pytest run confirms no unrelated command regressions
- direct workflow safety check confirms no live-LLM or provider-secret markers remain
Known reviewer focus:
- Confirm final integration did not alter locked acceptance criteria or expand scope into live provider setup.
- Confirm final evidence maps AC1, AC2, and AC3 to runnable commands.
Non-goals:
- Do not commit final review, create PR, run GitHub Actions remotely, or add release automation.
- Do not broaden CI matrix or add real LLM smoke tests.

## Finalization

Final code review: pending
Final code review report: none
Final verification: pending
Integration fix:
  Status: none
  Commit: none
  Affected tasks: none
Run Record: pending
Finish decision: pending
PR URL: none
