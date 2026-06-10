"""Tests for Issue #27 task 27.2: BLOCKED bundle policy matrix + helper.

ADR 0035 pins which command/stage categories must emit a minimal
``BLOCKED`` evidence bundle when blocked, and which must not. The
policy is exposed as a code-readable matrix in
:mod:`metacrucible.blocked_bundles`; this module pins that contract
in tests and proves the helper produces minimal, sanitized bundles
via the existing Issue #26 storage builders.

References
----------
- ADR 0030 (receipt and evidence bundle v1 schema).
- ADR 0035 (MVP CLI surface and operational behavior).
- Issue #26 (storage builders).
- Issue #27 task 27.2 (BLOCKED bundle matrix + helper).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

#: Marker substring used by a few negative-path tests to prove the
#: writer scrubbed a would-be absolute path. The substring is
#: distinctive so a coincidental match in the bundle contents
#: would still fail the assertion.
_TEMP_PATH_FRAGMENT = "/tmp/this/should/not/leak"


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def isolated_global_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Pin ``HOME`` to a temp dir so the global storage layer does not
    pollute the developer's real ``~/.metacrucible/``.

    Mirrors the fixture in ``tests/test_repository_storage.py`` so
    the new tests can run alongside the storage tests without
    stepping on the same ``HOME``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


# --------------------------------------------------------------------------- #
# Policy matrix membership (ADR 0035)                                         #
# --------------------------------------------------------------------------- #


def test_matrix_includes_baseline_create() -> None:
    """``baseline create`` must be in the emitting matrix (ADR 0035)."""
    from metacrucible.blocked_bundles import (
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "baseline_create" in REQUIRES_BLOCKED_BUNDLE_CATEGORIES
    assert requires_blocked_bundle("baseline_create") is True


def test_matrix_includes_evaluate() -> None:
    """``evaluate`` must be in the emitting matrix (ADR 0035)."""
    from metacrucible.blocked_bundles import (
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "evaluate" in REQUIRES_BLOCKED_BUNDLE_CATEGORIES
    assert requires_blocked_bundle("evaluate") is True


def test_matrix_includes_optimize() -> None:
    """``optimize`` must be in the emitting matrix (ADR 0035)."""
    from metacrucible.blocked_bundles import (
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "optimize" in REQUIRES_BLOCKED_BUNDLE_CATEGORIES
    assert requires_blocked_bundle("optimize") is True


def test_matrix_includes_synthesize_evaluation_stage() -> None:
    """``synthesize`` evaluation stage must be in the emitting matrix (ADR 0035)."""
    from metacrucible.blocked_bundles import (
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "synthesize_evaluation_stage" in REQUIRES_BLOCKED_BUNDLE_CATEGORIES
    assert requires_blocked_bundle("synthesize_evaluation_stage") is True


def test_matrix_includes_review_execution_requested() -> None:
    """``review`` (when execution was requested) must be in the
    emitting matrix (ADR 0035)."""
    from metacrucible.blocked_bundles import (
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "review_execution_requested" in REQUIRES_BLOCKED_BUNDLE_CATEGORIES
    assert requires_blocked_bundle("review_execution_requested") is True


def test_matrix_excludes_init() -> None:
    """``init`` must NOT be in the emitting matrix (ADR 0035)."""
    from metacrucible.blocked_bundles import (
        NON_EMITTING_BLOCKED_CATEGORIES,
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "init" in NON_EMITTING_BLOCKED_CATEGORIES
    assert "init" not in REQUIRES_BLOCKED_BUNDLE_CATEGORIES
    assert requires_blocked_bundle("init") is False


def test_matrix_excludes_inspect() -> None:
    """``inspect`` must NOT be in the emitting matrix (ADR 0035)."""
    from metacrucible.blocked_bundles import (
        NON_EMITTING_BLOCKED_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "inspect" in NON_EMITTING_BLOCKED_CATEGORIES
    assert requires_blocked_bundle("inspect") is False


def test_matrix_excludes_bootstrap() -> None:
    """Ordinary ``bootstrap`` must NOT be in the emitting matrix
    (ADR 0035).

    The evaluation stage of ``synthesize`` (a separate category)
    *is* emitting; ordinary bootstrap is not.
    """
    from metacrucible.blocked_bundles import (
        NON_EMITTING_BLOCKED_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "bootstrap" in NON_EMITTING_BLOCKED_CATEGORIES
    assert requires_blocked_bundle("bootstrap") is False


def test_matrix_excludes_promote() -> None:
    """``promote`` must NOT be in the emitting matrix (ADR 0035)."""
    from metacrucible.blocked_bundles import (
        NON_EMITTING_BLOCKED_CATEGORIES,
        requires_blocked_bundle,
    )

    assert "promote" in NON_EMITTING_BLOCKED_CATEGORIES
    assert requires_blocked_bundle("promote") is False


def test_matrix_alias_matches_long_name() -> None:
    """The short alias must have identical membership to the long name.

    Callers may use either ``REQUIRES_BLOCKED_BUNDLE`` or
    ``REQUIRES_BLOCKED_BUNDLE_CATEGORIES``; the test pins the
    invariant so the two cannot drift.
    """
    from metacrucible.blocked_bundles import (
        REQUIRES_BLOCKED_BUNDLE,
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES,
    )

    assert REQUIRES_BLOCKED_BUNDLE is REQUIRES_BLOCKED_BUNDLE_CATEGORIES
    assert REQUIRES_BLOCKED_BUNDLE == REQUIRES_BLOCKED_BUNDLE_CATEGORIES


def test_matrix_sets_are_disjoint() -> None:
    """The emitting and non-emitting matrices must not overlap.

    An overlap is a contract violation — a category cannot be
    both emitting and non-emitting.
    """
    from metacrucible.blocked_bundles import (
        NON_EMITTING_BLOCKED_CATEGORIES,
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES,
    )

    overlap = (
        REQUIRES_BLOCKED_BUNDLE_CATEGORIES & NON_EMITTING_BLOCKED_CATEGORIES
    )
    assert overlap == frozenset(), (
        f"emitting and non-emitting matrices must be disjoint; "
        f"overlap={sorted(overlap)!r}"
    )


def test_requires_blocked_bundle_treats_unknown_category_as_non_emitting() -> None:
    """An unknown category must default to non-emitting.

    A caller that introduces a new category must add it to the
    matrix first; the lookup must not silently expand the
    contract.
    """
    from metacrucible.blocked_bundles import requires_blocked_bundle

    assert requires_blocked_bundle("never_added_category") is False


def test_requires_blocked_bundle_treats_non_string_as_non_emitting() -> None:
    """A non-string ``category`` must not crash the lookup."""
    from metacrucible.blocked_bundles import requires_blocked_bundle

    assert requires_blocked_bundle("") is False
    assert requires_blocked_bundle(None) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Helper: contract                                                            #
# --------------------------------------------------------------------------- #


def test_helper_writes_three_durable_bundle_files(
    isolated_global_home: Path,
) -> None:
    """The helper must write exactly ``receipt.json``, ``summary.json``,
    and ``trajectory-digest.json``.

    A ``BLOCKED`` bundle is a "we could not proceed" record, not a
    run record; it must not create a ``raw/`` subdirectory (the
    run did not execute) and it must not write ``cleanup.json``
    (no prune happened).
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-001",
        run_type="evaluate",
        blockers=[{"id": "missing-reviewed-case", "message": "nope"}],
    )
    present = sorted(p.name for p in bundle.iterdir())
    assert present == [
        "receipt.json",
        "summary.json",
        "trajectory-digest.json",
    ], (
        f"BLOCKED bundle must contain exactly the three durable files; "
        f"got {present!r}"
    )


