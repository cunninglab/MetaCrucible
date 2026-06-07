# Pin receipt and evidence bundle v1 schema

MetaCrucible receipt v1 is the stable entrypoint for an evidence bundle and stores identities, hashes, status, blockers, and relative bundle references rather than raw local paths. Evidence bundle hashes use canonical bytes or canonical JSON scopes that exclude volatile local paths and timestamps, and summaries provide an aggregate view while per-round receipts remain the immutable evidence entrypoints for optimization rounds.

**Consequences**

- `receipt.json` records run type, status, artifact, envelope, benchmark, executable benchmark, evaluation harness, optimizer harness, runtime adapter, model identities, summary reference, case result references, event log references, and blockers.
- `benchmark_sha` identifies the reviewed benchmark file as provided, while `executable_benchmark_sha` identifies the eligible reviewed cases after masking and split selection.
- `summary.json` contains aggregate status, counts, split summaries, weakest dimensions, accepted or best revision ids, blockers, warnings, cost summary, and duration, but not raw events, full model outputs, raw local paths, or held-out evidence fed to the optimizer.
- Fresh run receipts may reference cached case results only when artifact, executable case, harness, adapter or runtime version, model identities, and execution boundary identity all match; `--fresh` disables cache reads and `--no-cache` disables reads and writes.
- Repository-local state keeps lightweight history and receipt indexes, user-global state keeps evidence bundles and cache, redacted raw evidence is retained by default for 30 days, and cleanup commands prune raw evidence or cache without deleting receipts, summaries, or trajectory digests.