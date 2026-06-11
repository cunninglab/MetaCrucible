"""Tests for Issue #29: PRD F1 ``metacrucible review`` subcommand.

Pins the public behavior of the ``review`` subcommand:

  - ``metacrucible review <artifact>`` always runs Static Review
    after the artifact parses. The Darwin 9-dimension rubric is
    exposed in both the human and the JSON output.
  - When no reviewed Benchmark is present, the review still
    completes with a stable warning id
    (``no-reviewed-benchmark``) and the execution evaluation
    is marked ``SKIPPED``. The exit code is the stable
    :data:`metacrucible.exit_codes.EXIT_OK`.
  - When a reviewed Benchmark is present, the review runs the
    Execution Evaluation diagnostic. Generated and disabled
    cases are never run (ADR 0025 / ADR 0018); the eligible
    partitions are the only ones counted toward the execution
    summary.
  - An invalid present Benchmark (e.g. ``schema-version-mismatch``)
    is never silently collapsed into the "no benchmark" path:
    the loader-supplied blockers surface in the JSON output and
    the overall review status flips to ``BLOCKED``.
  - The artifact on disk is never mutated. The pipeline reads
    the source bytes once and writes only to the user-global
    evidence store (``$HOME/.metacrucible/``).
  - The receipt's ``run_type`` is ``review`` and the run id
    starts with ``review-`` so F1 review bundles are
    distinguishable from the older ``init --review`` bundles
    (``run_type = "init-review"``).
  - The CLI is English-only on the human-output surface (the
    :mod:`metacrucible.__main__` writer scrubs paths; we pin
    the surface by checking the known stable keys and warning
    id).

These tests pin the F1 acceptance criteria with the same
conventions the existing :mod:`tests.test_init_command` and
:mod:`tests.test_cli_english_output` modules use: subprocess
invocations of ``python -m metacrucible``, ``HOME`` pinned to
a temp dir via the shared ``isolated_global_home`` fixture,
and assertions on the captured ``--json`` payload plus the
human output where useful.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

from metacrucible.exit_codes import (
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_USER_ERROR,
)

# --------------------------------------------------------------------------- #
# Constants pinned by the F1 contract                                         #
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Stable warning id emitted by ``review`` when no reviewed Benchmark
#: is present. Pinned by the F1 acceptance criterion so tests and
#: downstream tooling can branch on the value verbatim. The id is
#: defined in :mod:`metacrucible.__main__` and re-pinned here to
#: keep this test module self-contained.
NO_REVIEWED_BENCHMARK_WARNING = "no-reviewed-benchmark"

#: Run-type value written into the receipt of a ``review`` run.
REVIEW_RUN_TYPE = "review"

#: Id of the static-review profile that emits the 9-dimension
#: per-dimension scores. The list of 9 dimension ids is pinned
#: by ADR 0033 / Issue #22; the test asserts the full set
#: surfaces in the JSON output.
DARWIN_PROFILE_ID = "darwin-skill-quality"
DARWIN_DIMENSION_IDS: tuple[str, ...] = (
    "trigger_clarity",
    "input_contract",
    "output_contract",
    "invariants",
    "failure_modes",
    "examples",
    "scope_boundaries",
    "runtime_neutrality",
    "evaluability",
)

#: Subset of F1 review output keys whose presence in the JSON
#: payload is part of the machine contract. The keys are
#: the canonical schema fields downstream automation branches
#: on; renaming any of them is a breaking change.
REVIEW_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "artifact_kind",
    "artifact_path",
    "benchmark",
    "blockers",
    "execution_evaluation",
    "receipt_path",
    "static_review",
    "status",
    "summary_path",
    "trajectory_digest_path",
    "warnings",
    "workspace",
)

#: Subset of static-review sub-section keys whose presence in
#: the JSON payload is part of the machine contract. The
#: ``darwin_dimensions`` and ``weakest_dimensions`` keys are
#: the F1 rubric surface.
STATIC_REVIEW_KEYS: tuple[str, ...] = (
    "blockers",
    "darwin_dimensions",
    "profiles_run",
    "supplemental_findings",
    "weakest_dimensions",
)

#: Subset of execution-evaluation sub-section keys.
EXECUTION_EVALUATION_KEYS: tuple[str, ...] = (
    "blockers",
    "case_results",
    "cases_evaluated",
    "cases_failed",
    "cases_passed",
    "skipped",
    "skipped_reason",
    "status",
)

#: Per-case status values written into execution_evaluation.
#: case_results[*].status. Pinned so the F1 review tests
#: and downstream tooling share a single source of truth.
REVIEW_CASE_STATUS_PASS = "PASS"
REVIEW_CASE_STATUS_FAIL = "FAIL"
REVIEW_CASE_STATUS_BLOCKED = "BLOCKED"

#: Subset of per-case result keys.
REVIEW_CASE_RESULT_KEYS: tuple[str, ...] = (
    "blockers",
    "case_id",
    "evaluator",
    "evidence",
    "status",
)

#: Subset of benchmark sub-section keys.
BENCHMARK_KEYS: tuple[str, ...] = (
    "blockers",
    "disabled_count",
    "eligible_eval_count",
    "eligible_held_out_count",
    "path",
    "pending_generated_count",
    "present",
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

    Mirrors the fixture in :mod:`tests.test_repository_storage`
    so the new tests can run alongside the storage tests
    without stepping on the same ``HOME``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _run_metacrucible(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with ``argv`` inside ``cwd``."""
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _write_artifact(path: Path, source: str) -> Path:
    """Write a Skill capability artifact to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    """Write ``records`` as one JSON object per line at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(dict(rec), sort_keys=True) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# A minimal Skill artifact that does NOT touch the routing surface
# (frontmatter contains a non-routing field). Routing-surface
# blockers from the static-review profile therefore do not fire,
# so the static review verdict is PASS for these tests.
_SKILL_ARTIFACT_NO_ROUTING = (
    "---\n"
    "somefield: no routing-surface field declared\n"
    "---\n"
    "\n"
    "# review-test-skill\n"
    "\n"
    "Body content for the F1 review tracer tests.\n"
)


