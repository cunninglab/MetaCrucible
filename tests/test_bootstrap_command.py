"""CLI tests for the ``bootstrap`` subcommand (Issue #30, PRD F2).

Pins the public behavior of ``metacrucible bootstrap <workspace>``:

  - The subcommand is recognized by argparse (no "unrecognized
    arguments" error from ``bootstrap --help``).
  - With a workspace that has an empty benchmark container,
    ``bootstrap`` appends ``--case-count`` draft case records
    to ``benchmark.jsonl`` (default 3). Each new record carries
    ``record_type="case"``, ``status="generated"``,
    ``split=None``, an empty ``checks`` list, a ``None``
    ``judgment``, and the ``BOOTSTRAP_PENDING_REVIEW=True``
    sentinel that the ``optimize`` gate keys off.
  - The atomic write does NOT leave a ``benchmark.jsonl.tmp``
    file behind on success (mirrors the contract
    :func:`metacrucible.promote._atomic_write_jsonl` pins for
    ``promote``).
  - The append is order-preserving: existing records come
    first, the new bootstrap records follow in the order they
    were generated.
  - A ``cases_bootstrapped`` event is appended to the
    workspace's ``.metacrucible/history.jsonl`` so the audit
    lineage carries the bootstrap provenance.
  - The ``--json`` flag emits a parseable JSON object with the
    workspace, benchmark, case count, and generated
    ``case_ids`` so a caller can branch on the machine-stable
    keys.
  - A workspace without a benchmark file is a precondition
    failure: ``bootstrap`` returns ``EXIT_BLOCKED`` with the
    ``bootstrap-missing-benchmark`` blocker so the operator
    is forced through ``init`` first.

These tests follow the subprocess invocation pattern from
:mod:`tests.test_promote_command` and
:mod:`tests.test_review_command`: ``python -m metacrucible`` is
invoked in a temp dir, both stdout and stderr are captured, and
the JSON payload is parsed for the machine-stable fields.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

from metacrucible.benchmark import STATUS_GENERATED, load_benchmark
from metacrucible.exit_codes import EXIT_BLOCKED, EXIT_OK, EXIT_USER_ERROR

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Stable blocker id emitted by ``bootstrap`` when the workspace
#: has no benchmark file yet. Pinned here as a single source of
#: truth for the bootstrap tests and re-imported by the
#: optimize tests so the machine contract is testable from
#: outside ``__main__``.
BOOTSTRAP_MISSING_BENCHMARK_BLOCKER = "bootstrap-missing-benchmark"

#: Stable blocker id emitted by ``bootstrap`` when ``--case-count``
#: is a non-positive integer. The test pins the id so a future
#: rename is a deliberate, single-site change.
BOOTSTRAP_INVALID_CASE_COUNT_BLOCKER = "bootstrap-invalid-case-count"

#: Literal case-level field that flags bootstrap-generated
#: cases as "pending human review". The string is the
#: machine-stable contract; ``promote`` removes the field and
#: ``optimize`` keys the sentinel check off of it.
BOOTSTRAP_PENDING_REVIEW_FIELD = "BOOTSTRAP_PENDING_REVIEW"

#: Default ``--case-count`` value. Pinned so the test does not
#: silently drift when the module constant changes.
BOOTSTRAP_DEFAULT_CASE_COUNT = 3


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _run_metacrucible(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with captured text output.

    Mirrors the helper in :mod:`tests.test_promote_command` so
    the bootstrap tests can use the same subprocess pattern the
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

    ``init`` creates the empty benchmark container that
    ``bootstrap`` then appends to. The fixture is reused
    across tests so each test starts from a known-good
    state with an empty benchmark file at the workspace
    root.
    """
    workspace = tmp_path / "ws-bootstrap"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`init` must exit 0 before bootstrap; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return workspace


def _metadata_record() -> dict[str, Any]:
    """Minimal benchmark metadata record (ADR 0029)."""
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
    }


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
# AC1 — ``bootstrap`` is a recognized subcommand                              #
# --------------------------------------------------------------------------- #

