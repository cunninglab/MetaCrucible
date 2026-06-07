# Pin MVP CLI surface and operational behavior

MetaCrucible's MVP CLI keeps the PRD commands as the public user surface and exposes `init`, `baseline create`, and `evaluate` as support commands. Commands that create baseline, evaluation, or optimization facts write minimal blocked evidence bundles when they cannot proceed, while purely initializing or inspecting state reports normal CLI errors; human output may show local paths, but machine evidence stores only hashes, categories, and relative bundle references.

**Consequences**

- Public commands are `review`, `bootstrap`, `optimize`, `synthesize`, and `inspect`; support commands are `init`, `baseline create`, and `evaluate`.
- `baseline create`, `evaluate`, `optimize`, evaluation-stage `synthesize`, and execution-requested `review` emit minimal `BLOCKED` bundles when blocked; `init`, `inspect`, and ordinary `bootstrap` failures do not.
- `init` is noninteractive by default, creates minimal envelope and benchmark skeletons, records safe mutable ranges when detectable, and uses `--interactive` for guided mutable-range setup.
- MVP flags have stable meanings for cache freshness, diagnostics, apply behavior, routing revisions, dirty unrelated files, target model, edit budget, context lines, and the high-risk `--no-isolation` bypass that requires explicit confirmation.
- CLI human output is English, exit codes are a small stable automation contract, optional JSONL logs are not evidence sources of truth, and runtime adapter ambiguity blocks noninteractive runs unless the envelope or `--runtime-adapter` resolves it.