"""Tests for Issue #6: ``metacrucible init`` workspace envelope + empty benchmark.

Issue #6 pins the public behavior of the ``metacrucible init`` subcommand:

  - ``python -m metacrucible init <artifact-workspace>`` exits 0 and
    creates the repository-side ``.metacrucible/`` envelope/state plus
    a structurally valid empty benchmark container (ADR 0025 says
    empty benchmark files created by ``init`` are valid containers
    but not runnable benchmarks).
  - Re-running ``init`` on an already-initialized workspace is
    stable: it must not crash and must not silently corrupt the
    existing envelope/state/history.
  - The empty benchmark container is structurally valid: the file
    parses as JSONL and the first record is the benchmark
    ``metadata`` record (ADR 0029 pins the one-metadata-record shape).
  - ``init`` emits a machine-parseable ``--json`` output with stable
    top-level fields that downstream automation can rely on.
  - The empty benchmark cannot be run: a workspace validation pass
    (driven by ``init --check``) reports the ``missing-reviewed-case``
    blocker (ADR 0029 lists missing reviewed eval/held-out cases as
    one of the fixed small machine-stable set of invalid benchmark
    blocker codes) and exits with the stable ``EXIT_BLOCKED`` code
    (Issue #27 task 27.1). The blocker id must surface in both
    human output and the parseable JSON output so CI and tooling
    can branch on it.

These tests are the red step: the ``init`` subcommand is not yet
implemented, so invoking ``metacrucible init`` exits with an argparse
"unrecognized arguments" error and the assertions below fail for
that reason — not for a syntactic or import defect.

The implementation under test (not yet written) is expected to live
in ``src/metacrucible/__main__.py`` (the CLI surface) and the storage
helpers exposed by ``metacrucible.storage`` (Issue #5) provide the
file write primitives. ADR 0035 pins ``init`` as a support command
that creates a minimal envelope and benchmark skeleton; it does NOT
emit minimal BLOCKED bundles itself (those are reserved for
``baseline create``, ``evaluate``, ``optimize``, evaluation-stage
``synthesize``, and execution-requested ``review`` per ADR 0035).
The blocker reporting in these tests is therefore surfaced through
``init --check`` (a forward-looking post-init validation pass) rather
than as a BLOCKED bundle.

References
----------
- ADR 0016 (store light history locally, heavy evidence globally).
- ADR 0025 (empty benchmarks are valid containers, not runnable).
- ADR 0029 (benchmark JSONL v1 schema + missing-reviewed-case blocker).
- ADR 0035 (init is noninteractive; does not emit BLOCKED bundles).
- Issue #6 acceptance criteria.
- Issue #27 task 27.1 (stable exit-code matrix).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from metacrucible.exit_codes import EXIT_BLOCKED

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_DIR_NAME = ".metacrucible"
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Stable blocker id for "no reviewed cases are eligible". Pinned by
#: ADR 0029's "fixed small machine-stable set" of invalid benchmark
#: blocker codes (the ADR lists "missing reviewed eval or held-out
#: cases" — the test pins the canonical snake_case form that surfaces
#: in both human and JSON output).
MISSING_REVIEWED_CASE_BLOCKER = "missing-reviewed-case"

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _run_metacrucible(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with ``argv`` inside ``cwd``.

    Both stdout and stderr are captured as text so the test can
    distinguish argparse errors (which write to stderr) from the
    command's own human output (which goes to stdout). Tests assert
    on the captured text directly.
    """
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )

