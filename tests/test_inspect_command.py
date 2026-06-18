"""Tests for Issue #42 / PRD F5 ``metacrucible inspect`` subcommand.

Task 1 pins only the public parser surface, the ``main()`` dispatch
branch, and the read-only contract that a missing path never writes
a BLOCKED evidence bundle:

  - ``metacrucible inspect <path> [--json]`` parses via the central
    :func:`metacrucible.__main__._build_parser` and exposes the
    ``path`` positional plus the ``--json`` flag.
  - ``main(['inspect', <artifact>, '--json'])`` dispatches to
    :func:`metacrucible.__main__.cmd_inspect` and returns
    :data:`metacrucible.exit_codes.EXIT_OK`.
  - A missing path is reported to ``stderr`` and returns a
    non-zero exit code without creating
    ``$HOME/.metacrucible/evidence/`` on disk.
  - ``cmd_inspect`` never imports or calls
    :func:`metacrucible.blocked_bundles.write_blocked_bundle`; the
    missing-path branch must stay free of any BLOCKED-bundle write.

Task 2 replaces the temporary Task 1 payload with the real-schema
reader pinned by the F5 acceptance criteria: inspect consumes
``.metacrucible/state.json`` (with ``schema_version``,
``current_best_revision``, ``last_run_id``, ``baseline``),
``envelope.json``, and the optimizer's append-only
``history.jsonl`` of event-shaped records
(``optimize_started`` / ``optimize_accepted`` /
``optimize_rejected`` with nested ``decision`` dicts). The reader
must surface a revision history table, acceptance decisions, and
the current best revision id; the read-only / no-bundle contract
proven in Task 1 must stay true throughout.

Task 3 extends inspect with a user-global evidence bundle index
scanned from ``$HOME/.metacrucible/evidence/<run_id>/receipt.json``
plus a fallback best-revision resolution: when
``state.current_best_revision`` is null, inspect resolves the
current best revision id from the latest accepted event in the
real ``optimize_accepted`` history. The user-global scan must
not mutate the inspected workspace or the monkeypatched ``HOME``
tree in either ``--json`` or human output mode.
"""
from __future__ import annotations

import argparse
import inspect
import json

import pytest

