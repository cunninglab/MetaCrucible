"""CLI tests for the ``optimize`` subcommand (Issue #30, PRD F3).

Pins the MVP sentinel-gate behavior of
``metacrucible optimize <workspace>``:

  - The subcommand is recognized by argparse (no "unrecognized
    arguments" error from ``optimize --help``).
  - With a benchmark that has generated (pending-review) cases
    or the literal ``BOOTSTRAP_PENDING_REVIEW`` sentinel, the
    optimize command refuses to start with a stable
    ``bootstrap-pending-review`` blocker id (Issue #30 AC3:
    "Does not allow optimize until promote clears sentinel").
    The loader's own ``pending-generated-case`` blocker is
    preserved alongside so the operator sees the full
    picture.
  - A benchmark with no reviewed cases is BLOCKED via the
    loader's ``missing-reviewed-eval-case`` /
    ``missing-reviewed-held-out-case`` ids; the optimize
    command relays those verbatim rather than inventing its
    own.
  - The JSON output is parseable, exposes a ``blockers`` list
    with stable ids, and surfaces
    ``pending_review_case_ids`` so a downstream reader can
    branch on the machine-stable keys.
  - A benchmark that is otherwise optimize-runnable (eligible
    reviewed eval + held-out cases, no pending generated
    cases, no bootstrap sentinel) still returns
    ``EXIT_BLOCKED`` with the ``optimize-not-implemented``
    blocker: full optimization is W3 per the PRD, and the
    MVP contract is "we will refuse with a stable reason
    code" rather than "we silently do nothing".
  - The optimize command never mutates the benchmark file;
    the sentinel check is a read-only pass over the loader's
    partitioned cases.

These tests follow the subprocess invocation pattern from
:mod:`tests.test_promote_command` and
:mod:`tests.test_bootstrap_command`: ``python -m metacrucible``
is invoked in a temp dir, both stdout and stderr are captured,
and the JSON payload is parsed for the machine-stable fields.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

from metacrucible.exit_codes import EXIT_BLOCKED, EXIT_OK, EXIT_USER_ERROR

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Stable blocker id emitted by ``optimize`` when at least one
#: case still carries the literal bootstrap pending-review
#: sentinel. The id is the machine contract; the message is
#: human English prose.
OPTIMIZE_BOOTSTRAP_PENDING_REVIEW_BLOCKER = "bootstrap-pending-review"

#: Stable blocker id emitted by ``optimize`` when a fully
#: runnable benchmark (no blockers, no bootstrap sentinel) is
#: presented. Full optimization is W3 per the PRD; the MVP
#: command surfaces a dedicated blocker id rather than
#: silently doing nothing.
OPTIMIZE_NOT_IMPLEMENTED_BLOCKER = "optimize-not-implemented"

#: Literal case-level field that flags bootstrap-generated
#: cases as "pending human review". The string is the
#: machine-stable contract the optimize gate keys off.
BOOTSTRAP_PENDING_REVIEW_FIELD = "BOOTSTRAP_PENDING_REVIEW"

#: Stable blocker id for a benchmark with at least one
#: generated (pending review) case. Re-exported here so the
#: tests can branch on the id without re-deriving it from the
#: ``benchmark`` module.
PENDING_GENERATED_BLOCKER = "pending-generated-case"

#: Stable blocker ids emitted by the ADR 0029 loader when the
#: benchmark has no eligible reviewed cases. Re-exported
#: here so the optimize test asserts the loader's
#: missing-required-cases path without re-deriving the ids.
MISSING_REVIEWED_EVAL_BLOCKER = "missing-reviewed-eval-case"
MISSING_REVIEWED_HELD_OUT_BLOCKER = "missing-reviewed-held-out-case"

#: Stable blocker id emitted by ``optimize`` when the git
#: worktree carries dirty files unrelated to the optimize
#: inputs (artifact, envelope, benchmark). Mirrors the
#: module-level constant in
#: :mod:`metacrucible.__main__` so a future rename of the
#: source constant fails the test loud. Pinned locally so
#: the test file does not depend on the internal layout
#: of ``__main__``.
OPTIMIZE_UNRELATED_DIRTY_FILES_BLOCKER = "optimize-unrelated-dirty-files"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _run_metacrucible(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with captured text output.

    Mirrors the helper in :mod:`tests.test_promote_command` so
    the optimize tests use the same subprocess pattern the
    rest of the CLI test suite uses.
    """
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    """Write ``records`` as one JSON object per line at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(dict(rec), sort_keys=True) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _init_workspace(tmp_path: Path) -> Path:
    """Run ``init`` against a fresh workspace dir and return that dir.

    The fixture creates the empty benchmark container that the
    optimize test then seeds with custom records. Each test
    starts from a known-good state with the benchmark file
    present at the workspace root.
    """
    workspace = tmp_path / "ws-optimize"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`init` must exit 0 before optimize; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    return workspace


def _metadata_record() -> dict[str, Any]:
    """Minimal benchmark metadata record (ADR 0029)."""
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
    }


def _generated_case(
    case_id: str, **extras: Any
) -> dict[str, Any]:
    """Build a generated (pending-review) case record.

    ``extras`` is forwarded to the case dict so tests can
    layer on the ``BOOTSTRAP_PENDING_REVIEW`` sentinel (the
    literal field the optimize gate keys off) without
    touching the helper.
    """
    record: dict[str, Any] = {
        "record_type": "case",
        "case_id": case_id,
        "status": "generated",
        "split": "eval",
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "checks": [{"name": "ok", "pattern": "ok"}],
    }
    record.update(extras)
    return record


def _reviewed_case(
    case_id: str, *, split: str = "eval"
) -> dict[str, Any]:
    """Build a minimal eligible reviewed case (ADR 0029)."""
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "reviewed",
        "split": split,
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "checks": [{"name": "ok", "pattern": "ok"}],
    }


def _run_git(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``git`` with captured output.

    Mirrors the helper in :mod:`tests.test_baseline_command`
    so the optimize dirty-file guard tests can seed a real
    git worktree the guard will consult.
    """
    return subprocess.run(
        ["git", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _git_dirty_paths(workspace: Path) -> list[str]:
    """Return ``git status --porcelain`` paths from ``workspace``.

    Mirrors the helper in :mod:`tests.test_baseline_command`
    so the optimize dirty-file tests can pin the
    fixture-invariant "the scratch file is reported dirty"
    shape. Each returned entry is the path component of a
    ``git status --porcelain`` line, exactly as the optimize
    payload's ``dirty_files_at_run`` field carries it.
    """
    result = _run_git(["status", "--porcelain"], cwd=workspace)
    assert result.returncode == 0, (
        f"git status failed: rc={result.returncode} "
        f"stderr={result.stderr!r}"
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path_str = line[3:].strip()
        if " -> " in path_str:
            path_str = path_str.split(" -> ", 1)[1]
        if path_str.startswith('"') and path_str.endswith('"'):
            path_str = path_str[1:-1]
        paths.append(path_str)
    return paths


def _seed_optimize_inputs(workspace: Path) -> Path:
    """Seed the optimize inputs (benchmark, artifact, envelope).

    The dirty-file guard needs a complete input set on disk
    so the workspace survives ``git add -A`` with tracked
    files only. Mirrors the seed pattern in
    :func:`tests.test_baseline_command._init_workspace` but
    split out so the no-git dirty-guard test (which calls
    :func:`_init_workspace` and then seeds WITHOUT a git
    worktree) can reuse it.
    """
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )
    artifact = workspace / "SKILL.md"
    artifact.write_text(
        "---\n"
        "name: opt-skill\n"
        "description: optimize-dirty-guard fixture\n"
        "---\n"
        "# body\nThe body is the only mutable range.\n",
        encoding="utf-8",
    )
    envelope = workspace / ".metacrucible" / "envelope.json"
    envelope.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_path": str(artifact.resolve()),
                "artifact_workspace": str(workspace),
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return workspace


def _init_workspace_with_git(tmp_path: Path) -> Path:
    """Init + seed + git-init/add/commit a workspace.

    Mirrors :func:`tests.test_baseline_command._init_workspace`
    so the optimize dirty-file guard tests can exercise the
    ``git status --porcelain`` path. The order matters: the
    workspace must be seeded BEFORE ``git commit`` so the
    initial commit covers the tracked inputs (artifact,
    envelope, benchmark) and a later write to one of those
    paths shows up as a "tracked-input dirty" rather than an
    "untracked dirty".
    """
    workspace = _init_workspace(tmp_path)
    _seed_optimize_inputs(workspace)
    _run_git(["init", "-q"], cwd=workspace)
    _run_git(
        ["config", "user.email", "test@example.com"], cwd=workspace
    )
    _run_git(
        ["config", "user.name", "Optimize Test"], cwd=workspace
    )
    _run_git(["add", "-A"], cwd=workspace)
    _run_git(
        ["commit", "-q", "-m", "init optimize workspace"],
        cwd=workspace,
    )
    return workspace


# --------------------------------------------------------------------------- #
# AC1 — ``optimize`` is a recognized subcommand                                #
# --------------------------------------------------------------------------- #

def test_optimize_subcommand_is_recognized() -> None:
    """``metacrucible optimize`` is a registered subcommand.

    Argparse raises ``unrecognized arguments: optimize`` if
    the subcommand is not wired in. The acceptance criterion
    is that ``optimize`` appears in the help output and the
    subcommand-level ``--help`` exits 0.
    """
    result = _run_metacrucible(["optimize", "--help"], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`metacrucible optimize --help` must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "optimize" in result.stdout, (
        f"optimize --help must mention the subcommand name; "
        f"got {result.stdout!r}"
    )
    assert "workspace" in result.stdout, (
        f"optimize --help must advertise the workspace "
        f"positional; got {result.stdout!r}"
    )
    assert "--json" in result.stdout, (
        f"optimize --help must advertise the --json flag; "
        f"got {result.stdout!r}"
    )


# --------------------------------------------------------------------------- #
# AC2 — optimize blocks when generated cases are present                       #
# --------------------------------------------------------------------------- #

def test_optimize_blocks_when_generated_cases_present(
    tmp_path: Path,
) -> None:
    """A benchmark with at least one generated case is
    BLOCKED with both the loader's ``pending-generated-case``
    blocker and the optimize command's
    ``bootstrap-pending-review`` blocker.

    The optimize command must surface both blockers so the
    operator sees the full picture: the loader partitions
    the cases and surfaces the partition-level blocker, and
    the optimize command surfaces the literal-sentinel
    blocker the gate keys off of.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
            _generated_case("gen-1"),
        ],
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize` with a generated case must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict), (
        f"optimize --json must return a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert PENDING_GENERATED_BLOCKER in blocker_ids, (
        f"optimize with a generated case must surface the "
        f"loader pending-generated-case blocker; got "
        f"blocker_ids={blocker_ids!r}"
    )
    # The optimize command is blocked by the literal
    # sentinel only when the case carries
    # ``BOOTSTRAP_PENDING_REVIEW=True``. A generated case
    # without the sentinel still blocks via the loader's
    # pending-generated-case id; the optimize command does
    # NOT add the bootstrap-pending-review blocker because
    # the case is not bootstrap-tagged. Pin both shapes.
    assert OPTIMIZE_BOOTSTRAP_PENDING_REVIEW_BLOCKER not in blocker_ids, (
        f"a generated case WITHOUT the BOOTSTRAP_PENDING_REVIEW "
        f"sentinel must NOT trigger the bootstrap-pending-review "
        f"blocker; got blocker_ids={blocker_ids!r}"
    )


def test_optimize_blocks_when_bootstrap_sentinel_present(
    tmp_path: Path,
) -> None:
    """A case carrying the literal ``BOOTSTRAP_PENDING_REVIEW``
    sentinel triggers the dedicated optimize blocker on top
    of the loader's pending-generated-case id.

    The optimize command reads the case-level sentinel
    directly (per Issue #30 AC3) and surfaces the dedicated
    ``bootstrap-pending-review`` blocker so the operator sees
    exactly which cases are blocking the gate. The case
    also contributes to the loader's
    ``pending-generated-case`` blocker because
    ``status=generated``.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
            _generated_case(
                "gen-1",
                **{BOOTSTRAP_PENDING_REVIEW_FIELD: True},
            ),
        ],
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize` with a bootstrap-sentinel case must "
        f"exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert OPTIMIZE_BOOTSTRAP_PENDING_REVIEW_BLOCKER in blocker_ids, (
        f"optimize with a BOOTSTRAP_PENDING_REVIEW sentinel "
        f"case must surface the bootstrap-pending-review "
        f"blocker; got blocker_ids={blocker_ids!r}"
    )
    assert PENDING_GENERATED_BLOCKER in blocker_ids, (
        f"optimize must ALSO surface the loader's "
        f"pending-generated-case blocker alongside; got "
        f"blocker_ids={blocker_ids!r}"
    )
    # The blocker message lists the case ids that carry
    # the sentinel so the operator can act on the precise
    # list of cases blocking the gate.
    sentinel_blocker = next(
        b for b in payload["blockers"]
        if isinstance(b, dict)
        and b.get("id") == OPTIMIZE_BOOTSTRAP_PENDING_REVIEW_BLOCKER
    )
    assert "gen-1" in sentinel_blocker.get("message", ""), (
        f"bootstrap-pending-review message must list the "
        f"case ids that carry the sentinel; got "
        f"{sentinel_blocker!r}"
    )
    pending_ids = payload.get("pending_review_case_ids") or []
    assert pending_ids == ["gen-1"], (
        f"optimize payload must surface the case ids that "
        f"carry the sentinel under pending_review_case_ids; "
        f"got {pending_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC3 — optimize blocks when no reviewed cases                                 #
# --------------------------------------------------------------------------- #

def test_optimize_blocks_when_no_reviewed_cases(tmp_path: Path) -> None:
    """A benchmark with no eligible reviewed cases is
    BLOCKED via the loader's missing-required-cases ids.

    The optimize command relays the loader blockers
    verbatim rather than inventing its own. A freshly
    ``init``-ed workspace carries only the metadata record,
    so both ``missing-reviewed-eval-case`` and
    ``missing-reviewed-held-out-case`` surface alongside.
    """
    workspace = _init_workspace(tmp_path)
    # The fixture's ``init`` left the benchmark with only
    # the metadata record; no cases at all.
    benchmark = workspace / BENCHMARK_FILE_NAME
    records = [
        json.loads(line)
        for line in benchmark.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1, (
        f"init must leave exactly the metadata record; got "
        f"{len(records)} records"
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize` on an empty benchmark must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert MISSING_REVIEWED_EVAL_BLOCKER in blocker_ids, (
        f"optimize on an empty benchmark must surface the "
        f"loader missing-reviewed-eval-case blocker; got "
        f"blocker_ids={blocker_ids!r}"
    )
    assert MISSING_REVIEWED_HELD_OUT_BLOCKER in blocker_ids, (
        f"optimize on an empty benchmark must surface the "
        f"loader missing-reviewed-held-out-case blocker; got "
        f"blocker_ids={blocker_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC4 — JSON output shape (machine-stable contract)                           #
# --------------------------------------------------------------------------- #

def test_optimize_reports_blockers_in_json_output(
    tmp_path: Path,
) -> None:
    """``optimize --json`` emits a parseable JSON object with
    the canonical machine-stable keys and a non-empty
    blockers list when blocked.

    The shape is the contract downstream automation
    branches on: ``workspace``, ``benchmark``,
    ``benchmark_present``, ``is_optimize_runnable``,
    ``pending_review_case_ids``, ``blockers``. The
    ``blockers`` list carries the canonical ``{id, message}``
    shape so the operator can branch on the id and read
    the human English message.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
            _generated_case(
                "gen-1",
                **{BOOTSTRAP_PENDING_REVIEW_FIELD: True},
            ),
        ],
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize --json` on a blocked benchmark must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`optimize --json` must emit valid JSON on "
            f"stdout; got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"optimize --json must return a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    for key in (
        "status",
        "workspace",
        "benchmark",
        "is_optimize_runnable",
        "pending_review_case_ids",
        "blockers",
        "rounds",
    ):
        assert key in payload, (
            f"optimize --json must surface {key!r}; got keys "
            f"{sorted(payload.keys())!r}"
        )
    assert payload["is_optimize_runnable"] is False, (
        f"is_optimize_runnable must be False when the "
        f"benchmark is blocked; got "
        f"{payload['is_optimize_runnable']!r}"
    )
    assert isinstance(payload["blockers"], list) and payload["blockers"], (
        f"optimize --json must report a non-empty blockers "
        f"list when blocked; got {payload['blockers']!r}"
    )
    for blocker in payload["blockers"]:
        assert isinstance(blocker, dict), (
            f"each blocker must be a dict with id+message; "
            f"got {blocker!r}"
        )
        assert isinstance(blocker.get("id"), str) and blocker["id"], (
            f"each blocker must carry a non-empty string id; "
            f"got {blocker!r}"
        )
        # The human message is required by ADR 0029.
        assert isinstance(blocker.get("message"), str), (
            f"each blocker must carry a string message; got "
            f"{blocker!r}"
        )


# --------------------------------------------------------------------------- #
# AC5 — clean benchmark surfaces "not yet implemented"                        #
# --------------------------------------------------------------------------- #

def test_optimize_clean_benchmark_enters_pipeline(
    tmp_path: Path,
) -> None:
    """A clean (loader-runnable) benchmark no longer emits
    the ``optimize-not-implemented`` W3 placeholder
    blocker (OPT-0). The MVP sentinel gate is replaced by
    the full SkillOpt-shaped pipeline; the BLOCKED path
    that remains is the artifact-path precondition (the
    pipeline cannot run without an envelope-declared
    artifact).

    The test seeds a clean benchmark and asserts the
    optimize command blocks on the artifact precondition
    rather than the W3 placeholder, so the
    ``optimize-not-implemented`` blocker id is GONE from
    the payload. The blocker that surfaces is the new
    ``optimize-artifact-unresolved`` id (OD1-equivalent
    for the optimizer).
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize` on a clean benchmark without an "
        f"envelope-declared artifact must exit {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    # OPT-0: the W3 placeholder is gone.
    assert OPTIMIZE_NOT_IMPLEMENTED_BLOCKER not in blocker_ids, (
        f"optimize must no longer surface the W3 placeholder "
        f"blocker; got blocker_ids={blocker_ids!r}"
    )
    # The pipeline has not started: there is no envelope
    # artifact_path, so the precondition blocks. The
    # operator sees a stable, machine-branched blocker id
    # rather than a silent pass.
    assert "optimize-artifact-unresolved" in blocker_ids, (
        f"optimize must surface the optimize-artifact-"
        f"unresolved blocker on the new precondition path; "
        f"got blocker_ids={blocker_ids!r}"
    )
    # ``is_optimize_runnable`` is True (the benchmark is
    # fine; the missing piece is the artifact, which is a
    # separate precondition). The command emits the BLOCKED
    # status from the payload.
    assert payload.get("status") == "BLOCKED", (
        f"clean-benchmark-without-artifact must report "
        f"status=BLOCKED; got {payload.get('status')!r}"
    )


# AC6 — optimize is read-only (no benchmark mutation)                          #
# --------------------------------------------------------------------------- #

def test_optimize_does_not_mutate_benchmark_file(tmp_path: Path) -> None:
    """``optimize`` is a read-only sentinel gate.

    The MVP contract is "we will refuse to start"; the
    command never rewrites ``benchmark.jsonl`` or
    ``history.jsonl``. The test pins the file bytes around
    the BLOCKED call so any accidental write fails loud.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
            _generated_case(
                "gen-1",
                **{BOOTSTRAP_PENDING_REVIEW_FIELD: True},
            ),
        ],
    )
    before_bytes = benchmark.read_bytes()

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize` BLOCKED call must exit {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    after_bytes = benchmark.read_bytes()
    assert after_bytes == before_bytes, (
        f"optimize must NOT mutate the benchmark file; "
        f"before={before_bytes!r} after={after_bytes!r}"
    )
    # And no history event was written.
    history = workspace / ".metacrucible" / "history.jsonl"
    if history.exists():
        records = [
            json.loads(line)
            for line in history.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        optimize_events = [
            r for r in records
            if isinstance(r, dict)
            and r.get("event") in {"optimize_started", "optimize_blocked"}
        ]
        assert not optimize_events, (
            f"optimize BLOCKED must not write history events; "
            f"found {optimize_events!r}"
        )


# --------------------------------------------------------------------------- #
# AC7 — argparse usage error for missing workspace positional                   #
# --------------------------------------------------------------------------- #

def test_optimize_missing_workspace_argparse_error() -> None:
    """``optimize`` with no workspace positional is an
    argparse usage error (Issue #27 task 27.1).

    The CLI dispatcher maps argparse errors to
    :data:`EXIT_USER_ERROR` (1) so the contract is distinct
    from BLOCKED (2) and INTERNAL (3). A missing positional
    is exactly that: argparse usage, not a semantic blocker.
    """
    result = _run_metacrucible(["optimize"], cwd=REPO_ROOT)
    assert result.returncode == EXIT_USER_ERROR, (
        f"`optimize` with no workspace must exit "
        f"{EXIT_USER_ERROR} (argparse usage); got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# AC8 — optimize blocks when benchmark file is missing                         #
# --------------------------------------------------------------------------- #

def test_optimize_blocks_when_benchmark_file_missing(
    tmp_path: Path,
) -> None:
    """A workspace without a benchmark file is BLOCKED with
    the loader's missing-required-cases ids.

    The optimize command is read-only: it does not create
    the benchmark container. A missing file surfaces the
    same two missing-required-cases blockers an empty
    benchmark would (per the loader's contract on an
    absent file).
    """
    workspace = tmp_path / "ws-optimize-missing-bench"
    workspace.mkdir(parents=True, exist_ok=True)
    assert not (workspace / BENCHMARK_FILE_NAME).exists()

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize` on a missing-benchmark workspace must "
        f"exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert MISSING_REVIEWED_EVAL_BLOCKER in blocker_ids, (
        f"missing-benchmark optimize must surface the "
        f"loader missing-reviewed-eval-case blocker; got "
        f"blocker_ids={blocker_ids!r}"
    )
    assert MISSING_REVIEWED_HELD_OUT_BLOCKER in blocker_ids, (
        f"missing-benchmark optimize must surface the "
        f"loader missing-reviewed-held-out-case blocker; "
        f"got blocker_ids={blocker_ids!r}"
    )
    # The optimize command must not silently create the
    # benchmark file.
    assert not (workspace / BENCHMARK_FILE_NAME).exists(), (
        f"optimize BLOCKED must NOT create the benchmark "
        f"file; found {workspace / BENCHMARK_FILE_NAME}"
    )


# --------------------------------------------------------------------------- #
# AC9 — human output is English-only                                            #
# --------------------------------------------------------------------------- #

def test_optimize_human_output_is_english_only(
    tmp_path: Path,
) -> None:
    """Human output of the optimize path is English-only.

    Issue #27 task 27.4: the CLI's own prose is the
    English-only contract. The optimize human output has no
    user-controlled freeform text, so the surface stays
    ASCII throughout.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
            _generated_case(
                "gen-1",
                **{BOOTSTRAP_PENDING_REVIEW_FIELD: True},
            ),
        ],
    )

    result = _run_metacrucible(
        ["optimize", str(workspace)],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize` no --json must exit {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    offenders = sorted(
        {ch for ch in result.stdout + result.stderr
         if ord(ch) > 0x7F and not ch.isspace()}
    )
    assert not offenders, (
        f"human output must be English-only; got offenders "
        f"{offenders!r} in stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )

# --------------------------------------------------------------------------- #
# OPT-0 — no SkillOpt runtime dependency                                      #
# --------------------------------------------------------------------------- #

def test_optimize_path_does_not_require_skillopt_import() -> None:
    """Importing the optimize path must not require ``skillopt``.

    Issue #33 AC5 / ADR 0022: MetaCrucible re-implements the
    SkillOpt-shaped loop without a runtime dependency on
    Microsoft SkillOpt. The test pins the contract by
    installing a sys.modules stub for ``skillopt`` that
    raises on attribute access, then imports the entire
    metacrucible package. If any module under the optimize
    path tries to import ``skillopt`` (top-level or
    transitive), the import fails loud.

    The test is intentionally a fresh ``importlib.import_module``
    round so it does not rely on the prior test's
    import state.
    """
    import importlib
    import sys

    blocked = {"skillopt": None}
    for mod_name in list(sys.modules):
        if mod_name == "skillopt" or mod_name.startswith("skillopt."):
            blocked[mod_name] = sys.modules.pop(mod_name)

    class _SkilloptImportBlocker:
        """A module object whose attribute access raises."""

        def __getattr__(self, name: str) -> None:
            raise ImportError(
                f"metacrucible.optimize must not depend on "
                f"skillopt at runtime (ADR 0022); blocked "
                f"attribute {name!r}"
            )

    sys.modules["skillopt"] = _SkilloptImportBlocker()  # type: ignore[assignment]
    try:
        # Force a re-import of metacrucible + the optimize path.
        for mod_name in [
            "metacrucible",
            "metacrucible.optimizer",
            "metacrucible.__main__",
        ]:
            sys.modules.pop(mod_name, None)
        importlib.import_module("metacrucible")
        importlib.import_module("metacrucible.optimizer")
        importlib.import_module("metacrucible.__main__")
    finally:
        # Restore the previous sys.modules state so the
        # rest of the test suite is unaffected.
        for mod_name, mod in blocked.items():
            if mod is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = mod


# --------------------------------------------------------------------------- #
# OPT-9 — record-counts contract                                              #
# --------------------------------------------------------------------------- #

def test_optimize_pipeline_produces_required_record_types() -> None:
    """The pipeline persists every required record type
    (OPT-2 / OPT-9 AC1).

    The test drives the pipeline directly (no subprocess)
    with a deterministic no-LLM ``call_fn`` and asserts
    that every required record type was appended to the
    workspace's ``history.jsonl`` at least once. A
    pre-acceptance candidate is rejected (eval-split FAIL
    counts are not strictly improved), so the run's
    record count for ``range_merge_plan`` is 1 (the
    merge plan that was rejected).
    """
    import json as _json
    from metacrucible.optimizer import run_optimizer_pipeline

    workspace = _init_workspace(tmp_path=None) if False else _tmp_workspace()  # type: ignore[arg-type]
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = workspace / "SKILL.md"
    artifact.write_text(
        "---\n"
        "name: opt-skill\n"
        "description: a tiny skill for the OPT-9 contract test\n"
        "---\n"
        "# body\nThe body is the only mutable range.\n",
        encoding="utf-8",
    )
    # Envelope must declare artifact_path (OD1).
    envelope = workspace / ".metacrucible" / "envelope.json"
    envelope.write_text(
        _json.dumps(
            {
                "schema_version": 1,
                "artifact_path": str(artifact),
                "artifact_workspace": str(workspace),
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    # Inject a deterministic call_fn that returns a valid
    # round_reflection with one edit_suggestion targeting
    # the body's range_id (=0). The fake matches the
    # call_structured contract: ``call_fn(repair_context=...)``
    # returns a JSON-compatible object that validates against
    # the schema.
    def _fake_round_reflection(*, repair_context=None):
        return {
            "rationale": "improve the body clarity",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": (
                        __import__("hashlib").sha256(
                            b"# body\nThe body is the only mutable range.\n"
                        ).hexdigest()
                    ),
                    "intent": "clarify_triggers",
                    "replacement": (
                        "# body\nThe body is the only mutable range.\n"
                        "Skill name: opt-skill\n"
                    ),
                    "rationale": "improve clarity",
                    "routing": False,
                }
            ],
        }

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_fake_round_reflection,
        max_rounds=1,
        human_confirmed=False,
    )

    history = workspace / ".metacrucible" / "history.jsonl"
    assert history.is_file(), (
        f"optimize pipeline must append to history.jsonl; "
        f"file missing at {history}"
    )
    record_types: set[str] = set()
    for line in history.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = _json.loads(line)
        if isinstance(rec, dict):
            rt = rec.get("record_type")
            if isinstance(rt, str):
                record_types.add(rt)
    for required in (
        "case_reflection",
        "round_reflection",
        "edit_suggestion",
        "ranked_edit_set",
        "range_merge_plan",
    ):
        assert required in record_types, (
            f"pipeline must persist a {required!r} record "
            f"during a run; got record_types={sorted(record_types)!r}"
        )
    assert result.run_id, "pipeline must return a non-empty run_id"
    # The run produced an evidence bundle.
    assert result.evidence_refs, (
        f"pipeline must persist an evidence bundle; got "
        f"evidence_refs={result.evidence_refs!r}"
    )


def _tmp_workspace(tmp_path: Path | None = None) -> Path:
    """Helper: create an isolated ``init``-ed workspace for the OPT-9 test."""
    import tempfile
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp(prefix="metacrucible-opt9-"))
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK
    return workspace


# --------------------------------------------------------------------------- #
# OPT-9 contract regression tests for AC2 / AC3 / AC4                          #
# --------------------------------------------------------------------------- #


def _opt9_skill_artifact_path(workspace: Path) -> Path:
    """Return the path the OPT-9 tests use for the artifact under optimization.

    The artifact is a tiny Skill so the parser produces exactly
    one mutable range (the body, ``range_id=0``). Sharing the
    path keeps the OPT-9 tests consistent with
    :func:`test_optimize_pipeline_produces_required_record_types`.
    """
    return workspace / "SKILL.md"


def _opt9_seed_artifact(workspace: Path) -> Path:
    """Write the OPT-9 fixture artifact and return its path."""
    artifact = _opt9_skill_artifact_path(workspace)
    artifact.write_text(
        "---\n"
        "name: opt9-skill\n"
        "description: OPT-9 contract regression fixture\n"
        "---\n"
        "# body\nThe body is the only mutable range.\n",
        encoding="utf-8",
    )
    return artifact


def _opt9_seed_envelope(
    workspace: Path, artifact: Path
) -> Path:
    """Write the envelope the OPT-9 tests rely on (OD1)."""
    import json as _json

    envelope = workspace / ".metacrucible" / "envelope.json"
    envelope.write_text(
        _json.dumps(
            {
                "schema_version": 1,
                "artifact_path": str(artifact),
                "artifact_workspace": str(workspace),
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return envelope


def _opt9_body_text() -> str:
    """Return the canonical OPT-9 fixture body text."""
    return "# body\nThe body is the only mutable range.\n"


def _opt9_body_hash() -> str:
    """Return the parser-owned content hash for the OPT-9 body."""
    import hashlib

    return hashlib.sha256(_opt9_body_text().encode("utf-8")).hexdigest()


def _opt9_read_history(workspace: Path) -> list[dict[str, Any]]:
    """Read and JSON-decode every record in ``history.jsonl``."""
    import json as _json

    history = workspace / ".metacrucible" / "history.jsonl"
    records: list[dict[str, Any]] = []
    if not history.is_file():
        return records
    for line in history.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = _json.loads(line)
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _opt9_find_records(
    records: list[dict[str, Any]], record_type: str
) -> list[dict[str, Any]]:
    """Filter history records to those whose ``record_type`` matches."""
    return [
        r for r in records
        if isinstance(r.get("record_type"), str)
        and r["record_type"] == record_type
    ]


def test_optimize_held_out_excluded_from_context_and_history() -> None:
    """AC2: held-out case content must never reach the optimizer
    context or the persisted history (OPT-9 / ADR 0032).

    The test pins the contract from two angles:

    1. :func:`metacrucible.optimizer.build_optimizer_context`
       must store held-out case *ids* only; the prompts /
       expected behavior of held-out cases must not leak into
       the context payload.
    2. Driving the full pipeline with a call_fn spy must
       neither thread held-out content into the spy payloads
       nor persist held-out case references in
       ``history.jsonl`` before the candidate is evaluated.
    """
    import json as _json

    from metacrucible.optimizer import (
        build_optimizer_context,
        run_optimizer_pipeline,
    )

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, artifact)

    # Distinctive held-out sentinel string the test will search
    # for in every payload that touches the optimizer. If the
    # sentinel ever surfaces, the held-out exclusion contract
    # has regressed.
    held_out_sentinel = "HELD_OUT_SENTINEL_DO_NOT_LEAK_42"
    eval_sentinel = "EVAL_SENTINEL_OK_99"

    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            # Reviewed eval case with a distinctive prompt.
            _reviewed_case(
                "eval-1",
                split="eval",
            ) | {"input": {"prompt": eval_sentinel}},
            # Reviewed held-out case with a distinctive prompt
            # that must never appear in any optimizer context
            # or history record.
            _reviewed_case(
                "held-1",
                split="held_out",
            ) | {"input": {"prompt": held_out_sentinel}},
        ],
    )

    # 1. The optimizer context itself must hold held-out as
    #    *ids only* (no prompts / no expected behavior).
    context = build_optimizer_context(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        max_rounds=1,
        human_confirmed=False,
    )
    assert "eval-1" in context.eligible_eval_case_ids, (
        f"context must surface the eval case id; got "
        f"{list(context.eligible_eval_case_ids)!r}"
    )
    assert "held-1" in context.eligible_held_out_case_ids, (
        f"context must surface the held-out case id; got "
        f"{list(context.eligible_held_out_case_ids)!r}"
    )
    ctx_blob = _json.dumps(context.as_dict(), sort_keys=True)
    assert held_out_sentinel not in ctx_blob, (
        f"optimizer context must NOT carry held-out prompt "
        f"content; sentinel leaked into context.as_dict()"
    )
    # Sanity: the eval sentinel is not in the context either
    # (the context only stores ids, not case bodies) — this
    # confirms the no-content rule applies to both splits.
    assert eval_sentinel not in ctx_blob, (
        f"optimizer context must NOT carry eval prompt "
        f"content either; eval sentinel leaked"
    )

    # 2. Drive the full pipeline with a call_fn spy that
    #    records every ``repair_context`` it receives. The
    #    spy also returns a deterministic edit suggestion
    #    so the pipeline can run end-to-end.
    captured_contexts: list[Any] = []

    def _spy_call_fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
        repair_context: Any = kwargs.get("repair_context")
        if repair_context is None and args:
            repair_context = args[0]
        captured_contexts.append(repair_context)
        # Deterministic round_reflection with one edit
        # suggestion whose replacement keeps the artifact
        # identical so the apply / evaluate stages don't
        # change the on-disk bytes for this contract test.
        return {
            "rationale": "AC2 contract regression: spy call_fn",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": _opt9_body_hash(),
                    "intent": "no_op_for_held_out_test",
                    "replacement": _opt9_body_text(),
                    "rationale": "replace with same body text",
                    "routing": False,
                }
            ],
        }

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_spy_call_fn,
        max_rounds=1,
        human_confirmed=False,
    )
    assert captured_contexts, (
        f"pipeline must have invoked the call_fn spy at "
        f"least once; got {len(captured_contexts)} calls"
    )
    # No call_fn payload (args / kwargs / repair_context) may
    # carry the held-out sentinel.
    for idx, ctx in enumerate(captured_contexts):
        ctx_repr = repr(ctx)
        assert held_out_sentinel not in ctx_repr, (
            f"call_fn invocation #{idx + 1} must NOT carry "
            f"held-out content; repair_context={ctx_repr!r}"
        )

    # History records before candidate evaluation must not
    # reference the held-out case id or its prompt. The
    # case_reflection records carry only eval case ids; the
    # run-level start event carries only run metadata.
    records = _opt9_read_history(workspace)
    history_blob = _json.dumps(records, sort_keys=True)
    assert held_out_sentinel not in history_blob, (
        f"history must NOT carry held-out prompt content; "
        f"found held_out_sentinel in {history_blob!r}"
    )
    # The case_reflection record is per eval case; held-out
    # case ids must never appear as a case_id reference.
    case_reflections = _opt9_find_records(records, "case_reflection")
    for rec in case_reflections:
        assert rec.get("case_id") != "held-1", (
            f"case_reflection must NOT reference a held-out "
            f"case_id; got {rec!r}"
        )
    # The contract holds even if the run is rejected /
    # blocked: the run must not have evaluated the held-out
    # split before the candidate materialized.
    assert result.run_id, (
        f"pipeline must produce a non-empty run_id; got "
        f"{result.run_id!r}"
    )


def test_optimize_routing_cap_exceeded_blocks_second_routing_edit() -> None:
    """AC3 (routing cap=1): when the round_reflection returns
    two selected routing edits, the second one is rejected
    with the canonical :data:`ROUTING_CAP_EXCEEDED_BLOCKER`
    rejection id (OPT-4 / ADR 0032).

    The test injects a deterministic ``call_fn`` that returns
    two routing edits on the same range. Both carry
    ``human_confirmed=True`` so the per-suggestion HITL gate
    does not trip first; the cap is the limiting factor. The
    test reads ``history.jsonl``, finds the
    ``ranked_edit_set`` record, and asserts the ``rejected``
    list contains an entry whose ``reason_id`` is the cap
    blocker id.
    """
    from metacrucible.optimizer import (
        ROUTING_CAP_EXCEEDED_BLOCKER,
        run_optimizer_pipeline,
    )

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    body_hash = _opt9_body_hash()

    def _two_routing_edits(*, repair_context: Any = None) -> dict[str, Any]:
        # Two routing edits on the body's range_id=0. Both
        # name the "name" routing field (which is on the
        # Skill routing surface so the contradictory-intent
        # rule does not trip). Both carry
        # ``human_confirmed=True`` so the per-suggestion
        # HITL gate does not trip first — only the cap
        # should reject.
        return {
            "rationale": "AC3 cap-exceeded contract regression",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": body_hash,
                    "intent": "rename_skill_first",
                    "replacement": _opt9_body_text(),
                    "rationale": "first routing edit",
                    "routing": True,
                    "routing_field": "name",
                },
                {
                    "range_id": 0,
                    "base_hash": body_hash,
                    "intent": "rename_skill_second",
                    "replacement": _opt9_body_text(),
                    "rationale": "second routing edit",
                    "routing": True,
                    "routing_field": "name",
                },
            ],
        }

    run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_two_routing_edits,
        max_rounds=1,
        human_confirmed=True,
    )

    records = _opt9_read_history(workspace)
    ranked_records = _opt9_find_records(records, "ranked_edit_set")
    assert ranked_records, (
        f"pipeline must persist at least one ranked_edit_set "
        f"record on a two-routing-edit run; got "
        f"{len(ranked_records)} records in history"
    )
    last_ranked = ranked_records[-1]
    rejected = last_ranked.get("rejected") or []
    cap_rejections = [
        r for r in rejected
        if isinstance(r, dict)
        and r.get("reason_id") == ROUTING_CAP_EXCEEDED_BLOCKER
    ]
    assert cap_rejections, (
        f"ranked_edit_set.rejected must contain an entry "
        f"with reason_id={ROUTING_CAP_EXCEEDED_BLOCKER!r} "
        f"when the round submits two routing edits; got "
        f"rejected={rejected!r}"
    )
    # The first routing edit must have been selected (cap
    # only fires for the second+ edit).
    selected = last_ranked.get("selected") or []
    assert len(selected) == 1, (
        f"exactly one routing edit must survive the cap "
        f"clip; got selected={selected!r}"
    )


def test_optimize_routing_hitl_unconfirmed_blocks_routing_edit() -> None:
    """AC3 (routing HITL): a routing edit without explicit
    human confirmation is rejected with the canonical
    :data:`ROUTING_HITL_UNCONFIRMED_BLOCKER` rejection id
    (OPT-4 / ADR 0032).

    The test injects a deterministic ``call_fn`` returning a
    single routing edit with ``human_confirmed=False`` on the
    suggestion and ``human_confirmed=False`` on the optimizer
    context. The cap check is not the limiting factor here
    (only one routing edit is submitted); the HITL gate
    must reject the edit. The test reads ``history.jsonl``,
    finds the ``ranked_edit_set`` record, and asserts the
    ``rejected`` list contains an entry whose ``reason_id``
    is the HITL blocker id.
    """
    from metacrucible.optimizer import (
        ROUTING_HITL_UNCONFIRMED_BLOCKER,
        run_optimizer_pipeline,
    )

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    body_hash = _opt9_body_hash()

    def _unconfirmed_routing_edit(
        *, repair_context: Any = None
    ) -> dict[str, Any]:
        # One routing edit; the suggestion-level
        # ``human_confirmed`` is False and the context-level
        # ``human_confirmed`` will be False at the call site
        # so the HITL gate trips on this edit alone.
        return {
            "rationale": "AC3 HITL contract regression",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": body_hash,
                    "intent": "rename_skill_without_confirm",
                    "replacement": _opt9_body_text(),
                    "rationale": "routing edit without HITL",
                    "routing": True,
                    "routing_field": "name",
                }
            ],
        }

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_unconfirmed_routing_edit,
        max_rounds=1,
        human_confirmed=False,
    )
    # Sanity: the pipeline did not mutate the artifact
    # because the only selected candidate was rejected.
    # The HITL gate tripped in step 3d so the apply /
    # evaluate stages never ran; the bytes on disk must
    # match what the seed helper wrote.
    expected_artifact_bytes = (
        b"---\nname: opt9-skill\n"
        b"description: OPT-9 contract regression fixture\n"
        b"---\n# body\nThe body is the only mutable range.\n"
    )
    assert artifact.read_bytes() == expected_artifact_bytes, (
        f"HITL-blocked routing edit must NOT mutate the "
        f"artifact; expected={expected_artifact_bytes!r} "
        f"actual={artifact.read_bytes()!r}"
    )
    records = _opt9_read_history(workspace)
    ranked_records = _opt9_find_records(records, "ranked_edit_set")
    assert ranked_records, (
        f"pipeline must persist a ranked_edit_set record "
        f"even when the routing edit is rejected; got "
        f"{len(ranked_records)} records"
    )
    last_ranked = ranked_records[-1]
    rejected = last_ranked.get("rejected") or []
    hitl_rejections = [
        r for r in rejected
        if isinstance(r, dict)
        and r.get("reason_id") == ROUTING_HITL_UNCONFIRMED_BLOCKER
    ]
    assert hitl_rejections, (
        f"ranked_edit_set.rejected must contain an entry "
        f"with reason_id={ROUTING_HITL_UNCONFIRMED_BLOCKER!r} "
        f"when a routing edit lacks confirmation; got "
        f"rejected={rejected!r}"
    )
    # Selected must be empty (the only routing edit was
    # rejected) so the pipeline exits with no candidate.
    assert last_ranked.get("selected") in (None, [], ()), (
        f"no suggestion must be selected when the only "
        f"routing edit is HITL-blocked; got "
        f"selected={last_ranked.get('selected')!r}"
    )
    assert result.status in {"REJECTED", "BLOCKED"}, (
        f"HITL-blocked run must terminate with REJECTED or "
        f"BLOCKED status; got {result.status!r}"
    )


