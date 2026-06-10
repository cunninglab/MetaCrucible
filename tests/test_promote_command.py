"""CLI tests for generated-case promotion (Issue #8)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

from metacrucible.benchmark import PENDING_GENERATED_BLOCKER, load_benchmark
from metacrucible.exit_codes import EXIT_BLOCKED, EXIT_USER_ERROR

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_FILE_NAME = "benchmark.jsonl"


def _run_metacrucible(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with captured text output."""
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    lines = [json.dumps(dict(rec), sort_keys=True) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _metadata_record() -> dict[str, Any]:
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
    }


def _case_record(
    case_id: str,
    *,
    status: str = "generated",
    split: str = "eval",
    **extras: Any,
) -> dict[str, Any]:
    record = {
        "record_type": "case",
        "case_id": case_id,
        "status": status,
        "split": split,
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "checks": [{"name": "ok", "pattern": "ok"}],
    }
    record.update(extras)
    return record


def _generated_case(case_id: str, **extras: Any) -> dict[str, Any]:
    return _case_record(case_id, status="generated", **extras)


def test_promote_requires_case_id_split_and_reviewer(tmp_path: Path) -> None:
    """Argparse must reject promote calls that omit required review fields.

    Issue #27 task 27.1: argparse usage errors map to ``EXIT_USER_ERROR``
    (1) so they stay distinct from the semantic blocked (2) and
    internal (3) exit codes.
    """
    workspace = tmp_path / "ws-promote-required"
    workspace.mkdir()

    result = _run_metacrucible(["promote", str(workspace)], cwd=REPO_ROOT)

    assert result.returncode == EXIT_USER_ERROR
    assert "--case-id" in result.stderr
    assert "--split" in result.stderr
    assert "--reviewed-by" in result.stderr


def test_promote_subcommand_dry_run_is_recognized(tmp_path: Path) -> None:
    """``promote`` must plan a generated-case promotion without writing by default."""
    workspace = tmp_path / "ws-promote"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert "unrecognized arguments" not in result.stderr
    assert result.returncode == 0, (
        f"`metacrucible promote` dry-run must exit 0; got "
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["case_id"] == "gen-1"
    assert payload["dry_run"] is True
    assert payload["applied"] is False

    records = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines()]
    assert records[1]["status"] == "generated"


def test_promote_apply_records_reviewer_and_status(tmp_path: Path) -> None:
    """``--apply`` must promote the generated case and record reviewer provenance."""
    workspace = tmp_path / "ws-promote-apply"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "held_out",
            "--reviewed-by",
            "alice",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        f"`metacrucible promote --apply` must exit 0; got "
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["sentinel_cleared"] is True

    records = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines()]
    case = records[1]
    assert case["status"] == "reviewed"
    assert case["split"] == "held_out"
    assert case["reviewed"] is True
    assert case["reviewed_by"] == "alice"


def test_promote_apply_records_review_note(tmp_path: Path) -> None:
    """Promotion must record the human review note on the case."""
    workspace = tmp_path / "ws-promote-note"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--review-note",
            "Reviewed: 覆盖 held-out 风险\nOK",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        f"promotion with review_note must exit 0; got "
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    records = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines()]
    assert records[1]["review_note"] == "Reviewed: 覆盖 held-out 风险\nOK"


def test_promote_clears_pending_generated_blocker_for_case(tmp_path: Path) -> None:
    """A promoted case must stop contributing to the pending-generated blocker."""
    workspace = tmp_path / "ws-promote-blocker"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    before = load_benchmark(benchmark)
    before_blockers = [blocker["id"] for blocker in before.blockers]
    assert PENDING_GENERATED_BLOCKER in before_blockers

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        f"promotion must exit 0; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    after = load_benchmark(benchmark)
    after_blockers = [blocker["id"] for blocker in after.blockers]
    assert PENDING_GENERATED_BLOCKER not in after_blockers
    assert [case["case_id"] for case in after.pending_generated_cases] == []
    assert [case["case_id"] for case in after.eligible_eval_cases] == ["gen-1"]


def test_promote_apply_appends_history_record(tmp_path: Path) -> None:
    """Applied promotion must leave an append-only history audit row."""
    workspace = tmp_path / "ws-promote-history"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--review-note",
            "looks good",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        f"promotion must exit 0; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    history = workspace / ".metacrucible" / "history.jsonl"
    records = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "case_promoted"
    assert records[-1]["case_id"] == "gen-1"
    assert records[-1]["split"] == "eval"
    assert records[-1]["reviewed_by"] == "alice"
    assert records[-1]["review_note"] == "looks good"
    assert records[-1]["reviewed_at"]


