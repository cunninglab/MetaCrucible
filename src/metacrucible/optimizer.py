"""Optimizer pipeline (Issue #33 / PRD F3 / ADR 0032 / ADR 0022).

Reimplements the SkillOpt-shaped optimizer loop *without* a runtime
dependency on Microsoft SkillOpt. The loop is the single
contract every ``metacrucible optimize`` run executes; the CLI
entry point (:func:`metacrucible.__main__.cmd_optimize`) is a thin
wrapper that builds the immutable run context and persists the
records via :class:`metacrucible.storage.RepositoryStorage` and
:class:`metacrucible.storage.UserGlobalStorage`.

Pinned by:

  - ADR 0032 (optimizer pipeline contract): eval-split-only
    context, per-case / per-round reflections, bounded edit
    suggestions, rank-and-clip selection, deterministic conflict
    checks, same-range LLM merge, Patch Revision constrained to
    mutable ranges.
  - ADR 0022 (reimplement without SkillOpt): this module has no
    runtime import of ``skillopt``; the algorithm is the
    SkillOpt-shaped reference reimplemented in pure Python.
  - PRD F3 (acceptance / MVP scope): strict eval-split
    improvement, zero new held-out FAIL/BLOCKED case_ids, no
    automatic git commits, human-readable default with ``--json``
    switchable.

The module is single-convention: it does not invent a second
parser/producer for ``MutableRange`` identity, a second
``call_structured``/``record_provider_run_outcome`` wrapper, or a
second storage layout. The only writer of history records is
:meth:`RepositoryStorage.append_history` (ADR 0016); the only
writer of evidence bundles is
:meth:`UserGlobalStorage.write_receipt` /
``write_summary`` / ``write_trajectory_digest`` (ADR 0030).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .artifact import (
    SUBAGENT_ROUTING_FIELDS,
    SKILL_ROUTING_FIELDS,
    MutableRange,
    SubagentArtifact,
    SkillArtifact,
    _split_frontmatter,
)
from .benchmark import BenchmarkResult, load_benchmark
from .profiles import (
    BUILTIN_PROFILES,
    ROUTING_SURFACE_CAP,
    evaluate_acceptance,
    evaluate_profile_specs,
    select_triggers,
)
from .provider_config import (
    STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER,
    call_structured,
    record_provider_run_outcome,
)
from .storage import (
    REPO_DIR_NAME,
    SCHEMA_VERSION,
    RepositoryStorage,
    UserGlobalStorage,
)

__all__ = [
    "OptimizerContext",
    "OptimizerPipelineResult",
    "PatchRevision",
    "RANKED_EDIT_BUDGET",
    "ROUND_BUDGET_DEFAULT",
    "ROUTING_CHANGE_REJECTED_BLOCKER",
    "ROUTING_CAP_EXCEEDED_BLOCKER",
    "ROUTING_HITL_UNCONFIRMED_BLOCKER",
    "SCHEMA_VALIDATION_BLOCKED",
    "STALE_BASE_HASH_BLOCKER",
    "MUTABLE_RANGE_CONFLICT_BLOCKER",
    "STOP_REASONS",
    "STOP_REASON_ACCEPTED",
    "STOP_REASON_MAX_ROUNDS_REACHED",
    "STOP_REASON_NO_CANDIDATE_EDITS",
    "STOP_REASON_NO_CANDIDATE_SELECTED",
    "STOP_REASON_PRECONDITION_BLOCKED",
    "STOP_REASON_ROUND_BLOCKED",
    "RESUME_CONFIRMATION_REQUIRED_BLOCKER",
    "RESUME_NON_INTERACTIVE_BLOCKER",
    "apply_patch_revision",
    "build_optimizer_context",
    "compare_eval_held_out",
    "detect_interrupted_optimizer_runs",
    "run_optimizer_pipeline",
    "validate_resume_interrupted_runs",
]


# --------------------------------------------------------------------------- #
# Constants — pinned by ADR 0032 / PRD F3                                    #
# --------------------------------------------------------------------------- #

#: Default round budget (PRD F3 / Risk note in plan-33-revised: minimal
#: safe default unless user/product confirms more). The pipeline
#: stops after at most this many optimization rounds; the CLI can
#: raise it via ``--max-rounds``. The cap exists so a misconfigured
#: pipeline cannot loop forever (PRD F3 / OPT-8: bounded and
#: observable).
ROUND_BUDGET_DEFAULT: int = 1

#: Maximum number of edits a single round may apply. The plan
#: (OPT-4) calls this the "routing edit budget" but the value
#: is the same machine contract for *any* selected edit
#: (routing or non-routing) per round. The cap is a hard upper
#: bound: any candidate revision that exceeds the cap is
#: rejected before conflict checks.
RANKED_EDIT_BUDGET: int = 4

#: Stable blocker id emitted when a structured provider response
#: (case_reflection, round_reflection, edit_suggestion) fails JSON
#: Schema validation after the bounded repair budget is exhausted
#: (OPT-3). The id extends ADR 0035's emitting matrix.
SCHEMA_VALIDATION_BLOCKED: str = "schema-validation-blocked"

#: Stable blocker id emitted when a selected edit proposal carries
#: more than the routing surface cap of 1 routing change (OPT-4 /
#: ADR 0027). The id is part of the machine contract; downstream
#: tools branch on it.
ROUTING_CAP_EXCEEDED_BLOCKER: str = "routing-cap-exceeded"

#: Stable blocker id emitted when a routing change lacks explicit
#: human confirmation (OPT-4 / ADR 0032). The CLI surfaces this
#: blocker in :data:`HumanConfirms` shape; the pipeline does not
#: invent a confirmation.
ROUTING_HITL_UNCONFIRMED_BLOCKER: str = "routing-hitl-unconfirmed"

#: Stable blocker id emitted when a selected edit's recorded base
#: content hash does not match the current mutable range hash
#: (OPT-5). A stale base hash is the canonical "another writer
#: changed the range under us" signal.
STALE_BASE_HASH_BLOCKER: str = "stale-base-hash"


#: Stable blocker id emitted when an interactive ``optimize`` run is
#: asked to resume an interrupted run without explicit confirmation
#: (Issue #38 / ADR 0017). The gate surfaces the blocker so the CLI
#: can prompt the user; the pipeline never auto-resumes.
RESUME_CONFIRMATION_REQUIRED_BLOCKER: str = "resume-confirmation-required"

#: Stable blocker id emitted when a non-interactive ``optimize`` run
#: is asked to resume an interrupted run without the
#: ``--confirm-resume`` flag (Issue #38 / ADR 0017). The gate blocks
#: the run so automation aborts instead of silently resuming.
RESUME_NON_INTERACTIVE_BLOCKER: str = "resume-non-interactive-blocked"

#: Stable blocker id emitted when a conflict check rejects the
#: selected edit set: overlapping ranges, contradictory intent,
#: budget violations, routing violations, or merge output that
#: falls outside the mutable range (OPT-5).
MUTABLE_RANGE_CONFLICT_BLOCKER: str = "mutable-range-conflict"

#: Stable blocker id emitted when a selected routing change is
#: rejected and the rejection summary flows back into a later
#: round as bounded guidance (OPT-4). Distinct from the
#: cap-exceeded / hitl-unconfirmed ids so a downstream report
#: can group on the specific reason.
ROUTING_CHANGE_REJECTED_BLOCKER: str = "routing-change-rejected"

#: Bounded theme summary cap (OPT-4). Rejected edit buffers are
#: injected into later rounds only as bounded theme summaries
#: (reasons + avoid guidance), not as raw unbounded suggestions.
#: The cap is the maximum number of theme entries the pipeline
#: retains for re-injection; a longer rejected buffer is clipped.
THEME_SUMMARY_BUDGET: int = 5

#: Bounded theme summary per-entry character cap. The cap keeps
#: the re-injected guidance small so a future round's provider
#: payload stays bounded.
THEME_SUMMARY_MAX_CHARS: int = 280

#: Machine-stable stop reasons the pipeline may emit on
#: :class:`OptimizerPipelineResult.stop_reason` and on the
#: ``optimize_finished`` history event. The strings are the
#: contract; downstream tools branch on them. Do not change
#: the spelling of any existing id; add a new constant if a
#: new stop path is introduced.
STOP_REASON_MAX_ROUNDS_REACHED: str = "max_rounds_reached"
STOP_REASON_ACCEPTED: str = "accepted"
STOP_REASON_NO_CANDIDATE_EDITS: str = "no_candidate_edits"
STOP_REASON_NO_CANDIDATE_SELECTED: str = "no_candidate_selected"
STOP_REASON_ROUND_BLOCKED: str = "round_blocked"
STOP_REASON_PRECONDITION_BLOCKED: str = "precondition_blocked"

#: All stop reasons the pipeline may emit. Useful for tests
#: that want to assert exhaustiveness over the vocabulary.
STOP_REASONS: frozenset[str] = frozenset({
    STOP_REASON_MAX_ROUNDS_REACHED,
    STOP_REASON_ACCEPTED,
    STOP_REASON_NO_CANDIDATE_EDITS,
    STOP_REASON_NO_CANDIDATE_SELECTED,
    STOP_REASON_ROUND_BLOCKED,
    STOP_REASON_PRECONDITION_BLOCKED,
})


# --------------------------------------------------------------------------- #
# Run identity                                                               #
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z``.

    Local copy of the helper that lives in :mod:`metacrucible.storage`
    and :mod:`metacrucible.__main__` so this module does not import
    :mod:`metacrucible.__main__` (which would invert the dependency
    graph: the CLI calls into the pipeline, not the other way
    around).
    """
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _new_run_id() -> str:
    """Return a flat, path-safe run id keyed on UTC time + entropy.

    The id is short, deterministic per-second, and includes a small
    random tail so two back-to-back runs in the same second still
    get distinct ids. Path-safety is the contract: the id is
    threaded into ``append_history``'s JSONL file and into the
    evidence bundle directory, so it must be flat, untrimmed, and
    free of separators.
    """
    suffix = hashlib.sha256(
        f"{time.time_ns()}-{id(object())}".encode("utf-8")
    ).hexdigest()[:8]
    return f"opt-{_now_iso().replace(':', '').replace('-', '')}-{suffix}"


# --------------------------------------------------------------------------- #
# Record types (OPT-2)                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class CaseReflection:
    """One ``case_reflection`` record (ADR 0032 / OPT-3).

    The record captures the bounded reflection on a single failed
    or weak eval case. ``rationale`` is bounded by the caller's
    schema validation; ``source_refs`` is the list of case ids /
    range ids the reflection was generated against. A field-level
    record is also exposed via :meth:`as_dict` for evidence
    bundle round-trip.
    """

    record_type: str
    run_id: str
    round_id: str
    case_id: str
    timestamp: str
    rationale: str
    source_refs: list[str] = field(default_factory=list)
    schema_id: str = "case_reflection_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "schema_id": self.schema_id,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "case_id": self.case_id,
            "timestamp": self.timestamp,
            "rationale": self.rationale,
            "source_refs": list(self.source_refs),
        }


@dataclass
class RoundReflection:
    """One ``round_reflection`` record (ADR 0032 / OPT-3).

    The per-round synthesis over the bounded set of case
    reflections. ``bounded_rejected_themes`` is a list of
    bounded theme summaries (reasons + avoid guidance) so a
    later round can re-inject them without leaking raw
    unbounded edit suggestions.
    """

    record_type: str
    run_id: str
    round_id: str
    timestamp: str
    rationale: str
    bounded_rejected_themes: list[dict[str, str]] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    schema_id: str = "round_reflection_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "schema_id": self.schema_id,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "timestamp": self.timestamp,
            "rationale": self.rationale,
            "bounded_rejected_themes": list(self.bounded_rejected_themes),
            "source_refs": list(self.source_refs),
        }