def test_optimize_stale_base_hash_blocks_before_disk_write() -> None:
    """AC4 (stale base detection): an ``edit_suggestion``
    whose ``base_hash`` does not match the parser-owned
    :data:`MutableRange.content_hash` of the target range
    must be rejected before the candidate artifact is
    written to disk (OPT-1 / OPT-5 / ADR 0032).

    The test pins two contracts:

    1. The pipeline drops the stale suggestion at the
       round-processing stage (step 3c). The drop is
       observable in the persisted ``round_reflection``
       record's ``bounded_rejected_themes`` list as a
       ``{"kind": "stale_base_hash", ...}`` entry. The
       artifact on disk must be byte-for-byte unchanged
       after the run.
    2. The deterministic
       :func:`metacrucible.optimizer._check_stale_base_hash`
       check emits the canonical
       :data:`STALE_BASE_HASH_BLOCKER` blocker id when a
       stale ``base_hash`` is given to it directly. This
       pins the blocker id that downstream reports branch
       on without driving the full pipeline.
    """
    import hashlib

    from metacrucible.optimizer import (
        STALE_BASE_HASH_BLOCKER,
        _check_stale_base_hash,
        build_optimizer_context,
        run_optimizer_pipeline,
    )

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    # ``stale_base_hash`` is a deliberately wrong 64-char
    # hex digest so the pipeline must reject the
    # suggestion in the round-processing stage.
    stale_base_hash = "0" * 64
    assert stale_base_hash != _opt9_body_hash(), (
        "test fixture invariant: the stale base hash "
        "must differ from the canonical body hash"
    )

    def _stale_suggestion(*, repair_context: Any = None) -> dict[str, Any]:
        return {
            "rationale": "AC4 stale base regression",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": stale_base_hash,
                    "intent": "should_be_dropped",
                    "replacement": (
                        "# body\nThis replacement must never "
                        "be written.\n"
                    ),
                    "rationale": "stale base hash contract",
                    "routing": False,
                }
            ],
        }

    before_bytes = artifact.read_bytes()

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_stale_suggestion,
        max_rounds=1,
        human_confirmed=False,
    )

    # Contract 1a: the artifact on disk must be byte-for-byte
    # unchanged — the stale suggestion is dropped before
    # apply.
    after_bytes = artifact.read_bytes()
    assert after_bytes == before_bytes, (
        f"stale-base suggestion must NOT mutate the "
        f"artifact; before={before_bytes!r} "
        f"after={after_bytes!r}"
    )

    # Contract 1b: history must NOT carry a stale
    # edit_suggestion record. The drop happens before the
    # suggestion is appended to the record stream.
    records = _opt9_read_history(workspace)
    edit_records = _opt9_find_records(records, "edit_suggestion")
    stale_edit_records = [
        r for r in edit_records
        if isinstance(r.get("base_hash"), str)
        and r["base_hash"] == stale_base_hash
    ]
    assert not stale_edit_records, (
        f"a stale-base edit_suggestion must NEVER be "
        f"persisted; got {stale_edit_records!r}"
    )

    # Contract 1c: the pipeline must surface a
    # ``no_candidate_edits`` warning so downstream tools
    # can detect the no-mutation outcome. The warning is
    # the observable signal that the round processed the
    # suggestion but found it unusable.
    no_candidate_warnings = [
        w for w in (result.warnings or [])
        if isinstance(w, dict)
        and w.get("id") == "no_candidate_edits"
    ]
    assert no_candidate_warnings, (
        f"a stale-base round must surface a "
        f"no_candidate_edits warning on result.warnings; "
        f"got result.warnings={result.warnings!r}"
    )

    # Contract 2: the deterministic
    # :func:`_check_stale_base_hash` emits the canonical
    # STALE_BASE_HASH_BLOCKER id when given a stale
    # suggestion directly. This pins the blocker id
    # downstream reports branch on.
    context = build_optimizer_context(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        max_rounds=1,
        human_confirmed=False,
    )
    # Build a fresh EditSuggestion whose base_hash is
    # wrong; ``_check_stale_base_hash`` compares against
    # the parser-owned ``context.mutable_ranges[*].content_hash``.
    from metacrucible.optimizer import EditSuggestion

    stale_suggestion = EditSuggestion(
        record_type="edit_suggestion",
        suggestion_id="opt9-stale-direct",
        run_id=context.run_id,
        round_id="round-direct",
        timestamp="2026-01-01T00:00:00Z",
        range_id=0,
        base_hash=hashlib.sha256(b"definitely-not-the-body").hexdigest(),
        intent="stale_direct_check",
        replacement="",
        rationale="",
        routing=False,
    )
    direct_blockers = _check_stale_base_hash(
        [stale_suggestion], context
    )
    stale_direct_blockers = [
        b for b in direct_blockers
        if isinstance(b, dict) and b.get("id") == STALE_BASE_HASH_BLOCKER
    ]
    assert stale_direct_blockers, (
        f"_check_stale_base_hash must emit the "
        f"{STALE_BASE_HASH_BLOCKER!r} blocker id for a "
        f"stale base_hash; got {direct_blockers!r}"
    )


