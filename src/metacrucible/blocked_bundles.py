"""Minimal ``BLOCKED`` evidence bundle helper and policy matrix.

ADR 0035 pins which command/stage categories must emit a minimal
``BLOCKED`` evidence bundle when blocked, and which must not.
This module codifies that policy as a stable, code-readable matrix
and provides a single helper that emits the minimal bundle via the
existing :class:`metacrucible.storage.UserGlobalStorage` writers
introduced for Issue #26.

The matrix is the source of truth
---------------------------------

The policy is documented in ADR 0035, but documentation drifts. To
keep the contract enforceable from code, the policy is exposed as
two frozensets:

  - :data:`REQUIRES_BLOCKED_BUNDLE_CATEGORIES` — categories that
    must write a minimal ``BLOCKED`` bundle when blocked
  - :data:`NON_EMITTING_BLOCKED_CATEGORIES` — categories that
    must NOT write a bundle (their blockers surface through CLI
    output only)

Callers that need to decide whether to emit a bundle call
:func:`requires_blocked_bundle` rather than re-deriving the
membership. The function is intentionally tiny so the policy check
stays a one-liner at every callsite.

The helper
----------

:func:`write_blocked_bundle` writes exactly three files inside the
evidence bundle directory (created via
:meth:`UserGlobalStorage.evidence_bundle_dir`):

  - ``receipt.json`` — bundle entrypoint; carries ``status=BLOCKED``,
    ``run_type``, the run id, the normalised blockers list, and any
    stable identity fields the caller provided
  - ``summary.json`` — aggregate view; carries ``status=BLOCKED``
    and the same normalised blockers list
  - ``trajectory-digest.json`` — bounded, redacted narrative; carries
    ``status=BLOCKED`` and one step per blocker (no raw events, no
    transcripts, no full model output)

The helper reuses the Issue #26 builders
(:func:`metacrucible.storage.build_receipt_payload`,
:func:`build_summary_payload`, and
:func:`build_trajectory_digest_payload`) via the existing
``UserGlobalStorage.write_*`` writers. No new schema is invented;
the on-disk artifacts satisfy the v1 evidence-bundle contract
(``schema_version`` stamped, default sibling refs, summary
allowlist, no absolute paths in machine evidence) by construction.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .storage import UserGlobalStorage, _scrub_string

__all__ = [
    "BLOCKED_STATUS",
    "NON_EMITTING_BLOCKED_CATEGORIES",
    "REQUIRES_BLOCKED_BUNDLE",
    "REQUIRES_BLOCKED_BUNDLE_CATEGORIES",
    "requires_blocked_bundle",
    "write_blocked_bundle",
]


#: Stable ``status`` value written into the receipt, summary, and
#: trajectory digest of a minimal ``BLOCKED`` bundle (ADR 0030 +
#: ADR 0035). Pinned as a module constant so a future change is a
#: deliberate, single-site update.
BLOCKED_STATUS = "BLOCKED"


#: Command/stage categories that MUST emit a minimal ``BLOCKED``
#: evidence bundle when blocked (ADR 0035):
#:
#:   - ``baseline_create`` — ``baseline create`` was a blocker.
#:   - ``evaluate`` — evaluation could not proceed.
#:   - ``optimize`` — an optimization round could not proceed.
#:   - ``synthesize_evaluation_stage`` — the evaluation stage of
#:     ``synthesize`` was blocked (synthesis itself is non-emitting
#:     for ordinary cases; only the evaluation stage emits).
#:   - ``review_execution_requested`` — ``review`` was blocked when
#:     execution was requested (review without execution is
#:     non-emitting).
#:
#: Order is not significant; the set is the contract.
REQUIRES_BLOCKED_BUNDLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "baseline_create",
        "evaluate",
        "optimize",
        "synthesize_evaluation_stage",
        "review_execution_requested",
    }
)


#: Command/stage categories that MUST NOT emit a ``BLOCKED`` bundle
#: when blocked (ADR 0035). Blockers are reported through the CLI
#: only (human output and/or parseable ``--json`` output); no
#: evidence bundle is written.
#:
#:   - ``init`` — initialization is a support command; ``init
#:     --check`` reports the missing-reviewed-case blocker through
#:     CLI output only.
#:   - ``inspect`` — inspection is a read-only view of state.
#:   - ``bootstrap`` — ordinary bootstrap; only the evaluation
#:     stage of ``synthesize`` emits, not the bootstrap itself.
#:   - ``promote`` — promotion is a repository-side write that
#:     already records its outcome in the benchmark JSONL.
NON_EMITTING_BLOCKED_CATEGORIES: frozenset[str] = frozenset(
    {"init", "inspect", "bootstrap", "promote"}
)


#: Shorter alias used at callsites; identical membership to
#: :data:`REQUIRES_BLOCKED_BUNDLE_CATEGORIES`. The shorter name reads
#: better in ``if requires_blocked_bundle(...):`` expressions and
#: matches the public helper signature.
REQUIRES_BLOCKED_BUNDLE: frozenset[str] = REQUIRES_BLOCKED_BUNDLE_CATEGORIES


def requires_blocked_bundle(category: str) -> bool:
    """Return ``True`` when ``category`` must emit a ``BLOCKED`` bundle.

    Categories outside either pinned set are not part of the
    ADR 0035 contract. To avoid silently expanding the contract,
    the helper treats unknown categories as non-emitting: the
    caller must explicitly add a new category to the matrix first.

    A non-string input is treated as non-emitting rather than
    raising so a defensive caller does not crash on a malformed
    ``run_type``. The helper itself validates ``run_type`` as a
    non-empty string and raises :class:`ValueError` on a bad value.
    """
    if not isinstance(category, str):
        return False
    return category in REQUIRES_BLOCKED_BUNDLE_CATEGORIES


def _normalised_blockers(
    blockers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return ``blockers`` as a list of ``{id, message}`` dicts.

    The shape mirrors the existing ``init --check`` blocker output
    (ADR 0029): every entry has a stable machine ``id`` and a human
    ``message``. Other fields are dropped so a ``BLOCKED`` bundle
    cannot leak transient error data (raw exception text, file
    paths, model output) into the bundle through the matrix.

    Entries missing a non-empty string ``id`` are dropped: a
    blocker without a stable id is not part of the machine
    contract.
    """
    out: list[dict[str, Any]] = []
    for entry in blockers:
        if not isinstance(entry, Mapping):
            continue
        bid = entry.get("id")
        if not isinstance(bid, str) or not bid:
            continue
        normalised: dict[str, Any] = {"id": bid}
        msg = entry.get("message")
        if isinstance(msg, str) and msg:
            # Scrub absolute paths and secrets from blocker messages
            # so a caller that threads an exception traceback or a
            # ``Path`` through the matrix cannot leak raw local
            # paths into the receipt (which the existing receipt
            # builder carries through verbatim — only the summary
            # and trajectory digest builders scrub). The same
            # scrubbed value lands in the summary and digest
            # writers, where the idempotent re-scrub is a no-op.
            normalised["message"] = _scrub_string(msg)
        out.append(normalised)
    return out


