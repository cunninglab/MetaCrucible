# Pin workspace masking and boundary reporting

MetaCrucible prepares target execution with a copy-on-write workspace algorithm: first build a masked prepared workspace from the canonical source and reviewed support files, then create an independent per-case workspace and overlay reviewed fixtures after masking. Workspace masking, temporary home directories, and sanitized environments are the MVP enforcement mechanism for read boundaries, and runtime-level gaps are reported explicitly rather than silently downgraded.

**Consequences**

- Prepared workspaces do not copy `.git`, `.metacrucible`, evidence or cache directories, default-denied hidden files, database files, key material, environment files, dependency caches, or files matching secret deny rules.
- Secret-like content is categorized by a built-in high-confidence pattern library, redacted or removed before target execution, and may be exempted only by reviewed, scoped, hash-bound fake/test fixture exceptions that never contain plaintext real secrets.
- Synthetic fixtures are overlaid only after base masking, cannot overwrite canonical source unless explicitly reviewed, cannot write into denied secret paths, and may simulate secrets only with reviewed fake-secret fixtures.
- Dotfiles and hidden support files are not copied by default; explicit reviewed support-file allowlists may include them, but deny rules always win and `SKILL.md` remains the required canonical Skill file.
- Claude Code read-path limits are reported as warnings when approximated by workspace masking; if a case declares strict read-path enforcement, the Claude Code adapter blocks with an unsupported strict-read-path result.