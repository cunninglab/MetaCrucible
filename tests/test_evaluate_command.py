# --------------------------------------------------------------------------- #
# Tests for ``metacrucible evaluate`` (Issue #32).                           #
# --------------------------------------------------------------------------- #
"""Acceptance tests for the ``metacrucible evaluate`` subcommand.

The contract (Issue #32 / ADR 0029):

  - ``metacrucible evaluate <workspace> [--split {all,eval,held_out}] [--json]``
    discovers the workspace's ``benchmark.jsonl`` and evaluates the
    eligible reviewed cases in the selected split partition.
  - The top-level verdict vocabulary mirrors the F1 review execution
    evaluation: ``PASS`` (all selected cases passed) / ``FAILED`` (at
    least one case failed, none blocked) / ``BLOCKED`` (a precondition
    failed or at least one case was blocked).
  - Missing benchmark is BLOCKED (precondition failure) with the
    ``evaluate-benchmark-missing`` id, not SKIPPED + warning like
    ``review``. The ``evaluate`` category is in the ADR 0035 BLOCKED
    bundle matrix.
  - The workspace is read-only: ``evaluate`` never mutates the
    benchmark file, the envelope, the artifact, or the workspace
    ``.metacrucible/`` tree. Evidence bundles are written only to
    the user-global store (``$HOME/.metacrucible/``).
  - The CLI is English-only on the human-output surface.

The tests use ``python -m metacrucible`` so the same subprocess
pattern the rest of the CLI test suite uses is exercised end-to-end.
``HOME`` is pinned to ``tmp_path`` via the ``isolated_global_home``
fixture so :class:`UserGlobalStorage` does not pollute the developer's
real ``~/.metacrucible/`` while the BLOCKED-bundle writers fire.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from metacrucible.exit_codes import (
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_USER_ERROR,
)

# --------------------------------------------------------------------------- #
# Constants pinned by the Issue #32 contract                                  #
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Machine-stable split value accepted by ``--split`` (Issue #32).
#: Re-pinned locally so the test module is self-contained.
EVALUATE_SPLIT_ALL = "all"
EVALUATE_SPLIT_EVAL = "eval"
EVALUATE_SPLIT_HELD_OUT = "held_out"

#: Machine-stable blocker ids emitted by ``evaluate`` (Issue #32).
#: Pinned here so a future rename fails the test loud.
EVALUATE_BENCHMARK_MISSING_BLOCKER = "evaluate-benchmark-missing"
EVALUATE_NO_ELIGIBLE_CASES_BLOCKER = "evaluate-no-eligible-cases"

#: Per-case status values from the execution engine. Pinned
#: locally so a future rename of the underlying constant fails
#: the test loud.
REVIEW_CASE_STATUS_PASS = "PASS"
REVIEW_CASE_STATUS_FAIL = "FAIL"
REVIEW_CASE_STATUS_BLOCKED = "BLOCKED"

#: Top-level evaluate status values. Same set as the F1 review
#: execution evaluation status vocabulary; pinned to a
#: machine-stable set so downstream tooling can branch on them.
EXECUTION_STATUS_PASS = "PASS"
EXECUTION_STATUS_FAIL = "FAIL"
EXECUTION_STATUS_BLOCKED = "BLOCKED"

#: Subset of evaluate output keys whose presence in the JSON
#: payload is part of the machine contract. The keys are the
#: canonical schema fields downstream automation branches on;
#: renaming any of them is a breaking change.
EVALUATE_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "benchmark",
    "benchmark_path",
    "blockers",
    "case_results",
    "cases_evaluated",
    "cases_failed",
    "cases_passed",
    "split",
    "status",
    "workspace",
)

# --------------------------------------------------------------------------- #
# Fixtures + helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture()
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


def _run_metacrucible(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with captured text output.

    Mirrors the helper in :mod:`tests.test_review_command` so the
    evaluate tests use the same subprocess pattern the rest of
    the CLI test suite uses.
    """
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _write_jsonl(
    path: Path, records: Iterable[dict[str, Any]]
) -> Path:
    """Write ``records`` as one JSON object per line at ``path``.

    Mirrors the helper in :mod:`tests.test_review_command` so the
    evaluate tests can seed a minimal benchmark container.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(dict(rec), sort_keys=True) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _metadata_record() -> dict[str, Any]:
    """Build a minimal valid benchmark metadata record (ADR 0029)."""
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
    }


def _reviewed_case(
    case_id: str,
    *,
    split: str = "eval",
    expected_output: str | None = None,
) -> dict[str, Any]:
    """Build a minimal eligible reviewed case record (ADR 0029).

    ``expected_output`` is the fixture the F1 deterministic
    check engine consumes; the default
    (``"The thing worked."``) contains the pattern the
    default ``checks`` list below grep for, so the case
    passes out of the box. Tests that exercise the FAILED /
    BLOCKED paths override this value to drive the case
    evaluator through its other branches.
    """
    output = (
        expected_output
        if expected_output is not None
        else "The thing worked."
    )
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "reviewed",
        "split": split,
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "expected_output": output,
        "checks": [
            {"name": "output_contains_thing", "pattern": "thing"}
        ],
    }


def _no_checks_case(case_id: str, *, split: str) -> dict[str, Any]:
    """Build a reviewed case with neither ``checks`` nor
    ``judgment`` (per ADR 0010 this is a BLOCKED per-case
    verdict via the no-checks-or-judgment blocker).
    """
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "reviewed",
        "split": split,
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
    }


def _run_evaluate(
    *,
    tmp_path: Path,
    isolated_global_home: Path,
    benchmark_records: list[dict[str, Any]] | None = None,
    extra_args: list[str] | None = None,
    workspace_path: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run ``metacrucible evaluate`` end-to-end and return artifacts.

    The workspace path and the captured subprocess result are
    returned so each test can assert on its slice of state.
    ``HOME`` is pinned to the ``isolated_global_home`` fixture
    so the test does not leak evidence bundles into the
    developer's real ``~/.metacrucible/``.

    When ``workspace_path`` is omitted the workspace defaults
    to a fresh ``tmp_path / "ws"``. When ``benchmark_records`` is
    provided, the helper writes the records as ``benchmark.jsonl``
    in the workspace; when it is ``None`` no benchmark file is
    created (the "no benchmark" path).
    """
    workspace = workspace_path or (tmp_path / "ws")
    workspace.mkdir(parents=True, exist_ok=True)

    if benchmark_records is not None:
        benchmark = workspace / BENCHMARK_FILE_NAME
        _write_jsonl(benchmark, benchmark_records)

    argv = ["evaluate", str(workspace)]
    if extra_args:
        argv.extend(extra_args)
    result = _run_metacrucible(argv, cwd=REPO_ROOT)
    return result, workspace