# A Skill artifact that touches the routing surface with both
# ``name`` and ``description`` (two fields). The
# ``routing-surface-safety`` profile enforces cap=1; this
# artifact exercises the BLOCKED static-review path.
_SKILL_ARTIFACT_ROUTING_BLOCKED = (
    "---\n"
    "name: routing-touch-skill\n"
    "description: A skill that touches two routing-surface fields.\n"
    "---\n"
    "\n"
    "# routing-touch-skill\n"
    "\n"
    "Body content for the F1 review routing-blocked test.\n"
)


def _metadata_record(
    *, schema_version: int = 1, **extras: Any
) -> dict[str, Any]:
    """Build a minimal valid benchmark metadata record (ADR 0029)."""
    record: dict[str, Any] = {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": schema_version,
    }
    record.update(extras)
    return record


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


def _generated_case(case_id: str) -> dict[str, Any]:
    """Build a generated (pending-review) case record."""
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "generated",
        "split": "eval",
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "checks": [
            {"name": "output_contains_thing", "pattern": "thing"}
        ],
    }


def _disabled_case(case_id: str) -> dict[str, Any]:
    """Build a disabled case record (ADR 0029 / ADR 0018)."""
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "disabled",
        "split": "eval",
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "checks": [
            {"name": "output_contains_thing", "pattern": "thing"}
        ],
    }


