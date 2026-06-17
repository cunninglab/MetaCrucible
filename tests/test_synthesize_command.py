"""Tests for Issue #41 (PRD F4 ``metacrucible synthesize``).

Task 1 ships ONLY the parser shell + a temporary
``synthesize-not-implemented`` placeholder; Task 2 wires the real
synthesis pipeline in (input resolution, draft canonical source,
generated cases, workspace writes). These tests pin the public
parser contract that subsequent tasks must not break AND the
Task 2 command-level contract:

  - ``synthesize`` is a registered subcommand of
    ``metacrucible`` (reachable from both ``python -m metacrucible``
    and the ``metacrucible`` console script).
  - A positional ``capability_need`` argument captures the freeform
    capability-need text.
  - ``--from <path>`` (stored on the namespace as ``from_spec``)
    captures the spec-file alternative input mode.
  - The two input modes are mutually exclusive: providing both, or
    neither, must raise ``SystemExit(2)`` at the parser level so
    automation sees a stable usage-error exit code (Issue #27).
  - Shared CLI flags (``--output``, ``--max-rounds``,
    ``--allow-routing-revision``, ``--allow-dirty-unrelated``,
    ``--confirm-resume``, ``--json``) are wired and expose their
    ``--no-...`` counterparts as the snake_case namespace
    attributes that the dispatcher and downstream command code
    read.

The real synthesis pipeline (workspace creation, baseline write,
benchmark generation, optimization loop wiring) lands in later
tasks; those tests live in subsequent Task-N files.

Task 2 contract pinned by the command-level tests below:

  - A draft canonical source is produced and a baseline is recorded.
  - Generated Evaluation Cases are produced for the draft and held
    pending review (the same
    :data:`metacrucible.benchmark.STATUS_GENERATED` sentinel +
    :data:`metacrucible.benchmark.BOOTSTRAP_PENDING_REVIEW_FIELD`
    envelope mechanism as F2 ``bootstrap``).
  - The blocker id ``synthesize-not-implemented`` from Task 1 is
    REMOVED; valid input creates the workspace and exits with
    :data:`metacrucible.exit_codes.EXIT_OK` and a
    ``draft_pending_review`` outcome.
  - Precondition blockers (missing spec path, empty spec content,
    existing output directory) return
    :data:`metacrucible.exit_codes.EXIT_BLOCKED` with stable ids
    and do NOT create the workspace.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from pathlib import Path

import pytest

from metacrucible.benchmark import SPLIT_EVAL, SPLIT_HELD_OUT, STATUS_GENERATED
from metacrucible.exit_codes import EXIT_BLOCKED, EXIT_OK
from metacrucible.optimizer import ROUND_BUDGET_DEFAULT
from metacrucible.synthesize import (
    BENCHMARK_FILE_NAME,
    BOOTSTRAP_PENDING_REVIEW_FIELD,
    SYNTHESIZE_DRAFT_PENDING_REVIEW,
    SYNTHESIZE_INPUT_MISSING_BLOCKER,
    SYNTHESIZE_OUTPUT_EXISTS_BLOCKER,
    SYNTHESIZE_SPEC_EMPTY_BLOCKER,
    SYNTHESIZE_SPEC_MISSING_BLOCKER,
)


def test_synthesize_parser_accepts_positional_capability_need(
    tmp_path: Path,
) -> None:
    """AC0 (parser): ``synthesize <need> --output <path>`` parses cleanly.

    Asserts the contract surfaced by the dispatcher's happy path:
    a freeform ``capability_need`` positional plus an ``--output``
    path, with the shared flags present and ``--json`` defaulting
    off. The optimizer round budget default is loaded from
    :data:`metacrucible.optimizer.ROUND_BUDGET_DEFAULT` so the
    shared flag defaulting mirrors :mod:`metacrucible.__main__`
    exactly.
    """
    from metacrucible.__main__ import _build_parser

    parser = _build_parser()
    output_path = tmp_path / "skill"
    args = parser.parse_args(
        [
            "synthesize",
            "write a skill",
            "--output",
            str(output_path),
        ]
    )

    assert args.command == "synthesize", (
        f"args.command must be 'synthesize'; got {args.command!r}"
    )
    assert args.capability_need == "write a skill", (
        f"positional capability_need must surface verbatim; "
        f"got {args.capability_need!r}"
    )
    assert args.from_spec is None, (
        f"--from must default to None when omitted; got {args.from_spec!r}"
    )
    assert args.output == str(output_path), (
        f"--output must surface the supplied path verbatim; "
        f"got {args.output!r}"
    )
    assert args.max_rounds == ROUND_BUDGET_DEFAULT, (
        f"--max-rounds must default to ROUND_BUDGET_DEFAULT "
        f"({ROUND_BUDGET_DEFAULT}); got {args.max_rounds!r}"
    )
    assert args.json is False, (
        f"--json must default to False; got {args.json!r}"
    )
    assert args.allow_routing_revision is False, (
        f"--allow-routing-revision must default to False on the "
        f"renamed dest args.allow_routing_revision; "
        f"got {args.allow_routing_revision!r}"
    )
    assert args.allow_dirty_unrelated is False, (
        f"--allow-dirty-unrelated must default to False on the "
        f"renamed dest args.allow_dirty_unrelated; "
        f"got {args.allow_dirty_unrelated!r}"
    )
    assert args.confirm_resume is False, (
        f"--confirm-resume must default to False on the "
        f"renamed dest args.confirm_resume; "
        f"got {args.confirm_resume!r}"
    )


def test_synthesize_parser_accepts_from_spec_with_json(tmp_path: Path) -> None:
    """AC0 (parser): ``synthesize --from <spec> --output <path> --json`` parses cleanly.

    Mirrors the test above but on the ``--from`` arm of the
    mutually-exclusive input group: ``capability_need`` is
    ``None`` because no positional was supplied, ``from_spec``
    carries the spec path, and ``--json`` flips on. The other
    shared flags keep their defaults.
    """
    from metacrucible.__main__ import _build_parser

    parser = _build_parser()
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec\n", encoding="utf-8")
    output_path = tmp_path / "skill"

    args = parser.parse_args(
        [
            "synthesize",
            "--from",
            str(spec_path),
            "--output",
            str(output_path),
            "--json",
        ]
    )

    assert args.command == "synthesize", (
        f"args.command must be 'synthesize'; got {args.command!r}"
    )
    assert args.capability_need is None, (
        f"capability_need must be None when --from is used; "
        f"got {args.capability_need!r}"
    )
    assert args.from_spec == str(spec_path), (
        f"--from must surface as args.from_spec with the supplied "
        f"path verbatim; got {args.from_spec!r}"
    )
    assert args.output == str(output_path), (
        f"--output must surface the supplied path verbatim; "
        f"got {args.output!r}"
    )
    assert args.json is True, (
        f"--json must flip on; got {args.json!r}"
    )


def test_synthesize_parser_rejects_missing_input_with_systemexit_2(
    tmp_path: Path,
) -> None:
    """AC0 (parser): omitting both ``capability_need`` and ``--from``
    must raise ``SystemExit(2)`` (argparse usage-error code) so
    automation sees a stable, distinguishable failure mode and
    the command never reaches the dispatcher (Issue #27 task 27.1).
    """
    from metacrucible.__main__ import _build_parser

    parser = _build_parser()
    output_path = tmp_path / "skill"

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["synthesize", "--output", str(output_path)])

    assert exc_info.value.code == 2, (
        f"missing both inputs must produce argparse usage-error "
        f"SystemExit(2); got code={exc_info.value.code!r}"
    )


def test_synthesize_parser_rejects_conflicting_input_with_systemexit_2(
    tmp_path: Path,
) -> None:
    """AC0 (parser): providing both ``capability_need`` and ``--from``
    must raise ``SystemExit(2)`` at the parser level (mutually
    exclusive input modes). The command never reaches the
    dispatcher and the operator sees a clean argparse error
    message.
    """
    from metacrucible.__main__ import _build_parser

    parser = _build_parser()
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec\n", encoding="utf-8")
    output_path = tmp_path / "skill"

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "synthesize",
                "need",
                "--from",
                str(spec_path),
                "--output",
                str(output_path),
            ]
        )

    assert exc_info.value.code == 2, (
        f"conflicting inputs must produce argparse usage-error "
        f"SystemExit(2); got code={exc_info.value.code!r}"
    )

def test_synthesize_parser_renamed_confirm_flags_flip_renamed_dests(
    tmp_path: Path,
) -> None:
    """AC0 (parser): the three confirmation flags surface on the
    snake_case namespace dests the dispatcher and downstream code
    read: ``--allow-routing-revision`` -> ``args.allow_routing_revision``,
    ``--allow-dirty-unrelated`` -> ``args.allow_dirty_unrelated``,
    ``--confirm-resume`` -> ``args.confirm_resume``. All three are
    ``store_true`` confirmations aligned with the ``optimize`` command
    and a single parse that flips them on must flip the renamed
    dests on. This closes the parser-contract gap left by the
    default-false assertions in the positional-need test (Finding 3
    of the Task 1 code-quality review).
    """
    from metacrucible.__main__ import _build_parser

    parser = _build_parser()
    output_path = tmp_path / "skill"
    args = parser.parse_args(
        [
            "synthesize",
            "write a skill",
            "--output",
            str(output_path),
            "--allow-routing-revision",
            "--allow-dirty-unrelated",
            "--confirm-resume",
        ]
    )

    assert args.allow_routing_revision is True, (
        f"--allow-routing-revision must flip on args.allow_routing_revision; "
        f"got {args.allow_routing_revision!r}"
    )
    assert args.allow_dirty_unrelated is True, (
        f"--allow-dirty-unrelated must flip on args.allow_dirty_unrelated; "
        f"got {args.allow_dirty_unrelated!r}"
    )
    assert args.confirm_resume is True, (
        f"--confirm-resume must flip on args.confirm_resume; "
        f"got {args.confirm_resume!r}"
    )



# --------------------------------------------------------------------------- #
# Task 2 command-level tests (Issue #41 / PRD F4)                            #
# --------------------------------------------------------------------------- #

#: Pinned ``now`` value used to freeze timestamps so the synthesized
#: case_ids, baseline hashes, and history events are byte-stable across
#: runs. The string is an ISO-8601 UTC instant with a ``Z`` suffix to
#: mirror :func:`metacrucible.__main__._now_iso` exactly.
FROZEN_NOW = "2026-06-17T00:00:00Z"

#: Frozen ``case_id`` values the inline-need happy path test asserts on.
#: Derived from the SHA-256 of ``("write a skill to summarize documents"\x00eval\x00{FROZEN_NOW}")``
#: and the held-out split, sliced to the first 16 hex chars.
_FROZEN_NEED = "write a skill to summarize documents"


def _synthesize_namespace(
    *,
    tmp_path: Path,
    capability_need: str | None,
    from_spec: str | None,
    json_mode: bool = True,
) -> argparse.Namespace:
    """Build the ``argparse.Namespace`` ``cmd_synthesize`` expects.

    Mirrors the dispatcher's parser output: a fully-populated
    namespace with every shared CLI flag wired so the dispatcher
    can read ``args.json`` and the wrapper can build the
    ``_emit`` partial without raising ``AttributeError``. The
    snake_case dests match the parser rename contract pinned
    by the parser-level tests above.
    """
    output = tmp_path / "skill"
    return argparse.Namespace(
        command="synthesize",
        capability_need=capability_need,
        from_spec=from_spec,
        output=str(output),
        max_rounds=ROUND_BUDGET_DEFAULT,
        json=json_mode,
        allow_routing_revision=False,
        allow_dirty_unrelated=False,
        confirm_resume=False,
    )


def _read_benchmark_records(path: Path) -> list[dict[str, object]]:
    """Read all parseable JSONL records from ``path``.

    Mirrors :func:`metacrucible.__main__._read_benchmark_records`
    in shape (skip blank lines, parse JSON) so the Task 2 tests
    can read the benchmark the pipeline wrote without depending
    on private ``__main__`` helpers.
    """
    records: list[dict[str, object]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        records.append(json.loads(raw))
    return records


def test_synthesize_inline_need_creates_draft_pending_review_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 (command, inline-need happy path):
    ``cmd_synthesize`` with a freeform ``capability_need`` and
    ``--json`` returns :data:`metacrucible.exit_codes.EXIT_OK`
    and emits a parseable JSON payload whose ``outcome`` is
    ``draft_pending_review``.

    The full Task 2 contract is asserted end-to-end:

      - return code is :data:`metacrucible.exit_codes.EXIT_OK`,
      - JSON ``status`` is ``"OK"`` and ``outcome`` is
        :data:`metacrucible.synthesize.SYNTHESIZE_DRAFT_PENDING_REVIEW`
        (``"draft_pending_review"``),
      - the draft artifact file exists under the workspace,
      - the envelope (``<workspace>/.metacrucible/envelope.json``)
        points at that artifact and declares
        ``source == "synthesize"``,
      - the benchmark (``<workspace>/benchmark.jsonl``) carries
        one ``case_eval`` (split=eval) and one ``case_held_out``
        (split=held_out) record, both with
        :data:`metacrucible.benchmark.STATUS_GENERATED` and
        :data:`metacrucible.synthesize.BOOTSTRAP_PENDING_REVIEW_FIELD`
        set to ``True``,
      - the state (``<workspace>/.metacrucible/state.json``)
        contains a ``baseline`` mapping with both artifact and
        benchmark hashes,
      - the history (``<workspace>/.metacrucible/history.jsonl``)
        contains the four synthesis events in the pinned order
        (``synthesis_started``, ``baseline_recorded``,
        ``generated_cases_created``, ``synthesis_pending_review``),
      - the ``sentinel`` field in the JSON payload is the literal
        :data:`metacrucible.synthesize.BOOTSTRAP_PENDING_REVIEW_FIELD`
        constant so downstream consumers see which sentinel is
        in effect.

    Time is frozen via ``monkeypatch.setattr`` on
    :mod:`metacrucible.__main__._now_iso` so the case_ids and
    history events are byte-stable across runs (the case_id is
    derived from a SHA-256 of ``need + split + now``).
    """
    from metacrucible import __main__ as cli_main

    monkeypatch.setattr(cli_main, "_now_iso", lambda: FROZEN_NOW)

    ns = _synthesize_namespace(
        tmp_path=tmp_path,
        capability_need=_FROZEN_NEED,
        from_spec=None,
    )
    rc = cli_main.cmd_synthesize(ns)
    captured = capsys.readouterr()

    assert rc == EXIT_OK, (
        f"inline-need synthesize must return EXIT_OK; got "
        f"rc={rc} stdout={captured.out!r} stderr={captured.err!r}"
    )
    payload = json.loads(captured.out)
    assert isinstance(payload, dict), (
        f"--json payload must be a JSON object; got "
        f"{type(payload).__name__} ({payload!r})"
    )
    assert payload.get("status") == "OK", (
        f"payload status must be 'OK'; got {payload.get('status')!r}"
    )
    assert (
        payload.get("outcome") == SYNTHESIZE_DRAFT_PENDING_REVIEW
    ), (
        f"payload outcome must be {SYNTHESIZE_DRAFT_PENDING_REVIEW!r}; "
        f"got {payload.get('outcome')!r}"
    )

    workspace = pathlib.Path(payload["workspace"])
    artifact_path = pathlib.Path(payload["artifact_path"])
    benchmark_path = pathlib.Path(payload["benchmark"])

    # Artifact file exists, is under the workspace, and carries
    # the verbatim capability need inside a ``# Capability Need``
    # section so a human reviewer can confirm the synthesis.
    assert artifact_path.is_file(), (
        f"artifact file must exist after a successful synthesize; "
        f"got artifact_path={artifact_path!r}"
    )
    assert artifact_path.parent == workspace, (
        f"artifact must live directly under the workspace; got "
        f"artifact={artifact_path!r} workspace={workspace!r}"
    )
    artifact_text = artifact_path.read_text(encoding="utf-8")
    assert _FROZEN_NEED in artifact_text, (
        f"artifact must contain the verbatim capability need; "
        f"need={_FROZEN_NEED!r} not in artifact"
    )
    assert "# Capability Need" in artifact_text, (
        f"artifact must carry a '# Capability Need' section; got "
        f"artifact text={artifact_text!r}"
    )
    assert artifact_text.endswith("\n"), (
        f"artifact must end with exactly one newline; got tail={artifact_text[-5:]!r}"
    )

    # Envelope: ``source == 'synthesize'`` + artifact path + need hash.
    envelope = json.loads(
        (workspace / ".metacrucible" / "envelope.json").read_text(
            encoding="utf-8"
        )
    )
    assert envelope.get("source") == "synthesize", (
        f"envelope must declare source='synthesize'; got "
        f"envelope={envelope!r}"
    )
    assert envelope.get("artifact_path") == str(artifact_path), (
        f"envelope.artifact_path must point at the draft artifact; "
        f"got envelope.artifact_path={envelope.get('artifact_path')!r} "
        f"artifact_path={str(artifact_path)!r}"
    )
    assert envelope.get("artifact_workspace") == str(workspace), (
        f"envelope.artifact_workspace must equal the workspace; got "
        f"{envelope.get('artifact_workspace')!r}"
    )
    assert isinstance(envelope.get("capability_need_hash"), str), (
        f"envelope must carry a capability_need_hash string; got "
        f"{envelope.get('capability_need_hash')!r}"
    )
    assert len(envelope["capability_need_hash"]) == 64, (
        f"capability_need_hash must be a SHA-256 hex digest (64 "
        f"chars); got {envelope['capability_need_hash']!r}"
    )

    # Benchmark: metadata + one eval case + one held-out case.
    records = _read_benchmark_records(benchmark_path)
    assert records[0]["record_type"] == "metadata", (
        f"benchmark[0] must be the metadata record; got "
        f"{records[0]!r}"
    )
    case_records = [r for r in records if r.get("record_type") != "metadata"]
    assert len(case_records) == 2, (
        f"synthesize must write exactly 2 generated cases (eval + "
        f"held-out); got {len(case_records)} case records"
    )
    eval_cases = [
        r for r in case_records if r.get("split") == SPLIT_EVAL
    ]
    held_out_cases = [
        r for r in case_records if r.get("split") == SPLIT_HELD_OUT
    ]
    assert len(eval_cases) == 1, (
        f"benchmark must have exactly 1 eval case; got {eval_cases!r}"
    )
    assert len(held_out_cases) == 1, (
        f"benchmark must have exactly 1 held-out case; got "
        f"{held_out_cases!r}"
    )
    for case in case_records:
        assert case["status"] == STATUS_GENERATED, (
            f"synthesized case must be status=generated; got "
            f"{case!r}"
        )
        assert case[BOOTSTRAP_PENDING_REVIEW_FIELD] is True, (
            f"synthesized case must carry the bootstrap-pending-review "
            f"sentinel; got case={case!r}"
        )
        assert case["reviewed"] is False, (
            f"synthesized case must be reviewed=False; got {case!r}"
        )
        assert case["checks"] == [], (
            f"synthesized case must have empty checks list; got "
            f"{case!r}"
        )
        assert case["judgment"] is None, (
            f"synthesized case must have judgment=None; got {case!r}"
        )
        cid = case["case_id"]
        assert isinstance(cid, str) and cid.startswith("synthesize-"), (
            f"synthesized case_id must be 'synthesize-<hex>'; got "
            f"{cid!r}"
        )
    assert eval_cases[0]["record_type"] == "case_eval", (
        f"eval case must carry record_type='case_eval'; got "
        f"{eval_cases[0]!r}"
    )
    assert held_out_cases[0]["record_type"] == "case_held_out", (
        f"held-out case must carry record_type='case_held_out'; got "
        f"{held_out_cases[0]!r}"
    )
    assert (
        eval_cases[0]["case_id"] != held_out_cases[0]["case_id"]
    ), (
        f"eval and held-out case_ids must be unique; got "
        f"eval={eval_cases[0]['case_id']!r} "
        f"held_out={held_out_cases[0]['case_id']!r}"
    )

    # State: default fields + a ``baseline`` mapping.
    state = json.loads(
        (workspace / ".metacrucible" / "state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state.get("current_best_revision") is None
    assert state.get("last_run_id") is None
    baseline = state.get("baseline")
    assert isinstance(baseline, dict), (
        f"state.baseline must be a dict; got {baseline!r}"
    )
    assert isinstance(baseline.get("artifact_hash"), str) and len(
        baseline["artifact_hash"]
    ) == 64, (
        f"state.baseline.artifact_hash must be a 64-char SHA-256 hex; "
        f"got {baseline.get('artifact_hash')!r}"
    )
    assert isinstance(baseline.get("benchmark_hash"), str) and len(
        baseline["benchmark_hash"]
    ) == 64, (
        f"state.baseline.benchmark_hash must be a 64-char SHA-256 hex; "
        f"got {baseline.get('benchmark_hash')!r}"
    )

    # History: the four synthesis events in the pinned order.
    history_records = [
        json.loads(line)
        for line in (workspace / ".metacrucible" / "history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    history_events = [r["event"] for r in history_records]
    assert history_events == [
        "synthesis_started",
        "baseline_recorded",
        "generated_cases_created",
        "synthesis_pending_review",
    ], (
        f"history must contain the four synthesis events in the "
        f"pinned order; got {history_events!r}"
    )

    # JSON payload cross-references.
    assert payload["sentinel"] == BOOTSTRAP_PENDING_REVIEW_FIELD, (
        f"payload sentinel must be the BOOTSTRAP_PENDING_REVIEW_FIELD "
        f"constant; got {payload.get('sentinel')!r}"
    )
    assert payload["blockers"] == [], (
        f"payload blockers must be empty on success; got "
        f"{payload.get('blockers')!r}"
    )
    assert payload["generated_case_ids"] == [
        eval_cases[0]["case_id"],
        held_out_cases[0]["case_id"],
    ], (
        f"payload generated_case_ids must list the eval case first "
        f"then the held-out case; got {payload.get('generated_case_ids')!r}"
    )
    assert (
        payload["baseline"]["artifact_hash"] == baseline["artifact_hash"]
    )
    assert (
        payload["baseline"]["benchmark_hash"] == baseline["benchmark_hash"]
    )


def test_synthesize_from_spec_creates_same_pending_review_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 (command, --from spec happy path):
    ``cmd_synthesize`` with a ``--from <spec>`` path returns
    :data:`metacrucible.exit_codes.EXIT_OK` and produces a
    workspace whose draft artifact contains the spec text and
    whose benchmark carries the same generated-case / sentinel
    contract as the inline-need path.

    The spec file is written to ``tmp_path / "spec.md"`` and
    read by the pipeline via :func:`metacrucible.synthesize.resolve_synthesize_input`
    with UTF-8. The draft artifact must contain the spec text
    verbatim (after stripping) so a human reviewer can confirm
    the spec round-tripped through the pipeline.
    """
    from metacrucible import __main__ as cli_main

    monkeypatch.setattr(cli_main, "_now_iso", lambda: FROZEN_NOW)

    spec_text = "summarize legal documents into a 5-bullet brief"
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(spec_text + "\n", encoding="utf-8")

    ns = _synthesize_namespace(
        tmp_path=tmp_path,
        capability_need=None,
        from_spec=str(spec_path),
    )
    rc = cli_main.cmd_synthesize(ns)
    captured = capsys.readouterr()

    assert rc == EXIT_OK, (
        f"--from spec synthesize must return EXIT_OK; got "
        f"rc={rc} stdout={captured.out!r} stderr={captured.err!r}"
    )
    payload = json.loads(captured.out)
    assert (
        payload.get("outcome") == SYNTHESIZE_DRAFT_PENDING_REVIEW
    ), (
        f"payload outcome must be {SYNTHESIZE_DRAFT_PENDING_REVIEW!r}; "
        f"got {payload.get('outcome')!r}"
    )

    workspace = pathlib.Path(payload["workspace"])
    artifact_path = pathlib.Path(payload["artifact_path"])
    artifact_text = artifact_path.read_text(encoding="utf-8")
    assert spec_text in artifact_text, (
        f"artifact must contain the verbatim spec text; spec="
        f"{spec_text!r} not in artifact"
    )

    # Same generated-case / sentinel contract as inline-need path.
    records = _read_benchmark_records(workspace / BENCHMARK_FILE_NAME)
    case_records = [
        r for r in records if r.get("record_type") != "metadata"
    ]
    assert len(case_records) == 2
    eval_cases = [
        r for r in case_records if r.get("split") == SPLIT_EVAL
    ]
    held_out_cases = [
        r for r in case_records if r.get("split") == SPLIT_HELD_OUT
    ]
    assert len(eval_cases) == 1 and len(held_out_cases) == 1
    for case in case_records:
        assert case["status"] == STATUS_GENERATED
        assert case[BOOTSTRAP_PENDING_REVIEW_FIELD] is True
        assert case["reviewed"] is False
    assert payload["sentinel"] == BOOTSTRAP_PENDING_REVIEW_FIELD


def test_synthesize_blocks_when_spec_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 (command, precondition): ``--from`` pointing at a
    non-existent path returns
    :data:`metacrucible.exit_codes.EXIT_BLOCKED` with the
    stable :data:`metacrucible.synthesize.SYNTHESIZE_SPEC_MISSING_BLOCKER`
    id and does NOT create the workspace.

    The blocker path must short-circuit BEFORE any filesystem
    mutation so a missing spec cannot leave a half-created
    workspace behind.
    """
    from metacrucible import __main__ as cli_main

    monkeypatch.setattr(cli_main, "_now_iso", lambda: FROZEN_NOW)

    spec_path = tmp_path / "missing-spec.md"
    assert not spec_path.exists(), (
        f"precondition: spec path must not exist before the test; "
        f"got {spec_path!r}"
    )
    output = tmp_path / "skill"
    assert not output.exists(), (
        f"precondition: output must not exist before the test; "
        f"got {output!r}"
    )

    ns = _synthesize_namespace(
        tmp_path=tmp_path,
        capability_need=None,
        from_spec=str(spec_path),
    )
    rc = cli_main.cmd_synthesize(ns)
    captured = capsys.readouterr()

    assert rc == EXIT_BLOCKED, (
        f"missing --from spec must return EXIT_BLOCKED; got "
        f"rc={rc} stdout={captured.out!r} stderr={captured.err!r}"
    )
    payload = json.loads(captured.out)
    assert payload.get("status") == "BLOCKED", (
        f"missing spec payload must be status=BLOCKED; got "
        f"{payload.get('status')!r}"
    )
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert SYNTHESIZE_SPEC_MISSING_BLOCKER in blocker_ids, (
        f"missing spec payload must carry the "
        f"{SYNTHESIZE_SPEC_MISSING_BLOCKER!r} blocker id; got "
        f"blocker_ids={blocker_ids!r}"
    )
    # No workspace written for a blocker path.
    assert not output.exists(), (
        f"output path must NOT be created when the precondition "
        f"blocks; got {output!r} (exists={output.exists()!r})"
    )
    assert payload.get("generated_case_ids") == [], (
        f"BLOCKED payload must have empty generated_case_ids; got "
        f"{payload.get('generated_case_ids')!r}"
    )


def test_synthesize_blocks_when_spec_file_empty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 (command, precondition): ``--from`` pointing at an
    empty (or whitespace-only) file returns
    :data:`metacrucible.exit_codes.EXIT_BLOCKED` with the
    stable :data:`metacrucible.synthesize.SYNTHESIZE_SPEC_EMPTY_BLOCKER`
    id and does NOT create the workspace.

    A whitespace-only file is also rejected (the pipeline
    strips the spec before checking emptiness so the operator
    cannot smuggle a no-op spec through with trailing
    newlines).
    """
    from metacrucible import __main__ as cli_main

    monkeypatch.setattr(cli_main, "_now_iso", lambda: FROZEN_NOW)

    spec_path = tmp_path / "empty-spec.md"
    spec_path.write_text("  \n\n  ", encoding="utf-8")
    output = tmp_path / "skill"
    assert not output.exists()

    ns = _synthesize_namespace(
        tmp_path=tmp_path,
        capability_need=None,
        from_spec=str(spec_path),
    )
    rc = cli_main.cmd_synthesize(ns)
    captured = capsys.readouterr()

    assert rc == EXIT_BLOCKED, (
        f"empty --from spec must return EXIT_BLOCKED; got "
        f"rc={rc} stdout={captured.out!r} stderr={captured.err!r}"
    )
    payload = json.loads(captured.out)
    assert payload.get("status") == "BLOCKED"
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert SYNTHESIZE_SPEC_EMPTY_BLOCKER in blocker_ids, (
        f"empty spec payload must carry the "
        f"{SYNTHESIZE_SPEC_EMPTY_BLOCKER!r} blocker id; got "
        f"blocker_ids={blocker_ids!r}"
    )
    assert not output.exists(), (
        f"output path must NOT be created when the precondition "
        f"blocks; got {output!r}"
    )


def test_synthesize_blocks_when_output_exists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 (command, precondition): an existing ``--output`` path
    (directory or file) returns
    :data:`metacrucible.exit_codes.EXIT_BLOCKED` with the
    stable :data:`metacrucible.synthesize.SYNTHESIZE_OUTPUT_EXISTS_BLOCKER`
    id and does NOT mutate the existing path.

    The pipeline refuses to clobber an existing workspace or
    file (per the ``init``-style idempotency contract); the
    operator must remove or rename the target before
    re-running synthesize. The test pins the contract for
    both the directory-already-exists and the file-already-
    exists shapes.
    """
    from metacrucible import __main__ as cli_main

    monkeypatch.setattr(cli_main, "_now_iso", lambda: FROZEN_NOW)

    output = tmp_path / "skill"
    output.mkdir(parents=True)
    existing_file = output / "preexisting.txt"
    existing_file.write_text("do not clobber\n", encoding="utf-8")

    ns = _synthesize_namespace(
        tmp_path=tmp_path,
        capability_need=_FROZEN_NEED,
        from_spec=None,
    )
    rc = cli_main.cmd_synthesize(ns)
    captured = capsys.readouterr()

    assert rc == EXIT_BLOCKED, (
        f"existing --output must return EXIT_BLOCKED; got "
        f"rc={rc} stdout={captured.out!r} stderr={captured.err!r}"
    )
    payload = json.loads(captured.out)
    assert payload.get("status") == "BLOCKED"
    blocker_ids = [
        b.get("id") for b in payload.get("blockers", [])
        if isinstance(b, dict)
    ]
    assert SYNTHESIZE_OUTPUT_EXISTS_BLOCKER in blocker_ids, (
        f"existing output payload must carry the "
        f"{SYNTHESIZE_OUTPUT_EXISTS_BLOCKER!r} blocker id; got "
        f"blocker_ids={blocker_ids!r}"
    )
    # The pre-existing file inside the output dir is preserved.
    assert existing_file.is_file(), (
        f"pre-existing file must be preserved when the precondition "
        f"blocks; got {existing_file!r}"
    )
    # No new artifact was written under the output path.
    assert not (output / "synthesized-skill.md").exists(), (
        f"no draft artifact must be written when output already "
        f"exists; got {(output / 'synthesized-skill.md')!r}"
    )
    assert not (output / ".metacrucible").exists(), (
        f"no .metacrucible/ must be created when output already "
        f"exists; got {(output / '.metacrucible')!r}"
    )