def write_blocked_bundle(
    global_store: UserGlobalStorage,
    *,
    run_id: str,
    run_type: str,
    blockers: Sequence[Mapping[str, Any]],
    identities: Mapping[str, Any] | None = None,
) -> Path:
    """Write a minimal ``BLOCKED`` evidence bundle for ``run_id``.

    The bundle is exactly three files (no ``raw/`` subdirectory —
    a ``BLOCKED`` bundle is a "we could not proceed" record, not a
    run record, and the run did not execute). All three files
    carry ``status=BLOCKED``; the receipt and summary both carry
    the same normalised ``blockers`` list so a downstream reader
    can branch on either entrypoint.

    Parameters
    ----------
    global_store:
        The :class:`UserGlobalStorage` writer that owns the
        ``$HOME/.metacrucible/evidence/<run_id>/`` directory.
    run_id:
        Per-run identifier. Validated by
        :meth:`UserGlobalStorage.evidence_bundle_dir` (path-safe
        flat name, no traversal, no absolute prefix).
    run_type:
        The command/stage category. The matrix in this module
        defines which categories should call this helper; the
        helper itself does not enforce the matrix so it remains a
        primitive that future categories can use during rollout.
    blockers:
        Sequence of ``{id, message}`` mappings. Extra keys are
        dropped; entries missing a non-empty ``id`` are dropped.
    identities:
        Optional stable identity fields to thread into the
        receipt (artifact, envelope, ``benchmark_sha``,
        ``executable_benchmark_sha``, ``evaluation_harness``,
        ``runtime_adapter``, ``model_identities``, etc.). These
        pass through the receipt builder verbatim and are
        validated as sibling-relative refs where applicable. A
        ``BLOCKED`` bundle is not required to carry identities —
        a run blocked before identity resolution should still
        write a bundle so reviewers can see the failure.

    Returns
    -------
    The path to the evidence bundle directory.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(
            f"run_id must be a non-empty string; got {run_id!r}"
        )
    if not isinstance(run_type, str) or not run_type:
        raise ValueError(
            f"run_type must be a non-empty string; got {run_type!r}"
        )
    if identities is not None and not isinstance(identities, Mapping):
        raise ValueError(
            f"identities must be a mapping or None; got {type(identities).__name__}"
        )

    normalised_blockers = _normalised_blockers(blockers)

    # Receipt: bundle entrypoint. Carries run id, run type, status,
    # the normalised blockers, and any identity fields the caller
    # provided. The builder stamps schema_version and applies the
    # default sibling refs (summary.json, trajectory-digest.json).
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "run_type": run_type,
        "status": BLOCKED_STATUS,
        "blockers": normalised_blockers,
    }
    if identities:
        for key, value in identities.items():
            # Do not let the caller override the run id, run type,
            # status, or blockers via the identities mapping; those
            # are owned by the helper and the matrix.
            if key in {"run_id", "run_type", "status", "blockers"}:
                continue
            receipt[key] = value

    # Summary: aggregate view. The summary allowlist drops anything
    # not in SUMMARY_ALLOWED_TOP_KEYS, so only the aggregate fields
    # a BLOCKED bundle legitimately carries (status, blockers) are
    # passed in. The builder scrubs absolute paths and secrets
    # out of every string value, so a caller that accidentally
    # embeds a path in a blocker message cannot leak it.
    summary: dict[str, Any] = {
        "status": BLOCKED_STATUS,
        "blockers": normalised_blockers,
    }

    # Trajectory digest: bounded, redacted narrative. For a
    # BLOCKED bundle the only "steps" are the blockers themselves
    # — one step per blocker with action=blocked. No raw events,
    # no transcripts, no full model output (a BLOCKED bundle must
    # not carry evidence the run did not produce).
    trajectory_steps: list[dict[str, Any]] = [
        {
            "step": idx,
            "action": "blocked",
            "status": BLOCKED_STATUS,
            "blocker": blocker,
        }
        for idx, blocker in enumerate(normalised_blockers)
    ]
    trajectory: dict[str, Any] = {
        "status": BLOCKED_STATUS,
        "steps": trajectory_steps,
    }

    # All three writes go through the existing UserGlobalStorage
    # writers. Each writer normalises the payload through the v1
    # builder, so the on-disk artifacts satisfy the evidence-
    # bundle v1 contract (schema_version stamped, default sibling
    # refs, summary allowlist, no absolute paths in machine
    # evidence) by construction.
    global_store.write_receipt(run_id, receipt)
    global_store.write_summary(run_id, summary)
    global_store.write_trajectory_digest(run_id, trajectory)

    return global_store.evidence_bundle_dir(run_id)
