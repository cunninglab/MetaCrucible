# MetaCrucible

## Commands

- Setup tools/env: `mise install`
- Install package + dev deps: `mise run install`
- Run tests: `mise run test`
- Build wheel dry-run: `uv build --wheel`

## Toolchain

`mise.toml` is the source of truth for tool versions and tasks. It provisions Python 3.14 and `.venv`; do not add another root environment manager without rationale.

## Project shape

- Python package: `src/metacrucible/`
- CLI entry: `metacrucible = "metacrucible.__main__:main"`
- Tests: `tests/`
- Domain glossary: `CONTEXT.md`
- Decisions: `docs/adr/`

Before domain/design work, read `CONTEXT.md` and relevant ADRs. Surface ADR conflicts instead of silently overriding.

## Agent skills

### Issue tracker

GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical roles (`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`) match the label strings 1:1. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
