# Pin optimizer pipeline contract

MetaCrucible implements the MVP optimizer as a SkillOpt-shaped pipeline with per-case reflections, per-round synthesis, bounded edit suggestions, rank-and-clip selection, deterministic conflict checks, and one merged revision per mutable range. Optimizer context is built from eval-split evidence only, rejected suggestions are summarized into bounded guidance, and post-MVP slow updates or persistent optimizer memory do not receive MVP domain terms or schema slots.

**Consequences**

- Failed or weak eval cases produce `case_reflection` records, rounds produce `round_reflection` records, and edit generation outputs `edit_suggestion`, `ranked_edit_set`, `range_merge_plan`, and optional `generated_case_suggestion` records.
- Rejected edit buffers are injected into later rounds only as bounded theme summaries with reasons and avoid guidance, never as raw held-out evidence or unbounded rejected edits.
- Selected edits are checked before merge for overlapping ranges, routing-surface confirmation, contradictory intent, budget violations, unsupported ranges, and stale base hashes.
- Routing revisions remain capped at one selected edit and require explicit confirmation before they can enter a candidate revision.
- Same-range non-conflicting suggestions are merged by an LLM into one Patch Revision that must stay inside the mutable range; merge attempts outside the mutable range block the round.