from metacrucible.__main__ import (
    _build_parser,
    cmd_inspect,
    main,
)
from metacrucible.exit_codes import EXIT_OK, EXIT_USER_ERROR

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture()
def isolated_global_home(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Pin ``HOME`` to a temp dir so any user-global storage write
    would land in ``tmp_path`` instead of the developer's real
    ``~/.metacrucible/``.

    Mirrors the fixture in :mod:`tests.test_review_command` so the
    new tests can run alongside the review tests without
    stepping on the same ``HOME``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    return str(fake_home)

# --------------------------------------------------------------------------- #
# Parser surface                                                              #
# --------------------------------------------------------------------------- #

def test_inspect_parser_accepts_path(tmp_path) -> None:
    from metacrucible.__main__ import _build_parser

    artifact = tmp_path / "artifact.md"
    args = _build_parser().parse_args(["inspect", str(artifact)])

    assert args.command == "inspect"
    assert args.path == str(artifact)
    assert args.json is False

def test_inspect_parser_accepts_json(tmp_path) -> None:
    from metacrucible.__main__ import _build_parser

    artifact = tmp_path / "artifact.md"
    args = _build_parser().parse_args(
        ["inspect", str(artifact), "--json"]
    )

    assert args.command == "inspect"
    assert args.path == str(artifact)
    assert args.json is True

# --------------------------------------------------------------------------- #
# Read-only contract: missing path → no BLOCKED bundle                        #
# --------------------------------------------------------------------------- #

def test_inspect_missing_path_does_not_write_evidence(
    tmp_path,
    isolated_global_home: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing path must return EXIT_USER_ERROR, report to stderr,
    and NOT create ``$HOME/.metacrucible/evidence/`` on disk.

    This pins the F5 acceptance bullet "No files are modified" for
    the negative-input branch and proves that ``cmd_inspect`` does
    not call :func:`metacrucible.blocked_bundles.write_blocked_bundle`.
    """
    missing = tmp_path / "does-not-exist.md"

    # ``isolated_global_home`` is ``tmp_path / "home"``; resolve to a
    # concrete Path so the post-condition check is readable.
    from pathlib import Path

    home = Path(isolated_global_home)
    evidence_root = home / ".metacrucible" / "evidence"

    args = argparse.Namespace(path=str(missing), json=True)

    rc = cmd_inspect(args)

    captured = capsys.readouterr()
    assert rc != 0, (
        "missing path must return a non-zero exit code; "
        f"got rc={rc}"
    )
    assert rc == EXIT_USER_ERROR
    assert "inspect path" in captured.err
    assert "does not exist" in captured.err
    assert not evidence_root.exists(), (
        f"missing-path branch must not create {evidence_root}; "
        "the inspect command is contractually read-only"
    )
    assert captured.out == "", (
        "missing-path branch must not emit a payload to stdout"
    )

def test_inspect_does_not_reference_write_blocked_bundle() -> None:
    """Static guarantee that ``cmd_inspect`` stays free of the
    BLOCKED-bundle writer even after future refactors.

    The Task 1 contract pins inspect as a read-only command; this
    test fails loudly if a later task accidentally imports
    :func:`metacrucible.blocked_bundles.write_blocked_bundle` into
    the ``cmd_inspect`` source body.
    """
    source = inspect.getsource(cmd_inspect)
    assert "write_blocked_bundle" not in source, (
        "cmd_inspect must not call write_blocked_bundle; the inspect "
        "command is contractually read-only (PRD F5 'No files are "
        "modified')"
    )

# --------------------------------------------------------------------------- #
# main() dispatch                                                             #
# --------------------------------------------------------------------------- #

def test_inspect_dispatch_smoke_returns_exit_ok(
    tmp_path,
    isolated_global_home: str,
) -> None:
    """``main(['inspect', <artifact>, '--json'])`` reaches
    :func:`cmd_inspect` and returns :data:`EXIT_OK`.

    The smoke test pins dispatch end-to-end (parser + ``cmd_inspect``
    + JSON emission) for the real-schema reader. The fixture writes
    a minimal valid state plus a single optimizer event record so
    the reader takes its success branch.
    """
    artifact, _workspace = make_inspect_workspace(tmp_path)

    rc = main(["inspect", str(artifact), "--json"])

    assert rc == EXIT_OK

# --------------------------------------------------------------------------- #
# Task 2 — fixture helpers for state / envelope / history (PRD F5)           #
# --------------------------------------------------------------------------- #

def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

def _append_history(workspace, records):
    (workspace / "history.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )

def _decision(round_id, revision_id, *, accepted, eval_score, held_out_delta, accepted_at):
    return {
        "round_id": round_id,
        "revision_id": revision_id,
        "accepted": accepted,
        "status": "ACCEPTED" if accepted else "REJECTED",
        "eval_score": eval_score,
        "held_out_delta": held_out_delta,
        "accepted_at": accepted_at,
    }

def make_inspect_workspace(tmp_path):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Artifact\n", encoding="utf-8")
    workspace = tmp_path / ".metacrucible"
    workspace.mkdir()
    _write_json(workspace / "envelope.json", {"status": "ready"})
    _write_json(
        workspace / "state.json",
        {
            "schema_version": 1,
            "current_best_revision": "rev-001",
            "last_run_id": "run-001",
            "baseline": {"artifact_path": str(artifact)},
        },
    )
    _append_history(
        workspace,
        [
            {
                "event": "optimize_started",
                "run_id": "run-001",
                "workspace": str(tmp_path),
                "artifact_path": str(artifact),
                "base_content_hash": "sha256:base",
                "max_rounds": 2,
                "human_confirmed": True,
                "timestamp": "2026-06-18T00:00:00Z",
            },
            {
                "event": "optimize_rejected",
                "run_id": "run-001",
                "round_id": "round-001",
                "decision": _decision(
                    "round-001",
                    "rev-000",
                    accepted=False,
                    eval_score=0.70,
                    held_out_delta=-0.01,
                    accepted_at=None,
                ),
                "timestamp": "2026-06-18T00:01:00Z",
            },
            {
                "event": "optimize_accepted",
                "run_id": "run-001",
                "round_id": "round-002",
                "decision": _decision(
                    "round-002",
                    "rev-001",
                    accepted=True,
                    eval_score=0.75,
                    held_out_delta=0.03,
                    accepted_at="2026-06-18T00:02:00Z",
                ),
                "timestamp": "2026-06-18T00:02:00Z",
            },
        ],
    )
    return artifact, workspace

def snapshot_tree(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

# --------------------------------------------------------------------------- #
# Task 2 — JSON happy path                                                    #
# --------------------------------------------------------------------------- #

def test_inspect_json_reads_real_state_and_event_history(tmp_path, monkeypatch, capsys):
    from metacrucible.__main__ import cmd_inspect

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    artifact, _workspace = make_inspect_workspace(tmp_path)

    code = cmd_inspect(argparse.Namespace(path=str(artifact), json=True))

    captured = capsys.readouterr()
    assert code == EXIT_OK
    payload = json.loads(captured.out)
    assert set(payload) == {
        "artifact_path",
        "workspace_path",
        "envelope_status",
        "current_best_revision_id",
        "revision_history",
        "acceptance_decisions",
        "evidence_bundles",
    }
    assert payload["envelope_status"] == "ready"
    assert payload["current_best_revision_id"] == "rev-001"
    assert payload["revision_history"] == [
        {
            "event": "optimize_started",
            "run_id": "run-001",
            "round_id": None,
            "revision_id": None,
            "status": "STARTED",
            "accepted_at": None,
            "eval_score": None,
            "held_out_delta": None,
            "timestamp": "2026-06-18T00:00:00Z",
        },
        {
            "event": "optimize_rejected",
            "run_id": "run-001",
            "round_id": "round-001",
            "revision_id": "rev-000",
            "status": "REJECTED",
            "accepted_at": None,
            "eval_score": 0.70,
            "held_out_delta": -0.01,
            "timestamp": "2026-06-18T00:01:00Z",
        },
        {
            "event": "optimize_accepted",
            "run_id": "run-001",
            "round_id": "round-002",
            "revision_id": "rev-001",
            "status": "ACCEPTED",
            "accepted_at": "2026-06-18T00:02:00Z",
            "eval_score": 0.75,
            "held_out_delta": 0.03,
            "timestamp": "2026-06-18T00:02:00Z",
        },
    ]
    assert payload["acceptance_decisions"] == [
        {
            "event": "optimize_rejected",
            "run_id": "run-001",
            "round_id": "round-001",
            "revision_id": "rev-000",
            "status": "REJECTED",
            "accepted": False,
            "accepted_at": None,
            "eval_score": 0.70,
            "held_out_delta": -0.01,
            "timestamp": "2026-06-18T00:01:00Z",
        },
        {
            "event": "optimize_accepted",
            "run_id": "run-001",
            "round_id": "round-002",
            "revision_id": "rev-001",
            "status": "ACCEPTED",
            "accepted": True,
            "accepted_at": "2026-06-18T00:02:00Z",
            "eval_score": 0.75,
            "held_out_delta": 0.03,
            "timestamp": "2026-06-18T00:02:00Z",
        },
    ]
    assert payload["evidence_bundles"] == []
    assert "blockers" not in payload
    assert "evidence_refs" not in payload

# --------------------------------------------------------------------------- #
# Task 2 — human output                                                       #
# --------------------------------------------------------------------------- #

def test_inspect_human_output_shows_required_sections(tmp_path, capsys):
    from metacrucible.__main__ import cmd_inspect

    artifact, _workspace = make_inspect_workspace(tmp_path)

    code = cmd_inspect(argparse.Namespace(path=str(artifact), json=False))

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "Artifact path:" in captured.out
    assert "Envelope status:" in captured.out
    assert "Current best revision id: rev-001" in captured.out
    assert "Revision history:" in captured.out
    assert "revision_id | status | accepted_at | eval_score | held_out_delta" in captured.out
    assert "rev-000 | REJECTED |  | 0.7 | -0.01" in captured.out
    assert "rev-001 | ACCEPTED | 2026-06-18T00:02:00Z | 0.75 | 0.03" in captured.out
    assert "Acceptance decisions:" in captured.out
    assert "Evidence bundle index:" in captured.out

# --------------------------------------------------------------------------- #
# Task 2 — missing state, no BLOCKED evidence bundle                          #
# --------------------------------------------------------------------------- #

def test_inspect_missing_state_returns_clean_error_without_blocked_bundle(
    tmp_path, monkeypatch, capsys
):
    from metacrucible.__main__ import cmd_inspect

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Artifact\n", encoding="utf-8")
    (tmp_path / ".metacrucible").mkdir()

    code = cmd_inspect(argparse.Namespace(path=str(artifact), json=True))

    captured = capsys.readouterr()
    assert code == EXIT_USER_ERROR
    assert captured.out == ""
    assert "missing state.json" in captured.err
    assert not (home / ".metacrucible" / "evidence").exists()

# --------------------------------------------------------------------------- #
# Task 3 — empty workspace (real state, no history)                           #
# --------------------------------------------------------------------------- #

def test_inspect_empty_workspace_reports_empty_lists_and_no_best_revision(tmp_path, monkeypatch, capsys):
    from metacrucible.__main__ import cmd_inspect

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Artifact\n", encoding="utf-8")
    workspace = tmp_path / ".metacrucible"
    workspace.mkdir()
    _write_json(
        workspace / "state.json",
        {
            "schema_version": 1,
            "current_best_revision": None,
            "last_run_id": None,
            "baseline": None,
        },
    )
    _write_json(workspace / "envelope.json", {"status": "ready"})

    code = cmd_inspect(argparse.Namespace(path=str(artifact), json=True))

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["revision_history"] == []
    assert payload["acceptance_decisions"] == []
    assert payload["evidence_bundles"] == []
    assert payload["current_best_revision_id"] is None

# --------------------------------------------------------------------------- #
# Task 3 — user-global evidence receipt index                                 #
# --------------------------------------------------------------------------- #

def test_inspect_indexes_user_global_evidence_receipts(tmp_path, monkeypatch, capsys):
    from metacrucible.__main__ import cmd_inspect

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    artifact, _workspace = make_inspect_workspace(tmp_path)
    evidence = home / ".metacrucible" / "evidence" / "run-001"
    _write_json(
        evidence / "receipt.json",
        {
            "run_id": "run-001",
            "run_type": "optimize",
            "status": "PASS",
            "summary_ref": "summary.json",
            "trajectory_digest_ref": "trajectory-digest.json",
        },
    )
    _write_json(evidence / "summary.json", {"status": "PASS"})
    _write_json(evidence / "trajectory-digest.json", {"steps": []})

    code = cmd_inspect(argparse.Namespace(path=str(artifact), json=True))

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["evidence_bundles"] == [
        {
            "run_id": "run-001",
            "receipt_path": str(evidence / "receipt.json"),
            "summary_path": str(evidence / "summary.json"),
            "run_type": "optimize",
            "status": "PASS",
            "summary_ref": "summary.json",
            "trajectory_digest_ref": "trajectory-digest.json",
        }
    ]

# --------------------------------------------------------------------------- #
# Task 3 — malformed receipt is skipped (Quality repair)                       #
# --------------------------------------------------------------------------- #

def test_inspect_skips_malformed_receipts(tmp_path, monkeypatch, capsys):
    """A corrupt ``receipt.json`` under
    ``$HOME/.metacrucible/evidence/<run_id>/`` must not crash
    the entire inspect diagnostic.

    The corrupt bundle is silently skipped while the sibling
    valid bundle still appears in ``evidence_bundles`` and the
    exit code stays :data:`EXIT_OK`. Regression for the quality
    review finding on ``_load_evidence_bundles`` — per-receipt
    JSON errors must be isolated per-bundle.
    """
    from metacrucible.__main__ import cmd_inspect

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    artifact, _workspace = make_inspect_workspace(tmp_path)

    # Well-formed bundle that MUST survive.
    good = home / ".metacrucible" / "evidence" / "run-good"
    _write_json(
        good / "receipt.json",
        {
            "run_id": "run-good",
            "run_type": "optimize",
            "status": "PASS",
            "summary_ref": "summary.json",
            "trajectory_digest_ref": "trajectory-digest.json",
        },
    )
    _write_json(good / "summary.json", {"status": "PASS"})
    _write_json(good / "trajectory-digest.json", {"steps": []})

    # Sibling with a syntactically broken receipt — must be
    # skipped without aborting the loop.
    bad = home / ".metacrucible" / "evidence" / "run-bad"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "receipt.json").write_text("{not valid json", encoding="utf-8")

    code = cmd_inspect(argparse.Namespace(path=str(artifact), json=True))

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["evidence_bundles"] == [
        {
            "run_id": "run-good",
            "receipt_path": str(good / "receipt.json"),
            "summary_path": str(good / "summary.json"),
            "run_type": "optimize",
            "status": "PASS",
            "summary_ref": "summary.json",
            "trajectory_digest_ref": "trajectory-digest.json",
        }
    ]

# --------------------------------------------------------------------------- #
# Task 3 — best revision falls back to latest accepted event                  #
# --------------------------------------------------------------------------- #

def test_inspect_best_revision_falls_back_to_latest_accepted_event(tmp_path, capsys):
    from metacrucible.__main__ import cmd_inspect

    artifact, workspace = make_inspect_workspace(tmp_path)
    _write_json(
        workspace / "state.json",
        {
            "schema_version": 1,
            "current_best_revision": None,
            "last_run_id": "run-001",
            "baseline": {"artifact_path": str(artifact)},
        },
    )
    _append_history(
        workspace,
        [
            {
                "event": "optimize_rejected",
                "run_id": "run-001",
                "round_id": "round-001",
                "decision": _decision(
                    "round-001",
                    "rev-001",
                    accepted=False,
                    eval_score=0.60,
                    held_out_delta=-0.02,
                    accepted_at=None,
                ),
                "timestamp": "2026-06-18T00:01:00Z",
            },
            {
                "event": "optimize_accepted",
                "run_id": "run-001",
                "round_id": "round-002",
                "decision": _decision(
                    "round-002",
                    "rev-002",
                    accepted=True,
                    eval_score=0.80,
                    held_out_delta=0.04,
                    accepted_at="2026-06-18T00:02:00Z",
                ),
                "timestamp": "2026-06-18T00:02:00Z",
            },
            {
                "event": "optimize_accepted",
                "run_id": "run-001",
                "round_id": "round-003",
                "decision": _decision(
                    "round-003",
                    "rev-003",
                    accepted=True,
                    eval_score=0.82,
                    held_out_delta=0.05,
                    accepted_at="2026-06-18T00:03:00Z",
                ),
                "timestamp": "2026-06-18T00:03:00Z",
            },
        ],
    )

    json_code = cmd_inspect(argparse.Namespace(path=str(artifact), json=True))
    json_payload = json.loads(capsys.readouterr().out)
    human_code = cmd_inspect(argparse.Namespace(path=str(artifact), json=False))
    human_output = capsys.readouterr().out

    assert json_code == EXIT_OK
    assert human_code == EXIT_OK
    assert json_payload["current_best_revision_id"] == "rev-003"
    assert "Current best revision id: rev-003" in human_output

# --------------------------------------------------------------------------- #
# Task 3 — state current_best_revision wins over fallback                    #
# --------------------------------------------------------------------------- #

def test_inspect_best_revision_prefers_state_current_best_revision(tmp_path, capsys):
    from metacrucible.__main__ import cmd_inspect

    artifact, workspace = make_inspect_workspace(tmp_path)
    _write_json(
        workspace / "state.json",
        {
            "schema_version": 1,
            "current_best_revision": "rev-state",
            "last_run_id": "run-001",
            "baseline": {"artifact_path": str(artifact)},
        },
    )

    code = cmd_inspect(argparse.Namespace(path=str(artifact), json=True))

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["current_best_revision_id"] == "rev-state"

# --------------------------------------------------------------------------- #
# Task 3 — inspect never mutates workspace or HOME                            #
# --------------------------------------------------------------------------- #

def test_inspect_does_not_modify_workspace_or_home(tmp_path, monkeypatch, capsys):
    from metacrucible.__main__ import cmd_inspect

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    artifact, workspace = make_inspect_workspace(tmp_path)
    _write_json(home / ".metacrucible" / "evidence" / "run-001" / "receipt.json", {"run_id": "run-001"})
    before = snapshot_tree(tmp_path)

    assert cmd_inspect(argparse.Namespace(path=str(artifact), json=True)) == EXIT_OK
    capsys.readouterr()
    assert cmd_inspect(argparse.Namespace(path=str(artifact), json=False)) == EXIT_OK
    capsys.readouterr()

    assert snapshot_tree(tmp_path) == before
    assert workspace.is_dir()

# --------------------------------------------------------------------------- #
# Task 4 — full PRD F5 public acceptance + negative BLOCKED-bundle tests     #
# --------------------------------------------------------------------------- #

def test_inspect_public_command_full_prd_f5_acceptance(
    tmp_path, monkeypatch, capsys
):
    """End-to-end ``main()`` acceptance for Issue #42 / PRD F5.

    Pins every public surface that PRD F5 demands:

      * ``metacrucible inspect <path>`` returns ``EXIT_OK``.
      * ``metacrucible inspect <path> --json`` returns
        ``EXIT_OK`` and the JSON payload exposes
        ``revision_history`` (non-empty),
        ``acceptance_decisions`` (non-empty),
        ``evidence_bundles`` (non-empty), and
        ``current_best_revision_id`` resolved to ``"rev-001"``.
      * Human output names the artifact path, envelope status,
        current best revision, revision-history table,
        acceptance decisions, and evidence bundle index.
      * The inspected workspace and monkeypatched ``$HOME``
        tree are byte-for-byte unchanged after both runs
        (read-only contract from PRD F5 bullet "No files are
        modified").

    The receipt is written under BOTH the repo-local
    ``workspace / "evidence" / "run-001" / "receipt.json"``
    (so the workspace snapshot stays consistent) and the
    user-global ``$HOME/.metacrucible/evidence/run-001/``
    (so the evidence-bundle indexer finds it). The
    repo-local copy is *not* indexed by inspect.
    """
    from metacrucible.__main__ import main

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    artifact, workspace = make_inspect_workspace(tmp_path)

    # Repo-local receipt — kept for snapshot consistency only;
    # the inspect indexer reads from $HOME/.metacrucible/evidence.
    _write_json(
        workspace / "evidence" / "run-001" / "receipt.json",
        {"run_id": "run-001", "run_type": "optimize", "status": "PASS"},
    )
    # User-global receipt — the one the indexer actually picks up.
    user_evidence = home / ".metacrucible" / "evidence" / "run-001"
    _write_json(
        user_evidence / "receipt.json",
        {
            "run_id": "run-001",
            "run_type": "optimize",
            "status": "PASS",
            "summary_ref": "summary.json",
            "trajectory_digest_ref": "trajectory-digest.json",
        },
    )
    _write_json(user_evidence / "summary.json", {"status": "PASS"})
    _write_json(user_evidence / "trajectory-digest.json", {"steps": []})

    before = snapshot_tree(tmp_path)

    human_code = main(["inspect", str(artifact)])
    human = capsys.readouterr().out
    json_code = main(["inspect", str(artifact), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert human_code == EXIT_OK
    assert json_code == EXIT_OK
    assert "Artifact path:" in human
    assert "Envelope status:" in human
    assert "Current best revision id: rev-001" in human
    assert "Revision history:" in human
    assert "revision_id | status | accepted_at | eval_score | held_out_delta" in human
    assert "Acceptance decisions:" in human
    assert "Evidence bundle index:" in human
    assert payload["revision_history"]
    assert payload["acceptance_decisions"]
    assert payload["evidence_bundles"]
    assert payload["current_best_revision_id"] == "rev-001"
    assert snapshot_tree(tmp_path) == before

def test_inspect_never_writes_blocked_bundle_on_bad_input(
    tmp_path, monkeypatch, capsys
):
    """Pin the F5 contract: bad input never emits a BLOCKED bundle.

    The ``write_blocked_bundle`` writer is monkeypatched to raise
    on every call. If ``cmd_inspect`` accidentally routes any
    bad-input branch through ``write_blocked_bundle``, the test
    blows up with a loud ``AssertionError`` carrying the
    forbidden call message. Each bad-input scenario must instead
    return :data:`EXIT_USER_ERROR` with a clean ``metacrucible:``
    line on ``stderr``.

    Covers three negative paths:

      * missing artifact path (no file at all),
      * artifact present but no ``.metacrucible`` workspace,
      * workspace present but ``state.json`` missing.
    """
    import metacrucible.__main__ as cli

    def fail_writer(*args, **kwargs):
        raise AssertionError("inspect must not write BLOCKED bundle")

    monkeypatch.setattr(cli, "write_blocked_bundle", fail_writer)

    missing = tmp_path / "missing.md"
    assert cli.main(["inspect", str(missing)]) == EXIT_USER_ERROR
    capsys.readouterr()

    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Artifact\n", encoding="utf-8")
    assert cli.main(["inspect", str(artifact)]) == EXIT_USER_ERROR
    capsys.readouterr()

    (tmp_path / ".metacrucible").mkdir()
    assert cli.main(["inspect", str(artifact)]) == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "missing state.json" in captured.err

def test_inspect_is_not_blocked_bundle_emitter(tmp_path, monkeypatch, capsys):
    """Inspect must never create ``$HOME/.metacrucible/evidence``.

    Pins the policy matrix cell for ``inspect``: it is a
    non-emitting diagnostic, so the user-global evidence root
    must stay untouched even when the command fails on a missing
    path. Mirrors the BLOCKED-bundle policy coverage used for
    the other non-emitting commands.
    """
    import metacrucible.__main__ as cli

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert cli.main(["inspect", str(tmp_path / "missing.md")]) == EXIT_USER_ERROR
    capsys.readouterr()
    assert not (home / ".metacrucible" / "evidence").exists()