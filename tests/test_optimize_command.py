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
from typing import Any, Iterable

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
        "workspace",
        "benchmark",
        "benchmark_present",
        "is_optimize_runnable",
        "pending_review_case_ids",
        "blockers",
    ):
        assert key in payload, (
            f"optimize --json must surface {key!r}; got keys "
            f"{sorted(payload.keys())!r}"
        )
    assert payload["benchmark_present"] is True, (
        f"benchmark_present must be True for a present "
        f"benchmark; got {payload['benchmark_present']!r}"
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

def test_optimize_clean_benchmark_reports_not_yet_implemented(
    tmp_path: Path,
) -> None:
    """A benchmark that is otherwise optimize-runnable
    (eligible reviewed eval + held-out cases, no pending
    generated, no bootstrap sentinel) still returns
    ``EXIT_BLOCKED`` with the ``optimize-not-implemented``
    blocker.

    Full optimization is W3 per the PRD; the MVP command
    is a sentinel gate that refuses to start with a stable
    blocker id so the contract is "we will refuse with a
    clear reason" rather than "we silently do nothing".
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
        f"`optimize` on a clean benchmark must exit "
        f"{EXIT_BLOCKED} (full optimization is W3); got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["benchmark_present"] is True
    assert payload["is_optimize_runnable"] is True, (
        f"is_optimize_runnable must be True for a clean "
        f"benchmark; got {payload['is_optimize_runnable']!r}"
    )
    assert payload["pending_review_case_ids"] == [], (
        f"pending_review_case_ids must be empty for a clean "
        f"benchmark; got {payload['pending_review_case_ids']!r}"
    )
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert OPTIMIZE_NOT_IMPLEMENTED_BLOCKER in blocker_ids, (
        f"optimize on a clean benchmark must surface the "
        f"optimize-not-implemented blocker (W3 placeholder); "
        f"got blocker_ids={blocker_ids!r}"
    )
    # No other load-bearing blockers should be present on
    # the clean-benchmark path: the loader reported no
    # blockers (eligible eval + held-out, no pending
    # generated), so the only blocker in the payload is
    # the W3 placeholder.
    assert blocker_ids == [OPTIMIZE_NOT_IMPLEMENTED_BLOCKER], (
        f"clean benchmark path must report exactly the "
        f"optimize-not-implemented blocker; got "
        f"blocker_ids={blocker_ids!r}"
    )


# --------------------------------------------------------------------------- #
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
    assert payload["benchmark_present"] is False, (
        f"benchmark_present must be False for a missing "
        f"file; got {payload['benchmark_present']!r}"
    )
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
