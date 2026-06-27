# Plan: PyPI release process (MetaCrucible issue #48)

## Source
Type: issue
Ref: #48 — https://github.com/cunninglab/MetaCrucible/issues/48
Issue close mode: auto-on-merge

## Repo
Origin: https://github.com/cunninglab/MetaCrucible.git
Primary root: /Users/cunning/Workspace/repos/cunninglab/MetaCrucible
Repo key: cunninglab/MetaCrucible

## Pipeline status
Status: implementing
Branch: sdd/issue-48-pypi-release-process
Workspace: /Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release
Workspace root: n/a
Workspace strategy: workspace-policy
Workspace owner: sdd
Workspace cleanup: user-confirmation-required
Workspace isolation proof: git-linked-worktree
Workspace created by SDD: yes
Workspace creation proof: git-worktree-add-success
Workspace collision status: none
Workspace reason: Host workspace policy from global CLAUDE.md places external workspaces under /Users/cunning/Workspace/worktrees/<repo-or-org>/<repo>/<task-or-branch-slug>/; isolated git-linked worktree created from origin/main for source-id issue-48.
Base ref: origin/main
Base: 68c4a5239e11bd2393a01939f1ff5bd465fa4475

## Goal
Add a Mise-routed, secret-free PyPI build + release-gate toolchain to MetaCrucible: a `build` task that produces wheel+sdist without bundling dev-only paths, a version+changelog release gate that refuses malformed releases, a Trusted-Publishing (OIDC) GitHub Actions release workflow gated behind tag push / manual dispatch, and documentation pinning the release process — all deterministic, none of which call a live LLM.

## Acceptance criteria
- [ ] Dry-run build works: a `mise run build` task runs `uv build --wheel --sdist` into a temp/`dist` location and a test pins that both a `metacrucible-<version>-py3-none-any.whl` and a `metacrucible-<version>.tar.gz` are produced; the wheel excludes `.sdd/`, `tests/`, fixtures, `.venv/`, and secrets.
- [ ] Release does not call live LLM: the `build` task and release gate run with no provider API key and no live-LLM SDK call; the release workflow contains no `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `PYPI_API_TOKEN` reference and no live-LLM marker, pinned by test.
- [ ] Version + changelog gate exists: `scripts/release_gate.py` exits non-zero when `pyproject.toml` `version` is a placeholder, when `CHANGELOG.md` has no matching `## [<version>]` section, or (when `--check-tag` is passed) when the `v<version>` git tag is absent; unit tests pin all branches.
- [ ] Trusted Publishing/OIDC is considered or documented: `.github/workflows/release.yml` uses `pypa/gh-action-pypi-publish` with `permissions: id-token: write`, no `password:`/API-token secret, triggered on `push: tags: ['v*']` and `workflow_dispatch`; a "Releasing" section in `CONTRIBUTING.md` records the choice; tests pin the OIDC + trigger contract.

## Assumptions
- Hatchling is the PEP 517 backend (already declared in `pyproject.toml` `[build-system]`); the wheel target is pinned to `packages = ["src/metacrucible"]`, so the wheel already excludes `tests/` and `.sdd/` from the *package*, but sdist inclusion must be asserted by test because hatchling sdist defaults include repo files.
- The existing `tests/test_packaging_skeleton.py::test_wheel_build_succeeds` already pins `uv build --wheel` end-to-end and must keep passing; this plan extends build coverage (adds sdist + exclusion assertions) rather than duplicating it.
- The repo has no `scripts/` directory today; creating `scripts/release_gate.py` is consistent with a standalone, non-imported CLI helper (no change to the `metacrucible` package import surface, no `[project.scripts]` entry).
- PyPI Trusted Publishing (OIDC) is the documented target; a real upload is NOT executed by any test or by CI on every push — the publish step is gated behind tag push / `workflow_dispatch` only.
- All verification runs inside the recorded-worktree `.venv` created by `mise install`; venv-relative Python paths are stable because `mise.toml` pins `_.python.venv = { path = ".venv", create = true }`.
- The build path is deterministic and secret-free: no `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` is read, and no provider LLM call is made during build, gate, or workflow definition inspection.