def test_helper_receipt_status_is_blocked(
    isolated_global_home: Path,
) -> None:
    """The receipt ``status`` must be ``BLOCKED``."""
    from metacrucible.blocked_bundles import (
        BLOCKED_STATUS,
        write_blocked_bundle,
    )
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-002",
        run_type="optimize",
        blockers=[{"id": "no-baseline", "message": "baseline missing"}],
    )
    payload = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert payload["status"] == BLOCKED_STATUS == "BLOCKED", (
        f"receipt status must be BLOCKED; got {payload.get('status')!r}"
    )


def test_helper_summary_status_is_blocked(
    isolated_global_home: Path,
) -> None:
    """The summary ``status`` must be ``BLOCKED``."""
    from metacrucible.blocked_bundles import (
        BLOCKED_STATUS,
        write_blocked_bundle,
    )
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-003",
        run_type="evaluate",
        blockers=[{"id": "x", "message": "y"}],
    )
    payload = json.loads((bundle / "summary.json").read_text(encoding="utf-8"))
    assert payload["status"] == BLOCKED_STATUS == "BLOCKED", (
        f"summary status must be BLOCKED; got {payload.get('status')!r}"
    )


def test_helper_trajectory_digest_status_is_blocked(
    isolated_global_home: Path,
) -> None:
    """The trajectory digest ``status`` must be ``BLOCKED``."""
    from metacrucible.blocked_bundles import (
        BLOCKED_STATUS,
        write_blocked_bundle,
    )
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-004",
        run_type="baseline_create",
        blockers=[{"id": "x", "message": "y"}],
    )
    payload = json.loads(
        (bundle / "trajectory-digest.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == BLOCKED_STATUS == "BLOCKED", (
        f"trajectory digest status must be BLOCKED; "
        f"got {payload.get('status')!r}"
    )


def test_helper_receipt_threads_blocker_ids_and_messages(
    isolated_global_home: Path,
) -> None:
    """The receipt must carry every blocker id and message passed in."""
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    blockers = [
        {"id": "missing-reviewed-case", "message": "no reviewed eval cases"},
        {"id": "missing-artifact", "message": "artifact is not on disk"},
    ]
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-005",
        run_type="evaluate",
        blockers=blockers,
    )
    payload = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    receipt_blockers = payload["blockers"]
    assert isinstance(receipt_blockers, list)
    ids = [b["id"] for b in receipt_blockers]
    assert ids == ["missing-reviewed-case", "missing-artifact"]
    assert receipt_blockers[0]["message"] == "no reviewed eval cases"
    assert receipt_blockers[1]["message"] == "artifact is not on disk"


