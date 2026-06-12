# --------------------------------------------------------------------------- #
# Tests for ``metacrucible baseline create`` (Issue #31).                     #
# --------------------------------------------------------------------------- #
"""Acceptance tests for the ``metacrucible baseline create`` subcommand.

The MVP contract pinned by Issue #31:

  - Creates a digest baseline ``<workspace>/.metacrucible/baseline.json``
    that records the four input hashes (artifact, envelope, benchmark,
    evaluation harness) plus the schema version and ``created_at``
    timestamp.
  - Refuses to start with stable blocker ids when the workspace,
    envelope, benchmark, or artifact path is missing.
  - Refuses to start (BLOCKED, ``baseline-unrelated-dirty-files``) when
    the workspace is a git worktree and ``git status --porcelain``
    reports dirty files that are not the tracked baseline inputs,
    unless ``--allow-dirty-unrelated`` is set.

The tests use ``python -m metacrucible`` so the same subprocess
pattern the rest of the CLI test suite uses is exercised end-to-end.
``HOME`` is pinned to ``tmp_path`` via the ``isolated_global_home``
fixture so :class:`UserGlobalStorage` does not pollute the developer's
real ``~/.metacrucible/`` while the BLOCKED-bundle writers fire.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from metacrucible.exit_codes import EXIT_BLOCKED, EXIT_OK, EXIT_USER_ERROR

#: Literal case-level field name used to flag bootstrap-generated
#: cases as "pending human review". Mirrors the constant in
#: :mod:`metacrucible.__main__` (the field name is part of the
#: machine-stable contract). Defined locally so the test file
#: does not depend on the internal layout of ``__main__``.
BOOTSTRAP_PENDING_REVIEW_FIELD = "BOOTSTRAP_PENDING_REVIEW"
# required for this module; the helpers below mirror the patterns in
# the optimize / bootstrap test files.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Name of the benchmark container at the workspace root. Pinned by
#: ADR 0025 so the loader reads this path by convention.
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Name of the envelope file under ``.metacrucible/`` (Issue #6).
ENVELOPE_FILE_NAME = "envelope.json"

#: Name of the baseline digest file written under ``.metacrucible/``
#: (Issue #31).
BASELINE_FILE_NAME = "baseline.json"

#: Stable exit codes from :mod:`metacrucible.exit_codes`. Pinned
#: locally so the test file does not depend on internal layout.
EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_BLOCKED = 2

#: Stable blocker ids from :mod:`metacrucible.__main__`. Mirror the
#: constant definitions so a future rename fails the test loud.
BASELINE_WORKSPACE_MISSING_BLOCKER = "baseline-workspace-missing"
BASELINE_ENVELOPE_MISSING_BLOCKER = "baseline-envelope-missing"
BASELINE_BENCHMARK_MISSING_BLOCKER = "baseline-benchmark-missing"
BASELINE_ARTIFACT_UNRESOLVED_BLOCKER = "baseline-artifact-unresolved"
BASELINE_UNRELATED_DIRTY_FILES_BLOCKER = "baseline-unrelated-dirty-files"


def _run_metacrucible(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with captured text output.

    Mirrors the helper in :mod:`tests.test_optimize_command` so the
    baseline tests use the same subprocess pattern the rest of the
    CLI test suite uses.
    """
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _run_git(
    args: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``git`` with captured output.

    The dirty-file guard tests need to seed a real git worktree
    so ``git status --porcelain`` returns the expected lines.
    """
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write ``records`` to ``path`` as JSONL.

    Mirrors the helper in :mod:`tests.test_optimize_command` so the
    baseline tests can seed the benchmark container.
    """
    payload = "\n".join(
        json.dumps(rec, sort_keys=True) for rec in records
    )
    path.write_text(payload + "\n", encoding="utf-8")


def _metadata_record() -> dict:
    """Return the benchmark-level ``metadata`` record (ADR 0029)."""
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
        "created_at": "1970-01-01T00:00:00Z",
    }