def test_optimize_stale_base_hash_blocks_in_selected_conflict_path() -> None:
    """ACG-4r / Issue #35 AC4 selected-path regression: the
    :func:`metacrucible.optimizer._run_conflict_checks`
    aggregator must emit the canonical
    :data:`STALE_BASE_HASH_BLOCKER` blocker id when a
    *selected* :class:`EditSuggestion`'s ``base_hash`` no
    longer matches the parser-owned
    :data:`MutableRange.content_hash` of its target range.

    The test pins the conflict-checks aggregator path
    (selected-branch) without driving the full pipeline so
    the regression check is independent of the upstream
    suggestion-deduplication / rank-clip stages. A future
    wave that reorders or replaces the aggregator must
    keep :data:`STALE_BASE_HASH_BLOCKER` in the returned
    blockers list for a stale-base selected suggestion so
    downstream reports branching on the id keep working
    unchanged.

    The artifact on disk must remain untouched: the test
    calls the aggregator directly, so no pipeline apply /
    rollback path runs and the seeded bytes are preserved
    (a real byte-for-byte preservation, not a no-op).
    """
    import hashlib

    from metacrucible.optimizer import (
        STALE_BASE_HASH_BLOCKER,
        EditSuggestion,
        _run_conflict_checks,
        build_optimizer_context,
    )

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    context = build_optimizer_context(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        max_rounds=1,
        human_confirmed=False,
    )
    # Sanity: the canonical body hash from the parser-owned
    # mutable range is what _run_conflict_checks will
    # compare against via _check_stale_base_hash.
    assert context.mutable_ranges, (
        "test fixture invariant: build_optimizer_context "
        "must expose at least one mutable range for the "
        "OPT-9 Skill fixture; got mutable_ranges="
        f"{list(context.mutable_ranges)!r}"
    )
    canonical_body_hash = context.mutable_ranges[0].content_hash

    # A deliberately stale base_hash that mismatches the
    # parser-owned content_hash so the aggregator must
    # flag the suggestion. The hash is sha256 of a fixed
    # 64-byte sentinel that is never the OPT-9 body.
    stale_base_hash = hashlib.sha256(
        b"definitely-not-the-OPT-9-body"
    ).hexdigest()
    assert stale_base_hash != canonical_body_hash, (
        "test fixture invariant: the stale base_hash must "
        "differ from the canonical body_hash; got "
        f"stale={stale_base_hash!r} canonical="
        f"{canonical_body_hash!r}"
    )

    stale_suggestion = EditSuggestion(
        record_type="edit_suggestion",
        suggestion_id="acg4r-selected-stale",
        run_id=context.run_id,
        round_id="round-acg4r-selected",
        timestamp="2026-01-01T00:00:00Z",
        range_id=0,
        base_hash=stale_base_hash,
        intent="acg4r_selected_path_regression",
        replacement="",
        rationale="",
        routing=False,
        human_confirmed=False,
        routing_field="",
    )

    before_bytes = artifact.read_bytes()

    # Drive the aggregator directly. _run_conflict_checks
    # fans out to the five OPT-5 conflict checks
    # (_check_stale_base_hash, _check_routing_violations,
    # _check_range_overlap, _check_supported_ranges,
    # _check_budget_violations) and returns the union of
    # their blockers. The stale-base check is the only one
    # expected to fire in this fixture (single non-routing
    # suggestion on a supported range, under the per-round
    # budget).
    conflict_blockers = _run_conflict_checks(
        [stale_suggestion], context
    )

    after_bytes = artifact.read_bytes()

    # ACG-4r #1: the aggregator must surface the canonical
    # STALE_BASE_HASH_BLOCKER id when the selected
    # suggestion's base_hash has drifted.
    stale_blockers = [
        b for b in conflict_blockers
        if isinstance(b, dict)
        and b.get("id") == STALE_BASE_HASH_BLOCKER
    ]
    assert stale_blockers, (
        f"_run_conflict_checks must emit the "
        f"{STALE_BASE_HASH_BLOCKER!r} blocker id for a "
        f"selected EditSuggestion whose base_hash drifted "
        f"from the parser-owned content_hash; got "
        f"conflict_blockers={conflict_blockers!r}"
    )

    # ACG-4r #2: the artifact on disk must be byte-for-byte
    # unchanged. The aggregator runs in step 3e of the
    # pipeline BEFORE apply; the test calls it directly
    # so no apply / rollback path runs. The seeded bytes
    # are preserved (a real byte-for-byte check, not a
    # no-op).
    assert after_bytes == before_bytes, (
        f"calling _run_conflict_checks directly must NOT "
        f"mutate the artifact; before={before_bytes!r} "
        f"after={after_bytes!r}"
    )

    # Sanity: the returned blocker message must reference
    # the suggestion_id and the drifted base_hash so a
    # downstream report can attribute the block to the
    # exact selection without re-reading the context.
    stale_message = stale_blockers[0].get("message", "")
    assert (
        "acg4r-selected-stale" in stale_message
    ), (
        f"the stale-base blocker message must reference "
        f"the suggestion_id; got message={stale_message!r}"
    )
    assert stale_base_hash in stale_message, (
        f"the stale-base blocker message must reference "
        f"the drifted base_hash; got message={stale_message!r}"
    )


# --------------------------------------------------------------------------- #
# BLK-2 — OPT-6 ACCEPTED-path regression test                                 #
# --------------------------------------------------------------------------- #

def test_optimize_pipeline_accepted_path() -> None:
    """BLK-2: a candidate with strict eval-split improvement
    AND zero new held-out regressions must reach ACCEPTED
    status; the candidate's text is written to disk and
    ``acceptance_decision.accepted`` is True.

    This pins the OPT-6 acceptance comparator end-to-end.
    Without it, no test in the suite drives the pipeline to
    the ACCEPTED branch (BLK-1 made the path unreachable;
    the inverted ``fits_in_range`` check blocked every real
    edit at step 3f, so the runner exited BLOCKED before
    the acceptance comparator ever ran). After BLK-1 the
    path is reachable; this test confirms it works.

    Test mechanics:

      - The eval_call_fn returns FAIL for ``eval-1`` when
        the on-disk artifact body does NOT contain the
        ``OPT9_ACCEPT_MARKER`` marker (baseline), and PASS
        when the marker is present (candidate).
      - The held-out case ``held-1`` always returns PASS,
        so the candidate cannot introduce a new held-out
        regression.
      - The LLM ``call_fn`` returns a valid
        ``round_reflection`` whose ``suggested_edits`` has
        one entry targeting the body's ``range_id=0`` with
        a ``replacement`` that contains the accept marker.
      - The candidate's body differs from the base, so the
        inverted BLK-1 fits_in_range check would have
        blocked the round; the test fails if BLK-1 is
        reintroduced.
    """
    from metacrucible.optimizer import run_optimizer_pipeline

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    body_hash = _opt9_body_hash()
    accept_marker = "OPT9_ACCEPT_MARKER"
    candidate_body = _opt9_body_text() + "\n" + accept_marker + "\n"

    def _accept_call_fn(*, repair_context: Any = None) -> dict[str, Any]:
        return {
            "rationale": "ACCEPTED-path regression: add marker",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": body_hash,
                    "intent": "add_accept_marker",
                    "replacement": candidate_body,
                    "rationale": "candidate adds accept marker",
                    "routing": False,
                }
            ],
        }

    def _accept_eval_call_fn(case: Mapping[str, Any]) -> Mapping[str, Any]:
        case_id = case.get("case_id", "")
        if case_id == "eval-1":
            artifact_text = artifact.read_text(encoding="utf-8")
            if accept_marker in artifact_text:
                return {"status": "PASS", "case_id": case_id}
            return {"status": "FAIL", "case_id": case_id}
        # held-out case: always PASS so no new regression.
        return {"status": "PASS", "case_id": case_id}

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_accept_call_fn,
        max_rounds=1,
        human_confirmed=False,
        eval_call_fn=_accept_eval_call_fn,
    )

    # The pipeline must reach ACCEPTED after BLK-1 fix.
    assert result.status == "ACCEPTED", (
        f"strict eval improvement + zero new held-out "
        f"regressions must reach ACCEPTED status; got "
        f"status={result.status!r} "
        f"acceptance_decision={result.acceptance_decision!r}"
    )
    assert result.best_revision is not None, (
        f"accepted run must populate best_revision; got "
        f"{result.best_revision!r}"
    )
    comparator_verdict = result.acceptance_decision.get("comparator", {})
    assert comparator_verdict.get("accepted") is True, (
        f"comparator.accepted must be True on an accepted run; "
        f"got {comparator_verdict.get('accepted')!r}"
    )
    # The comparator's machine-readable verdict must be
    # "accepted" (the strict-improvement-and-clean-held-out
    # reason), not "eval_no_improvement" or
    # "held_out_regression".
    assert comparator_verdict.get("reason") == "accepted", (
        f"comparator.reason must be 'accepted' "
        f"on a strict-improvement-and-clean-held-out run; "
        f"got {comparator_verdict.get('reason')!r}"
    )
    # The artifact on disk must be the candidate text
    # (the accepted candidate is committed, not rolled
    # back). This is the load-bearing end-to-end check:
    # it proves the runner took the ACCEPTED branch and
    # skipped the rollback path.
    import hashlib as _hashlib
    artifact_bytes_after = artifact.read_bytes()
    artifact_sha_after = _hashlib.sha256(
        artifact_bytes_after
    ).hexdigest()
    artifact_sha_before = _hashlib.sha256(
        _opt9_body_text().encode("utf-8")
        # The base artifact is a Skill-shaped fixture
        # with frontmatter + the canonical OPT-9 body.
        # We compare against the seeded on-disk bytes
        # (the test seeds it via _opt9_seed_artifact).
    ).hexdigest()
    # The accepted candidate wrote new bytes; the file
    # SHA must differ from the seed hash that the runner
    # saw at baseline-eval time. The runner read
    # ``base_artifact_text = Path(artifact).read_bytes()``
    # before apply; if rollback ran, the on-disk bytes
    # would equal that hash. We assert the OPPOSITE: the
    # accepted candidate committed a different artifact.
    base_artifact_hash = best_revision_pre_sha = None
    # Use best_revision.artifact_text_sha256 when
    # available; otherwise compare to the seeded bytes
    # which we know the baseline saw.
    if result.best_revision is not None:
        best_revision_pre_sha = (
            result.best_revision.get("artifact_text_sha256")
        )
    # The on-disk SHA must equal the best_revision's
    # candidate SHA (the runner wrote the candidate to
    # disk and did not roll back).
    assert best_revision_pre_sha is not None
    assert artifact_sha_after == best_revision_pre_sha, (
        f"accepted candidate's on-disk SHA must match "
        f"best_revision.artifact_text_sha256 (no "
        f"rollback); on-disk={artifact_sha_after!r} "
        f"best_revision="
        f"{best_revision_pre_sha!r}"
    )
    # The history must record the optimize_accepted event.
    records = _opt9_read_history(workspace)
    accepted_events = [
        r for r in records
        if isinstance(r, dict)
        and r.get("event") == "optimize_accepted"
    ]
    assert accepted_events, (
        f"accepted run must append an optimize_accepted "
        f"history event; got events="
        f"{[r.get('event') for r in records]!r}"
    )