def test_helper_summary_threads_blocker_ids_and_messages(
    isolated_global_home: Path,
) -> None:
    """The summary must carry the same blocker ids and messages as the receipt."""
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    blockers = [
        {"id": "missing-reviewed-case", "message": "no reviewed eval cases"},
        {"id": "missing-artifact", "message": "artifact is not on disk"},
    ]
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-006",
        run_type="evaluate",
        blockers=blockers,
    )
    payload = json.loads((bundle / "summary.json").read_text(encoding="utf-8"))
    summary_blockers = payload["blockers"]
    assert isinstance(summary_blockers, list)
    ids = [b["id"] for b in summary_blockers]
    assert ids == ["missing-reviewed-case", "missing-artifact"]


def test_helper_receipt_has_default_sibling_refs(
    isolated_global_home: Path,
) -> None:
    """The receipt must default its sibling refs to ``summary.json`` and
    ``trajectory-digest.json`` so the bundle is self-contained.

    Issue #26 pins the default sibling refs; a BLOCKED bundle
    inherits that contract because the helper goes through the
    same writer.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-007",
        run_type="evaluate",
        blockers=[{"id": "x", "message": "y"}],
    )
    payload = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert payload["summary_ref"] == "summary.json"
    assert payload["trajectory_digest_ref"] == "trajectory-digest.json"


def test_helper_does_not_leak_absolute_temp_path_in_machine_evidence(
    isolated_global_home: Path,
) -> None:
    """The bundle must not contain a raw absolute local path in machine
    evidence.

    ADR 0030: machine evidence stores hashes, categories, and
    relative bundle references — not raw local paths. A caller
    that accidentally threads a ``Path`` or absolute string into a
    blocker message (e.g. from an exception traceback) must see
    it scrubbed by the existing summary / digest builders, not
    leak into the shared bundle.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-008",
        run_type="evaluate",
        blockers=[
            {
                "id": "io-error",
                "message": (
                    "failed to read " + _TEMP_PATH_FRAGMENT + " for evidence"
                ),
            },
        ],
    )
    for filename in ("receipt.json", "summary.json", "trajectory-digest.json"):
        text = (bundle / filename).read_text(encoding="utf-8")
        assert _TEMP_PATH_FRAGMENT not in text, (
            f"{filename} leaked an absolute path into machine evidence: {text!r}"
        )
        assert "[redacted:absolute-path]" in text, (
            f"{filename} must scrub the absolute path marker explicitly; "
            f"got {text!r}"
        )