def _reviewed_case(
    case_id: str, *, split: str
) -> dict:
    """Return a single reviewed-eval / reviewed-held-out case record.

    Mirrors the helper in :mod:`tests.test_optimize_command` so the
    baseline tests can seed a minimal benchmark container with
    reviewed cases. The benchmark hash differs across runs only when
    the payload differs, so the helper pins a deterministic shape.
    """
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "reviewed",
        "split": split,
        "input": f"input for {case_id}",
        "checks": [],
        "judgment": None,
        "reviewed": True,
        "reviewed_by": "test",
        "reviewed_at": "1970-01-01T00:00:00Z",
        BOOTSTRAP_PENDING_REVIEW_FIELD: False,
    }


@pytest.fixture
def isolated_global_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Pin ``HOME`` to a temp dir so :class:`UserGlobalStorage`
    does not pollute the developer's real ``~/.metacrucible/``.

    Mirrors the fixture in :mod:`tests.test_review_command` so the
    new tests can run alongside the storage tests without
    stepping on the same ``HOME``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _init_workspace(tmp_path: Path) -> Path:
    """Run ``init`` against a fresh workspace dir and return that dir.

    The baseline tests then seed the workspace with an artifact
    and update the envelope with the artifact path so the
    ``baseline create`` precondition (``baseline-envelope-missing``
    is absent and the envelope carries ``artifact_path``) is
    satisfied. The workspace is initialised as a git worktree so
    the dirty-file guard can be exercised.
    """
    workspace = tmp_path / "ws-baseline"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`init` must exit 0 before baseline; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    # Seed a benchmark with the minimum reviewed cases so the
    # baseline can hash a non-empty payload. A baseline against an
    # empty benchmark is also valid (the digest is deterministic)
    # but seeding reviewed cases matches the rest of the CLI test
    # suite and keeps the ``benchmark_hash`` output meaningful.
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("baseline-eval-1", split="eval"),
            _reviewed_case("baseline-held-1", split="held_out"),
        ],
    )
    # Write a placeholder artifact so the baseline can hash it.
    artifact = workspace / "my-skill.md"
    artifact.write_text(
        "---\nname: my-skill\n---\nbody of the skill\n",
        encoding="utf-8",
    )
    # Update the envelope with the artifact path so the baseline
    # can resolve it from the envelope (the canonical contract;
    # ``baseline create`` does not scan / glob the workspace).
    envelope_path = workspace / ".metacrucible" / ENVELOPE_FILE_NAME
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["artifact_path"] = str(artifact.resolve())
    envelope_path.write_text(
        json.dumps(envelope, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Initialise the workspace as a git worktree so the dirty-file
    # guard has a real ``git status --porcelain`` to consult. A
    # baseline against a non-worktree workspace is also valid
    # (the guard is skipped with a warning) but seeding a worktree
    # matches the "real operator workflow" and lets the dirty-
    # file tests opt-in by modifying tracked or untracked files.
    _run_git(["init", "-q"], cwd=workspace)
    _run_git(["config", "user.email", "test@example.com"], cwd=workspace)
    _run_git(["config", "user.name", "Baseline Test"], cwd=workspace)
    _run_git(["add", "-A"], cwd=workspace)
    _run_git(["commit", "-q", "-m", "init baseline workspace"], cwd=workspace)
    return workspace


def _git_dirty_paths(workspace: Path) -> list[str]:
    """Return ``git status --porcelain`` paths from ``workspace``."""
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


# --------------------------------------------------------------------------- #
# Acceptance tests                                                            #
# --------------------------------------------------------------------------- #


def test_baseline_subcommand_is_recognized() -> None:
    """``metacrucible baseline --help`` is a registered subcommand.

    Argparse raises ``unrecognized arguments`` if the subcommand
    is not wired in. The acceptance criterion is that ``baseline``
    appears in the help output and the subcommand-level ``--help``
    exits 0.
    """
    result = _run_metacrucible(["baseline", "--help"], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`metacrucible baseline --help` must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "baseline" in result.stdout, (
        f"baseline --help must mention the subcommand name; got "
        f"{result.stdout!r}"
    )
    assert "create" in result.stdout, (
        f"baseline --help must advertise the nested create action; "
        f"got {result.stdout!r}"
    )


def test_baseline_create_subcommand_is_recognized() -> None:
    """``metacrucible baseline create --help`` exits 0.

    The nested ``create`` subparser must be wired into the
    ``baseline`` subparser so argparse recognises the deeper
    command shape.
    """
    result = _run_metacrucible(
        ["baseline", "create", "--help"], cwd=REPO_ROOT
    )
    assert result.returncode == EXIT_OK, (
        f"`metacrucible baseline create --help` must exit "
        f"{EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "baseline create" in result.stdout, (
        f"baseline create --help must mention the full command "
        f"path; got {result.stdout!r}"
    )
    assert "workspace" in result.stdout, (
        f"baseline create --help must advertise the workspace "
        f"positional; got {result.stdout!r}"
    )
    assert "--allow-dirty-unrelated" in result.stdout, (
        f"baseline create --help must advertise "
        f"--allow-dirty-unrelated; got {result.stdout!r}"
    )
    assert "--json" in result.stdout, (
        f"baseline create --help must advertise --json; got "
        f"{result.stdout!r}"
    )


def test_baseline_alone_exits_user_error(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``metacrucible baseline`` without an action exits user-error.

    Argparse maps the missing-action error to exit code 2, which
    :func:`metacrucible.__main__.main` maps to
    :data:`metacrucible.exit_codes.EXIT_USER_ERROR`. The command
    never silently returns EXIT_OK for an unrecognised shape.
    """
    result = _run_metacrucible(["baseline"], cwd=REPO_ROOT)
    assert result.returncode == EXIT_USER_ERROR, (
        f"`metacrucible baseline` (no action) must exit "
        f"{EXIT_USER_ERROR}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_baseline_create_without_workspace_exits_user_error(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``baseline create`` without a workspace exits user-error.

    The workspace is a required positional; argparse rejects the
    call with exit code 2, which ``main`` maps to EXIT_USER_ERROR.
    """
    result = _run_metacrucible(
        ["baseline", "create"], cwd=REPO_ROOT
    )
    assert result.returncode == EXIT_USER_ERROR, (
        f"`baseline create` without workspace must exit "
        f"{EXIT_USER_ERROR}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_baseline_create_success_writes_baseline_json(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A successful baseline create writes ``baseline.json`` and
    the recorded hashes match the on-disk bytes.
    """
    workspace = _init_workspace(tmp_path)
    artifact = workspace / "my-skill.md"
    envelope = workspace / ".metacrucible" / ENVELOPE_FILE_NAME
    benchmark = workspace / BENCHMARK_FILE_NAME

    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`baseline create` on a fully-seeded workspace must exit "
        f"{EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    baseline_path = workspace / ".metacrucible" / BASELINE_FILE_NAME
    assert baseline_path.is_file(), (
        f"baseline.json must be written under the workspace "
        f".metacrucible/ directory; missing {baseline_path}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "OK", (
        f"baseline create success must report status=OK; got "
        f"{payload.get('status')!r}"
    )
    # Hash invariants: each recorded hash must match an
    # independent re-hash of the source bytes / payload so a
    # future drift fails loud.
    expected_artifact_hash = hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    expected_envelope_hash = hashlib.sha256(
        envelope.read_bytes()
    ).hexdigest()
    assert payload["artifact_hash"] == expected_artifact_hash, (
        f"artifact_hash must match SHA-256 of artifact bytes; got "
        f"{payload['artifact_hash']!r} expected "
        f"{expected_artifact_hash!r}"
    )
    assert payload["envelope_hash"] == expected_envelope_hash, (
        f"envelope_hash must match SHA-256 of envelope bytes; got "
        f"{payload['envelope_hash']!r} expected "
        f"{expected_envelope_hash!r}"
    )
    # ``benchmark_hash`` is computed via ``compute_benchmark_digest``
    # which is the canonical-JSON digest of the parsed records.
    # Re-load and re-digest to verify the recorded value.
    records = []
    for raw in benchmark.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        records.append(json.loads(raw))
    expected_benchmark_hash = hashlib.sha256(
        json.dumps(
            records, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()
    assert payload["benchmark_hash"] == expected_benchmark_hash, (
        f"benchmark_hash must match canonical-JSON digest of the "
        f"benchmark records; got {payload['benchmark_hash']!r} "
        f"expected {expected_benchmark_hash!r}"
    )
    # ``harness_sha`` must be a non-empty hex digest when
    # ``BUILTIN_PROFILES`` is populated (the MVP ``profiles.py``
    # ships the full harness so this is the common case).
    assert isinstance(payload["harness_sha"], str), (
        f"harness_sha must be a string; got "
        f"{type(payload['harness_sha']).__name__}"
    )
    if payload["harness_sha"]:
        # Empty string is the documented no-profiles fallback;
        # otherwise the value is a 64-hex SHA-256 digest.
        assert len(payload["harness_sha"]) == 64, (
            f"harness_sha must be a SHA-256 hex digest when "
            f"non-empty; got {payload['harness_sha']!r}"
        )
        int(payload["harness_sha"], 16)
    assert payload["allow_dirty_unrelated"] is False, (
        f"allow_dirty_unrelated must be False on the default "
        f"path; got {payload['allow_dirty_unrelated']!r}"
    )
    assert payload["dirty_files_at_creation"] == [], (
        f"dirty_files_at_creation must be empty on a clean "
        f"worktree; got {payload['dirty_files_at_creation']!r}"
    )
    # The on-disk baseline.json must echo the same payload so
    # downstream tooling can re-derive the inputs the baseline
    # pinned against.
    baseline_record = json.loads(
        baseline_path.read_text(encoding="utf-8")
    )
    assert baseline_record["schema_version"] == (
        "metacrucible.baseline.v1"
    ), (
        f"baseline.json must stamp schema_version="
        f"metacrucible.baseline.v1; got "
        f"{baseline_record.get('schema_version')!r}"
    )
    for key in (
        "created_at",
        "artifact_hash",
        "envelope_hash",
        "benchmark_hash",
        "harness_sha",
        "allow_dirty_unrelated",
        "dirty_files_at_creation",
    ):
        assert key in baseline_record, (
            f"baseline.json must carry the {key!r} field; got "
            f"keys {sorted(baseline_record.keys())!r}"
        )


def test_baseline_create_blocks_when_workspace_missing(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A non-existent workspace is BLOCKED with the
    ``baseline-workspace-missing`` blocker id.
    """
    missing_ws = tmp_path / "ws-missing-baseline"
    assert not missing_ws.exists()

    result = _run_metacrucible(
        ["baseline", "create", str(missing_ws), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`baseline create` on a missing workspace must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert BASELINE_WORKSPACE_MISSING_BLOCKER in blocker_ids, (
        f"baseline-workspace-missing blocker must surface in the "
        f"JSON output; got blocker_ids={blocker_ids!r}"
    )
    assert payload["status"] == "BLOCKED", (
        f"missing-workspace baseline must report status=BLOCKED; "
        f"got {payload.get('status')!r}"
    )


def test_baseline_create_blocks_when_envelope_missing(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """An existing workspace without ``envelope.json`` is BLOCKED
    with the ``baseline-envelope-missing`` blocker id.

    The fixture seeds ``benchmark.jsonl`` only so the
    envelope-missing path is the first precondition failure.
    """
    workspace = tmp_path / "ws-baseline-no-env"
    workspace.mkdir(parents=True, exist_ok=True)
    # Seed the benchmark so the next-failing precondition is the
    # envelope; otherwise the workspace-missing blocker would
    # dominate and the test would not exercise the envelope path.
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record()])
    assert not (workspace / ".metacrucible").exists(), (
        f"fixture invariant: workspace must not have "
        f".metacrucible/; found {workspace / '.metacrucible'}"
    )

    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`baseline create` on a missing-envelope workspace must "
        f"exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert BASELINE_ENVELOPE_MISSING_BLOCKER in blocker_ids, (
        f"baseline-envelope-missing blocker must surface; got "
        f"blocker_ids={blocker_ids!r}"
    )


def test_baseline_create_blocks_when_benchmark_missing(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A workspace without ``benchmark.jsonl`` is BLOCKED with
    the ``baseline-benchmark-missing`` blocker id.
    """
    workspace = tmp_path / "ws-baseline-no-bench"
    workspace.mkdir(parents=True, exist_ok=True)
    # Run ``init`` to create the envelope (so envelope-precondition
    # is satisfied), then delete the benchmark so the
    # benchmark-precondition is the failing one.
    result = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`init` must exit 0; got {result.returncode}"
    )
    benchmark = workspace / BENCHMARK_FILE_NAME
    benchmark.unlink()
    # Seed an artifact path in the envelope so the artifact-
    # precondition is also satisfied (the test exercises the
    # benchmark-missing path, not the artifact-unresolved path).
    artifact = workspace / "my-skill.md"
    artifact.write_text(
        "---\nname: my-skill\n---\nbody\n", encoding="utf-8"
    )
    envelope = workspace / ".metacrucible" / ENVELOPE_FILE_NAME
    envelope_data = json.loads(envelope.read_text(encoding="utf-8"))
    envelope_data["artifact_path"] = str(artifact.resolve())
    envelope.write_text(
        json.dumps(envelope_data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`baseline create` on a missing-benchmark workspace must "
        f"exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert BASELINE_BENCHMARK_MISSING_BLOCKER in blocker_ids, (
        f"baseline-benchmark-missing blocker must surface; got "
        f"blocker_ids={blocker_ids!r}"
    )
    # No baseline.json must have been written on a BLOCKED call.
    baseline_path = workspace / ".metacrucible" / BASELINE_FILE_NAME
    assert not baseline_path.exists(), (
        f"baseline BLOCKED must NOT create baseline.json; found "
        f"{baseline_path}"
    )


def test_baseline_create_blocks_when_artifact_path_unresolved(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """An envelope missing ``artifact_path`` is BLOCKED with
    the ``baseline-artifact-unresolved`` blocker id.

    The command refuses to scan / glob the workspace (per OD1);
    a missing declaration surfaces as this stable id rather than
    a silent guess.
    """
    workspace = _init_workspace(tmp_path)
    # Strip the artifact_path field from the envelope so the
    # artifact-resolved precondition fails. The artifact file
    # itself is left in place so we can verify the command
    # really did NOT scan / glob the workspace to find it.
    envelope = workspace / ".metacrucible" / ENVELOPE_FILE_NAME
    envelope_data = json.loads(envelope.read_text(encoding="utf-8"))
    envelope_data.pop("artifact_path", None)
    envelope_data.pop("canonical_source", None)
    envelope.write_text(
        json.dumps(envelope_data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`baseline create` on an envelope without artifact_path "
        f"must exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert BASELINE_ARTIFACT_UNRESOLVED_BLOCKER in blocker_ids, (
        f"baseline-artifact-unresolved blocker must surface; got "
        f"blocker_ids={blocker_ids!r}"
    )
    baseline_path = workspace / ".metacrucible" / BASELINE_FILE_NAME
    assert not baseline_path.exists(), (
        f"baseline BLOCKED must NOT create baseline.json; found "
        f"{baseline_path}"
    )


def test_baseline_create_blocks_on_unrelated_dirty_files(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """An unrelated dirty file blocks ``baseline create`` with
    the ``baseline-unrelated-dirty-files`` blocker id, and no
    ``baseline.json`` is written.
    """
    workspace = _init_workspace(tmp_path)
    # Create an unrelated dirty file (untracked) so ``git status
    # --porcelain`` reports it as ``?? <path>`` and the guard
    # classifies it as unrelated.
    (workspace / "scratch-notes.txt").write_text(
        "untracked; not a baseline input\n",
        encoding="utf-8",
    )
    dirty = _git_dirty_paths(workspace)
    assert "scratch-notes.txt" in dirty, (
        f"fixture invariant: scratch-notes.txt must be reported "
        f"as dirty; got dirty={dirty!r}"
    )

    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`baseline create` on a worktree with unrelated dirty "
        f"files must exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert BASELINE_UNRELATED_DIRTY_FILES_BLOCKER in blocker_ids, (
        f"baseline-unrelated-dirty-files blocker must surface; "
        f"got blocker_ids={blocker_ids!r}"
    )
    assert payload["status"] == "BLOCKED", (
        f"unrelated-dirty baseline must report status=BLOCKED; "
        f"got {payload.get('status')!r}"
    )
    baseline_path = workspace / ".metacrucible" / BASELINE_FILE_NAME
    assert not baseline_path.exists(), (
        f"baseline BLOCKED must NOT create baseline.json; found "
        f"{baseline_path}"
    )


def test_baseline_create_allows_unrelated_dirty_with_flag(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``--allow-dirty-unrelated`` records the dirty list and
    proceeds: success exit, ``allow_dirty_unrelated: true``,
    ``dirty_files_at_creation`` populated.
    """
    workspace = _init_workspace(tmp_path)
    (workspace / "scratch-notes.txt").write_text(
        "untracked; not a baseline input\n",
        encoding="utf-8",
    )

    result = _run_metacrucible(
        [
            "baseline",
            "create",
            str(workspace),
            "--allow-dirty-unrelated",
            "--json",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`baseline create --allow-dirty-unrelated` must exit "
        f"{EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "OK", (
        f"--allow-dirty-unrelated success must report status=OK; "
        f"got {payload.get('status')!r}"
    )
    assert payload["allow_dirty_unrelated"] is True, (
        f"allow_dirty_unrelated must be True when the flag is "
        f"set; got {payload['allow_dirty_unrelated']!r}"
    )
    assert "scratch-notes.txt" in (
        payload.get("dirty_files_at_creation") or []
    ), (
        f"dirty_files_at_creation must record the unrelated "
        f"dirty file; got "
        f"{payload.get('dirty_files_at_creation')!r}"
    )
    # baseline.json must also record the same flag and dirty
    # list so a downstream reader sees the same audit trail.
    baseline_path = workspace / ".metacrucible" / BASELINE_FILE_NAME
    record = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert record["allow_dirty_unrelated"] is True, (
        f"on-disk baseline.json must record "
        f"allow_dirty_unrelated=True; got "
        f"{record.get('allow_dirty_unrelated')!r}"
    )
    assert "scratch-notes.txt" in (
        record.get("dirty_files_at_creation") or []
    ), (
        f"on-disk baseline.json must record the dirty file list; "
        f"got {record.get('dirty_files_at_creation')!r}"
    )


def test_baseline_create_allows_only_tracked_inputs_dirty(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A dirty file that IS one of the baseline inputs does NOT
    block ``baseline create``: the guard only blocks unrelated
    dirty files.
    """
    workspace = _init_workspace(tmp_path)
    artifact = workspace / "my-skill.md"
    # Modify the tracked artifact (one of the baseline inputs)
    # without committing. ``git status --porcelain`` reports it
    # as `` M my-skill.md`` and the guard must treat it as a
    # baseline input, not unrelated.
    artifact.write_text(
        "---\nname: my-skill\n---\nupdated body content\n",
        encoding="utf-8",
    )
    dirty = _git_dirty_paths(workspace)
    assert "my-skill.md" in dirty, (
        f"fixture invariant: my-skill.md must be reported as "
        f"dirty after the edit; got dirty={dirty!r}"
    )

    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`baseline create` with only tracked baseline inputs "
        f"dirty must exit {EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "OK", (
        f"baseline create with tracked-input dirty must report "
        f"status=OK; got {payload.get('status')!r}"
    )
    # The recorded artifact_hash must reflect the post-edit
    # bytes; the baseline pins the current state, not the last
    # commit.
    post_edit_hash = hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    assert payload["artifact_hash"] == post_edit_hash, (
        f"artifact_hash must reflect the current (post-edit) "
        f"bytes; got {payload['artifact_hash']!r} expected "
        f"{post_edit_hash!r}"
    )


def test_baseline_create_does_not_mutate_inputs(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``baseline create`` must not mutate envelope.json, state.json,
    history.jsonl, the artifact, or benchmark.jsonl.

    Only ``baseline.json`` is written. The test pins each input
    file's bytes around the call so an accidental write fails
    loud.
    """
    workspace = _init_workspace(tmp_path)
    artifact = workspace / "my-skill.md"
    envelope = workspace / ".metacrucible" / ENVELOPE_FILE_NAME
    state = workspace / ".metacrucible" / "state.json"
    history = workspace / ".metacrucible" / "history.jsonl"
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact_bytes = artifact.read_bytes()
    envelope_bytes = envelope.read_bytes()
    state_bytes = (
        state.read_bytes() if state.is_file() else None
    )
    history_bytes = (
        history.read_bytes() if history.is_file() else None
    )
    benchmark_bytes = benchmark.read_bytes()

    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`baseline create` on a fully-seeded workspace must exit "
        f"{EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Each input file's bytes must be unchanged. ``state.json``
    # and ``history.jsonl`` may not exist (init does not write
    # them by default); when absent the absence must remain
    # absent. ``envelope.json`` / ``benchmark.jsonl`` /
    # ``my-skill.md`` are seeded by the fixture and must remain.
    assert artifact.read_bytes() == artifact_bytes, (
        f"baseline create must not mutate the artifact; "
        f"before={artifact_bytes!r} after={artifact.read_bytes()!r}"
    )
    assert envelope.read_bytes() == envelope_bytes, (
        f"baseline create must not mutate envelope.json; "
        f"before={envelope_bytes!r} after={envelope.read_bytes()!r}"
    )
    assert benchmark.read_bytes() == benchmark_bytes, (
        f"baseline create must not mutate benchmark.jsonl; "
        f"before={benchmark_bytes!r} after="
        f"{benchmark.read_bytes()!r}"
    )
    if state_bytes is None:
        assert not state.exists(), (
            f"baseline create must not create state.json; found "
            f"{state}"
        )
    else:
        assert state.read_bytes() == state_bytes, (
            f"baseline create must not mutate state.json; "
            f"before={state_bytes!r} after={state.read_bytes()!r}"
        )
    if history_bytes is None:
        assert not history.exists(), (
            f"baseline create must not create history.jsonl; "
            f"found {history}"
        )
    else:
        assert history.read_bytes() == history_bytes, (
            f"baseline create must not mutate history.jsonl; "
            f"before={history_bytes!r} after="
            f"{history.read_bytes()!r}"
        )


def test_baseline_create_json_output_is_parseable(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``baseline create --json`` emits a parseable JSON object
    with the canonical machine-stable keys.
    """
    workspace = _init_workspace(tmp_path)
    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`baseline create --json` must exit {EXIT_OK}; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`baseline create --json` must emit valid JSON on "
            f"stdout; got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"baseline create --json must emit a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    for key in (
        "status",
        "workspace",
        "baseline_path",
        "schema_version",
        "created_at",
        "artifact_hash",
        "envelope_hash",
        "benchmark_hash",
        "harness_sha",
        "allow_dirty_unrelated",
        "dirty_files_at_creation",
        "git_worktree",
        "blockers",
    ):
        assert key in payload, (
            f"baseline create --json must surface {key!r}; got "
            f"keys {sorted(payload.keys())!r}"
        )
    assert payload["status"] == "OK", (
        f"status must be OK on the success path; got "
        f"{payload['status']!r}"
    )
    assert payload["schema_version"] == "metacrucible.baseline.v1", (
        f"schema_version must be metacrucible.baseline.v1; got "
        f"{payload['schema_version']!r}"
    )
    assert payload["blockers"] == [], (
        f"blockers must be empty on the success path; got "
        f"{payload['blockers']!r}"
    )


def test_baseline_create_overwrites_existing_baseline(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A second ``baseline create`` succeeds with an updated
    ``created_at`` timestamp; the prior ``baseline.json`` is
    overwritten.
    """
    workspace = _init_workspace(tmp_path)
    first = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert first.returncode == EXIT_OK, (
        f"first `baseline create` must exit {EXIT_OK}; got "
        f"rc={first.returncode} stdout={first.stdout!r} "
        f"stderr={first.stderr!r}"
    )
    first_payload = json.loads(first.stdout)
    first_created_at = first_payload["created_at"]
    baseline_path = workspace / ".metacrucible" / BASELINE_FILE_NAME
    assert baseline_path.is_file(), (
        f"baseline.json must exist after the first create; "
        f"missing {baseline_path}"
    )

    # Sleep just long enough that the timestamp granularity
    # (``timespec="seconds"``) is guaranteed to differ. A bare
    # ``time.sleep(1.1)`` would be enough but we use a longer
    # window so a slow CI does not flake.
    import time
    time.sleep(1.2)

    second = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert second.returncode == EXIT_OK, (
        f"second `baseline create` must exit {EXIT_OK}; got "
        f"rc={second.returncode} stdout={second.stdout!r} "
        f"stderr={second.stderr!r}"
    )
    second_payload = json.loads(second.stdout)
    assert second_payload["created_at"] != first_created_at, (
        f"second baseline create must bump created_at; first="
        f"{first_created_at!r} second="
        f"{second_payload['created_at']!r}"
    )
    # Hashes must remain identical because the inputs did not
    # change between the two calls.
    for key in (
        "artifact_hash",
        "envelope_hash",
        "benchmark_hash",
        "harness_sha",
    ):
        assert second_payload[key] == first_payload[key], (
            f"{key} must be stable across two consecutive "
            f"baseline creates with unchanged inputs; first="
            f"{first_payload[key]!r} second="
            f"{second_payload[key]!r}"
        )


def test_baseline_create_skips_dirty_guard_outside_worktree(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A workspace outside any git worktree skips the dirty-file
    guard with a stderr warning; ``baseline create`` succeeds.

    OD3 pins the contract: the guard is a no-op outside a
    worktree so the operator can see the silent-skip via
    stderr.
    """
    workspace = tmp_path / "ws-baseline-no-git"
    workspace.mkdir(parents=True, exist_ok=True)
    # Initialise the workspace WITHOUT a git worktree. The
    # baseline create dirty-file guard must skip the check and
    # emit a stderr warning so the operator sees the silent-
    # skip (per OD3).
    result = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`init` must exit 0; got {result.returncode}"
    )
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _reviewed_case("no-git-eval-1", split="eval"),
            _reviewed_case("no-git-held-1", split="held_out"),
        ],
    )
    artifact = workspace / "my-skill.md"
    artifact.write_text(
        "---\nname: my-skill\n---\nbody\n", encoding="utf-8"
    )
    envelope = workspace / ".metacrucible" / ENVELOPE_FILE_NAME
    envelope_data = json.loads(envelope.read_text(encoding="utf-8"))
    envelope_data["artifact_path"] = str(artifact.resolve())
    envelope.write_text(
        json.dumps(envelope_data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = _run_metacrucible(
        ["baseline", "create", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`baseline create` outside a worktree must exit "
        f"{EXIT_OK} (dirty guard is skipped per OD3); got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "git worktree" in result.stderr, (
        f"non-worktree baseline must surface a stderr warning so "
        f"the operator sees the dirty-guard skip; got "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["git_worktree"] is False, (
        f"git_worktree flag must be False on a non-worktree "
        f"workspace; got {payload.get('git_worktree')!r}"
    )
    assert payload["status"] == "OK", (
        f"non-worktree baseline must report status=OK; got "
        f"{payload.get('status')!r}"
    )