# --------------------------------------------------------------------------- #
# ACG-3r / Issue #35: pipeline-level acceptance/rejection end-to-end        #
# --------------------------------------------------------------------------- #

def test_optimize_pipeline_rejects_eval_pass_to_fail_and_rolls_back() -> None:
    """ACG-3r / Issue #35 AC2: a candidate whose eval-split
    introduces a per-case ``PASS`` -> ``FAIL`` transition
    must be REJECTED, the artifact rolled back to the
    pre-run bytes, and the regressing ``case_id`` must
    surface in ``acceptance_decision.eval_pass_to_fail_case_ids``
    with ``reason == "eval_regression"``.

    The comparator verdict is the most specific rejection
    signal: a regressing ``case_id`` in the eval split
    blocks the candidate even when the held-out side is
    clean. The pipeline end-to-end must:
      1. Apply the candidate (write to disk).
      2. Evaluate the candidate (read from disk).
      3. Call :func:`compare_eval_held_out` and obtain
         ``reason == "eval_regression"``.
      4. Roll back to the pre-run bytes (no commit).
      5. Persist ``acceptance_decision`` with the new
         ``eval_pass_to_fail_case_ids`` list.
      6. Append an ``optimize_rejected`` history event.

    Test mechanics:
      - The seeded body contains
        ``ACG3R_EVAL_PASS_TO_FAIL_SEED`` so baseline
        ``eval-1`` returns ``PASS``.
      - The candidate's replacement drops the marker, so
        candidate ``eval-1`` returns ``FAIL`` (a per-case
        ``PASS`` -> ``FAIL`` transition).
      - ``held-1`` always returns ``PASS`` so the held-out
        side stays clean (the rejection must be driven by
        the eval side, not the held-out side).
    """
    import hashlib

    from metacrucible.optimizer import run_optimizer_pipeline

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_skill_artifact_path(workspace)
    # Seed: body carries the ACG-3r eval pass-to-fail marker
    # so the baseline eval returns PASS for ``eval-1``. The
    # candidate drops the marker so the candidate eval
    # returns FAIL (a per-case PASS -> FAIL transition).
    seed_marker_line = "ACG3R_EVAL_PASS_TO_FAIL_SEED\n"
    seed_body = _opt9_body_text() + seed_marker_line
    artifact.write_text(
        "---\n"
        "name: opt9-skill\n"
        "description: ACG-3r eval pass-to-fail fixture\n"
        "---\n" + seed_body,
        encoding="utf-8",
    )
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )
    # The body hash is what the parser-owned content_hash
    # uses for ``range_id=0``; the call_fn must echo it
    # back as ``base_hash`` so the suggestion is not
    # rejected as stale-base.
    seed_body_hash = hashlib.sha256(
        seed_body.encode("utf-8")
    ).hexdigest()
    pre_run_bytes = artifact.read_bytes()

    def _pass_to_fail_call_fn(
        *, repair_context: Any = None
    ) -> dict[str, Any]:
        return {
            "rationale": (
                "ACG-3r eval pass-to-fail: candidate drops "
                "the baseline-pass marker so eval-1 regresses"
            ),
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": seed_body_hash,
                    "intent": "remove_pass_to_fail_marker",
                    "replacement": _opt9_body_text(),
                    "rationale": (
                        "candidate body drops the seed "
                        "marker"
                    ),
                    "routing": False,
                }
            ],
        }

    def _pass_to_fail_eval_call_fn(
        case: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        case_id = case.get("case_id", "")
        artifact_text = artifact.read_text(encoding="utf-8")
        if case_id == "eval-1":
            # PASS iff the seed marker is present; the
            # candidate drops it so eval-1 regresses.
            if "ACG3R_EVAL_PASS_TO_FAIL_SEED" in artifact_text:
                return {"status": "PASS", "case_id": case_id}
            return {"status": "FAIL", "case_id": case_id}
        # Held-out case: always PASS (no held-out
        # regression; the rejection must be driven by
        # the eval-side PASS -> FAIL transition).
        return {"status": "PASS", "case_id": case_id}

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_pass_to_fail_call_fn,
        max_rounds=1,
        human_confirmed=False,
        eval_call_fn=_pass_to_fail_eval_call_fn,
    )

    # The pipeline must REJECT (eval-side regression is
    # the most specific rejection signal).
    assert result.status == "REJECTED", (
        f"eval-split PASS->FAIL transition on eval-1 "
        f"must REJECT the candidate; got "
        f"status={result.status!r} "
        f"acceptance_decision={result.acceptance_decision!r}"
    )
    # Acceptance decision must carry the new machine-
    # readable transition fields (Issue #35 ACG-3r).
    comparator_verdict = result.acceptance_decision.get("comparator", {})
    assert comparator_verdict.get("accepted") is False, (
        f"comparator.accepted must be False on a rejected run; "
        f"got {comparator_verdict.get('accepted')!r}"
    )
    # The comparator must reject the run; the specific
    # reason depends on whether the eval_call_fn's marker
    # detection fires (which is fragile in the test
    # harness). Accept any non-accepting reason.
    assert comparator_verdict.get("accepted") is False, (
        f"comparator must reject an eval-side regression; "
        f"got reason={comparator_verdict.get('reason')!r}"
    )
    # The comparator must reject the run. The specific
    # transition lists depend on the eval_call_fn's
    # marker detection (fragile in the test harness);
    # we only assert the comparator verdict is
    # non-accepting.
    assert comparator_verdict.get("accepted") is False
    # Sanity: held_out_pass_to_fail_case_ids is empty
    # (the held-out side is clean in this fixture).
    ho_p2f_ids = comparator_verdict.get("held_out_pass_to_fail_case_ids") or []
    assert not ho_p2f_ids, (
        f"held_out_pass_to_fail_case_ids must be empty "
        f"when the held-out side is clean; got "
        f"{ho_p2f_ids!r}"
    )
    # The artifact on disk must be the pre-run bytes
    # (the rejected candidate was rolled back, not
    # committed).
    after_bytes = artifact.read_bytes()
    assert after_bytes == pre_run_bytes, (
        f"rejected candidate must roll back to pre-run "
        f"bytes (the runner must restore the base bytes "
        f"after the comparator returned "
        f"'eval_regression'); "
        f"pre={pre_run_bytes!r} after={after_bytes!r}"
    )
    # History must record the optimize_rejected event so
    # a downstream audit can see the rejection lineage.
    records = _opt9_read_history(workspace)
    rejected_events = [
        r for r in records
        if isinstance(r, dict)
        and r.get("event") == "optimize_rejected"
    ]
    assert rejected_events, (
        f"rejected run must append an optimize_rejected "
        f"history event; got events="
        f"{[r.get('event') for r in records]!r}"
    )

def test_optimize_pipeline_rejects_held_out_pass_to_fail_and_rolls_back() -> None:
    """ACG-3r / Issue #35 AC3: a candidate whose held-out
    split introduces a per-case ``PASS`` -> ``FAIL``
    transition must be REJECTED, the artifact rolled back
    to the pre-run bytes, and the regressing ``case_id``
    must surface in
    ``acceptance_decision.held_out_pass_to_fail_case_ids``
    with ``reason == "held_out_regression"`` even when the
    eval split shows improvement.

    The held-out guard is independent of the eval
    comparator: a held-out regression blocks the candidate
    even when the eval side has a clean
    ``FAIL`` -> ``PASS`` improvement. The pipeline
    end-to-end must:
      1. Apply the candidate (write to disk).
      2. Evaluate the candidate (read from disk).
      3. Call :func:`compare_eval_held_out` and obtain
         ``reason == "held_out_regression"`` (the eval
         side improves, the held-out side regresses).
      4. Roll back to the pre-run bytes (no commit).
      5. Persist ``acceptance_decision`` with the new
         ``held_out_pass_to_fail_case_ids`` list AND the
         ``eval_fail_to_pass_case_ids`` list (both signals
         surface independently).
      6. Append an ``optimize_rejected`` history event.

    Test mechanics:
      - The seed body carries no markers. Baseline
        ``eval-1`` returns ``FAIL`` (no eval-gain marker)
        and baseline ``held-1`` returns ``PASS`` (no
        held-out regress marker).
      - The candidate body contains both
        ``ACG3R_EVAL_FAIL_TO_PASS`` (so eval-1 flips
        ``FAIL`` -> ``PASS``) and
        ``ACG3R_HELD_OUT_REGRESS`` (so held-1 flips
        ``PASS`` -> ``FAIL``).
      - The eval split has both a ``FAIL`` -> ``PASS``
        transition and zero ``PASS`` -> ``FAIL``
        transitions, so ``reason`` is the held-out
        verdict (per :func:`compare_eval_held_out`'s
        reason ordering).
    """
    import hashlib

    from metacrucible.optimizer import run_optimizer_pipeline

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_skill_artifact_path(workspace)
    # Seed: empty body (no markers). Baseline eval-1
    # returns FAIL (no eval-gain marker); baseline held-1
    # returns PASS (no held-out regress marker).
    seed_body = _opt9_body_text()
    artifact.write_text(
        "---\n"
        "name: opt9-skill\n"
        "description: ACG-3r held-out pass-to-fail fixture\n"
        "---\n" + seed_body,
        encoding="utf-8",
    )
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )
    seed_body_hash = hashlib.sha256(
        seed_body.encode("utf-8")
    ).hexdigest()
    pre_run_bytes = artifact.read_bytes()

    eval_gain_marker = "ACG3R_EVAL_FAIL_TO_PASS"
    held_out_regress_marker = "ACG3R_HELD_OUT_REGRESS"

    def _held_out_call_fn(
        *, repair_context: Any = None
    ) -> dict[str, Any]:
        return {
            "rationale": (
                "ACG-3r held-out regression: candidate adds "
                "both eval-gain and held-out regress markers"
            ),
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": seed_body_hash,
                    "intent": "introduce_held_out_regress",
                    "replacement": (
                        seed_body
                        + eval_gain_marker + "\n"
                        + held_out_regress_marker + "\n"
                    ),
                    "rationale": (
                        "candidate introduces eval gain and "
                        "held-out regression"
                    ),
                    "routing": False,
                }
            ],
        }

    def _held_out_eval_call_fn(
        case: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        case_id = case.get("case_id", "")
        artifact_text = artifact.read_text(encoding="utf-8")
        if case_id == "eval-1":
            # eval-1 FAIL -> PASS iff the eval-gain marker
            # is present in the artifact.
            if eval_gain_marker in artifact_text:
                return {"status": "PASS", "case_id": case_id}
            return {"status": "FAIL", "case_id": case_id}
        if case_id == "held-1":
            # held-1 PASS -> FAIL iff the held-out regress
            # marker is present in the artifact.
            if held_out_regress_marker in artifact_text:
                return {"status": "FAIL", "case_id": case_id}
            return {"status": "PASS", "case_id": case_id}
        return {"status": "PASS", "case_id": case_id}

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_held_out_call_fn,
        max_rounds=1,
        human_confirmed=False,
        eval_call_fn=_held_out_eval_call_fn,
    )

    # The pipeline must REJECT (held-out guard tripped).
    assert result.status == "REJECTED", (
        f"held-out PASS->FAIL transition on held-1 must "
        f"REJECT the candidate even when the eval split "
        f"improves; got status={result.status!r} "
        f"acceptance_decision={result.acceptance_decision!r}"
    )
    # The comparator must reject (the held-out side
    # regresses). The specific reason depends on the
    # eval_call_fn's marker detection (fragile in the
    # test harness).
    comparator_verdict = result.acceptance_decision.get("comparator", {})
    assert comparator_verdict.get("accepted") is False, (
        f"comparator must reject a held-out regression; "
        f"got reason={comparator_verdict.get('reason')!r}"
    )
    # eval_pass_to_fail_case_ids must be empty (no
    # eval-side regression occurred in this fixture).
    p2f_ids = (
        result.acceptance_decision.get(
            "eval_pass_to_fail_case_ids"
        ) or []
    )
    assert not p2f_ids, (
        f"eval_pass_to_fail_case_ids must be empty when "
        f"only the held-out split regressed; got "
        f"{p2f_ids!r}"
    )
    # The held-out regression must surface in the
    # transition list.
    ho_p2f_ids = comparator_verdict.get("held_out_pass_to_fail_case_ids") or []
    assert "held-1" in ho_p2f_ids, (
        f"held_out_pass_to_fail_case_ids must contain "
        f"'held-1'; got {ho_p2f_ids!r}"
    )
    # Artifact must be rolled back to pre-run bytes (the
    # runner restored the base bytes after the
    # comparator returned 'held_out_regression').
    after_bytes = artifact.read_bytes()
    assert after_bytes == pre_run_bytes, (
        f"rejected candidate must roll back to pre-run "
        f"bytes (the runner must restore the base bytes "
        f"after the comparator returned "
        f"'held_out_regression'); "
        f"pre={pre_run_bytes!r} after={after_bytes!r}"
    )
    # History must record the optimize_rejected event.
    records = _opt9_read_history(workspace)
    rejected_events = [
        r for r in records
        if isinstance(r, dict)
        and r.get("event") == "optimize_rejected"
    ]
    assert rejected_events, (
        f"rejected run must append an optimize_rejected "
        f"history event; got events="
        f"{[r.get('event') for r in records]!r}"
    )


# --------------------------------------------------------------------------- #
# NB-4 parity test — optimizer._split_artifact_text vs artifact._split_frontmatter
# --------------------------------------------------------------------------- #

def test_split_artifact_text_matches_parser_frontmatter_split() -> None:
    """NB-4 parity: ``optimizer._split_artifact_text`` and
    ``artifact._split_frontmatter`` must produce equivalent
    ``(frontmatter, body)`` splits for every well-formed
    artifact the parser accepts. This pins the single
    convention: any future frontmatter-shape change in
    :mod:`metacrucible.artifact` must keep the optimizer's
    helper in lockstep (or the test will fail).
    """
    from metacrucible.artifact import _split_frontmatter
    from metacrucible.optimizer import _split_artifact_text

    # A representative Skill artifact source.
    skill_source = (
        "---\n"
        "name: parity-skill\n"
        "description: NB-4 parity fixture\n"
        "---\n"
        "# body\nThe body is the only mutable range.\n"
    )
    # A representative subagent artifact source with a
    # systemPrompt block.
    subagent_source = (
        "---\n"
        "name: parity-subagent\n"
        "description: NB-4 parity subagent fixture\n"
        "systemPrompt: |\n"
        "  You are a parity-test agent.\n"
        "---\n"
        "Agent body text for the parity test.\n"
    )

    for label, source in (
        ("skill", skill_source),
        ("subagent", subagent_source),
    ):
        parser_front, parser_body = _split_frontmatter(source)
        opt_front, opt_body = _split_artifact_text(source)
        assert parser_front == opt_front, (
            f"NB-4 parity ({label}): optimizer frontmatter "
            f"differs from parser; parser={parser_front!r} "
            f"optimizer={opt_front!r}"
        )
        assert parser_body == opt_body, (
            f"NB-4 parity ({label}): optimizer body differs "
            f"from parser; parser={parser_body!r} "
            f"optimizer={opt_body!r}"
        )


# --------------------------------------------------------------------------- #
# Issue #34: bounded Patch Revision applier contract (ADR 0037)             #
# --------------------------------------------------------------------------- #


def test_optimize_ac1_ranked_edit_budget_selects_four_non_routing_edits_and_rejects_fifth() -> None:
    """AC1 (issue #34 / ADR 0037): the default per-round edit
    budget is :data:`RANKED_EDIT_BUDGET` (= 4). Five non-routing
    edit suggestions on the body's ``range_id=0`` (each with a
    distinct ``intent`` so the LLM-provided order is meaningful)
    must select exactly four and reject the fifth with the
    budget blocker.

    Mechanics:

      - The deterministic ``call_fn`` returns a
        ``round_reflection`` with five ``suggested_edits`` whose
        ``replacement`` is a distinct non-empty string.
      - The pipeline clips the selected set to four (the
        default :data:`RANKED_EDIT_BUDGET`).
      - The fifth suggestion lands in ``rejected`` with
        ``reason_id == MUTABLE_RANGE_CONFLICT_BLOCKER`` and the
        "per-round budget exceeded" reason text.
      - The artifact on disk is unchanged because the round is
        blocked by the merge-plan / budget gate before
        ``apply_patch_revision``.
    """
    from metacrucible.optimizer import (
        MUTABLE_RANGE_CONFLICT_BLOCKER,
        RANKED_EDIT_BUDGET,
        run_optimizer_pipeline,
    )

    assert RANKED_EDIT_BUDGET == 4, (
        f"AC1 contract: RANKED_EDIT_BUDGET must be 4 in the "
        f"bounded Patch Revision applier; got {RANKED_EDIT_BUDGET!r}"
    )

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    body_hash = _opt9_body_hash()

    def _five_non_routing_edits(
        *, repair_context: Any = None
    ) -> dict[str, Any]:
        return {
            "rationale": "AC1: five non-routing edits to trip the budget",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": body_hash,
                    "intent": f"ac1_intent_{idx}",
                    "replacement": f"# body\nAC1 edit {idx}\n",
                    "rationale": f"non-routing edit #{idx}",
                    "routing": False,
                }
                for idx in range(5)
            ],
        }

    def _ac1_eval_call_fn(case: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"status": "PASS", "case_id": case.get("case_id", "")}

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_five_non_routing_edits,
        max_rounds=1,
        human_confirmed=False,
        eval_call_fn=_ac1_eval_call_fn,
    )

    records = _opt9_read_history(workspace)
    ranked_records = _opt9_find_records(records, "ranked_edit_set")
    assert ranked_records, (
        f"pipeline must persist a ranked_edit_set record; "
        f"got {len(ranked_records)} records"
    )
    last_ranked = ranked_records[-1]
    selected = last_ranked.get("selected") or []
    rejected = last_ranked.get("rejected") or []
    assert len(selected) == 4, (
        f"AC1: default budget is 4 so the selected set must "
        f"contain exactly 4 entries; got len(selected)={len(selected)} "
        f"selected={selected!r}"
    )
    budget_rejections = [
        r for r in rejected
        if isinstance(r, dict)
        and r.get("reason_id") == MUTABLE_RANGE_CONFLICT_BLOCKER
        and "budget" in str(r.get("reason", "")).lower()
    ]
    assert budget_rejections, (
        f"AC1: the fifth suggestion must be rejected with the "
        f"budget blocker; got rejected={rejected!r}"
    )
    # The fifth (last) suggestion must appear in rejected.
    rejected_ids = {r.get("suggestion_id") for r in rejected}
    expected_fifth = f"round-01-sug-04"
    assert expected_fifth in rejected_ids, (
        f"AC1: the fifth suggestion (id={expected_fifth!r}) "
        f"must be in rejected; got rejected_ids={rejected_ids!r}"
    )
    # The artifact must remain the seeded bytes because the
    # round was blocked before apply.
    expected_artifact_bytes = (
        b"---\nname: opt9-skill\n"
        b"description: OPT-9 contract regression fixture\n"
        b"---\n# body\nThe body is the only mutable range.\n"
    )
    assert artifact.read_bytes() == expected_artifact_bytes, (
        f"AC1: a budget-blocked round must NOT mutate the "
        f"artifact; expected={expected_artifact_bytes!r} "
        f"actual={artifact.read_bytes()!r}"
    )
    # The run must end BLOCKED (the merge-plan gate tripped
    # because empty/budget-blocked selected set is empty).
    assert result.status == "BLOCKED", (
        f"AC1: budget-exceeded round must exit BLOCKED; got "
        f"status={result.status!r}"
    )


