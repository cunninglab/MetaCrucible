# Define Claude Code adapter contract

MetaCrucible uses Adapter Preflight to verify that a materialized Claude Code Skill or injected subagent is discoverable before execution evaluation begins. The Claude Code adapter runs in `--bare` mode with `--add-dir` for Skills and `--agents` for subagents, uses a fixed one-line preflight sentinel instead of free-form model behavior, treats successful discovery as separate from whether the target later uses the artifact, and records only the minimum `stream-json` evidence needed to classify each run.

**Consequences**

- Skill preflight prompts must ask for exactly `METACRUCIBLE_SKILL_DISCOVERABLE=<yes|no>; NAME=<resolved-name-or-empty>`, and missing or mismatched sentinel output blocks preflight.
- MVP run evidence requires start/completion, exit code, final output, stderr or error diagnostics, raw event count, adapter version, and Claude Code version; usage and tool-call details are recorded when present but missing values are warnings rather than blockers.
- Portable `execution_boundary.target_commands` map only to exact Claude Code `Bash(...)` allow strings in the MVP; unsupported tools, broad wildcards, unsafe shell metacharacters, path traversal, or strict read-path enforcement requirements block the run.
- `target_commands` are accepted only as conservative argv arrays and normalized into display commands; complex shell behavior must be moved into reviewed wrapper files.
- Local empirical adapter tests must cover Skill discovery, subagent injection, exact allowed-tools behavior, noninteractive permission handling, and `stream-json` parser robustness, while CI uses recorded replay fixtures.