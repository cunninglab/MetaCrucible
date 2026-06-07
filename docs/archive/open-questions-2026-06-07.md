# Archived open questions

Archived on 2026-06-07 after all entries were resolved into ADRs 0028-0036.

The active open-questions tracker was removed to keep ADRs as the source of truth.

## Resolution map

- Runtime adapter implementation details → `docs/adr/0028-define-claude-code-adapter-contract.md`
- Benchmark and case schema details → `docs/adr/0029-pin-benchmark-jsonl-v1-schema.md`
- Evidence bundle and receipt schema details → `docs/adr/0030-pin-receipt-and-evidence-bundle-v1-schema.md`
- Workspace isolation and redaction → `docs/adr/0031-pin-workspace-masking-and-boundary-reporting.md`
- Optimizer pipeline details → `docs/adr/0032-pin-optimizer-pipeline-contract.md`
- Static review profiles → `docs/adr/0033-pin-static-review-profile-contract.md`
- Control plane provider configuration → `docs/adr/0034-pin-control-plane-provider-configuration.md`
- CLI behavior and UX → `docs/adr/0035-pin-mvp-cli-surface-and-operational-behavior.md`
- Project metadata → `docs/adr/0036-pin-project-metadata-policy.md`
- Operational gaps → `docs/adr/0035-pin-mvp-cli-surface-and-operational-behavior.md`

## Original open questions

Areas not yet resolved after the MVP scoping and ADR sync conversations. Resolved decisions live in `docs/adr/` and `CONTEXT.md`; this file tracks remaining discussion entry points only.

### Runtime adapter implementation details

- How exactly should the Claude Code adapter verify that a materialized Skill was loaded successfully in `--bare` + `--add-dir` mode?
- Which `stream-json` event fields are required for MVP evidence, and which missing fields make a case `BLOCKED`?
- What exact `--allowedTools` strings should be generated from portable `execution_boundary` declarations?
- How should `target_commands` argv arrays be shell-escaped into Claude Code Bash permission strings?
- What empirical tests are needed to confirm `--bare`, `--add-dir`, `--agents`, `dontAsk`, and `stream-json` behavior?

### Benchmark and case schema details

- Exact v1 JSONL schema for benchmark metadata records and case records.
- Exact fields for `input`, `adapter_overrides`, `fixtures`, `execution_boundary`, `checks`, `check_boundary`, and `judgments`.
- Whether `evaluate --split all` should return `PASS` only when all reviewed cases pass, or allow split-specific result summaries with a top-level `FAIL`.
- Exact blocker codes for invalid benchmark states (`no_eval_cases`, `no_held_out_cases`, `no_reviewed_cases`, schema errors, duplicate ids).
- Migration strategy when `schema_version` changes.

### Evidence bundle and receipt schema details

- Exact `receipt.json`, `summary.json`, `trajectory-digest.json`, and JSONL event schemas.
- Canonical digest scopes for `artifact_sha`, `envelope_sha`, `benchmark_sha`, `executable_benchmark_sha`, `evaluation_harness_sha`, and `optimizer_harness_sha`.
- Which fields belong in `summary.json` versus per-round receipts.
- How per-case cache result references are represented inside a fresh run receipt.
- Retention policy for user-state evidence, cache, run indexes, and optional raw evidence.

### Workspace isolation and redaction

- Exact masking algorithm for benchmark files, evidence files, and secret files inside prepared and per-case workspaces.
- Secret-like pattern library and redaction exception schema.
- How synthetic fixtures are overlaid after secret masking.
- Whether hidden support files in Skill directories need additional allow/deny rules beyond the fixed ignore list.
- How to report boundary enforcement warnings when Claude Code cannot enforce `read_paths` exactly.

### Optimizer pipeline details

- Exact prompts and structured output schemas for reflection, edit suggestion, rank-and-clip, per-range merge, and generated-case suggestions.
- Whether failure/success reflection is one model call per case, per batch, or per round.
- How rejected-buffer summaries are bounded and injected into later rounds.
- How conflicts between selected edits are detected before LLM merge.
- Whether post-MVP slow update and persistent optimizer memory need reserved terminology or remain purely future ADR topics.

### Static review profiles

- Exact built-in profile definitions for `runtime-neutrality`, `routing-surface-safety`, `secret/privacy-risk`, and `darwin-skill-quality`.
- Which profile triggers are hard-coded versus project-configurable.
- How rule hard-fails, ambiguous rule hits, LLM fallback, and `BLOCKED` review results are encoded.
- How `portability.target` affects profile selection and runtime-specific language checks.
- How profile versions and custom profile content hashes enter `evaluation_harness_sha`.

### Control plane provider configuration

- Exact layered config schema for `control_plane`, `providers`, and `runtime_adapters`.
- How Anthropic and OpenAI-compatible structured-call adapters validate JSON output support.
- Whether user config may contain API keys directly or only `api_key_env` references.
- Usage/cost accounting schema across Anthropic and OpenAI-compatible providers.
- Provider capability error taxonomy and retry policy.

### CLI behavior and UX

- Exact behavior and output for `metacrucible init`, `baseline create`, `evaluate`, and `optimize`.
- Which commands emit minimal `BLOCKED` evidence bundles.
- Whether `init` should offer interactive mutable range setup after creating minimal envelope and empty benchmark.
- Exact flags: `--fresh`, `--no-cache`, `--diagnose`, `--apply`, `--allow-routing-revision`, `--allow-dirty-unrelated`, `--isolated-bypass`, `--target-model`, `--edit-budget`, `--context-lines`.
- How CLI should surface local raw paths while receipts store only hashes/categories.

### Project metadata

- LICENSE choice (MIT likely, but not yet decided).
- CHANGELOG policy (Keep a Changelog? per-package?).
- README minimum content (install, quickstart, links to ADRs and reference projects).
- CONTRIBUTING policy and test discipline.

### Operational gaps

- Logging and audit format outside evidence bundles.
- Error reporting and exit codes for the CLI.
- Internationalization of CLI output (English vs Chinese).
- How users discover which runtime an artifact targets when paths are ambiguous.