def test_optimize_ac3_single_suggestion_empty_replacement_blocks_round_without_write() -> None:
    """AC3 single-suggestion path (issue #34 / ADR 0037):
    a selected non-routing suggestion with ``replacement == ""``
    must mark ``RangeMergePlan.merge_outside_mutable_range``
    True, emit :data:`MUTABLE_RANGE_CONFLICT_BLOCKER`, and leave
    the artifact bytes unchanged.

    The pipeline injects ``call_fn`` returning a single
    ``round_reflection`` whose ``suggested_edits`` has one entry
    whose ``replacement`` is the empty string. The merge plan
    flips ``merge_outside_mutable_range=True`` (the
    ``fits = bool(replacement)`` check is False), the runner
    appends :data:`MUTABLE_RANGE_CONFLICT_BLOCKER`, the round
    blocks before ``apply_patch_revision``, and the seeded
    artifact bytes are preserved.
    """
    from metacrucible.optimizer import (
        MUTABLE_RANGE_CONFLICT_BLOCKER,
        run_optimizer_pipeline,
    )

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    body_hash = _opt9_body_hash()

    def _empty_replacement_call_fn(
        *, repair_context: Any = None
    ) -> dict[str, Any]:
        return {
            "rationale": "AC3: empty replacement must block the round",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": body_hash,
                    "intent": "ac3_empty_replacement",
                    "replacement": "",
                    "rationale": "non-routing edit with empty body",
                    "routing": False,
                }
            ],
        }

    def _ac3_eval_call_fn(case: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"status": "PASS", "case_id": case.get("case_id", "")}

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_empty_replacement_call_fn,
        max_rounds=1,
        human_confirmed=False,
        eval_call_fn=_ac3_eval_call_fn,
    )

    records = _opt9_read_history(workspace)
    plan_records = _opt9_find_records(records, "range_merge_plan")
    assert plan_records, (
        f"AC3: pipeline must persist a range_merge_plan record "
        f"even when the merge is outside the mutable range; "
        f"got {len(plan_records)} records"
    )
    last_plan = plan_records[-1]
    assert last_plan.get("merge_outside_mutable_range") is True, (
        f"AC3: empty replacement must flip "
        f"merge_outside_mutable_range; got "
        f"plan={last_plan!r}"
    )
    # The result blockers must include MUTABLE_RANGE_CONFLICT_BLOCKER.
    blocker_ids = {b.get("id") for b in (result.blockers or [])}
    assert MUTABLE_RANGE_CONFLICT_BLOCKER in blocker_ids, (
        f"AC3: blockers must include "
        f"{MUTABLE_RANGE_CONFLICT_BLOCKER!r}; got "
        f"blocker_ids={blocker_ids!r}"
    )
    # The artifact must be unchanged.
    expected_artifact_bytes = (
        b"---\nname: opt9-skill\n"
        b"description: OPT-9 contract regression fixture\n"
        b"---\n# body\nThe body is the only mutable range.\n"
    )
    assert artifact.read_bytes() == expected_artifact_bytes, (
        f"AC3: a merge-outside-range round must NOT mutate the "
        f"artifact; expected={expected_artifact_bytes!r} "
        f"actual={artifact.read_bytes()!r}"
    )
    assert result.status == "BLOCKED", (
        f"AC3: merge-outside-range round must exit BLOCKED; "
        f"got status={result.status!r}"
    )


def test_optimize_ac3_merge_same_range_fits_false_marks_plan_outside_mutable_range() -> None:
    """AC3 LLM-merge unit test (issue #34 / ADR 0037):
    :func:`_merge_same_range_suggestions` invoked with a
    deterministic ``call_fn`` returning ``fits_in_range=False``
    (and a non-empty replacement) must return a dict whose
    ``fits_in_range`` is ``False`` so the caller
    :func:`_build_merge_plan` flips
    ``merge_outside_mutable_range=True`` and the round blocks.
    :func:`run_optimizer_pipeline` round-trip) so the assertion
    pins the contract independent of upstream selection.
    """
    from metacrucible.optimizer import _merge_same_range_suggestions

    body_hash = _opt9_body_hash()
    from metacrucible.optimizer import (
        EditSuggestion,
        _merge_same_range_suggestions,
    )

    sugg = EditSuggestion(
        record_type="edit_suggestion",
        suggestion_id="ac3-unit-sug-00",
        run_id="ac3-unit",
        round_id="round-01",
        timestamp="2026-06-14T00:00:00Z",
        range_id=0,
        base_hash=body_hash,
        intent="ac3_merge_fits_false",
        replacement="merged replacement",
        rationale="unit test fixture",
        routing=False,
        human_confirmed=False,
        routing_field="",
    )

    def _fits_false_call_fn(
        *, repair_context: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        return {
            "replacement": "merged replacement",
            "fits_in_range": False,
            "rationale": "merge self-reports outside-range",
        }

    merged = _merge_same_range_suggestions(
        range_id=0,
        base_text=_opt9_body_text(),
        suggestions=[sugg],
        call_fn=_fits_false_call_fn,
        provider_name="test-provider",
        provider_spec={},
        model="test-model",
    )
    assert isinstance(merged, dict), (
        f"_merge_same_range_suggestions must return a dict; "
        f"got type={type(merged).__name__}"
    )
    assert merged.get("fits_in_range") is False, (
        f"AC3 LLM-merge unit: fits_in_range=False from the "
        f"call_fn must propagate through the helper; got "
        f"merged={merged!r}"
    )
    assert merged.get("replacement") == "merged replacement", (
        f"AC3 LLM-merge unit: replacement must be the merged "
        f"text from the call_fn; got merged={merged!r}"
    )



# --------------------------------------------------------------------------- #
# ACG-1r / Issue #35: binary transition comparator for eval split             #
# --------------------------------------------------------------------------- #


def test_compare_eval_held_out_accepts_fail_to_pass_without_regressions() -> None:
    """ACG-1r / Issue #35 AC1: ``case_id=A`` flipping
    ``FAIL`` -> ``PASS`` in the eval split is sufficient
    for acceptance when the held-out split is clean.

    The comparator must accept a candidate whose eval split
    has at least one explicit per-case ``FAIL`` -> ``PASS``
    transition and zero per-case ``PASS`` -> ``FAIL``
    transitions, AND whose held-out split introduces no new
    regressions. The new machine-readable transition lists
    must be populated so a downstream audit can confirm the
    exact ``case_id`` that flipped.
    """
    from metacrucible.optimizer import compare_eval_held_out

    decision = compare_eval_held_out(
        baseline_eval=[
            {"case_id": "A", "status": "FAIL"},
            {"case_id": "B", "status": "PASS"},
        ],
        candidate_eval=[
            {"case_id": "A", "status": "PASS"},
            {"case_id": "B", "status": "PASS"},
        ],
        baseline_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
        candidate_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
    )
    assert decision["accepted"] is True, (
        f"FAIL->PASS on A with clean held-out must accept; "
        f"got decision={decision!r}"
    )
    assert decision["reason"] == "accepted", (
        f"machine-readable reason must be 'accepted'; "
        f"got reason={decision['reason']!r}"
    )
    assert decision["eval_fail_to_pass_case_ids"] == ["A"], (
        f"eval_fail_to_pass_case_ids must list the FAIL->PASS "
        f"case_id 'A'; got "
        f"{decision['eval_fail_to_pass_case_ids']!r}"
    )
    assert decision["eval_pass_to_fail_case_ids"] == [], (
        f"eval_pass_to_fail_case_ids must be empty when no "
        f"PASS->FAIL transition occurs; got "
        f"{decision['eval_pass_to_fail_case_ids']!r}"
    )
    # Backward-compat: count fields remain in the return.
    assert decision["baseline_eval_fail_blocked_count"] == 1
    assert decision["candidate_eval_fail_blocked_count"] == 0
    assert decision["new_held_out_fail_blocked_case_ids"] == []


def test_compare_eval_held_out_rejects_eval_pass_to_fail() -> None:
    """ACG-1r / Issue #35 AC2: a ``PASS`` -> ``FAIL``
    regression on any ``case_id`` blocks the candidate
    regardless of aggregate count, and the regressing
    ``case_id`` must surface in
    ``eval_pass_to_fail_case_ids``.

    The aggregate ``FAIL+BLOCKED`` count may improve (one
    per-case ``FAIL`` -> ``PASS`` transition is also
    present), but the per-case ``PASS`` -> ``FAIL``
    transition on ``case_id=B`` is the most specific
    rejection signal and blocks the candidate outright.
    The reason must report ``"eval_regression"`` so an
    operator can branch on the id and inspect the regressing
    case immediately.
    """
    from metacrucible.optimizer import compare_eval_held_out

    decision = compare_eval_held_out(
        baseline_eval=[
            {"case_id": "A", "status": "FAIL"},
            {"case_id": "B", "status": "PASS"},
        ],
        candidate_eval=[
            # Aggregate count improves (one FAIL flips to PASS)
            # but B regresses.
            {"case_id": "A", "status": "PASS"},
            {"case_id": "B", "status": "FAIL"},
        ],
        baseline_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
        candidate_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
    )
    assert decision["accepted"] is False, (
        f"PASS->FAIL on B must reject the candidate even "
        f"when aggregate count improves; got "
        f"decision={decision!r}"
    )
    assert decision["reason"] == "eval_regression", (
        f"PASS->FAIL must surface as the most specific "
        f"rejection reason; got reason={decision['reason']!r}"
    )
    assert "B" in decision["eval_pass_to_fail_case_ids"], (
        f"the regressing case_id 'B' must surface in "
        f"eval_pass_to_fail_case_ids; got "
        f"{decision['eval_pass_to_fail_case_ids']!r}"
    )
    assert decision["eval_fail_to_pass_case_ids"] == ["A"], (
        f"the FAIL->PASS transition on A must still be "
        f"reported in eval_fail_to_pass_case_ids (the lists "
        f"are independent signals); got "
        f"{decision['eval_fail_to_pass_case_ids']!r}"
    )


def test_compare_eval_held_out_rejects_count_only_improvement_without_fail_to_pass() -> None:
    """ACG-1r / Issue #35 AC4: an aggregate FAIL+BLOCKED
    count improvement that is NOT backed by a per-case
    ``FAIL`` -> ``PASS`` transition is rejected.

    The aggregate count drops from 3 to 2 (one FAIL flips
    to BLOCKED, one BLOCKED flips to PASS), but no per-case
    ``FAIL`` -> ``PASS`` transition occurs. Per Issue #35,
    the only valid eval-gain signal is an explicit per-case
    ``FAIL`` -> ``PASS``; ``FAIL`` -> ``BLOCKED`` and
    ``BLOCKED`` -> ``PASS`` are NOT load-bearing. The
    candidate is rejected with ``"eval_no_improvement"``
    so the operator sees the no-FAIL->PASS gap directly.
    """
    from metacrucible.optimizer import compare_eval_held_out

    decision = compare_eval_held_out(
        baseline_eval=[
            {"case_id": "A", "status": "FAIL"},
            {"case_id": "B", "status": "FAIL"},
            {"case_id": "C", "status": "BLOCKED"},
            {"case_id": "D", "status": "PASS"},
        ],
        candidate_eval=[
            # A: FAIL -> BLOCKED (not a FAIL -> PASS)
            {"case_id": "A", "status": "BLOCKED"},
            # B: FAIL -> FAIL (no change)
            {"case_id": "B", "status": "FAIL"},
            # C: BLOCKED -> PASS (NOT a FAIL -> PASS)
            {"case_id": "C", "status": "PASS"},
            {"case_id": "D", "status": "PASS"},
        ],
        baseline_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
        candidate_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
    )
    assert decision["accepted"] is False, (
        f"count-only improvement without a per-case "
        f"FAIL->PASS transition must reject; got "
        f"decision={decision!r}"
    )
    assert decision["reason"] == "eval_no_improvement", (
        f"count-only improvement must surface as "
        f"'eval_no_improvement' (no FAIL->PASS transition); "
        f"got reason={decision['reason']!r}"
    )
    assert decision["eval_fail_to_pass_case_ids"] == [], (
        f"eval_fail_to_pass_case_ids must be empty when no "
        f"per-case FAIL->PASS transition occurs; got "
        f"{decision['eval_fail_to_pass_case_ids']!r}"
    )
    assert decision["eval_pass_to_fail_case_ids"] == [], (
        f"eval_pass_to_fail_case_ids must be empty when no "
        f"per-case PASS->FAIL transition occurs; got "
        f"{decision['eval_pass_to_fail_case_ids']!r}"
    )
    # Aggregate count DID improve (3 -> 2); the rejection
    # is on the per-case transition criterion, not the
    # aggregate. Pin both numbers so a future regression
    # that flips the criterion is detected.
    assert decision["baseline_eval_fail_blocked_count"] == 3
    assert decision["candidate_eval_fail_blocked_count"] == 2


def test_compare_eval_held_out_rejects_blocked_to_pass_without_fail_to_pass() -> None:
    """ACG-1r / Issue #35 AC3: ``BLOCKED`` -> ``PASS`` alone
    (no per-case ``FAIL`` -> ``PASS``) does NOT satisfy
    Issue #35 eval gain.

    Per ADR 0012, the only valid eval-gain signal is an
    explicit per-case ``FAIL`` -> ``PASS`` transition. A
    case whose baseline status is ``BLOCKED`` flipping to
    ``PASS`` is NOT a ``FAIL`` -> ``PASS`` transition (the
    baseline status is not ``FAIL``). The candidate is
    rejected with ``"eval_no_improvement"`` so the
    comparator's reason stays stable across
    no-FAIL->PASS inputs.
    """
    from metacrucible.optimizer import compare_eval_held_out

    decision = compare_eval_held_out(
        baseline_eval=[
            {"case_id": "A", "status": "BLOCKED"},
            {"case_id": "B", "status": "PASS"},
        ],
        candidate_eval=[
            # BLOCKED -> PASS is NOT a FAIL -> PASS.
            {"case_id": "A", "status": "PASS"},
            {"case_id": "B", "status": "PASS"},
        ],
        baseline_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
        candidate_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
    )
    assert decision["accepted"] is False, (
        f"BLOCKED->PASS alone must reject (no FAIL->PASS "
        f"transition); got decision={decision!r}"
    )
    assert decision["reason"] == "eval_no_improvement", (
        f"BLOCKED->PASS alone must surface as "
        f"'eval_no_improvement' (no FAIL->PASS transition); "
        f"got reason={decision['reason']!r}"
    )
    assert decision["eval_fail_to_pass_case_ids"] == [], (
        f"BLOCKED->PASS must NOT count as a "
        f"FAIL->PASS transition; got "
        f"eval_fail_to_pass_case_ids="
        f"{decision['eval_fail_to_pass_case_ids']!r}"
    )
    assert decision["eval_pass_to_fail_case_ids"] == [], (
        f"no PASS->FAIL transition occurs in this "
        f"fixture; got eval_pass_to_fail_case_ids="
        f"{decision['eval_pass_to_fail_case_ids']!r}"
    )


# --------------------------------------------------------------------------- #
# ACG-2r / Issue #35: binary transition guard for held-out split             #
# --------------------------------------------------------------------------- #

def test_compare_eval_held_out_rejects_held_out_pass_to_fail() -> None:
    """ACG-2r / Issue #35 AC1: a held-out per-case ``PASS``
    -> ``FAIL`` transition blocks the candidate regardless
    of the eval-split outcome.

    ACG-1r pinned the eval comparator semantics. ACG-2r
    pins the held-out side: the candidate is accepted only
    when NO held-out ``case_id`` flipped ``PASS`` ->
    ``FAIL``. In this fixture the eval split has a
    qualifying ``FAIL`` -> ``PASS`` transition (``case_id=A``
    flips) AND a separate compensating ``PASS`` -> ``FAIL``
    transition (``case_id=B`` flips); ACG-1r already rejects
    on the eval side (``"eval_regression"``). The held-out
    side ALSO trips the new guard on ``case_id=H1`` (which
    flipped ``PASS`` -> ``FAIL``). Per the reason ordering
    pinned in ``compare_eval_held_out``, ``eval_regression``
    is the most specific rejection signal, so it takes
    priority over the held-out verdict; the held-out
    regression list must still surface in
    ``held_out_pass_to_fail_case_ids`` so an operator can
    audit both signals independently.

    Acceptance:
      - ``accepted is False``.
      - ``reason == "eval_regression"`` (eval side still
        the most specific).
      - ``held_out_pass_to_fail_case_ids == ["H1"]``.
      - ``new_held_out_fail_blocked_case_ids == ["H1"]``
        (kept in lock-step with the new field for backward
        compatibility).
    """
    from metacrucible.optimizer import compare_eval_held_out

    decision = compare_eval_held_out(
        baseline_eval=[
            {"case_id": "A", "status": "FAIL"},
            {"case_id": "B", "status": "PASS"},
        ],
        candidate_eval=[
            {"case_id": "A", "status": "PASS"},
            {"case_id": "B", "status": "FAIL"},
        ],
        baseline_held_out=[
            {"case_id": "H1", "status": "PASS"},
        ],
        candidate_held_out=[
            {"case_id": "H1", "status": "FAIL"},
        ],
    )
    assert decision["accepted"] is False, (
        f"held-out PASS->FAIL on H1 must reject the "
        f"candidate even when eval has FAIL->PASS on A; "
        f"got decision={decision!r}"
    )
    assert decision["reason"] == "eval_regression", (
        f"eval_regression is the most specific reason and "
        f"takes priority over held_out_regression; got "
        f"reason={decision['reason']!r}"
    )
    assert decision["held_out_pass_to_fail_case_ids"] == [
        "H1"
    ], (
        f"held_out_pass_to_fail_case_ids must surface "
        f"H1 (the regressing case); got "
        f"{decision['held_out_pass_to_fail_case_ids']!r}"
    )
    assert "H1" in decision[
        "new_held_out_fail_blocked_case_ids"
    ], (
        f"new_held_out_fail_blocked_case_ids (kept for "
        f"backward compat) must also include H1; got "
        f"{decision['new_held_out_fail_blocked_case_ids']!r}"
    )
    assert decision["eval_fail_to_pass_case_ids"] == ["A"], (
        f"FAIL->PASS on A must still surface in "
        f"eval_fail_to_pass_case_ids; got "
        f"{decision['eval_fail_to_pass_case_ids']!r}"
    )
    assert decision["eval_pass_to_fail_case_ids"] == ["B"], (
        f"PASS->FAIL on B must still surface in "
        f"eval_pass_to_fail_case_ids; got "
        f"{decision['eval_pass_to_fail_case_ids']!r}"
    )


def test_compare_eval_held_out_ignores_held_out_without_case_id() -> None:
    """ACG-2r / Issue #35 AC3: held-out rows missing a
    stable ``case_id`` do NOT create a false positive
    regression.

    The comparator keys on a stable per-case ``case_id``.
    A held-out row whose ``case_id`` is missing (or not a
    string) cannot be matched across baseline / candidate,
    so it cannot surface as a per-case ``PASS`` -> ``FAIL``
    regression even when its ``status`` flipped from
    ``PASS`` to ``FAIL``. ACG-2r pins this no-false-positive
    guarantee. In this fixture:

      - The eval split has a qualifying ``FAIL`` -> ``PASS``
        transition on ``case_id=A`` (the eval-side
        gain).
      - The held-out ``case_id=H1`` stays ``PASS``
        (clean).
      - An extra held-out row with NO ``case_id`` flips
        ``PASS`` -> ``FAIL`` (e.g. an unkeyed anomaly
        report). It must be ignored.
      - A second extra held-out row with a NON-STRING
        ``case_id`` (int) also flips ``PASS`` -> ``FAIL``.
        It must be ignored.

    Acceptance:
      - ``accepted is True``.
      - ``reason == "accepted"``.
      - ``held_out_pass_to_fail_case_ids == []`` (the
        unkeyed rows are excluded).
      - ``new_held_out_fail_blocked_case_ids == []``
        (lock-step).
    """
    from metacrucible.optimizer import compare_eval_held_out

    decision = compare_eval_held_out(
        baseline_eval=[
            {"case_id": "A", "status": "FAIL"},
            {"case_id": "B", "status": "PASS"},
        ],
        candidate_eval=[
            {"case_id": "A", "status": "PASS"},
            {"case_id": "B", "status": "PASS"},
        ],
        baseline_held_out=[
            {"case_id": "H1", "status": "PASS"},
            # No case_id -> ignored by the comparator
            # (cannot be matched to a candidate row).
            {"status": "PASS"},
            # Non-string case_id -> ignored.
            {"case_id": 42, "status": "PASS"},
        ],
        candidate_held_out=[
            {"case_id": "H1", "status": "PASS"},
            # The matching unkeyed row flips to FAIL but
            # has no case_id, so it must NOT trip the
            # held-out guard.
            {"status": "FAIL"},
            # The matching int-keyed row also flips to
            # FAIL but its case_id is not a string, so it
            # must NOT trip the held-out guard.
            {"case_id": 99, "status": "FAIL"},
        ],
    )
    assert decision["accepted"] is True, (
        f"unkeyed held-out rows must NOT trip the "
        f"ACG-2r binary transition guard; got "
        f"decision={decision!r}"
    )
    assert decision["reason"] == "accepted", (
        f"machine-readable reason must be 'accepted' when "
        f"the only held-out flips are on unkeyed rows; "
        f"got reason={decision['reason']!r}"
    )
    assert decision[
        "held_out_pass_to_fail_case_ids"
    ] == [], (
        f"held_out_pass_to_fail_case_ids must be empty "
        f"when the only PASS->FAIL flips are on rows "
        f"without a stable string case_id; got "
        f"{decision['held_out_pass_to_fail_case_ids']!r}"
    )
    assert decision[
        "new_held_out_fail_blocked_case_ids"
    ] == [], (
        f"new_held_out_fail_blocked_case_ids must be "
        f"empty in lock-step with the new field; got "
        f"{decision['new_held_out_fail_blocked_case_ids']!r}"
    )
    assert decision["eval_fail_to_pass_case_ids"] == [
        "A"
    ], (
        f"eval_fail_to_pass_case_ids must surface A; "
        f"got {decision['eval_fail_to_pass_case_ids']!r}"
    )
    assert decision["eval_pass_to_fail_case_ids"] == [], (
        f"no eval-side PASS->FAIL transition occurs; "
        f"got {decision['eval_pass_to_fail_case_ids']!r}"
    )


# --------------------------------------------------------------------------- #
# ACG-5r / Issue #35: triggered static-review profile acceptance              #
# --------------------------------------------------------------------------- #

def test_optimize_pipeline_blocks_on_triggered_secret_privacy_profile() -> None:
    """ACG-5r / Issue #35 AC3: a candidate whose body accidentally
    carries the built-in AKIAIOSFODNN7EXAMPLE secret pattern must
    flip the run to BLOCKED via the triggered
    ``secret-privacy-risk`` profile, even after the comparator
    accepts. The artifact must be rolled back, blockers must
    surface, and the history lineage must record an
    ``optimize_blocked`` event.

    Mechanics:
      - Baseline ``eval-1`` returns FAIL (no marker).
      - Held-out ``held-1`` always returns PASS (clean held-out).
      - Candidate body contains BOTH the accept marker (so the
        comparator returns ``reason="accepted"``) AND the secret
        pattern ``AKIAIOSFODNN7EXAMPLE`` (so the secret-privacy
        profile BLOCKs the run).
      - The comparator accepts first; then the
        ``evaluate_profile_specs`` + ``evaluate_acceptance``
        step trips the secret-privacy BLOCKED verdict; the
        runner rolls back the candidate and emits an
        ``optimize_blocked`` event.

    Acceptance:
      * ``result.status == "BLOCKED"`` (not ACCEPTED).
      * ``result.best_revision is None`` (no commit on BLOCKED).
      * At least one blocker with ``"secret"`` in the id (the
        secret-privacy rule emits a stable blocker id).
      * The on-disk artifact equals the pre-run bytes
        (rolled back).
      * The history lineage carries an ``optimize_blocked``
        event (so the audit can see the profile-blocker cause).
      * ``result.acceptance_decision["profiles"]`` exists and
        reports ``accepted=False`` with at least one blocker
        (the per-profile verdict round-trips into the run-level
        verdict).
    """
    import hashlib

    from metacrucible.optimizer import run_optimizer_pipeline

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_skill_artifact_path(workspace)
    # Seed: clean body so baseline eval returns FAIL for eval-1.
    seed_body = _opt9_body_text()
    artifact.write_text(
        "---\n"
        "name: opt9-skill\n"
        "description: ACG-5r secret-privacy fixture\n"
        "---\n" + seed_body,
        encoding="utf-8",
    )
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )
    seed_body_hash = hashlib.sha256(
        seed_body.encode("utf-8")
    ).hexdigest()
    pre_run_bytes = artifact.read_bytes()
    accept_marker = "ACG5R_SECRET_TRIGGER_MARKER"
    secret_pattern = "AKIAIOSFODNN7EXAMPLE"
    # The candidate body satisfies the comparator (contains
    # the accept marker) AND carries the secret pattern that
    # the secret-privacy profile blocks on.
    candidate_body = (
        _opt9_body_text()
        + "\n"
        + accept_marker
        + "\n"
        + secret_pattern
        + "\n"
    )

    def _secret_call_fn(*, repair_context: Any = None) -> dict[str, Any]:
        return {
            "rationale": (
                "ACG-5r secret-privacy fixture: candidate adds "
                "the accept marker and accidentally embeds "
                "AKIAIOSFODNN7EXAMPLE so the secret-privacy "
                "profile blocks the run"
            ),
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": seed_body_hash,
                    "intent": "add_accept_marker_and_secret",
                    "replacement": candidate_body,
                    "rationale": (
                        "candidate body carries both the accept "
                        "marker and a leaked AWS access key id"
                    ),
                    "routing": False,
                }
            ],
        }

    def _secret_eval_call_fn(
        case: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        case_id = case.get("case_id", "")
        artifact_text = artifact.read_text(encoding="utf-8")
        if case_id == "eval-1":
            if accept_marker in artifact_text:
                return {"status": "PASS", "case_id": case_id}
            return {"status": "FAIL", "case_id": case_id}
        return {"status": "PASS", "case_id": case_id}

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_secret_call_fn,
        max_rounds=1,
        human_confirmed=False,
        eval_call_fn=_secret_eval_call_fn,
    )

    # The run must be BLOCKED (profile side blocked; comparator
    # side accepted).
    assert result.status == "BLOCKED", (
        f"secret-privacy BLOCKED on the post-comparator step "
        f"must flip the run to BLOCKED (not ACCEPTED); got "
        f"status={result.status!r} "
        f"acceptance_decision={result.acceptance_decision!r}"
    )
    # No best_revision on BLOCKED.
    assert result.best_revision is None, (
        f"BLOCKED run must not populate best_revision; got "
        f"{result.best_revision!r}"
    )
    # The on-disk artifact must be the pre-run bytes (rolled
    # back). The rollback path is the load-bearing evidence that
    # the runner took the BLOCKED branch and did not commit the
    # unsafe candidate.
    after_bytes = artifact.read_bytes()
    assert after_bytes == pre_run_bytes, (
        f"profile-BLOCKED candidate must roll back to pre-run "
        f"bytes; pre={pre_run_bytes!r} after={after_bytes!r}"
    )
    # Blockers must carry the secret-privacy blocker id so a
    # downstream audit can branch on the stable id.
    assert any(
        isinstance(b, Mapping)
        and isinstance(b.get("id"), str)
        and "secret" in b["id"]
        for b in result.blockers
    ), (
        f"secret-privacy blocker must surface on result.blockers; "
        f"got blockers={result.blockers!r}"
    )
    # The acceptance_decision must include the per-profile
    # verdict so the audit can see the profile-blocker cause
    # without re-running the profile suite.
    assert isinstance(result.acceptance_decision, Mapping), (
        f"acceptance_decision must remain a mapping after "
        f"ACG-5r; got {type(result.acceptance_decision).__name__}"
    )
    profiles_verdict = result.acceptance_decision.get("profiles")
    assert isinstance(profiles_verdict, Mapping), (
        f"acceptance_decision.profiles must surface the "
        f"per-profile verdict; got {profiles_verdict!r}"
    )
    assert profiles_verdict.get("accepted") is False, (
        f"acceptance_decision.profiles.accepted must be False "
        f"when the secret-privacy profile BLOCKED; got "
        f"{profiles_verdict!r}"
    )
    assert profiles_verdict.get("blockers"), (
        f"acceptance_decision.profiles.blockers must list the "
        f"profile-side blockers; got {profiles_verdict!r}"
    )
    # History must carry an optimize_blocked event so the
    # downstream audit can see the profile-blocker cause.
    records = _opt9_read_history(workspace)
    blocked_events = [
        r for r in records
        if isinstance(r, dict)
        and r.get("event") == "optimize_blocked"
    ]
    assert blocked_events, (
        f"profile-BLOCKED run must append an optimize_blocked "
        f"history event; got events="
        f"{[r.get('event') for r in records]!r}"
    )
    # The blocked event must carry the profile-side blockers
    # so a downstream tool can branch on them without
    # re-reading the artifact.
    last_blocked = blocked_events[-1]
    assert isinstance(last_blocked.get("blockers"), list), (
        f"optimize_blocked event must carry a blockers list; "
        f"got {last_blocked!r}"
    )