@dataclass
class EditSuggestion:
    """One ``edit_suggestion`` record (ADR 0032 / OPT-4).

    Each bounded edit suggestion carries the target range's
    ``range_id`` and ``base_hash`` (the parser-owned
    ``MutableRange.content_hash``) so the merge / conflict
    check can verify the range has not drifted since the
    suggestion was generated. ``intent`` is a short
    machine-readable label (e.g. ``"clarify_triggers"``);
    ``replacement`` is the bounded text the optimizer
    intends to substitute into the mutable range. ``routing``
    is True when the suggestion mutates a routing surface
    field; ``human_confirmed`` is the OPT-4 HITL gate.
    ``generated_case_suggestion`` is optional and only
    populated when the round's reflections asked for a new
    case to be added to the benchmark (PRD F3: never
    appended during optimize).
    """

    record_type: str
    suggestion_id: str
    run_id: str
    round_id: str
    timestamp: str
    range_id: int
    base_hash: str
    intent: str
    replacement: str
    rationale: str
    routing: bool = False
    human_confirmed: bool = False
    routing_field: str = ""
    generated_case_suggestion: dict[str, Any] | None = None
    schema_id: str = "edit_suggestion_v1"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "record_type": self.record_type,
            "schema_id": self.schema_id,
            "suggestion_id": self.suggestion_id,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "timestamp": self.timestamp,
            "range_id": self.range_id,
            "base_hash": self.base_hash,
            "intent": self.intent,
            "replacement": self.replacement,
            "rationale": self.rationale,
            "routing": bool(self.routing),
            "human_confirmed": bool(self.human_confirmed),
        }
        if self.routing:
            out["routing_field"] = self.routing_field
        if self.generated_case_suggestion is not None:
            out["generated_case_suggestion"] = dict(
                self.generated_case_suggestion
            )
        return out


@dataclass
class RankedEditSet:
    """One ``ranked_edit_set`` record (ADR 0032 / OPT-4).

    The clip-and-rank output for a single round. ``ordered_candidates``
    is the list of selected candidate suggestion ids in priority
    order (the first entry is the highest-ranked). ``rejected`` is
    the bounded list of rejected suggestions with the reason each
    one was clipped. ``selected`` is the clip to
    :data:`RANKED_EDIT_BUDGET` (the editable cap per round).
    """

    record_type: str
    run_id: str
    round_id: str
    timestamp: str
    ordered_candidates: list[str]
    rejected: list[dict[str, str]]
    selected: list[str]
    schema_id: str = "ranked_edit_set_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "schema_id": self.schema_id,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "timestamp": self.timestamp,
            "ordered_candidates": list(self.ordered_candidates),
            "rejected": list(self.rejected),
            "selected": list(self.selected),
        }


@dataclass
class RangeMergePlan:
    """One ``range_merge_plan`` record (ADR 0032 / OPT-5).

    The deterministic conflict-check output. ``per_range_plan``
    maps each selected ``range_id`` to the single merged Patch
    Revision that will replace its ``.text``. ``base_hashes``
    pins the parser-owned ``content_hash`` for each range at
    merge time; a stale base hash in the next round's
    conflict check is the "another writer changed it" signal.
    ``merge_outside_mutable_range`` is set when an LLM merge
    produced text outside the range; the round is blocked on
    that signal.
    """

    record_type: str
    run_id: str
    round_id: str
    timestamp: str
    per_range_plan: dict[int, dict[str, Any]]
    base_hashes: dict[int, str]
    merge_outside_mutable_range: bool
    blocked_reasons: list[dict[str, str]]
    schema_id: str = "range_merge_plan_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "schema_id": self.schema_id,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "timestamp": self.timestamp,
            "per_range_plan": {
                str(k): dict(v) for k, v in self.per_range_plan.items()
            },
            "base_hashes": {str(k): v for k, v in self.base_hashes.items()},
            "merge_outside_mutable_range": bool(
                self.merge_outside_mutable_range
            ),
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass
class GeneratedCaseSuggestion:
    """One ``generated_case_suggestion`` record (ADR 0032 / OPT-2).

    A bounded proposal for a new benchmark case the reflections
    want to add. The record is *suggestion-only*: the pipeline
    never appends to ``benchmark.jsonl`` during optimize. A
    downstream ``bootstrap`` / human review flow can promote
    the suggestion to a real case after a reviewer vets it.
    """

    record_type: str
    suggestion_id: str
    run_id: str
    round_id: str
    timestamp: str
    case_draft: dict[str, Any]
    rationale: str
    schema_id: str = "generated_case_suggestion_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "schema_id": self.schema_id,
            "suggestion_id": self.suggestion_id,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "timestamp": self.timestamp,
            "case_draft": dict(self.case_draft),
            "rationale": self.rationale,
        }


@dataclass
class PatchRevision:
    """An in-memory candidate revision derived from a merge plan.

    ``per_range_text`` maps each selected ``range_id`` to the
    new mutable-range text. ``base_hashes`` mirrors the merge
    plan's base_hashes so callers (e.g. the rollback path) can
    verify the candidate still targets the original base.
    """

    run_id: str
    round_id: str
    per_range_text: dict[int, str] = field(default_factory=dict)
    base_hashes: dict[int, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Optimizer run context (OPT-1)                                              #
# --------------------------------------------------------------------------- #

@dataclass
class OptimizerContext:
    """Immutable per-run optimizer context (OPT-1 / ADR 0032).

    The context is built *once* at run start from the eval-split
    cases only; held-out cases are deliberately absent so the
    optimizer cannot leak held-out prompts into its proposals.
    ``base_artifact_path`` and ``base_content_hash`` are the
    single base inputs; ``mutable_ranges`` is the parsed artifact's
    mutable ranges with parser-owned ``range_id`` /
    ``content_hash``. ``routing_surface_fields`` lists the
    routing field names so a routing-cap violation can be
    reported with the field name.
    """

    run_id: str
    workspace: str
    benchmark_path: str
    artifact_path: str
    artifact_kind: str  # "skill" or "subagent"
    base_content_hash: str
    mutable_ranges: tuple[MutableRange, ...]
    routing_surface_fields: frozenset[str]
    eligible_eval_case_ids: tuple[str, ...]
    eligible_held_out_case_ids: tuple[str,  ...]  # held-out *ids* only (no evidence)
    benchmark_metadata: dict[str, Any]
    max_rounds: int
    human_confirmed: bool
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "workspace": self.workspace,
            "benchmark_path": self.benchmark_path,
            "artifact_path": self.artifact_path,
            "artifact_kind": self.artifact_kind,
            "base_content_hash": self.base_content_hash,
            "mutable_ranges": [
                {
                    "range_id": r.range_id,
                    "content_hash": r.content_hash,
                    "text": r.text,
                }
                for r in self.mutable_ranges
            ],
            "routing_surface_fields": sorted(self.routing_surface_fields),
            "eligible_eval_case_ids": list(self.eligible_eval_case_ids),
            "eligible_held_out_case_ids": list(self.eligible_held_out_case_ids),
            "benchmark_metadata": dict(self.benchmark_metadata),
            "max_rounds": self.max_rounds,
            "human_confirmed": self.human_confirmed,
        }


@dataclass
class OptimizerPipelineResult:
    """Final pipeline outcome (OPT-8).

    The CLI composes this into the JSON / human output. ``status``
    is one of ``"ACCEPTED"``, ``"REJECTED"``, or ``"BLOCKED"``.
    ``best_revision`` is the accepted revision's content (None
    when no revision was accepted). ``record_counts`` is the
    OPT-8 / OPT-9 contract: every required record type is
    counted so a downstream tool can verify the run produced
    the right shape. ``evidence_refs`` are the
    ``$HOME/.metacrucible/evidence/<run_id>/`` paths.
    """

    status: str  # "ACCEPTED" / "REJECTED" / "BLOCKED"
    run_id: str
    rounds: int
    record_counts: dict[str, int]
    evidence_refs: dict[str, str]
    blockers: list[dict[str, str]]
    warnings: list[dict[str, str]]
    best_revision: dict[str, Any] | None
    acceptance_decision: dict[str, Any]
    selected_candidate_ids: list[str]
    #: Machine-stable termination reason (one of
    #: :data:`STOP_REASONS`). Set on every
    #: :class:`OptimizerPipelineResult` regardless of
    #: whether the run completed, was rejected, or was
    #: blocked. The CLI composes this into the ``--json``
    #: payload at the top level.
    stop_reason: str


# --------------------------------------------------------------------------- #
# Context builder                                                            #
# --------------------------------------------------------------------------- #