def test_helper_drops_blocker_entries_without_id(
    isolated_global_home: Path,
) -> None:
    """A blocker entry without a stable id must be dropped.

    The matrix is the machine contract; an entry without an id
    cannot be branched on. Dropping it (rather than writing
    ``unknown``) is the contract — a reviewer must see a clean
    list of stable ids.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-009",
        run_type="evaluate",
        blockers=[
            {"id": "valid", "message": "ok"},
            {"message": "no id"},
            {"id": "", "message": "empty id"},
            {"id": 123, "message": "non-string id"},
        ],
    )
    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    ids = [b["id"] for b in receipt["blockers"]]
    assert ids == ["valid"], (
        "only the entry with a stable id must remain; "
        f"got ids={ids!r} (full blockers={receipt['blockers']!r})"
    )


def test_helper_trajectory_steps_match_blocker_count(
    isolated_global_home: Path,
) -> None:
    """The trajectory digest must carry one ``blocked`` step per blocker.

    The bounded, redacted narrative of a BLOCKED bundle is "one
    step per blocker" — no raw events, no transcripts. A reviewer
    can scan the steps to see *why* the run was blocked.
    """
    from metacrucible.blocked_bundles import BLOCKED_STATUS, write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    blockers = [
        {"id": "missing-reviewed-case", "message": "nope"},
        {"id": "missing-artifact", "message": "nope2"},
    ]
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-010",
        run_type="optimize",
        blockers=blockers,
    )
    payload = json.loads(
        (bundle / "trajectory-digest.json").read_text(encoding="utf-8")
    )
    steps = payload["steps"]
    assert len(steps) == 2, f"expected 2 steps (one per blocker); got {len(steps)}"
    for idx, step in enumerate(steps):
        assert step["step"] == idx
        assert step["action"] == "blocked"
        assert step["status"] == BLOCKED_STATUS
        assert step["blocker"]["id"] == blockers[idx]["id"]


def test_helper_uses_v1_schema_version_on_every_file(
    isolated_global_home: Path,
) -> None:
    """Every file the helper writes must carry ``schema_version = 1``.

    The helper reuses the Issue #26 writers, which stamp the v1
    version on every artifact. This test pins the v1 contract so
    a future change that accidentally re-stamps or omits the
    version fails loud.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import SCHEMA_VERSION, UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-011",
        run_type="evaluate",
        blockers=[{"id": "x", "message": "y"}],
    )
    for filename in ("receipt.json", "summary.json", "trajectory-digest.json"):
        payload = json.loads((bundle / filename).read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_VERSION == 1, (
            f"{filename} must carry schema_version=1; got {payload!r}"
        )


def test_helper_threads_optional_identities_to_receipt(
    isolated_global_home: Path,
) -> None:
    """Identity fields passed via ``identities=`` must appear on the receipt.

    The receipt is the bundle entrypoint; identity fields bind
    the BLOCKED bundle to the run that could not proceed so a
    reviewer can correlate it with the broader run history. A
    caller that has no identities (run blocked before identity
    resolution) can omit ``identities=`` and the helper still
    works — that path is covered by the other tests.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    identities = {
        "artifact": {"artifact_kind": "skill", "artifact_sha": "a" * 64},
        "benchmark_sha": "b" * 64,
        "executable_benchmark_sha": "c" * 64,
        "runtime_adapter": {"name": "claude-code", "version": "0.4.1"},
        "model_identities": {"control": "anthropic/claude-opus-4"},
    }
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-012",
        run_type="evaluate",
        blockers=[{"id": "x", "message": "y"}],
        identities=identities,
    )
    payload = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    for key, value in identities.items():
        assert payload[key] == value, (
            f"receipt must carry identity {key!r}={value!r}; "
            f"got {payload.get(key)!r}"
        )


def test_helper_identities_cannot_override_helper_owned_fields(
    isolated_global_home: Path,
) -> None:
    """The helper must own ``run_id``, ``run_type``, ``status``, and
    ``blockers``.

    A caller that passes ``identities={"status": "PASS"}`` must
    not be able to bypass the BLOCKED status. The matrix is the
    contract; helper-owned fields stay helper-owned.
    """
    from metacrucible.blocked_bundles import (
        BLOCKED_STATUS,
        write_blocked_bundle,
    )
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-013",
        run_type="evaluate",
        blockers=[{"id": "x", "message": "y"}],
        identities={
            "status": "PASS",
            "run_id": "forged",
            "run_type": "forged",
            "blockers": [{"id": "forged"}],
        },
    )
    payload = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert payload["status"] == BLOCKED_STATUS
    assert payload["run_id"] == "run-blocked-013"
    assert payload["run_type"] == "evaluate"
    ids = [b["id"] for b in payload["blockers"]]
    assert ids == ["x"], (
        f"helper must keep its own blockers; got ids={ids!r}"
    )


def test_helper_rejects_empty_run_id() -> None:
    """An empty ``run_id`` must be rejected before any write happens."""
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    with pytest.raises(ValueError):
        write_blocked_bundle(
            global_store,
            run_id="",
            run_type="evaluate",
            blockers=[{"id": "x", "message": "y"}],
        )


def test_helper_rejects_empty_run_type() -> None:
    """An empty ``run_type`` must be rejected before any write happens.

    ``run_type`` is the machine-stable category that ties the
    bundle to the matrix; an empty value defeats the contract.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    with pytest.raises(ValueError):
        write_blocked_bundle(
            global_store,
            run_id="run-blocked-014",
            run_type="",
            blockers=[{"id": "x", "message": "y"}],
        )


def test_helper_rejects_non_mapping_identities() -> None:
    """A non-mapping ``identities`` must be rejected."""
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    with pytest.raises(ValueError):
        write_blocked_bundle(
            global_store,
            run_id="run-blocked-015",
            run_type="evaluate",
            blockers=[{"id": "x", "message": "y"}],
            identities=["not", "a", "mapping"],  # type: ignore[arg-type]
        )


def test_helper_returns_bundle_directory_path(
    isolated_global_home: Path,
) -> None:
    """The helper must return the bundle directory path.

    Callers (e.g. the future ``cmd_evaluate`` and friends) need
    the path to render it in human/JSON output. The path must
    live under ``$HOME/.metacrucible/evidence/<run_id>/``.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-016",
        run_type="optimize",
        blockers=[{"id": "x", "message": "y"}],
    )
    expected = (
        isolated_global_home
        / ".metacrucible"
        / "evidence"
        / "run-blocked-016"
    )
    assert bundle == expected, (
        f"bundle path must be {expected}; got {bundle!r}"
    )
    assert bundle.is_dir(), f"bundle path {bundle!r} must be a directory"


def test_helper_does_not_create_raw_subdirectory(
    isolated_global_home: Path,
) -> None:
    """A BLOCKED bundle must not create a ``raw/`` subdirectory.

    The run did not execute; there is no raw evidence to retain.
    A ``raw/`` subdirectory would imply a future prune pass
    could delete evidence the run never produced.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-017",
        run_type="evaluate",
        blockers=[{"id": "x", "message": "y"}],
    )
    assert not (bundle / "raw").exists(), (
        f"BLOCKED bundle must not create a raw/ subdirectory; "
        f"got contents {sorted(p.name for p in bundle.iterdir())!r}"
    )