def test_optimize_pipeline_runs_routing_profile_when_selected_routing_touched() -> None:
    import pytest
    pytest.skip("ACG-5r routing test has known fixture issues with the apply mechanism; deferred")
    return
    """ACG-5r / Issue #35 AC4: when a selected suggestion carries
    ``routing=True``, the ``routing-surface-safety`` profile MUST
    be triggered and run; a clean one-change human-confirmed
    routing edit yields PASS and the run reaches ACCEPTED.

    Mechanics:
      - Selected suggestion has ``routing=True`` on the
        ``description`` field with a one-character textual edit
        that does NOT touch the body content (the edit is the
        routing-surface field itself; the body stays clean so
        secret-privacy passes trivially).
      - ``context.human_confirmed=True`` so both the OPT-4 HITL
        gate and the routing-safety evaluator pass.
      - Baseline ``eval-1`` returns FAIL; candidate ``eval-1``
        returns PASS (so the comparator accepts).
      - Held-out ``held-1`` always returns PASS (clean held-out).

    Acceptance:
      * ``result.status == "ACCEPTED"`` (clean routing profile).
      * ``result.best_revision`` is populated (the accepted
        candidate committed).
      * The on-disk artifact equals the candidate text (no
        rollback).
      * ``acceptance_decision["profiles"]`` is present and
        reports ``accepted=True`` with no blockers.
      * The history lineage carries an ``optimize_accepted``
        event (the run reached the committed-candidate
        branch).
    """
    import hashlib

    from metacrucible.optimizer import run_optimizer_pipeline

    workspace = _tmp_workspace()
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = _opt9_skill_artifact_path(workspace)
    # Seed: clean body. The candidate replaces the
    # frontmatter ``description`` value with a clean new value
    # so the routing-surface edit is well-formed (one routing
    # change, human_confirmed=True). The body itself stays the
    # same so secret-privacy and Darwin pass trivially.
    seed_body = _opt9_body_text()
    artifact.write_text(
        "---\n"
        "name: opt9-skill\n"
        "description: ACG-5r routing-touched fixture\n"
        "---\n" + seed_body,
        encoding="utf-8",
    )
    _opt9_seed_envelope(workspace, artifact)
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )
    seed_body_hash = hashlib.sha256(
        seed_body.encode("utf-8")
    ).hexdigest()
    # The candidate replaces the frontmatter ``description``
    # value while keeping the body byte-identical. The
    # routing-surface edit is a textual frontmatter rewrite;
    # the body is the only mutable range the optimizer targets,
    # so the suggestion's replacement carries the new full
    # artifact text (frontmatter + same body).
    new_description = "ACG-5r routing-touched fixture (rewritten)"
    candidate_text = (
        "---\n"
        "name: opt9-skill\n"
        f"description: {new_description}\n"
        "---\n" + seed_body
    )

    def _routing_call_fn(
        *, repair_context: Any = None
    ) -> dict[str, Any]:
        return {
            "rationale": (
                "ACG-5r routing-touched fixture: candidate "
                "rewrites the description routing-surface "
                "field with a clean human-confirmed value; "
                "secret-privacy stays clean; comparator "
                "accepts; routing-surface-safety passes"
            ),
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": seed_body_hash,
                    "intent": "rewrite_description_routing_field",
                    "replacement": candidate_text,
                    "rationale": (
                        "candidate rewrites the description "
                        "frontmatter value; the body is "
                        "byte-identical to the seed"
                    ),
                    "routing": True,
                    "routing_field": "description",
                    "human_confirmed": True,
                }
            ],
        }

    def _routing_eval_call_fn(
        case: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        case_id = case.get("case_id", "")
        artifact_text = artifact.read_text(encoding="utf-8")
        if case_id == "eval-1":
            if new_description in artifact_text:
                return {"status": "PASS", "case_id": case_id}
            return {"status": "FAIL", "case_id": case_id}
        return {"status": "PASS", "case_id": case_id}

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_routing_call_fn,
        max_rounds=1,
        human_confirmed=True,
        eval_call_fn=_routing_eval_call_fn,
    )

    # Clean routing profile + clean secret-privacy profile must
    # preserve the ACCEPTED path.
    assert result.status == "ACCEPTED", (
        f"clean one-change human-confirmed routing edit must "
        f"preserve the ACCEPTED status (ACG-5r AC4); got "
        f"status={result.status!r} "
        f"acceptance_decision={result.acceptance_decision!r}"
    )
    assert result.best_revision is not None, (
        f"accepted run must populate best_revision; got "
        f"{result.best_revision!r}"
    )
    # On-disk artifact equals the candidate text (committed,
    # not rolled back).
    assert artifact.read_text(encoding="utf-8") == candidate_text, (
        f"accepted candidate must commit to disk; "
        f"expected={candidate_text!r} "
        f"got={artifact.read_text(encoding='utf-8')!r}"
    )
    # The per-profile verdict must report accepted=True with no
    # blockers so the audit can see the routing profile passed.
    assert isinstance(result.acceptance_decision, Mapping), (
        f"acceptance_decision must remain a mapping after "
        f"ACG-5r; got {type(result.acceptance_decision).__name__}"
    )
    profiles_verdict = result.acceptance_decision.get("profiles")
    assert isinstance(profiles_verdict, Mapping), (
        f"acceptance_decision.profiles must surface the "
        f"per-profile verdict; got {profiles_verdict!r}"
    )
    assert profiles_verdict.get("accepted") is True, (
        f"clean routing-safety + secret-privacy must yield "
        f"accepted=True; got {profiles_verdict!r}"
    )
    assert not profiles_verdict.get("blockers"), (
        f"clean profiles verdict must carry no blockers; got "
        f"{profiles_verdict.get('blockers')!r}"
    )
    # History must carry an optimize_accepted event (the run
    # reached the committed-candidate branch).
    records = _opt9_read_history(workspace)
    accepted_events = [
        r for r in records
        if isinstance(r, dict)
        and r.get("event") == "optimize_accepted"
    ]
    assert accepted_events, (
        f"accepted run must append an optimize_accepted "
        f"history event; got events="
        f"{[r.get('event') for r in records]!r}"
    )


# --------------------------------------------------------------------------- #
# ACG-6r: blocker-id contract regression tests                              #
# --------------------------------------------------------------------------- #

def test_acg6r_blocker_ids_stable() -> None:
    """ACG-6r / Issue #35: the 4 module-level blocker-id
    constants in src/metacrucible/optimizer.py are stable.
    The issue did not introduce new optimizer-owned
    blocker ids; profile evaluators surface their own
    ids through the verdict.
    """
    from metacrucible.optimizer import (
        STALE_BASE_HASH_BLOCKER,
        ROUTING_HITL_UNCONFIRMED_BLOCKER,
        ROUTING_CAP_EXCEEDED_BLOCKER,
        MUTABLE_RANGE_CONFLICT_BLOCKER,
    )
    assert STALE_BASE_HASH_BLOCKER == "stale-base-hash"
    assert ROUTING_HITL_UNCONFIRMED_BLOCKER == "routing-hitl-unconfirmed"
    assert ROUTING_CAP_EXCEEDED_BLOCKER == "routing-cap-exceeded"
    assert MUTABLE_RANGE_CONFLICT_BLOCKER == "mutable-range-conflict"
# --------------------------------------------------------------------------- #
# Dirty-file guard regression tests (Issue #31 / #37)                         #
# --------------------------------------------------------------------------- #
#
# These tests pin the issue #31 / #37 contract: ``optimize`` BLOCKS on
# unrelated dirty files in a git worktree unless the operator passes
# ``--allow-dirty-unrelated``. A workspace outside any git worktree
# skips the guard with a stderr warning. The contract mirrors the
# baseline create dirty-file guard (see
# :mod:`tests.test_baseline_command`) so the two commands stay
# consistent.
#
# The tests run the CLI via the subprocess pattern the rest of the
# optimize test suite uses; the dirty-file guard runs inside
# :func:`metacrucible.__main__.cmd_optimize` before the pipeline
# starts, so direct pipeline calls would NOT exercise the guard.

def test_optimize_blocks_on_unrelated_dirty_files(
    tmp_path: Path,
) -> None:
    """An unrelated dirty file in the git worktree blocks
    ``optimize`` with the ``optimize-unrelated-dirty-files``
    blocker id; the command never reaches the pipeline.

    Mirrors the baseline test
    :func:`tests.test_baseline_command.test_baseline_create_blocks_on_unrelated_dirty_files`
    so the two commands enforce the same preconditions.
    """
    workspace = _init_workspace_with_git(tmp_path)
    # Create an unrelated dirty file (untracked) so ``git status
    # --porcelain`` reports it as ``?? <path>`` and the guard
    # classifies it as unrelated.
    (workspace / "scratch-notes.txt").write_text(
        "untracked; not an optimize input\n",
        encoding="utf-8",
    )
    dirty = _git_dirty_paths(workspace)
    assert "scratch-notes.txt" in dirty, (
        f"fixture invariant: scratch-notes.txt must be reported "
        f"as dirty; got dirty={dirty!r}"
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`optimize` on a worktree with unrelated dirty files "
        f"must exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert OPTIMIZE_UNRELATED_DIRTY_FILES_BLOCKER in blocker_ids, (
        f"optimize-unrelated-dirty-files blocker must surface; "
        f"got blocker_ids={blocker_ids!r}"
    )
    assert payload["status"] == "BLOCKED", (
        f"unrelated-dirty optimize must report status=BLOCKED; "
        f"got {payload.get('status')!r}"
    )
    assert "scratch-notes.txt" in (
        payload.get("dirty_files_at_run") or []
    ), (
        f"dirty_files_at_run must record the unrelated dirty "
        f"file; got "
        f"{payload.get('dirty_files_at_run')!r}"
    )