# --------------------------------------------------------------------------- #
# AC1 — ``evaluate`` is a recognized subcommand                               #
# --------------------------------------------------------------------------- #


def test_evaluate_subcommand_is_recognized() -> None:
    """``metacrucible evaluate --help`` exits 0 and advertises
    the contract: workspace positional, ``--split`` flag with
    the three pinned choices, and ``--json``.
    """
    result = _run_metacrucible(
        ["evaluate", "--help"], cwd=REPO_ROOT
    )
    assert result.returncode == EXIT_OK, (
        f"`metacrucible evaluate --help` must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "evaluate" in result.stdout, (
        f"evaluate --help must mention the subcommand name; "
        f"got {result.stdout!r}"
    )
    assert "workspace" in result.stdout, (
        f"evaluate --help must advertise the workspace positional; "
        f"got {result.stdout!r}"
    )
    assert "--split" in result.stdout, (
        f"evaluate --help must advertise the --split flag; got "
        f"{result.stdout!r}"
    )
    assert "all" in result.stdout, (
        f"evaluate --help must list the 'all' split choice; "
        f"got {result.stdout!r}"
    )
    assert "eval" in result.stdout, (
        f"evaluate --help must list the 'eval' split choice; "
        f"got {result.stdout!r}"
    )
    assert "held_out" in result.stdout, (
        f"evaluate --help must list the 'held_out' split choice; "
        f"got {result.stdout!r}"
    )
    assert "--json" in result.stdout, (
        f"evaluate --help must advertise the --json flag; got "
        f"{result.stdout!r}"
    )