def _init_workspace(tmp_path: Path) -> Path:
    """Run ``init`` against a fresh workspace dir and return that dir.

    Returns the workspace path; tests inspect the resulting files
    under ``<workspace>/.metacrucible/`` and ``<workspace>/<BENCHMARK_FILE_NAME>``.
    """
    workspace = tmp_path / "artifact-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(
        ["init", str(workspace)], cwd=REPO_ROOT
    )
    assert result.returncode == 0, (
        f"`metacrucible init` must exit 0 on a fresh workspace; "
        f"got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return workspace

# --------------------------------------------------------------------------- #
# AC1 — ``init`` creates the workspace envelope and empty benchmark           #
# --------------------------------------------------------------------------- #

def test_init_subcommand_is_recognized(tmp_path: Path) -> None:
    """``init`` must be a recognized ``metacrucible`` subcommand.

    Argparse raises "unrecognized arguments: init" today because the
    subcommand is not yet wired in. Once Issue #6 lands, the
    subcommand will be registered and argparse will return a clean
    usage error or, with a workspace arg, exit 0.
    """
    workspace = tmp_path / "ws-subcommand"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    # Argparse prints "unrecognized arguments: init" to stderr; once
    # the subcommand is registered, the error disappears and rc=0.
    assert "unrecognized arguments" not in result.stderr, (
        f"`metacrucible init` is not a registered subcommand yet; "
        f"got stderr={result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"`metacrucible init <workspace>` must exit 0 on a fresh "
        f"workspace; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

def test_init_exits_zero_on_fresh_workspace(tmp_path: Path) -> None:
    """``init`` must exit 0 on a fresh workspace dir (smoke test)."""
    workspace = tmp_path / "ws-exit-zero"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert result.returncode == 0, (
        f"`metacrucible init` must exit 0 on a fresh workspace; "
        f"got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

def test_init_creates_repository_envelope_directory(tmp_path: Path) -> None:
    """``init`` must create ``<workspace>/.metacrucible/`` (ADR 0016)."""
    workspace = _init_workspace(tmp_path / "ws-envelope")
    envelope_dir = workspace / REPO_DIR_NAME
    assert envelope_dir.is_dir(), (
        f"{REPO_DIR_NAME}/ must be created inside the workspace by "
        f"`metacrucible init`; got {sorted(p.name for p in workspace.iterdir())!r}"
    )

def test_init_creates_envelope_json(tmp_path: Path) -> None:
    """``init`` must create ``<workspace>/.metacrucible/envelope.json``.

    The envelope is the lightweight artifact metadata (ADR 0016 +
    ``RepositoryStorage.write_envelope``). It must be present and
    parseable so downstream commands can read the artifact identity.
    """
    workspace = _init_workspace(tmp_path / "ws-env-json")
    envelope = workspace / REPO_DIR_NAME / "envelope.json"
    assert envelope.is_file(), (
        f"init must write {envelope.relative_to(workspace)}; "
        f"got .metacrucible contents: "
        f"{sorted(p.name for p in (workspace / REPO_DIR_NAME).iterdir())!r}"
    )
    payload = json.loads(envelope.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), (
        f"envelope.json must parse as a JSON object; got {type(payload).__name__}"
    )

def test_init_creates_state_json(tmp_path: Path) -> None:
    """``init`` must create ``<workspace>/.metacrucible/state.json``.

    State holds current best revision / last run id (ADR 0016 +
    ``RepositoryStorage.write_state``). It must be present so
    inspect/optimize have a stable read target.
    """
    workspace = _init_workspace(tmp_path / "ws-state-json")
    state = workspace / REPO_DIR_NAME / "state.json"
    assert state.is_file(), (
        f"init must write {state.relative_to(workspace)}; "
        f"got .metacrucible contents: "
        f"{sorted(p.name for p in (workspace / REPO_DIR_NAME).iterdir())!r}"
    )
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), (
        f"state.json must parse as a JSON object; got {type(payload).__name__}"
    )

def test_init_creates_empty_benchmark_container(tmp_path: Path) -> None:
    """``init`` must create a benchmark file at the workspace root.

    Per ADR 0025, empty benchmark files created by ``init`` are valid
    containers. The file must exist so subsequent ``bootstrap`` and
    ``evaluate`` commands have a stable target path.
    """
    workspace = _init_workspace(tmp_path / "ws-bench-file")
    benchmark = workspace / BENCHMARK_FILE_NAME
    assert benchmark.is_file(), (
        f"init must create {benchmark.relative_to(workspace)}; "
        f"got workspace contents: "
        f"{sorted(p.name for p in workspace.iterdir())!r}"
    )

# --------------------------------------------------------------------------- #
# AC2 — empty benchmark is a structurally valid JSONL container               #
# --------------------------------------------------------------------------- #

def test_empty_benchmark_parses_as_jsonl(tmp_path: Path) -> None:
    """The empty benchmark file must parse as JSONL with a metadata record.

    ADR 0029 pins the v1 shape: one ``metadata`` record first, then
    case records. An empty benchmark is a valid container that
    contains exactly the metadata record and zero case records.
    """
    workspace = _init_workspace(tmp_path / "ws-jsonl-parse")
    benchmark = workspace / BENCHMARK_FILE_NAME
    raw = benchmark.read_text(encoding="utf-8")
    # Split into non-blank lines; each line must be valid JSON.
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert lines, (
        f"empty benchmark container must contain at least the metadata "
        f"record (ADR 0029); got empty file {benchmark.relative_to(workspace)}"
    )
    records = []
    for idx, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"empty benchmark line {idx} must be valid JSON; "
                f"line={line!r} error={exc}"
            )
    assert records, "empty benchmark must have at least the metadata record"

def test_empty_benchmark_first_record_is_metadata(tmp_path: Path) -> None:
    """The first JSONL record must declare itself as ``metadata``.

    ADR 0029: the first line is the benchmark-level ``metadata``
    record. The ``record_type`` discriminator is the stable contract
    that the loader (Issue #7) branches on; ``init`` must stamp it.
    """
    workspace = _init_workspace(tmp_path / "ws-metadata-record")
    benchmark = workspace / BENCHMARK_FILE_NAME
    lines = [
        ln for ln in benchmark.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert lines, (
        f"empty benchmark must contain at least the metadata record; "
        f"got empty file {benchmark.relative_to(workspace)}"
    )
    first = json.loads(lines[0])
    assert isinstance(first, dict), (
        f"first benchmark record must be a JSON object; got {type(first).__name__}"
    )
    record_type = first.get("record_type") or first.get("type") or first.get("kind")
    assert record_type == "metadata", (
        f"first benchmark record must be record_type='metadata' "
        f"(ADR 0029); got {record_type!r} (full first record: {first!r})"
    )

# --------------------------------------------------------------------------- #
# AC3 — re-running init is stable (idempotent or explicit)                    #
# --------------------------------------------------------------------------- #

def test_init_rerun_is_stable_and_does_not_crash(tmp_path: Path) -> None:
    """A second ``init`` on the same workspace must not crash.

    ADR 0035: ``init`` is noninteractive and must not corrupt the
    existing envelope/state. The contract is "stable, non-crashing
    behavior": the second invocation must exit with a clean
    returncode (0 if idempotent, a stable nonzero if it reports
    "already initialized") and must not raise. Pinning the exact
    returncode here would over-constrain; pinning "no crash + no
    argparse error" is the right floor.
    """
    workspace = tmp_path / "ws-rerun"
    workspace.mkdir(parents=True, exist_ok=True)
    first = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert first.returncode == 0, (
        f"first `init` must exit 0; got rc={first.returncode} "
        f"stderr={first.stderr!r}"
    )
    second = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    # Must not raise or emit a Python traceback.
    assert "Traceback (most recent call last)" not in second.stderr, (
        f"second `init` raised a Python exception; "
        f"stderr={second.stderr!r}"
    )
    # Must not be an argparse error (e.g. "unrecognized arguments").
    assert "unrecognized arguments" not in second.stderr, (
        f"second `init` failed argparse parsing; stderr={second.stderr!r}"
    )

def test_init_rerun_preserves_existing_envelope_json(tmp_path: Path) -> None:
    """Re-running ``init`` must not silently overwrite the envelope.

    ADR 0016 + ADR 0020: the envelope is the artifact's identity
    record. ``init`` is allowed to be idempotent OR to report
    "already initialized", but it must not silently mutate the
    existing envelope content.
    """
    workspace = _init_workspace(tmp_path / "ws-preserve-env")
    envelope = workspace / REPO_DIR_NAME / "envelope.json"
    before = envelope.read_text(encoding="utf-8")
    second = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert second.returncode == 0, (
        f"second `init` must exit 0 (idempotent) or a stable nonzero "
        f"(already-initialized); got rc={second.returncode} "
        f"stderr={second.stderr!r}"
    )
    after = envelope.read_text(encoding="utf-8")
    assert after == before, (
        f"second `init` must not mutate envelope.json content; "
        f"before={before!r} after={after!r}"
    )

# --------------------------------------------------------------------------- #
# AC4 — ``--json`` output is machine-parseable                                 #
# --------------------------------------------------------------------------- #

def test_init_json_output_is_parseable(tmp_path: Path) -> None:
    """``init --json`` must emit a parseable JSON object on stdout.

    Argparse errors and human banner text do not satisfy this: the
    JSON object must be the only meaningful content on stdout, and
    it must round-trip through ``json.loads``.
    """
    workspace = tmp_path / "ws-json"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(
        ["init", str(workspace), "--json"], cwd=REPO_ROOT
    )
    assert result.returncode == 0, (
        f"`metacrucible init --json` must exit 0; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip(), (
        f"`metacrucible init --json` must write a JSON payload to stdout; "
        f"got empty stdout (stderr={result.stderr!r})"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`metacrucible init --json` must emit valid JSON on stdout; "
            f"got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"init --json must return a JSON object; got {type(payload).__name__} "
        f"({payload!r})"
    )

def test_init_json_output_has_stable_machine_fields(tmp_path: Path) -> None:
    """``init --json`` must include stable, machine-branchable fields.

    Downstream automation (CI, scripts, future commands) needs to
    read at least the workspace path, the envelope/state file paths,
    and the benchmark file path off the JSON output. The exact field
    names are an implementation detail; the test pins only the
    minimum useful surface.
    """
    workspace = tmp_path / "ws-json-fields"
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run_metacrucible(
        ["init", str(workspace), "--json"], cwd=REPO_ROOT
    )
    assert result.returncode == 0, (
        f"`metacrucible init --json` must exit 0; got rc={result.returncode} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    # The JSON must tell the caller where the workspace envelope/state
    # live. The field names are a stable contract; tests use the
    # lower-snake-case names that match the rest of MetaCrucible's
    # CLI conventions.
    envelope_field = (
        payload.get("envelope_path")
        or payload.get("envelope")
    )
    state_field = (
        payload.get("state_path")
        or payload.get("state")
    )
    assert envelope_field, (
        f"init --json must report the envelope path; got keys "
        f"{sorted(payload.keys())!r}"
    )
    assert state_field, (
        f"init --json must report the state path; got keys "
        f"{sorted(payload.keys())!r}"
    )

# --------------------------------------------------------------------------- #
# AC5 — empty benchmark cannot run: missing-reviewed-case blocker             #
# --------------------------------------------------------------------------- #

def _init_and_check(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Initialize a workspace and run ``init --check`` against it."""
    workspace = tmp_path / "ws-check"
    workspace.mkdir(parents=True, exist_ok=True)
    init = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert init.returncode == 0, (
        f"`init` must exit 0 before --check; got rc={init.returncode} "
        f"stderr={init.stderr!r}"
    )
    return _run_metacrucible(
        ["init", "--check", str(workspace)], cwd=REPO_ROOT
    )

def test_init_check_exits_nonzero_for_empty_benchmark(tmp_path: Path) -> None:
    """``init --check`` must exit ``EXIT_BLOCKED`` on an empty benchmark.

    The empty benchmark is "valid but not runnable" (ADR 0025); the
    check pass must surface that as the stable ``EXIT_BLOCKED`` exit
    code (Issue #27 task 27.1) so automation can branch on it
    without re-deriving the matrix. The pre-Issue-#27 floor was
    "nonzero"; the new contract pins the exact value.
    """
    result = _init_and_check(tmp_path)
    assert result.returncode == EXIT_BLOCKED, (
        f"`init --check` must exit {EXIT_BLOCKED} for an empty benchmark "
        f"(missing-reviewed-case blocker); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

def test_init_check_human_output_contains_blocker_id(tmp_path: Path) -> None:
    """Human output of ``init --check`` must mention ``missing-reviewed-case``.

    The blocker id is the stable machine contract (ADR 0029). Humans
    reading the failure must see the id verbatim so they can map
    failure → fix.
    """
    result = _init_and_check(tmp_path)
    assert result.returncode == EXIT_BLOCKED, (
        f"`init --check` must exit {EXIT_BLOCKED} on empty benchmark; "
        f"got rc={result.returncode} stdout={result.stdout!r}"
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert MISSING_REVIEWED_CASE_BLOCKER in combined, (
        f"`init --check` human output must mention the "
        f"{MISSING_REVIEWED_CASE_BLOCKER!r} blocker id (ADR 0029); "
        f"got stdout={result.stdout!r} stderr={result.stderr!r}"
    )

def test_init_check_json_output_is_parseable_and_has_blockers(
    tmp_path: Path,
) -> None:
    """``init --check --json`` must emit parseable JSON with a blockers field.

    The blockers list is the machine-stable way to surface the
    missing-reviewed-case condition. The test pins the JSON shape
    (parseable, dict, carries a ``blockers`` field) and the
    presence of the canonical blocker id inside that field.
    """
    workspace = tmp_path / "ws-check-json"
    workspace.mkdir(parents=True, exist_ok=True)
    init = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert init.returncode == 0, (
        f"`init` must exit 0 before --check; got rc={init.returncode} "
        f"stderr={init.stderr!r}"
    )
    result = _run_metacrucible(
        ["init", "--check", str(workspace), "--json"], cwd=REPO_ROOT
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`init --check --json` must exit {EXIT_BLOCKED} on empty "
        f"benchmark; got rc={result.returncode} stdout={result.stdout!r}"
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`init --check --json` must emit valid JSON on stdout; "
            f"got stdout={result.stdout!r} error={exc}"
        )
    assert isinstance(payload, dict), (
        f"`init --check --json` must return a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    blockers: Any = payload.get("blockers")
    assert blockers is not None, (
        f"`init --check --json` must report a 'blockers' field; "
        f"got keys {sorted(payload.keys())!r}"
    )
    assert isinstance(blockers, list), (
        f"`init --check --json` 'blockers' must be a list; got "
        f"{type(blockers).__name__} ({blockers!r})"
    )
    blocker_ids: list[str] = []
    for entry in blockers:
        if isinstance(entry, str):
            blocker_ids.append(entry)
        elif isinstance(entry, dict):
            # Accept either ``{"id": "..."}`` or ``{"code": "..."}`` as
            # the machine identifier — the implementor picks the
            # shape, the test asserts the id is present.
            bid = entry.get("id") or entry.get("code")
            if isinstance(bid, str):
                blocker_ids.append(bid)
    assert MISSING_REVIEWED_CASE_BLOCKER in blocker_ids, (
        f"`init --check --json` blockers must include "
        f"{MISSING_REVIEWED_CASE_BLOCKER!r} (ADR 0029); "
        f"got blocker_ids={blocker_ids!r} (full blockers={blockers!r})"
    )


# --------------------------------------------------------------------------- #
# AC6 — init BLOCKED path does not emit an evidence bundle (Issue #27 27.2)  #
# --------------------------------------------------------------------------- #
#
# ADR 0035: ``init`` is a non-emitting BLOCKED category. The
# ``init --check`` BLOCKED exit must surface the missing-reviewed-
# case blocker through CLI output only — no evidence bundle is
# written. The test pins the no-bundle contract for the existing
# ``init --check`` flow so a future change that accidentally
# couples ``init`` to a bundle helper fails loud.

def test_init_check_blocked_does_not_create_evidence_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``init --check`` BLOCKED must not write a user-global evidence bundle.

    The init BLOCKED path is a non-emitting category (ADR 0035).
    Blockers are reported through CLI output; no
    ``$HOME/.metacrucible/evidence/`` directory is created.

    HOME is pinned to a temp dir so the test cannot leak evidence
    into the developer's real ``~/.metacrucible/`` if it
    misbehaves.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))

    workspace = tmp_path / "ws-init-blocked"
    workspace.mkdir(parents=True, exist_ok=True)
    init = _run_metacrucible(["init", str(workspace)], cwd=REPO_ROOT)
    assert init.returncode == 0, (
        f"`init` must exit 0 before --check; got rc={init.returncode} "
        f"stderr={init.stderr!r}"
    )

    result = _run_metacrucible(
        ["init", "--check", str(workspace), "--json"], cwd=REPO_ROOT
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`init --check --json` must exit {EXIT_BLOCKED} on empty "
        f"benchmark; got rc={result.returncode} stdout={result.stdout!r}"
    )

    evidence_root = fake_home / ".metacrucible" / "evidence"
    if evidence_root.exists():
        contents = sorted(p.name for p in evidence_root.iterdir())
        assert contents == [], (
            f"`init --check` must NOT create an evidence bundle "
            f"(ADR 0035 non-emitting category); found {contents!r} "
            f"under {evidence_root}"
        )
    # If the evidence root was never created, that is the cleanest
    # possible signal that no bundle was written. The above branch
    # covers the case where the root exists but is empty (which can
    # happen if a previous test in the same run-id namespace wrote
    # to a sibling directory).