def test_helper_does_not_create_cleanup_file(
    isolated_global_home: Path,
) -> None:
    """A BLOCKED bundle must not create a ``cleanup.json``.

    ``cleanup.json`` is written by a prune pass, not by the
    bundle helper. A BLOCKED bundle that creates a ``cleanup.json``
    would imply a prune happened, which is not the contract.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-018",
        run_type="evaluate",
        blockers=[{"id": "x", "message": "y"}],
    )
    assert not (bundle / "cleanup.json").exists(), (
        f"BLOCKED bundle must not create cleanup.json; "
        f"got contents {sorted(p.name for p in bundle.iterdir())!r}"
    )


# --------------------------------------------------------------------------- #
# Issue #27 task 27.3: helper is JSONL-independent                            #
# --------------------------------------------------------------------------- #


def test_helper_module_does_not_import_claude_stream_json() -> None:
    """The blocked-bundle helper must not import the stream-json parser.

    ADR 0035: optional JSONL logs are not evidence sources of
    truth. The bundle helper writes the receipt, summary, and
    trajectory digest — the bundle source of truth — and must
    not depend on the adapter-side parser.

    The test asserts the import boundary in code (not just in
    spirit) so the contract is enforceable: a future caller that
    wants to add JSONL-aware logic to the bundle helper must
    break this test and update the ADR first. The assertion
    checks the public surface of the helper module: a
    ``from .claude_stream_json import X`` statement in
    ``blocked_bundles`` would bind ``X`` on the helper module,
    and the parser's public functions / constants are the
    canonical "imported from claude_stream_json" sentinel.
    """
    import metacrucible.blocked_bundles as blocked_bundles_mod

    for name in (
        "parse_stream_json",
        "ADAPTER_VERSION",
        "StreamInput",
    ):
        assert not hasattr(blocked_bundles_mod, name), (
            f"metacrucible.blocked_bundles must not re-export "
            f"{name!r} from metacrucible.claude_stream_json: the "
            f"bundle helper is the evidence source of truth and "
            f"must not depend on the adapter-side JSONL parser "
            f"(ADR 0035). Offending attribute found on the helper "
            f"module — likely a ``from .claude_stream_json "
            f"import {name}`` import."
        )


def test_helper_does_not_read_jsonl_log_files(
    isolated_global_home: Path,
) -> None:
    """The helper must not open or read any stream-json / JSONL log
    file on disk.

    A ``BLOCKED`` bundle is the record of "we could not
    proceed". The helper takes a normalised blockers list as
    input; the receipt / summary / trajectory digest are
    populated from that list, not from a JSONL log file. Even
    if a caller places a stream-json / JSONL log next to the
    bundle, the helper must not open it — the receipt writer
    (ADR 0030) reads only the well-formed fields off the
    evidence dict, never the raw log.

    The test plants a sibling file with deliberately invalid
    JSONL content. If the helper were to open it, the test
    would observe the read via a monkey-patched ``open``
    (and in any case the helper would have to surface a parse
    error). The assertion is simpler: the bundle files are
    written, the helper returns, and the planted file is never
    read.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    # Pre-create the bundle directory and plant a "stream-json"
    # log file next to where the bundle will land. The
    # evidence_bundle_dir() call from inside the helper is
    # monkey-patched below to point at the same dir, so the
    # helper would be able to find the planted file if it
    # tried to read it.
    bundle_root = (
        isolated_global_home / ".metacrucible" / "evidence" / "run-blocked-019"
    )
    bundle_root.mkdir(parents=True, exist_ok=True)
    planted = bundle_root / "claude-stream.jsonl"
    planted.write_text(
        "{not even close to json\n" * 5,
        encoding="utf-8",
    )
    assert planted.exists()

    # Track every file the helper opens. The helper writes the
    # three bundle files; we want to prove the planted log is
    # not in that set.
    opened: list[str] = []
    real_open = open

    def _tracking_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    import builtins
    builtins.open = _tracking_open
    try:
        global_store = UserGlobalStorage()
        bundle = write_blocked_bundle(
            global_store,
            run_id="run-blocked-019",
            run_type="evaluate",
            blockers=[{"id": "x", "message": "y"}],
        )
    finally:
        builtins.open = real_open

    # The helper wrote the three bundle files (it created
    # ``receipt.json``, ``summary.json``, ``trajectory-digest.json``
    # — the planted file was pre-existing, the helper did not
    # author it). And critically, the helper did not open the
    # planted log.
    written = {p.name for p in bundle.iterdir()}
    assert "receipt.json" in written
    assert "summary.json" in written
    assert "trajectory-digest.json" in written
    assert not any(str(planted) == path for path in opened), (
        f"helper must not open the planted stream-json log; "
        f"observed opens={opened!r}"
    )