def test_evaluate_rejects_invalid_split() -> None:
    """``--split bad`` exits ``EXIT_USER_ERROR`` (argparse usage).

    Argparse constrains ``--split`` to the three pinned values
    via ``choices=``; an unrecognised value is mapped by
    :func:`metacrucible.__main__.main` to
    :data:`metacrucible.exit_codes.EXIT_USER_ERROR`.
    """
    result = _run_metacrucible(
        ["evaluate", str(REPO_ROOT / "ws-irrelevant"), "--split", "bad"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_USER_ERROR, (
        f"`evaluate --split bad` must exit {EXIT_USER_ERROR} "
        f"(argparse usage); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# AC2 — happy path: both partitions present                                   #
# --------------------------------------------------------------------------- #


def test_evaluate_split_all_pass(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``--split all`` with 1 eval + 1 held_out reviewed cases
    returns ``EXIT_OK`` with status ``PASS`` and
    ``cases_evaluated == 2``.

    ADR 0029: ``--split all`` evaluates the union of the eval
    and held_out partitions; PASS only when every eligible
    case passes. The default ``--split`` value is ``all`` so
    the same payload without ``--split`` must produce the
    identical verdict.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, workspace = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--split", EVALUATE_SPLIT_ALL, "--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`evaluate --split all` with two passing cases must exit "
        f"{EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    for key in EVALUATE_TOP_LEVEL_KEYS:
        assert key in payload, (
            f"evaluate --json must surface top-level key {key!r}; "
            f"got keys {sorted(payload.keys())!r}"
        )
    assert payload["status"] == "PASS", (
        f"status must be PASS when every selected case passes; "
        f"got {payload['status']!r}"
    )
    assert payload["split"] == EVALUATE_SPLIT_ALL, (
        f"split must echo the --split argument; got "
        f"{payload['split']!r}"
    )
    assert payload["cases_evaluated"] == 2, (
        f"cases_evaluated must equal the number of selected "
        f"cases (1 eval + 1 held_out = 2); got "
        f"{payload['cases_evaluated']!r}"
    )
    assert payload["cases_passed"] == 2, (
        f"cases_passed must equal cases_evaluated for a clean "
        f"PASS; got {payload['cases_passed']!r}"
    )
    assert payload["cases_failed"] == 0, (
        f"cases_failed must be 0 on the PASS path; got "
        f"{payload['cases_failed']!r}"
    )
    assert payload["blockers"] == [], (
        f"blockers must be empty on the PASS path; got "
        f"{payload['blockers']!r}"
    )
    case_results = payload["case_results"]
    assert len(case_results) == 2, (
        f"case_results must carry one entry per evaluated case; "
        f"got {len(case_results)} entries"
    )
    by_id = {r["case_id"]: r for r in case_results}
    assert by_id["case-eval-1"]["status"] == REVIEW_CASE_STATUS_PASS
    assert by_id["case-held-1"]["status"] == REVIEW_CASE_STATUS_PASS
    # The benchmark sub-section reports the discovered state.
    benchmark = payload["benchmark"]
    assert benchmark["present"] is True
    assert benchmark["eligible_eval_count"] == 1
    assert benchmark["eligible_held_out_count"] == 1


# --------------------------------------------------------------------------- #
# AC3 — split filter: only the matching partition is evaluated                #
# --------------------------------------------------------------------------- #


def test_evaluate_split_eval_ignores_held_out(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``--split eval`` with 1 eval + 1 held_out case evaluates
    only the eval case (``cases_evaluated == 1``).

    Both partitions are seeded so the ADR 0025 loader does
    not surface the missing-reviewed-held-out-case blocker;
    the test exercises the split filter, not the loader gate.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--split", EVALUATE_SPLIT_EVAL, "--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`evaluate --split eval` must exit {EXIT_OK} when the "
        f"eval case passes; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS", (
        f"status must be PASS for a passing eval case; got "
        f"{payload['status']!r}"
    )
    assert payload["split"] == EVALUATE_SPLIT_EVAL
    assert payload["cases_evaluated"] == 1, (
        f"--split eval must evaluate only the eval case; got "
        f"cases_evaluated={payload['cases_evaluated']!r}"
    )
    case_results = payload["case_results"]
    assert len(case_results) == 1
    assert case_results[0]["case_id"] == "case-eval-1", (
        f"--split eval must skip the held_out case; got "
        f"case_results={case_results!r}"
    )


def test_evaluate_split_held_out_ignores_eval(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``--split held_out`` with 1 eval + 1 held_out case
    evaluates only the held_out case (``cases_evaluated == 1``).

    Both partitions are seeded so the ADR 0025 loader does
    not surface the missing-reviewed-eval-case blocker; the
    test exercises the split filter, not the loader gate.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--split", EVALUATE_SPLIT_HELD_OUT, "--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`evaluate --split held_out` must exit {EXIT_OK} when "
        f"the held_out case passes; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["split"] == EVALUATE_SPLIT_HELD_OUT
    assert payload["cases_evaluated"] == 1, (
        f"--split held_out must evaluate only the held_out case; "
        f"got cases_evaluated={payload['cases_evaluated']!r}"
    )
    case_results = payload["case_results"]
    assert len(case_results) == 1
    assert case_results[0]["case_id"] == "case-held-1", (
        f"--split held_out must skip the eval case; got "
        f"case_results={case_results!r}"
    )


# --------------------------------------------------------------------------- #
# AC4 — precondition failures BLOCK the run                                   #
# --------------------------------------------------------------------------- #


def test_evaluate_missing_benchmark_blocks(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A workspace without ``benchmark.jsonl`` is BLOCKED with
    the ``evaluate-benchmark-missing`` blocker id.

    Unlike ``review`` (which treats a missing benchmark as a
    static+warning path), ``evaluate`` is a support command
    whose explicit purpose is evaluation, so a missing
    benchmark is a precondition failure.
    """
    workspace = tmp_path / "ws-eval-missing"
    workspace.mkdir(parents=True, exist_ok=True)
    # Do NOT create benchmark.jsonl.
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=None,
        extra_args=["--json"],
        workspace_path=workspace,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`evaluate` on a missing-benchmark workspace must exit "
        f"{EXIT_BLOCKED} (precondition failure); got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "BLOCKED", (
        f"status must be BLOCKED for a missing-benchmark "
        f"workspace; got {payload['status']!r}"
    )
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert EVALUATE_BENCHMARK_MISSING_BLOCKER in blocker_ids, (
        f"evaluate-benchmark-missing blocker must surface; got "
        f"blocker_ids={blocker_ids!r}"
    )
    assert payload["cases_evaluated"] == 0
    assert payload["cases_passed"] == 0
    assert payload["cases_failed"] == 0
    assert payload["case_results"] == []
    # The benchmark sub-section reports the missing-file state.
    benchmark = payload["benchmark"]
    assert benchmark["present"] is False


# --------------------------------------------------------------------------- #
# AC5 — per-case FAILED and BLOCKED propagation                               #
# --------------------------------------------------------------------------- #


def test_evaluate_failed_case_returns_failed(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A case whose ``expected_output`` does NOT contain the
    check's pattern is FAILED. The overall ``status`` is
    ``FAILED``, ``cases_failed == 1``, and the exit code is
    ``EXIT_BLOCKED`` (the existing matrix has no separate
    FAILED code; the negative outcome is observable from the
    JSON ``status`` field).

    No BLOCKED bundle is written on FAILED: the run executed
    end-to-end and produced a real verdict, so the BLOCKED
    bundle (which is the "we could not proceed" record) is
    not the right evidence shape.

    Both partitions are seeded so the ADR 0025 loader does
    not surface the missing-reviewed-held-out-case blocker;
    the per-case FAILED verdict is what the test exercises.
    """
    records = [
        _metadata_record(),
        _reviewed_case(
            "case-fail-1",
            split="eval",
            # The default checks pattern is "thing"; this
            # output does NOT contain it.
            expected_output="An entirely different outcome.",
        ),
        # Passing held_out case so the loader does not surface
        # the missing-reviewed-held-out-case blocker first.
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`evaluate` with one FAILED case must exit "
        f"{EXIT_BLOCKED} (negative verdict); got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == EXECUTION_STATUS_FAIL, (
        f"status must be FAILED when at least one case failed "
        f"with no BLOCKED cases mixed in; got "
        f"{payload['status']!r}"
    )
    assert payload["cases_evaluated"] == 2
    assert payload["cases_passed"] == 1, (
        f"cases_passed must reflect the passing held_out case; "
        f"got {payload['cases_passed']!r}"
    )
    assert payload["cases_failed"] == 1, (
        f"cases_failed must reflect the actual run; got "
        f"{payload['cases_failed']!r}"
    )
    assert payload["blockers"] == [], (
        f"top-level blockers must be empty on a pure FAILED "
        f"verdict; got {payload['blockers']!r}"
    )
    case_results = payload["case_results"]
    assert len(case_results) == 2
    by_id = {r["case_id"]: r for r in case_results}
    assert by_id["case-fail-1"]["status"] == REVIEW_CASE_STATUS_FAIL
    assert by_id["case-held-1"]["status"] == REVIEW_CASE_STATUS_PASS
    # FAILED: no BLOCKED bundle should have been written.
    evidence_root = (
        isolated_global_home / ".metacrucible" / "evidence"
    )
    evaluate_bundles = [
        p for p in evidence_root.glob("evaluate-*")
        if p.is_dir()
    ] if evidence_root.is_dir() else []
    assert evaluate_bundles == [], (
        f"FAILED outcome must NOT write an evaluate BLOCKED "
        f"bundle; got {evaluate_bundles!r}"
    )


def test_evaluate_blocked_case_returns_blocked(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A case with neither ``checks`` nor ``judgment`` is
    BLOCKED at the per-case level. The overall ``status`` is
    ``BLOCKED`` and the per-case blocker is rolled up into
    the top-level ``blockers`` list with a ``case_id`` field.

    The ADR 0035 ``evaluate`` BLOCKED bundle is written.

    Both partitions are seeded so the ADR 0025 loader does
    not surface the missing-reviewed-held-out-case blocker;
    the per-case BLOCKED verdict is what the test exercises.
    """
    records = [
        _metadata_record(),
        _no_checks_case("case-noop-1", split="eval"),
        # Passing held_out case so the loader does not surface
        # the missing-reviewed-held-out-case blocker first.
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`evaluate` with a BLOCKED case must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == EXECUTION_STATUS_BLOCKED, (
        f"status must be BLOCKED when at least one case is "
        f"blocked; got {payload['status']!r}"
    )
    assert payload["cases_evaluated"] == 2
    assert payload["cases_passed"] == 1
    assert payload["cases_failed"] == 0
    case_results = payload["case_results"]
    assert len(case_results) == 2
    by_id = {r["case_id"]: r for r in case_results}
    noop = by_id["case-noop-1"]
    assert noop["status"] == REVIEW_CASE_STATUS_BLOCKED
    per_case_blocker_ids = [
        b.get("id") for b in noop.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert "review-case-no-checks-or-judgment" in per_case_blocker_ids, (
        f"per-case blockers must include "
        f"review-case-no-checks-or-judgment; got "
        f"per_case_blocker_ids={per_case_blocker_ids!r}"
    )
    # The per-case blocker is rolled up into the top-level
    # blockers list with a ``case_id`` annotation so the
    # operator can see which case blocked and why.
    top_blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert "review-case-no-checks-or-judgment" in top_blocker_ids, (
        f"top-level blockers must include the rolled-up per-case "
        f"blocker; got top_blocker_ids={top_blocker_ids!r}"
    )
    case_id_annotations = [
        b.get("case_id") for b in payload.get("blockers", [])
        if isinstance(b, dict) and b.get("id")
        == "review-case-no-checks-or-judgment"
    ]
    assert "case-noop-1" in case_id_annotations, (
        f"rolled-up blocker must carry the originating case_id; "
        f"got case_id_annotations={case_id_annotations!r}"
    )
    # The ADR 0035 ``evaluate`` BLOCKED bundle is written for
    # the BLOCKED outcome so the receipt lineage carries the
    # "we could not proceed" record.
    evidence_root = (
        isolated_global_home / ".metacrucible" / "evidence"
    )
    evaluate_bundles = (
        sorted(p.name for p in evidence_root.glob("evaluate-*"))
        if evidence_root.is_dir() else []
    )
    assert evaluate_bundles, (
        f"BLOCKED outcome must write an evaluate BLOCKED bundle "
        f"under {evidence_root}; got bundles={evaluate_bundles!r}"
    )


# --------------------------------------------------------------------------- #
# AC6 — output contract: JSON is parseable, human is English-only             #
# --------------------------------------------------------------------------- #


def test_evaluate_json_emits_parseable_json(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``--json`` output parses as a JSON object with the
    canonical machine-stable keys.

    Both partitions are seeded so the ADR 0025 loader does
    not surface a missing-reviewed blocker; the test
    exercises the ``--json`` output contract, not the
    loader gate.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        # Passing held_out case so the loader does not surface
        # the missing-reviewed-held-out-case blocker first.
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`evaluate --json` on a passing case must exit "
        f"{EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`evaluate --json` must emit valid JSON on stdout; "
            f"got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"evaluate --json must emit a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    for key in EVALUATE_TOP_LEVEL_KEYS:
        assert key in payload, (
            f"evaluate --json must surface top-level key {key!r}; "
            f"got keys {sorted(payload.keys())!r}"
        )


def test_evaluate_human_output_is_english_only(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Human (no ``--json``) output is English-only.

    Issue #27 task 27.4 pins the CLI's own prose as
    English-only. The test checks the captured stdout and
    stderr for non-ASCII printable characters and asserts the
    ``status`` value (the machine contract key) is reachable
    in the human surface.

    Both partitions are seeded so the ADR 0025 loader does
    not surface a missing-reviewed blocker; the test
    exercises the human-output surface, not the loader gate.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        # Passing held_out case so the loader does not surface
        # the missing-reviewed-held-out-case blocker first.
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        # No --json: the human output is rendered.
    )
    assert result.returncode == EXIT_OK, (
        f"`evaluate` (human output) on a passing case must exit "
        f"{EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
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
    # The status field is the machine-stable top-level verdict
    # key; the human surface must echo it so an operator can
    # grep for the value without flipping to --json.
    combined = f"{result.stdout}\n{result.stderr}"
    assert EXECUTION_STATUS_PASS in combined, (
        f"human output must echo the status value "
        f"{EXECUTION_STATUS_PASS!r}; got stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# Gap coverage: behaviour the AC suite calls out implicitly but does not      #
# assert directly. Each test pins an *observable* consequence of the         #
# implementation -- exit code, JSON status, blockers list, on-disk BLOCKED    #
# bundle, or workspace file listing -- not an internal implementation detail. #
# --------------------------------------------------------------------------- #


def _evidence_bundles(home: Path) -> list[str]:
    """Return the sorted names of ``evaluate-*`` BLOCKED bundles."""
    evidence_root = home / ".metacrucible" / "evidence"
    if not evidence_root.is_dir():
        return []
    return sorted(p.name for p in evidence_root.glob("evaluate-*"))


def test_evaluate_aggregation_blocked_beats_failed_beats_pass(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Three cases (PASS, FAILED, BLOCKED) in the same run must
    aggregate to top-level ``BLOCKED`` because BLOCKED beats
    FAILED beats PASS (ADR 0029).

    The aggregator rolls up per-case BLOCKED blockers with a
    ``case_id`` annotation so the operator can see which case
    blocked and why. Aggregation is BLOCKED, so the BLOCKED
    bundle must be written even though a passing case is in
    the run.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-pass-1", split="eval"),
        _reviewed_case(
            "case-fail-1",
            split="eval",
            # The default ``thing`` pattern is absent in this
            # output, so the deterministic check engine FAILs.
            expected_output="An entirely different outcome.",
        ),
        _no_checks_case("case-block-1", split="eval"),
        # Passing held_out case so the loader does not surface
        # the missing-reviewed-held-out-case blocker first.
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--split", EVALUATE_SPLIT_EVAL, "--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`evaluate` with PASS + FAILED + BLOCKED cases must exit "
        f"{EXIT_BLOCKED} (BLOCKED wins); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == EXECUTION_STATUS_BLOCKED, (
        f"aggregation must pick BLOCKED when at least one case is "
        f"BLOCKED; got {payload['status']!r}"
    )
    assert payload["cases_evaluated"] == 3, (
        f"--split eval must evaluate only the eval cases "
        f"(3 cases seeded in eval); got "
        f"cases_evaluated={payload['cases_evaluated']!r}"
    )
    assert payload["cases_passed"] == 1, (
        f"cases_passed must count only the PASS case; got "
        f"{payload['cases_passed']!r}"
    )
    assert payload["cases_failed"] == 1, (
        f"cases_failed must count only the FAILED case; got "
        f"{payload['cases_failed']!r}"
    )
    top_blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert "review-case-no-checks-or-judgment" in top_blocker_ids, (
        f"per-case BLOCKED blocker must roll up into the top-level "
        f"blockers list; got top_blocker_ids={top_blocker_ids!r}"
    )
    case_id_annotations = [
        b.get("case_id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
        and b.get("id") == "review-case-no-checks-or-judgment"
    ]
    assert "case-block-1" in case_id_annotations, (
        f"rolled-up blocker must carry the originating case_id; "
        f"got case_id_annotations={case_id_annotations!r}"
    )
    assert _evidence_bundles(isolated_global_home), (
        f"BLOCKED aggregation must write an evaluate BLOCKED "
        f"bundle under {isolated_global_home / '.metacrucible' / 'evidence'}; "
        f"got bundles={_evidence_bundles(isolated_global_home)!r}"
    )


def test_evaluate_loader_schema_mismatch_blocks(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A benchmark with ``schema_version != 1`` surfaces the
    loader's ``schema-version-mismatch`` blocker; ``evaluate``
    propagates it verbatim and the run BLOCKS with
    :data:`EXIT_BLOCKED`.

    The ``evaluate`` command must NOT invent its own verdict
    when the loader already explained why the benchmark is
    not runnable (ADR 0029). The BLOCKED bundle is written
    so the receipt lineage carries the "we could not
    proceed" record.
    """
    records = [
        {
            "record_type": "metadata",
            "name": "default-benchmark",
            "schema_version": 2,  # Future-version fixture.
        },
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`evaluate` on a schema-mismatched benchmark must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == EXECUTION_STATUS_BLOCKED, (
        f"loader blocker must surface as BLOCKED top-level status; "
        f"got {payload['status']!r}"
    )
    top_blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert "schema-version-mismatch" in top_blocker_ids, (
        f"loader ``schema-version-mismatch`` blocker must be "
        f"propagated to the top-level blockers list; got "
        f"top_blocker_ids={top_blocker_ids!r}"
    )
    assert payload["cases_evaluated"] == 0, (
        f"no cases may be evaluated when the loader BLOCKed; got "
        f"cases_evaluated={payload['cases_evaluated']!r}"
    )
    assert payload["case_results"] == [], (
        f"case_results must be empty when the loader BLOCKed; got "
        f"{payload['case_results']!r}"
    )
    assert _evidence_bundles(isolated_global_home), (
        f"loader-blocker BLOCKED outcome must write an evaluate "
        f"BLOCKED bundle; got bundles="
        f"{_evidence_bundles(isolated_global_home)!r}"
    )


def test_evaluate_missing_benchmark_writes_blocked_bundle(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A missing-benchmark BLOCKED outcome must also write the
    ``evaluate`` BLOCKED bundle.

    :func:`cmd_evaluate` writes the bundle for *every* BLOCKED
    precondition, not just for loader and per-case BLOCKs.
    The bundle is the receipt lineage that records "we could
    not proceed" alongside the in-memory payload; missing it
    would leave the operator without evidence of the
    precondition failure.
    """
    workspace = tmp_path / "ws-eval-missing-bundle"
    workspace.mkdir(parents=True, exist_ok=True)
    # Do NOT create benchmark.jsonl.
    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=None,
        extra_args=["--json"],
        workspace_path=workspace,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"missing-benchmark workspace must exit {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert _evidence_bundles(isolated_global_home), (
        f"missing-benchmark BLOCKED outcome must write an "
        f"evaluate BLOCKED bundle; got bundles="
        f"{_evidence_bundles(isolated_global_home)!r}"
    )


def test_evaluate_is_read_only_over_workspace(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """The Issue #32 contract pins ``evaluate`` as read-only:
    the benchmark file, the envelope, the artifact, and the
    workspace ``.metacrucible/`` tree must all be unchanged
    after the run. Evidence is written only to the
    user-global store, mirroring ``review``'s read-only
    contract.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    workspace = tmp_path / "ws-readonly"
    workspace.mkdir(parents=True, exist_ok=True)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, records)

    def _snapshot(ws: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for entry in sorted(ws.rglob("*")):
            if entry.is_file():
                files[str(entry.relative_to(ws))] = (
                    entry.read_text(encoding="utf-8")
                )
        return files

    before_snapshot = _snapshot(workspace)
    assert before_snapshot, (
        f"workspace must contain the seeded benchmark file before "
        f"the run; got snapshot={before_snapshot!r}"
    )

    result, _ = _run_evaluate(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=None,  # benchmark already seeded above
        extra_args=["--json"],
        workspace_path=workspace,
    )
    assert result.returncode == EXIT_OK, (
        f"`evaluate` on a passing benchmark must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == EXECUTION_STATUS_PASS, (
        f"setup must produce a PASS payload; got {payload['status']!r}"
    )

    after_snapshot = _snapshot(workspace)
    assert after_snapshot == before_snapshot, (
        f"evaluate must NOT mutate the workspace; got "
        f"before={sorted(before_snapshot.keys())!r} "
        f"after={sorted(after_snapshot.keys())!r}"
    )
    # Evidence bundles must NOT appear under the workspace; they
    # belong to the user-global store only.
    workspace_metacrucible = workspace / ".metacrucible"
    workspace_bundles = (
        list(workspace_metacrucible.glob("**/*"))
        if workspace_metacrucible.is_dir()
        else []
    )
    assert workspace_bundles == [], (
        f"evaluate must NOT write evidence under the workspace; "
        f"got {workspace_bundles!r}"
    )
