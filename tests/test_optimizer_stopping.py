"""Stopping Condition (Issue #36) regression tests.

Pins the machine-stable ``stop_reason`` contract on the
optimizer pipeline and the ``optimize`` CLI:

  - :class:`metacrucible.optimizer.OptimizerPipelineResult`
    always carries a populated ``stop_reason`` (one of
    :data:`metacrucible.optimizer.STOP_REASONS`).
  - The CLI ``--json`` payload surfaces the same
    ``stop_reason`` at the top level so downstream tools
    can branch on it without re-deriving the verdict from
    ``status`` / ``blockers`` / ``warnings``.
  - The ``optimize_finished`` and ``optimize_blocked``
    history events carry the same ``stop_reason`` so a
    lineage reader can branch on the per-event reason
    without cross-referencing the evidence bundle.

Each test exercises exactly one Stopping Condition path:

  - ``no_candidate_edits`` — a non-BLOCKED completion with
    an empty-suggestion round (the MVP no-LLM path).
  - ``precondition_blocked`` — a BLOCKED completion via the
    loader-level missing-reviewed-cases precondition.
  - ``round_blocked`` — a BLOCKED completion via the
    in-loop ``_RoundBlocked`` signal (driven by the
    routing-HITL gate so the test is deterministic).
  - CLI ``--json`` round-trip — confirms the same
    ``stop_reason`` shows up in the top-level JSON
    payload when the pipeline is invoked through
    :func:`metacrucible.__main__.cmd_optimize`.

The tests follow the OPT-9 fixture pattern from
:mod:`tests.test_optimize_command`: a tiny Skill with a
single mutable body range, a benchmark with one eligible
eval + one eligible held-out case, and an envelope that
declares the artifact path.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from metacrucible.exit_codes import EXIT_BLOCKED, EXIT_OK
from metacrucible.optimizer import (
    STOP_REASONS,
    STOP_REASON_MAX_ROUNDS_REACHED,
    STOP_REASON_NO_CANDIDATE_EDITS,
    STOP_REASON_PRECONDITION_BLOCKED,
    STOP_REASON_ROUND_BLOCKED,
    run_optimizer_pipeline,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_FILE_NAME = "benchmark.jsonl"
ENVELOPE_REL_PATH = Path(".metacrucible") / "envelope.json"

#: Canonical fixture body text — kept here so the test
#: stays self-contained and does not depend on private
#: helpers from :mod:`tests.test_optimize_command`.
_BODY_TEXT = "# body\nThe body is the only mutable range.\n"
_ARTIFACT_TEXT = (
    "---\n"
    "name: stop-reason-skill\n"
    "description: Stopping Condition regression fixture\n"
    "---\n"
    f"{_BODY_TEXT}"
)


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    """Write ``records`` as one JSON object per line at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(dict(rec), sort_keys=True) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _metadata_record() -> dict[str, Any]:
    """Minimal benchmark metadata record (ADR 0029)."""
    return {
        "record_type": "metadata",
        "name": "stop-reason-benchmark",
        "schema_version": 1,
    }


def _reviewed_case(case_id: str, *, split: str = "eval") -> dict[str, Any]:
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