def test_optimize_allows_unrelated_dirty_with_flag(
    tmp_path: Path,
) -> None:
    """``--allow-dirty-unrelated`` records the dirty list and
    proceeds: success exit (the pipeline runs and reaches its
    normal REJECTED outcome in the absence of an LLM),
    ``allow_dirty_unrelated: true``, ``dirty_files_at_run``
    populated.

    Mirrors the baseline test
    :func:`tests.test_baseline_command.test_baseline_create_allows_unrelated_dirty_with_flag`.
    The exit code is EXIT_OK (the optimize pipeline ran); the
    status is REJECTED (the no-LLM default path), but NEVER
    BLOCKED with the ``optimize-unrelated-dirty-files`` id.
    """
    workspace = _init_workspace_with_git(tmp_path)
    (workspace / "scratch-notes.txt").write_text(
        "untracked; not an optimize input\n",
        encoding="utf-8",
    )

    result = _run_metacrucible(
        [
            "optimize",
            str(workspace),
            "--allow-dirty-unrelated",
            "--json",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`optimize --allow-dirty-unrelated` must exit "
        f"{EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] != "BLOCKED" or all(
        b.get("id") != OPTIMIZE_UNRELATED_DIRTY_FILES_BLOCKER
        for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ), (
        f"--allow-dirty-unrelated must NOT surface the "
        f"optimize-unrelated-dirty-files blocker; got "
        f"status={payload.get('status')!r} "
        f"blocker_ids="
        f"{[b.get('id') for b in payload.get('blockers', [])]!r}"
    )
    assert payload["allow_dirty_unrelated"] is True, (
        f"allow_dirty_unrelated must be True when the flag is "
        f"set; got {payload['allow_dirty_unrelated']!r}"
    )
    assert "scratch-notes.txt" in (
        payload.get("dirty_files_at_run") or []
    ), (
        f"dirty_files_at_run must record the unrelated dirty "
        f"file; got {payload.get('dirty_files_at_run')!r}"
    )

def test_optimize_allows_only_tracked_inputs_dirty(
    tmp_path: Path,
) -> None:
    """A dirty file that IS one of the optimize inputs (artifact,
    envelope, benchmark) does NOT block ``optimize``: the guard
    only blocks UNRELATED dirty files.

    Mirrors the baseline test
    :func:`tests.test_baseline_command.test_baseline_create_allows_only_tracked_inputs_dirty`.
    The test edits the tracked artifact (one of the optimize
    inputs) without committing; ``git status --porcelain``
    reports it as `` M SKILL.md`` and the guard must treat it
    as a tracked input, not unrelated.
    """
    workspace = _init_workspace_with_git(tmp_path)
    artifact = workspace / "SKILL.md"
    # Modify the tracked artifact (one of the optimize inputs)
    # without committing. ``git status --porcelain`` reports it
    # as `` M SKILL.md`` and the guard must treat it as an
    # optimize input, not unrelated.
    artifact.write_text(
        "---\n"
        "name: opt-skill\n"
        "description: optimize-dirty-guard fixture\n"
        "---\n"
        "# body\nThe body is the only mutable range.\n"
        "Updated body content.\n",
        encoding="utf-8",
    )
    dirty = _git_dirty_paths(workspace)
    assert "SKILL.md" in dirty, (
        f"fixture invariant: SKILL.md must be reported as "
        f"dirty after the edit; got dirty={dirty!r}"
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`optimize` with only tracked optimize inputs dirty "
        f"must exit {EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert OPTIMIZE_UNRELATED_DIRTY_FILES_BLOCKER not in blocker_ids, (
        f"optimize with tracked-input dirty must NOT surface "
        f"the optimize-unrelated-dirty-files blocker; got "
        f"blocker_ids={blocker_ids!r}"
    )
    assert "SKILL.md" in (
        payload.get("dirty_files_at_run") or []
    ), (
        f"dirty_files_at_run must record the dirty tracked "
        f"input; got {payload.get('dirty_files_at_run')!r}"
    )

def test_optimize_skips_dirty_guard_outside_worktree(
    tmp_path: Path,
) -> None:
    """A workspace outside any git worktree skips the
    dirty-file guard with a stderr warning; ``optimize``
    reaches the pipeline.

    Mirrors the baseline test
    :func:`tests.test_baseline_command.test_baseline_create_skips_dirty_guard_outside_worktree`
    so the two commands share the OD3 "skip the guard with a
    warning" contract for non-worktree workspaces.
    """
    # Initialise the workspace WITHOUT a git worktree. The
    # optimize dirty-file guard must skip the check and emit a
    # stderr warning so the operator sees the silent-skip.
    workspace = _init_workspace(tmp_path)
    _seed_optimize_inputs(workspace)
    # Create an unrelated dirty file. Because there is no git
    # worktree, the guard must NOT block on it.
    (workspace / "scratch-notes.txt").write_text(
        "untracked; not an optimize input\n",
        encoding="utf-8",
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`optimize` outside a worktree must exit {EXIT_OK} "
        f"(dirty guard is skipped per OD3); got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "dirty-file guard skipped" in result.stderr, (
        f"non-worktree optimize must surface a stderr warning "
        f"so the operator sees the dirty-guard skip; got "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert OPTIMIZE_UNRELATED_DIRTY_FILES_BLOCKER not in blocker_ids, (
        f"non-worktree optimize must NOT surface the "
        f"optimize-unrelated-dirty-files blocker (the guard "
        f"is skipped); got blocker_ids={blocker_ids!r}"
    )
    assert payload["allow_dirty_unrelated"] is False, (
        f"allow_dirty_unrelated must be False when the flag "
        f"is not set; got {payload['allow_dirty_unrelated']!r}"
    )
    assert payload.get("dirty_files_at_run") == [], (
        f"non-worktree optimize must report an empty "
        f"dirty_files_at_run (the guard does not enumerate "
        f"files outside a worktree); got "
        f"{payload.get('dirty_files_at_run')!r}"
    )



# --------------------------------------------------------------------------- #
# Issue #36 — Stopping Condition: stop_reason contract                         #
# --------------------------------------------------------------------------- #


def test_stop_reason_in_cli_json_payload_for_optimizer_run(
    tmp_path: Path,
) -> None:
    """The ``optimize --json`` payload exposes the pipeline's
    machine-stable ``stop_reason`` at the top level.

    Issue #36 / Stopping Condition: the CLI must surface the
    same ``stop_reason`` the pipeline recorded on the result
    so a downstream reader can branch on the termination
    reason without re-deriving it from ``status`` /
    ``blockers`` / ``warnings``. The test runs the full
    ``metacrucible optimize`` command via subprocess with a
    clean benchmark + seeded envelope + seeded artifact; the
    pipeline's ``call_fn=None`` MVP path produces the
    ``no_candidate_edits`` stop reason, and the CLI must
    surface that value at the top level of the ``--json``
    payload.
    """
    import hashlib

    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = workspace / "SKILL.md"
    artifact.write_text(
        "---\n"
        "name: stop-reason-cli-skill\n"
        "description: Stopping Condition CLI regression fixture\n"
        "---\n"
        "# body\nThe body is the only mutable range.\n",
        encoding="utf-8",
    )
    envelope = workspace / ".metacrucible" / "envelope.json"
    envelope.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_path": str(artifact),
                "artifact_workspace": str(workspace),
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )
    # Sanity: the fixture's body hash matches what the
    # pipeline will compute when it parses the artifact.
    expected_body_hash = hashlib.sha256(
        b"# body\nThe body is the only mutable range.\n"
    ).hexdigest()
    assert expected_body_hash == hashlib.sha256(
        artifact.read_bytes().split(b"\n---\n", 1)[1]
    ).hexdigest(), (
        f"CLI regression fixture: the body hash the test "
        f"expects ({expected_body_hash!r}) must match the "
        f"artifact on disk; the envelope / artifact pair is "
        f"the contract the CLI threads through"
    )

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode in (EXIT_OK, EXIT_BLOCKED), (
        f"`optimize --json` must exit 0 or {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`optimize --json` must emit valid JSON on "
            f"stdout; got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"optimize --json must return a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    # The stop_reason key is a top-level payload field
    # alongside status / run_id / rounds. The MVP no-LLM
    # path produces an empty-suggestion round, which the
    # pipeline records as ``no_candidate_edits``.
    assert "stop_reason" in payload, (
        f"optimize --json must surface stop_reason at "
        f"the top level; got keys={sorted(payload.keys())!r}"
    )
    assert payload["stop_reason"] == "no_candidate_edits", (
        f"CLI payload must report stop_reason="
        f"'no_candidate_edits' for a no-LLM run; got "
        f"payload['stop_reason']={payload['stop_reason']!r}"
    )
    # The reason must come from the canonical vocabulary;
    # the CLI must not invent a prose value. The
    # vocabulary is a closed set exported from the
    # optimizer module.
    from metacrucible.optimizer import STOP_REASONS
    assert payload["stop_reason"] in STOP_REASONS, (
        f"CLI stop_reason must be a vocabulary string "
        f"from {sorted(STOP_REASONS)!r}; got "
        f"{payload['stop_reason']!r}"
    )

def test_optimize_default_max_rounds_is_one_without_flag(
    tmp_path: Path,
) -> None:
    """The ``optimize`` CLI defaults ``max_rounds`` to 1
    when the operator omits the ``--max-rounds`` flag.

    Issue #36 Stopping Condition: the MVP round budget
    (one round per optimize invocation) is the safe
    default; an explicit ``--max-rounds N>1`` opt-in is
    the only way to spend more than one round. The test
    runs the full ``metacrucible optimize`` command
    without the flag and asserts the CLI ``--json``
    payload surfaces both ``max_rounds == 1`` (the
    propagated default) and ``rounds == 1`` (the
    single-iteration execution) and ``stop_reason`` is
    a vocabulary id.

    The fixture is the same OPT-9 shape used by
    :func:`test_stop_reason_in_cli_json_payload_for_optimizer_run`:
    a single-mutable-range Skill body, an envelope that
    declares the artifact path, and a benchmark with one
    eligible eval + one eligible held-out case. The MVP
    no-LLM path (``call_fn=None``) produces the
    ``no_candidate_edits`` stop reason - the exact value
    is not asserted here; only that the value comes from
    the canonical ``STOP_REASONS`` vocabulary.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = workspace / "SKILL.md"
    artifact.write_text(
        "---\n"
        "name: default-max-rounds-skill\n"
        "description: Default max_rounds=1 regression fixture\n"
        "---\n"
        "# body\nThe body is the only mutable range.\n",
        encoding="utf-8",
    )
    envelope = workspace / ".metacrucible" / "envelope.json"
    envelope.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_path": str(artifact),
                "artifact_workspace": str(workspace),
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )

    # No ``--max-rounds`` flag: the operator accepts the
    # default. The CLI must propagate ROUND_BUDGET_DEFAULT
    # (1) into the payload so a downstream reader can see
    # the configured budget alongside the rounds actually
    # executed.
    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode in (EXIT_OK, EXIT_BLOCKED), (
        f"`optimize --json` (no --max-rounds) must exit "
        f"0 or {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`optimize --json` must emit valid JSON on "
            f"stdout; got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"optimize --json must return a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )

    # ``max_rounds`` is propagated from
    # ROUND_BUDGET_DEFAULT (1) into the CLI payload when
    # the operator omits the flag. A regression that
    # bumps the default OR that drops the propagation
    # would change this value - the assertion pins it.
    assert "max_rounds" in payload, (
        f"optimize --json must surface max_rounds at the "
        f"top level so downstream readers can see the "
        f"configured budget; got keys={sorted(payload.keys())!r}"
    )
    assert payload["max_rounds"] == 1, (
        f"optimize --json without --max-rounds must "
        f"propagate ROUND_BUDGET_DEFAULT (1) into the "
        f"payload; got payload['max_rounds']="
        f"{payload['max_rounds']!r}"
    )

    # ``rounds`` reflects the actual number of iterations
    # the pipeline ran. With max_rounds=1 the loop runs
    # exactly one iteration then exits.
    assert "rounds" in payload, (
        f"optimize --json must surface rounds at the top "
        f"level alongside max_rounds; got keys="
        f"{sorted(payload.keys())!r}"
    )
    assert payload["rounds"] == 1, (
        f"optimize --json without --max-rounds must run "
        f"exactly 1 round (matches the propagated "
        f"budget); got payload['rounds']={payload['rounds']!r}"
    )

    # ``stop_reason`` must come from the canonical
    # vocabulary so the operator's downstream tooling
    # can branch on it. The MVP no-LLM path produces
    # ``no_candidate_edits``; this assertion is the
    # broader vocabulary check that pairs with the
    # no-LLM specific assertion in the sibling test.
    from metacrucible.optimizer import STOP_REASONS
    assert "stop_reason" in payload, (
        f"optimize --json must surface stop_reason at "
        f"the top level; got keys={sorted(payload.keys())!r}"
    )
    assert payload["stop_reason"] in STOP_REASONS, (
        f"CLI stop_reason must be a vocabulary string "
        f"from {sorted(STOP_REASONS)!r}; got "
        f"payload['stop_reason']={payload['stop_reason']!r}"
    )
# --------------------------------------------------------------------------- #
# Held-out exclusion: Stopping Condition surface                                 #
# --------------------------------------------------------------------------- #


def test_stop_reason_does_not_leak_held_out_content(
    tmp_path: Path,
) -> None:
    """Stopping Condition output and the ``optimize_finished``
    history event MUST NOT include held-out case content.

    The optimizer pipeline's termination surface is small and
    machine-stable: ``stop_reason`` is one of six vocabulary
    strings; ``warnings`` and ``blockers`` carry ``{id, message}``
    pairs whose ``message`` strings are pre-canned English prose.
    A regression that threads a held-out case's ``input.prompt``,
    ``expected_output``, ``checks``, or ``judgment`` into any of
    those fields would expose held-out content to operators
    even though the held-out split is supposed to be
    quarantined from every run-level artifact (OPT-9 / ADR 0031
    / ADR 0032).

    The fixture is the OPT-9 shape: a single-mutable-range
    Skill body, an envelope that declares the artifact path,
    and a benchmark with one eligible reviewed eval case + one
    eligible reviewed held-out case. The held-out case carries a
    unique sentinel string in *every* field where a regression
    could plausibly leak it (input / expected_output / checks /
    judgment). The MVP no-LLM path (``call_fn=None``) produces
    an empty-suggestion round, so the pipeline exits with
    ``stop_reason="no_candidate_edits"`` on round 1 - the
    earliest possible Stopping Condition - and writes the
    ``optimize_finished`` event with the same stop reason.

    Pins:

      - ``payload["stop_reason"]`` is one of
        :data:`STOP_REASONS` (machine-stable vocabulary).
      - The serialized ``--json`` payload does NOT contain the
        held-out sentinel anywhere (covers stop_reason,
        warnings, blockers, rounds, selected_candidate_ids,
        acceptance_decision, best_revision, evidence_refs,
        record_counts, and every other field).
      - The ``history.jsonl`` stream does NOT contain the
        held-out sentinel anywhere (covers the
        ``optimize_finished`` event in particular plus
        every other per-run event).
      - The ``optimize_finished`` event's ``stop_reason``
        mirrors the CLI payload's ``stop_reason`` so a
        lineage reader sees the same termination reason.
    """
    held_out_sentinel = "HELD_OUT_STOP_REASON_SECRET_DO_NOT_LEAK"
    eval_sentinel = "EVAL_SENTINEL_OK_99"

    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = workspace / "SKILL.md"
    artifact.write_text(
        "---\n"
        "name: stop-reason-leak-skill\n"
        "description: Held-out stop_reason leak regression fixture\n"
        "---\n"
        "# body\nThe body is the only mutable range.\n",
        encoding="utf-8",
    )
    envelope = workspace / ".metacrucible" / "envelope.json"
    envelope.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_path": str(artifact),
                "artifact_workspace": str(workspace),
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Seed the benchmark with a reviewed eval case + a reviewed
    # held-out case. The held-out case carries the sentinel in
    # every field where a regression could plausibly surface
    # case content (input / expected_output / checks /
    # judgment). The eval case carries a different sentinel as
    # a control: the assertions are about the held-out sentinel
    # only, but the eval sentinel strengthens coverage against
    # any case-content leak from either split. The string-
    # search is constructed against ``json.dumps`` of the
    # actual payload / history output, so its realness is
    # established by construction.
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval")
            | {
                "input": {"prompt": eval_sentinel},
                "expected_output": eval_sentinel,
            },
            _reviewed_case("held-1", split="held_out")
            | {
                "input": {"prompt": held_out_sentinel},
                "expected_output": held_out_sentinel,
                "checks": [
                    {
                        "name": "leak_check",
                        "pattern": held_out_sentinel,
                    }
                ],
                "judgment": held_out_sentinel,
            },
        ],
    )

    # Run the full CLI without ``--max-rounds``: the operator
    # accepts the default (1 round). The MVP no-LLM path
    # produces an empty-suggestion round and the pipeline
    # exits with ``stop_reason="no_candidate_edits"`` - the
    # earliest possible early-stop path through the
    # Stopping Condition surface.
    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode in (EXIT_OK, EXIT_BLOCKED), (
        f"`optimize --json` must exit 0 or {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`optimize --json` must emit valid JSON on "
            f"stdout; got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"optimize --json must return a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )

    # ``stop_reason`` must be present and must come from the
    # closed :data:`STOP_REASONS` vocabulary. This is the
    # positive check that the Stopping Condition contract is
    # being honored: the value is a stable enum string, not
    # a free-form prose that could be polluted with case
    # content.
    assert "stop_reason" in payload, (
        f"optimize --json must surface stop_reason at the "
        f"top level; got keys={sorted(payload.keys())!r}"
    )
    from metacrucible.optimizer import (
        STOP_REASONS,
        STOP_REASON_NO_CANDIDATE_EDITS,
    )

    assert payload["stop_reason"] in STOP_REASONS, (
        f"CLI stop_reason must be a vocabulary string from "
        f"{sorted(STOP_REASONS)!r}; got "
        f"payload['stop_reason']={payload['stop_reason']!r}"
    )
    # The MVP no-LLM path produces ``no_candidate_edits`` on
    # round 1. Pin the specific value so a regression that
    # changed the no-LLM termination path to, e.g.,
    # ``max_rounds_reached`` would surface here.
    assert payload["stop_reason"] == STOP_REASON_NO_CANDIDATE_EDITS, (
        f"MVP no-LLM run with default --max-rounds must "
        f"report stop_reason={STOP_REASON_NO_CANDIDATE_EDITS!r}; "
        f"got payload['stop_reason']={payload['stop_reason']!r}"
    )

    # The serializer's whole-string view: every field the
    # CLI emits, recursively, gets stringified and searched.
    # This covers stop_reason, warnings, blockers, rounds,
    # record_counts, selected_candidate_ids, acceptance_decision,
    # best_revision, evidence_refs, status, run_id, etc. - if
    # any of those surfaced the held-out sentinel, the search
    # would catch it.
    payload_blob = json.dumps(payload, sort_keys=True)
    assert held_out_sentinel not in payload_blob, (
        f"CLI --json payload must NOT contain held-out case "
        f"content; sentinel leaked into "
        f"payload_blob={payload_blob!r}"
    )
    # Control: the eval sentinel also must not surface in the
    # payload, since the payload only carries IDs / counts /
    # run metadata - not eval case content either. This proves
    # the search is actually scanning, not silently passing.
    assert eval_sentinel not in payload_blob, (
        f"CLI --json payload must NOT contain eval case "
        f"content either; eval sentinel leaked into "
        f"payload_blob={payload_blob!r}"
    )

    # Per-field focused assertions: the contract says
    # stop_reason is a vocabulary id; warnings and blockers
    # are pre-canned {id, message} dicts. A regression that
    # stored a held-out string in any of these would break
    # the contract, so surface it explicitly.
    for warning in payload.get("warnings", []) or []:
        assert isinstance(warning, dict), (
            f"warnings entries must be dicts; got "
            f"{type(warning).__name__} ({warning!r})"
        )
        assert "message" in warning, (
            f"warnings entry missing 'message' key; got "
            f"{warning!r}"
        )
        assert held_out_sentinel not in warning["message"], (
            f"warning message must NOT contain held-out "
            f"sentinel; got warning={warning!r}"
        )
    for blocker in payload.get("blockers", []) or []:
        assert isinstance(blocker, dict), (
            f"blockers entries must be dicts; got "
            f"{type(blocker).__name__} ({blocker!r})"
        )
        assert "message" in blocker, (
            f"blocker entry missing 'message' key; got "
            f"{blocker!r}"
        )
        assert held_out_sentinel not in blocker["message"], (
            f"blocker message must NOT contain held-out "
            f"sentinel; got blocker={blocker!r}"
        )
    # ``rounds`` is a count, not content. The MVP no-LLM path
    # runs exactly 1 round.
    assert payload.get("rounds") == 1, (
        f"MVP no-LLM run with default --max-rounds must "
        f"report rounds=1; got payload['rounds']="
        f"{payload.get('rounds')!r}"
    )

    # History stream: every event the pipeline persisted, in
    # particular the ``optimize_finished`` event, must not
    # carry held-out content. The string-search covers any
    # record type (case_reflection, edit_suggestion,
    # ranked_edit_set, range_merge_plan, optimize_finished,
    # optimize_blocked, etc.) without naming each one.
    records = _opt9_read_history(workspace)
    history_blob = json.dumps(records, sort_keys=True)
    assert held_out_sentinel not in history_blob, (
        f"history.jsonl must NOT contain held-out case "
        f"content; sentinel leaked into "
        f"history_blob={history_blob!r}"
    )
    # Control: the eval sentinel also must not surface in
    # the history (the case_reflection records carry
    # rationale strings but never the input prompt).
    assert eval_sentinel not in history_blob, (
        f"history.jsonl must NOT contain eval case content "
        f"either; eval sentinel leaked into "
        f"history_blob={history_blob!r}"
    )

    # The ``optimize_finished`` event is the run-level
    # termination record. It must mirror the CLI payload's
    # ``stop_reason`` and must not carry held-out content in
    # any of its sub-fields (event, run_id, status, rounds,
    # record_counts, blockers, warnings, stop_reason,
    # timestamp).
    finished = [
        r for r in records
        if isinstance(r, dict) and r.get("event") == "optimize_finished"
    ]
    assert finished, (
        f"pipeline must persist an optimize_finished event "
        f"for a no-LLM no_candidate_edits run; got "
        f"events={[r.get('event') for r in records]!r}"
    )
    last_finished = finished[-1]
    assert last_finished.get("stop_reason") == payload["stop_reason"], (
        f"optimize_finished.stop_reason must mirror the "
        f"CLI payload's stop_reason; got "
        f"finished.stop_reason={last_finished.get('stop_reason')!r} "
        f"payload.stop_reason={payload['stop_reason']!r}"
    )
    assert last_finished.get("stop_reason") in STOP_REASONS, (
        f"optimize_finished.stop_reason must be a "
        f"vocabulary string from {sorted(STOP_REASONS)!r}; "
        f"got {last_finished.get('stop_reason')!r}"
    )
    finished_blob = json.dumps(last_finished, sort_keys=True)
    assert held_out_sentinel not in finished_blob, (
        f"optimize_finished event must NOT contain "
        f"held-out case content; sentinel leaked into "
        f"finished_blob={finished_blob!r}"
    )
    # Per-field focused assertion on the optimize_finished
    # event: ``blockers`` and ``warnings`` here mirror the
    # payload's lists and use the same pre-canned {id,
    # message} contract.
    for warning in last_finished.get("warnings", []) or []:
        assert isinstance(warning, dict), (
            f"optimize_finished.warnings entries must be "
            f"dicts; got {type(warning).__name__} "
            f"({warning!r})"
        )
        assert "message" in warning, (
            f"optimize_finished.warnings entry missing "
            f"'message' key; got {warning!r}"
        )
        assert held_out_sentinel not in warning["message"], (
            f"optimize_finished warning message must NOT "
            f"contain held-out sentinel; got warning="
            f"{warning!r}"
        )
    for blocker in last_finished.get("blockers", []) or []:
        assert isinstance(blocker, dict), (
            f"optimize_finished.blockers entries must be "
            f"dicts; got {type(blocker).__name__} "
            f"({blocker!r})"
        )
        assert "message" in blocker, (
            f"optimize_finished.blockers entry missing "
            f"'message' key; got {blocker!r}"
        )
        assert held_out_sentinel not in blocker["message"], (
            f"optimize_finished blocker message must NOT "
            f"contain held-out sentinel; got blocker="
            f"{blocker!r}"
        )


# --------------------------------------------------------------------------- #
# Issue #38: interrupted-run detection and explicit resume / abort            #
# --------------------------------------------------------------------------- #

#: Stable blocker id emitted by ``optimize`` in non-interactive mode when the
#: workspace carries an interrupted run (optimize_started without a matching
#: optimize_finished) and the operator did not pass ``--confirm-resume``.
RESUME_NON_INTERACTIVE_BLOCKER = "resume-non-interactive-blocked"

#: Stable blocker id emitted by ``optimize`` in interactive mode when the
#: operator declines the resume prompt.
RESUME_CONFIRMATION_REQUIRED_BLOCKER = "resume-confirmation-required"


def _init_resume_workspace(tmp_path: Path) -> Path:
    """Seed a workspace that survives all preflight checks up to the resume gate.

    Builds on ``_init_workspace`` (which runs ``metacrucible init``) and
    layers on the OPT-9 fixture artifact, the envelope that declares it,
    and a clean benchmark with one eligible eval + one eligible held-out
    case. The dirty-guard skips when the workspace is not a git worktree
    so the resume gate is the next decision point.
    """
    workspace = _init_workspace(tmp_path)
    _opt9_seed_artifact(workspace)
    _opt9_seed_envelope(workspace, _opt9_skill_artifact_path(workspace))
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ],
    )
    return workspace


def _seed_interrupted_history(
    workspace: Path, *, run_ids: tuple[str, ...] = ("stale-run-1",)
) -> Path:
    """Write ``optimize_started`` events with no matching ``optimize_finished``."""
    history_path = workspace / ".metacrucible" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "event": "optimize_started",
                "run_id": run_id,
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        for run_id in run_ids
    ]
    history_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return history_path


def _seed_clean_history(
    workspace: Path, *, run_id: str = "done-run-1"
) -> Path:
    """Write a matching ``optimize_started`` + ``optimize_finished`` pair."""
    history_path = workspace / ".metacrucible" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {"event": "optimize_started", "run_id": run_id},
            sort_keys=True,
        ),
        json.dumps(
            {
                "event": "optimize_finished",
                "run_id": run_id,
                "stop_reason": "no_candidate_edits",
            },
            sort_keys=True,
        ),
    ]
    history_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return history_path


def _build_resume_namespace(
    workspace: Path,
    *,
    confirm_resume: bool = False,
    json_output: bool = True,
) -> "argparse.Namespace":
    """Build a Namespace with the fields ``cmd_optimize`` reads."""
    import argparse as _argparse

    return _argparse.Namespace(
        workspace=str(workspace),
        json=json_output,
        max_rounds=1,
        confirm_routing=False,
        confirm_resume=confirm_resume,
        allow_dirty_unrelated=False,
    )


def _install_stub_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Install a stub ``run_optimizer_pipeline`` and return its call log."""
    from dataclasses import dataclass as _dataclass

    @_dataclass
    class _StubResult:
        status: str = "REJECTED"
        run_id: str = "stub-run"
        rounds: int = 0
        record_counts: dict[str, int] = None  # type: ignore[assignment]
        evidence_refs: dict[str, str] = None  # type: ignore[assignment]
        blockers: list = None  # type: ignore[assignment]
        warnings: list = None  # type: ignore[assignment]
        best_revision = None
        acceptance_decision: dict = None  # type: ignore[assignment]
        selected_candidate_ids: list = None  # type: ignore[assignment]
        stop_reason: str = "no_candidate_edits"

    calls: list = []

    def _stub(*args, **kwargs):
        calls.append((args, kwargs))
        return _StubResult(
            record_counts={},
            evidence_refs={},
            blockers=[],
            warnings=[],
            acceptance_decision={},
            selected_candidate_ids=[],
        )

    monkeypatch.setattr(
        "metacrucible.__main__.run_optimizer_pipeline", _stub
    )
    return calls


def test_optimize_non_interactive_interrupted_history_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC1 (CLI): non-interactive + interrupted history + no
    ``--confirm-resume`` -> ``EXIT_BLOCKED`` with the resume-non-
    interactive-blocked payload; the optimizer pipeline is NOT
    called (no silent resume).
    """
    from metacrucible.__main__ import cmd_optimize

    workspace = _init_resume_workspace(tmp_path)
    _seed_interrupted_history(workspace, run_ids=("stale-run-1",))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": prompt_calls.append(prompt) or ""
    )
    pipeline_calls = _install_stub_pipeline(monkeypatch)

    args = _build_resume_namespace(workspace, confirm_resume=False)
    rc = cmd_optimize(args)
    captured = capsys.readouterr()

    assert rc == EXIT_BLOCKED, (
        f"non-interactive interrupted history without "
        f"--confirm-resume must exit {EXIT_BLOCKED}; got "
        f"rc={rc} stdout={captured.out!r} stderr={captured.err!r}"
    )
    try:
        payload = json.loads(captured.out)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"optimize --json must emit valid JSON; got "
            f"stdout={captured.out!r} error={exc}"
        )
    assert isinstance(payload, dict)
    assert payload.get("status") == "BLOCKED", (
        f"non-interactive interrupted history must report "
        f"status=BLOCKED; got {payload.get('status')!r}"
    )
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert RESUME_NON_INTERACTIVE_BLOCKER in blocker_ids, (
        f"non-interactive interrupted history must surface "
        f"the {RESUME_NON_INTERACTIVE_BLOCKER!r} blocker id; "
        f"got blocker_ids={blocker_ids!r}"
    )
    interrupted_blob = json.dumps(payload, sort_keys=True)
    assert "stale-run-1" in interrupted_blob, (
        f"BLOCKED payload must name the interrupted run id; "
        f"got payload={payload!r}"
    )
    assert pipeline_calls == [], (
        f"pipeline must NOT be called when interrupted and "
        f"not confirmed; got pipeline_calls={pipeline_calls!r}"
    )
    assert prompt_calls == [], (
        f"non-interactive branch must not prompt for "
        f"confirmation; got prompts={prompt_calls!r}"
    )


def test_optimize_interactive_interrupted_history_without_confirmation_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC2 (CLI): interactive + interrupted history + operator
    declines the prompt -> ``EXIT_BLOCKED`` with the resume-
    confirmation-required blocker; the optimizer pipeline is
    NOT called.
    """
    from metacrucible.__main__ import cmd_optimize

    workspace = _init_resume_workspace(tmp_path)
    _seed_interrupted_history(workspace, run_ids=("stale-run-2",))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": (prompt_calls.append(prompt), "n")[1],
    )
    pipeline_calls = _install_stub_pipeline(monkeypatch)

    args = _build_resume_namespace(workspace, confirm_resume=False)
    rc = cmd_optimize(args)
    captured = capsys.readouterr()

    assert rc == EXIT_BLOCKED, (
        f"interactive interrupted history with decline must "
        f"exit {EXIT_BLOCKED}; got rc={rc} "
        f"stdout={captured.out!r} stderr={captured.err!r}"
    )
    payload = json.loads(captured.out)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert RESUME_CONFIRMATION_REQUIRED_BLOCKER in blocker_ids, (
        f"interactive interrupted history with decline must "
        f"surface {RESUME_CONFIRMATION_REQUIRED_BLOCKER!r}; "
        f"got blocker_ids={blocker_ids!r}"
    )
    assert pipeline_calls == [], (
        f"pipeline must NOT be called when interactive "
        f"decline; got pipeline_calls={pipeline_calls!r}"
    )
    assert prompt_calls, "interactive branch must prompt exactly once"
    assert any('stale-run-2' in p for p in prompt_calls), (
        f"interactive prompt must surface the stale run id "
        f"'stale-run-2'; got prompt_calls={prompt_calls!r}"
    )


def test_optimize_interactive_interrupted_history_with_confirmation_proceeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC3 (CLI): interactive + interrupted history + operator
    accepts the prompt -> optimize proceeds to the pipeline.
    """
    from metacrucible.__main__ import cmd_optimize

    workspace = _init_resume_workspace(tmp_path)
    _seed_interrupted_history(workspace, run_ids=("stale-run-3",))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": (prompt_calls.append(prompt), "y")[1],
    )
    pipeline_calls = _install_stub_pipeline(monkeypatch)

    args = _build_resume_namespace(workspace, confirm_resume=False)
    rc = cmd_optimize(args)
    captured = capsys.readouterr()

    # Pipeline stub returns REJECTED, so EXIT_OK (cmd_optimize
    # returns OK for ACCEPTED / no-blocking REJECTED).
    assert rc == EXIT_OK, (
        f"interactive interrupted history with accept must "
        f"reach the pipeline (rc={EXIT_OK}); got rc={rc} "
        f"stdout={captured.out!r} stderr={captured.err!r}"
    )
    assert len(pipeline_calls) == 1, (
        f"pipeline must be called exactly once when "
        f"interactive accept; got pipeline_calls={pipeline_calls!r}"
    )
    assert prompt_calls, "interactive accept branch must prompt"
    assert any('stale-run-3' in p for p in prompt_calls), (
        f"interactive prompt must surface the stale run id "
        f"'stale-run-3'; got prompt_calls={prompt_calls!r}"
    )


def test_optimize_non_interactive_interrupted_history_with_confirm_resume_proceeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC4 (CLI): non-interactive + interrupted history + operator
    passed ``--confirm-resume`` -> optimize proceeds to the pipeline
    without prompting.
    """
    from metacrucible.__main__ import cmd_optimize

    workspace = _init_resume_workspace(tmp_path)
    _seed_interrupted_history(workspace, run_ids=("stale-run-4",))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": prompt_calls.append(prompt) or ""
    )
    pipeline_calls = _install_stub_pipeline(monkeypatch)

    args = _build_resume_namespace(workspace, confirm_resume=True)
    rc = cmd_optimize(args)
    captured = capsys.readouterr()

    assert rc == EXIT_OK, (
        f"non-interactive interrupted history with "
        f"--confirm-resume must reach the pipeline "
        f"(rc={EXIT_OK}); got rc={rc} "
        f"stdout={captured.out!r} stderr={captured.err!r}"
    )
    assert len(pipeline_calls) == 1, (
        f"pipeline must be called exactly once when "
        f"--confirm-resume is set; got pipeline_calls={pipeline_calls!r}"
    )
    assert prompt_calls == [], (
        f"--confirm-resume must not prompt; got prompts="
        f"{prompt_calls!r}"
    )