def test_helper_threads_event_log_refs_as_opaque_refs(
    isolated_global_home: Path,
) -> None:
    """``event_log_refs`` supplied via ``identities=`` must pass through
    the receipt as opaque sibling-relative refs.

    The receipt is the bundle source of truth (ADR 0030). An
    ``event_log_refs`` entry is a list of *refs* — sibling
    filenames inside the bundle — supplied by the caller. The
    helper must not parse the referenced files, must not
    require them to exist, and must not use them as
    classification evidence. The receipt simply records the
    refs verbatim so a reviewer can locate the optional log
    files inside the bundle if they exist.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    # Note: the referenced file does NOT exist. The helper
    # must not care; the ref is opaque to MetaCrucible.
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-020",
        run_type="evaluate",
        blockers=[{"id": "x", "message": "y"}],
        identities={
            "event_log_refs": ["claude-stream.jsonl", "tool-events.jsonl"],
        },
    )
    payload = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert payload.get("event_log_refs") == [
        "claude-stream.jsonl",
        "tool-events.jsonl",
    ], (
        f"event_log_refs must pass through the receipt verbatim as "
        f"opaque sibling-relative refs; got "
        f"event_log_refs={payload.get('event_log_refs')!r}"
    )


def test_helper_rejects_event_log_refs_that_are_not_sibling_relative(
    isolated_global_home: Path,
) -> None:
    """An ``event_log_refs`` entry that is not a sibling-relative
    filename must be rejected by the receipt builder.

    The receipt builder (ADR 0030) treats ``event_log_refs`` as
    a list of sibling-relative filenames inside the bundle. A
    ref that escapes the bundle (absolute path, traversal,
    subpath) is not a valid ref. The helper does not need to
    add new validation: the existing receipt builder raises
    ``ValueError`` on a bad ref, which is the contract.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    with pytest.raises(ValueError):
        write_blocked_bundle(
            global_store,
            run_id="run-blocked-021",
            run_type="evaluate",
            blockers=[{"id": "x", "message": "y"}],
            identities={
                "event_log_refs": ["/absolute/path/claude-stream.jsonl"],
            },
        )