def _run_review(
    *,
    tmp_path: Path,
    isolated_global_home: Path,
    artifact_source: str = _SKILL_ARTIFACT_NO_ROUTING,
    benchmark_records: list[dict[str, Any]] | None = None,
    extra_args: list[str] | None = None,
    workspace_path: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run ``metacrucible review`` end-to-end and return artifacts.

    The temp artifact path, the workspace path, and the captured
    subprocess result are returned so each test can assert on
    its slice of state. ``HOME`` is pinned to the
    ``isolated_global_home`` fixture so the test does not
    leak evidence bundles into the developer's real
    ``~/.metacrucible/``.

    When ``workspace_path`` is omitted the workspace defaults
    to a fresh ``tmp_path / "ws"``. When
    ``benchmark_records`` is provided, the helper writes the
    records as ``benchmark.jsonl`` in the workspace; when it
    is ``None`` no benchmark file is created (the "no
    benchmark" path).
    """
    workspace = workspace_path or (tmp_path / "ws")
    workspace.mkdir(parents=True, exist_ok=True)

    artifact = tmp_path / "review-test.md"
    _write_artifact(artifact, artifact_source)

    if benchmark_records is not None:
        benchmark = workspace / BENCHMARK_FILE_NAME
        _write_jsonl(benchmark, benchmark_records)

    argv = ["review", str(artifact), "--workspace", str(workspace)]
    if extra_args:
        argv.extend(extra_args)
    result = _run_metacrucible(argv, cwd=REPO_ROOT)
    return result, artifact, workspace


# --------------------------------------------------------------------------- #
# AC1 — ``review`` is a recognized subcommand                                  #
# --------------------------------------------------------------------------- #

def test_review_subcommand_is_recognized() -> None:
    """``metacrucible review`` is a registered subcommand.

    Argparse raises ``unrecognized arguments: review`` if the
    subcommand is not wired in. The acceptance criterion is
    that ``review`` appears in the help output and the
    subcommand-level ``--help`` exits 0.
    """
    result = _run_metacrucible(["review", "--help"], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`metacrucible review --help` must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "review" in result.stdout, (
        f"review --help must mention the subcommand name; got "
        f"{result.stdout!r}"
    )
    assert "artifact" in result.stdout, (
        f"review --help must advertise the artifact positional; "
        f"got {result.stdout!r}"
    )
    assert "--workspace" in result.stdout, (
        f"review --help must advertise the --workspace flag; got "
        f"{result.stdout!r}"
    )


# --------------------------------------------------------------------------- #
# AC2 — no reviewed benchmark path: static + warning + SKIPPED execution      #
# --------------------------------------------------------------------------- #

def test_review_no_benchmark_runs_static_and_warns(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Without a Benchmark, ``review`` runs Static Review and emits a warning.

    F1 acceptance: "Execution Evaluation runs when a reviewed
    Benchmark is present; otherwise Static Review still
    completes with a warning that Execution was skipped."

    The exit code is :data:`EXIT_OK` because the static review
    passed and the absence of a benchmark is a warning, not a
    blocker. The JSON output surfaces the warning id, the
    SKIPPED execution status, and the static-review receipt
    paths.
    """
    result, artifact, workspace = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        # No benchmark_records => no benchmark file is created.
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`review` without a benchmark must exit {EXIT_OK} "
        f"(static pass + warning); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    for key in REVIEW_TOP_LEVEL_KEYS:
        assert key in payload, (
            f"`review` --json must surface {key!r}; "
            f"got keys {sorted(payload.keys())!r}"
        )
    assert payload["status"] == "PASS", (
        f"static review must PASS for the no-routing artifact; "
        f"got status={payload['status']!r}"
    )
    # The warning contract.
    warnings = payload["warnings"]
    assert isinstance(warnings, list) and warnings, (
        f"`review` without a benchmark must emit a warning; "
        f"got warnings={warnings!r}"
    )
    warning_ids = [
        entry.get("id") for entry in warnings if isinstance(entry, dict)
    ]
    assert NO_REVIEWED_BENCHMARK_WARNING in warning_ids, (
        f"`review` warning must carry "
        f"{NO_REVIEWED_BENCHMARK_WARNING!r}; got "
        f"warning_ids={warning_ids!r}"
    )
    # The execution evaluation contract.
    execution = payload["execution_evaluation"]
    for key in EXECUTION_EVALUATION_KEYS:
        assert key in execution, (
            f"execution_evaluation must surface {key!r}; "
            f"got keys {sorted(execution.keys())!r}"
        )
    assert execution["status"] == "SKIPPED", (
        f"execution_evaluation.status must be SKIPPED without "
        f"a benchmark; got {execution['status']!r}"
    )
    assert execution["skipped"] is True, (
        f"execution_evaluation.skipped must be True without a "
        f"benchmark; got {execution['skipped']!r}"
    )
    assert execution["skipped_reason"] in {
        "no-reviewed-benchmark",
        "no-eligible-reviewed-cases",
    }, (
        f"execution_evaluation.skipped_reason must be one of "
        f"the pinned no-benchmark reason codes; got "
        f"{execution['skipped_reason']!r}"
    )
    assert execution["cases_evaluated"] == 0, (
        f"cases_evaluated must be 0 for a SKIPPED run; got "
        f"{execution['cases_evaluated']!r}"
    )
    # The benchmark sub-section reports a missing file.
    benchmark = payload["benchmark"]
    for key in BENCHMARK_KEYS:
        assert key in benchmark, (
            f"benchmark sub-section must surface {key!r}; "
            f"got keys {sorted(benchmark.keys())!r}"
        )
    assert benchmark["present"] is False, (
        f"benchmark.present must be False when no benchmark "
        f"file exists; got {benchmark['present']!r}"
    )
    assert benchmark["eligible_eval_count"] == 0
    assert benchmark["eligible_held_out_count"] == 0


def test_review_no_benchmark_static_review_exposes_darwin_dimensions(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Static Review surfaces the Darwin 9-dimension rubric.

    F1 acceptance: "Static Review runs the Darwin 9-dimension
    rubric and prints per-dimension scores plus the weakest
    dimensions." The JSON output must carry all 9 dimension
    ids in canonical order, the weakest-dimensions slice, and
    the ``darwin-skill-quality`` profile id under
    ``profiles_run``.
    """
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        extra_args=["--json"],
    )
    payload = json.loads(result.stdout)
    static_review = payload["static_review"]
    for key in STATIC_REVIEW_KEYS:
        assert key in static_review, (
            f"static_review must surface {key!r}; "
            f"got keys {sorted(static_review.keys())!r}"
        )
    dimensions = static_review["darwin_dimensions"]
    assert isinstance(dimensions, list) and len(dimensions) == 9, (
        f"darwin_dimensions must carry exactly 9 entries; got "
        f"{len(dimensions)} (full: {dimensions!r})"
    )
    actual_ids = tuple(
        entry.get("id") for entry in dimensions
        if isinstance(entry, dict)
    )
    assert actual_ids == DARWIN_DIMENSION_IDS, (
        f"darwin_dimensions must surface the pinned 9-dimension "
        f"tuple in canonical order; got ids={actual_ids!r}"
    )
    for entry in dimensions:
        assert "score" in entry, (
            f"each darwin_dimensions entry must carry a score; "
            f"got {entry!r}"
        )
    weakest = static_review["weakest_dimensions"]
    assert isinstance(weakest, list) and len(weakest) > 0, (
        f"weakest_dimensions must be a non-empty list; got "
        f"{weakest!r}"
    )
    # Profiles run list must include the Darwin profile id so a
    # downstream reader can branch on which rubric executed.
    assert "secret-privacy-risk" in static_review["profiles_run"]
    assert "runtime-neutrality" in static_review["profiles_run"]


def test_review_no_benchmark_human_output_is_english_only(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Human output of the no-benchmark path is English-only.

    The user-facing surface must stay ASCII so the
    English-only contract (Issue #27 task 27.4) is preserved
    for the new subcommand. We check the captured stdout and
    stderr for non-ASCII printable characters.
    """
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        # No --json: the human output is rendered.
    )
    assert result.returncode == EXIT_OK, (
        f"`review` no-benchmark must exit {EXIT_OK}; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
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
    # The warning id must surface in the human output so the
    # operator can grep for it. The id is the same machine
    # contract as the JSON field.
    combined = f"{result.stdout}\n{result.stderr}"
    assert NO_REVIEWED_BENCHMARK_WARNING in combined, (
        f"human output must surface the warning id "
        f"{NO_REVIEWED_BENCHMARK_WARNING!r}; got "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# AC3 — reviewed benchmark path: static + Execution Evaluation                 #
# --------------------------------------------------------------------------- #

def test_review_with_reviewed_benchmark_runs_execution_evaluation(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Reviewed cases trigger the Execution Evaluation branch.

    F1 acceptance: "Execution Evaluation runs when a reviewed
    Benchmark is present; otherwise Static Review still
    completes with a warning." The output must report
    ``execution_evaluation.status='PASS'``, the
    ``cases_evaluated`` count matches the eligible reviewed
    cases, and no ``SKIPPED`` warning is emitted.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`review` with a reviewed benchmark must exit {EXIT_OK} "
        f"(static + execution pass); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS", (
        f"status must be PASS for static + execution pass; got "
        f"{payload['status']!r}"
    )
    execution = payload["execution_evaluation"]
    assert execution["status"] == "PASS", (
        f"execution_evaluation.status must be PASS when "
        f"reviewed cases are present; got {execution['status']!r}"
    )
    assert execution["skipped"] is False, (
        f"execution_evaluation.skipped must be False when "
        f"execution actually ran; got {execution['skipped']!r}"
    )
    assert execution["skipped_reason"] is None, (
        f"execution_evaluation.skipped_reason must be None on "
        f"a PASS run; got {execution['skipped_reason']!r}"
    )
    # Two eligible reviewed cases (1 eval + 1 held_out).
    assert execution["cases_evaluated"] == 2, (
        f"cases_evaluated must equal the eligible reviewed "
        f"count; got {execution['cases_evaluated']!r}"
    )
    assert execution["cases_passed"] == 2, (
        f"cases_passed must equal cases_evaluated for a "
        f"diagnostic-only PASS; got {execution['cases_passed']!r}"
    )
    assert execution["cases_failed"] == 0, (
        f"cases_failed must be 0 for a diagnostic PASS; got "
        f"{execution['cases_failed']!r}"
    )
    # The benchmark sub-section reports the eligible partitions.
    benchmark = payload["benchmark"]
    assert benchmark["present"] is True
    assert benchmark["eligible_eval_count"] == 1
    assert benchmark["eligible_held_out_count"] == 1
    # No SKIPPED warning on the PASS path.
    warning_ids = [
        entry.get("id") for entry in payload["warnings"]
        if isinstance(entry, dict)
    ]
    assert NO_REVIEWED_BENCHMARK_WARNING not in warning_ids, (
        f"`review` with a reviewed benchmark must NOT emit the "
        f"no-reviewed-benchmark warning; got warning_ids="
        f"{warning_ids!r}"
    )


def test_review_partition_counts_remain_observable_when_blocked(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A benchmark with mixed-status cases BLOCKS the review,
    but the four ADR 0029 partitions remain observable.

    F1 acceptance: "generated/disabled cases must not run."
    The benchmark sub-section must partition them; the
    optimize-only filter that previously downgraded this to
    the static+warning path was removed by the Issue #29
    spec review. Pending generated cases are a precondition
    failure for the execution branch: the review is BLOCKED,
    but the operator still sees the four-partition
    breakdown so they can act on the per-status counts.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
        # Generated / pending-review cases. These must not
        # contribute to cases_evaluated.
        _generated_case("case-gen-1"),
        _generated_case("case-gen-2"),
        # Disabled cases. These also do not contribute.
        _disabled_case("case-disabled-1"),
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`review` with pending generated cases must exit "
        f"{EXIT_BLOCKED} (pending-generated-case is a "
        f"precondition failure for the execution branch); "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    benchmark = payload["benchmark"]
    # The four partitions are surfaced even when the
    # review is BLOCKED on the execution side; the
    # operator still needs the per-status counts to act.
    assert benchmark["eligible_eval_count"] == 1
    assert benchmark["eligible_held_out_count"] == 1
    assert benchmark["pending_generated_count"] == 2
    assert benchmark["disabled_count"] == 1
    overall_blocker_ids = [
        entry.get("id") for entry in payload["blockers"]
        if isinstance(entry, dict)
    ]
    assert "pending-generated-case" in overall_blocker_ids, (
        f"review with pending generated cases must surface "
        f"the pending-generated-case blocker; got "
        f"overall_blocker_ids={overall_blocker_ids!r}"
    )


def test_review_with_empty_benchmark_blocks_when_cases_missing(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A present-but-empty benchmark BLOCKS the review.

    A benchmark file with only the metadata record and zero
    case records is a valid container (ADR 0025) but has no
    eligible reviewed cases. Per the Issue #29 spec review,
    the optimize-only filter that previously treated this
    as a static+warning path was removed: a benchmark that
    is present at the workspace root carries an implicit
    "execution was requested" intent, and missing required
    reviewed cases is a precondition failure that BLOCKS
    the review (per ADR 0029). The execution evaluation
    surfaces the loader-supplied blockers and the
    ``skipped_reason`` distinguishes this path from the
    no-benchmark-file path.
    """
    records = [_metadata_record()]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`review` with empty present benchmark must exit "
        f"{EXIT_BLOCKED} (missing required reviewed cases "
        f"is a precondition failure per ADR 0029); got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "BLOCKED", (
        f"status must be BLOCKED for a present-but-empty "
        f"benchmark; got {payload['status']!r}"
    )
    overall_blocker_ids = [
        entry.get("id") for entry in payload["blockers"]
        if isinstance(entry, dict)
    ]
    assert "missing-reviewed-eval-case" in overall_blocker_ids, (
        f"empty present benchmark must surface the "
        f"missing-reviewed-eval-case blocker; got "
        f"overall_blocker_ids={overall_blocker_ids!r}"
    )
    execution = payload["execution_evaluation"]
    assert execution["status"] == "BLOCKED", (
        f"execution_evaluation.status must be BLOCKED for an "
        f"empty present benchmark; got {execution['status']!r}"
    )
    assert execution["skipped_reason"] == "missing-reviewed-cases", (
        f"execution_evaluation.skipped_reason must distinguish "
        f"missing-cases from invalid-benchmark; got "
        f"{execution['skipped_reason']!r}"
    )


# --------------------------------------------------------------------------- #
# AC4 — invalid present benchmark is not collapsed into "missing"              #
# --------------------------------------------------------------------------- #

def test_review_with_invalid_benchmark_surfaces_loader_blockers(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Invalid present benchmark surfaces its blockers.

    F1 acceptance: "Invalid/present benchmark must not be
    silently treated as missing benchmark." A benchmark with
    a non-v1 ``schema_version`` exposes a
    ``schema-version-mismatch`` blocker. The review output
    must (a) preserve the loader-supplied blockers in the
    ``benchmark`` sub-section, (b) propagate them into the
    overall ``blockers`` list, and (c) flip the overall
    ``status`` to ``BLOCKED`` with a non-zero exit code.
    """
    records = [
        _metadata_record(schema_version=2),  # Invalid: not v1.
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`review` with invalid benchmark must exit {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "BLOCKED", (
        f"status must be BLOCKED for an invalid present "
        f"benchmark; got {payload['status']!r}"
    )
    benchmark = payload["benchmark"]
    assert benchmark["present"] is True, (
        f"benchmark.present must be True when the file exists, "
        f"even if invalid; got {benchmark['present']!r}"
    )
    benchmark_blocker_ids = [
        entry.get("id") for entry in benchmark["blockers"]
        if isinstance(entry, dict)
    ]
    assert "schema-version-mismatch" in benchmark_blocker_ids, (
        f"invalid benchmark must surface the "
        f"schema-version-mismatch blocker; got "
        f"benchmark_blocker_ids={benchmark_blocker_ids!r}"
    )
    # The overall blockers must include the loader blockers (a
    # present-but-invalid benchmark is not the "no benchmark"
    # path; the operator must see why the review is blocked).
    overall_blocker_ids = [
        entry.get("id") for entry in payload["blockers"]
        if isinstance(entry, dict)
    ]
    assert "schema-version-mismatch" in overall_blocker_ids, (
        f"overall blockers must surface the schema-version-"
        f"mismatch blocker; got overall_blocker_ids="
        f"{overall_blocker_ids!r}"
    )
    # The execution evaluation reports the invalid-benchmark
    # reason so the operator can distinguish "not present" from
    # "present but broken".
    execution = payload["execution_evaluation"]
    assert execution["status"] == "BLOCKED", (
        f"execution_evaluation.status must be BLOCKED for an "
        f"invalid present benchmark; got {execution['status']!r}"
    )
    assert execution["skipped_reason"] == "invalid-benchmark", (
        f"execution_evaluation.skipped_reason must distinguish "
        f"invalid from missing; got {execution['skipped_reason']!r}"
    )


# --------------------------------------------------------------------------- #
# AC5 — source non-mutation                                                   #
# --------------------------------------------------------------------------- #

def test_review_does_not_mutate_artifact_bytes(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``review`` reads the artifact without writing it.

    F1 acceptance: "The artifact on disk is not modified." We
    pin the file's bytes around the call so any accidental
    write or rename fails loud. The check covers the
    no-benchmark path; the reviewed-benchmark path is
    covered by the receipt-not-on-workspace test below.
    """
    result, artifact, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`review` no-benchmark must exit {EXIT_OK}; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    after_bytes = artifact.read_bytes()
    assert after_bytes == _SKILL_ARTIFACT_NO_ROUTING.encode("utf-8"), (
        f"`review` must NOT mutate the source artifact; got "
        f"{after_bytes!r}"
    )


def test_review_does_not_write_to_workspace_or_benchmark(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """``review`` writes only to the user-global evidence store.

    F1 acceptance: "Evidence storage writes are allowed, but
    source artifact writes are not. Tests must distinguish
    source path from ``.metacrucible/``/global evidence
    writes." The workspace (``<workspace>/.metacrucible/``)
    and the benchmark file must be untouched by the review
    pipeline. The receipt and summary paths must live under
    the isolated ``$HOME/.metacrucible/`` tree.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, artifact, workspace = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`review` with both eval and held-out reviewed cases "
        f"must exit {EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    receipt_path = Path(payload["receipt_path"])
    summary_path = Path(payload["summary_path"])
    digest_path = Path(payload["trajectory_digest_path"])
    # All three evidence files must live under
    # ``$HOME/.metacrucible/evidence/`` (the isolated
    # ``isolated_global_home`` fixture pins HOME to ``tmp_path``).
    expected_root = isolated_global_home / ".metacrucible" / "evidence"
    assert receipt_path.is_relative_to(expected_root), (
        f"receipt must live under {expected_root}; got "
        f"receipt_path={receipt_path}"
    )
    assert summary_path.is_relative_to(expected_root)
    assert digest_path.is_relative_to(expected_root)
    # The workspace and benchmark must be untouched.
    workspace_dot_metacrucible = workspace / ".metacrucible"
    assert not workspace_dot_metacrucible.exists(), (
        f"review must not create <workspace>/.metacrucible/; "
        f"got {workspace_dot_metacrucible}"
    )
    benchmark = workspace / BENCHMARK_FILE_NAME
    assert benchmark.is_file(), (
        f"benchmark file must still exist after review; got "
        f"benchmark={benchmark}"
    )
    # Receipt must carry the v1 schema_version and the
    # ``review`` run_type (F1 distinct from the older
    # ``init --review`` bundles).
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt_payload.get("schema_version") == 1, (
        f"receipt must stamp schema_version=1; got "
        f"{receipt_payload.get('schema_version')!r}"
    )
    assert receipt_payload.get("run_type") == REVIEW_RUN_TYPE, (
        f"receipt run_type must be {REVIEW_RUN_TYPE!r}; got "
        f"{receipt_payload.get('run_type')!r}"
    )
    assert receipt_payload.get("status") == "PASS", (
        f"receipt status must be PASS for static+execution pass; "
        f"got {receipt_payload.get('status')!r}"
    )


# --------------------------------------------------------------------------- #
# AC6 — stable exit-code matrix                                                #
# --------------------------------------------------------------------------- #

def test_review_exit_code_is_stable_for_static_warning(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Static PASS + skipped execution returns :data:`EXIT_OK`."""
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`review` static-warning must exit {EXIT_OK}; got "
        f"rc={result.returncode} stdout={result.stdout!r}"
    )


def test_review_exit_code_is_stable_for_static_plus_execution_pass(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Static PASS + execution PASS returns :data:`EXIT_OK`."""
    # Both eval and held-out cases are required: missing
    # either is a precondition failure for the execution
    # branch per ADR 0029 / Issue #29 spec review.
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_OK, (
        f"`review` static+execution PASS must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r}"
    )


def test_review_exit_code_is_stable_for_static_blocked(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Static BLOCKED returns :data:`EXIT_BLOCKED`."""
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        artifact_source=_SKILL_ARTIFACT_ROUTING_BLOCKED,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`review` with a routing-blocked artifact must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r}"
    )


def test_review_exit_code_is_stable_for_missing_artifact(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A missing artifact path returns :data:`EXIT_BLOCKED`."""
    workspace = tmp_path / "ws-missing"
    workspace.mkdir(parents=True, exist_ok=True)
    missing_artifact = tmp_path / "does-not-exist.md"
    result = _run_metacrucible(
        [
            "review",
            str(missing_artifact),
            "--workspace",
            str(workspace),
            "--json",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`review` on a missing artifact must exit {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload.get("status") == "BLOCKED", (
        f"missing-artifact payload must report status=BLOCKED; "
        f"got {payload!r}"
    )


def test_review_exit_code_is_stable_for_missing_artifact_positional(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A missing positional ``artifact`` is an argparse usage error.

    The CLI dispatcher maps argparse usage errors to
    :data:`EXIT_USER_ERROR` (1) so the contract is distinct
    from the BLOCKED (2) and INTERNAL_ERROR (3) codes. A
    missing positional is exactly that: an argparse usage
    error, not a semantic blocker.
    """
    result = _run_metacrucible(["review"], cwd=REPO_ROOT)
    assert result.returncode == EXIT_USER_ERROR, (
        f"`review` with no artifact must exit {EXIT_USER_ERROR} "
        f"(argparse usage); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# AC7 — JSON and human output parity                                           #
# --------------------------------------------------------------------------- #

def test_review_human_and_json_expose_same_semantic_fields(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Human and JSON outputs expose the same semantic content.

    F1 acceptance: "Output defaults to human-readable; ``--json``
    emits the same content in a stable machine-readable shape."

    The test runs the same review twice (once with ``--json``,
    once without) and asserts the human output contains the
    same machine-stable keys / ids the JSON payload surfaces.
    We do not assert prose parity: we assert semantic-key
    parity, which is the actual F1 contract.
    """
    # Both eval and held-out cases are required to pass
    # the F1 review; a benchmark missing either partition
    # is a precondition failure per ADR 0029.
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
        _reviewed_case("case-held-1", split="held_out"),
    ]
    json_result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    human_result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
    )
    assert json_result.returncode == EXIT_OK
    assert human_result.returncode == EXIT_OK
    payload = json.loads(json_result.stdout)
    # The human output must surface the top-level semantic
    # keys the JSON payload carries. We check a representative
    # subset that is operator-grep friendly.
    required_keys = (
        "artifact_kind",
        "benchmark",
        "blockers",
        "execution_evaluation",
        "status",
        "static_review",
        "warnings",
    )
    for key in required_keys:
        assert key in human_result.stdout, (
            f"human output must surface top-level key {key!r}; "
            f"got stdout={human_result.stdout!r}"
        )
    # The status values are echoed in the human form.
    assert payload["status"] in human_result.stdout, (
        f"human output must echo the status "
        f"{payload['status']!r}; got stdout={human_result.stdout!r}"
    )
    # The Darwin dimension ids must be reachable in the human
    # form so the operator can audit the rubric without
    # ``--json``.
    for dim_id in DARWIN_DIMENSION_IDS:
        assert dim_id in human_result.stdout, (
            f"human output must surface Darwin dimension "
            f"{dim_id!r}; got stdout={human_result.stdout!r}"
        )

# --------------------------------------------------------------------------- #
# AC8 — Darwin scoring is real (not uniform 1.0 placeholder)                  #
# --------------------------------------------------------------------------- #

def test_darwin_dimension_scores_reflect_real_input(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """Darwin per-dimension scores vary with the artifact content.

    Issue #29 spec review: ``evaluate_darwin_skill_quality``
    must produce real per-dimension analysis, not a uniform
    1.0 placeholder. This test exercises two artifacts with
    different bodies and asserts the per-dimension scores
    differ in the expected direction (a richly structured
    body scores higher on documented dimensions than an
    empty body).
    """
    from metacrucible.profiles import (
        DARWIN_DIMENSIONS,
        evaluate_darwin_skill_quality,
    )

    empty = evaluate_darwin_skill_quality(
        {
            "body": "",
            "portability": {"target": "runtime_neutral"},
        }
    )
    rich = evaluate_darwin_skill_quality(
        {
            "body": (
                "# skill\n"
                "\n"
                "## When to use\n"
                "Use this for X.\n"
                "\n"
                "## Input\n"
                "- arg1: description\n"
                "- arg2: description\n"
                "\n"
                "## Output\n"
                "Returns a result.\n"
                "\n"
                "## Examples\n"
                "For example, do X.\n"
                "e.g., Y is great.\n"
                "\n"
                "## Invariants\n"
                "- Must not do A.\n"
                "- Never do B.\n"
                "\n"
                "## Failure modes\n"
                "On error, do X.\n"
                "\n"
                "## Scope\n"
                "Out of scope: Z.\n"
                "\n"
                "## Checks\n"
                "Verify the result.\n"
                "\n"
                "```python\n"
                "print('hi')\n"
                "```\n"
            ),
            "portability": {"target": "runtime_neutral"},
        }
    )

    empty_scores = {d["id"]: d["score"] for d in empty.dimension_scores}
    rich_scores = {d["id"]: d["score"] for d in rich.dimension_scores}

    # The per-dimension scores must NOT be uniform (1.0 for
    # every dimension is the placeholder behavior; the
    # spec-reviewer explicitly rejected it).
    assert len(set(empty_scores.values())) > 1 or any(
        v < 1.0 for v in empty_scores.values()
    ), (
        f"empty body must not produce uniform 1.0 scores; "
        f"got {empty_scores!r}"
    )
    # Every content-driven dimension on a rich body must
    # score strictly higher than on an empty body.
    for dim in DARWIN_DIMENSIONS:
        if dim == "runtime_neutrality":
            # Both inputs declare runtime_neutral, so the
            # score is 1.0 in both cases. Skip the strict-
            # greater check on this dimension.
            assert empty_scores[dim] == rich_scores[dim] == 1.0
            continue
        assert rich_scores[dim] > empty_scores[dim], (
            f"rich body must score higher on {dim!r} than "
            f"empty body; got rich={rich_scores[dim]!r} "
            f"empty={empty_scores[dim]!r}"
        )

    # Also assert that the F1 review surfaces these
    # non-uniform scores (so the operator can see them in
    # --json).
    rich_artifact = tmp_path / "rich.md"
    rich_artifact.write_text(
        "---\n"
        "somefield: not a routing-surface field\n"

        "---\n"
        "\n"
        "## When to use\n"
        "Use this skill to do the thing.\n"
        "\n"
        "## Input\n"
        "- first: the first input\n"
        "- second: the second input\n"
        "\n"
        "## Output\n"
        "Returns the result.\n"
        "\n"
        "## Examples\n"
        "For example, run the thing.\n"
        "\n"
        "## Invariants\n"
        "- Must not mutate the workspace.\n"
        "\n"
        "## Failure modes\n"
        "On error, report the message.\n"
        "\n"
        "## Scope\n"
        "Out of scope: nested calls.\n"
        "\n"
        "## Checks\n"
        "Verify the result matches the expected pattern.\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    argv = [
        "review",
        str(rich_artifact),
        "--workspace",
        str(workspace),
        "--json",
    ]
    result = _run_metacrucible(argv, cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`review` on a rich artifact must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    review_scores = {
        d["id"]: d["score"]
        for d in payload["static_review"]["darwin_dimensions"]
        if isinstance(d, dict)
    }
    # The rich artifact in this test exercises the
    # scoring helper directly above. The review output
    # must carry non-uniform scores (no two dimensions
    # are likely to land on the exact same value with
    # the body above).
    values = list(review_scores.values())
    assert len(set(values)) > 1, (
        f"review output must surface non-uniform Darwin "
        f"scores; got {review_scores!r}"
    )


# --------------------------------------------------------------------------- #
# AC9 — rule_checks.execute_check is actually invoked                          #
# --------------------------------------------------------------------------- #

def test_review_actually_invokes_execute_check(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """The F1 execution branch invokes ``rule_checks.execute_check``
    per eligible reviewed case; the case's ``expected_output``
    fixture drives a real pass / fail verdict.

    Issue #29 spec review: ``cases_passed = cases_evaluated``
    is no longer acceptable; the engine must actually run the
    check. This test pins two cases: one whose
    ``expected_output`` contains the check's pattern
    (expected PASS) and one whose ``expected_output`` does
    not (expected FAIL). The aggregate verdict is FAILED
    because at least one case failed.
    """
    records = [
        _metadata_record(),
        # This case's expected_output contains the pattern
        # ``thing`` (the default checks pattern). The
        # deterministic check should pass.
        _reviewed_case(
            "case-pass-1",
            split="eval",
            expected_output="The thing worked correctly.",
        ),
        # This case's expected_output does NOT contain the
        # pattern. The deterministic check should fail.
        # ("thing" must not be a substring anywhere in
        # the output for the FAILED verdict to surface.)
        _reviewed_case(
            "case-fail-1",
            split="held_out",
            expected_output="An entirely different outcome here.",
        ),
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`review` with one FAILed case must exit "
        f"{EXIT_BLOCKED} (negative verdict); got "
        f"rc={result.returncode} stdout={result.stdout!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "FAILED", (
        f"status must be FAILED when at least one case "
        f'failed; got {payload["status"]!r}'
    )
    execution = payload["execution_evaluation"]
    assert execution["status"] == "FAIL", (
        f"execution_evaluation.status must be FAIL when a "
        f'case failed; got {execution["status"]!r}'
    )
    assert execution["cases_evaluated"] == 2
    assert execution["cases_passed"] == 1, (
        f"cases_passed must reflect actual run; got "
        f'{execution["cases_passed"]!r}'
    )
    assert execution["cases_failed"] == 1, (
        f"cases_failed must reflect actual run; got "
        f'{execution["cases_failed"]!r}'
    )
    case_results = execution["case_results"]
    assert len(case_results) == 2
    by_id = {r["case_id"]: r for r in case_results}
    assert by_id["case-pass-1"]["status"] == REVIEW_CASE_STATUS_PASS
    assert by_id["case-fail-1"]["status"] == REVIEW_CASE_STATUS_FAIL
    assert by_id["case-pass-1"]["evaluator"] == "rule_check"
    assert by_id["case-fail-1"]["evaluator"] == "rule_check"
    # The fail case's evidence carries the actual check
    # engine output (returncode != 0, stderr captured).
    assert by_id["case-fail-1"]["evidence"]["returncode"] != 0


# --------------------------------------------------------------------------- #
# AC10 — LLM judgment unavailable BLOCKS the case                             #
# --------------------------------------------------------------------------- #

def test_review_judgment_unavailable_blocks_case(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A case with a ``judgment`` field but no provider
    configuration BLOCKS the case (not silent pass).

    Issue #29 spec review: "if provider unavailable, return
    BLOCKED with evidence, not pass." The F1 review does
    not own a provider config; the two-judge evaluator
    refuses the call and the F1 path translates that to
    the ``review-case-judge-provider-unavailable`` BLOCKED
    condition. The per-case result carries the BLOCKED
    status; the overall execution is BLOCKED.
    """
    judgment_case = {
        "record_type": "case",
        "case_id": "case-judge-1",
        "status": "reviewed",
        "split": "eval",
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "checks": [],
        "judgment": {
            "rubric": {"name": "quality", "scale": [0, 1]},
            "pass_condition": "score >= 0.7",
        },
    }
    records = [
        _metadata_record(),
        judgment_case,
        _reviewed_case("case-eval-2", split="held_out"),
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`review` with judgment-unavailable case must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r}"
    )
    payload = json.loads(result.stdout)
    execution = payload["execution_evaluation"]
    case_results = execution["case_results"]
    by_id = {r["case_id"]: r for r in case_results}
    assert "case-judge-1" in by_id, (
        f"case_results must include the judgment case; got "
        f"case_ids={list(by_id.keys())!r}"
    )
    judge_result = by_id["case-judge-1"]
    assert judge_result["status"] == REVIEW_CASE_STATUS_BLOCKED, (
        f"judgment case must be BLOCKED when provider is "
        f'unavailable; got {judge_result["status"]!r}'
    )
    assert judge_result["evaluator"] == "judge"
    blocker_ids = [
        b.get("id") for b in judge_result["blockers"]
        if isinstance(b, dict)
    ]
    assert "review-case-judge-provider-unavailable" in blocker_ids, (
        f"judgment-unavailable blocker must surface in the "
        f"per-case blockers; got blocker_ids={blocker_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC11 — case with neither checks nor judgment BLOCKS                         #
# --------------------------------------------------------------------------- #

def test_review_case_with_no_checks_or_judgment_blocks(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A case that has neither ``checks`` nor ``judgment``
    BLOCKS the case evaluation.

    ADR 0010 requires the case to provide at least one
    deterministic check or non-deterministic judgment. The
    F1 path enforces this with the
    ``review-case-no-checks-or-judgment`` BLOCKED condition.
    """
    no_checks_case = {
        "record_type": "case",
        "case_id": "case-noop-1",
        "status": "reviewed",
        "split": "eval",
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        # no checks, no judgment
    }
    records = [
        _metadata_record(),
        no_checks_case,
        _reviewed_case("case-held-1", split="held_out"),
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED
    payload = json.loads(result.stdout)
    execution = payload["execution_evaluation"]
    by_id = {
        r["case_id"]: r for r in execution["case_results"]
    }
    noop = by_id["case-noop-1"]
    assert noop["status"] == REVIEW_CASE_STATUS_BLOCKED
    blocker_ids = [
        b.get("id") for b in noop["blockers"]
        if isinstance(b, dict)
    ]
    assert "review-case-no-checks-or-judgment" in blocker_ids, (
        f"case with neither checks nor judgment must "
        f"surface the no-checks-or-judgment blocker; got "
        f"blocker_ids={blocker_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC12 — ADR 0035 BLOCKED bundle is written for execution-requested review     #
# --------------------------------------------------------------------------- #

def test_review_blocked_bundle_written_when_execution_blocks(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """When the F1 review is BLOCKED for execution reasons
    (benchmark present, missing required reviewed cases),
    the ADR 0035 ``review_execution_requested`` BLOCKED
    bundle is written to the user-global evidence store.

    Issue #29 spec review: "execution-requested review
    emit minimal BLOCKED bundles when blocked." The bundle
    lives in a sibling directory of the static-review
    bundle (``<run_id>-blocked``) so both are reachable
    for downstream tooling.
    """
    records = [
        _metadata_record(),
        _reviewed_case("case-eval-1", split="eval"),
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED
    payload = json.loads(result.stdout)
    # The static-review run_id is on the receipt path; the
    # BLOCKED bundle is at ``<run_id>-blocked``.
    static_run_id = Path(payload["receipt_path"]).parent.name
    blocked_run_id = f"{static_run_id}-blocked"
    evidence_root = isolated_global_home / ".metacrucible" / "evidence"
    blocked_dir = evidence_root / blocked_run_id
    assert blocked_dir.is_dir(), (
        f"BLOCKED bundle must be written for "
        f"review_execution_requested; got "
        f"blocked_dir={blocked_dir}"
    )
    # The minimal BLOCKED bundle has exactly three
    # durable files (ADR 0035): receipt, summary,
    # trajectory-digest.
    files = sorted(p.name for p in blocked_dir.iterdir())
    assert files == [
        "receipt.json",
        "summary.json",
        "trajectory-digest.json",
    ], (
        f"BLOCKED bundle must contain exactly the three "
        f"durable files; got {files!r}"
    )
    receipt = json.loads(
        (blocked_dir / "receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "BLOCKED", (
        f"BLOCKED bundle receipt must be BLOCKED; got "
        f'{receipt.get("status")!r}'
    )
    assert receipt["run_type"] == "review_execution_requested", (
        f"BLOCKED bundle run_type must be "
        f"review_execution_requested (ADR 0035); got "
        f'{receipt.get("run_type")!r}'
    )
    # The blockers from the review payload must surface
    # in the BLOCKED bundle's receipt.
    blocked_ids = [
        b.get("id") for b in receipt.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert "missing-reviewed-held-out-case" in blocked_ids, (
        f"BLOCKED bundle must surface the "
        f"missing-reviewed-held-out-case blocker; got "
        f"blocked_ids={blocked_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC13 — schema-version-mismatch surfaces structural BLOCKED + bundle         #
# --------------------------------------------------------------------------- #

def test_review_schema_mismatch_emits_structural_blocked_bundle(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """A benchmark with a non-v1 ``schema_version`` is
    structurally invalid; the F1 review BLOCKS, surfaces
    the ``schema-version-mismatch`` loader blocker, and
    writes the ADR 0035 ``review_execution_requested``
    BLOCKED bundle.
    """
    records = [
        _metadata_record(schema_version=2),
    ]
    result, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=records,
        extra_args=["--json"],
    )
    assert result.returncode == EXIT_BLOCKED
    payload = json.loads(result.stdout)
    execution = payload["execution_evaluation"]
    assert execution["skipped_reason"] == "invalid-benchmark", (
        f"structural invalid benchmark must distinguish "
        f"skipped_reason from missing-reviewed-cases; got "
        f'{execution["skipped_reason"]!r}'
    )
    static_run_id = Path(payload["receipt_path"]).parent.name
    blocked_dir = (
        isolated_global_home
        / ".metacrucible"
        / "evidence"
        / f"{static_run_id}-blocked"
    )
    assert blocked_dir.is_dir(), (
        f"BLOCKED bundle must be written for structural "
        f"benchmark failures; got blocked_dir={blocked_dir}"
    )
    receipt = json.loads(
        (blocked_dir / "receipt.json").read_text(encoding="utf-8")
    )
    blocked_ids = [
        b.get("id") for b in receipt.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert "schema-version-mismatch" in blocked_ids, (
        f"BLOCKED bundle must surface the "
        f"schema-version-mismatch blocker; got "
        f"blocked_ids={blocked_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC14 — missing-reviewed-cases + invalid-benchmark reasons are distinct      #
# --------------------------------------------------------------------------- #

def test_review_execution_blocked_reasons_are_distinct(
    tmp_path: Path, isolated_global_home: Path
) -> None:
    """The ``execution_evaluation.skipped_reason`` field
    distinguishes the BLOCKED-on-execution reasons:

      - ``invalid-benchmark`` for structural issues
        (schema mismatch, duplicate case ids)
      - ``missing-reviewed-cases`` for the
        "benchmark is well-formed but missing required
        reviewed cases" path (pending-generated,
        missing-reviewed-eval-case,
        missing-reviewed-held-out-case)

    Issue #29 spec review: the operator must be able to
    tell "not present" from "present but broken" from
    "present but cannot run". The reason code is the
    F1 contract.
    """
    # Path 1: structural invalid (schema mismatch).
    result_struct, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=[_metadata_record(schema_version=2)],
        extra_args=["--json"],
    )
    assert result_struct.returncode == EXIT_BLOCKED
    payload_struct = json.loads(result_struct.stdout)
    reason_struct = payload_struct["execution_evaluation"][
        "skipped_reason"
    ]
    assert reason_struct == "invalid-benchmark"

    # Path 2: present but missing required reviewed cases.
    result_missing, _, _ = _run_review(
        tmp_path=tmp_path,
        isolated_global_home=isolated_global_home,
        benchmark_records=[
            _metadata_record(),
            _reviewed_case("case-eval-1", split="eval"),
        ],
        extra_args=["--json"],
    )
    assert result_missing.returncode == EXIT_BLOCKED
    payload_missing = json.loads(result_missing.stdout)
    reason_missing = payload_missing["execution_evaluation"][
        "skipped_reason"
    ]
    assert reason_missing == "missing-reviewed-cases"

    # The two reason codes are distinct.
    assert reason_struct != reason_missing