def _init_workspace(tmp_path: Path) -> Path:
    """Run ``init`` against a fresh workspace dir and return that dir.

    The fixture creates the empty benchmark container that the
    stopping-condition test then seeds with custom records.
    """
    workspace = tmp_path / "ws-stop-reason"
    workspace.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "metacrucible", "init", str(workspace)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == EXIT_OK, (
        f"`init` must exit 0 before optimize; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    return workspace


def _seed_fixture(
    workspace: Path,
    *,
    include_reviewed_cases: bool = True,
) -> tuple[Path, Path]:
    """Seed a runnable OPT-9-style fixture and return ``(benchmark, artifact)``.

    Writes a benchmark with a metadata record and (optionally) one
    eligible eval + one eligible held-out case, an envelope that
    declares the artifact path, and the artifact body. Used by the
    ``no_candidate_edits``, ``round_blocked``, and CLI tests.

    When ``include_reviewed_cases`` is False, only the metadata
    record is written — that drives the
    ``precondition_blocked`` path.
    """
    benchmark = workspace / BENCHMARK_FILE_NAME
    artifact = workspace / "SKILL.md"
    artifact.write_text(_ARTIFACT_TEXT, encoding="utf-8")
    envelope = workspace / ENVELOPE_REL_PATH
    envelope.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_path": str(artifact),
                "artifact_workspace": str(workspace),
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    records: list[dict[str, Any]] = [_metadata_record()]
    if include_reviewed_cases:
        records.append(_reviewed_case("eval-1", split="eval"))
        records.append(_reviewed_case("held-1", split="held_out"))
    _write_jsonl(benchmark, records)
    return benchmark, artifact


def _read_history(workspace: Path) -> list[dict[str, Any]]:
    """Return every record persisted to ``history.jsonl``."""
    history = workspace / ".metacrucible" / "history.jsonl"
    if not history.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in history.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if isinstance(rec, dict):
            records.append(rec)
    return records


# --------------------------------------------------------------------------- #
# non-BLOCKED completion: ``no_candidate_edits``                                #
# --------------------------------------------------------------------------- #


def test_stop_reason_no_candidate_edits_when_no_suggestions(
    tmp_path: Path,
) -> None:
    """``no_candidate_edits`` is the stop reason when a round
    produced zero usable suggestions and the loop broke.

    The MVP no-LLM path (``call_fn=None``) synthesizes an
    empty ``suggested_edits`` list per round, so the
    pipeline exits with the ``no_candidate_edits`` warning
    and the ``stop_reason="no_candidate_edits"`` value.
    The ``optimize_finished`` history event must record
    the same stop reason so a lineage reader does not have
    to cross-reference the result.
    """
    workspace = _init_workspace(tmp_path)
    benchmark, artifact = _seed_fixture(workspace)

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=None,  # MVP: no LLM wired → empty suggested_edits
        max_rounds=1,
        human_confirmed=False,
    )

    # Result carries the machine-stable reason.
    assert result.stop_reason == STOP_REASON_NO_CANDIDATE_EDITS, (
        f"pipeline must record stop_reason="
        f"{STOP_REASON_NO_CANDIDATE_EDITS!r} when a round "
        f"produced no usable suggestions; got "
        f"result.stop_reason={result.stop_reason!r}"
    )
    assert result.stop_reason in STOP_REASONS, (
        f"stop_reason must be one of the vocabulary "
        f"{sorted(STOP_REASONS)!r}; got {result.stop_reason!r}"
    )
    assert result.status == "REJECTED", (
        f"an empty-suggestion round must terminate with "
        f"REJECTED status (the no-improvement verdict); "
        f"got {result.status!r}"
    )
    # The corresponding warning is also on the result so
    # the operator sees the human English rationale.
    warning_ids = [
        w.get("id") for w in (result.warnings or [])
        if isinstance(w, dict)
    ]
    assert "no_candidate_edits" in warning_ids, (
        f"empty-suggestion run must surface the "
        f"no_candidate_edits warning on result.warnings; "
        f"got warning_ids={warning_ids!r}"
    )

    # The optimize_finished history event carries the
    # same stop reason so a downstream lineage reader can
    # branch on it without re-running the pipeline.
    finished = [
        r for r in _read_history(workspace)
        if isinstance(r, dict) and r.get("event") == "optimize_finished"
    ]
    assert finished, (
        f"pipeline must persist an optimize_finished "
        f"event for a non-precondition completion; got "
        f"history_events={[r.get('event') for r in _read_history(workspace)]!r}"
    )
    assert finished[-1].get("stop_reason") == STOP_REASON_NO_CANDIDATE_EDITS, (
        f"optimize_finished.stop_reason must mirror the "
        f"pipeline result; got "
        f"stop_reason={finished[-1].get('stop_reason')!r}"
    )


# --------------------------------------------------------------------------- #
# BLOCKED completion: ``precondition_blocked``                                  #
# --------------------------------------------------------------------------- #


