"""Console entrypoint for the ``metacrucible`` command.

Exposes :func:`main` as the ``metacrucible`` console script (declared
in ``pyproject.toml`` under ``[project.scripts]``) and is also invokable
as ``python -m metacrucible``. This module owns the CLI surface:

  - the skeleton flags (``--help`` / ``--version``) from Issue #3,
  - the ``init`` subcommand from Issue #6, which creates the
    per-artifact ``.metacrucible/`` envelope/state plus an empty
    ``benchmark.jsonl`` container at the workspace root, and which
    exposes ``--check`` for a post-init validation pass that surfaces
    the ``missing-reviewed-case`` blocker (ADR 0029) on an empty
    benchmark, and
  - the ``review`` subcommand from Issue #29 (PRD F1): a one-shot
    diagnostic that runs Static Review (Darwin 9-dimension rubric)
    against a capability artifact and conditionally runs Execution
    Evaluation when a reviewed Benchmark is present at the workspace
    root. The artifact on disk is never mutated; the source bytes are
    read once and the evidence bundle is written to the user-global
    store (ADR 0016 / ADR 0030).

The remaining MVP subcommands from ADR 0035 (``bootstrap``,
``optimize``, ``synthesize``, ``inspect``, ``baseline create``,
``evaluate``) land in later waves per ``docs/roadmap.md``.

Exit codes
----------

The exact integer returned by :func:`main` is pinned by
:mod:`metacrucible.exit_codes`` so scripts and CI can branch on it
without re-deriving the matrix:

  - ``0`` — success.
  - ``1`` — argparse usage error (unknown subcommand, missing
    required positional/flag, or invalid argument).
  - ``2`` — semantic blocker (the command ran, but a precondition
    prevented the requested outcome).
  - ``3`` — uncaught exception past the command dispatcher; an
    English error message is written to stderr first.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import uuid
from .optimizer import (
    ROUND_BUDGET_DEFAULT,
    ROUTING_CAP_EXCEEDED_BLOCKER,
    ROUTING_HITL_UNCONFIRMED_BLOCKER,
    SCHEMA_VALIDATION_BLOCKED,
    STALE_BASE_HASH_BLOCKER,
    MUTABLE_RANGE_CONFLICT_BLOCKER,
    OptimizerContext,
    OptimizerPipelineResult,
    build_optimizer_context,
    run_optimizer_pipeline,
)
import subprocess
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .benchmark import SPLIT_EVAL, SPLIT_HELD_OUT, STATUS_GENERATED
from .blocked_bundles import write_blocked_bundle
from .dirty_guard import git_dirty_check
from .exit_codes import (
    EXIT_BLOCKED,
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_USER_ERROR,
)
from .promote import _atomic_write_jsonl, promote_case
from .storage import RepositoryStorage, UserGlobalStorage
from . import rule_checks as _rule_checks

__all__ = [
    "main",
    "NO_REVIEWED_BENCHMARK_WARNING",
    "REVIEW_RUN_TYPE",
]

#: Name of the benchmark container at the workspace root. ADR 0025
#: pins the empty benchmark as a valid container; the loader
#: (Issue #7) reads this path by convention.
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Stable blocker id emitted by ``init --check`` when the benchmark
#: has no reviewed cases. Pinned by ADR 0029's "fixed small
#: machine-stable set" of invalid benchmark blocker codes.
MISSING_REVIEWED_CASE_BLOCKER = "missing-reviewed-case"

#: Stable blocker id emitted by ``bootstrap`` when the workspace
#: does not have a benchmark container yet. The bootstrap
#: command (PRD F2) requires the empty container to exist so it
#: has a stable on-disk target. ``init`` creates the file; the
#: bootstrap contract is "init first, then bootstrap".
BOOTSTRAP_MISSING_BENCHMARK_BLOCKER = "bootstrap-missing-benchmark"

#: Stable blocker id emitted by ``bootstrap`` when ``--case-count``
#: is a non-positive integer. The command always writes at least
#: one draft case; a zero / negative count is a user error, not a
#: semantic precondition, but the CLI maps the rejection through
#: the same stable exit code so callers can branch on a single
#: signal.
BOOTSTRAP_INVALID_CASE_COUNT_BLOCKER = "bootstrap-invalid-case-count"

#: Stable blocker id emitted by ``optimize`` when a fully-runnable
#: benchmark (eligible reviewed eval + held-out cases, no
#: generated/sentinel cases) is presented. Full optimization is
#: W3 per the PRD; the MVP optimize command is a sentinel gate
#: that refuses to start with a stable blocker id so the
#: contract is "we will surface a clear reason" rather than
#: "we silently do nothing".
OPTIMIZE_NOT_IMPLEMENTED_BLOCKER = "optimize-not-implemented"

#: Stable blocker id emitted by ``optimize`` when the workspace
#: is inside a git worktree and ``git status --porcelain`` reports
#: dirty files that are not the tracked optimize inputs (the
#: artifact, ``.metacrucible/envelope.json``, and
#: ``benchmark.jsonl``). The caller can pass
#: ``--allow-dirty-unrelated`` to record the dirty file list and
#: proceed (Issue #31 dirty-file guard; mirrors
#: :data:`BASELINE_UNRELATED_DIRTY_FILES_BLOCKER`).
OPTIMIZE_UNRELATED_DIRTY_FILES_BLOCKER = "optimize-unrelated-dirty-files"

# --------------------------------------------------------------------------- #
# Issue #29 (PRD F1 ``review``) constants                                     #
# --------------------------------------------------------------------------- #

#: Stable warning id emitted by ``review`` when no reviewed Benchmark
#: is found at the workspace root. Pinned by F1 acceptance: "Execution
#: Evaluation was skipped because no reviewed Benchmark was present;
#: Static Review still completes." The id is distinct from
#: :data:`MISSING_REVIEWED_CASE_BLOCKER` because the review flow does
#: not own a benchmark container — a missing file is "no reviewed
#: benchmark", not "missing reviewed case in a present file".
NO_REVIEWED_BENCHMARK_WARNING = "no-reviewed-benchmark"

#: Run-type value written into the receipt of a ``review`` run.
#: Pinned to make ``review`` evidence bundles distinguishable from
#: the older ``init --review`` tracer-bullet bundles (run_type
#: ``"init-review"``). Downstream tooling branches on the value.
REVIEW_RUN_TYPE = "review"

#: Run-type for the ``init --review`` tracer-bullet bundles (kept
#: stable so the existing init --review tests still pass and the
#: receipt lineage is preserved).
INIT_REVIEW_RUN_TYPE = "init-review"

#: Stable reason code for ``execution_evaluation.skipped_reason``
#: when the review flow has no reviewed Benchmark to evaluate.
EXECUTION_SKIPPED_NO_REVIEWED_BENCHMARK = "no-reviewed-benchmark"

#: Stable reason code for ``execution_evaluation.skipped_reason``
#: when a benchmark file is present but the loader reports
#: blockers (e.g. ``schema-version-mismatch``). A present-but-
#: invalid benchmark is not the same as "no benchmark": the
#: loader-supplied blockers surface in the JSON output, the
#: review flow does not pretend the cases are runnable, and
#: the execution evaluation reports the precise reason.
EXECUTION_SKIPPED_INVALID_BENCHMARK = "invalid-benchmark"

#: Stable reason code for ``execution_evaluation.skipped_reason``
#: when the benchmark file is present and well-formed but has
#: no eligible reviewed cases (the empty / generated / disabled
#: shape). This is the canonical "review still completes with a
#: warning" path.
EXECUTION_SKIPPED_NO_ELIGIBLE_CASES = "no-eligible-reviewed-cases"

#: Status values written into the ``execution_evaluation.status``
#: field of a ``review`` payload. Pinned as module-level strings
#: so downstream tooling and tests can branch on the same
#: constants the review orchestrator emits.
EXECUTION_STATUS_PASS = "PASS"
EXECUTION_STATUS_FAIL = "FAIL"
EXECUTION_STATUS_BLOCKED = "BLOCKED"
EXECUTION_STATUS_SKIPPED = "SKIPPED"

#: Per-case status values written into ``execution_evaluation.
#: case_results[*].status``. Pinned as module-level strings so
#: downstream tooling and tests can branch on the same constants
#: the execution evaluator emits. Mirrors the ProfileResult
#: status vocabulary so a case-evaluator verdict is reusable.
REVIEW_CASE_STATUS_PASS = "PASS"
REVIEW_CASE_STATUS_FAIL = "FAIL"
REVIEW_CASE_STATUS_BLOCKED = "BLOCKED"

#: Stable reason code for ``execution_evaluation.skipped_reason``
#: when a benchmark file is present but is missing the required
#: reviewed cases (``pending-generated-case`` /
#: ``missing-reviewed-eval-case`` /
#: ``missing-reviewed-held-out-case``). Per ADR 0029 / Issue
#: #29 spec review, these are precondition failures for the
#: execution branch: a benchmark without eligible reviewed
#: cases cannot be evaluated, so the review is BLOCKED on the
#: execution path. Distinct from the no-benchmark-file path,
#: which keeps the static+warning behavior.
EXECUTION_BLOCKED_MISSING_REVIEWED = "missing-reviewed-cases"

#: Stable reason code for ``execution_evaluation.skipped_reason``
#: when at least one eligible reviewed case is missing the
#: ``expected_output`` fixture that the F1 deterministic
#: check engine needs to evaluate it. The case evaluation
#: itself is BLOCKED (the engine has nothing to grep against),
#: which propagates to the overall execution verdict.
EXECUTION_BLOCKED_MISSING_EXPECTED_OUTPUT = "missing-expected-output"

#: Stable blocker id for a case that has no ``checks`` and no
#: ``judgment`` -- the F1 execution engine cannot evaluate it
#: without one or the other.
REVIEW_CASE_NO_CHECKS_NO_JUDGMENT_BLOCKER = "review-case-no-checks-or-judgment"

#: Stable blocker id for a case whose ``judgment`` references
#: a control-plane judge provider that the F1 review cannot
#: reach (e.g. no provider configured). Mirrors
#: :data:`metacrucible.provider_config.JUDGE_EVALUATOR_BLOCKER`
#: semantically but is a distinct, F1-specific id so the
#: receipt surfaces the F1 path.
REVIEW_CASE_JUDGE_PROVIDER_UNAVAILABLE_BLOCKER = "review-case-judge-provider-unavailable"

#: Category passed to :func:`write_blocked_bundle` when the F1
#: review is BLOCKED for an execution-related reason and
#: execution was effectively requested (benchmark present).
#: Matches the ADR 0035 ``review_execution_requested`` slot.
REVIEW_EXECUTION_REQUESTED_BLOCKED_CATEGORY = "review_execution_requested"

#: Suffix appended to a review's run id when emitting the
#: execution-BLOCKED bundle. The static-review bundle keeps
#: the unsuffixed id; the BLOCKED bundle lands in a sibling
#: directory so both are reachable from the user-global store
#: and the F1 review is a "we could not proceed" record.
REVIEW_BLOCKED_BUNDLE_RUN_ID_SUFFIX = "-blocked"

#: Default number of draft cases ``bootstrap`` writes when the
#: caller does not pass ``--case-count``. Pinned here so the
#: help text, the parser default, and the test fixtures all
#: read from a single source of truth.
BOOTSTRAP_DEFAULT_CASE_COUNT = 3

#: Placeholder input text written onto every bootstrap-generated
#: case record. The case is meant to be human-reviewed and
#: fleshed out before promotion (per Issue #30 / PRD F2);
#: the text is the machine-stable contract that downstream
#: tooling can branch on, so a future change is a deliberate
#: single-site update.
BOOTSTRAP_DRAFT_INPUT = (
    "Draft evaluation case generated by bootstrap. "
    "Review and fill in checks, judgment, and expected_output "
    "before promoting."
)

#: Literal case-level field name used to flag bootstrap-generated
#: cases as "pending human review". ``promote`` removes the
#: field (via :func:`metacrucible.promote.promote_case`) when a
#: human promotes the case. The string is the machine-stable
#: contract used by the ``optimize`` sentinel gate.
BOOTSTRAP_PENDING_REVIEW_FIELD = "BOOTSTRAP_PENDING_REVIEW"

# --------------------------------------------------------------------------- #
# Issue #31 (PRD ``baseline create``) constants                              #
# --------------------------------------------------------------------------- #

#: Name of the baseline digest file written under
#: ``<workspace>/.metacrucible/``. Pinned by the Issue #31 contract so
#: downstream tooling can branch on the path without re-deriving it.
BASELINE_FILE_NAME = "baseline.json"

#: Schema version string stamped into ``baseline.json``. Pinned so a
#: future v2 is a deliberate single-site update; the on-disk digest
#: identifies a baseline as a specific contract version.
BASELINE_SCHEMA_VERSION = "metacrucible.baseline.v1"

#: Stable blocker id emitted by ``baseline create`` when the workspace
#: path does not exist on disk. The ``init`` command creates the
#: workspace; ``baseline create`` does not, so a missing workspace is
#: a precondition failure with a stable id.
BASELINE_WORKSPACE_MISSING_BLOCKER = "baseline-workspace-missing"

#: Stable blocker id emitted by ``baseline create`` when
#: ``<workspace>/.metacrucible/envelope.json`` is absent. The envelope
#: is the artifact-identity record; without it the baseline has no
#: artifact reference to hash.
BASELINE_ENVELOPE_MISSING_BLOCKER = "baseline-envelope-missing"

#: Stable blocker id emitted by ``baseline create`` when the
#: ``benchmark.jsonl`` container is absent at the workspace root.
#: The baseline hashes the canonical benchmark payload, so a missing
#: file is a precondition failure with a stable id.
BASELINE_BENCHMARK_MISSING_BLOCKER = "baseline-benchmark-missing"

#: Stable blocker id emitted by ``baseline create`` when the
#: envelope does not carry an ``artifact_path`` (or ``canonical_source``)
#: field. The baseline must read the artifact source bytes to hash
#: them; without an envelope-declared path the command refuses to
#: scan / glob the filesystem (per OD1) and surfaces this stable id.
BASELINE_ARTIFACT_UNRESOLVED_BLOCKER = "baseline-artifact-unresolved"

#: Stable blocker id emitted by ``baseline create`` when the workspace
#: is inside a git worktree and ``git status --porcelain`` reports dirty
#: files that are not the tracked baseline inputs (the artifact,
#: ``.metacrucible/envelope.json``, and ``benchmark.jsonl``). The
#: caller can pass ``--allow-dirty-unrelated`` to record the dirty
#: file list and proceed (Issue #31 dirty-file guard; subsumes the
#: earlier Issue #37 standalone guard).
BASELINE_UNRELATED_DIRTY_FILES_BLOCKER = "baseline-unrelated-dirty-files"

#: Run-type value written into the BLOCKED evidence bundle by
#: :func:`cmd_baseline_create`. Matches the ADR 0035 ``baseline_create``
#: slot in :data:`metacrucible.blocked_bundles.REQUIRES_BLOCKED_BUNDLE_CATEGORIES`
#: so the matrix already routes the BLOCKED bundle write through
#: :func:`metacrucible.blocked_bundles.write_blocked_bundle`.
BASELINE_BLOCKED_BUNDLE_RUN_TYPE = "baseline_create"

#: Run-id prefix used when emitting the ``baseline_create`` BLOCKED
#: evidence bundle. Mirrors the ``review-`` prefix the F1 review path
#: uses; downstream tooling can branch on the prefix to distinguish
#: baseline-create bundles from other BLOCKED categories.
BASELINE_BLOCKED_BUNDLE_RUN_ID_PREFIX = "baseline-create"

# --------------------------------------------------------------------------- #
# Issue #32 (``evaluate`` subcommand) constants                              #
# --------------------------------------------------------------------------- #

#: Machine-stable split value accepted by the ``--split`` flag of
#: ``metacrucible evaluate``; evaluates eligible reviewed eval AND
#: held-out cases. The existing ADR 0025 / ADR 0029 split values
#: (``SPLIT_EVAL`` / ``SPLIT_HELD_OUT``) are imported from
#: :mod:`metacrucible.benchmark`; this constant is the new
#: ``evaluate``-only choice.
SPLIT_ALL = "all"

#: Stable blocker id emitted by ``evaluate`` when the workspace has
#: no ``benchmark.jsonl`` at the workspace root. Distinct from the
#: review-only :data:`NO_REVIEWED_BENCHMARK_WARNING`: ``evaluate`` is
#: a support command whose explicit purpose is evaluation, so a
#: missing benchmark is a precondition failure (BLOCKED), not a
#: static-review + warning outcome.
EVALUATE_BENCHMARK_MISSING_BLOCKER = "evaluate-benchmark-missing"

#: Stable blocker id emitted by ``evaluate`` when the selected
#: ``--split`` partition is present in the benchmark but has zero
#: eligible reviewed cases. The message carries the split name so
#: the operator can tell ``eval`` / ``held_out`` / ``all`` apart
#: when triaging the result.
EVALUATE_NO_ELIGIBLE_CASES_BLOCKER = "evaluate-no-eligible-cases"

#: Run-type value written into the BLOCKED evidence bundle by
# --------------------------------------------------------------------------- #
# Issue #33 (PRD F3 ``optimize``) constants                                  #
# --------------------------------------------------------------------------- #
#
# The MVP optimizer is a reimplementation of the SkillOpt-shaped
# loop (ADR 0022 / ADR 0032). The CLI is a thin wrapper around
# :func:`metacrucible.optimizer.run_optimizer_pipeline`; the new
# blocker ids emitted on the pipeline paths land here as
# machine-stable re-exports so the rest of the CLI surface
# (blockers list, evidence receipt, --json output) can branch
# on the same string.

#: Run-type value written into the evidence bundle by
#: :func:`cmd_optimize`. Matches the ADR 0035 ``optimize``
#: slot in
#: :data:`metacrucible.blocked_bundles.REQUIRES_BLOCKED_BUNDLE_CATEGORIES`
#: so the matrix routes the BLOCKED bundle write through
#: :func:`metacrucible.blocked_bundles.write_blocked_bundle`.
OPTIMIZE_BLOCKED_BUNDLE_RUN_TYPE = "optimize"

#: Run-id prefix used when emitting the ``optimize`` BLOCKED
#: evidence bundle. Mirrors the ``evaluate`` / ``baseline-create``
#: prefixes; downstream tooling can branch on the prefix to
#: distinguish optimize bundles from other BLOCKED categories.
OPTIMIZE_BLOCKED_BUNDLE_RUN_ID_PREFIX = "optimize"
#: :func:`_write_evaluate_blocked_bundle`. Matches the ADR 0035
#: ``evaluate`` slot in
#: :data:`metacrucible.blocked_bundles.REQUIRES_BLOCKED_BUNDLE_CATEGORIES`
#: so the matrix already routes the BLOCKED bundle write through
#: :func:`metacrucible.blocked_bundles.write_blocked_bundle`.
EVALUATE_BLOCKED_BUNDLE_RUN_TYPE = "evaluate"

#: Run-id prefix used when emitting the ``evaluate`` BLOCKED
#: evidence bundle. Mirrors the ``baseline-create`` prefix; downstream
#: tooling can branch on the prefix to distinguish evaluate bundles
#: from other BLOCKED categories.
EVALUATE_BLOCKED_BUNDLE_RUN_ID_PREFIX = "evaluate"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metacrucible",
        description=(
            "MetaCrucible: a workbench for improving portable agent "
            "capabilities through repeatable optimization, evaluation, "
            "and review loops."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"metacrucible {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser(
        "init",
        help=(
            "initialize an artifact workspace envelope and empty "
            "benchmark container (ADR 0035)"
        ),
    )
    init_parser.add_argument(
        "workspace",
        help="path to the artifact workspace (created if missing)",
    )
    init_parser.add_argument(
        "--check",
        action="store_true",
        help="validate an existing workspace without creating files",
    )
    init_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    init_parser.add_argument(
        "--no-isolation",
        action="store_true",
        help=(
            "skip copy-on-write workspace masking (Issue #13); "
            "requires --confirm-no-isolation and a TTY, or the "
            "METACRUCIBLE_ALLOW_NO_ISOLATION=1 env-var override"
        ),
    )
    init_parser.add_argument(
        "--review",
        dest="review_artifact",
        default=None,
        metavar="ARTIFACT_FILE",
        help=(
            "read a capability artifact file, run the static review "
            "profiles against its parsed body, and write a receipt + "
            "summary + trajectory digest to the user-global evidence "
            "store (Issue #28 tracer bullet). The artifact is read "
            "only; the source bytes are never mutated."
        ),
    )
    init_parser.add_argument(
        "--confirm-no-isolation",
        action="store_true",
        help=(
            "explicit human confirmation that workspace masking is "
            "intentionally being disabled (Issue #13 AC3)"
        ),
    )
    review_parser = subparsers.add_parser(
        "review",
        help=(
            "one-shot diagnostic against an existing capability "
            "artifact (PRD F1): Static Review with the Darwin "
            "9-dimension rubric plus Execution Evaluation when a "
            "reviewed Benchmark is present (Issue #29)"
        ),
    )
    review_parser.add_argument(
        "artifact",
        help=(
            "path to a capability artifact file (Skill or "
            "subagent Markdown). The source bytes are read only; "
            "they are never mutated by the review pipeline."
        ),
    )
    review_parser.add_argument(
        "--workspace",
        default=None,
        metavar="WORKSPACE",
        help=(
            "path to the artifact workspace (defaults to the "
            "artifact's parent directory). The review command "
            "looks for ``benchmark.jsonl`` at the workspace root."
        ),
    )
    review_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    promote_parser = subparsers.add_parser(
        "promote",
        help="promote a generated benchmark case after human review",
    )
    promote_parser.add_argument(
        "workspace",
        help="path to the artifact workspace",
    )
    promote_parser.add_argument(
        "--case-id",
        required=True,
        help="case_id of the generated benchmark case to promote",
    )
    promote_parser.add_argument(
        "--split",
        choices=[SPLIT_EVAL, SPLIT_HELD_OUT],
        required=True,
        help="reviewed split to assign to the promoted case",
    )
    promote_parser.add_argument(
        "--reviewed-by",
        required=True,
        help="human reviewer identity to record on the case",
    )
    promote_parser.add_argument(
        "--review-note",
        default="",
        help="human review note to record on the case",
    )
    promote_parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite benchmark.jsonl; default is dry-run",
    )
    promote_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help=(
            "generate draft evaluation cases for an existing "
            "artifact and write them to benchmark.jsonl as "
            "pending generated cases (PRD F2)"
        ),
    )
    bootstrap_parser.add_argument(
        "workspace",
        help="path to the artifact workspace",
    )
    bootstrap_parser.add_argument(
        "--case-count",
        type=int,
        default=BOOTSTRAP_DEFAULT_CASE_COUNT,
        help=(
            "number of draft evaluation cases to generate "
            f"(default: {BOOTSTRAP_DEFAULT_CASE_COUNT})"
        ),
    )
    bootstrap_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    optimize_parser = subparsers.add_parser(
        "optimize",
        help=(
            "improve an existing artifact against a reviewed "
            "benchmark (PRD F3); refuses to start if generated "
            "cases or the bootstrap sentinel are present"
        ),
    )
    optimize_parser.add_argument(
        "workspace",
        help="path to the artifact workspace",
    )
    optimize_parser.add_argument(
        "--max-rounds",
        type=int,
        default=ROUND_BUDGET_DEFAULT,
        help=(
            "maximum number of optimization rounds the pipeline "
            f"may attempt (default: {ROUND_BUDGET_DEFAULT}); "
            "bounded and observable; no silent infinite loop "
            "(OPT-8 / PRD F3)"
        ),
    )
    optimize_parser.add_argument(
        "--confirm-routing",
        action="store_true",
        help=(
            "explicit human confirmation that any selected "
            "routing edit may enter a candidate revision "
            "(ADR 0027 / ADR 0032); without this flag the "
            "routing HITL gate blocks the round"
        ),
    )
    optimize_parser.add_argument(
        "--allow-dirty-unrelated",
        action="store_true",
        help=(
            "record the dirty-file list and proceed even when the "
            "git worktree carries dirty files unrelated to the "
            "optimize inputs (artifact, envelope, benchmark); "
            "default is to BLOCK on unrelated dirty files"
        ),
    )
    optimize_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    # ``verify``) extend the same nested parser without breaking
    # the ``baseline`` outer shape. ``dest="baseline_action"`` on
    # the inner subparser so the dispatcher can branch on
    # ``args.baseline_action`` without ambiguity.
    baseline_parser = subparsers.add_parser(
        "baseline",
        help=(
            "record or inspect digest baselines for an artifact "
            "workspace (ADR 0035 / Issue #31)"
        ),
    )
    baseline_subparsers = baseline_parser.add_subparsers(
        dest="baseline_action"
    )
    baseline_create_parser = baseline_subparsers.add_parser(
        "create",
        help=(
            "compute and write a digest baseline that pins the "
            "artifact, envelope, benchmark, and evaluation harness "
            "hashes for the workspace"
        ),
    )
    baseline_create_parser.add_argument(
        "workspace",
        help="path to the artifact workspace",
    )
    baseline_create_parser.add_argument(
        "--allow-dirty-unrelated",
        action="store_true",
        help=(
            "record the dirty-file list and proceed even when the "
            "git worktree carries dirty files unrelated to the "
            "baseline inputs (artifact, envelope, benchmark); "
            "default is to BLOCK on unrelated dirty files"
        ),
    )
    baseline_create_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    # ``evaluate`` subcommand (Issue #32). The split filter is
    # pinned by ADR 0029 / ADR 0025: ``all`` runs every eligible
    # reviewed case, ``eval`` runs only the eval partition, and
    # ``held_out`` runs only the held-out partition. A missing
    # benchmark is BLOCKED (precondition failure), not SKIPPED
    # + warning like ``review`` -- ``evaluate`` is a support
    # command whose explicit purpose is evaluation.
    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help=(
            "evaluate the eligible reviewed cases in a benchmark "
            "(ADR 0029 / Issue #32); supports --split all/eval/held_out"
        ),
    )
    evaluate_parser.add_argument(
        "workspace",
        help="path to the artifact workspace",
    )
    evaluate_parser.add_argument(
        "--split",
        choices=[SPLIT_ALL, SPLIT_EVAL, SPLIT_HELD_OUT],
        default=SPLIT_ALL,
        help=(
            "which reviewed cases to evaluate; "
            f"{SPLIT_ALL!r} runs both eval and held_out, "
            f"{SPLIT_EVAL!r} runs only eval, "
            f"{SPLIT_HELD_OUT!r} runs only held_out "
            f"(default: {SPLIT_ALL!r})"
        ),
    )
    evaluate_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    return parser


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _default_envelope(workspace: Path) -> dict[str, Any]:
    return {
        "artifact_workspace": str(workspace),
        "created_at": _now_iso(),
    }


def _default_state() -> dict[str, Any]:
    return {
        "current_best_revision": None,
        "last_run_id": None,
    }


def _default_metadata_record() -> dict[str, Any]:
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
        "created_at": _now_iso(),
    }


def _read_benchmark_records(benchmark: Path) -> list[dict[str, Any]]:
    """Return all parseable JSON object records from a JSONL file.

    Lines that fail to parse or that do not decode as a JSON object
    are skipped: ``init --check`` is a non-destructive validator and
    must not crash on a malformed line.
    """
    if not benchmark.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in benchmark.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _reviewed_case_count(records: list[dict[str, Any]]) -> int:
    """Count case records that have been reviewed.

    A case record is any record whose ``record_type`` is one of
    ``case`` / ``case_eval`` / ``case_held_out`` (the discriminator
    set ADR 0029 reserves for benchmark case rows). A record counts
    as "reviewed" when ``reviewed`` is ``True`` or ``status`` is
    ``"reviewed"`` — the two machine-stable shapes the rest of the
    pipeline emits.
    """
    count = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("record_type")
        if rtype not in {"case", "case_eval", "case_held_out"}:
            continue
        if rec.get("reviewed") is True or rec.get("status") == "reviewed":
            count += 1
    return count


def _create_workspace(workspace: Path) -> dict[str, Any]:
    """Create envelope/state/benchmark if absent; return path map.

    Idempotent by design: existing files are left untouched so a
    second ``init`` on the same workspace does not silently mutate
    the envelope (ADR 0016 + ADR 0020).
    """
    storage = RepositoryStorage(workspace)
    created = False
    if not storage.envelope_path.is_file():
        storage.write_envelope(_default_envelope(workspace))
        created = True
    if not storage.state_path.is_file():
        storage.write_state(_default_state())
        created = True
    benchmark = workspace / BENCHMARK_FILE_NAME
    if not benchmark.is_file():
        benchmark.write_text(
            json.dumps(_default_metadata_record(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        created = True
    return {
        "workspace": workspace,
        "envelope_path": storage.envelope_path,
        "state_path": storage.state_path,
        "benchmark_path": benchmark,
        "created": created,
    }


def _check_workspace(workspace: Path) -> dict[str, Any]:
    """Validate a workspace; return blockers and the path map.

    ``RepositoryStorage`` is constructed so the path map reflects
    where the envelope/state *would* live; the validator does not
    write any files itself.
    """
    storage = RepositoryStorage(workspace)
    benchmark = workspace / BENCHMARK_FILE_NAME
    records = _read_benchmark_records(benchmark)
    blockers: list[dict[str, Any]] = []
    if _reviewed_case_count(records) == 0:
        blockers.append(
            {
                "id": MISSING_REVIEWED_CASE_BLOCKER,
                "message": (
                    "benchmark has no reviewed cases; "
                    "an empty benchmark is a valid container but "
                    "cannot be evaluated (ADR 0025, ADR 0029)"
                ),
            }
        )
    return {
        "workspace": workspace,
        "envelope_path": storage.envelope_path,
        "state_path": storage.state_path,
        "benchmark_path": benchmark,
        "ok": not blockers,
        "blockers": blockers,
    }


def _parse_artifact_source(
    source: str, *, artifact_path: Path
) -> tuple[str, Any]:
    """Parse ``source`` as a subagent-first, then-Skill artifact.

    The parser API is :func:`parse_subagent` and :func:`parse_skill`
    (Issue #4). Subagents and Skills share the frontmatter shape but
    differ in field semantics (subagents add ``tools``/``spawns``/
    ``systemPrompt``); we try subagent first and fall back to Skill
    so the caller's filename is informational, not a contract.
    """
    from . import artifact as _artifact
    from .artifact import parse_skill, parse_subagent

    try:
        parsed = parse_subagent(source)
        return ("subagent", parsed)
    except ValueError:
        try:
            parsed = parse_skill(source)
            return ("skill", parsed)
        except ValueError:
            raise ValueError(
                f"artifact {artifact_path} is not a recognized Skill or "
                f"subagent source; frontmatter is missing or malformed "
                f"(see {_artifact.__name__})"
            ) from None


def _run_static_review(
    *,
    workspace: Path,
    artifact_path: Path,
    run_id_prefix: str = INIT_REVIEW_RUN_TYPE,
    run_type: str = INIT_REVIEW_RUN_TYPE,
) -> dict[str, Any]:
    """Read ``artifact_path`` and write a v1 evidence bundle.

    Tracer-bullet pipeline (Issue #28 acceptance):

      1. Read the artifact source bytes (read-only — caller must not
         pass a path the CLI would write to; we never mutate the
         source).
      2. Parse via the existing :mod:`metacrucible.artifact` parser.
      3. Feed the parsed body into the existing static-review
         profile surfaces (``evaluate_secret_privacy_risk``,
         ``evaluate_runtime_neutrality``) plus the harness-identity
         helper ``compute_evaluation_harness_sha``. No new review
         semantics are invented here.
      4. Aggregate per-profile results through
         :func:`evaluate_acceptance` (the existing verdict
         primitive).
      5. Persist the receipt, summary, and trajectory digest via
         the existing :class:`UserGlobalStorage` writers, which run
         the payload through :func:`build_receipt_payload`,
         :func:`build_summary_payload`, and
         :func:`build_trajectory_digest_payload` (v1 contracts).

    Returns the path map so the caller can surface it through
    ``--json`` / human output. On a missing artifact file, raises
    ``FileNotFoundError``; on a malformed source, raises
    ``ValueError`` (a ``BLOCKED`` bundle is not written for
    pre-pipeline failures — the CLI maps the exception to
    ``EXIT_USER_ERROR``).
    """
    from .profiles import (
        BUILTIN_PROFILES,
        evaluate_acceptance,
        evaluate_runtime_neutrality,
        evaluate_secret_privacy_risk,
        compute_evaluation_harness_sha,
    )

    source_bytes = artifact_path.read_bytes()
    # Decode for the parser; the source is runtime-native Markdown
    # (per ADR 0005), so UTF-8 is the right contract. A
    # ``UnicodeDecodeError`` is a user-input error, not a BLOCKED
    # condition.
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"artifact {artifact_path} is not valid UTF-8: {exc}"
        ) from exc

    kind, parsed = _parse_artifact_source(source_text, artifact_path=artifact_path)

    # Build the review input mapping. The static-review profiles
    # read ``body`` and ``portability.target``; we project the
    # parsed artifact into that shape without inventing new
    # review semantics. ``routing_touched`` follows the parsed
    # routing surface (Issue #21 / ADR 0033): if the artifact
    # declares any routing-surface field, treat the surface as
    # touched for the purposes of trigger selection.
    body = parsed.body
    if hasattr(parsed, "frontmatter") and isinstance(parsed.frontmatter, dict):
        # Subagent artifacts expose a ``systemPrompt`` mutable range
        # that the secret-privacy scanner also wants to see. The
        # framework's existing ``body`` field is the contract; we
        # concatenate the system prompt so the scanner sees the full
        # surface it would see in a real run.
        system_prompt = parsed.frontmatter.get("systemPrompt")
        if isinstance(system_prompt, str) and system_prompt:
            body = system_prompt + "\n" + body
    review_input: dict[str, Any] = {
        "body": body,
        "portability": {"target": "runtime_neutral"},
        "reviewed_fake_secrets": (),
    }

    secret_result = evaluate_secret_privacy_risk(review_input)
    runtime_result = evaluate_runtime_neutrality(review_input)

    # Trigger selection: secret-privacy-risk is hard-coded for every
    # run; routing-surface-safety is triggered when routing was
    # touched (we keep it informational here — the static-review
    # tracer bullet is about wiring, not verdict policy).
    routing_touched = bool(getattr(parsed, "routing_surface", frozenset()))
    spec_index = {spec.id: spec for spec in BUILTIN_PROFILES}
    triggered_ids = {secret_result.profile_id, runtime_result.profile_id}
    if routing_touched:
        # Surface routing-safety as a triggered profile so the
        # harness identity digest matches what a real run would
        # hash. The per-profile result is still whatever the
        # profile produced; we do not invent a verdict.
        from .profiles import evaluate_routing_surface_safety
        routing_result = evaluate_routing_surface_safety(
            {"routing_changes": list(getattr(parsed, "routing_surface", ()))}
        )
        profile_results = [secret_result, runtime_result, routing_result]
        triggered_ids.add(routing_result.profile_id)
    else:
        profile_results = [secret_result, runtime_result]

    verdict = evaluate_acceptance(
        profile_results,
        profile_specs=spec_index,
    )
    harness_sha = compute_evaluation_harness_sha(
        tuple(spec_index[pid] for pid in sorted(triggered_ids))
    )

    # Persist the three durable bundle files via the existing v1
    # builders / writers. No new schema is invented.
    run_id = (
        f"{run_id_prefix}-{_now_iso().replace(':', '').replace('-', '')}"
    )
    global_store = UserGlobalStorage()

    receipt_payload: dict[str, Any] = {
        "run_id": run_id,
        "run_type": run_type,
        "status": "PASS" if verdict["accepted"] else "BLOCKED",
        "artifact": str(artifact_path),
        "artifact_kind": kind,
        "envelope": str(workspace / ".metacrucible" / "envelope.json"),
        "evaluation_harness": harness_sha,
        "blockers": verdict["blockers"],
    }
    receipt_path = global_store.write_receipt(run_id, receipt_payload)

    summary_payload: dict[str, Any] = {
        "status": receipt_payload["status"],
        "blockers": verdict["blockers"],
        "counts": {
            "profiles_run": len(profile_results),
            "blockers": len(verdict["blockers"]),
            "supplemental_findings": len(verdict["supplemental_findings"]),
        },
    }
    summary_path = global_store.write_summary(run_id, summary_payload)

    trajectory_steps: list[dict[str, Any]] = [
        {
            "step": 0,
            "action": "parse_artifact",
            "status": "PASS",
            "kind": kind,
        },
        {
            "step": 1,
            "action": "static_review",
            "status": receipt_payload["status"],
            "profile_ids": [r.profile_id for r in profile_results],
        },
    ]
    for idx, blocker in enumerate(verdict["blockers"]):
        trajectory_steps.append(
            {
                "step": 2 + idx,
                "action": "blocker",
                "status": "BLOCKED",
                "blocker": blocker,
            }
        )
    digest_payload: dict[str, Any] = {
        "run_id": run_id,
        "artifact": str(artifact_path),
        "steps": trajectory_steps,
    }
    digest_path = global_store.write_trajectory_digest(run_id, digest_payload)

    return {
        "artifact_path": str(artifact_path),
        "artifact_kind": kind,
        "run_id": run_id,
        "receipt_path": str(receipt_path),
        "summary_path": str(summary_path),
        "trajectory_digest_path": str(digest_path),
        "accepted": verdict["accepted"],
        "blockers": verdict["blockers"],
        # Non-breaking extension (Issue #29): surface the
        # supplemental findings and the per-profile result ids
        # so the F1 ``review`` orchestrator can build its
        # ``static_review`` sub-section without re-running the
        # profile suite. Existing callers (``init --review``)
        # ignore the new keys; their tests only assert on the
        # path fields above.
        "supplemental_findings": verdict["supplemental_findings"],
        "profile_ids": [r.profile_id for r in profile_results],
    }


def _emit_human_value(
    key: str,
    value: Any,
    *,
    indent: str = "",
) -> None:
    """Render ``value`` for the human (non-JSON) CLI surface.

    Recursively formats nested mappings with a stable two-space
    indent and lists of dicts as a bullet list. Bypasses the
    user-controlled freeform text surface (``review_note``) so a
    multilingual note never contaminates the English prose
    contract (Issue #27 task 27.4). Special-cases ``blockers``
    and ``warnings`` so a list of ``{id, message}`` dicts
    renders as ``- <id>: <message>`` lines in the operator
    workflow.
    """
    if key == "blockers" and isinstance(value, list):
        if value:
            for blocker in value:
                if isinstance(blocker, dict):
                    bid = blocker.get("id", "?")
                    msg = blocker.get("message", "")
                    print(f"{indent}- {bid}: {msg}")
                else:
                    print(f"{indent}- {blocker}")
        else:
            print(f"{indent}{key}: (none)")
    elif key == "warnings" and isinstance(value, list):
        if value:
            for warning in value:
                if isinstance(warning, dict):
                    wid = warning.get("id", "?")
                    msg = warning.get("message", "")
                    print(f"{indent}- {wid}: {msg}")
                else:
                    print(f"{indent}- {warning}")
        else:
            print(f"{indent}{key}: (none)")
    elif key == "review_note":
        # User-controlled freeform text; the operator does not
        # need it echoed back as part of the English prose
        # surface, and a non-ASCII note would otherwise
        # contaminate the human-only contract. Use ``--json``
        # to retrieve the verbatim value.
        if isinstance(value, str) and value:
            print(
                f"{indent}{key}: <{len(value)} chars, hidden in "
                f"human output; use --json to view>"
            )
        else:
            print(f"{indent}{key}: (empty)")
    elif isinstance(value, dict):
        if value:
            print(f"{indent}{key}:")
            for sub_key in sorted(value.keys()):
                _emit_human_value(
                    sub_key, value[sub_key], indent=indent + "  "
                )
        else:
            print(f"{indent}{key}: {{}}")
    elif isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            for item in value:
                print(f"{indent}- {key}:")
                for sub_key in sorted(item.keys()):
                    _emit_human_value(
                        sub_key, item[sub_key], indent=indent + "    "
                    )
        else:
            print(f"{indent}{key}: {value}")
    else:
        print(f"{indent}{key}: {value}")


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    """Write ``payload`` to stdout in JSON or human form.

    The human form is a key/value summary that keeps the CLI's
    own prose English-only (Issue #27 task 27.4). User-controlled
    freeform text (currently ``review_note`` from ``promote``) is
    masked in the human surface so a multilingual review note
    never contaminates the English prose contract. The full
    value is preserved by ``--json`` for callers that need it.
    Nested mappings and lists of dicts (used by the F1 ``review``
    output) are rendered recursively with a stable two-space
    indent so the operator can scan the full review payload
    without flipping to ``--json``.
    """
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key in sorted(payload.keys()):
        _emit_human_value(key, payload[key])


def _discover_benchmark_state(workspace: Path) -> dict[str, Any]:
    """Inspect the workspace's ``benchmark.jsonl`` and report its state.

    The state is a machine-stable mapping the F1 ``review``
    orchestrator composes into its JSON output:

      - ``present`` (``bool``) — ``True`` iff the benchmark file
        exists at the workspace root. A missing file is not
        collapsed into a "present but invalid" state; the two
        are distinct paths in the output (``execution_evaluation.
        status`` differs).
      - ``path`` (``str``) — the resolved benchmark path the
        orchestrator inspected (or would have inspected).
      - ``eligible_eval_count`` / ``eligible_held_out_count`` /
        ``pending_generated_count`` / ``disabled_count``
        (``int``) — the four ADR 0029 partitions. Generated
        and disabled cases are never eligible for execution.
      - ``blockers`` (``list[dict]``) — the *full* loader
        blocker list. Every loader blocker surfaces to the
        operator in the benchmark sub-section; the
        execution-evaluation BLOCKED gate uses the same
        list (no optimize-only carve-out: missing required
        reviewed cases are a precondition failure for the
        execution branch and BLOCK the review).

    A missing benchmark file returns a default state with
    ``present=False`` and zeroed counts so the orchestrator
    can take the "no reviewed benchmark" path without
    special-casing ``None``.
    """
    from .benchmark import load_benchmark

    benchmark_path = workspace / BENCHMARK_FILE_NAME
    if not benchmark_path.is_file():
        return {
            "present": False,
            "path": str(benchmark_path),
            "eligible_eval_count": 0,
            "eligible_held_out_count": 0,
            "pending_generated_count": 0,
            "disabled_count": 0,
            "blockers": [],
        }

    result = load_benchmark(benchmark_path)
    # The benchmark sub-section reports the *full* loader
    # blocker list so the operator sees the complete picture;
    # the execution-evaluation BLOCKED gate uses the narrower
    # list that excludes optimize-only ids.
    return {
        "present": True,
        "path": str(benchmark_path),
        "eligible_eval_count": len(result.eligible_eval_cases),
        "eligible_held_out_count": len(result.eligible_held_out_cases),
        "pending_generated_count": len(result.pending_generated_cases),
        "disabled_count": len(result.disabled_cases),
        "blockers": list(result.blockers),
    }


def _run_execution_evaluation(
    *,
    benchmark_state: dict[str, Any],
) -> dict[str, Any]:
    """Compute the F1 Execution Evaluation diagnostic (Issue #29).

    The F1 spec requires that "Execution Evaluation runs when a
    reviewed Benchmark is present." The diagnostic is wired to
    the real execution machinery:

      * **Deterministic checks** are evaluated through
        :func:`metacrucible.rule_checks.execute_check` with a
        :class:`metacrucible.rule_checks.CheckBoundary` built
        from the case's ``checks`` field. The case's
        ``expected_output`` (if present) is written to a
        per-case workspace and a small Python check script
        is dispatched through ``execute_check``. A case is
        PASS only when the check engine returns
        ``ok=True``; a non-zero returncode is a FAILED case;
        any engine blocker (workspace invalid, complex shell
        without wrapper, etc.) is a BLOCKED case.

      * **Non-deterministic judgments** are routed through
        the two-independent-LLM-judge evaluator
        (:func:`metacrucible.provider_config.run_judge_evaluator`).
        If a judgment is requested but the control-plane
        judge provider is not configured (no provider entry
        in the resolved config), the case is BLOCKED with
        the ``review-case-judge-provider-unavailable`` id
        per the spec-reviewer's "BLOCKED, not pass" rule.

      * A case with **neither** checks **nor** judgment is
        BLOCKED with the
        ``review-case-no-checks-or-judgment`` id; the F1
        engine cannot evaluate a case without one or the
        other (per ADR 0010 / ADR 0029).

    The function returns one of four statuses:

      - :data:`EXECUTION_STATUS_PASS` — every eligible reviewed
        case was evaluated and passed.
      - :data:`EXECUTION_STATUS_FAIL` — every eligible
        reviewed case was evaluated and at least one FAILED
        with no BLOCKED cases mixed in. The run executed;
        the verdict is FAILED.
      - :data:`EXECUTION_STATUS_BLOCKED` — at least one case
        could not be evaluated (no checks / no judgment /
        engine blocker / judge unavailable), **or** the
        benchmark file is present and the loader surfaced
        structural blockers (``schema-version-mismatch`` /
        ``duplicate-case-id``), **or** the benchmark file
        is present but missing the required reviewed cases
        (the
        ``pending-generated-case`` /
        ``missing-reviewed-eval-case`` /
        ``missing-reviewed-held-out-case`` ids). All three
        paths emit the BLOCKED verdict; the
        ``skipped_reason`` distinguishes them so the
        operator can tell "structural" from "missing
        cases".
      - :data:`EXECUTION_STATUS_SKIPPED` — the benchmark
        file is missing entirely (``skipped_reason`` =
        :data:`EXECUTION_SKIPPED_NO_REVIEWED_BENCHMARK`).
        This is the canonical F1 "static + warning" path:
        the static review completes with a warning and the
        exit code is :data:`EXIT_OK`.

    Per the spec-reviewer's "BLOCKED, not silently
    downgraded" finding, the optimize-only blocker filter
    that previously excluded
    ``pending-generated-case``,
    ``missing-reviewed-eval-case``, and
    ``missing-reviewed-held-out-case`` from the BLOCKED
    gate has been removed. A benchmark present at the
    workspace root carries an implicit "execution was
    requested" intent; missing required reviewed cases
    is a precondition failure, not a warning.

    The returned mapping's ``case_results`` slot carries a
    per-case verdict so a downstream reader can branch on
    which case failed / blocked without re-reading the
    benchmark file.
    """
    if not benchmark_state["present"]:
        return {
            "status": EXECUTION_STATUS_SKIPPED,
            "skipped": True,
            "skipped_reason": EXECUTION_SKIPPED_NO_REVIEWED_BENCHMARK,
            "cases_evaluated": 0,
            "cases_passed": 0,
            "cases_failed": 0,
            "case_results": [],
            "blockers": [],
        }

    # 1. Loader-supplied blockers gate the whole run. Every
    #    loader blocker is propagated (no optimize-only
    #    filter); the ``structural_ids`` split is purely
    #    informational so the receipt can report the
    #    reason code in a stable shape.
    all_blockers: list[dict[str, Any]] = [
        b for b in benchmark_state["blockers"]
        if isinstance(b, dict) and isinstance(b.get("id"), str)
    ]
    if all_blockers:
        reason = (
            EXECUTION_SKIPPED_INVALID_BENCHMARK
            if any(b["id"] in _REVIEW_STRUCTURAL_BENCHMARK_BLOCKERS
                   for b in all_blockers)
            else EXECUTION_BLOCKED_MISSING_REVIEWED
        )
        return {
            "status": EXECUTION_STATUS_BLOCKED,
            "skipped": False,
            "skipped_reason": reason,
            "cases_evaluated": 0,
            "cases_passed": 0,
            "cases_failed": 0,
            "case_results": [],
            "blockers": all_blockers,
        }

    # 2. Walk the eligible partitions and actually run the
    #    execution machinery per case. The four ADR 0029
    #    partitions are observed: generated / disabled
    #    cases are never evaluated.
    eligible_cases = _collect_eligible_cases(benchmark_state)
    if not eligible_cases:
        # This path is unreachable now that the optimize-only
        # filter is gone (any missing-reviewed blocker is
        # raised in the loader step). Kept for the no-bench
        # edge case where a benchmark carries only generated
        # / disabled cases and the loader happens not to
        # surface a missing-reviewed-eval-case blocker
        # (defensive only).
        return {
            "status": EXECUTION_STATUS_BLOCKED,
            "skipped": False,
            "skipped_reason": EXECUTION_BLOCKED_MISSING_REVIEWED,
            "cases_evaluated": 0,
            "cases_passed": 0,
            "cases_failed": 0,
            "case_results": [],
            "blockers": [{
                "id": "missing-reviewed-eval-case",
                "message": (
                    "no eligible reviewed eval cases (ADR 0025)"
                ),
            }],
        }

    case_results: list[dict[str, Any]] = []
    for case in eligible_cases:
        result = _evaluate_single_case(case)
        case_results.append(result)

    # 3. Aggregate per-case verdicts into the top-level
    #    execution verdict. BLOCKED beats FAILED beats PASS:
    #    a single blocked case blocks the whole run; a
    #    single failed case (with no blocked) fails the
    #    whole run; only a clean PASS-everywhere run
    #    returns PASS.
    any_blocked = any(
        r["status"] == REVIEW_CASE_STATUS_BLOCKED
        for r in case_results
    )
    any_failed = any(
        r["status"] == REVIEW_CASE_STATUS_FAIL
        for r in case_results
    )
    cases_evaluated = len(case_results)
    cases_passed = sum(
        1 for r in case_results
        if r["status"] == REVIEW_CASE_STATUS_PASS
    )
    cases_failed = sum(
        1 for r in case_results
        if r["status"] == REVIEW_CASE_STATUS_FAIL
    )
    if any_blocked:
        # Roll up the per-case blockers so the operator
        # sees which case blocked and why.
        blockers: list[dict[str, Any]] = []
        for r in case_results:
            if r["status"] != REVIEW_CASE_STATUS_BLOCKED:
                continue
            case_id = r.get("case_id") or "?"
            for blocker in r.get("blockers", []):
                if not isinstance(blocker, dict):
                    continue
                blockers.append(
                    {
                        "id": blocker.get("id", "?"),
                        "message": blocker.get("message", ""),
                        "case_id": case_id,
                    }
                )
        return {
            "status": EXECUTION_STATUS_BLOCKED,
            "skipped": False,
            "skipped_reason": None,
            "cases_evaluated": cases_evaluated,
            "cases_passed": cases_passed,
            "cases_failed": cases_failed,
            "case_results": case_results,
            "blockers": blockers,
        }
    if any_failed:
        return {
            "status": EXECUTION_STATUS_FAIL,
            "skipped": False,
            "skipped_reason": None,
            "cases_evaluated": cases_evaluated,
            "cases_passed": cases_passed,
            "cases_failed": cases_failed,
            "case_results": case_results,
            "blockers": [],
        }
    return {
        "status": EXECUTION_STATUS_PASS,
        "skipped": False,
        "skipped_reason": None,
        "cases_evaluated": cases_evaluated,
        "cases_passed": cases_passed,
        "cases_failed": cases_failed,
        "case_results": case_results,
        "blockers": [],
    }


#: Benchmark blocker ids that signal a structurally broken
#: benchmark file (ADR 0029). Distinguished from the
#: "missing required reviewed cases" ids so the receipt
#: can report a stable ``skipped_reason`` for the BLOCKED
#: verdict.
_REVIEW_STRUCTURAL_BENCHMARK_BLOCKERS: frozenset[str] = frozenset(
    {
        "schema-version-mismatch",
        "duplicate-case-id",
    }
)


def _collect_eligible_cases(
    benchmark_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Re-load the benchmark and return the eligible cases.

    The benchmark ``state`` mapping carries counts only; the
    F1 execution engine needs the actual case records to
    dispatch per-case evaluation. The state is trusted to
    reflect the on-disk file (it was produced by
    :func:`_discover_benchmark_state` immediately above),
    so the re-load is a single read-and-partition pass that
    does not mutate the file.
    """
    from .benchmark import load_benchmark
    if not benchmark_state.get("present"):
        return []
    benchmark_path = Path(benchmark_state["path"])
    if not benchmark_path.is_file():
        return []
    result = load_benchmark(benchmark_path)
    return list(result.eligible_eval_cases) + list(
        result.eligible_held_out_cases
    )


def _evaluate_single_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one eligible reviewed case.

    The dispatcher branches on the case schema:

      1. ``checks`` field present and non-empty -- route
         through :func:`metacrucible.rule_checks.execute_check`
         with a :class:`metacrucible.rule_checks.CheckBoundary`
         built from the case's pattern list and the case's
         ``expected_output`` fixture.
      2. ``judgment`` field present -- route through
         :func:`metacrucible.provider_config.run_judge_evaluator`.
         A missing provider is BLOCKED (per the spec-reviewer's
         "BLOCKED with evidence, not pass" finding).
      3. Neither -- BLOCKED with the
         :data:`REVIEW_CASE_NO_CHECKS_NO_JUDGMENT_BLOCKER` id.

    The returned mapping has shape::

        {
            "case_id": str,
            "evaluator": "rule_check" | "judge" | "none",
            "status": PASS | FAIL | BLOCKED,
            "blockers": list[dict],
            "evidence": dict | None,
        }
    """
    case_id = case.get("case_id") if isinstance(case, Mapping) else None
    case_id_str = case_id if isinstance(case_id, str) else "?"

    checks = case.get("checks") if isinstance(case, Mapping) else None
    judgment = case.get("judgment") if isinstance(case, Mapping) else None

    if isinstance(checks, list) and checks:
        result = _evaluate_case_with_rule_check(
            case=case,
            case_id=case_id_str,
        )
        return result
    if isinstance(judgment, dict) and judgment:
        result = _evaluate_case_with_judgment(
            case=case,
            case_id=case_id_str,
        )
        return result
    return {
        "case_id": case_id_str,
        "evaluator": "none",
        "status": REVIEW_CASE_STATUS_BLOCKED,
        "blockers": [{
            "id": REVIEW_CASE_NO_CHECKS_NO_JUDGMENT_BLOCKER,
            "message": (
                f"case {case_id_str!r} has neither ``checks`` nor "
                "``judgment``; the F1 execution engine cannot "
                "evaluate a case without one or the other (ADR "
                "0010 / ADR 0029)"
            ),
        }],
        "evidence": None,
    }


def _evaluate_case_with_rule_check(
    *,
    case: Mapping[str, Any],
    case_id: str,
) -> dict[str, Any]:
    """Evaluate one case via :func:`rule_checks.execute_check`.

    The case's ``checks`` field is a list of
    ``{name, pattern}`` patterns. The F1 engine materialises
    the patterns as a real :class:`CheckBoundary` whose
    command is a small Python check script, and the case's
    ``expected_output`` (when present) is written to the
    per-case workspace as the data the check script
    inspects. A case missing ``expected_output`` is BLOCKED:
    the engine has nothing to grep against, and a silently
    passing case would mask a real fixture gap (per the
    spec-reviewer's "BLOCKED with evidence, not pass" rule).
    """
    checks = case.get("checks")
    if not isinstance(checks, list) or not checks:
        return {
            "case_id": case_id,
            "evaluator": "rule_check",
            "status": REVIEW_CASE_STATUS_BLOCKED,
            "blockers": [{
                "id": REVIEW_CASE_NO_CHECKS_NO_JUDGMENT_BLOCKER,
                "message": (
                    f"case {case_id!r} ``checks`` is empty or "
                    "not a list; the F1 deterministic path "
                    "requires at least one pattern"
                ),
            }],
            "evidence": None,
        }
    expected_output = case.get("expected_output")
    if not isinstance(expected_output, str):
        return {
            "case_id": case_id,
            "evaluator": "rule_check",
            "status": REVIEW_CASE_STATUS_BLOCKED,
            "blockers": [{
                "id": EXECUTION_BLOCKED_MISSING_EXPECTED_OUTPUT,
                "message": (
                    f"case {case_id!r} has no ``expected_output`` "
                    "fixture; the F1 deterministic check engine "
                    "cannot evaluate a case without output to "
                    "check against (ADR 0010 / Issue #29 spec "
                    "review)"
                ),
            }],
            "evidence": None,
        }

    # Set up a per-case workspace and materialise the
    # case's data. The parent workspace is a per-review
    # temp dir (not the user's workspace, so we never
    # mutate the workspace-side tree).
    parent = _review_per_case_parent()
    plan = _rule_checks.plan_check_workspace(parent, case_id)
    if not plan.get("ok"):
        return {
            "case_id": case_id,
            "evaluator": "rule_check",
            "status": REVIEW_CASE_STATUS_BLOCKED,
            "blockers": list(plan.get("blockers") or []),
            "evidence": None,
        }
    case_workspace = Path(plan["workspace"])
    case_workspace.mkdir(parents=True, exist_ok=True)

    patterns_payload = [
        {
            "name": c.get("name") if isinstance(c, Mapping) else None,
            "pattern": c.get("pattern") if isinstance(c, Mapping) else None,
        }
        for c in checks
        if isinstance(c, Mapping)
    ]
    (case_workspace / "_expected_output.txt").write_text(
        expected_output, encoding="utf-8"
    )
    (case_workspace / "_patterns.json").write_text(
        json.dumps(patterns_payload), encoding="utf-8"
    )
    (case_workspace / "_check.py").write_text(
        _DETERMINISTIC_CHECK_SCRIPT, encoding="utf-8"
    )

    boundary = _rule_checks.CheckBoundary(
        commands=((sys.executable, "_check.py"),),
    )
    result = _rule_checks.execute_check(
        boundary, case_workspace, index=0, timeout=15.0
    )

    if result.get("ok") is True:
        return {
            "case_id": case_id,
            "evaluator": "rule_check",
            "status": REVIEW_CASE_STATUS_PASS,
            "blockers": [],
            "evidence": {
                "returncode": result.get("returncode"),
                "actual_cwd": result.get("actual_cwd"),
            },
        }
    if result.get("blockers"):
        return {
            "case_id": case_id,
            "evaluator": "rule_check",
            "status": REVIEW_CASE_STATUS_BLOCKED,
            "blockers": list(result["blockers"]),
            "evidence": {
                "returncode": result.get("returncode"),
                "actual_cwd": result.get("actual_cwd"),
            },
        }
    return {
        "case_id": case_id,
        "evaluator": "rule_check",
        "status": REVIEW_CASE_STATUS_FAIL,
        "blockers": [],
        "evidence": {
            "returncode": result.get("returncode"),
            "stderr": (result.get("stderr") or "").strip(),
            "stdout": (result.get("stdout") or "").strip(),
        },
    }


def _evaluate_case_with_judgment(
    *,
    case: Mapping[str, Any],
    case_id: str,
) -> dict[str, Any]:
    """Evaluate one case via the two-judge LLM path (ADR 0010).

    The F1 review does not own a provider config; the
    control-plane provider selection is out of scope for
    the diagnostic. When no provider is configured, the
    two-judge evaluator returns ``ok=False`` with the
    stable :data:`metacrucible.provider_config.
    JUDGE_EVALUATOR_BLOCKER` id; the F1 path translates
    that to its own :data:
    `REVIEW_CASE_JUDGE_PROVIDER_UNAVAILABLE_BLOCKER` id
    so the receipt surfaces the F1 path while the
    underlying cause (no provider) is still in the
    evidence. A future issue can wire a real provider
    selection through ``config``; the F1 path is the
    integration point.
    """
    judgment = case.get("judgment")
    rubric = (
        judgment.get("rubric", {}) if isinstance(judgment, Mapping) else {}
    )
    pass_condition = (
        judgment.get("pass_condition")
        if isinstance(judgment, Mapping)
        else None
    )

    # The MVP review path has no provider config: an empty
    # config makes the judge evaluator refuse the call with
    # JUDGE_EVALUATOR_BLOCKER. We surface that as the F1
    # ``judge-provider-unavailable`` BLOCKED condition.
    try:
        from .provider_config import run_judge_evaluator
    except ImportError:
        return {
            "case_id": case_id,
            "evaluator": "judge",
            "status": REVIEW_CASE_STATUS_BLOCKED,
            "blockers": [{
                "id": REVIEW_CASE_JUDGE_PROVIDER_UNAVAILABLE_BLOCKER,
                "message": (
                    f"case {case_id!r} ``judgment`` requested but "
                    "the provider_config module is not "
                    "importable from the F1 review path"
                ),
            }],
            "evidence": None,
        }
    judge_result = run_judge_evaluator(
        config={},
        trajectory_digest={"steps": []},
        rubric=rubric,
        call_fns=[_stub_judge_call, _stub_judge_call],
    )
    if not judge_result.get("ok"):
        return {
            "case_id": case_id,
            "evaluator": "judge",
            "status": REVIEW_CASE_STATUS_BLOCKED,
            "blockers": [{
                "id": REVIEW_CASE_JUDGE_PROVIDER_UNAVAILABLE_BLOCKER,
                "message": (
                    f"case {case_id!r} ``judgment`` requested but "
                    "no control-plane judge provider is configured "
                    "for the F1 review path; the two-judge "
                    "evaluator refuses the call (ADR 0010). "
                    "Original cause: "
                    f"{(judge_result.get('blockers') or [{}])[0].get('message', '')}"
                ),
            }],
            "evidence": {
                "judge_evidence": judge_result.get("judge_evidence") or {},
            },
        }
    return {
        "case_id": case_id,
        "evaluator": "judge",
        "status": REVIEW_CASE_STATUS_PASS,
        "blockers": [],
        "evidence": {
            "judge_evidence": judge_result.get("judge_evidence") or {},
            "pass_condition": pass_condition,
        },
    }


def _stub_judge_call(*args: Any, **kwargs: Any) -> Any:
    """Placeholder judge callable for the F1 review path.

    The F1 review diagnostic is not configured to call a
    real provider; the two-judge evaluator's no-provider
    path rejects the call before either callable is
    invoked. The stub exists only to satisfy the
    two-distinct-callables contract enforced by
    :func:`run_judge_evaluator` so the refusal is the
    determinate, well-typed outcome.
    """
    return None


_REVIEW_PER_CASE_PARENT: Path | None = None


def _review_per_case_parent() -> Path:
    """Return (and lazily create) the per-review per-case parent dir.

    The F1 deterministic check engine needs a per-case
    workspace. The parent lives under the system temp
    tree (never the user's workspace, so the workspace
    ``.metacrucible/`` invariant is preserved) and is
    reused across cases in a single review so the
    orchestrator can inspect it during testing. The
    directory is created on first call and re-used for
    the duration of the process; the existing
    :func:`rule_checks.execute_check` cleans up after
    itself per case (subprocess exit; no in-process
    state).
    """
    global _REVIEW_PER_CASE_PARENT
    if _REVIEW_PER_CASE_PARENT is None:
        import tempfile
        _REVIEW_PER_CASE_PARENT = Path(
            tempfile.mkdtemp(prefix="metacrucible-review-")
        )
    return _REVIEW_PER_CASE_PARENT


#: Python script written to each per-case workspace by
#: :func:`_evaluate_case_with_rule_check`. The script reads
#: the case's pattern list and expected output, then exits
#: 0 if every pattern is found in the output and 1
#: otherwise. The script is intentionally tiny (no
#: third-party imports) so the F1 path does not depend on
#: anything beyond the Python standard library.
_DETERMINISTIC_CHECK_SCRIPT: str = (
    "import json\n"
    "import sys\n"
    "\n"
    "patterns_file = '_patterns.json'\n"
    "output_file = '_expected_output.txt'\n"
    "\n"
    "with open(patterns_file, encoding='utf-8') as f:\n"
    "    patterns = json.load(f)\n"
    "with open(output_file, encoding='utf-8') as f:\n"
    "    output = f.read()\n"
    "\n"
    "failed = []\n"
    "for entry in patterns:\n"
    "    if not isinstance(entry, dict):\n"
    "        continue\n"
    "    name = entry.get('name', '?')\n"
    "    pattern = entry.get('pattern') or ''\n"
    "    if pattern and pattern in output:\n"
    "        continue\n"
    "    failed.append(name)\n"
    "\n"
    "if failed:\n"
    "    print(f'FAILED: {failed}', file=sys.stderr)\n"
    "    sys.exit(1)\n"
    "sys.exit(0)\n"
)


def _write_review_execution_blocked_bundle(
    *,
    run_id: str,
    blockers: list[dict[str, Any]],
    execution_evaluation: dict[str, Any],
) -> Path | None:
    """Emit the ADR 0035 ``review_execution_requested`` BLOCKED bundle.

    The standard static-review receipt/summary/digest is
    always written by :func:`_run_static_review`. When the
    execution branch is BLOCKED, a sibling minimal
    ``BLOCKED`` bundle is written via
    :func:`metacrucible.blocked_bundles.write_blocked_bundle`
    so the receipt lineage carries a "we could not proceed"
    record tagged with the
    :data:`REVIEW_EXECUTION_REQUESTED_BLOCKED_CATEGORY`
    category (ADR 0035). The BLOCKED bundle's run id is the
    static review's run id plus a
    :data:`REVIEW_BLOCKED_BUNDLE_RUN_ID_SUFFIX` suffix so
    the two bundles live in sibling directories under
    ``$HOME/.metacrucible/evidence/`` and a downstream
    reader can see both.

    The bundle is best-effort: a write failure is logged
    to stderr and the function returns ``None``; the
    in-memory review payload still carries the BLOCKED
    verdict and the caller (``cmd_review``) still maps
    the verdict to :data:`EXIT_BLOCKED`. The BLOCKED
    bundle is the *evidence* of the BLOCKED verdict, not
    the source of truth; the in-memory payload wins.
    """
    try:
        global_store = UserGlobalStorage()
        blocked_run_id = (
            f"{run_id}{REVIEW_BLOCKED_BUNDLE_RUN_ID_SUFFIX}"
        )
        identities: dict[str, Any] = {
            "review_run_id": run_id,
            "execution_evaluation": {
                "status": execution_evaluation.get("status"),
                "skipped_reason": execution_evaluation.get(
                    "skipped_reason"
                ),
                "cases_evaluated": execution_evaluation.get(
                    "cases_evaluated"
                ),
                "cases_passed": execution_evaluation.get(
                    "cases_passed"
                ),
                "cases_failed": execution_evaluation.get(
                    "cases_failed"
                ),
            },
        }
        bundle = write_blocked_bundle(
            global_store,
            run_id=blocked_run_id,
            run_type=REVIEW_EXECUTION_REQUESTED_BLOCKED_CATEGORY,
            blockers=blockers,
            identities=identities,
        )
        return bundle
    except Exception as exc:  # noqa: BLE001
        print(
            f"metacrucible: failed to write review BLOCKED "
            f"bundle: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


def _run_review(
    *,
    artifact_path: Path,
    workspace: Path,
) -> dict[str, Any]:
    """Run the F1 ``review`` orchestrator (Issue #29).

    The orchestrator composes three layers into a single
    review payload:

      1. **Static Review** (always) — runs the existing
         :func:`_run_static_review` pipeline. The Darwin
         9-dimension rubric is sourced separately via
         :func:`evaluate_darwin_skill_quality` so the F1 output
         can surface the per-dimension scores and the weakest
         dimensions (PRD F1).
      2. **Benchmark discovery + eligibility gate** — partitions
         the workspace's ``benchmark.jsonl`` into the four
         ADR 0029 buckets. A missing file, an empty benchmark,
         and a present-but-invalid benchmark are three distinct
         states; the orchestrator never silently collapses them.
      3. **Execution Evaluation** (conditional) — runs the F1
         diagnostic when eligible reviewed cases are present.
         Generated / disabled cases are never run. Skipped
         execution is reported with a stable ``skipped_reason``
         code; blocked execution carries the loader-supplied
         blockers.

    The source artifact is read once and never written to. The
    evidence bundle (receipt / summary / trajectory digest) is
    written to the user-global store via the existing v1
    writers (ADR 0030). The receipt's ``run_type`` is
    :data:`REVIEW_RUN_TYPE` so F1 review bundles are
    distinguishable from the older ``init --review`` bundles
    (``run_type = "init-review"``).

    Returns a JSON-serializable dict whose top-level keys are
    stable: ``status``, ``artifact_path``, ``artifact_kind``,
    ``workspace``, ``benchmark``, ``static_review``,
    ``execution_evaluation``, ``warnings``, ``blockers``,
    ``receipt_path``, ``summary_path``,
    ``trajectory_digest_path``.
    """
    from .profiles import (
        DARWIN_SKILL_QUALITY_ID,
        ProfileResult,
        evaluate_darwin_skill_quality,
        weakest_darwin_dimensions,
    )

    # 1. Read the source bytes (never mutated).
    source_bytes = artifact_path.read_bytes()
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"artifact {artifact_path} is not valid UTF-8: {exc}"
        ) from exc

    # 2. Parse the artifact (subagent-first, then Skill).
    kind, parsed = _parse_artifact_source(
        source_text, artifact_path=artifact_path
    )

    # 3. Run Static Review (existing pipeline) — always. The
    #    F1 review writes a distinct evidence bundle (run_type
    #    ``review``, run_id prefix ``review-``) so the receipt
    #    lineage is separable from the older ``init --review``
    #    tracer-bullet bundles (run_type ``init-review``).
    static_report = _run_static_review(
        workspace=workspace,
        artifact_path=artifact_path,
        run_id_prefix=REVIEW_RUN_TYPE,
        run_type=REVIEW_RUN_TYPE,
    )

    # 4. Build the per-dimension Darwin result. The MVP evaluator
    #    in profiles.py is a body-agnostic placeholder; downstream
    #    tooling that needs a real score must replace it. The
    #    review output surfaces the dimension ids and the weakest
    #    dimensions so the rubric is observable end-to-end.
    body = parsed.body
    if hasattr(parsed, "frontmatter") and isinstance(
        parsed.frontmatter, dict
    ):
        system_prompt = parsed.frontmatter.get("systemPrompt")
        if isinstance(system_prompt, str) and system_prompt:
            body = system_prompt + "\n" + body
    darwin_input: dict[str, Any] = {
        "body": body,
        "portability": {"target": "runtime_neutral"},
        "reviewed_fake_secrets": (),
    }
    darwin_result = evaluate_darwin_skill_quality(darwin_input)
    if not isinstance(darwin_result, ProfileResult):
        # Defensive: the MVP evaluator always returns a real
        # ProfileResult. A future rewrite must keep the contract
        # so the F1 review output remains a stable shape.
        raise ValueError(
            "evaluate_darwin_skill_quality must return a "
            "ProfileResult; got "
            f"{type(darwin_result).__name__}"
        )
    if darwin_result.profile_id != DARWIN_SKILL_QUALITY_ID:
        raise ValueError(
            "evaluate_darwin_skill_quality must return a result "
            "with profile_id=darwin-skill-quality; got "
            f"{darwin_result.profile_id!r}"
        )
    darwin_dim_scores = [dict(s) for s in darwin_result.dimension_scores]
    weakest_dimensions = [
        dict(entry)
        for entry in weakest_darwin_dimensions(darwin_result, n=3)
    ]

    # 5. Discover the benchmark state (presence + eligibility).
    benchmark_state = _discover_benchmark_state(workspace)

    # 6. Run the F1 Execution Evaluation diagnostic.
    execution_evaluation = _run_execution_evaluation(
        benchmark_state=benchmark_state
    )

    # 7. Compose warnings and blockers. The F1 contract is
    #    "review succeeds with a warning" when static review
    #    passes and no reviewed benchmark is present.
    warnings: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = list(static_report["blockers"])

    if execution_evaluation["skipped"]:
        # Skipped execution is a warning, not a blocker: F1
        # acceptance pins the static+warning path as a success
        # outcome. The warning id is the machine-stable contract;
        # the message is human English prose.
        warnings.append(
            {
                "id": NO_REVIEWED_BENCHMARK_WARNING,
                "message": (
                    "Execution Evaluation was skipped because no "
                    "reviewed Benchmark was found at the workspace "
                    "root; Static Review still completed "
                    f"({execution_evaluation['skipped_reason']})."
                ),
            }
        )
    elif execution_evaluation["status"] == EXECUTION_STATUS_BLOCKED:
        # A present-but-invalid benchmark, a benchmark missing
        # required reviewed cases, or a per-case engine
        # BLOCKED outcome is a precondition failure: the
        # execution branch cannot proceed, so the blockers
        # surface to the operator. Per ADR 0035, a minimal
        # ``BLOCKED`` bundle is written for the
        # ``review_execution_requested`` category so the
        # review lineage carries the "we could not proceed"
        # record alongside the standard static-review receipt.
        blockers.extend(execution_evaluation["blockers"])
        _write_review_execution_blocked_bundle(
            run_id=static_report["run_id"],
            blockers=execution_evaluation["blockers"],
            execution_evaluation=execution_evaluation,
        )
    elif execution_evaluation["status"] == EXECUTION_STATUS_FAIL:
        # Execution ran end-to-end but at least one case
        # FAILED with no BLOCKED cases mixed in. The review
        # verdict is FAILED; the standard static-review
        # receipt still carries status=BLOCKED so the
        # receipt lineage is consistent (the overall
        # ``status`` field is the operator-facing verdict,
        # the receipt ``status`` reflects the worst of
        # static + execution). No BLOCKED bundle is written
        # because the run executed; the FAILED outcome is
        # not a "we could not proceed" condition.
        warnings.append(
            {
                "id": "execution-evaluation-failed",
                "message": (
                    "Execution Evaluation ran but at least one "
                    "case FAILED; Static Review passed. "
                    f"(failed={execution_evaluation['cases_failed']}, "
                    f"passed={execution_evaluation['cases_passed']})."
                ),
            }
        )

    # 8. Decide the overall review status. Static blockers,
    #    execution-blocked benchmark, or execution-FAILED all
    #    move the review away from the canonical PASS path.
    #    Static blockers or execution BLOCKED flip the review
    #    to BLOCKED (precondition failure); execution FAILED
    #    with a static pass flips the review to FAILED (the
    #    execution ran and gave a verdict, not a precondition
    #    failure). Skipped execution with a static pass is the
    #    canonical F1 success (static + warning).
    if blockers:
        overall_status = "BLOCKED"
    elif execution_evaluation["status"] == EXECUTION_STATUS_FAIL:
        overall_status = "FAILED"
    else:
        overall_status = "PASS"

    # 9. Compose the static_review sub-section. The blockers /
    #    supplemental findings are sourced from the existing
    #    static report; the Darwin dimensions are sourced from
    #    the separate Darwin profile run so the rubric output
    #    is independent of the static-review verdict.
    static_review_section: dict[str, Any] = {
        "profiles_run": list(static_report["profile_ids"]),
        "blockers": list(static_report["blockers"]),
        "supplemental_findings": list(
            static_report["supplemental_findings"]
        ),
        "darwin_dimensions": darwin_dim_scores,
        "weakest_dimensions": weakest_dimensions,
    }

    return {
        "status": overall_status,
        "artifact_path": str(artifact_path),
        "artifact_kind": kind,
        "workspace": str(workspace),
        "benchmark": benchmark_state,
        "static_review": static_review_section,
        "execution_evaluation": execution_evaluation,
        "warnings": warnings,
        "blockers": blockers,
        "receipt_path": static_report["receipt_path"],
        "summary_path": static_report["summary_path"],
        "trajectory_digest_path": static_report[
            "trajectory_digest_path"
        ],
    }


def cmd_promote(args: argparse.Namespace) -> int:
    """Run the ``promote`` subcommand; return the process exit code."""
    workspace = Path(args.workspace).resolve()
    benchmark = workspace / BENCHMARK_FILE_NAME
    result = promote_case(
        benchmark,
        case_id=args.case_id,
        split=args.split,
        reviewed_by=args.reviewed_by,
        review_note=args.review_note,
        reviewed_at=_now_iso(),
        dry_run=not args.apply,
    )
    _emit(result, as_json=args.json)
    return EXIT_OK if not result["blockers"] else EXIT_BLOCKED


def _build_bootstrap_case(
    *,
    case_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Build a single draft case record for the bootstrap flow.

    The shape follows the existing ``_case_record`` convention
    used by :mod:`metacrucible.benchmark` and the
    :mod:`tests.test_promote_command` test fixtures: a
    ``record_type`` discriminator, an ADR 0029 status of
    :data:`STATUS_GENERATED`, a ``split`` of ``None`` so promote
    can assign it later, an empty ``checks`` list, a ``None``
    ``judgment``, and a placeholder ``input`` string the human
    reviewer replaces with the real scenario before promoting.

    The :data:`BOOTSTRAP_PENDING_REVIEW_FIELD` sentinel is the
    machine contract the ``optimize`` gate keys off; promote
    pops it (via :func:`metacrucible.promote.promote_case`) so a
    reviewed case never carries the sentinel forward.
    """
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": STATUS_GENERATED,
        "split": None,
        "input": BOOTSTRAP_DRAFT_INPUT,
        "checks": [],
        "judgment": None,
        "created_at": created_at,
        BOOTSTRAP_PENDING_REVIEW_FIELD: True,
    }


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Run the ``bootstrap`` subcommand; return the process exit code.

    The contract (PRD F2 / Issue #30):

      - Resolves the workspace path and reads the existing
        ``benchmark.jsonl`` via :func:`_read_benchmark_records`.
        A missing benchmark file is a BLOCKED precondition
        (``bootstrap-missing-benchmark``); the bootstrap command
        does not silently create the container so the operator
        is forced through ``init`` first.
      - Generates ``args.case_count`` draft case records with
        :data:`STATUS_GENERATED` status, a unique
        ``case_id`` of the form ``bootstrap-<8-hex>`` derived
        from :func:`uuid.uuid4`, and the
        :data:`BOOTSTRAP_PENDING_REVIEW_FIELD` sentinel set to
        ``True`` (per Issue #30 AC2).
      - Appends the new records to the existing record list and
        writes the file atomically via the existing
        :func:`metacrucible.promote._atomic_write_jsonl` helper
        so a crash mid-write cannot leave a half-written
        benchmark.
      - Records a ``cases_bootstrapped`` history event on the
        workspace's ``history.jsonl`` (per ADR 0016) so the
        audit lineage carries the bootstrap provenance.
      - Emits the result via :func:`_emit` and returns the
        stable :data:`EXIT_OK` exit code on success or
        :data:`EXIT_BLOCKED` when the benchmark is missing or
        the requested case count is non-positive.
    """
    workspace = Path(args.workspace).resolve()
    benchmark = workspace / BENCHMARK_FILE_NAME
    blockers: list[dict[str, Any]] = []
    if not benchmark.is_file():
        blockers.append(
            {
                "id": BOOTSTRAP_MISSING_BENCHMARK_BLOCKER,
                "message": (
                    f"benchmark file {benchmark} does not exist; "
                    f"run `metacrucible init {workspace}` first "
                    f"to create the empty benchmark container"
                ),
            }
        )
    case_count = int(getattr(args, "case_count", BOOTSTRAP_DEFAULT_CASE_COUNT))
    if case_count <= 0:
        blockers.append(
            {
                "id": BOOTSTRAP_INVALID_CASE_COUNT_BLOCKER,
                "message": (
                    f"--case-count must be a positive integer; got "
                    f"{case_count!r}"
                ),
            }
        )
    if blockers:
        payload: dict[str, Any] = {
            "workspace": str(workspace),
            "benchmark": str(benchmark),
            "case_count": case_count,
            "generated_case_ids": [],
            "blockers": blockers,
        }
        _emit(payload, as_json=args.json)
        return EXIT_BLOCKED

    records = _read_benchmark_records(benchmark)
    now = _now_iso()
    generated_case_ids: list[str] = []
    new_records: list[dict[str, Any]] = []
    for _ in range(case_count):
        case_id = f"bootstrap-{uuid.uuid4().hex[:8]}"
        generated_case_ids.append(case_id)
        new_records.append(
            _build_bootstrap_case(case_id=case_id, created_at=now)
        )

    merged = list(records) + new_records
    _atomic_write_jsonl(benchmark, merged)
    RepositoryStorage(workspace).append_history(
        {
            "event": "cases_bootstrapped",
            "case_count": case_count,
            "case_ids": list(generated_case_ids),
            "created_at": now,
        }
    )

    payload = {
        "workspace": str(workspace),
        "benchmark": str(benchmark),
        "case_count": case_count,
        "generated_case_ids": generated_case_ids,
        "sentinel": BOOTSTRAP_PENDING_REVIEW_FIELD,
        "blockers": [],
    }
    _emit(payload, as_json=args.json)
    return EXIT_OK


def _write_optimize_blocked_bundle(
    *, blockers: list[dict[str, Any]]
) -> None:
    """Emit the ADR 0035 ``optimize`` BLOCKED evidence bundle.

    Best-effort: a write failure is logged to stderr and the
    in-memory payload still carries the BLOCKED verdict, so
    the caller (:func:`cmd_optimize`) still returns the
    :data:`EXIT_BLOCKED` exit code. The BLOCKED bundle is
    the *evidence* of the BLOCKED verdict, not the source
    of truth; the in-memory payload wins.

    Mirrors :func:`_write_evaluate_blocked_bundle` /
    :func:`_write_baseline_blocked_bundle` so the four
    BLOCKED-emitting commands share a single, predictable
    write contract.
    """
    try:
        global_store = UserGlobalStorage()
        run_id = (
            f"{OPTIMIZE_BLOCKED_BUNDLE_RUN_ID_PREFIX}-"
            f"{_now_iso().replace(':', '').replace('-', '')}"
        )
        write_blocked_bundle(
            global_store,
            run_id=run_id,
            run_type=OPTIMIZE_BLOCKED_BUNDLE_RUN_TYPE,
            blockers=blockers,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"metacrucible: failed to write optimize BLOCKED "
            f"bundle: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def cmd_optimize(args: argparse.Namespace) -> int:
    """Run the ``optimize`` subcommand; return the process exit code.

    Issue #33 / PRD F3: replace the MVP sentinel gate with the
    full SkillOpt-shaped optimizer pipeline (ADR 0032 /
    ADR 0022). The command is a thin wrapper around
    :func:`metacrucible.optimizer.run_optimizer_pipeline`
    that:

      - Resolves the workspace, benchmark path, and the
        envelope-declared ``artifact_path``. A missing
        workspace, benchmark, or artifact is BLOCKED with
        the canonical loader blocker ids; the optimize
        command never invents a verdict the loader did
        not already explain.
      - Propagates the loader's blockers and the
        bootstrap-sentinel blocker so a generated case or
        ``BOOTSTRAP_PENDING_REVIEW`` flag still refuses
        the start (the existing test contract).
      - Threads ``--max-rounds`` and ``--confirm-routing``
        through to the pipeline.
      - Emits the human / ``--json`` payload per the
        :func:`cmd_optimize` output contract (status,
        rounds, decision, blockers, warnings, record
        counts, evidence refs, artifact path, best
        revision). ``EXIT_OK`` for accepted / no-blocking
        outcomes; ``EXIT_BLOCKED`` otherwise.
      - On a precondition failure (loader blocker /
        bootstrap sentinel / missing artifact), writes
        the ADR 0035 ``optimize`` BLOCKED evidence bundle
        so the receipt lineage carries the "we could not
        proceed" record.

    The command never mutates the benchmark file. The
    artifact is mutated *only* when the pipeline accepts a
    candidate; the run-level rollback restores the base
    bytes on every non-accepted outcome (no automatic git
    commits, per PRD F3 / Issue #33).
    """
    from .benchmark import load_benchmark
    from .optimizer import _resolve_artifact_path

    workspace = Path(args.workspace).resolve()
    benchmark = workspace / BENCHMARK_FILE_NAME
    as_json = bool(getattr(args, "json", False))
    max_rounds = int(
        getattr(args, "max_rounds", ROUND_BUDGET_DEFAULT)
    )
    if max_rounds < 1:
        max_rounds = 1
    human_confirmed = bool(getattr(args, "confirm_routing", False))

    # Precondition 1: workspace must be an existing directory.
    if not workspace.is_dir():
        blockers: list[dict[str, Any]] = [
            {
                "id": "optimize-workspace-missing",
                "message": (
                    f"workspace {workspace} does not exist or "
                    f"is not a directory; run ``metacrucible init "
                    f"{workspace}`` first"
                ),
            }
        ]
        _write_optimize_blocked_bundle(blockers=blockers)
        _emit(
            {
                "status": "BLOCKED",
                "workspace": str(workspace),
                "benchmark": str(benchmark),
                "rounds": 0,
                "blockers": blockers,
            },
            as_json=as_json,
        )
        return EXIT_BLOCKED

    # Precondition 2: benchmark file must be present.
    if not benchmark.is_file():
        blockers = [
            {
                "id": "missing-reviewed-eval-case",
                "message": (
                    "no eligible reviewed eval cases (ADR 0025)"
                ),
            },
            {
                "id": "missing-reviewed-held-out-case",
                "message": (
                    "no eligible reviewed held-out cases (ADR 0025)"
                ),
            },
        ]
        _write_optimize_blocked_bundle(blockers=blockers)
        _emit(
            {
                "status": "BLOCKED",
                "workspace": str(workspace),
                "benchmark": str(benchmark),
                "rounds": 0,
                "blockers": blockers,
            },
            as_json=as_json,
        )
        return EXIT_BLOCKED

    # Precondition 3: loader-level blockers / bootstrap
    # sentinel. The loader partitions the cases; we
    # propagate its blockers verbatim and add the
    # bootstrap-pending-review blocker when the literal
    # sentinel is present (per the existing Issue #30 AC3
    # contract).
    result = load_benchmark(benchmark)
    blockers = [
        dict(b) for b in result.blockers
        if isinstance(b, dict) and isinstance(b.get("id"), str)
    ]
    pending_review_case_ids: list[str] = []
    for case in result.cases:
        if not isinstance(case, dict):
            continue
        if case.get(BOOTSTRAP_PENDING_REVIEW_FIELD) is True:
            cid = case.get("case_id")
            if isinstance(cid, str) and cid:
                pending_review_case_ids.append(cid)
    if pending_review_case_ids:
        blockers.append(
            {
                "id": "bootstrap-pending-review",
                "message": (
                    f"{len(pending_review_case_ids)} bootstrap-"
                    "generated case(s) still carry the "
                    "pending-review sentinel; promote them (or "
                    "remove the sentinel) before optimizing. "
                    f"case_ids={pending_review_case_ids!r}"
                ),
            }
        )

    if not result.is_optimize_runnable or pending_review_case_ids:
        _write_optimize_blocked_bundle(blockers=blockers)
        _emit(
            {
                "status": "BLOCKED",
                "workspace": str(workspace),
                "benchmark": str(benchmark),
                "is_optimize_runnable": bool(
                    result.is_optimize_runnable
                    and not pending_review_case_ids
                ),
                "pending_review_case_ids": pending_review_case_ids,
                "rounds": 0,
                "blockers": blockers,
            },
            as_json=as_json,
        )
        return EXIT_BLOCKED

    # Precondition 4: envelope-declared artifact_path. The
    # optimizer cannot run without an artifact to
    # optimize; the contract is the same as baseline
    # create (OD1).
    artifact_path_value = _resolve_artifact_path(workspace)
    if artifact_path_value is None:
        blockers = [
            {
                "id": "optimize-artifact-unresolved",
                "message": (
                    f"envelope at {workspace / '.metacrucible' / 'envelope.json'} "
                    "does not declare an ``artifact_path`` (or "
                    "``canonical_source``) field; the optimizer "
                    "refuses to scan / glob the workspace"
                ),
            }
        ]
        _write_optimize_blocked_bundle(blockers=blockers)
        _emit(
            {
                "status": "BLOCKED",
                "workspace": str(workspace),
                "benchmark": str(benchmark),
                "rounds": 0,
                "blockers": blockers,
            },
            as_json=as_json,
        )
        return EXIT_BLOCKED

    artifact_path = Path(artifact_path_value).resolve()
    if not artifact_path.is_file():
        blockers = [
            {
                "id": "optimize-artifact-missing",
                "message": (
                    f"artifact {artifact_path} does not exist; "
                    "the envelope declared a path that is not "
                    "on disk"
                ),
            }
        ]
        _write_optimize_blocked_bundle(blockers=blockers)
        _emit(
            {
                "status": "BLOCKED",
                "workspace": str(workspace),
                "benchmark": str(benchmark),
                "artifact_path": str(artifact_path),
                "rounds": 0,
                "blockers": blockers,
            },
            as_json=as_json,
        )
        return EXIT_BLOCKED

    # Dirty-file guard (mirrors baseline create). The optimize
    # inputs (artifact, envelope, benchmark) are not considered
    # "unrelated" so a freshly-modified artifact does not block
    # itself. Everything else triggers BLOCK unless
    # ``--allow-dirty-unrelated`` is set. A workspace outside a
    # git worktree skips the guard with a stderr warning (per
    # OD3): the command cannot enforce a commit-before-optimize
    # policy outside git's purview.
    optimize_input_paths: list[Path] = [
        Path(artifact_path_value),
        Path(".metacrucible/envelope.json"),
        Path(BENCHMARK_FILE_NAME),
    ]
    unrelated_dirty, dirty_paths, is_worktree = git_dirty_check(
        workspace, optimize_input_paths
    )
    if not is_worktree:
        # Workspace is not a git worktree; skip the dirty
        # guard with a one-line English warning so the
        # operator sees the silent-skip. We do not BLOCK.
        print(
            "metacrucible: warning: workspace is not a git "
            "worktree; dirty-file guard skipped (Issue #31 "
            "OD3)",
            file=sys.stderr,
        )
    elif unrelated_dirty and not args.allow_dirty_unrelated:
        blockers = [
            {
                "id": OPTIMIZE_UNRELATED_DIRTY_FILES_BLOCKER,
                "message": (
                    f"workspace has dirty files unrelated to the "
                    f"optimize inputs (artifact, envelope, "
                    f"benchmark); pass ``--allow-dirty-unrelated`` "
                    f"to record the dirty file list and proceed. "
                    f"dirty_files={dirty_paths!r}"
                ),
            }
        ]
        _write_optimize_blocked_bundle(blockers=blockers)
        _emit(
            {
                "status": "BLOCKED",
                "workspace": str(workspace),
                "benchmark": str(benchmark),
                "artifact_path": str(artifact_path),
                "dirty_files_at_run": list(dirty_paths),
                "allow_dirty_unrelated": bool(
                    args.allow_dirty_unrelated
                ),
                "rounds": 0,
                "blockers": blockers,
            },
            as_json=as_json,
        )
        return EXIT_BLOCKED

    # Run the pipeline. The CLI passes ``call_fn=None``;
    # tests monkey-patch ``run_optimizer_pipeline`` to
    # inject a deterministic fake. The pipeline is
    # bounded by ``max_rounds``; the CLI never loops
    # forever.
    pipeline_result: OptimizerPipelineResult = run_optimizer_pipeline(
        workspace=workspace,
        benchmark_path=benchmark,
        artifact_path=artifact_path,
        call_fn=None,
        max_rounds=max_rounds,
        human_confirmed=human_confirmed,
    )

    # Compose the OPT-8 / OPT-9 output. The status field
    # is the machine-stable verdict: ``ACCEPTED`` /
    # ``REJECTED`` / ``BLOCKED``. The CLI exit code is
    # :data:`EXIT_OK` for accepted and no-blocking
    # rejected, and :data:`EXIT_BLOCKED` for everything
    # else. The contract mirrors the central matrix in
    # :mod:`metacrucible.exit_codes`.
    record_counts = dict(pipeline_result.record_counts)
    best_revision = pipeline_result.best_revision or {}
    best_revision_summary: dict[str, Any] | None = (
        {
            "run_id": str(best_revision.get("run_id", "")),
            "round_id": str(best_revision.get("round_id", "")),
            "artifact_path": str(
                best_revision.get("artifact_path", "")
            ),
            "artifact_text_sha256": str(
                best_revision.get("artifact_text_sha256", "")
            ),
        }
        if pipeline_result.best_revision
        else None
    )
    payload: dict[str, Any] = {
        "status": pipeline_result.status,
        "workspace": str(workspace),
        "benchmark": str(benchmark),
        "artifact_path": str(artifact_path),
        "run_id": pipeline_result.run_id,
        "rounds": pipeline_result.rounds,
        "max_rounds": max_rounds,
        "record_counts": record_counts,
        "evidence_refs": dict(pipeline_result.evidence_refs),
        "blockers": list(pipeline_result.blockers),
        "warnings": list(pipeline_result.warnings),
        "acceptance_decision": dict(
            pipeline_result.acceptance_decision
        ),
        "best_revision": best_revision_summary,
        "selected_candidate_ids": list(
            pipeline_result.selected_candidate_ids
        ),
        "allow_dirty_unrelated": bool(args.allow_dirty_unrelated),
        "dirty_files_at_run": list(dirty_paths),
        "stop_reason": pipeline_result.stop_reason,
    }
    _emit(payload, as_json=as_json)

    if pipeline_result.status in ("ACCEPTED", "REJECTED"):
        # REJECTED with no blockers is the
        # "no-improvement-yet" path; the in-memory
        # payload still carries the verdict. EXIT_OK so
        # the caller can branch on the JSON ``status``
        # field, not the exit code.
        return EXIT_OK
    return EXIT_BLOCKED




def cmd_init(args: argparse.Namespace) -> int:
    """Run the ``init`` subcommand; return the process exit code."""
    workspace = Path(args.workspace).resolve()
    if args.check:
        result = _check_workspace(workspace)
        payload = {
            "workspace": str(result["workspace"]),
            "envelope_path": str(result["envelope_path"]),
            "state_path": str(result["state_path"]),
            "benchmark_path": str(result["benchmark_path"]),
            "ok": result["ok"],
            "blockers": result["blockers"],
        }
        _emit(payload, as_json=args.json)
        return EXIT_OK if result["ok"] else EXIT_BLOCKED
    # ``--no-isolation`` gate (Issue #13 AC3+AC4). The flag is a
    # safety escape hatch for callers that intentionally want to
    # skip copy-on-write masking; the gate refuses the call unless
    # the caller passed ``--confirm-no-isolation`` AND either stdin
    # is a TTY or the explicit env-var override is set. The
    # validation lives in :mod:`metacrucible.workspace_isolation`.
    if getattr(args, "no_isolation", False):
        from .workspace_isolation import validate_no_isolation

        interactive = sys.stdin.isatty()
        gate = validate_no_isolation(
            confirmed=bool(getattr(args, "confirm_no_isolation", False)),
            interactive=interactive,
        )
        if not gate["ok"]:
            payload = {
                "workspace": str(workspace),
                "ok": gate["ok"],
                "blockers": gate["blockers"],
            }
            _emit(payload, as_json=args.json)
            return EXIT_BLOCKED
    paths = _create_workspace(workspace)
    # Optional static-review tracer bullet (Issue #28). The flag is
    # opt-in so the default ``init`` contract is unchanged; when
    # set, the helper reads the artifact, parses it, runs the
    # existing static-review profiles, and writes a v1 evidence
    # bundle to the user-global store. The source artifact is
    # never written to.
    review_report: dict[str, Any] | None = None
    if getattr(args, "review_artifact", None):
        artifact_path = Path(args.review_artifact).resolve()
        review_report = _run_static_review(
            workspace=workspace,
            artifact_path=artifact_path,
        )
    # Boundary report (ADR 0031, Issue #13 AC1). When
    # ``--no-isolation`` is set the gate above has already passed,
    # so masking is intentionally skipped and the report is
    # recorded as ``masking: "skipped"`` so a reviewer can tell the
    # silent-skip from a successful plan.
    boundary_report: dict[str, Any]
    if getattr(args, "no_isolation", False):
        boundary_report = {
            "ok": True,
            "blockers": [],
            "masking": "skipped",
        }
    else:
        from .workspace_isolation import plan_workspace_mask

        boundary_report = plan_workspace_mask(workspace)
    payload = {
        "workspace": str(paths["workspace"]),
        "envelope_path": str(paths["envelope_path"]),
        "state_path": str(paths["state_path"]),
        "benchmark_path": str(paths["benchmark_path"]),
        "created": paths["created"],
        "boundary_report": boundary_report,
    }
    if review_report is not None:
        payload["review"] = review_report
    _emit(payload, as_json=args.json)
    return EXIT_OK


def cmd_review(args: argparse.Namespace) -> int:
    """Run the ``review`` subcommand; return the process exit code.

    The contract (PRD F1 / Issue #29):

      - The artifact is read once and the source bytes are
        never mutated. The pipeline writes only to the
        user-global evidence store (``$HOME/.metacrucible/``);
        the workspace side (``<workspace>/.metacrucible/``) is
        referenced by the receipt but not touched by the
        review flow.
      - Static Review always runs after the artifact parses.
        The Darwin 9-dimension rubric is exposed in the output
        even when static review is the only path that runs.
      - Execution Evaluation runs only when a reviewed
        Benchmark is present. The four ADR 0029 buckets
        (reviewed-eval / reviewed-held-out / generated /
        disabled) are partitioned; generated and disabled
        cases are never evaluated.
      - Exit codes follow the central matrix in
        :mod:`metacrucible.exit_codes`. Static success +
        execution skipped is a SUCCESS (with a warning).
        Static success + execution PASS is a SUCCESS.
        Static BLOCKED or execution BLOCKED is
        :data:`EXIT_BLOCKED`. The CLI never returns a
        raw numeric exit code; every return is a symbolic
        constant from the matrix.
    """
    artifact_path = Path(args.artifact).resolve()
    if not artifact_path.is_file():
        payload = {
            "status": "BLOCKED",
            "artifact_path": str(artifact_path),
            "error": {
                "id": "review-artifact-missing",
                "message": (
                    f"artifact {artifact_path} does not exist or "
                    "is not a regular file"
                ),
            },
        }
        _emit(payload, as_json=args.json)
        return EXIT_BLOCKED

    workspace = (
        Path(args.workspace).resolve()
        if args.workspace
        else artifact_path.parent
    )

    try:
        review = _run_review(
            artifact_path=artifact_path, workspace=workspace
        )
    except FileNotFoundError as exc:
        # The artifact path was resolved above, so a
        # FileNotFoundError past this point is unusual;
        # surface it as a BLOCKED precondition with the
        # explicit error message so the operator can act.
        payload = {
            "status": "BLOCKED",
            "artifact_path": str(artifact_path),
            "error": {
                "id": "review-artifact-missing",
                "message": str(exc),
            },
        }
        _emit(payload, as_json=args.json)
        return EXIT_BLOCKED
    except ValueError as exc:
        # Pre-pipeline failure (UTF-8 decode, malformed
        # frontmatter, neither Skill nor subagent). The
        # CLI maps the exception to EXIT_USER_ERROR so the
        # operator sees a usage-style failure rather than
        # a BLOCKED bundle (no review ran).
        payload = {
            "status": "BLOCKED",
            "artifact_path": str(artifact_path),
            "error": {
                "id": "review-artifact-invalid",
                "message": str(exc),
            },
        }
        _emit(payload, as_json=args.json)
        return EXIT_USER_ERROR

    _emit(review, as_json=args.json)
    # The F1 verdict vocabulary is PASS / FAILED / BLOCKED.
    # PASS maps to EXIT_OK. FAILED and BLOCKED both map to
    # EXIT_BLOCKED (the existing exit-code matrix does not
    # have a separate FAILED code; the negative outcome is
    # observable from the JSON ``status`` field, and the
    # ``blockers`` / ``execution_evaluation.cases_failed``
    # fields carry the detail).
    if review["status"] in ("BLOCKED", "FAILED"):
        return EXIT_BLOCKED
    return EXIT_OK


def _baseline_git_dirty_check(
    workspace: Path, baseline_inputs: list[Path]
) -> tuple[bool, list[str], bool]:
    """Thin wrapper around :func:`git_dirty_check`.

    Preserved as a private symbol so existing baseline call
    sites and any test that intentionally references the
    private name keep working. The reusable logic lives in
    :mod:`metacrucible.dirty_guard`.
    """
    return git_dirty_check(workspace, baseline_inputs)


def _write_evaluate_blocked_bundle(
    *, blockers: list[dict[str, Any]]
) -> None:
    """Emit the ADR 0035 ``evaluate`` BLOCKED evidence bundle.

    Best-effort: a write failure is logged to stderr and the
    in-memory payload still carries the BLOCKED verdict, so the
    caller (:func:`cmd_evaluate`) still returns the
    :data:`EXIT_BLOCKED` exit code. The BLOCKED bundle is the
    *evidence* of the BLOCKED verdict, not the source of
    truth; the in-memory payload wins.

    Mirrors :func:`_write_baseline_blocked_bundle` exactly so
    the four BLOCKED-emitting commands (``baseline create``,
    ``evaluate``, ``optimize``, ``synthesize evaluation
    stage``) share a single, predictable write contract.
    """
    try:
        global_store = UserGlobalStorage()
        run_id = (
            f"{EVALUATE_BLOCKED_BUNDLE_RUN_ID_PREFIX}-"
            f"{_now_iso().replace(':', '').replace('-', '')}"
        )
        write_blocked_bundle(
            global_store,
            run_id=run_id,
            run_type=EVALUATE_BLOCKED_BUNDLE_RUN_TYPE,
            blockers=blockers,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"metacrucible: failed to write evaluate BLOCKED "
            f"bundle: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _write_baseline_blocked_bundle(
    *, blockers: list[dict[str, Any]]
) -> None:
    """Emit the ADR 0035 ``baseline_create`` BLOCKED evidence bundle.

    Best-effort: a write failure is logged to stderr and the
    in-memory payload still carries the BLOCKED verdict, so the
    caller (``cmd_baseline_create``) still returns the
    :data:`EXIT_BLOCKED` exit code. The BLOCKED bundle is the
    *evidence* of the BLOCKED verdict, not the source of
    truth; the in-memory payload wins.
    """
    try:
        global_store = UserGlobalStorage()
        run_id = (
            f"{BASELINE_BLOCKED_BUNDLE_RUN_ID_PREFIX}-"
            f"{_now_iso().replace(':', '').replace('-', '')}"
        )
        write_blocked_bundle(
            global_store,
            run_id=run_id,
            run_type=BASELINE_BLOCKED_BUNDLE_RUN_TYPE,
            blockers=blockers,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"metacrucible: failed to write baseline BLOCKED "
            f"bundle: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def cmd_baseline(args: argparse.Namespace) -> int:
    """Dispatch nested ``baseline`` actions.

    The Issue #31 MVP exposes a single nested action (``create``).
    An unknown / missing action is mapped to :data:`EXIT_USER_ERROR`
    so the CLI never silently returns :data:`EXIT_OK` for an
    unrecognised command (matches the contract pinned for the
    other subcommands).
    """
    action = getattr(args, "baseline_action", None)
    if action == "create":
        return cmd_baseline_create(args)
    payload = {
        "command": "baseline",
        "baseline_action": action,
        "blockers": [
            {
                "id": "baseline-unknown-action",
                "message": (
                    f"``baseline`` requires a nested action; got "
                    f"{action!r}. Supported action: ``create``."
                ),
            }
        ],
    }
    _emit(payload, as_json=getattr(args, "json", False))
    return EXIT_USER_ERROR


def cmd_baseline_create(args: argparse.Namespace) -> int:
    """Run the ``baseline create`` subcommand; return the exit code.

    The contract (Issue #31):

      - Resolves the workspace path and reads the three
        baseline inputs (the artifact at the envelope-declared
        path, :data:`metacrucible.storage.RepositoryStorage`
        envelope, and the workspace ``benchmark.jsonl``). Each
        is hashed; the resulting four-tuple (artifact, envelope,
        benchmark, harness) plus the schema version is written
        to ``<workspace>/.metacrucible/baseline.json`` so a
        downstream reviewer can re-derive the inputs the
        baseline pinned against.
      - The artifact path is resolved from the envelope's
        ``artifact_path`` (preferred) or ``canonical_source``
        field. When neither is present, the command BLOCKS
        with the stable ``baseline-artifact-unresolved`` id
        rather than scanning / globbing the workspace (per
        OD1). The envelope, benchmark, and workspace itself
        must already exist (the baseline does not create
        any of them); missing preconditions BLOCK with
        their own stable ids.
      - The harness SHA is the
        :func:`compute_evaluation_harness_sha` digest over
        :data:`BUILTIN_PROFILES`, the stable full-harness
        snapshot (OD2). When no built-in profiles are
        available the harness field is the empty string
        so the baseline still pins the rest of the inputs
        without inventing a digest.
      - The dirty-file guard (subsumes Issue #37) reads
        ``git status --porcelain`` against the workspace.
        Baseline inputs (the artifact, envelope, benchmark)
        are not considered ``unrelated`` so a freshly-modified
        artifact at baselining time does not block; the
        ``--allow-dirty-unrelated`` flag records the dirty
        file list and proceeds, otherwise unrelated dirty
        files BLOCK with the ``baseline-unrelated-dirty-files``
        id. A workspace that is not a git worktree skips the
        guard with a stderr warning so the operator can see
        the silent-skip (per OD3).
      - The command never mutates ``envelope.json``,
        ``state.json``, ``history.jsonl``, the artifact, or
        ``benchmark.jsonl``; the only write is
        ``baseline.json`` itself. A second ``baseline create``
        is allowed (overwrite) and bumps ``created_at``.
      - On any BLOCKED outcome, a minimal ``BLOCKED`` evidence
        bundle is written via
        :func:`metacrucible.blocked_bundles.write_blocked_bundle`
        with ``run_type="baseline_create"`` per ADR 0035 (the
        matrix in :mod:`metacrucible.blocked_bundles` already
        lists ``baseline_create`` as a required emitter).
    """
    from .profiles import BUILTIN_PROFILES, compute_evaluation_harness_sha
    from .storage import compute_benchmark_digest

    workspace = Path(args.workspace).resolve()
    envelope_path = workspace / ".metacrucible" / "envelope.json"
    benchmark_path = workspace / BENCHMARK_FILE_NAME
    baseline_path = workspace / ".metacrucible" / BASELINE_FILE_NAME

    blockers: list[dict[str, Any]] = []
    artifact_path_value: str | None = None

    # Precondition 1: workspace must be an existing directory.
    # The ``init`` command creates the workspace; ``baseline
    # create`` does not, so a missing workspace is a hard
    # precondition failure.
    if not workspace.is_dir():
        blockers.append(
            {
                "id": BASELINE_WORKSPACE_MISSING_BLOCKER,
                "message": (
                    f"workspace {workspace} does not exist or is "
                    f"not a directory; run ``metacrucible init "
                    f"{workspace}`` first"
                ),
            }
        )

    # Precondition 2: ``.metacrucible/envelope.json`` must be
    # present. The envelope is the artifact identity record
    # and the source of the artifact path.
    if not envelope_path.is_file():
        blockers.append(
            {
                "id": BASELINE_ENVELOPE_MISSING_BLOCKER,
                "message": (
                    f"envelope {envelope_path} is missing; run "
                    f"``metacrucible init {workspace}`` first"
                ),
            }
        )

    # Precondition 3: ``benchmark.jsonl`` must be present at
    # the workspace root. The baseline hashes the canonical
    # payload; a missing file is a precondition failure with
    # a stable id so the operator knows exactly what is
    # missing.
    if not benchmark_path.is_file():
        blockers.append(
            {
                "id": BASELINE_BENCHMARK_MISSING_BLOCKER,
                "message": (
                    f"benchmark file {benchmark_path} is missing; "
                    f"run ``metacrucible init {workspace}`` first"
                ),
            }
        )

    # Precondition 4: the envelope must declare the artifact
    # path (``artifact_path`` preferred; ``canonical_source``
    # accepted as a synonym). We refuse to scan / glob the
    # workspace (per OD1) so a missing declaration surfaces
    # as ``baseline-artifact-unresolved`` rather than a
    # silent guess.
    if envelope_path.is_file():
        try:
            envelope_obj = json.loads(
                envelope_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            envelope_obj = None
        if isinstance(envelope_obj, dict):
            raw_ap = envelope_obj.get("artifact_path")
            raw_cs = envelope_obj.get("canonical_source")
            if isinstance(raw_ap, str) and raw_ap:
                artifact_path_value = raw_ap
            elif isinstance(raw_cs, str) and raw_cs:
                artifact_path_value = raw_cs
    if (
        not blockers
        and artifact_path_value is None
    ):
        blockers.append(
            {
                "id": BASELINE_ARTIFACT_UNRESOLVED_BLOCKER,
                "message": (
                    f"envelope {envelope_path} does not declare an "
                    f"``artifact_path`` (or ``canonical_source``) "
                    f"field; the baseline refuses to scan / glob "
                    f"the workspace"
                ),
            }
        )

    if blockers:
        # Per ADR 0035 the ``baseline_create`` category must
        # emit a BLOCKED evidence bundle so the receipt
        # lineage carries the "we could not proceed" record
        # alongside the in-memory payload. Best-effort: a
        # write failure does not change the exit code.
        _write_baseline_blocked_bundle(blockers=blockers)
        payload = {
            "status": "BLOCKED",
            "workspace": str(workspace),
            "baseline_path": str(baseline_path),
            "blockers": blockers,
        }
        _emit(payload, as_json=args.json)
        return EXIT_BLOCKED

    artifact_path = Path(artifact_path_value).resolve()

    # Dirty-file guard (subsumes Issue #37). The baseline
    # inputs (artifact, envelope, baseline.json, benchmark) are
    # not considered "unrelated" so a freshly-modified artifact
    # at baselining time does not block; ``baseline.json`` is
    # included so the overwrite path does not self-block (a
    # previously-written uncommitted ``baseline.json`` is an
    # expected dirty file when the operator re-runs the
    # command). Everything else triggers BLOCK unless
    # ``--allow-dirty-unrelated`` is set. A workspace outside
    # a git worktree skips the guard with a stderr warning
    # (per OD3): the command cannot enforce a commit-before-
    # baseline policy outside git's purview.
    baseline_input_paths: list[Path] = [
        Path(artifact_path_value),
        Path(".metacrucible/envelope.json"),
        Path(f".metacrucible/{BASELINE_FILE_NAME}"),
        Path(BENCHMARK_FILE_NAME),
    ]
    unrelated_dirty, dirty_files, is_worktree = _baseline_git_dirty_check(
        workspace, baseline_input_paths
    )
    if not is_worktree:
        # Workspace is not a git worktree; skip the dirty
        # guard with a one-line English warning so the
        # operator sees the silent-skip. We do not BLOCK.
        print(
            "metacrucible: warning: workspace is not a git "
            "worktree; dirty-file guard skipped (Issue #31 "
            "OD3)",
            file=sys.stderr,
        )
    elif unrelated_dirty and not args.allow_dirty_unrelated:
        blockers.append(
            {
                "id": BASELINE_UNRELATED_DIRTY_FILES_BLOCKER,
                "message": (
                    f"workspace has dirty files unrelated to the "
                    f"baseline inputs (artifact, envelope, "
                    f"benchmark); pass ``--allow-dirty-unrelated`` "
                    f"to record the dirty file list and proceed. "
                    f"dirty_files={dirty_files!r}"
                ),
            }
        )
        _write_baseline_blocked_bundle(blockers=blockers)
        payload = {
            "status": "BLOCKED",
            "workspace": str(workspace),
            "baseline_path": str(baseline_path),
            "dirty_files_at_creation": dirty_files,
            "allow_dirty_unrelated": bool(args.allow_dirty_unrelated),
            "blockers": blockers,
        }
        _emit(payload, as_json=args.json)
        return EXIT_BLOCKED

    # Hash the four baseline inputs. ``artifact_hash`` and
    # ``envelope_hash`` are SHA-256 of the raw file bytes;
    # ``benchmark_hash`` is the canonical-JSON digest from
    # :func:`metacrucible.storage.compute_benchmark_digest`
    # (so a whitespace-stable benchmark produces the same
    # digest across runs); ``harness_sha`` is the full-
    # harness digest over :data:`BUILTIN_PROFILES` (per OD2)
    # or the empty string when no profiles are available.
    artifact_hash = hashlib.sha256(
        artifact_path.read_bytes()
    ).hexdigest()
    envelope_hash = hashlib.sha256(
        envelope_path.read_bytes()
    ).hexdigest()
    benchmark_records = _read_benchmark_records(benchmark_path)
    benchmark_hash = compute_benchmark_digest(benchmark_records)
    harness_sha = (
        compute_evaluation_harness_sha(BUILTIN_PROFILES)
        if BUILTIN_PROFILES
        else ""
    )

    baseline_record: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "created_at": _now_iso(),
        "artifact_hash": artifact_hash,
        "envelope_hash": envelope_hash,
        "benchmark_hash": benchmark_hash,
        "harness_sha": harness_sha,
        "allow_dirty_unrelated": bool(args.allow_dirty_unrelated),
        "dirty_files_at_creation": list(dirty_files),
    }
    baseline_path.write_text(
        json.dumps(baseline_record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    payload = {
        "status": "OK",
        "workspace": str(workspace),
        "baseline_path": str(baseline_path),
        "schema_version": BASELINE_SCHEMA_VERSION,
        "created_at": baseline_record["created_at"],
        "artifact_hash": artifact_hash,
        "envelope_hash": envelope_hash,
        "benchmark_hash": benchmark_hash,
        "harness_sha": harness_sha,
        "allow_dirty_unrelated": bool(args.allow_dirty_unrelated),
        "dirty_files_at_creation": list(dirty_files),
        "git_worktree": is_worktree,
        "blockers": [],
    }
    _emit(payload, as_json=args.json)
    return EXIT_OK

def _aggregate_case_verdicts(
    case_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate per-case verdicts into the top-level execution verdict.

    Duplicates the ~15 lines of aggregation in
    :func:`_run_execution_evaluation` (the F1 review's
    execution branch) so the ``evaluate`` subcommand can
    produce a stable per-split verdict without going through
    the F1 review's static-review / execution evaluation
    composition. BLOCKED > FAILED > PASS precedence is
    preserved; per-case blockers are rolled up into the
    BLOCKED status payload so the operator sees which case
    blocked and why.
    """
    any_blocked = any(
        r["status"] == REVIEW_CASE_STATUS_BLOCKED
        for r in case_results
    )
    any_failed = any(
        r["status"] == REVIEW_CASE_STATUS_FAIL
        for r in case_results
    )
    cases_evaluated = len(case_results)
    cases_passed = sum(
        1 for r in case_results
        if r["status"] == REVIEW_CASE_STATUS_PASS
    )
    cases_failed = sum(
        1 for r in case_results
        if r["status"] == REVIEW_CASE_STATUS_FAIL
    )
    if any_blocked:
        # Roll up the per-case blockers so the operator
        # sees which case blocked and why. Mirrors the
        # rollup in :func:`_run_execution_evaluation`.
        blockers: list[dict[str, Any]] = []
        for r in case_results:
            if r["status"] != REVIEW_CASE_STATUS_BLOCKED:
                continue
            case_id = r.get("case_id") or "?"
            for blocker in r.get("blockers", []):
                if not isinstance(blocker, dict):
                    continue
                blockers.append(
                    {
                        "id": blocker.get("id", "?"),
                        "message": blocker.get("message", ""),
                        "case_id": case_id,
                    }
                )
        return {
            "status": EXECUTION_STATUS_BLOCKED,
            "cases_evaluated": cases_evaluated,
            "cases_passed": cases_passed,
            "cases_failed": cases_failed,
            "blockers": blockers,
        }
    if any_failed:
        return {
            "status": EXECUTION_STATUS_FAIL,
            "cases_evaluated": cases_evaluated,
            "cases_passed": cases_passed,
            "cases_failed": cases_failed,
            "blockers": [],
        }
    return {
        "status": EXECUTION_STATUS_PASS,
        "cases_evaluated": cases_evaluated,
        "cases_passed": cases_passed,
        "cases_failed": cases_failed,
        "blockers": [],
    }


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Run the ``evaluate`` subcommand; return the process exit code.

    The contract (Issue #32 / ADR 0029):

      - Resolves the workspace path and discovers the
        benchmark state via :func:`_discover_benchmark_state`.
        A missing ``benchmark.jsonl`` is BLOCKED (precondition
        failure) with the :data:`EVALUATE_BENCHMARK_MISSING_BLOCKER`
        id, not SKIPPED + warning like the F1 review flow;
        ``evaluate`` is a support command whose explicit purpose
        is evaluation, so a missing benchmark is a hard
        precondition failure.
      - Loads the benchmark through :func:`metacrucible.benchmark.load_benchmark`.
        Loader-supplied blockers (schema mismatch, duplicate ids,
        missing required reviewed cases, pending generated) are
        propagated verbatim and the run BLOCKS. The ``evaluate``
        category is in the ADR 0035 emitting matrix so the
        BLOCKED bundle is written before returning.
      - Filters the eligible cases by ``args.split``:
        :data:`SPLIT_ALL` runs eval + held_out,
        :data:`SPLIT_EVAL` runs only the eval partition, and
        :data:`SPLIT_HELD_OUT` runs only the held-out partition.
        Generated / disabled / non-matching-split cases are
        never evaluated (ADR 0029 partitions are preserved).
      - Evaluates each selected case via
        :func:`_evaluate_single_case`; aggregates per-case
        verdicts through :func:`_aggregate_case_verdicts` with
        BLOCKED > FAILED > PASS precedence.
      - On BLOCKED, writes the ADR 0035 ``evaluate`` evidence
        bundle via :func:`_write_evaluate_blocked_bundle` so the
        receipt lineage carries the "we could not proceed"
        record alongside the in-memory payload. The bundle
        write is best-effort (a failure logs to stderr; the
        in-memory verdict still wins).
      - On FAILED, the run executed and surfaced a real verdict;
        the BLOCKED bundle is NOT written (the FAILED outcome
        is not a "we could not proceed" condition).
      - The command never mutates the workspace (no benchmark
        / envelope / artifact / state writes). Evidence is
        written only to the user-global store, mirroring
        ``review``'s read-only contract.
      - Exit codes follow the central matrix: PASS maps to
        :data:`EXIT_OK`; FAILED and BLOCKED both map to
        :data:`EXIT_BLOCKED` (the existing matrix has no
        separate FAILED code; the negative outcome is
        observable from the JSON ``status`` field, and the
        ``blockers`` / ``cases_failed`` fields carry the
        detail).
    """
    from .benchmark import load_benchmark

    workspace = Path(args.workspace).resolve()
    benchmark_state = _discover_benchmark_state(workspace)

    # Precondition 1: missing benchmark is BLOCKED for
    # ``evaluate``. Unlike ``review`` (which treats a missing
    # benchmark as a static+warning path), ``evaluate`` is a
    # support command whose explicit purpose is evaluation; a
    # missing benchmark is a hard precondition failure.
    if not benchmark_state["present"]:
        blockers: list[dict[str, Any]] = [
            {
                "id": EVALUATE_BENCHMARK_MISSING_BLOCKER,
                "message": (
                    f"benchmark file {benchmark_state['path']} "
                    f"does not exist; run ``metacrucible init "
                    f"{workspace}`` first"
                ),
            }
        ]
        _write_evaluate_blocked_bundle(blockers=blockers)
        payload: dict[str, Any] = {
            "status": "BLOCKED",
            "workspace": str(workspace),
            "benchmark_path": str(benchmark_state["path"]),
            "benchmark": dict(benchmark_state),
            "split": args.split,
            "cases_evaluated": 0,
            "cases_passed": 0,
            "cases_failed": 0,
            "case_results": [],
            "blockers": blockers,
        }
        _emit(payload, as_json=args.json)
        return EXIT_BLOCKED

    # Precondition 2: loader-supplied blockers gate the run.
    # Every loader blocker is propagated; the ``evaluate``
    # command never invents its own verdict when the loader
    # already explained why the benchmark is not runnable.
    load_result = load_benchmark(benchmark_state["path"])
    loader_blockers: list[dict[str, Any]] = [
        dict(b) for b in load_result.blockers
        if isinstance(b, dict) and isinstance(b.get("id"), str)
    ]
    if loader_blockers:
        _write_evaluate_blocked_bundle(blockers=loader_blockers)
        payload = {
            "status": "BLOCKED",
            "workspace": str(workspace),
            "benchmark_path": str(benchmark_state["path"]),
            "benchmark": dict(benchmark_state),
            "split": args.split,
            "cases_evaluated": 0,
            "cases_passed": 0,
            "cases_failed": 0,
            "case_results": [],
            "blockers": loader_blockers,
        }
        _emit(payload, as_json=args.json)
        return EXIT_BLOCKED

    # Precondition 3: split filter. ``SPLIT_ALL`` evaluates
    # eval + held_out (mirrors :func:`_run_execution_evaluation`);
    # the per-split choices evaluate only the matching
    # partition. The three branches are an explicit if/elif
    # (not a dict dispatch) so the loader's eligible list is
    # the single source of truth for what is eligible.
    if args.split == SPLIT_ALL:
        selected_cases: list[dict[str, Any]] = list(
            load_result.eligible_eval_cases
        ) + list(load_result.eligible_held_out_cases)
    elif args.split == SPLIT_EVAL:
        selected_cases = list(load_result.eligible_eval_cases)
    elif args.split == SPLIT_HELD_OUT:
        selected_cases = list(
            load_result.eligible_held_out_cases
        )
    else:
        # Argparse already constrains ``--split`` to the three
        # pinned values; this branch is defensive only.
        selected_cases = []

    if not selected_cases:
        blockers = [
            {
                "id": EVALUATE_NO_ELIGIBLE_CASES_BLOCKER,
                "message": (
                    f"no eligible reviewed cases in split "
                    f"{args.split!r} (ADR 0029); the "
                    f"benchmark has no cases for the "
                    f"requested partition"
                ),
            }
        ]
        _write_evaluate_blocked_bundle(blockers=blockers)
        payload = {
            "status": "BLOCKED",
            "workspace": str(workspace),
            "benchmark_path": str(benchmark_state["path"]),
            "benchmark": dict(benchmark_state),
            "split": args.split,
            "cases_evaluated": 0,
            "cases_passed": 0,
            "cases_failed": 0,
            "case_results": [],
            "blockers": blockers,
        }
        _emit(payload, as_json=args.json)
        return EXIT_BLOCKED

    # Per-case evaluation. The dispatcher is the existing
    # :func:`_evaluate_single_case` so ``evaluate`` shares the
    # deterministic check engine and the judgment path the F1
    # review uses; the contract is the same set of per-case
    # verdicts (``PASS`` / ``FAIL`` / ``BLOCKED``).
    case_results = [
        _evaluate_single_case(case) for case in selected_cases
    ]

    # Aggregate per-case verdicts. BLOCKED > FAILED > PASS.
    aggregation = _aggregate_case_verdicts(case_results)

    # On a BLOCKED aggregation, the per-case blockers are the
    # "we could not proceed" record; the BLOCKED bundle is the
    # evidence. On FAILED, the run executed end-to-end and the
    # verdict is a real failure -- no BLOCKED bundle, mirroring
    # the F1 review's contract.
    if aggregation["status"] == EXECUTION_STATUS_BLOCKED:
        _write_evaluate_blocked_bundle(
            blockers=aggregation["blockers"]
        )

    payload = {
        "status": aggregation["status"],
        "workspace": str(workspace),
        "benchmark_path": str(benchmark_state["path"]),
        "benchmark": dict(benchmark_state),
        "split": args.split,
        "cases_evaluated": aggregation["cases_evaluated"],
        "cases_passed": aggregation["cases_passed"],
        "cases_failed": aggregation["cases_failed"],
        "case_results": case_results,
        "blockers": aggregation["blockers"],
    }
    _emit(payload, as_json=args.json)

    # Exit code: PASS -> EXIT_OK; FAILED / BLOCKED -> EXIT_BLOCKED.
    # The existing exit-code matrix has no separate FAILED code;
    # the negative outcome is observable from the JSON ``status``
    # field and the ``blockers`` / ``cases_failed`` fields.
    if aggregation["status"] == EXECUTION_STATUS_PASS:
        return EXIT_OK
    return EXIT_BLOCKED


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``metacrucible`` console script.

    Returns the process exit code, pinned by
    :mod:`metacrucible.exit_codes`. Argparse's ``--help`` /
    ``--version`` actions raise ``SystemExit`` to terminate; we
    catch those here and translate to a clean integer return value
    so the console-script wrapper and unit tests get a stable
    contract.

    Any uncaught exception past the command dispatcher is mapped
    to ``EXIT_INTERNAL_ERROR`` with a one-line English message on
    stderr; the caller treats this as a bug report.
    """
    parser = _build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        # Bare invocation: print a short banner so the CLI is useful
        # out of the box even before the MVP subcommands land.
        print(f"metacrucible {__version__}")
        print(
            "A workbench for improving portable agent capabilities. "
            "Run 'metacrucible --help' for usage."
        )
        return EXIT_OK
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        # Argparse raises SystemExit on --help / --version (code 0
        # or None) and on usage errors (code 2). Map success to
        # EXIT_OK; map any nonzero (i.e. usage error) to
        # EXIT_USER_ERROR so the contract stays distinct from the
        # blocked (2) and internal (3) codes.
        code = exc.code
        if code is None or int(code) == 0:
            return EXIT_OK
        return EXIT_USER_ERROR
    try:
        if getattr(args, "command", None) == "init":
            return cmd_init(args)
        if getattr(args, "command", None) == "promote":
            return cmd_promote(args)
        if getattr(args, "command", None) == "review":
            return cmd_review(args)
        if getattr(args, "command", None) == "bootstrap":
            return cmd_bootstrap(args)
        if getattr(args, "command", None) == "optimize":
            return cmd_optimize(args)
        if getattr(args, "command", None) == "baseline":
            return cmd_baseline(args)
        if getattr(args, "command", None) == "evaluate":
            return cmd_evaluate(args)
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - exit-code firewall
        # Catch-all so an uncaught command-handler bug still
        # returns a stable code; the English message is the
        # diagnostic the caller reads.
        print(
            f"metacrucible: internal error: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
