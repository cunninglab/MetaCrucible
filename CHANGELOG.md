# Changelog

All notable changes to MetaCrucible are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project metadata four-piece set being established per ADR 0036: MIT `LICENSE`,
  this `CHANGELOG`, `README`, and `CONTRIBUTING` guide (issue #47).
- PyPI release tooling: a `build` Mise task producing wheel + sdist via
  `uv build --wheel --sdist`; a `release-gate` Mise task validating
  `pyproject.toml` `[project].version` against a matching `## [<version>]`
  section in `CHANGELOG.md` (and optionally the `v<version>` git tag); and
  a Trusted-Publishing (OIDC) GitHub Actions release workflow publishing
  on `v*` tag push or manual `workflow_dispatch` (issue #48).