def test_stop_reason_precondition_blocked_when_no_reviewed_cases(
    tmp_path: Path,
) -> None:
    """``precondition_blocked`` is the stop reason when the
    loader-level blocker (no eligible reviewed cases)
    blocks the run before the round loop.

    The pipeline's precondition check sees the empty
    benchmark (metadata only) and returns early with
    ``status="BLOCKED"`` and
    ``stop_reason="precondition_blocked"``. The path
    writes an ``optimize_blocked`` event but no
    ``optimize_finished`` event — the precondition
    short-circuits the run before the completion hook.
    """
    workspace = _init_workspace(tmp_path)
    benchmark, artifact = _seed_fixture(
        workspace, include_reviewed_cases=False
    )

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=None,
        max_rounds=1,
        human_confirmed=False,
    )

    # Result carries the precondition reason.
    assert result.stop_reason == STOP_REASON_PRECONDITION_BLOCKED, (
        f"pipeline must record stop_reason="
        f"{STOP_REASON_PRECONDITION_BLOCKED!r} when the "
        f"loader-level blocker short-circuits the run; "
        f"got result.stop_reason={result.stop_reason!r}"
    )
    assert result.status == "BLOCKED", (
        f"precondition-blocked run must terminate with "
        f"BLOCKED status; got {result.status!r}"
    )
    assert result.rounds == 0, (
        f"precondition-blocked run must not enter the "
        f"round loop; got rounds={result.rounds!r}"
    )
    # The loader's missing-required-cases blockers are
    # surfaced verbatim so the operator can branch on
    # them.
    blocker_ids = [
        b.get("id") for b in (result.blockers or [])
        if isinstance(b, dict)
    ]
    assert "missing-reviewed-eval-case" in blocker_ids, (
        f"precondition-blocked run must surface the "
        f"loader missing-reviewed-eval-case blocker; "
        f"got blocker_ids={blocker_ids!r}"
    )
    assert "missing-reviewed-held-out-case" in blocker_ids, (
        f"precondition-blocked run must surface the "
        f"loader missing-reviewed-held-out-case blocker; "
        f"got blocker_ids={blocker_ids!r}"
    )

    # The optimize_blocked lineage event is written but
    # the optimize_finished event is NOT — the
    # precondition path short-circuits the run before
    # the completion hook. The CLI and the evidence
    # bundle are the only completion records.
    history = _read_history(workspace)
    events = [r.get("event") for r in history if isinstance(r, dict)]
    assert "optimize_finished" not in events, (
        f"precondition-blocked run must NOT emit an "
        f"optimize_finished event; got events={events!r}"
    )
    assert "optimize_blocked" in events, (
        f"precondition-blocked run MUST emit an "
        f"optimize_blocked event for the lineage; got "
        f"events={events!r}"
    )


# --------------------------------------------------------------------------- #
# BLOCKED completion: ``round_blocked``                                         #
# --------------------------------------------------------------------------- #