@pytest.mark.parametrize("reviewed_by", ["", "   "])
def test_promote_blocks_empty_reviewer_identity(
    tmp_path: Path, reviewed_by: str
) -> None:
    """Promotion must require an explicit non-empty reviewer identity.

    Issue #27 task 27.1: the empty-reviewer precondition maps to the
    stable ``EXIT_BLOCKED`` code so callers can branch on it.
    """
    workspace = tmp_path / "ws-promote-empty-reviewer"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            reviewed_by,
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == EXIT_BLOCKED
    payload = json.loads(result.stdout)
    assert [blocker["id"] for blocker in payload["blockers"]] == [
        "promote-empty-reviewed-by"
    ]
    records = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines()]
    assert records[1]["status"] == "generated"


def test_promote_default_review_note_is_empty_string(tmp_path: Path) -> None:
    """Promotion must record an empty string when no review note is supplied."""
    workspace = tmp_path / "ws-promote-default-note"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        f"promotion without review_note must exit 0; got "
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    records = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines()]
    assert records[1]["review_note"] == ""

@pytest.mark.parametrize("status", ["reviewed", "disabled"])
def test_promote_blocks_cases_that_are_not_generated(
    tmp_path: Path, status: str
) -> None:
    """Only generated cases may be promoted; existing provenance must stay intact.

    Issue #27 task 27.1: the not-generated precondition maps to the
    stable ``EXIT_BLOCKED`` code.
    """
    workspace = tmp_path / "ws-promote-not-generated"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _case_record(
                "case-1",
                status=status,
                reviewed_by="bob",
                review_note="original",
            ),
        ],
    )

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "case-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--review-note",
            "overwrite attempt",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == EXIT_BLOCKED
    payload = json.loads(result.stdout)
    assert [blocker["id"] for blocker in payload["blockers"]] == [
        "promote-case-not-generated"
    ]
    records = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines()]
    assert records[1]["status"] == status
    assert records[1]["reviewed_by"] == "bob"
    assert records[1]["review_note"] == "original"

def test_promote_one_case_preserves_remaining_generated_blocker(tmp_path: Path) -> None:
    """Promoting one generated case must not hide other pending generated cases."""
    workspace = tmp_path / "ws-promote-one-of-two"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [_metadata_record(), _generated_case("gen-1"), _generated_case("gen-2")],
    )

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0
    loaded = load_benchmark(benchmark)
    blocker_ids = [blocker["id"] for blocker in loaded.blockers]
    assert PENDING_GENERATED_BLOCKER in blocker_ids
    assert [case["case_id"] for case in loaded.pending_generated_cases] == ["gen-2"]
    assert [case["case_id"] for case in loaded.eligible_eval_cases] == ["gen-1"]

def test_promote_preserves_jsonl_case_order_and_removes_literal_sentinel(
    tmp_path: Path,
) -> None:
    """Promotion must preserve authoring order and clear literal pending-review marker."""
    workspace = tmp_path / "ws-promote-order"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(
        benchmark,
        [
            _metadata_record(),
            _generated_case("gen-1", BOOTSTRAP_PENDING_REVIEW=True),
            _case_record("eval-1", status="reviewed", split="eval"),
            _case_record("held-1", status="reviewed", split="held_out"),
        ],
    )

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "held_out",
            "--reviewed-by",
            "alice",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0
    records = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines()]
    assert [record.get("case_id") for record in records[1:]] == [
        "gen-1",
        "eval-1",
        "held-1",
    ]
    assert "BOOTSTRAP_PENDING_REVIEW" not in records[1]
    assert not (workspace / f"{BENCHMARK_FILE_NAME}.tmp").exists()

def test_promote_blocks_unknown_case_id(tmp_path: Path) -> None:
    """Promotion must report a stable blocker when the target case is absent.

    Issue #27 task 27.1: the missing-case precondition maps to the
    stable ``EXIT_BLOCKED`` code.
    """
    workspace = tmp_path / "ws-promote-missing"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run_metacrucible(
        [
            "promote",
            str(workspace),
            "--case-id",
            "missing",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--apply",
            "--json",
        ],
        cwd=REPO_ROOT,
    )

    assert result.returncode == EXIT_BLOCKED
    payload = json.loads(result.stdout)
    assert [blocker["id"] for blocker in payload["blockers"]] == [
        "promote-case-not-found"
    ]
    records = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines()]
    assert [record.get("case_id") for record in records[1:]] == ["gen-1"]