def test_bootstrap_subcommand_is_recognized() -> None:
    """``metacrucible bootstrap`` is a registered subcommand.

    Argparse raises ``unrecognized arguments: bootstrap`` if
    the subcommand is not wired in. The acceptance criterion
    is that ``bootstrap`` appears in the help output and the
    subcommand-level ``--help`` exits 0.
    """
    result = _run_metacrucible(["bootstrap", "--help"], cwd=REPO_ROOT)
    assert result.returncode == EXIT_OK, (
        f"`metacrucible bootstrap --help` must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "bootstrap" in result.stdout, (
        f"bootstrap --help must mention the subcommand name; got "
        f"{result.stdout!r}"
    )
    assert "workspace" in result.stdout, (
        f"bootstrap --help must advertise the workspace "
        f"positional; got {result.stdout!r}"
    )
    assert "--case-count" in result.stdout, (
        f"bootstrap --help must advertise the --case-count "
        f"flag; got {result.stdout!r}"
    )
    assert "--json" in result.stdout, (
        f"bootstrap --help must advertise the --json flag; "
        f"got {result.stdout!r}"
    )


# --------------------------------------------------------------------------- #
# AC2 — bootstrap writes draft cases with the correct shape                   #
# --------------------------------------------------------------------------- #

def test_bootstrap_generates_default_three_cases_with_generated_status(
    tmp_path: Path,
) -> None:
    """With a fresh ``init`` workspace, ``bootstrap`` writes
    the default ``--case-count`` (3) draft cases, each with
    ``status=generated`` and a unique ``case_id`` of the form
    ``bootstrap-<8-hex>``.

    Issue #30 AC1: "Generates candidate cases as ``generated``
    status." The default is the module constant
    :data:`BOOTSTRAP_DEFAULT_CASE_COUNT` (3). The test pins
    that exact value so the CLI contract is stable.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME

    result = _run_metacrucible(
        ["bootstrap", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`bootstrap` must exit 0 on a fresh workspace; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "unrecognized arguments" not in result.stderr, (
        f"bootstrap must be a recognized subcommand; got "
        f"stderr={result.stderr!r}"
    )

    records = [
        json.loads(line)
        for line in benchmark.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # The metadata record is preserved; three new case records
    # follow.
    assert records[0]["record_type"] == "metadata", (
        f"first record must be the metadata record; got "
        f"{records[0]!r}"
    )
    case_records = [r for r in records[1:]]
    assert len(case_records) == BOOTSTRAP_DEFAULT_CASE_COUNT, (
        f"bootstrap must write exactly the default case count "
        f"({BOOTSTRAP_DEFAULT_CASE_COUNT}) when --case-count "
        f"is omitted; got {len(case_records)} case records"
    )
    for case in case_records:
        assert case["record_type"] == "case", (
            f"bootstrap case must be record_type='case'; got "
            f"{case!r}"
        )
        assert case["status"] == STATUS_GENERATED, (
            f"bootstrap case must be status={STATUS_GENERATED!r}; "
            f"got status={case.get('status')!r}"
        )
        assert case["split"] is None, (
            f"bootstrap case must have split=None until promote; "
            f"got split={case.get('split')!r}"
        )
        # Issue #30 AC2: unique case_id of the form
        # ``bootstrap-<8-hex>``. The hex prefix is the
        # 8-character slice of a uuid4's hex.
        cid = case.get("case_id")
        assert isinstance(cid, str) and cid.startswith("bootstrap-"), (
            f"bootstrap case_id must start with 'bootstrap-'; "
            f"got {cid!r}"
        )
        suffix = cid.split("bootstrap-", 1)[1]
        assert len(suffix) == 8, (
            f"bootstrap case_id suffix must be 8 hex chars; got "
            f"id={cid!r} suffix={suffix!r}"
        )
        int(suffix, 16)  # raises ValueError on non-hex
    # All three ids are unique.
    ids = [case["case_id"] for case in case_records]
    assert len(set(ids)) == len(ids), (
        f"bootstrap case_ids must be unique; got {ids!r}"
    )


def test_bootstrap_respects_explicit_case_count(tmp_path: Path) -> None:
    """``--case-count`` overrides the default of 3.

    The test pins ``--case-count 5`` so a future change to
    the default value (or to the parser) cannot silently
    regress the explicit-count contract.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME

    result = _run_metacrucible(
        ["bootstrap", str(workspace), "--case-count", "5", "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`bootstrap --case-count 5` must exit 0; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    records = [
        json.loads(line)
        for line in benchmark.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    case_records = [r for r in records[1:]]
    assert len(case_records) == 5, (
        f"bootstrap must write exactly the requested case count "
        f"(5); got {len(case_records)}"
    )


def test_bootstrap_writes_bootstrap_pending_review_sentinel(
    tmp_path: Path,
) -> None:
    """Every bootstrap-generated case carries the
    ``BOOTSTRAP_PENDING_REVIEW=True`` literal sentinel.

    Issue #30 AC2: "Writes sentinel (``BOOTSTRAP_PENDING_REVIEW``)."
    The literal field name is the machine-stable contract the
    ``optimize`` gate keys off; the test pins both the
    field name and the value so a future change to either
    is a deliberate, single-site update.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME

    result = _run_metacrucible(
        ["bootstrap", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`bootstrap` must exit 0; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    records = [
        json.loads(line)
        for line in benchmark.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    case_records = [r for r in records[1:]]
    assert case_records, (
        "bootstrap must write at least one case record; got "
        f"records={[r.get('case_id') for r in records]!r}"
    )
    for case in case_records:
        assert case.get(BOOTSTRAP_PENDING_REVIEW_FIELD) is True, (
            f"bootstrap case must carry "
            f"{BOOTSTRAP_PENDING_REVIEW_FIELD}=True; got "
            f"case_id={case.get('case_id')!r} "
            f"sentinel={case.get(BOOTSTRAP_PENDING_REVIEW_FIELD)!r}"
        )
        # The literal case is empty for human review:
        # ``checks`` is an empty list, ``judgment`` is None,
        # and ``input`` is a placeholder string the human
        # reviewer replaces before promoting.
        assert case.get("checks") == [], (
            f"bootstrap case must start with empty checks list; "
            f"got {case.get('checks')!r}"
        )
        assert case.get("judgment") is None, (
            f"bootstrap case must start with judgment=None; "
            f"got {case.get('judgment')!r}"
        )
        assert isinstance(case.get("input"), str) and case["input"], (
            f"bootstrap case must carry a placeholder input "
            f"string; got {case.get('input')!r}"
        )
        assert "created_at" in case, (
            f"bootstrap case must record created_at; got "
            f"case_id={case.get('case_id')!r} keys="
            f"{sorted(case.keys())!r}"
        )


# --------------------------------------------------------------------------- #
# AC3 — bootstrap appends without clobbering existing records                 #
# --------------------------------------------------------------------------- #

def test_bootstrap_appends_to_existing_benchmark(tmp_path: Path) -> None:
    """``bootstrap`` is append-only: existing records are
    preserved and the new case records follow in the order
    they were generated.

    The test seeds the benchmark with a metadata record plus
    one reviewed eval and one reviewed held-out case (the
    minimal "optimize-runnable" shape), then runs
    ``bootstrap`` and asserts the original three records are
    still present at the start of the file and the new
    bootstrap cases follow.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    seed_records = [
        _metadata_record(),
        _reviewed_case("eval-1", split="eval"),
        _reviewed_case("held-1", split="held_out"),
    ]
    _write_jsonl(benchmark, seed_records)

    result = _run_metacrucible(
        ["bootstrap", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`bootstrap` must exit 0; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    records = [
        json.loads(line)
        for line in benchmark.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # The original seed records are preserved in order
    # (metadata, eval-1, held-1) and the three bootstrap
    # cases follow.
    assert [r.get("case_id") for r in records] == [
        None,  # metadata record has no case_id
        "eval-1",
        "held-1",
        *[r["case_id"] for r in records[3:]],
    ], (
        f"bootstrap must preserve the original benchmark "
        f"record order and append new cases; got "
        f"ids={[r.get('case_id') for r in records]!r}"
    )
    # The two original reviewed cases are still reviewed.
    assert records[1]["status"] == "reviewed"
    assert records[2]["status"] == "reviewed"
    # The three new bootstrap cases are appended.
    assert len(records) == 1 + 2 + BOOTSTRAP_DEFAULT_CASE_COUNT, (
        f"expected {1 + 2 + BOOTSTRAP_DEFAULT_CASE_COUNT} total "
        f"records (1 metadata + 2 seed + 3 bootstrap); got "
        f"{len(records)}"
    )
    # No half-written tmp file remains on disk.
    assert not (workspace / f"{BENCHMARK_FILE_NAME}.tmp").exists(), (
        f"bootstrap atomic write must not leave a .tmp file; "
        f"found {workspace / (BENCHMARK_FILE_NAME + '.tmp')}"
    )


# --------------------------------------------------------------------------- #
# AC4 — bootstrap records a history event                                      #
# --------------------------------------------------------------------------- #

def test_bootstrap_records_history_event(tmp_path: Path) -> None:
    """Applied ``bootstrap`` appends a ``cases_bootstrapped``
    event to the workspace's ``history.jsonl`` (ADR 0016).

    The audit lineage carries the count and case ids so a
    reviewer can trace which cases were added in which
    bootstrap run.
    """
    workspace = _init_workspace(tmp_path)

    result = _run_metacrucible(
        ["bootstrap", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`bootstrap` must exit 0; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    history = workspace / ".metacrucible" / "history.jsonl"
    assert history.is_file(), (
        f"bootstrap must append a history event to "
        f"{history.relative_to(workspace)}; got "
        f".metacrucible contents: "
        f"{sorted(p.name for p in (workspace / '.metacrucible').iterdir())!r}"
    )
    history_records = [
        json.loads(line)
        for line in history.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    bootstrap_events = [
        r for r in history_records
        if r.get("event") == "cases_bootstrapped"
    ]
    assert len(bootstrap_events) == 1, (
        f"exactly one cases_bootstrapped event must be "
        f"appended; got {len(bootstrap_events)} events: "
        f"{bootstrap_events!r}"
    )
    event = bootstrap_events[0]
    assert event["case_count"] == BOOTSTRAP_DEFAULT_CASE_COUNT, (
        f"history event case_count must match the default; "
        f"got {event['case_count']!r}"
    )
    assert isinstance(event.get("case_ids"), list) and len(
        event["case_ids"]
    ) == BOOTSTRAP_DEFAULT_CASE_COUNT, (
        f"history event case_ids must list all generated "
        f"case ids; got {event.get('case_ids')!r}"
    )
    assert event.get("created_at"), (
        f"history event must record created_at; got {event!r}"
    )
    # The ids in the history event must match the ids in
    # the benchmark file (same bootstrap run).
    benchmark = workspace / BENCHMARK_FILE_NAME
    case_records = [
        json.loads(line)
        for line in benchmark.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][1:]
    benchmark_ids = [c["case_id"] for c in case_records]
    assert event["case_ids"] == benchmark_ids, (
        f"history case_ids must match the benchmark case "
        f"ids (same bootstrap run); history="
        f"{event['case_ids']!r} benchmark={benchmark_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC5 — bootstrap BLOCKED when the workspace is missing the benchmark file     #
# --------------------------------------------------------------------------- #

def test_bootstrap_blocks_when_workspace_missing_benchmark(
    tmp_path: Path,
) -> None:
    """A workspace without a benchmark file is a precondition
    failure.

    The contract is "run ``init`` first" so the benchmark
    container exists at a stable path; bootstrap refuses
    rather than silently creating the container (the latter
    would mask an operator workflow error).
    """
    # ``tmp_path / "ws-bootstrap-missing"`` exists but has
    # NO benchmark file.
    workspace = tmp_path / "ws-bootstrap-missing"
    workspace.mkdir(parents=True, exist_ok=True)
    assert not (workspace / BENCHMARK_FILE_NAME).exists(), (
        f"fixture invariant: workspace must not have a "
        f"benchmark file; found {workspace / BENCHMARK_FILE_NAME}"
    )

    result = _run_metacrucible(
        ["bootstrap", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`bootstrap` on a missing-benchmark workspace must "
        f"exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict), (
        f"bootstrap --json must emit a dict; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert BOOTSTRAP_MISSING_BENCHMARK_BLOCKER in blocker_ids, (
        f"bootstrap missing-benchmark blocker must surface in "
        f"the JSON output; got blocker_ids={blocker_ids!r}"
    )
    # The workspace must not have been mutated by the
    # BLOCKED call: no benchmark file should have been
    # silently created.
    assert not (workspace / BENCHMARK_FILE_NAME).exists(), (
        f"bootstrap BLOCKED must NOT create the benchmark "
        f"file; found {workspace / BENCHMARK_FILE_NAME}"
    )
    # And no history event was written.
    assert not (workspace / ".metacrucible").exists(), (
        f"bootstrap BLOCKED must NOT create the .metacrucible/ "
        f"envelope; found {workspace / '.metacrucible'}"
    )


def test_bootstrap_blocks_when_path_does_not_exist(
    tmp_path: Path,
) -> None:
    """A workspace path that does not exist at all is also
    BLOCKED with the missing-benchmark id.

    The bootstrap command is read-mostly: it does not create
    the workspace directory itself. The contract is "the
    benchmark file must exist", which is the same condition
    whether the parent directory is missing or just the
    benchmark file.
    """
    missing_workspace = tmp_path / "ws-bootstrap-does-not-exist"
    assert not missing_workspace.exists()

    result = _run_metacrucible(
        ["bootstrap", str(missing_workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`bootstrap` on a missing workspace must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert BOOTSTRAP_MISSING_BENCHMARK_BLOCKER in blocker_ids, (
        f"bootstrap missing-workspace must surface the "
        f"missing-benchmark blocker; got blocker_ids="
        f"{blocker_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC6 — ``--json`` flag emits a parseable, machine-stable payload             #
# --------------------------------------------------------------------------- #

def test_bootstrap_json_output_is_parseable_and_has_stable_fields(
    tmp_path: Path,
) -> None:
    """``bootstrap --json`` emits a parseable JSON object with
    the canonical machine-stable keys.

    The shape is the contract downstream automation
    branches on: ``workspace``, ``benchmark``,
    ``case_count``, ``generated_case_ids``, ``sentinel``,
    ``blockers``. The sentinel field surfaces the literal
    field name (``BOOTSTRAP_PENDING_REVIEW``) so a caller
    can echo it back into the optimize gate without
    re-deriving the constant.
    """
    workspace = _init_workspace(tmp_path)

    result = _run_metacrucible(
        ["bootstrap", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`bootstrap --json` must exit 0; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip(), (
        f"`bootstrap --json` must write a JSON payload to "
        f"stdout; got empty stdout (stderr={result.stderr!r})"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`bootstrap --json` must emit valid JSON on "
            f"stdout; got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"bootstrap --json must return a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    for key in (
        "workspace",
        "benchmark",
        "case_count",
        "generated_case_ids",
        "sentinel",
        "blockers",
    ):
        assert key in payload, (
            f"bootstrap --json must surface {key!r}; got "
            f"keys {sorted(payload.keys())!r}"
        )
    assert payload["case_count"] == BOOTSTRAP_DEFAULT_CASE_COUNT
    assert isinstance(payload["generated_case_ids"], list)
    assert len(payload["generated_case_ids"]) == BOOTSTRAP_DEFAULT_CASE_COUNT
    assert payload["sentinel"] == BOOTSTRAP_PENDING_REVIEW_FIELD, (
        f"bootstrap --json sentinel must echo the literal "
        f"field name; got {payload['sentinel']!r}"
    )
    assert payload["blockers"] == [], (
        f"bootstrap --json blockers must be empty on success; "
        f"got {payload['blockers']!r}"
    )
    # The benchmark and workspace paths are absolute,
    # stable, and match the resolved dir.
    assert payload["workspace"] == str(workspace), (
        f"bootstrap --json workspace must match the input; "
        f"got {payload['workspace']!r}"
    )
    assert payload["benchmark"] == str(workspace / BENCHMARK_FILE_NAME), (
        f"bootstrap --json benchmark must be the workspace's "
        f"benchmark.jsonl; got {payload['benchmark']!r}"
    )


def test_bootstrap_human_output_is_english_only(
    tmp_path: Path,
) -> None:
    """Human output of the bootstrap path is English-only.

    Issue #27 task 27.4: the CLI's own prose is the
    English-only contract. User-controlled freeform text
    (e.g. a future review note) is masked by the writer;
    the bootstrap path has no user-controlled freeform
    text, so the human surface stays ASCII throughout.
    """
    workspace = _init_workspace(tmp_path)

    result = _run_metacrucible(
        ["bootstrap", str(workspace)],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`bootstrap` no --json must exit 0; got rc={result.returncode} "
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


# --------------------------------------------------------------------------- #
# AC7 — loader sees the new pending-generated cases                          #
# --------------------------------------------------------------------------- #

def test_bootstrap_makes_cases_visible_to_load_benchmark_loader(
    tmp_path: Path,
) -> None:
    """The ADR 0029 loader must partition the bootstrap-written
    cases into the ``pending_generated`` bucket and surface
    the ``pending-generated-case`` blocker.

    This pins the contract between ``bootstrap`` and the
    existing loader: a downstream reader (e.g. the
    ``optimize`` sentinel gate) that consumes
    :func:`metacrucible.benchmark.load_benchmark` will see
    the bootstrap cases as pending-generated. Without this
    pin, a future change to the on-disk shape could break
    the ``optimize`` gate silently.
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME

    result = _run_metacrucible(
        ["bootstrap", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK

    loaded = load_benchmark(benchmark)
    assert len(loaded.pending_generated_cases) == BOOTSTRAP_DEFAULT_CASE_COUNT, (
        f"loader must put all bootstrap cases in the "
        f"pending_generated bucket; got "
        f"{len(loaded.pending_generated_cases)} pending cases"
    )
    blocker_ids = [b["id"] for b in loaded.blockers]
    assert "pending-generated-case" in blocker_ids, (
        f"loader must surface the pending-generated-case "
        f"blocker after bootstrap; got blocker_ids="
        f"{blocker_ids!r}"
    )
    # Each pending case carries the literal sentinel the
    # optimize gate keys off of.
    for case in loaded.pending_generated_cases:
        assert case.get(BOOTSTRAP_PENDING_REVIEW_FIELD) is True, (
            f"loaded pending case must carry "
            f"{BOOTSTRAP_PENDING_REVIEW_FIELD}=True; got "
            f"{case!r}"
        )


# --------------------------------------------------------------------------- #
# AC8 — argparse usage error for missing workspace positional                   #
# --------------------------------------------------------------------------- #

def test_bootstrap_missing_workspace_argparse_error() -> None:
    """``bootstrap`` with no workspace positional is an
    argparse usage error (Issue #27 task 27.1).

    The CLI dispatcher maps argparse errors to
    :data:`EXIT_USER_ERROR` (1) so the contract is distinct
    from BLOCKED (2) and INTERNAL (3). A missing positional
    is exactly that: argparse usage, not a semantic blocker.
    """
    result = _run_metacrucible(["bootstrap"], cwd=REPO_ROOT)
    assert result.returncode == EXIT_USER_ERROR, (
        f"`bootstrap` with no workspace must exit "
        f"{EXIT_USER_ERROR} (argparse usage); got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# AC9 — case-count validation                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_count", ["0", "-1"])
def test_bootstrap_blocks_non_positive_case_count(
    tmp_path: Path, bad_count: str
) -> None:
    """``--case-count`` must be a positive integer.

    Zero and negative values are rejected with the
    ``bootstrap-invalid-case-count`` blocker and a stable
    ``EXIT_BLOCKED`` exit code, so the bootstrap contract
    is "we always write at least one draft case" (or the
    caller did not really mean to call bootstrap at all).
    """
    workspace = _init_workspace(tmp_path)
    benchmark = workspace / BENCHMARK_FILE_NAME
    # Snapshot the benchmark to assert no append happened
    # on the BLOCKED path.
    before = benchmark.read_text(encoding="utf-8")

    result = _run_metacrucible(
        [
            "bootstrap",
            str(workspace),
            "--case-count",
            bad_count,
            "--json",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`bootstrap --case-count {bad_count}` must exit "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert BOOTSTRAP_INVALID_CASE_COUNT_BLOCKER in blocker_ids, (
        f"bootstrap invalid-case-count blocker must surface "
        f"in the JSON output; got blocker_ids={blocker_ids!r}"
    )
    # The benchmark file is unchanged on the BLOCKED path.
    assert benchmark.read_text(encoding="utf-8") == before, (
        f"bootstrap BLOCKED must not mutate the benchmark "
        f"file; before={before!r} after="
        f"{benchmark.read_text(encoding='utf-8')!r}"
    )