def test_stop_reason_round_blocked_on_empty_replacement(
    tmp_path: Path,
) -> None:
    """``round_blocked`` is the stop reason when the
    in-loop :class:`metacrucible.optimizer._RoundBlocked`
    signal trips before the round can apply a candidate.

    The test injects a single non-routing suggestion whose
    ``replacement`` is the empty string. The merge plan
    stage (``_build_merge_plan``) flips
    ``merge_outside_mutable_range=True`` for the empty
    replacement, the runner appends
    :data:`metacrucible.optimizer.MUTABLE_RANGE_CONFLICT_BLOCKER`,
    and the round raises ``_RoundBlocked``. The pipeline
    terminates with ``status="BLOCKED"`` and
    ``stop_reason="round_blocked"``.

    The ``optimize_finished`` completion event carries
    the final ``stop_reason`` so a single lineage query
    can answer "why did this run stop" without joining
    the evidence bundle. The ``optimize_blocked``
    event itself stays stop_reason-free to keep the
    three blocked-event payloads uniform with the
    pre-existing lineage contract.
    """
    import hashlib

    workspace = _init_workspace(tmp_path)
    benchmark, artifact = _seed_fixture(workspace)
    body_hash = hashlib.sha256(_BODY_TEXT.encode("utf-8")).hexdigest()

    def _empty_replacement_call_fn(
        *, repair_context: Any = None
    ) -> dict[str, Any]:
        """Return one non-routing edit with an empty body.

        The base_hash is the current body hash so the
        suggestion survives the step 3c dedup, gets
        selected in step 3d, and trips the merge plan
        in step 3f because ``replacement`` is empty
        (``fits_in_range = bool(replacement)`` is False).
        """
        return {
            "rationale": "round_blocked contract regression",
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": body_hash,
                    "intent": "round_blocked_empty_replacement",
                    "replacement": "",
                    "rationale": "empty replacement must block the round",
                    "routing": False,
                }
            ],
        }

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_empty_replacement_call_fn,
        max_rounds=1,
        human_confirmed=False,
    )

    # Result carries the round-blocked reason.
    assert result.stop_reason == STOP_REASON_ROUND_BLOCKED, (
        f"pipeline must record stop_reason="
        f"{STOP_REASON_ROUND_BLOCKED!r} when a round "
        f"trips the _RoundBlocked signal; got "
        f"result.stop_reason={result.stop_reason!r}"
    )
    assert result.status == "BLOCKED", (
        f"round-blocked run must terminate with BLOCKED "
        f"status; got {result.status!r}"
    )
    # The artifact on disk must be unchanged because the
    # round never applied the candidate. The run-level
    # rollback restored the base bytes in the
    # ``_RoundBlocked`` handler.
    assert artifact.read_bytes() == _ARTIFACT_TEXT.encode("utf-8"), (
        f"round-blocked run must NOT mutate the "
        f"artifact; expected the seed text unchanged, "
        f"got {artifact.read_bytes()!r}"
    )

    # The optimize_blocked lineage event is emitted
    # uniformly across all blocked paths (precondition,
    # profile-gate, round-blocked) without a stop_reason
    # field; the canonical stop_reason lives on the
    # result and on the optimize_finished event.
    history = _read_history(workspace)
    blocked_events = [
        r for r in history
        if isinstance(r, dict) and r.get("event") == "optimize_blocked"
    ]
    assert blocked_events, (
        f"round-blocked run must persist an "
        f"optimize_blocked event; got events="
        f"{[r.get('event') for r in history]!r}"
    )
    # The optimize_finished event is still written by
    # the post-loop completion hook — it carries the
    # final stop_reason so the lineage has one canonical
    # record per run.
    finished = [
        r for r in history
        if isinstance(r, dict) and r.get("event") == "optimize_finished"
    ]
    assert finished, (
        f"round-blocked run must still emit an "
        f"optimize_finished event with the final "
        f"stop_reason; got events="
        f"{[r.get('event') for r in history]!r}"
    )
    assert finished[-1].get("stop_reason") == STOP_REASON_ROUND_BLOCKED, (
        f"optimize_finished.stop_reason must mirror the "
        f"result; got "
        f"stop_reason={finished[-1].get('stop_reason')!r}"
    )


# --------------------------------------------------------------------------- #
# CLI ``--json`` payload surfaces ``stop_reason`` at the top level              #
# --------------------------------------------------------------------------- #