def _resolve_artifact_path(workspace: Path) -> Path | None:
    """Read the envelope and return the declared ``artifact_path``.

    Mirrors the contract :func:`cmd_baseline_create` enforces
    (OD1): the envelope's ``artifact_path`` (preferred) or
    ``canonical_source`` field is the only path source. A
    missing / malformed envelope is reported by the caller as a
    precondition failure.
    """
    envelope_path = workspace / REPO_DIR_NAME / "envelope.json"
    if not envelope_path.is_file():
        return None
    try:
        raw = json.loads(envelope_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, Mapping):
        return None
    for key in ("artifact_path", "canonical_source"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return Path(value)
    return None


def build_optimizer_context(
    *,
    workspace: Path,
    benchmark_path: Path,
    artifact_path: Path,
    max_rounds: int,
    human_confirmed: bool,
) -> OptimizerContext:
    """Build the immutable optimizer context (OPT-1).

    Loads the benchmark, partitions it into the four ADR 0029
    buckets, and reads the artifact through the existing
    subagent-first / Skill parser. The context exposes
    ``base_content_hash``, ``mutable_ranges`` with parser-owned
    ``range_id`` / ``content_hash``, the routing surface field
    list, the eligible eval case ids (full cases via
    ``benchmark_path``'s partition), and the held-out case ids
    only (no evidence). Held-out case content is not threaded
    into the context: the provider payloads see only the
    eval-split case ids.
    """
    from .__main__ import _parse_artifact_source  # local import; avoid cycle

    artifact_path = Path(artifact_path).resolve()
    source_bytes = artifact_path.read_bytes()
    source_text = source_bytes.decode("utf-8")
    kind, parsed = _parse_artifact_source(
        source_text, artifact_path=artifact_path
    )

    if isinstance(parsed, SkillArtifact):
        routing_fields = frozenset(SKILL_ROUTING_FIELDS)
    elif isinstance(parsed, SubagentArtifact):
        routing_fields = frozenset(SUBAGENT_ROUTING_FIELDS)
        # Subagent frontmatter can add to the routing surface
        # dynamically (e.g. ``tools`` / ``spawns`` when present
        # are routed in addition to the canonical set).
        for k in parsed.frontmatter:
            if k in SUBAGENT_ROUTING_FIELDS:
                routing_fields = frozenset(
                    set(routing_fields) | {k}
                )
    else:  # defensive
        routing_fields = frozenset()

    base_content_hash = hashlib.sha256(source_bytes).hexdigest()
    benchmark_result = load_benchmark(benchmark_path)
    eval_ids = tuple(
        c.get("case_id") for c in benchmark_result.eligible_eval_cases
        if isinstance(c, Mapping) and isinstance(c.get("case_id"), str)
    )
    held_out_ids = tuple(
        c.get("case_id")
        for c in benchmark_result.eligible_held_out_cases
        if isinstance(c, Mapping) and isinstance(c.get("case_id"), str)
    )

    return OptimizerContext(
        run_id=_new_run_id(),
        workspace=str(Path(workspace).resolve()),
        benchmark_path=str(benchmark_path.resolve()),
        artifact_path=str(artifact_path),
        artifact_kind=kind,
        base_content_hash=base_content_hash,
        mutable_ranges=tuple(parsed.mutable_ranges),
        routing_surface_fields=routing_fields,
        eligible_eval_case_ids=eval_ids,
        eligible_held_out_case_ids=held_out_ids,
        benchmark_metadata={
            "schema_version": (
                benchmark_result.metadata.get("schema_version")
                if isinstance(benchmark_result.metadata, Mapping)
                else None
            ),
            "name": (
                benchmark_result.metadata.get("name")
                if isinstance(benchmark_result.metadata, Mapping)
                else None
            ),
        },
        max_rounds=max(1, int(max_rounds)),
        human_confirmed=bool(human_confirmed),
    )


# --------------------------------------------------------------------------- #
# Persistence helpers                                                        #
# --------------------------------------------------------------------------- #

def _append_history(
    repo: RepositoryStorage, record: Mapping[str, Any]
) -> None:
    """Append a single record to the workspace's ``history.jsonl``.

    The history stream is the audit lineage every optimize run
    leaves behind. Records must be JSON-serializable; non-string
    values are coerced through ``str``/``dict`` to keep the
    contract minimal.
    """
    repo.append_history(dict(record))


# --------------------------------------------------------------------------- #
# Issue #38 / ADR 0017 — interrupted-run detection + resume gate (pure)        #
# --------------------------------------------------------------------------- #


def detect_interrupted_optimizer_runs(
    history_events: Iterable[Mapping[str, object]],
) -> list[str]:
    """Return optimize run IDs that started without a matching finish event.

    The detector is pure: it consumes an already-loaded iterable
    of history records (the same shape
    :meth:`metacrucible.storage.RepositoryStorage.read_history`
    returns) and reports the set of run ids whose
    ``optimize_started`` event has no matching
    ``optimize_finished`` event in the same stream.

    Contract:

    - Iterates the input exactly once.
    - Only ``event == "optimize_started"`` / ``"optimize_finished"``
      with a string ``run_id`` count; malformed records are
      silently skipped.
    - Returned run ids preserve first-seen start order.
    - Duplicate starts for the same unfinished run id collapse
      to a single entry (the id appears in the output once).
    - A finish event with a different ``run_id`` does NOT clear
      a started run (finish is keyed on ``run_id``).

    The function performs no I/O, reads no stdin, and never
    instantiates storage. It is the unit-testable core the CLI
    layer (Task 2) wires into the resume gate.
    """
    # State per run_id:
    #   - ``started`` means at least one ``optimize_started`` has
    #     been seen; ``started_order`` preserves first-seen start
    #     order for the result list.
    #   - ``finished`` means the LAST seen event for the run_id
    #     was an ``optimize_finished``. A later ``optimize_started``
    #     resets it to ``False`` (the run was restarted).
    # The dict keys preserve insertion order so we can iterate
    # in first-seen start order without an extra index.
    state: dict[str, dict[str, object]] = {}
    for event in history_events:
        if not isinstance(event, Mapping):
            continue
        event_name = event.get("event")
        run_id = event.get("run_id")
        if not isinstance(run_id, str):
            continue
        if event_name == "optimize_started":
            entry = state.get(run_id)
            if entry is None:
                state[run_id] = {"started": True, "finished": False}
            else:
                # Duplicate starts for the same run collapse;
                # any prior finish is irrelevant because the
                # run was restarted (the duplicate start is
                # what the contract collapses).
                entry["started"] = True
                entry["finished"] = False
        elif event_name == "optimize_finished":
            entry = state.get(run_id)
            if entry is None:
                # Orphan finish (no prior start): nothing
                # to clear. A later optimize_started for the
                # same run_id starts a fresh state entry
                # independent of this finish, so recording
                # an intermediate entry adds no information.
                continue
            entry["finished"] = True
    return [
        run_id
        for run_id, entry in state.items()
        if entry["started"] and not entry["finished"]
    ]


def validate_resume_interrupted_runs(
    interrupted_runs: Sequence[str],
    *,
    interactive: bool,
    confirmed: bool,
) -> dict[str, object]:
    """Gate resume of interrupted optimizer runs (Issue #38 / ADR 0017).

    The gate is pure: it takes an already-computed list of
    interrupted run ids (the output of
    :func:`detect_interrupted_optimizer_runs`) plus the
    caller's interactivity and confirmation flags, and returns
    a ``{"ok", "blockers"}`` payload the CLI can render or
    ``--json``-emit directly.

    Decision matrix:

    - Empty ``interrupted_runs`` → ``{"ok": True, "blockers": []}``.
      There is nothing to confirm.
    - ``confirmed=True`` → ``{"ok": True, "blockers": []}``.
      The caller explicitly opted in via ``--confirm-resume``
      (or the interactive prompt); the gate honors that.
    - Otherwise → ``{"ok": False, "blockers": [...]}`` with a
      single ``{id, message}`` blocker. The ``id`` is
      :data:`RESUME_CONFIRMATION_REQUIRED_BLOCKER` for
      ``interactive=True`` and
      :data:`RESUME_NON_INTERACTIVE_BLOCKER` for
      ``interactive=False``. The message names ``--confirm-resume``
      (non-interactive) or the confirmation requirement
      (interactive) and embeds the interrupted run ids so the
      payload is actionable.

    The function never reads stdin, reads history, writes
    output, accesses environment variables, instantiates
    storage, or calls other optimizer code. The CLI layer
    owns those side effects.
    """
    if not interrupted_runs:
        return {"ok": True, "blockers": []}
    if confirmed:
        return {"ok": True, "blockers": []}
    joined_ids = ", ".join(interrupted_runs)
    if interactive:
        blocker_id: str = RESUME_CONFIRMATION_REQUIRED_BLOCKER
        message = (
            "interactive optimize requires resume confirmation "
            f"before continuing interrupted run(s): {joined_ids}; "
            "pass --confirm-resume to acknowledge the resume "
            "decision (Issue #38 / ADR 0017)"
        )
    else:
        blocker_id = RESUME_NON_INTERACTIVE_BLOCKER
        message = (
            "non-interactive optimize cannot resume interrupted "
            f"run(s) ({joined_ids}) without --confirm-resume; "
            "aborting to avoid a silent resume (Issue #38 / "
            "ADR 0017)"
        )
    blocker: dict[str, str] = {"id": blocker_id, "message": message}
    return {"ok": False, "blockers": [blocker]}


def _emit_evidence_bundle(
    *,
    global_store: UserGlobalStorage,
    run_id: str,
    receipt: Mapping[str, Any],
    summary: Mapping[str, Any],
    trajectory: Mapping[str, Any],
) -> dict[str, str]:
    """Persist the v1 evidence bundle and return the on-disk refs.

    The receipt, summary, and trajectory digest go through the
    existing v1 writers (ADR 0030). The returned mapping is the
    ``evidence_refs`` slot on the optimizer result.
    """
    receipt_path = global_store.write_receipt(run_id, dict(receipt))
    summary_path = global_store.write_summary(run_id, dict(summary))
    digest_path = global_store.write_trajectory_digest(
        run_id, dict(trajectory)
    )
    return {
        "receipt_path": str(receipt_path),
        "summary_path": str(summary_path),
        "trajectory_digest_path": str(digest_path),
    }


# --------------------------------------------------------------------------- #
# Provider call wrappers (OPT-3 / OPT-4 / OPT-5)                             #
# --------------------------------------------------------------------------- #

def _call_structured_with_evidence(
    *,
    provider_name: str,
    provider_spec: Mapping[str, Any],
    model: str,
    schema: Mapping[str, Any],
    call_fn: Callable[..., Any],
    repo: RepositoryStorage,
    run_id: str,
    round_id: str,
    step_label: str,
    usage: Mapping[str, Any] | None = None,
    cost: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run :func:`call_structured` and persist usage/cost to state.

    The wrapper enforces the OPT-3 contract: every optimizer
    structured call passes through :func:`call_structured` (so
    schema validation failures are result-level, not
    exceptions) and the outcome is recorded via
    :func:`record_provider_run_outcome` (so provider usage
    and cost land in ``state.json``). A failed validation
    returns the stable
    :data:`STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER` and
    raises :class:`OptimizerSchemaBlocked` so the caller can
    stop the round with ``EXIT_BLOCKED``.

    The wrapper does NOT pass a real provider: callers (CLI
    and tests) inject ``call_fn``. The provider_spec argument
    is forwarded for parity with the contract.
    """
    result = call_structured(
        provider_name=provider_name,
        provider_spec=provider_spec,
        model=model,
        schema=schema,
        call_fn=call_fn,
    )
    record_provider_run_outcome(
        repo,
        run_id=run_id,
        provider=provider_name,
        model=model,
        usage=usage,
        cost=cost,
    )
    if not result.get("ok"):
        # Re-raise so the caller maps to a BLOCKED outcome;
        # the result's ``blockers`` carries the stable id.
        raise OptimizerSchemaBlocked(
            str(step_label),
            result.get("blockers") or [],
            result.get("validation_errors") or [],
        )
    return result  # type: ignore[return-value]


class OptimizerSchemaBlocked(Exception):
    """Raised when a structured provider response fails validation.

    Carries the step label, the structured blockers, and the
    raw validation errors so the runner can compose the
    failure into the run's history and exit code without
    re-running the provider.
    """

    def __init__(
        self,
        step: str,
        blockers: Sequence[Mapping[str, Any]],
        validation_errors: Sequence[str],
    ) -> None:
        super().__init__(step)
        self.step = step
        self.blockers = [dict(b) for b in blockers]
        self.validation_errors = list(validation_errors)


# --------------------------------------------------------------------------- #
# Schemas for call_structured (OPT-3 / OPT-4)                                 #
# --------------------------------------------------------------------------- #

ROUND_REFLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "suggested_edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "range_id": {"type": "integer"},
                    "base_hash": {"type": "string"},
                    "intent": {"type": "string"},
                    "replacement": {"type": "string"},
                    "rationale": {"type": "string"},
                    "routing": {"type": "boolean"},
                    "routing_field": {"type": "string"},
                    "generated_case_suggestion": {
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            "split": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["case_id", "rationale"],
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "range_id",
                    "base_hash",
                    "intent",
                    "replacement",
                    "rationale",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rationale", "suggested_edits"],
    "additionalProperties": False,
}

#: Schema for the same-range merge step (OPT-5). The merge must
#: stay inside the mutable range's text; ``replacement`` is the
#: final per-range text and ``fits_in_range`` is a self-report
#: that the merge caller cross-checks against the actual range
#: hash.
RANGE_MERGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "replacement": {"type": "string"},
        "fits_in_range": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["replacement", "fits_in_range", "rationale"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Conflict checks (OPT-5)                                                    #
# --------------------------------------------------------------------------- #

def _check_stale_base_hash(
    suggestions: Sequence[EditSuggestion],
    context: OptimizerContext,
) -> list[dict[str, str]]:
    """Return blockers for any suggestion whose base hash drifted.

    The check compares the suggestion's recorded ``base_hash``
    to the parser-owned ``MutableRange.content_hash`` of the
    target range in the current context. A mismatch is the
    canonical "another writer changed the range under us"
    signal (OPT-1 / OPT-5).
    """
    range_hashes = {r.range_id: r.content_hash for r in context.mutable_ranges}
    blockers: list[dict[str, str]] = []
    for s in suggestions:
        current = range_hashes.get(s.range_id)
        if current is None:
            blockers.append({
                "id": STALE_BASE_HASH_BLOCKER,
                "message": (
                    f"edit_suggestion {s.suggestion_id!r} targets "
                    f"range_id={s.range_id} which is not in the "
                    "current mutable ranges; the base is stale"
                ),
            })
            continue
        if current != s.base_hash:
            blockers.append({
                "id": STALE_BASE_HASH_BLOCKER,
                "message": (
                    f"edit_suggestion {s.suggestion_id!r} "
                    f"base_hash={s.base_hash!r} does not match the "
                    f"current range_id={s.range_id} content_hash="
                    f"{current!r}; the base is stale"
                ),
            })
    return blockers


def _check_routing_violations(
    suggestions: Sequence[EditSuggestion],
    context: OptimizerContext,
) -> list[dict[str, str]]:
    """Return blockers for routing cap / HITL / unsupported range.

      1. At most :data:`ROUTING_SURFACE_CAP` (= 1) selected
         routing edit per round (ADR 0027 / ADR 0032).
      2. A routing edit must carry ``human_confirmed=True`` or
      3. A routing edit must target a routing-surface field
         (``context.routing_surface_fields``); a routing edit
         outside the routing surface is contradictory intent.
    """
    blockers: list[dict[str, str]] = []
    routing_suggestions = [s for s in suggestions if s.routing]
    if len(routing_suggestions) > ROUTING_SURFACE_CAP:
        blockers.append({
            "id": ROUTING_CAP_EXCEEDED_BLOCKER,
            "message": (
                f"{len(routing_suggestions)} selected routing "
                f"edits exceeds the cap of {ROUTING_SURFACE_CAP} "
                "(ADR 0027 / ADR 0032)"
            ),
        })
    for s in routing_suggestions:
        if not (s.human_confirmed or context.human_confirmed):
            blockers.append({
                "id": ROUTING_HITL_UNCONFIRMED_BLOCKER,
                "message": (
                    f"edit_suggestion {s.suggestion_id!r} is a "
                    "routing change without explicit human "
                    "confirmation (ADR 0032)"
                ),
            })
        if s.routing_field and s.routing_field not in context.routing_surface_fields:
            blockers.append({
                "id": MUTABLE_RANGE_CONFLICT_BLOCKER,
                "message": (
                    f"edit_suggestion {s.suggestion_id!r} declares "
                    f"routing_field={s.routing_field!r} which is "
                    "not in the artifact's routing surface"
                ),
            })
    return blockers


def _check_range_overlap(
    suggestions: Sequence[EditSuggestion],
) -> list[dict[str, str]]:
    """Return blockers when two selected suggestions target the same range.

    Two selected edits on the same range is a contradiction;
    the deterministic rule is to either merge them (the
    :func:`_merge_same_range_suggestions` path) or block the
    round. We block before the merge call: the merge step is
    only invoked when the selected set has at most one
    suggestion per range. (This check matters once
    :data:`RANKED_EDIT_BUDGET` >= 2 allows more than one
    selected edit per round; with the budget above 1 a
    same-range pair is reachable. The MVP one-edit budget
    keeps the check degenerate; it stays in the contract for
    ``--max-rounds > 1`` and the bounded multi-edit rounds.)
    """
    seen: dict[int, str] = {}
    blockers: list[dict[str, str]] = []
    for s in suggestions:
        prev = seen.get(s.range_id)
        if prev is not None:
            blockers.append({
                "id": MUTABLE_RANGE_CONFLICT_BLOCKER,
                "message": (
                    f"edit_suggestion {s.suggestion_id!r} targets "
                    f"range_id={s.range_id} which is already "
                    f"targeted by {prev!r}; same-range edits must "
                    "be merged before selection"
                ),
            })
        else:
            seen[s.range_id] = s.suggestion_id
    return blockers


def _check_supported_ranges(
    suggestions: Sequence[EditSuggestion],
    context: OptimizerContext,
) -> list[dict[str, str]]:
    """Return blockers when a selected suggestion targets no mutable range.

    A suggestion whose ``range_id`` is not present in the
    artifact's mutable range set is unsupported (OPT-5
    acceptance: "unsupported ranges" must be blocked).
    """
    range_ids = {r.range_id for r in context.mutable_ranges}
    return [
        {
            "id": MUTABLE_RANGE_CONFLICT_BLOCKER,
            "message": (
                f"edit_suggestion {s.suggestion_id!r} targets "
                f"range_id={s.range_id} which is not in the "
                "artifact's mutable ranges"
            ),
        }
        for s in suggestions
        if s.range_id not in range_ids
    ]


def _check_budget_violations(
    suggestions: Sequence[EditSuggestion],
) -> list[dict[str, str]]:
    """Return blockers when the selected set exceeds the per-round budget.
    The :data:`RANKED_EDIT_BUDGET` (= 4) is the hard upper
    bound: any candidate revision that exceeds the cap is
    rejected before conflict checks. The blocker id is the
    same machine-stable string the cap-exceeded check uses
    so a downstream report can group on it.
    """
    if len(suggestions) > RANKED_EDIT_BUDGET:
        return [
            {
                "id": MUTABLE_RANGE_CONFLICT_BLOCKER,
                "message": (
                    f"{len(suggestions)} selected suggestions "
                    f"exceeds the per-round budget of "
                    f"{RANKED_EDIT_BUDGET}"
                ),
            }
        ]
    return []


def _run_conflict_checks(
    selected: Sequence[EditSuggestion],
    context: OptimizerContext,
) -> list[dict[str, str]]:
    """Aggregate every deterministic conflict check (OPT-5).

    The order is deliberate: stale-base is the cheapest
    check and the most likely to fail in a long-running
    workspace; range-overlap / supported-range follow;
    routing and budget close the gate. The returned list is
    the union of every check's blockers; the caller maps an
    empty list to "no conflict", a non-empty list to a
    blocked round.
    """
    blockers: list[dict[str, str]] = []
    blockers.extend(_check_stale_base_hash(selected, context))
    blockers.extend(_check_routing_violations(selected, context))
    blockers.extend(_check_range_overlap(selected))
    blockers.extend(_check_supported_ranges(selected, context))
    blockers.extend(_check_budget_violations(selected))
    return blockers


# --------------------------------------------------------------------------- #
# Merge / range mutation (OPT-5 / OPT-6)                                     #
# --------------------------------------------------------------------------- #

def _merge_same_range_suggestions(
    *,
    range_id: int,
    base_text: str,
    suggestions: Sequence[EditSuggestion],
    call_fn: Callable[..., Any],
    provider_name: str,
    provider_spec: Mapping[str, Any],
    model: str,
) -> dict[str, Any]:
    """Run the same-range LLM merge (OPT-5).

    Two or more non-conflicting suggestions for the same
    range are merged into a single Patch Revision via a
    single :func:`call_structured` call against
    :data:`RANGE_MERGE_SCHEMA`. The returned ``replacement``
    is the final per-range text; ``fits_in_range`` is a
    self-report that the merge caller cross-checks against
    the actual range hash (a hash mismatch means the merge
    produced text outside the mutable range, which blocks
    the round per ADR 0032).
    """
    result = call_structured(
        provider_name=provider_name,
        provider_spec=provider_spec,
        model=model,
        schema=RANGE_MERGE_SCHEMA,
        call_fn=call_fn,
    )
    if not result.get("ok"):
        raise OptimizerSchemaBlocked(
            f"range_merge range_id={range_id}",
            result.get("blockers") or [],
            result.get("validation_errors") or [],
        )
    value = result.get("value") or {}
    replacement = value.get("replacement")
    fits = bool(value.get("fits_in_range"))
    if not isinstance(replacement, str):
        raise OptimizerSchemaBlocked(
            f"range_merge range_id={range_id}",
            [{"id": STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER,
              "message": "range_merge response missing replacement"}],
            ["$.replacement: expected string"],
        )
    # BLK-1 fix: the historical SHA-256 hash-equality
    # cross-check against base_text was inverted. apply_patch_revision
    # replaces the range's .text wholesale, so the merge
    # produces a NEW text whose hash differs from the base
    # by design. The cross-check made fits_in_range=False for
    # every real edit and blocked the round before
    # apply/evaluate/acceptance. The ``fits_in_range`` flag
    # is now treated as the LLM self-report only; structural
    # validity (non-empty replacement string) is the local
    # contract. ``base_text`` is no longer used here.
    return {
        "replacement": replacement,
        "fits_in_range": fits,
        "rationale": value.get("rationale") or "",
        "source_suggestion_ids": [s.suggestion_id for s in suggestions],
    }
def _build_merge_plan(
    *,
    selected: Sequence[EditSuggestion],
    context: OptimizerContext,
    call_fn: Callable[..., Any] | None,
    provider_name: str,
    provider_spec: Mapping[str, Any],
    model: str,

) -> RangeMergePlan:
    """Build the per-range merge plan (OPT-5).
    For each selected ``range_id`` a single ``replacement``
    ``replacement`` when only one does). The plan is
    deterministic; a same-range merge that produces text
    outside the mutable range flips
    ``merge_outside_mutable_range`` and the round blocks.
    """
    per_range: dict[int, dict[str, Any]] = {}
    base_hashes: dict[int, str] = {}
    blocked_reasons: list[dict[str, str]] = []
    outside = False
    by_range: dict[int, list[EditSuggestion]] = {}
    for s in selected:
        by_range.setdefault(s.range_id, []).append(s)
    for r in context.mutable_ranges:
        base_hashes[r.range_id] = r.content_hash
    for range_id, suggs in by_range.items():
        # Find the base range text for cross-check.
        base_text = ""
        for r in context.mutable_ranges:
            if r.range_id == range_id:
                base_text = r.text
                break
        if len(suggs) == 1:
            # BLK-1 fix: the historical ``fits`` check
            # compared SHA-256 of the replacement against the
            # base text; that is True ONLY for a no-op edit
            # and made every real improvement block the round.
            # The replacement is the bounded per-range text
            # the optimizer intends to substitute; a
            # non-string or empty replacement is the only
            # invalid case (the per-range text must be a
            # string for ``apply_patch_revision`` to
            # substitute; the caller marks the plan
            # outside the mutable range and blocks).
            replacement = suggs[0].replacement
            fits = isinstance(replacement, str) and bool(replacement)
            per_range[range_id] = {
                "replacement": replacement,
                "fits_in_range": fits,
                "source_suggestion_ids": [suggs[0].suggestion_id],
                "rationale": suggs[0].rationale,
            }
            if not fits:
                outside = True
            continue
        if call_fn is None:
            # No LLM available for the merge; the round
            # cannot complete. Record the reason; the
            # runner surfaces it as a BLOCKED outcome.
            blocked_reasons.append({
                "id": MUTABLE_RANGE_CONFLICT_BLOCKER,
                "message": (
                    f"range_id={range_id} has {len(suggs)} "
                    "selected suggestions but no LLM "
                    "call_fn was provided for the same-range "
                    "merge"
                ),
            })
            continue
        merged = _merge_same_range_suggestions(
            range_id=range_id,
            base_text=base_text,
            suggestions=suggs,
            call_fn=call_fn,
            provider_name=provider_name,
            provider_spec=provider_spec,
            model=model,
        )
        per_range[range_id] = merged
        if not merged["fits_in_range"]:
            outside = True
    return RangeMergePlan(
        record_type="range_merge_plan",
        run_id=context.run_id,
        round_id="",  # filled by caller
        timestamp=_now_iso(),
        per_range_plan=per_range,
        base_hashes=base_hashes,
        merge_outside_mutable_range=outside,
        blocked_reasons=blocked_reasons,
    )


# --------------------------------------------------------------------------- #
# Apply / rollback (OPT-6)                                                   #
# --------------------------------------------------------------------------- #

def apply_patch_revision(
    *,
    base_artifact_path: str,
    artifact_kind: str,
    base_artifact_text: str,
    per_range_text: Mapping[int, str],
    mutable_ranges: Sequence[MutableRange],
) -> str:
    """Apply a candidate Patch Revision to the base artifact text.

    The function is *pure*: it does not write the artifact; it
    returns the candidate text. The CLI's caller decides
    whether to commit the new text to disk. The function
    applies the per-range replacements in ``range_id`` order
    so the substitution is deterministic. The function does
    NOT touch the routing surface (the routing edits are
    folded in only when the candidate passed all the
    routing-cap / HITL / supported-range checks).
    """
    if artifact_kind == "subagent":
        # Concatenate the systemPrompt (if any) and the body
        # in declaration order. The artifact text we re-emit
        # is a single string with a frontmatter boundary so
        # the parser round-trips.
        front, body = _split_artifact_text(base_artifact_text)
        system_prompt = ""
        if mutable_ranges:
            first = mutable_ranges[0]
            system_prompt = first.text
        # Apply per-range text in the same order as the
        # mutable ranges (frontmatter systemPrompt first,
        # then body).
        #
        # NB-3 hard contract: range_id==0 is coupled to the
        # subagent ``systemPrompt`` mutable range by parser
        # convention (positional index: systemPrompt=0,
        # body=1). The Skill single-range path is degenerate
        # (only one range exists). If the parser ever emits
        # a third mutable range, this positional coupling
        # must be replaced with a ``range_kind`` field match;
        # today, range_id==0 is the systemPrompt by contract.
        for r in mutable_ranges:
            replacement = per_range_text.get(r.range_id)
            if replacement is None:
                continue
            if r.range_id == 0 and system_prompt:
                system_prompt = replacement
            else:
                body = replacement
        rebuilt = _join_subagent_text(front, system_prompt, body)
        return rebuilt
    # Skill: single mutable range (the body). The body range
    # is always range_id=0.
    front, body = _split_artifact_text(base_artifact_text)
    if mutable_ranges:
        body_replacement = per_range_text.get(mutable_ranges[0].range_id)
        if body_replacement is not None:
            body = body_replacement
    return _join_skill_text(front, body)


def _split_artifact_text(source: str) -> tuple[str, str]:
    """Return ``(frontmatter_text, body_text)`` for an artifact.

    NB-4 fix: this used to mirror
    :func:`metacrucible.artifact._split_frontmatter` with a
    private regex copy. The module already imports types from
    :mod:`artifact` at module load time, so the historical
    "future test stubs artifact" cycle risk is moot. We now
    delegate to the parser's authoritative helper and fall
    back to ``("", source)`` if a caller passes a source
    without a YAML frontmatter block (the parser already
    rejects such sources upstream, so this branch is
    defensive).
    """
    try:
        return _split_frontmatter(source)
    except ValueError:
        return "", source


def _join_skill_text(front: str, body: str) -> str:
    """Re-emit a Skill artifact source from frontmatter + body.

    Mirrors the canonical source shape (``---\\nfront\\n---\\nbody``)
    so the rebuilt source is a drop-in replacement for the
    original.
    """
    if not front:
        return body
    return f"---\n{front}\n---\n{body}"


def _join_subagent_text(front: str, system_prompt: str, body: str) -> str:
    """Re-emit a subagent artifact source from frontmatter + body.

    The systemPrompt is folded back into the frontmatter
    as a block scalar so the parser round-trips. The body
    is appended after the closing ``---`` delimiter.
    """
    if not system_prompt:
        return _join_skill_text(front, body)
    # Insert / replace the ``systemPrompt: |`` block in the
    # frontmatter. The MVP does not reformat unrelated
    # frontmatter keys.
    lines = front.splitlines()
    out_lines: list[str] = []
    inserted = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("systemPrompt:"):
            out_lines.append("systemPrompt: |")
            for sp_line in system_prompt.splitlines():
                out_lines.append(f"  {sp_line}")
            inserted = True
            # Skip lines belonging to the original systemPrompt
            # block scalar.
            i += 1
            while i < len(lines) and (
                lines[i].startswith(" ") or lines[i].startswith("\t")
            ):
                i += 1
            continue
        out_lines.append(line)
        i += 1
    if not inserted:
        out_lines.append("systemPrompt: |")
        for sp_line in system_prompt.splitlines():
            out_lines.append(f"  {sp_line}")
    rebuilt_front = "\n".join(out_lines)
    return f"---\n{rebuilt_front}\n---\n{body}"


def _rollback_artifact_text(
    *,
    base_artifact_path: Path,
    saved_text: bytes,
) -> None:
    """Restore the artifact's source bytes to ``saved_text``.

    The MVP rollback path writes the original bytes back to
    disk. The function is best-effort: an OSError is logged
    to stderr by the caller and the in-memory rejection
    state wins.
    """
    Path(base_artifact_path).write_bytes(saved_text)


# --------------------------------------------------------------------------- #
# Acceptance comparator (OPT-6)                                              #
# --------------------------------------------------------------------------- #

def _eval_split_fail_or_blocked_count(
    case_results: Sequence[Mapping[str, Any]],
) -> int:
    """Count cases whose status is ``FAIL`` or ``BLOCKED``.

    The OPT-6 acceptance rule is concrete: the candidate
    eval-split FAIL+BLOCKED count must be strictly less than
    the baseline eval-split FAIL+BLOCKED count, AND the
    candidate held-out split must introduce zero new
    FAIL/BLOCKED ``case_id``s vs the baseline. This helper
    implements the first half of the rule.
    """
    count = 0
    for r in case_results:
        if not isinstance(r, Mapping):
            continue
        status = r.get("status")
        if status in ("FAIL", "BLOCKED"):
            count += 1
    return count


def _held_out_pass_to_fail_case_ids(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return held-out ``case_id``s with a binary ``PASS`` -> ``FAIL`` regression.

    ``baseline`` / ``candidate`` are the per-case verdicts of
    the held-out split for the baseline and candidate
    artifact. The function returns the sorted list of
    ``case_id``s whose baseline status was exactly ``"PASS"``
    and whose candidate status is exactly ``"FAIL"``. This is
    the strict binary transition guard pinned by ACG-2r /
    Issue #35: only an explicit per-case ``PASS`` -> ``FAIL``
    flip in the held-out split is load-bearing as a
    regression. ``BLOCKED`` -> ``FAIL``, ``PASS`` ->
    ``BLOCKED``, and cases missing from one side are NOT
    regressions (no transition occurred). Cases with a
    non-string ``case_id`` or non-string ``status`` are
    ignored so a missing stable ``case_id`` cannot create a
    false positive. An empty list is the "zero held-out
    regression" condition required for acceptance.
    """
    base_statuses: dict[str, str] = {}
    for r in baseline:
        if not isinstance(r, Mapping):
            continue
        cid = r.get("case_id")
        status = r.get("status")
        if isinstance(cid, str) and isinstance(status, str):
            base_statuses[cid] = status
    out: set[str] = set()
    for r in candidate:
        if not isinstance(r, Mapping):
            continue
        cid = r.get("case_id")
        status = r.get("status")
        if not isinstance(cid, str) or not isinstance(status, str):
            continue
        # Binary transition guard: only PASS -> FAIL flips
        # count as a held-out regression. Any other state
        # delta (BLOCKED -> FAIL, PASS -> BLOCKED,
        # FAIL -> BLOCKED, missing baseline, missing
        # candidate) is NOT a regression.
        if status == "FAIL" and base_statuses.get(cid) == "PASS":
            out.add(cid)
    return sorted(out)


