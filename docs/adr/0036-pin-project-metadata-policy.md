# Pin project metadata policy

MetaCrucible uses MIT as the intended project license, Keep a Changelog with SemVer for release notes, a README focused on install, quickstart, concepts, safety, and reference links, and a CONTRIBUTING guide that makes pure logic tests, recorded replay tests, and opt-in real Claude Code smoke tests explicit.

**Consequences**

- The GitHub repository license should move from Apache-2.0 to MIT when repository metadata is updated.
- `CHANGELOG.md` follows Keep a Changelog with an `Unreleased` section and records user-visible CLI, schema, behavior, and migration changes.
- The README must state the product definition, experimental status, install command, quickstart commands, core concepts, safety model, docs links, reference project roles, and MIT license.
- CONTRIBUTING must document Python environment setup, expected commands such as pytest, Ruff, and mypy when available, and the distinction between unit, replay, and local-real tests.
- Schema changes require fixtures and migration notes, optimizer changes require replay coverage, adapter changes require local-real smoke evidence before release, and fixtures must not contain real secrets.