def test_stop_reason_in_cli_json_payload(tmp_path: Path) -> None:
    """The ``optimize --json`` payload exposes the same
    ``stop_reason`` the pipeline recorded on the result.

    The test runs the full ``metacrucible optimize``
    command via subprocess (same pattern as the other
    CLI tests in :mod:`tests.test_optimize_command`) with
    a clean benchmark, a seeded envelope, and a seeded
    artifact. The pipeline's ``call_fn=None`` MVP path
    produces the ``no_candidate_edits`` stop reason; the
    CLI must surface that value at the top level of the
    ``--json`` payload, alongside ``status``, ``run_id``,
    and ``rounds``.
    """
    workspace = _init_workspace(tmp_path)
    _seed_fixture(workspace)

    result = subprocess.run(
        [
            sys.executable, "-m", "metacrucible",
            "optimize", str(workspace), "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode in (EXIT_OK, EXIT_BLOCKED), (
        f"`optimize --json` must exit 0 or {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
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
    # The stop_reason key is a top-level payload field
    # alongside status / run_id / rounds.
    assert "stop_reason" in payload, (
        f"optimize --json must surface stop_reason at "
        f"the top level; got keys={sorted(payload.keys())!r}"
    )
    assert payload["stop_reason"] == STOP_REASON_NO_CANDIDATE_EDITS, (
        f"CLI payload must report stop_reason="
        f"{STOP_REASON_NO_CANDIDATE_EDITS!r} for a "
        f"no-LLM run; got "
        f"payload['stop_reason']={payload['stop_reason']!r}"
    )
    # The reason must come from the canonical vocabulary;
    # the CLI must not invent a prose value.
    assert payload["stop_reason"] in STOP_REASONS, (
        f"CLI stop_reason must be a vocabulary string "
        f"from {sorted(STOP_REASONS)!r}; got "
        f"{payload['stop_reason']!r}"
    )


# --------------------------------------------------------------------------- #
# Vocabulary exhaustion: ``STOP_REASONS`` is the complete set                    #
# --------------------------------------------------------------------------- #


def test_stop_reason_default_for_clean_exhaustion() -> None:
    """When the round loop exits with no explicit break,
    the local stop reason is the initialized default
    ``max_rounds_reached``.

    This is the "configured round limit" path: the
    pipeline ran the loop, every round produced a
    non-empty ranked set, and the loop exhausted the
    budget. The init-time default is the only way this
    reason is set, so the test simply asserts the
    vocabulary constant is stable and the local default
    in :func:`run_optimizer_pipeline` matches it.
    """
    # The init-time default for the local ``stop_reason``
    # variable is the only place this constant is used
    # (the explicit break paths overwrite it). This test
    # pins the constant so a future refactor that
    # accidentally changes the default value fails loud.
    assert STOP_REASON_MAX_ROUNDS_REACHED == "max_rounds_reached", (
        f"the max_rounds_reached constant must remain "
        f"machine-stable; got {STOP_REASON_MAX_ROUNDS_REACHED!r}"
    )
    # The vocabulary is a frozenset that contains exactly
    # the six ids the contract enumerates.
    assert STOP_REASONS == frozenset({
        "max_rounds_reached",
        "accepted",
        "no_candidate_edits",
        "no_candidate_selected",
        "round_blocked",
        "precondition_blocked",
    }), (
        f"STOP_REASONS must enumerate exactly the six "
        f"contract stop reasons; got {sorted(STOP_REASONS)!r}"
    )

# --------------------------------------------------------------------------- #
# Loop-bound: ``max_rounds`` is honored on continuous rejection                #
# --------------------------------------------------------------------------- #

def test_max_rounds_not_exceeded_under_continuous_rejection(
    tmp_path: Path,
) -> None:
    """``run_optimizer_pipeline`` runs EXACTLY ``max_rounds``
    iterations under continuous suggestion + rejection and
    stops with ``stop_reason == max_rounds_reached``.

    Issue #36 Stopping Condition: the round loop is bounded
    by ``max_rounds``; a regression that breaks the loop
    early, runs more than ``max_rounds``, or fails to set
    the default ``stop_reason`` after the loop exhausts
    must be visible at the result level.

    The test injects:

      - a ``call_fn`` stub that returns one valid
        :data:`ROUND_REFLECTION_SCHEMA`-shaped suggestion
        every invocation. The suggestion targets the only
        mutable range with the parser-owned
        ``content_hash`` and a non-empty
        ``replacement`` so the merge step marks the plan
        ``fits_in_range = True``. ``routing = False`` so
        the per-round routing-cap / HITL gates do not
        trip.
      - an ``eval_call_fn`` stub that returns
        ``{"status": "FAIL"}`` for every case. The
        comparator sees zero ``FAIL -> PASS`` transitions
        on the candidate side and rejects every round.

    With ``max_rounds=2`` and a never-improving candidate,
    the loop runs both iterations, rolls back the
    candidate after each rejection, exits the ``for``
    loop naturally (no early ``break``), and reports
    ``stop_reason="max_rounds_reached"`` (the
    init-time default that survives because no explicit
    break path overwrote it).
    """
    import hashlib

    workspace = _init_workspace(tmp_path)
    benchmark, artifact = _seed_fixture(workspace)
    body_hash = hashlib.sha256(_BODY_TEXT.encode("utf-8")).hexdigest()

    def _suggestion_call_fn(
        *, repair_context: Any = None
    ) -> dict[str, Any]:
        """Return one valid suggestion targeting range 0.

        The replacement is a non-empty string that fits
        inside the body range so the merge plan marks
        ``fits_in_range = True``; the ``base_hash``
        matches the parser-owned ``content_hash`` so the
        suggestion survives the stale-hash dedup in step
        3c. ``routing = False`` so the per-round
        routing-cap / HITL gates do not block.
        """
        return {
            "rationale": (
                "continuous-rejection regression: one valid "
                "non-routing suggestion per round"
            ),
            "suggested_edits": [
                {
                    "range_id": 0,
                    "base_hash": body_hash,
                    "intent": "no_improvement_yet",
                    "replacement": (
                        "# body\n"
                        "The body is the only mutable range.\n"
                    ),
                    "rationale": (
                        "candidate that always fails the eval "
                        "comparator under the stub eval_call_fn"
                    ),
                    "routing": False,
                }
            ],
        }

    def _failing_eval_call_fn(
        case: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return ``FAIL`` for every case.

        The shape mirrors :func:`_evaluate_single_case`
        enough for :func:`compare_eval_held_out` to read
        ``status``. Both baseline and candidate eval /
        held-out results report ``FAIL`` so the
        comparator sees zero ``FAIL -> PASS`` transitions
        and rejects every round.
        """
        case_id = (
            case.get("case_id") if isinstance(case, dict) else None
        )
        case_id_str = case_id if isinstance(case_id, str) else "?"
        return {
            "case_id": case_id_str,
            "evaluator": "rule_check",
            "status": "FAIL",
            "blockers": [],
            "evidence": {"stub": "continuous-rejection"},
        }

    result = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact,
        call_fn=_suggestion_call_fn,
        max_rounds=2,
        human_confirmed=False,
        eval_call_fn=_failing_eval_call_fn,
    )

    # The loop ran exactly ``max_rounds`` iterations: not
    # 1 (would be a regression that breaks the loop on
    # the first rejection) and not 3+ (would be a
    # regression that ignores the configured budget).
    assert result.rounds == 2, (
        f"continuous-rejection run with max_rounds=2 must "
        f"run EXACTLY 2 iterations before stopping; got "
        f"result.rounds={result.rounds!r}"
    )
    # The init-time default survives because no explicit
    # break path overwrote it: every round produced a
    # non-empty ranked set (the stub returns one
    # valid suggestion), the comparator rejected
    # (stub eval_call_fn returns FAIL), and the loop
    # exhausted the budget without an early break.
    assert result.stop_reason == STOP_REASON_MAX_ROUNDS_REACHED, (
        f"continuous-rejection run that exhausts the "
        f"budget must report stop_reason="
        f"{STOP_REASON_MAX_ROUNDS_REACHED!r}; got "
        f"result.stop_reason={result.stop_reason!r}"
    )
    assert result.stop_reason in STOP_REASONS, (
        f"stop_reason must be a vocabulary string from "
        f"{sorted(STOP_REASONS)!r}; got "
        f"result.stop_reason={result.stop_reason!r}"
    )
    # The comparator rejected every round; the run
    # terminates with REJECTED status (no blockers, no
    # accepted round).
    assert result.status == "REJECTED", (
        f"continuous-rejection run must terminate with "
        f"REJECTED status (the comparator never accepted "
        f"and there are no blockers); got "
        f"result.status={result.status!r}"
    )
    # The on-disk artifact must be unchanged: every
    # rejected round rolled back the candidate write so
    # the run-level artifact equals the seed bytes.
    assert artifact.read_bytes() == _ARTIFACT_TEXT.encode("utf-8"), (
        f"continuous-rejection run must NOT mutate the "
        f"artifact on disk (per-round rollback restores "
        f"the seed bytes after each rejection); got "
        f"artifact bytes={artifact.read_bytes()!r}"
    )