## File structure
- `mise.toml` — add `[tasks.build]` running `uv build --wheel --sdist`; add `[tasks.release-gate]` running the gate script. Modified in Task 1 (build) and Task 2 (gate).
- `tests/test_build_task.py` — new test file pinning the Mise `build` task declaration, wheel+sdist artifacts, and wheel exclusion of `.sdd/`, `tests/`, `.venv/`. Created in Task 1.
- `scripts/release_gate.py` — new standalone CLI gate: validates `pyproject.toml` version against `CHANGELOG.md` section and (optionally) the `v<version>` git tag. Created in Task 2.
- `tests/test_release_gate.py` — new test file pinning every gate branch via `tmp_path` repo fixtures (no real tags/PyPI). Created in Task 2.
- `.github/workflows/release.yml` — new GitHub Actions workflow using `pypa/gh-action-pypi-publish` with OIDC, triggered on `push: tags: ['v*']` + `workflow_dispatch`. Created in Task 3.
- `tests/test_release_workflow.py` — new test file reading `release.yml` as text, pinning OIDC usage, no API token, trigger shape, no live-LLM markers. Created in Task 3.
- `CHANGELOG.md` — extend `[Unreleased]` with the release-tooling note. Modified in Task 4.
- `CONTRIBUTING.md` — add a "Releasing" section documenting Mise build, the gate, Trusted Publishing, and the tag convention. Modified in Task 4.
- `tests/test_release_docs.py` — new test file pinning the "Releasing" section presence and the CHANGELOG release-tooling entry. Created in Task 4.

## Task 1: Add `build` Mise task + wheel/sdist artifact test

**Files:**
- Modify: `mise.toml` (append `[tasks.build]` after `[tasks.install]`)
- Create: `tests/test_build_task.py`

**Interfaces:**
- Consumes: `pyproject.toml` `[build-system]` (hatchling), `[tool.hatch.build.targets.wheel]` `packages = ["src/metacrucible"]`, `project.version`.
- Produces: `mise run build` invoking `uv build --wheel --sdist` (Task 3's release workflow and Task 5's integration both call this task by name); `tests/test_build_task.py` covers AC1 and the AC2 wheel-exclusion half.

**Checkpoint:**
- Run: `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_build_task.py -v`
- Expected: all tests in `tests/test_build_task.py` PASS, including a wheel+sdist artifact assertion and a wheel exclusion assertion.

### Status
Status: pass
Task review: pass
Spec verdict: pass
Quality verdict: pass
Task review rounds: 0/3
Task review report: .sdd/work/issue-48-pypi-release/task-001/task-review-report.json
Task base: b938ab4dcb9b576503527295ee1cd05b471b1f80
Task head: 6762d2b95da31165a0b75b30808c0eb86d14d57d
Work commits: 6762d2b95da31165a0b75b30808c0eb86d14d57d
Accepted range: b938ab4dcb9b576503527295ee1cd05b471b1f80..6762d2b95da31165a0b75b30808c0eb86d14d57d