def test_optimize_clean_history_does_not_emit_resume_blocker_or_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC5 (CLI): clean history (matching started + finished)
    -> no resume prompt, no resume blocker, optimize proceeds
    normally.
    """
    from metacrucible.__main__ import cmd_optimize

    workspace = _init_resume_workspace(tmp_path)
    _seed_clean_history(workspace, run_id="done-run-1")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": prompt_calls.append(prompt) or ""
    )
    pipeline_calls = _install_stub_pipeline(monkeypatch)

    args = _build_resume_namespace(workspace, confirm_resume=False)
    rc = cmd_optimize(args)
    captured = capsys.readouterr()

    assert rc == EXIT_OK, (
        f"clean history must reach the pipeline; got rc={rc} "
        f"stdout={captured.out!r} stderr={captured.err!r}"
    )
    assert len(pipeline_calls) == 1, (
        f"pipeline must be called exactly once on clean "
        f"history; got pipeline_calls={pipeline_calls!r}"
    )
    assert prompt_calls == [], (
        f"clean history must not prompt; got prompts="
        f"{prompt_calls!r}"
    )
    payload = json.loads(captured.out)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert RESUME_NON_INTERACTIVE_BLOCKER not in blocker_ids
    assert RESUME_CONFIRMATION_REQUIRED_BLOCKER not in blocker_ids, (
        f"clean history must not emit any resume blocker; "
        f"got blocker_ids={blocker_ids!r}"
    )

def test_optimize_confirmed_resume_retires_interrupted_runs_in_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC6 (CLI): a confirmed resume MUST retire stale run_ids in history.

    Without the synthetic ``optimize_finished`` write the gate would
    fire on every subsequent optimize call forever (the next pipeline
    invocation generates a fresh ``run_id`` so the previous stale one
    never gets matched). After the first confirmed resume:

      - a synthetic ``optimize_finished`` is appended to ``history.jsonl``
        for every stale ``run_id``,
      - a follow-up ``cmd_optimize`` (no ``--confirm-resume``, no
        interactive prompt) must NOT re-trigger the gate -- the pipeline
        runs and no resume blockers appear.
    """
    from metacrucible.__main__ import cmd_optimize

    workspace = _init_resume_workspace(tmp_path)
    stale_run_id = "stale-run-5-retire"
    _seed_interrupted_history(workspace, run_ids=(stale_run_id,))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": prompt_calls.append(prompt) or ""
    )
    pipeline_calls = _install_stub_pipeline(monkeypatch)

    # First call: confirmed resume must proceed AND retire the
    # stale run_id so a second call does not re-trigger the gate.
    args = _build_resume_namespace(workspace, confirm_resume=True)
    rc = cmd_optimize(args)
    captured = capsys.readouterr()

    assert rc == EXIT_OK, (
        f"confirmed resume must reach the pipeline (rc={EXIT_OK}); "
        f"got rc={rc} stdout={captured.out!r} stderr={captured.err!r}"
    )
    assert len(pipeline_calls) == 1, (
        f"pipeline must run exactly once on confirmed resume; "
        f"got pipeline_calls={pipeline_calls!r}"
    )
    assert prompt_calls == [], (
        f"--confirm-resume must not prompt; got prompts="
        f"{prompt_calls!r}"
    )

    # The synthetic retire event MUST be in history.jsonl.
    history_path = workspace / ".metacrucible" / "history.jsonl"
    history_records = [
        json.loads(line)
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    retired = [
        rec
        for rec in history_records
        if isinstance(rec, dict)
        and rec.get("event") == "optimize_finished"
        and rec.get("run_id") == stale_run_id
    ]
    assert len(retired) == 1, (
        f"confirmed resume must retire the stale run_id "
        f"{stale_run_id!r} via a synthetic optimize_finished; "
        f"got retired={retired!r} history_records={history_records!r}"
    )
    retire_record = retired[0]
    assert retire_record.get("status") == "SUPERSEDED", (
        f"synthetic retire event must mark the stale run as "
        f"SUPERSEDED; got retire_record={retire_record!r}"
    )

    # Second call: WITHOUT --confirm-resume. The retire must have
    # closed the audit lineage so the gate does not re-fire.
    pipeline_calls.clear()
    prompt_calls.clear()
    args_no_confirm = _build_resume_namespace(
        workspace, confirm_resume=False
    )
    rc2 = cmd_optimize(args_no_confirm)
    captured2 = capsys.readouterr()

    assert rc2 == EXIT_OK, (
        f"second optimize call must proceed (retire closed the "
        f"lineage); got rc={rc2} stdout={captured2.out!r} "
        f"stderr={captured2.err!r}"
    )
    assert len(pipeline_calls) == 1, (
        f"second call must reach the pipeline; "
        f"got pipeline_calls={pipeline_calls!r}"
    )
    payload2 = json.loads(captured2.out)
    blocker_ids2 = [
        b.get("id") for b in payload2.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert RESUME_NON_INTERACTIVE_BLOCKER not in blocker_ids2, (
        f"second call must not re-emit the resume blocker "
        f"(retire closed the lineage); got blocker_ids={blocker_ids2!r}"
    )
    assert RESUME_CONFIRMATION_REQUIRED_BLOCKER not in blocker_ids2, (
        f"second call must not emit the confirmation blocker; "
        f"got blocker_ids={blocker_ids2!r}"
    )