def _eval_split_transitions(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    """Return ``(fail_to_pass_case_ids, pass_to_fail_case_ids)``.

    Issue #35 / ADR 0012 require a per-case binary transition
    comparator for the eval split: the candidate is accepted
    only when at least one ``case_id`` flipped ``FAIL`` ->
    ``PASS`` AND no ``case_id`` flipped ``PASS`` -> ``FAIL``.
    This helper returns those two sorted lists.

    The transition is per ``case_id``: a case is a
    ``FAIL`` -> ``PASS`` transition iff the baseline status
    for that ``case_id`` was exactly ``"FAIL"`` and the
    candidate status for the same ``case_id`` is exactly
    ``"PASS"``. ``BLOCKED`` -> ``PASS`` is NOT a
    ``FAIL`` -> ``PASS`` transition (per Issue #35: only an
    explicit per-case ``FAIL`` -> ``PASS`` counts as the
    eval-gain signal). Cases present in only one split are
    ignored; the comparator keys on pairs.
    """
    base_map: dict[str, str] = {}
    for r in baseline:
        if not isinstance(r, Mapping):
            continue
        cid = r.get("case_id")
        status = r.get("status")
        if isinstance(cid, str) and isinstance(status, str):
            base_map[cid] = status
    cand_map: dict[str, str] = {}
    for r in candidate:
        if not isinstance(r, Mapping):
            continue
        cid = r.get("case_id")
        status = r.get("status")
        if isinstance(cid, str) and isinstance(status, str):
            cand_map[cid] = status
    fail_to_pass: set[str] = set()
    pass_to_fail: set[str] = set()
    for cid, base_status in base_map.items():
        cand_status = cand_map.get(cid)
        if cand_status is None:
            continue
        if base_status == "FAIL" and cand_status == "PASS":
            fail_to_pass.add(cid)
        elif base_status == "PASS" and cand_status == "FAIL":
            pass_to_fail.add(cid)
    return sorted(fail_to_pass), sorted(pass_to_fail)


def compare_eval_held_out(
    *,
    baseline_eval: Sequence[Mapping[str, Any]],
    candidate_eval: Sequence[Mapping[str, Any]],
    baseline_held_out: Sequence[Mapping[str, Any]],
    candidate_held_out: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Run the OPT-6 acceptance comparator (Issue #35 / ADR 0012).

    Acceptance criteria (per Issue #35 / ADR 0012):

      - At least one explicit per-case ``FAIL`` -> ``PASS``
        transition in the eval split (``BLOCKED`` -> ``PASS``
        alone is NOT a valid eval-gain signal).
      - Zero explicit per-case ``PASS`` -> ``FAIL`` transitions
        in the eval split (a regressing ``case_id`` blocks the
        candidate regardless of aggregate counts).
      - Zero per-case ``PASS`` -> ``FAIL`` transitions in
        the held-out split vs the baseline (held-out guard,
        ACG-2r). A ``BLOCKED`` -> ``FAIL`` or missing-side
        delta is NOT a regression. Cases without a stable
        ``case_id`` are ignored (no false positives).

    The function returns a dict with the boolean ``accepted``
    flag, the baseline / candidate eval-split FAIL+BLOCKED
    counts (kept for audit / backward compatibility), the list
    of newly-failing held-out case ids, the sorted
    ``eval_fail_to_pass_case_ids`` / ``eval_pass_to_fail_case_ids``
    transition lists (machine-readable), and a stable reason
    string for the verdict. The reason is one of:

      - ``"accepted"`` — all three criteria pass.
      - ``"eval_regression"`` — at least one per-case
        ``PASS`` -> ``FAIL`` transition in the eval split.
        This is the most specific rejection signal (a
        regressing case blocks acceptance even when an
        improvement is also present).
      - ``"eval_no_improvement"`` — zero per-case
        ``FAIL`` -> ``PASS`` transitions in the eval split
        (including the ``BLOCKED`` -> ``PASS``-only case).
      - ``"held_out_regression"`` — held-out guard tripped
        (the candidate introduced a per-case ``PASS`` ->
        ``FAIL`` transition vs the baseline).
    """
    base_eval_count = _eval_split_fail_or_blocked_count(baseline_eval)
    cand_eval_count = _eval_split_fail_or_blocked_count(candidate_eval)
    fail_to_pass_ids, pass_to_fail_ids = _eval_split_transitions(
        baseline_eval, candidate_eval
    )
    new_held_out_ids = _held_out_pass_to_fail_case_ids(
        baseline_held_out, candidate_held_out
    )
    eval_improved = bool(fail_to_pass_ids)
    eval_no_regression = not pass_to_fail_ids
    held_out_clean = not new_held_out_ids
    accepted = bool(eval_improved) and bool(eval_no_regression) and bool(
        held_out_clean
    )
    if pass_to_fail_ids:
        # Per-case PASS->FAIL regression is the most specific
        # reason: report it before the secondary
        # no-improvement or held-out verdict so an operator
        # can act on the regressing case_id without first
        # scanning the held-out side.
        reason = "eval_regression"
    elif not eval_improved:
        reason = "eval_no_improvement"
    elif not held_out_clean:
        reason = "held_out_regression"
    else:
        reason = "accepted"
    return {
        "accepted": accepted,
        "reason": reason,
        "baseline_eval_fail_blocked_count": base_eval_count,
        "candidate_eval_fail_blocked_count": cand_eval_count,
        "new_held_out_fail_blocked_case_ids": new_held_out_ids,
        "held_out_pass_to_fail_case_ids": new_held_out_ids,
        "eval_fail_to_pass_case_ids": fail_to_pass_ids,
        "eval_pass_to_fail_case_ids": pass_to_fail_ids,
    }


# --------------------------------------------------------------------------- #
# Pipeline runner                                                            #
# --------------------------------------------------------------------------- #

def run_optimizer_pipeline(
    *,
    workspace: Path,
    benchmark_path: Path,
    artifact_path: Path,
    call_fn: Callable[..., Any] | None,
    provider_name: str = "test-provider",
    provider_spec: Mapping[str, Any] | None = None,
    model: str = "test-model",
    max_rounds: int = ROUND_BUDGET_DEFAULT,
    human_confirmed: bool = False,
    eval_call_fn: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> OptimizerPipelineResult:
    """Run the full SkillOpt-shaped pipeline (OPT-1..OPT-8).

    The function builds the context, then for up to
    ``max_rounds`` runs the propose→apply→evaluate→accept
    loop. Each round is bounded; the runner exits with a
    stable status (``ACCEPTED`` / ``REJECTED`` / ``BLOCKED``)
    and the per-record counts the OPT-9 contract expects.

    The function never raises on a schema-validation,
    conflict, or acceptance failure: those paths return a
    ``BLOCKED`` / ``REJECTED`` result with the blockers on
    the ``blockers`` field. The function raises
    :class:`OptimizerSchemaBlocked` only when a structured
    call would otherwise be silently swallowed.

    Parameters
    ----------
    workspace:
        Path to the artifact workspace (the ``init`` root).
    benchmark_path:
        Path to the workspace's ``benchmark.jsonl``.
    artifact_path:
        Path to the artifact under optimization.
    call_fn:
        The :func:`call_structured` callable. Tests inject a
        deterministic fake; production wires a real provider
        call. ``None`` disables the LLM-backed stages; the
        pipeline still runs the deterministic checks and the
        acceptance evaluation.

        NOTE (MVP scope / NB-5 follow-up): the CLI always
        passes ``call_fn=None`` (see :func:`cmd_optimize`).
        Production wiring of a real ``call_fn`` from the
        resolved provider config is post-MVP; until wired,
        the optimizer emits REJECTED outcomes with no
        candidate edits on every production run.
    provider_name / provider_spec / model:
        Forwarded to :func:`call_structured` and
        :func:`record_provider_run_outcome`.
    max_rounds:
        Round budget. The pipeline stops after this many
        rounds even if the acceptance comparator would
        accept another candidate. ``1`` matches the
        PRD F3 minimal safe default.
    human_confirmed:
        Routing-change human confirmation (OPT-4 HITL
        gate). ``False`` means any selected routing edit
        is blocked.
    eval_call_fn:
        The per-case evaluator (defaults to the F1
        :func:`metacrucible.__main__._evaluate_single_case`
        dispatcher). Tests inject a deterministic stub.
    """
    from .__main__ import _evaluate_single_case  # local import; avoid cycle

    provider_spec_map: dict[str, Any] = (
        dict(provider_spec) if isinstance(provider_spec, Mapping) else {}
    )
    repo = RepositoryStorage(workspace)
    global_store = UserGlobalStorage()

    context = build_optimizer_context(
        workspace=workspace,
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        max_rounds=max_rounds,
        human_confirmed=human_confirmed,
    )

    # 1. Persist the run-level start record. The record is the
    #    "we started" event in the audit lineage; the runner
    #    records every round / merge / acceptance event the
    #    same way.
    _append_history(
        repo,
        {
            "event": "optimize_started",
            "run_id": context.run_id,
            "workspace": context.workspace,
            "artifact_path": context.artifact_path,
            "base_content_hash": context.base_content_hash,
            "max_rounds": context.max_rounds,
            "human_confirmed": context.human_confirmed,
            "timestamp": _now_iso(),
        },
    )

    record_counts: dict[str, int] = {
        "case_reflection": 0,
        "round_reflection": 0,
        "edit_suggestion": 0,
        "ranked_edit_set": 0,
        "range_merge_plan": 0,
        "generated_case_suggestion": 0,
    }
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    selected_candidate_ids: list[str] = []
    best_revision: dict[str, Any] | None = None
    acceptance_decision: dict[str, Any] = {
        "accepted": False,
        "reason": "no_candidate",
    }
    accepted_status = "REJECTED"
    rollback_paths: list[tuple[Path, bytes]] = []  # for run-level rollback
    base_artifact_text = Path(context.artifact_path).read_bytes()

    # 2. Baseline evaluation. The baseline runs once at the
    #    start of the run; the candidate evaluations (one per
    #    round) are compared against it. The MVP uses the
    #    existing :func:`_evaluate_single_case` per case so
    #    the optimizer shares the F1 evaluation engine.
    baseline = load_benchmark(benchmark_path)
    if not baseline.eligible_eval_cases:
        blockers.append({
            "id": "missing-reviewed-eval-case",
            "message": (
                "optimizer requires at least one eligible "
                "reviewed eval case (ADR 0025)"
            ),
        })
    if not baseline.eligible_held_out_cases:
        blockers.append({
            "id": "missing-reviewed-held-out-case",
            "message": (
                "optimizer requires at least one eligible "
                "reviewed held-out case (ADR 0025)"
            ),
        })
    if blockers:
        # Precondition failure: the loader-level blockers
        # (passed through by the caller) are run-blockers
        # and the run does not enter the round loop.
        receipt = {
            "run_id": context.run_id,
            "run_type": "optimize",
            "status": "BLOCKED",
            "blockers": blockers,
        }
        summary = {"status": "BLOCKED", "blockers": blockers}
        trajectory = {
            "status": "BLOCKED",
            "steps": [
                {"step": idx, "action": "blocked", "status": "BLOCKED",
                 "blocker": b}
                for idx, b in enumerate(blockers)
            ],
        }
        evidence_refs = _emit_evidence_bundle(
            global_store=global_store,
            run_id=context.run_id,
            receipt=receipt,
            summary=summary,
            trajectory=trajectory,
        )
        _append_history(
            repo,
            {
                "event": "optimize_blocked",
                "run_id": context.run_id,
                "blockers": blockers,
                "timestamp": _now_iso(),
            },
        )
        return OptimizerPipelineResult(
            status="BLOCKED",
            run_id=context.run_id,
            rounds=0,
            record_counts=record_counts,
            evidence_refs=evidence_refs,
            blockers=blockers,
            warnings=warnings,
            best_revision=None,
            acceptance_decision=acceptance_decision,
            selected_candidate_ids=[],
            stop_reason=STOP_REASON_PRECONDITION_BLOCKED,
        )

    # 3. Per-case reflections (OPT-3) over failed / weak
    #    eval cases. The MVP call_fn is None-tolerant: when
    #    no LLM is wired, the runner writes a no-op
    #    case_reflection record (bounded, schema-valid, with
    #    a "no provider" rationale) so the OPT-9 record
    #    count contract still passes and a downstream tool
    #    can branch on ``rationale`` to detect the
    #    no-provider path.
    def _eval_case(case: Mapping[str, Any]) -> Mapping[str, Any]:
        if eval_call_fn is not None:
            return eval_call_fn(case)
        return _evaluate_single_case(case)

    baseline_eval_results = [
        _eval_case(c) for c in baseline.eligible_eval_cases
    ]
    baseline_held_out_results = [
        _eval_case(c) for c in baseline.eligible_held_out_cases
    ]

    # Failed / weak eval cases for the per-case reflection
    # step. "Weak" is a policy: for the MVP we reflect on
    # every non-PASS case so the runner has at least one
    # ``case_reflection`` per non-passing case; the record
    # count contract (OPT-9) is then deterministic.
    weak_eval_cases = [
        c for c, r in zip(
            baseline.eligible_eval_cases, baseline_eval_results
        )
        if isinstance(r, Mapping) and r.get("status") != "PASS"
    ]
    bounded_rejected_themes: list[dict[str, str]] = []

    rounds_attempted = 0
    # ``stop_reason`` is the machine-stable termination
    # reason the pipeline writes onto
    # :class:`OptimizerPipelineResult` and onto the
    # ``optimize_finished`` history event. It is set
    # locally inside the round loop on every break path
    # and at the end of the function. The default is
    # ``max_rounds_reached`` so a clean exhaustion of
    # ``range(1, context.max_rounds + 1)`` records that
    # reason; explicit break paths overwrite it.
    stop_reason: str = STOP_REASON_MAX_ROUNDS_REACHED
    try:
        for round_idx in range(1, context.max_rounds + 1):
            rounds_attempted = round_idx
            round_id = f"round-{round_idx:02d}"

            # 3a. Per-case reflections. The MVP derives
            #     case_reflection locally from the case's
            #     eval result: it is a fixed-shape per-case
            #     extract, not a synthesis step. The LLM
            #     is reserved for the round-level
            #     synthesis in 3b. This keeps the
            #     case_reflection record deterministic
            #     and bounded, and avoids a per-case LLM
            #     round-trip that adds latency without
            #     changing the record content.
            for case in weak_eval_cases:
                case_id = str(case.get("case_id", "?"))
                if call_fn is None:
                    rationale = (
                        "no LLM call_fn wired; per-case "
                        "reflection recorded with no-op rationale"
                    )
                else:
                    rationale = (
                        f"case {case_id!r} failed baseline eval; "
                        "derive per-case reflection from eval result"
                    )
                rec = CaseReflection(
                    record_type="case_reflection",
                    run_id=context.run_id,
                    round_id=round_id,
                    case_id=case_id,
                    timestamp=_now_iso(),
                    rationale=rationale,
                    source_refs=[
                        f"benchmark:{benchmark_path.name}",
                        f"case_id:{case_id}",
                    ],
                )
                _append_history(repo, rec.as_dict())
                record_counts["case_reflection"] += 1

            # 3b. Per-round synthesis. The call is bounded
            #     to the eval-split reflections and the
            #     rejected-theme summaries; held-out
            #     evidence is never threaded in.
            if call_fn is None:
                round_value: dict[str, Any] = {
                    "rationale": "no LLM call_fn wired; round synthesis recorded with no-op rationale",
                    "suggested_edits": [],
                }
            else:
                try:
                    structured = _call_structured_with_evidence(
                        provider_name=provider_name,
                        provider_spec=provider_spec_map,
                        model=model,
                        schema=ROUND_REFLECTION_SCHEMA,
                        call_fn=call_fn,  # type: ignore[arg-type]
                        repo=repo,
                        run_id=context.run_id,
                        round_id=round_id,
                        step_label=f"round_reflection:{round_id}",
                    )
                except OptimizerSchemaBlocked as exc:
                    blockers.append({
                        "id": SCHEMA_VALIDATION_BLOCKED,
                        "message": (
                            f"round_reflection schema validation "
                            f"failed for {round_id}: "
                            f"{exc.validation_errors!r}"
                        ),
                    })
                    raise _RoundBlocked(round_id, blockers) from exc
                round_value = structured.get("value") or {}
            # NB-2: clip to THEME_SUMMARY_BUDGET before
            # re-injection so a long run's rejected buffer
            # stays bounded per ADR 0032. The list is
            # ordered oldest-first; keep the most recent
            # entries (the most relevant guidance for the
            # next round).
            if len(bounded_rejected_themes) > THEME_SUMMARY_BUDGET:
                clipped_themes = list(
                    bounded_rejected_themes[-THEME_SUMMARY_BUDGET:]
                )
            else:
                clipped_themes = list(bounded_rejected_themes)
            round_rec = RoundReflection(
                record_type="round_reflection",
                run_id=context.run_id,
                round_id=round_id,
                timestamp=_now_iso(),
                rationale=str(round_value.get("rationale") or ""),
                bounded_rejected_themes=clipped_themes,
                source_refs=[
                    f"benchmark:{benchmark_path.name}",
                    f"round_id:{round_id}",
                ],
            )
            _append_history(repo, round_rec.as_dict())
            record_counts["round_reflection"] += 1

            # 3c. Edit suggestions + generated_case_suggestion
            #     records. Each suggestion is validated against
            #     the parser-owned content_hash of its target
            #     range; a hash mismatch is recorded as a
            #     bounded theme summary (rejected) and a
            #     STALE_BASE_HASH_BLOCKER is logged for the
            #     current round. The mismatch suggestion is
            #     *not* persisted as an edit_suggestion; only
            #     suggestions whose base_hash matches the
            #     current range are persisted.
            suggestions: list[EditSuggestion] = []
            round_suggestions_raw = round_value.get("suggested_edits") or []
            if not isinstance(round_suggestions_raw, list):
                round_suggestions_raw = []
            range_hashes = {
                r.range_id: r.content_hash
                for r in context.mutable_ranges
            }
            for idx, raw in enumerate(round_suggestions_raw):
                if not isinstance(raw, Mapping):
                    continue
                range_id = raw.get("range_id")
                base_hash = raw.get("base_hash")
                if not isinstance(range_id, int) or not isinstance(base_hash, str):
                    # Drop the malformed suggestion; do not
                    # append a record. The runner still has
                    # the round's rationale + reflections.
                    bounded_rejected_themes.append(_bounded_theme_entry(
                        kind="malformed_suggestion",
                        reason="missing range_id or base_hash",
                        avoid="ensure range_id is int and base_hash is a 64-char hex digest",
                    ))
                    continue
                if range_hashes.get(range_id) != base_hash:
                    bounded_rejected_themes.append(_bounded_theme_entry(
                        kind="stale_base_hash",
                        reason=(
                            f"suggestion range_id={range_id} base_hash "
                            "does not match current range content_hash"
                        ),
                        avoid="re-derive base_hash from the parser-owned content_hash",
                    ))
                    continue
                routing = bool(raw.get("routing"))
                routing_field = str(raw.get("routing_field") or "")
                human_confirmed_suggestion = bool(
                    raw.get("human_confirmed") or context.human_confirmed
                )
                if routing and routing_field and routing_field not in context.routing_surface_fields:
                    # Contradictory intent: routing edit on a
                    # non-routing field. Drop and theme.
                    bounded_rejected_themes.append(_bounded_theme_entry(
                        kind="contradictory_routing_intent",
                        reason=(
                            f"routing edit on non-routing field "
                            f"{routing_field!r}"
                        ),
                        avoid="only set routing=True for routing-surface fields",
                    ))
                    continue
                suggestion = EditSuggestion(
                    record_type="edit_suggestion",
                    suggestion_id=f"{round_id}-sug-{idx:02d}",
                    run_id=context.run_id,
                    round_id=round_id,
                    timestamp=_now_iso(),
                    range_id=range_id,
                    base_hash=base_hash,
                    intent=str(raw.get("intent") or ""),
                    replacement=str(raw.get("replacement") or ""),
                    rationale=str(raw.get("rationale") or ""),
                    routing=routing,
                    human_confirmed=human_confirmed_suggestion,
                    routing_field=routing_field,
                )
                gen = raw.get("generated_case_suggestion")
                if isinstance(gen, Mapping):
                    suggestion.generated_case_suggestion = dict(gen)
                _append_history(repo, suggestion.as_dict())
                record_counts["edit_suggestion"] += 1
                suggestions.append(suggestion)
                if suggestion.generated_case_suggestion is not None:
                    gen_rec = GeneratedCaseSuggestion(
                        record_type="generated_case_suggestion",
                        suggestion_id=f"{suggestion.suggestion_id}-case",
                        run_id=context.run_id,
                        round_id=round_id,
                        timestamp=_now_iso(),
                        case_draft=dict(suggestion.generated_case_suggestion),
                        rationale=suggestion.rationale,
                    )
                    _append_history(repo, gen_rec.as_dict())
                    record_counts["generated_case_suggestion"] += 1

            # 3d. Rank / clip / routing-cap / HITL.
            if not suggestions:
                # No usable suggestions this round; the
                # pipeline cannot improve and we exit with a
                # rejected / no-candidate verdict.
                ranked = RankedEditSet(
                    record_type="ranked_edit_set",
                    run_id=context.run_id,
                    round_id=round_id,
                    timestamp=_now_iso(),
                    ordered_candidates=[],
                    rejected=[],
                    selected=[],
                )
                _append_history(repo, ranked.as_dict())
                record_counts["ranked_edit_set"] += 1
                warnings.append({
                    "id": "no_candidate_edits",
                    "message": (
                        f"round {round_id} produced no usable "
                        "edit suggestions; stopping"
                    ),
                })
                stop_reason = STOP_REASON_NO_CANDIDATE_EDITS
                break
            # Rank: deterministic — the LLM-provided
            # order is the priority order. The MVP does
            # not re-rank.
            ordered = [s.suggestion_id for s in suggestions]
            # Routing cap: cap the selected set to one
            # routing change max (and the budget to
            # RANKED_EDIT_BUDGET total).
            selected: list[EditSuggestion] = []
            rejected: list[dict[str, str]] = []
            routing_count = 0
            for s in suggestions:
                if s.routing:
                    if routing_count >= ROUTING_SURFACE_CAP:
                        rejected.append({
                            "suggestion_id": s.suggestion_id,
                            "reason_id": ROUTING_CAP_EXCEEDED_BLOCKER,
                            "reason": (
                                "routing cap exceeded; one "
                                "routing edit per round max"
                            ),
                        })
                        bounded_rejected_themes.append(_bounded_theme_entry(
                            kind="routing_cap_exceeded",
                            reason=(
                                "routing cap exceeded; one "
                                "routing edit per round max"
                            ),
                            avoid="split routing changes across rounds or merge into a single field",
                        ))
                        continue
                    if not (s.human_confirmed or context.human_confirmed):
                        rejected.append({
                            "suggestion_id": s.suggestion_id,
                            "reason_id": ROUTING_HITL_UNCONFIRMED_BLOCKER,
                            "reason": (
                                "routing change without "
                                "explicit human confirmation"
                            ),
                        })
                        bounded_rejected_themes.append(_bounded_theme_entry(
                            kind="routing_hitl_unconfirmed",
                            reason=(
                                "routing change without explicit "
                                "human confirmation"
                            ),
                            avoid="re-issue with --confirm-routing or set human_confirmed=True on the suggestion",
                        ))
                        continue
                    routing_count += 1
                if len(selected) >= RANKED_EDIT_BUDGET:
                    rejected.append({
                        "suggestion_id": s.suggestion_id,
                        "reason_id": MUTABLE_RANGE_CONFLICT_BLOCKER,
                        "reason": (
                            "per-round budget exceeded"
                        ),
                    })
                    bounded_rejected_themes.append(_bounded_theme_entry(
                        kind="budget_exceeded",
                        reason=(
                            f"per-round budget of "
                            f"{RANKED_EDIT_BUDGET} exceeded"
                        ),
                        avoid="clip to the per-round budget or use a different intent",
                    ))
                    continue
                selected.append(s)
            ranked = RankedEditSet(
                record_type="ranked_edit_set",
                run_id=context.run_id,
                round_id=round_id,
                timestamp=_now_iso(),
                ordered_candidates=ordered,
                rejected=rejected,
                selected=[s.suggestion_id for s in selected],
            )
            _append_history(repo, ranked.as_dict())
            record_counts["ranked_edit_set"] += 1
            if not selected:
                # Nothing selected this round; the candidate
                # is empty and we stop.
                warnings.append({
                    "id": "no_candidate_selected",
                    "message": (
                        f"round {round_id} rejected every "
                        "suggestion; stopping"
                    ),
                })
                stop_reason = STOP_REASON_NO_CANDIDATE_SELECTED
                break

            # 3e. Conflict checks (OPT-5) BEFORE mutation.
            conflict_blockers = _run_conflict_checks(selected, context)
            if conflict_blockers:
                blockers.extend(conflict_blockers)
                raise _RoundBlocked(round_id, conflict_blockers)

            # 3f. Range merge plan (OPT-5). The merge plan
            #     records the per-range final text plus
            #     the base_hashes for stale detection in
            #     the next round (if any). A merge outside
            #     the mutable range blocks the round
            #     without mutation.
            plan = _build_merge_plan(
                selected=selected,
                context=context,
                call_fn=call_fn,
                provider_name=provider_name,
                provider_spec=provider_spec_map,
                model=model,
            )
            plan.round_id = round_id
            _append_history(repo, plan.as_dict())
            record_counts["range_merge_plan"] += 1
            if plan.merge_outside_mutable_range or plan.blocked_reasons:
                blockers.extend(plan.blocked_reasons)
                if plan.merge_outside_mutable_range:
                    blockers.append({
                        "id": MUTABLE_RANGE_CONFLICT_BLOCKER,
                        "message": (
                            f"round {round_id} merge produced "
                            "text outside the mutable range; "
                            "round blocked, no mutation"
                        ),
                    })
                raise _RoundBlocked(round_id, blockers)

            # 3g. Apply candidate (OPT-6). The MVP writes
            #     the new artifact to disk so the candidate
            #     evaluation can read it; the rollback path
            #     restores the original bytes on rejection.
            try:
                candidate_text = apply_patch_revision(
                    base_artifact_path=context.artifact_path,
                    artifact_kind=context.artifact_kind,
                    base_artifact_text=base_artifact_text.decode("utf-8"),
                    per_range_text={
                        k: str(v.get("replacement", ""))
                        for k, v in plan.per_range_plan.items()
                    },
                    mutable_ranges=context.mutable_ranges,
                )
            except Exception as exc:  # noqa: BLE001
                blockers.append({
                    "id": MUTABLE_RANGE_CONFLICT_BLOCKER,
                    "message": (
                        f"round {round_id} apply failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                })
                raise _RoundBlocked(round_id, blockers) from exc

            saved_text = base_artifact_text
            Path(context.artifact_path).write_text(
                candidate_text, encoding="utf-8"
            )
            rollback_paths.append((Path(context.artifact_path), saved_text))

            # 3h. Evaluate eval + held-out splits on the
            #     candidate artifact. The evaluator is the
            #     existing F1 dispatcher; held-out
            #     evaluation runs only after the candidate
            #     is materialized (OPT-6 / ADR 0032).
            candidate_eval_results = [
                _eval_case(c) for c in baseline.eligible_eval_cases
            ]
            candidate_held_out_results = [
                _eval_case(c) for c in baseline.eligible_held_out_cases
            ]
            decision = compare_eval_held_out(
                baseline_eval=baseline_eval_results,
                candidate_eval=candidate_eval_results,
                baseline_held_out=baseline_held_out_results,
                candidate_held_out=candidate_held_out_results,
            )
            # ACG-5r / Issue #35: always wrap the verdict in a
            # nested structure so callers can read
            # ``acceptance_decision["comparator"]`` and
            # ``acceptance_decision["profiles"]`` regardless of
            # accept/reject. The accept branch below overrides
            # the profiles sub-dict with a real profile verdict.
            acceptance_decision = {
                "comparator": decision,
                "profiles": {
                    "accepted": True,
                    "blockers": [],
                    "supplemental_findings": [],
                    "not_evaluated": True,
                },
            }
            if decision["accepted"]:
                # ACG-5r / Issue #35: after the comparator accepts,
                # run triggered static-review profiles against the
                # candidate artifact and aggregate through
                # evaluate_acceptance. A triggered BLOCKING profile
                # that FAILs / BLOCKs (e.g. secret-privacy-risk on a
                # candidate body that carries AKIAIOSFODNN7EXAMPLE)
                # must flip the run to BLOCKED, roll back the
                # artifact, and append an optimize_blocked event so
                # the audit lineage captures the profile-blocker
                # cause rather than silently committing an unsafe
                # candidate.
                routing_touched = any(s.routing for s in selected)
                triggered_specs = select_triggers(
                    routing_touched=routing_touched,
                )
                if triggered_specs:
                    # Build the shared review_input from the
                    # post-apply candidate text plus the routing
                    # surface of the selected suggestions.
                    review_body = candidate_text
                    review_input: dict[str, Any] = {
                        "body": review_body,
                        "routing_changes": [
                            {
                                "field": s.routing_field,
                                "new": s.replacement,
                            }
                            for s in selected
                            if s.routing and s.routing_field
                        ],
                        "human_confirmed": bool(
                            context.human_confirmed
                        ),
                        "portability": {"target": "runtime_neutral"},
                        "reviewed_fake_secrets": (),
                    }
                    profile_results = evaluate_profile_specs(
                        triggered_specs, review_input
                    )
                    profile_specs_map: dict[str, Any] = {
                        spec.id: spec for spec in triggered_specs
                    }
                    profile_verdict = evaluate_acceptance(
                        profile_results,
                        profile_specs=profile_specs_map,
                    )
                else:
                    profile_verdict = {
                        "accepted": True,
                        "blockers": [],
                        "supplemental_findings": [],
                    }
                acceptance_decision = {
                    "comparator": decision,
                    "profiles": profile_verdict,
                }
                # Profile acceptance is the union of the
                # comparator's verdict and the profile verdict:
                # a non-accept on either side BLOCKs the run.
                profile_accepted = bool(
                    profile_verdict.get("accepted", True)
                )
                if not profile_accepted:
                    profile_blockers: list[dict[str, str]] = list(
                        profile_verdict.get("blockers", []) or []
                    )
                    if profile_blockers:
                        blockers.extend(profile_blockers)
                    else:
                        blockers.append({
                            "id": "static-review-profile-blocked",
                            "message": (
                                "triggered static-review profile "
                                "returned non-PASS verdict; see "
                                "acceptance_decision.profiles for "
                                "per-profile details"
                            ),
                        })
                    # Profile-side BLOCKED: roll back the
                    # candidate write so the on-disk artifact is
                    # the pre-run bytes, and break out of the
                    # round loop with BLOCKED status.
                    if rollback_paths:
                        p, saved = rollback_paths.pop()
                        _rollback_artifact_text(
                            base_artifact_path=p, saved_text=saved
                        )
                    accepted_status = "BLOCKED"
                    _append_history(
                        repo,
                        {
                            "event": "optimize_blocked",
                            "run_id": context.run_id,
                            "round_id": round_id,
                            "blockers": list(profile_blockers) or list(
                                profile_verdict.get(
                                    "blockers", []
                                ) or []
                            ),
                            "timestamp": _now_iso(),
                        },
                    )
                    break
                accepted_status = "ACCEPTED"
                best_revision = {
                    "run_id": context.run_id,
                    "round_id": round_id,
                    "artifact_path": context.artifact_path,
                    "artifact_text_sha256": hashlib.sha256(
                        candidate_text.encode("utf-8")
                    ).hexdigest(),
                    "per_range_text": {
                        str(k): str(v.get("replacement", ""))
                        for k, v in plan.per_range_plan.items()
                    },
                    "acceptance_decision": decision,
                }
                selected_candidate_ids = [s.suggestion_id for s in selected]
                # Optimistic outcome: roll back the rollback
                # record (the artifact is intentionally
                # updated).
                rollback_paths.pop()
                _append_history(
                    repo,
                    {
                        "event": "optimize_accepted",
                        "run_id": context.run_id,
                        "round_id": round_id,
                        "decision": decision,
                        "timestamp": _now_iso(),
                    },
                )
                stop_reason = STOP_REASON_ACCEPTED
                break
            else:
                # Reject the candidate: restore the base
                # bytes. Run-level rollback is handled by
                # the outer try / finally so a mid-round
                # failure also restores.
                if rollback_paths:
                    p, saved = rollback_paths.pop()
                    _rollback_artifact_text(
                        base_artifact_path=p, saved_text=saved
                    )
                selected_candidate_ids = [s.suggestion_id for s in selected]
                _append_history(
                    repo,
                    {
                        "event": "optimize_rejected",
                        "run_id": context.run_id,
                        "round_id": round_id,
                        "decision": decision,
                        "timestamp": _now_iso(),
                    },
                )
                if context.max_rounds <= 1:
                    # PRD F3 default budget is 1; a single
                    # rejection stops the run.
                    break
    except _RoundBlocked as rb:
        # Restore the artifact if the round produced
        # partial writes.
        while rollback_paths:
            p, saved = rollback_paths.pop()
            _rollback_artifact_text(
                base_artifact_path=p, saved_text=saved
            )
        accepted_status = "BLOCKED"
        blockers.extend(rb.blockers)
        stop_reason = STOP_REASON_ROUND_BLOCKED
        # NB-6: annotate the round-blocked event in the
        # history lineage so a downstream audit can detect
        # which round tripped the gate (the run-level
        # BLOCKED status is composed at the evidence layer;
        # this marker carries the per-round cause).
        _append_history(
            repo,
            {
                "event": "optimize_blocked",
                "run_id": context.run_id,
                "round_id": rb.round_id,
                "blockers": list(rb.blockers),
                "timestamp": _now_iso(),
            },
        )
    finally:
        # Defensive: any leftover rollback path is honored
        # so a crashed round never leaves a half-mutated
        # artifact.
        while rollback_paths:
            p, saved = rollback_paths.pop()
            _rollback_artifact_text(
                base_artifact_path=p, saved_text=saved
            )

    # 4. Persist the evidence bundle for the run. The
    #    bundle's ``status`` is the final pipeline status
    #    (``ACCEPTED`` / ``REJECTED`` / ``BLOCKED``); the
    #    summary carries the record counts and the
    #    acceptance decision; the trajectory digest is a
    #    bounded redacted narrative of the rounds.
    final_status = "BLOCKED" if blockers else accepted_status
    receipt = {
        "run_id": context.run_id,
        "run_type": "optimize",
        "status": final_status,
        "blockers": blockers,
        "rounds": rounds_attempted,
        "max_rounds": context.max_rounds,
        "acceptance_decision": acceptance_decision,
    }
    summary = {
        "status": final_status,
        "rounds": rounds_attempted,
        "max_rounds": context.max_rounds,
        "record_counts": dict(record_counts),
        "warnings": warnings,
        "blockers": blockers,
        "best_revision": (
            None if best_revision is None
            else {
                "run_id": best_revision["run_id"],
                "round_id": best_revision["round_id"],
                "artifact_path": best_revision["artifact_path"],
            }
        ),
    }
    trajectory = {
        "status": final_status,
        "steps": [
            {
                "step": idx,
                "action": "round",
                "round_id": f"round-{idx:02d}",
                "status": final_status,
                "record_counts": dict(record_counts),
            }
            for idx in range(1, rounds_attempted + 1)
        ]
        + [
            {"step": rounds_attempted + 1, "action": "decision",
             "status": final_status, "decision": acceptance_decision}
        ],
    }
    evidence_refs = _emit_evidence_bundle(
        global_store=global_store,
        run_id=context.run_id,
        receipt=receipt,
        summary=summary,
        trajectory=trajectory,
    )

    _append_history(
        repo,
        {
            "event": "optimize_finished",
            "run_id": context.run_id,
            "status": final_status,
            "rounds": rounds_attempted,
            "record_counts": dict(record_counts),
            "blockers": blockers,
            "warnings": warnings,
            "stop_reason": stop_reason,
            "timestamp": _now_iso(),
        },
    )

    return OptimizerPipelineResult(
        status=final_status,
        run_id=context.run_id,
        rounds=rounds_attempted,
        record_counts=record_counts,
        evidence_refs=evidence_refs,
        blockers=blockers,
        warnings=warnings,
        best_revision=best_revision,
        acceptance_decision=acceptance_decision,
        selected_candidate_ids=selected_candidate_ids,
        stop_reason=stop_reason,
    )


# --------------------------------------------------------------------------- #
# Bounded theme summary helper (OPT-4)                                       #
# --------------------------------------------------------------------------- #

def _bounded_theme_entry(
    *, kind: str, reason: str, avoid: str
) -> dict[str, str]:
    """Build a bounded theme summary for re-injection in later rounds.

    Per ADR 0032, rejected edit buffers are injected into later
    rounds only as bounded theme summaries (reasons + avoid
    guidance), not as raw unbounded suggestions or held-out
    evidence. Each entry is clipped to
    :data:`THEME_SUMMARY_MAX_CHARS` and the list is clipped to
    :data:`THEME_SUMMARY_BUDGET` entries by the caller.
    """
    def _clip(text: str) -> str:
        if len(text) <= THEME_SUMMARY_MAX_CHARS:
            return text
        return text[:THEME_SUMMARY_MAX_CHARS] + "..."

    return {
        "kind": kind,
        "reason": _clip(reason),
        "avoid": _clip(avoid),
    }


class _RoundBlocked(Exception):
    """Internal control-flow signal: the current round is blocked.

    The exception carries the round id and the blockers list
    so the runner can record the failure and roll back any
    partial writes. It is private to the module; the public
    surface never raises this exception type.
    """

    def __init__(self, round_id: str, blockers: Sequence[Mapping[str, str]]) -> None:
        super().__init__(round_id)
        self.round_id = round_id
        self.blockers = [dict(b) for b in blockers]