### Dispatch facts
Implementer allowed edit scope:
- `mise.toml` — append the `[tasks.build]` task definition described below
Implementer allowed create scope:
- `tests/test_build_task.py` — pin the `build` Mise task and the produced wheel/sdist artifacts
Implementer required read context:
- `mise.toml` — match the existing `[tasks.*]` TOML shape and the header comment block explaining Mise as the single toolchain source of truth
- `pyproject.toml` — confirm `version = "0.1.0"`, `[build-system]` hatchling, and `[tool.hatch.build.targets.wheel] packages = ["src/metacrucible"]`
- `tests/test_packaging_skeleton.py` — match the `uv build --wheel` subprocess pattern and the `REPO_ROOT`/`tmp_path` style; do NOT duplicate `test_wheel_build_succeeds`, extend coverage instead
- `tests/test_mise_toolchain.py` — match the `_load_mise_toml()`/`tomllib` parsing helpers and string-assertion style
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_build_task.py -v`
- `mise run build` (controller re-runs in the worktree to confirm the task exists and produces both artifacts)
Expected results:
- Every test in `tests/test_build_task.py` passes.
- `mise run build` produces both `metacrucible-0.1.0-py3-none-any.whl` and `metacrucible-0.1.0.tar.gz` under the build output directory.
- The wheel's namelist contains no path under `tests/`, `.sdd/`, `.venv/`, or `fixtures/`, and no entry whose name contains `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `.env`.
Known reviewer focus:
- The `build` task must run `uv build --wheel --sdist` (both targets), not `--wheel` only.
- The wheel exclusion test must read the actual wheel namelist (e.g. `zipfile.ZipFile(...).namelist()`), not assert on build log text.
- The sdist assertion must confirm the `.tar.gz` filename encodes the project version.
- `mise.toml` task must use the same TOML style as the existing `[tasks.test]` block (quoted `run` string) and must not introduce a competing env manager.
Non-goals:
- Do not add a PyPI publish step or any network upload.
- Do not add `release.yml` (Task 3).
- Do not add the release gate (Task 2).
- Do not modify `pyproject.toml` build target configuration; the wheel already excludes `tests/` via `packages = ["src/metacrucible"]` — this task asserts that fact, it does not change it.

---

## Task 2: Add version+changelog release gate script + unit tests

**Files:**
- Create: `scripts/release_gate.py`
- Create: `tests/test_release_gate.py`
- Modify: `mise.toml` (append `[tasks.release-gate]` after `[tasks.build]`)

**Interfaces:**
- Consumes: `pyproject.toml` `[project] version`; `CHANGELOG.md` `## [<version>]` sections; optionally `git tag --list "v<version>"`.
- Produces: `scripts/release_gate.py` with `main(argv=None) -> int` (0 = pass, non-zero = fail) and a parseable error message on stderr; `mise run release-gate` invokes it against the worktree root. Task 3's release workflow calls `mise run release-gate` before publish; Task 5's integration runs it as a dry-run.

**Checkpoint:**
- Run: `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_release_gate.py -v`
- Expected: all tests PASS, covering the placeholder-version rejection, the missing-changelog-section rejection, the missing-tag rejection (`--check-tag`), and the all-green pass path.

### Status
Status: pending
Task review: pending
Spec verdict: pending
Quality verdict: pending
Task review rounds: 0/3
Task review report: none
Task base: none
Task head: none
Work commits: none
Accepted range: none

