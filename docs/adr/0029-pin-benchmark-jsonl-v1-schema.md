# Pin benchmark JSONL v1 schema

MetaCrucible benchmarks use a single JSONL file with exactly one first-line metadata record followed by case records. Case records describe target input and judgment methods, not execution results, and only reviewed cases are eligible for baseline, evaluation, and optimization; generated cases remain pending until a human review promotes them, and disabled cases are ignored by eligibility checks.

**Consequences**

- Benchmark v1 contains one `metadata` record and many `case` records with unique `case_id`, `status` of `reviewed`, `generated`, or `disabled`, and `split` of `eval` or `held_out`.
- Each eligible case must provide input, execution boundary, and at least one deterministic check or non-deterministic judgment; judgments must name rubric and pass condition rather than relying on bare scores.
- `evaluate --split all` returns top-level `PASS` only when every eligible reviewed eval and held-out case passes; any blocked case blocks the result, and any failed case fails the result when no blockers exist.
- Invalid benchmark blocker codes are a fixed small machine-stable set, including schema, metadata, duplicate id, missing reviewed eval or held-out cases, pending generated cases, invalid checks or judgments, invalid execution boundary, and fixture boundary failures.
- Schema changes are never applied implicitly during evaluation or optimization; incompatible or newer versions block, while explicit migration may be provided as a dry-run-first command.