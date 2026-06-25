# Run Record — Issue #45 Recorded replay CI harness

## Source

- Type: issue
- Ref: #45 + https://github.com/cunninglab/MetaCrucible/issues/45
- Issue close mode: auto-on-merge
- Blocked by: #29, #40, #41, #44 (all complete on `origin/main`)

## Repository

- Origin: https://github.com/cunninglab/MetaCrucible.git
- Primary root: /Users/cunning/Workspace/repos/cunninglab/MetaCrucible
- Repo key: cunninglab/MetaCrucible
- Base ref: origin/main
- Base SHA: 3f19628dbf0300b6bc4fef91a0589d3f30f03e19

## Workspace

- Workspace: /Users/cunning/Workspace/repos/cunninglab/MetaCrucible-issue-45
- Branch: sdd/issue-45-recorded-replay-ci-harness
- Workspace strategy: sdd-fallback (worktree under repo parent)
- Workspace owner: sdd
- Workspace cleanup: sdd-may-remove
- Workspace isolation proof: git-linked-worktree
- Workspace created by SDD: yes
- Workspace creation proof: git-worktree-add-success
- Workspace collision status: none

## Tasks and commits

| # | Task title | Status | Commit |
| - | --- | --- | --- |
| 1 | Add replay loader and callable builders | pass | 2a5dfd801a62adecf9ed81a50ebf3b5543d51f30 |
| 2 | Wire replay flags through review, bootstrap, optimize, and synthesize | pass | d036f70ee38a7cf694ef035a8ab229ab7f050437 |
| 3 | Add Mise-based CI workflow and workflow pinning tests | pass | 29a007694581df5d68e344cad855a542e70bbf02 |
| 4 | Document test layers and add replay test task | pass | bba8e21222e33845e4cbed7b4e621bacf80b089d |
| 5 | Final integration and regression gate | pass | (no implementation commit; controller-only verification) |

Per-task review reports:
- .sdd/work/issue-45-recorded-replay-ci-harness/task-001/task-review-report.json
- .sdd/work/issue-45-recorded-replay-ci-harness/task-002/task-review-report.json
- .sdd/work/issue-45-recorded-replay-ci-harness/task-003/task-review-report.json
- .sdd/work/issue-45-recorded-replay-ci-harness/task-004/task-review-report.json
- (Task 5 is verification-only; no review report.)

## Final review summary

Final code review: pass.
Report: .sdd/work/issue-45-recorded-replay-ci-harness/final/code-review-report.json
No blocking findings. One non-blocking polish item (G1) noted: the bootstrap replay branch that consumes a real "bootstrap" entry to swap draft input is not directly exercised by a happy-path test; only the no-bootstrap-entry fallback path is covered. The covered case proves the sentinel + status=generated contract. Not required for merge.

## Final verification

- Focused replay/CI/mise subset (`tests/test_replay_harness.py` + `tests/test_replay_cli.py` + `tests/test_ci_workflow.py` + `tests/test_mise_toolchain.py`): 64 passed, 0 failed.
- Review/optimize focused regression (judgment_unavailable + clean_benchmark_enters_pipeline + stop_reason_in_cli_json_payload): 3 passed, 73 deselected, 0 failed.
- Direct workflow safety check (no `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `live-llm` / `provider-secret` markers in `.github/workflows/ci.yml`): exits 0.
- Full pytest suite: 1359 passed, 3 skipped, 0 failed.

## Acceptance criteria mapping (issue #45)

- [x] CI uses Mise: `.github/workflows/ci.yml` uses `jdx/mise-action@v2` (mise-version 2026.6.14), runs `mise install`, `mise run install`, `mise run test`, `mise run test-replay`. Pinned by `tests/test_ci_workflow.py` (17 cases) and `tests/test_mise_toolchain.py` (16 cases including the new test-replay task pins).
- [x] CI does not call live LLM: replay module (`src/metacrucible/replay.py`) scans fixture text and decoded string fields against `aws-access-key-id`, `github-personal-access-token`, `stripe-live-secret-key` regexes (byte-identical to `src/metacrucible/profiles.py`). Workflow has no `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `live-llm`, or `provider-secret` markers. Pinned by `tests/test_ci_workflow.py` and the direct safety check command.
- [x] Covers review, bootstrap, optimize, and synthesize: `tests/test_replay_cli.py` has 10 CLI replay tests (review judge callables, review judge-replay aliases, review no-replay BLOCKED, bootstrap replay passthrough, optimize replay threads call_fn to both pipeline calls, optimize accepted yields ACCEPTED, synthesize accepted yields `outcome: accepted`, synthesize no-replay default unchanged, optimize no-replay remains REJECTED, parser help includes `--replay`). Plus 21 replay harness tests, 17 CI workflow tests, and 16 mise toolchain tests. CONTRIBUTING.md documents the three test layers and the no-live-LLM policy.

## Finish decision

- Decision: open-pr
- PR URL: pending (push blocked — see below)
- Status: complete (5 task commits, all reviews pass, final review pass, final verification pass)
- Working tree side effects: none (only `.sdd/work/` controller artifacts are untracked; not part of the branch)

## Blocker on remote push

The branch `sdd/issue-45-recorded-replay-ci-harness` exists locally with all 10 commits (5 task commits + 5 plan updates + 1 finalization update). The push to `origin` is blocked by GitHub:

```
! [remote rejected] sdd/issue-45-recorded-replay-ci-harness -> sdd/issue-45-recorded-replay-ci-harness
(refusing to allow an OAuth App to create or update workflow `.github/workflows/ci.yml` without `workflow` scope)
```

The available GitHub OAuth token (via `gh auth token`) has scopes `admin:public_key`, `gist`, `read:org`, `repo` but not `workflow`. The `workflow` scope is required by GitHub to push files under `.github/workflows/` for security reasons. The SSH route is also unavailable in this environment (connection closed by 198.18.0.20).

The branch is fully ready; the push needs to be done by a human or by a token that has the `workflow` scope. Two safe paths:

1. The user grants the `workflow` scope to their GitHub OAuth token (or uses a personal access token with `workflow`) and pushes the branch.
2. The user uses the GitHub web UI to create a PR from the local branch after uploading it via `git bundle` or similar.

The controller has not committed reviewer side effects and has not bypassed the GitHub security guard. The local branch HEAD is `00c288b plan: mark finalization ready-for-pr (#45)`.