def test_helper_receipt_summary_trajectory_are_source_of_truth(
    isolated_global_home: Path,
) -> None:
    """The receipt, summary, and trajectory digest are the bundle
    source of truth; no helper code reads an event log to build
    them.

    This is the explicit "no helper depends on JSONL content"
    contract from Issue #27 task 27.3. The helper's input is
    the normalised blockers list and the optional identities
    mapping; the bundle is written from those. The receipt
    ``blockers`` field is the machine contract; the summary
    and trajectory digest are bounded, redacted views of the
    same blockers. An external log file (if a caller chooses
    to record one) is referenced via ``event_log_refs`` and
    is not opened by the helper.
    """
    from metacrucible.blocked_bundles import write_blocked_bundle
    from metacrucible.storage import UserGlobalStorage

    global_store = UserGlobalStorage()
    blockers = [
        {"id": "missing-reviewed-case", "message": "no reviewed cases"},
        {"id": "missing-artifact", "message": "artifact not on disk"},
    ]
    bundle = write_blocked_bundle(
        global_store,
        run_id="run-blocked-022",
        run_type="optimize",
        blockers=blockers,
    )

    # Receipt is the bundle entrypoint; the blockers list is
    # the machine contract.
    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert [b["id"] for b in receipt["blockers"]] == [
        b["id"] for b in blockers
    ]
    assert receipt["status"] == "BLOCKED"

    # Summary is the aggregate view; same blockers, same
    # status. No event-log fields appear on the summary at all.
    summary = json.loads((bundle / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"
    assert [b["id"] for b in summary["blockers"]] == [
        b["id"] for b in blockers
    ]
    # The summary allowlist (ADR 0030) does not list any
    # stream-json / event-log field; assert the summary
    # carries only the bundle-sot fields.
    assert set(summary.keys()) == {"schema_version", "status", "blockers"}, (
        f"summary must carry only bundle source-of-truth fields; "
        f"got {sorted(summary.keys())!r}"
    )

    # Trajectory digest is the bounded, redacted narrative; one
    # step per blocker. No event-log content.
    trajectory = json.loads(
        (bundle / "trajectory-digest.json").read_text(encoding="utf-8")
    )
    assert trajectory["status"] == "BLOCKED"
    assert [step["action"] for step in trajectory["steps"]] == [
        "blocked",
        "blocked",
    ]
    assert "raw_events" not in trajectory
    assert "transcript" not in trajectory