### Dispatch facts
Implementer allowed edit scope:
- `mise.toml` — append `[tasks.release-gate]` described below
Implementer allowed create scope:
- `scripts/release_gate.py` — the release gate CLI
- `tests/test_release_gate.py` — unit tests covering every gate branch
Implementer required read context:
- `pyproject.toml` — read the real `version = "0.1.0"` to seed the pass-path fixture
- `CHANGELOG.md` — read the real `[Unreleased]` section to understand the `## [<version>]` heading shape the gate must match
- `tests/test_packaging_skeleton.py` — match the `_load_pyproject()`/`tomllib` helpers and `REPO_ROOT` pattern
- `src/metacrucible/__init__.py` — confirm `__version__ = "0.1.0"` so the gate does not need to import the package (parse `pyproject.toml` only)
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_release_gate.py -v`
- `mise run release-gate` (controller re-runs in the worktree to confirm the task is wired and the gate fails loudly against the current `0.1.0` + `[Unreleased]`-only state, OR passes if the implementer adds the matching changelog section for 0.1.0)
Expected results:
- All `tests/test_release_gate.py` tests pass.
- The gate returns non-zero with a clear stderr message when version is a placeholder (`0.0.0`, `0.0`, `Unreleased`, empty), when `CHANGELOG.md` has no `## [<version>]` section, and (with `--check-tag`) when `git tag --list "v<version>"` returns nothing.
- The gate returns 0 when all checks pass against a `tmp_path` fixture repo.
Known reviewer focus:
- The gate must parse `pyproject.toml` with `tomllib` (stdlib only — no new runtime dependency).
- Tag check must be opt-in via `--check-tag` so unit tests do not require a real repo with tags; the default invocation validates version + changelog only.
- Placeholder detection must treat `0.0.0`, `0.0`, `Unreleased`, and empty string as invalid; it must treat `0.1.0` as valid.
- Changelog matching must use a `## [<version>]` heading regex (Keep a Changelog shape), not a free-text substring match.
- The script must exit non-zero on failure (return code, not just print) and write the reason to stderr.
- `mise.toml` `[tasks.release-gate]` must use the same TOML style as `[tasks.test]` and must not introduce a competing env manager.
Non-goals:
- Do not add a PyPI publish step.
- Do not create the `v0.1.0` git tag (the gate's tag check is unit-tested against a `tmp_path` repo, never against the real worktree tags).
- Do not modify `CHANGELOG.md` content (Task 4 owns the release-tooling note); the gate *reads* the changelog, it does not edit it.
- Do not import `metacrucible` from the gate script; parse `pyproject.toml` directly.

---

## Task 3: Add Trusted-Publishing (OIDC) release workflow + test

**Files:**
- Create: `.github/workflows/release.yml`
- Create: `tests/test_release_workflow.py`

**Interfaces:**
- Consumes: `mise run install`, `mise run test`, `mise run build` (Task 1), `mise run release-gate` (Task 2), `pypa/gh-action-pypi-publish` (GitHub-hosted action).
- Produces: `.github/workflows/release.yml` triggered on `push: tags: ['v*']` and `workflow_dispatch`, that runs the suite + build + gate, then publishes via OIDC Trusted Publishing with `permissions: id-token: write` and no `password:`/`PYPI_API_TOKEN`. Task 5's integration asserts this workflow is secret-free.

**Checkpoint:**
- Run: `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_release_workflow.py -v`
- Expected: all tests PASS, pinning the trigger shape, OIDC permission, absence of API tokens / live-LLM markers, and presence of `pypa/gh-action-pypi-publish`.

### Status
Status: pending
Task review: pending
Spec verdict: pending
Quality verdict: pending
Task review rounds: 0/3
Task review report: none
Task base: none
Task head: none
Work commits: none
Accepted range: none

### Dispatch facts
Implementer allowed edit scope:
- none (no existing file in this task is edited)
Implementer allowed create scope:
- `.github/workflows/release.yml` — the Trusted-Publishing release workflow
- `tests/test_release_workflow.py` — text-based pinning of the release workflow contract
Implementer required read context:
- `.github/workflows/ci.yml` — match the Mise action pin (`jdx/mise-action@v2`, `mise-version: 2026.6.14`), the `mise install` / `mise run install` / `mise run test` step shape, and the public-fork-safe comment style
- `tests/test_ci_workflow.py` — reuse the `_read_workflow()`/`_run_lines()` helpers and the string-assertion style (NO YAML library import — keep the pytest-only dependency set)
- `mise.toml` — confirm the `build` and `release-gate` task names added in Tasks 1 and 2 so the workflow invokes them by exact name
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_release_workflow.py -v`
Expected results:
- All `tests/test_release_workflow.py` tests pass.
- The workflow text contains `pypa/gh-action-pypi-publish`, `permissions: id-token: write`, `on: push: tags: ['v*']` (or equivalent), and `workflow_dispatch`.
- The workflow text contains none of: `password:`, `PYPI_API_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `secrets.` (no repository secret references).
Known reviewer focus:
- The publish job MUST be gated behind `push: tags: ['v*']` and/or `workflow_dispatch` — it must NOT run on every push to `main` or on pull_request.
- OIDC must use `permissions: id-token: write` and the `pypa/gh-action-pypi-publish` action; no `password:` field, no `PYPI_API_TOKEN` secret, no long-lived API token.
- The workflow must run the full `mise run test` (not just `test-replay`) before building, to keep parity with `ci.yml`'s suite coverage on the release path.
- The workflow must invoke `mise run build` and `mise run release-gate` by exact name before publish.
- Tests must stay string-only (no `pyyaml` import) to preserve the pytest-only runtime dependency set established by `tests/test_ci_workflow.py`.
- The workflow must contain no provider-secret references and no live-LLM markers, so it stays public-fork safe on the build+verify path.
Non-goals:
- Do not execute a real PyPI upload from any test or from CI on every push.
- Do not add `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or any provider key.
- Do not modify `ci.yml` (the release workflow is a separate, tag-gated file).
- Do not add repository secrets or a `secrets.` reference.

---

## Task 4: Document the release process (CHANGELOG + CONTRIBUTING)

**Files:**
- Modify: `CHANGELOG.md` (extend `[Unreleased]` ### Added with the release-tooling note)
- Modify: `CONTRIBUTING.md` (add a "Releasing" section after "Developer commands")
- Create: `tests/test_release_docs.py`

**Interfaces:**
- Consumes: the `build`, `release-gate` Mise tasks (Tasks 1, 2) and the Trusted-Publishing workflow (Task 3) as the documented procedure.
- Produces: a "Releasing" section in `CONTRIBUTING.md` recording Mise build, the gate, Trusted Publishing, and the `v*` tag convention; a CHANGELOG `[Unreleased]` entry; and `tests/test_release_docs.py` pinning both.

**Checkpoint:**
- Run: `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_release_docs.py -v`
- Expected: all tests PASS, confirming the CHANGELOG release-tooling entry and the CONTRIBUTING "Releasing" section both exist and reference the Mise tasks and Trusted Publishing.

### Status
Status: pending
Task review: pending
Spec verdict: pending
Quality verdict: pending
Task review rounds: 0/3
Task review report: none
Task base: none
Task head: none
Work commits: none
Accepted range: none

### Dispatch facts
Implementer allowed edit scope:
- `CHANGELOG.md` — append the release-tooling note under the existing `[Unreleased]` ### Added list
- `CONTRIBUTING.md` — add a new "Releasing" section
Implementer allowed create scope:
- `tests/test_release_docs.py` — pin the presence and content of the release-tooling CHANGELOG entry and the "Releasing" section
Implementer required read context:
- `CHANGELOG.md` — match the existing Keep a Changelog `[Unreleased]` ### Added bullet style and the SemVer/Keep a Changelog header lines
- `CONTRIBUTING.md` — match the existing section heading style and the "Developer commands" table so the new "Releasing" section is consistent; match the `docs/adr/` cross-reference style used in "Further reading"
- `docs/adr/0036-pin-project-metadata-policy.md` — CHANGELOG must follow Keep a Changelog + SemVer (already established); the release note must describe a user-visible process change
- `mise.toml` — reference the exact task names `build` and `release-gate` in the docs
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_release_docs.py -v`
Expected results:
- All `tests/test_release_docs.py` tests pass.
- `CHANGELOG.md` `[Unreleased]` ### Added contains a bullet describing the release tooling: the `build` and `release-gate` Mise tasks and the Trusted-Publishing release workflow.
- `CONTRIBUTING.md` contains a `## Releasing` section that names `mise run build`, `mise run release-gate`, Trusted Publishing (OIDC), and the `v*` tag convention.
Known reviewer focus:
- The CHANGELOG entry must live under `[Unreleased]` ### Added (Keep a Changelog shape), not a fabricated version section — the gate (Task 2) determines release readiness; do not pre-create a `## [0.1.0]` section here unless the gate's pass-path already requires it.
- The "Releasing" section must NOT instruct the reader to set `PYPI_API_TOKEN` or any provider API key; it must document Trusted Publishing (OIDC) as the publish mechanism.
- The docs must reference the exact task names (`mise run build`, `mise run release-gate`) and the `v*` tag convention used by `release.yml`.
- Tests must stay string-only (read the two files as text, assert substrings) to match the repo's pytest-only style.
Non-goals:
- Do not create the `v0.1.0` git tag.
- Do not add a `## [0.1.0]` section to `CHANGELOG.md` (that is a release activity, out of scope for this issue's tooling work; the gate validates the section exists *at release time*).
- Do not modify `README.md` (ADR 0036 README scope is install/quickstart/concepts/safety/reference; the release process belongs in CONTRIBUTING).
- Do not add an ADR (the release process is documented in CONTRIBUTING + CHANGELOG; an ADR is not required by any acceptance criterion).

---

## Task 5: Final integration + regression gate (verification-only)

**Files:**
- none (verification-only task; no production/test/doc edits)

**Interfaces:**
- Consumes: all deliverables from Tasks 1–4.
- Produces: an end-to-end verification pass proving all four acceptance criteria hold together: full `mise run test`, `mise run build` dry-run, `mise run release-gate` dry-run, and the release-workflow safety assertions (no provider keys, no API token).

**Checkpoint:**
- Run, in order, inside the worktree:
  1. `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest -m 'not local_real'`
  2. `mise run build`
  3. `mise run release-gate` (expected to fail loudly against the current `0.1.0` + `[Unreleased]`-only state, OR pass if a `## [0.1.0]` section was added — either outcome is acceptable as long as the gate returns a clear exit code and message)
- Expected: the full non-local-real suite passes; `mise run build` produces wheel+sdist; `mise run release-gate` exits with a deterministic code and a human-readable message; `tests/test_release_workflow.py` confirms `release.yml` has no provider keys and no API token.

### Status
Status: pending
Task review: pending
Spec verdict: pending
Quality verdict: pending
Task review rounds: 0/3
Task review report: none
Task base: none
Task head: none
Work commits: none
Accepted range: none

### Dispatch facts
Implementer allowed edit scope:
- none (this is a verification-only task; if a regression is found, raise it as a blocker rather than editing accepted-task files)
Implementer allowed create scope:
- none
Implementer required read context:
- `.github/workflows/release.yml` — confirm the OIDC + trigger contract assembled in Task 3
- `mise.toml` — confirm `build` and `release-gate` tasks assembled in Tasks 1 and 2
- `CHANGELOG.md` and `CONTRIBUTING.md` — confirm the release docs assembled in Task 4
Verification owner: both
Verification commands:
- `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest -m 'not local_real'`
- `mise run build`
- `mise run release-gate`
- `/Users/cunning/Workspace/worktrees/cunninglab/MetaCrucible/issue-48-pypi-release/.venv/bin/python -m pytest tests/test_release_workflow.py tests/test_build_task.py tests/test_release_gate.py tests/test_release_docs.py -v`
Expected results:
- Full non-local-real pytest suite passes (no regressions in `test_packaging_skeleton.py`, `test_mise_toolchain.py`, `test_ci_workflow.py`).
- `mise run build` produces both `metacrucible-0.1.0-py3-none-any.whl` and `metacrucible-0.1.0.tar.gz`.
- `mise run release-gate` exits deterministically with a clear message (pass or fail both acceptable; the point is the gate is wired and behaves).
- `tests/test_release_workflow.py` asserts `release.yml` contains no `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`PYPI_API_TOKEN`/`password:`/`secrets.` reference.
Known reviewer focus:
- This task MUST NOT edit accepted-task files; its only output is evidence that the assembled toolchain satisfies all four acceptance criteria together.
- Confirm no regression in the pre-existing `tests/test_packaging_skeleton.py::test_wheel_build_succeeds` (the Task 1 build test extends, not replaces, it).
- Confirm `release.yml` stays off the every-push path (trigger is tag/dispatch only).
- Confirm the build path never references a provider API key or makes a live-LLM call.
Non-goals:
- Do not create the `v0.1.0` git tag.
- Do not execute a real PyPI upload.
- Do not edit production, test, or doc files (out of scope; raise a blocker if a regression is found).

## Finalization

Final code review: pending
Final code review report: none
Final verification: pending
Integration fix:
  Status: none
  Base: none
  Head: none
  Work commits: none
  Affected tasks: none
Run Record: pending
Finish decision: pending
PR URL: none
