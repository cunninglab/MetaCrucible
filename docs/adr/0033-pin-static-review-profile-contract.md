# Pin static review profile contract

MetaCrucible ships four MVP built-in Static Review Profiles: runtime neutrality, routing-surface safety, secret/privacy risk, and Darwin skill quality. Safety and evidence profiles are hard-coded when their triggers apply, Darwin quality remains a supplemental review layer by default, and every profile identity, version, content hash, and configuration hash participates in evaluation harness identity.

**Consequences**

- `secret-privacy-risk.v1` runs for all runs, `routing-surface-safety.v1` runs when routing is touched, and held-out leakage prevention remains a hard optimizer/evaluation rule rather than a configurable profile.
- `darwin-skill-quality.v1` runs by default for review and can contribute auxiliary optimization signals, but it does not become a blocking acceptance gate unless project policy configures a threshold.
- Profile results contain rule results, optional LLM judgments, blockers, and top-level `PASS`, `FAIL`, or `BLOCKED`; hard rule failures fail the profile, ambiguous rules use required fallback when available, and unresolved required ambiguity blocks.
- `portability.target` is a portability claim that controls runtime-neutrality checks, not the runtime adapter selector; `claude_code`, `oh_my_pi`, `shared_claude_layout`, and `runtime_neutral` claims use progressively different language checks.
- Built-in profile versions, custom profile content hashes, threshold/config hashes, and disabled configurable-profile state are included in `evaluation_harness_sha`, while hard-coded safety profiles cannot be disabled.