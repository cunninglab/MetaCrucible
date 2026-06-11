"""Static review profile framework (Issue #21).

Pins the contract from ADR 0033 (static review profile contract) and
the three acceptance criteria from issue #21:

  1. **Profile version / content hash / config hash participate in
     ``evaluation_harness_sha``**. The harness identity shifts when
     any of those inputs change so cached results and receipts
     stay in lockstep with the profile set that produced them.

  2. **Triggered profiles can block acceptance**. Safety/evidence
     profiles (e.g. ``secret-privacy-risk.v1``) are hard-coded;
     they cannot be disabled, and their ``FAIL`` / ``BLOCKED``
     verdicts set ``accepted=False`` on the acceptance verdict.

  3. **Supplemental profiles report non-blocking findings**.
     ``darwin-skill-quality.v1`` and ``runtime-neutrality.v1`` are
     supplemental by default; their findings surface on the
     verdict but do not change ``accepted`` to ``False``.

Issue #22 extends the Darwin profile with a 9-dimension
SkillLens-derived rubric:

  * :data:`DARWIN_DIMENSIONS` — the pinned 9 dimension ids.
  * :class:`ProfileResult.dimension_scores` — per-dimension scores
    carried on the result, machine-readable via ``as_dict()``.
  * :func:`weakest_darwin_dimensions` — deterministic ranking
    helper (ascending score, ties broken by ascending id).

Public surface
--------------

* :data:`RUNTIME_NEUTRALITY_ID` / :data:`RUNTIME_NEUTRALITY_VERSION`
* :data:`ROUTING_SURFACE_SAFETY_ID` / :data:`ROUTING_SURFACE_SAFETY_VERSION`
* :data:`SECRET_PRIVACY_RISK_ID` / :data:`SECRET_PRIVACY_RISK_VERSION`
* :data:`DARWIN_SKILL_QUALITY_ID` / :data:`DARWIN_SKILL_QUALITY_VERSION`
* :data:`DARWIN_DIMENSIONS` — the 9-dimension rubric (Issue #22).
* :data:`BUILTIN_PROFILE_IDS` — the four pinned built-in profile ids.
* :data:`BUILTIN_PROFILES` — a tuple of :class:`ProfileSpec` for every
  built-in profile (id, version, blocking, built_in, content_hash).
* :class:`ProfileSpec` — versioned identity for a profile.
* :class:`ProfileResult` — top-level ``PASS`` / ``FAIL`` / ``BLOCKED``
  result plus per-rule blockers, supplemental findings, and
  per-dimension scores (Issue #22).
* :func:`select_triggers` — which profiles MUST run for a given
  artifact surface (e.g. ``routing_touched`` flips the routing-safety
  trigger).
* :func:`select_supplemental` — which profiles run by default as
  supplemental review layers.
* :func:`compute_evaluation_harness_sha` — produce a hex digest over
  every profile id, version, content hash, config hash, and
  disabled-state of every configurable profile.
* :func:`evaluate_acceptance` — aggregate per-profile results into a
  blocking verdict and a supplemental-findings list.
* :func:`weakest_darwin_dimensions` — deterministic ranking of
  the n lowest-scoring Darwin dimensions (Issue #22).

Harness identity
----------------

The ``evaluation_harness_sha`` is a content-addressed hex digest
(``SHA-256``) over a canonical JSON encoding of the harness
identity. The identity includes:

  - Every built-in profile's id, version, and content hash.
  - Every custom profile's id, version, content hash, and config hash.
  - The disabled-state of every configurable profile.

Hard-coded safety profiles (``secret-privacy-risk`` and
``routing-surface-safety``) cannot be disabled: a caller that
tries to put one in ``disabled_profiles`` gets a hard
``ValueError`` instead of a silently-downgraded harness identity.

The digest is order-independent: callers may pass ``profile_specs``
in any order, the function sorts internally before hashing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


__all__ = [
    "BUILTIN_PROFILE_IDS",
    "BUILTIN_PROFILES",
    "DARWIN_DIMENSIONS",
    "DARWIN_SKILL_QUALITY_ID",
    "DARWIN_SKILL_QUALITY_VERSION",
    "ROUTING_SURFACE_SAFETY_ID",
    "ROUTING_SURFACE_SAFETY_VERSION",
    "RUNTIME_NEUTRALITY_ID",
    "RUNTIME_NEUTRALITY_VERSION",
    "RUNTIME_PORTABILITY_TARGETS",
    "SECRET_PRIVACY_RISK_ID",
    "SECRET_PRIVACY_RISK_VERSION",
    "ProfileResult",
    "ProfileSpec",
    "ROUTING_SURFACE_CAP",
    "compute_evaluation_harness_sha",
    "evaluate_acceptance",
    "evaluate_routing_surface_safety",
    "evaluate_runtime_neutrality",
    "evaluate_secret_privacy_risk",
    "select_supplemental",
    "select_triggers",
    "evaluate_darwin_skill_quality",
    "weakest_darwin_dimensions",
]


# --------------------------------------------------------------------------- #
# Profile identity (ADR 0033)                                                #
# --------------------------------------------------------------------------- #
#
# Pinned built-in profile ids and versions. Renaming any of these is
# a breaking change because the id participates in the
# evaluation_harness_sha digest and shows up in receipts and
# evidence bundles.

RUNTIME_NEUTRALITY_ID: str = "runtime-neutrality"
RUNTIME_NEUTRALITY_VERSION: str = "v1"

ROUTING_SURFACE_SAFETY_ID: str = "routing-surface-safety"
ROUTING_SURFACE_SAFETY_VERSION: str = "v1"

SECRET_PRIVACY_RISK_ID: str = "secret-privacy-risk"
SECRET_PRIVACY_RISK_VERSION: str = "v1"

DARWIN_SKILL_QUALITY_ID: str = "darwin-skill-quality"
DARWIN_SKILL_QUALITY_VERSION: str = "v1"

#: Pinned tuple of built-in profile ids (ADR 0033). Iteration order
#: is the canonical order so the harness identity digest is stable
#: across processes and Python versions.
BUILTIN_PROFILE_IDS: tuple[str, ...] = (
    RUNTIME_NEUTRALITY_ID,
    ROUTING_SURFACE_SAFETY_ID,
    SECRET_PRIVACY_RISK_ID,
    DARWIN_SKILL_QUALITY_ID,
)

#: Built-in profile ids that are hard-coded safety / evidence
#: profiles. These profiles cannot be disabled through configuration
#: (ADR 0033: ``hard-coded safety profiles cannot be disabled'').
HARDCODED_SAFETY_PROFILE_IDS: frozenset[str] = frozenset(
    {SECRET_PRIVACY_RISK_ID, ROUTING_SURFACE_SAFETY_ID}
)


# --------------------------------------------------------------------------- #
# Darwin 9-dimension rubric (Issue #22)                                        #
# --------------------------------------------------------------------------- #
#
# The Darwin skill-quality profile is a SkillLens-derived 9-dimension
# rubric. The dimension ids are the machine contract: they show up
# in ``ProfileResult.dimension_scores`` and in the deterministic
# ``weakest_darwin_dimensions`` ranking. Adding, removing, or
# renaming a dimension is a rubric change; a content-hash bump
# will follow (see :data:`_DARWIN_SKILL_QUALITY_RULES`) and every
# cached result computed under the old rubric will be invalidated
# by the harness identity digest.
#
# The dimension set is intentionally a ``tuple`` so iteration order
# is byte-stable and downstream reports can render the breakdown
# in the canonical order without an extra sort.

DARWIN_DIMENSIONS: tuple[str, ...] = (
    "trigger_clarity",
    "input_contract",
    "output_contract",
    "invariants",
    "failure_modes",
    "examples",
    "scope_boundaries",
    "runtime_neutrality",
    "evaluability",
)


# --------------------------------------------------------------------------- #
# ProfileSpec — versioned identity for a profile                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProfileSpec:
    """Versioned identity for a single static review profile.

    The fields are deliberately minimal: enough to make the profile
    a content-addressed, versioned identity that participates in
    :func:`compute_evaluation_harness_sha`. A profile's *content*
    (the actual rule set) is hashed to ``content_hash`` by the
    profile author; this dataclass only stores the digest, not the
    rule bodies.

    Attributes
    ----------
    id:
        The profile's stable id, e.g. ``"secret-privacy-risk"``.
        Pinned for built-in profiles by ADR 0033.
    version:
        The profile's semver-ish version, e.g. ``"v1"``. A version
        bump is a contract change: the harness identity digest
        shifts and cached results become invalid for the new
        version.
    blocking:
        ``True`` for hard-coded safety/evidence profiles whose
        ``FAIL`` / ``BLOCKED`` verdicts can block acceptance.
        ``False`` for supplemental review layers that only report
        findings. ADR 0033: ``darwin-skill-quality.v1`` is
        ``blocking=False`` by default; a project policy that
        promotes it to ``blocking=True`` (hashed into the harness
        identity via the config hash) turns a Darwin FAIL into an
        acceptance block.
    built_in:
        ``True`` for profiles shipped with MetaCrucible; ``False``
        for custom user-defined profiles. Built-in safety profiles
        cannot be disabled through configuration; the check lives
        in :func:`compute_evaluation_harness_sha`.
    content_hash:
        A content-addressed hex digest of the profile's rule set.
        For built-in profiles this is computed at module load from
        the profile's static rule definitions; for custom profiles
        it is supplied by the profile author. A change to the rule
        set must shift this hash, which in turn shifts the
        harness identity digest.
    """

    id: str
    version: str
    blocking: bool
    built_in: bool
    content_hash: str

    def __post_init__(self) -> None:
        # Independent-review hardening: id and version must be
        # non-empty strings. The check is intentionally minimal
        # — the framework is content-agnostic and only validates
        # the *shape* of the identity, not the content of the
        # rule set. A profile author is expected to provide a
        # well-formed ``content_hash`` themselves; we only verify
        # it is a string so a None / bytes does not silently
        # stringify into a hash and look like a real digest.
        if not isinstance(self.id, str) or not self.id:
            raise ValueError(
                f"ProfileSpec.id must be a non-empty str; got {self.id!r}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise ValueError(
                f"ProfileSpec.version must be a non-empty str; got "
                f"{self.version!r}"
            )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise ValueError(
                f"ProfileSpec.content_hash must be a non-empty str; "
                f"got {self.content_hash!r}"
            )


# --------------------------------------------------------------------------- #
# BUILTIN_PROFILES — the registry of built-in profile specs                   #
# --------------------------------------------------------------------------- #
#
# The content hash for each built-in profile is computed at module
# load from a stable, human-readable rule summary. The rule summary
# is the canonical "what does this profile check?" string; any
# change to the summary shifts the content hash and therefore the
# harness identity digest. The summaries are kept short on purpose
# — they are machine-stable identifiers, not prose documentation.
# Detailed documentation lives in ADR 0033.

_RUNTIME_NEUTRALITY_RULES: str = (
    "checks language claims against portability.target "
    "(claude_code|oh_my_pi|shared_claude_layout|runtime_neutral); "
    "non-blocking by default"
)
_ROUTING_SURFACE_SAFETY_RULES: str = (
    "verifies routing surface is immutable, disjoint from execution "
    "params, and free of system-prompt leakage; blocking when routing "
    "is touched"
)
_SECRET_PRIVACY_RISK_RULES: str = (
    "applies built-in high-confidence secret/privacy pattern library; "
    "redacts or removes secret-like content; runs for every artifact; "
    "hard-coded; cannot be disabled"
)
_DARWIN_SKILL_QUALITY_RULES: str = (
    "Darwin 9-dimension SkillLens-derived rubric "
    "(trigger_clarity, input_contract, output_contract, invariants, "
    "failure_modes, examples, scope_boundaries, runtime_neutrality, "
    "evaluability); non-blocking by default; supplemental review layer"
)


def _content_hash(rules: str) -> str:
    """Return the SHA-256 hex digest of ``rules`` UTF-8 bytes.

    The function is intentionally minimal: a built-in profile's
    content hash is the hex digest of its canonical rule
    summary. The summary is plain text so a reviewer can audit
    the digest without running the code.
    """
    return hashlib.sha256(rules.encode("utf-8")).hexdigest()


BUILTIN_PROFILES: tuple[ProfileSpec, ...] = (
    ProfileSpec(
        id=RUNTIME_NEUTRALITY_ID,
        version=RUNTIME_NEUTRALITY_VERSION,
        blocking=False,
        built_in=True,
        content_hash=_content_hash(_RUNTIME_NEUTRALITY_RULES),
    ),
    ProfileSpec(
        id=ROUTING_SURFACE_SAFETY_ID,
        version=ROUTING_SURFACE_SAFETY_VERSION,
        blocking=True,
        built_in=True,
        content_hash=_content_hash(_ROUTING_SURFACE_SAFETY_RULES),
    ),
    ProfileSpec(
        id=SECRET_PRIVACY_RISK_ID,
        version=SECRET_PRIVACY_RISK_VERSION,
        blocking=True,
        built_in=True,
        content_hash=_content_hash(_SECRET_PRIVACY_RISK_RULES),
    ),
    ProfileSpec(
        id=DARWIN_SKILL_QUALITY_ID,
        version=DARWIN_SKILL_QUALITY_VERSION,
        blocking=False,
        built_in=True,
        content_hash=_content_hash(_DARWIN_SKILL_QUALITY_RULES),
    ),
)


def _builtin_spec_index() -> dict[str, ProfileSpec]:
    """Return a dict mapping built-in id to spec (canonical order)."""
    return {spec.id: spec for spec in BUILTIN_PROFILES}


# --------------------------------------------------------------------------- #
# ProfileResult — top-level PASS / FAIL / BLOCKED verdict                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProfileResult:
    """Top-level result for a single profile run.

    A profile produces exactly one :class:`ProfileResult` per
    artifact. The top-level ``status`` is one of ``"PASS"``,
    ``"FAIL"``, or ``"BLOCKED"``; ``blockers`` is the list of
    hard failures (a hard rule failure flips the top-level status
    to ``FAIL``; an unresolved required ambiguity flips it to
    ``BLOCKED``); ``findings`` is the list of supplemental
    findings (advisories, borderline scores, weak-evidence notes)
    that do not block acceptance by themselves; ``dimension_scores``
    is the per-dimension breakdown used by rubric profiles such as
    ``darwin-skill-quality.v1`` (Issue #22).

    Attributes
    ----------
    profile_id:
        The id of the profile that produced the result.
    version:
        The profile's version. A version mismatch between the
        spec and the result is the reviewer's signal that the
        result is stale.
    status:
        Top-level verdict: ``"PASS"``, ``"FAIL"``, or
        ``"BLOCKED"``. ADR 0033 names these three values
        explicitly.
    blockers:
        List of hard failures. Each entry is a mapping with at
        least ``id`` (str) and ``message`` (str). For
        triggered blocking profiles, any non-empty blockers list
        flips the acceptance verdict to ``accepted=False``.
    findings:
        List of supplemental findings. Each entry is a mapping
        with at least ``id`` (str) and ``message`` (str).
        Findings surface on the acceptance verdict regardless of
        status and never block acceptance.
    dimension_scores:
        Optional per-dimension score sequence (Issue #22). Each
        entry is a mapping with at least ``id`` (str) and
        ``score`` (numeric). Rubric profiles such as Darwin
        populate this field; non-rubric profiles leave it
        empty. The field is preserved verbatim by
        :meth:`as_dict` so receipts can carry the breakdown.
    """

    profile_id: str
    version: str
    status: str
    blockers: tuple[Mapping[str, str], ...] = ()
    findings: tuple[Mapping[str, str], ...] = ()
    dimension_scores: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _PROFILE_STATUSES:
            raise ValueError(
                f"ProfileResult.status must be one of "
                f"{sorted(_PROFILE_STATUSES)!r}; got {self.status!r}"
            )
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise ValueError(
                f"ProfileResult.profile_id must be a non-empty str; "
                f"got {self.profile_id!r}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise ValueError(
                f"ProfileResult.version must be a non-empty str; "
                f"got {self.version!r}"
            )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict form suitable for receipts."""
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "status": self.status,
            "blockers": [dict(b) for b in self.blockers],
            "findings": [dict(f) for f in self.findings],
            "dimension_scores": [dict(s) for s in self.dimension_scores],
        }


#: Canonical set of accepted top-level profile statuses. Renaming
#: or adding a status is a contract change because the acceptance
#: aggregator branches on these values.
_PROFILE_STATUSES: frozenset[str] = frozenset({"PASS", "FAIL", "BLOCKED"})


# --------------------------------------------------------------------------- #
# select_triggers / select_supplemental                                        #
# --------------------------------------------------------------------------- #


def select_triggers(
    *,
    routing_touched: bool,
) -> tuple[ProfileSpec, ...]:
    """Return the tuple of profiles that MUST run for a given surface.

    ADR 0033:

      * ``secret-privacy-risk.v1`` runs for every run (hard-coded).
      * ``routing-surface-safety.v1`` runs when routing is touched.
      * Held-out leakage prevention is not a profile — it is a
        hard optimizer/evaluation rule.

    The returned tuple is in canonical order (the order of
    :data:`BUILTIN_PROFILES`) so the harness identity digest is
    deterministic regardless of how callers branch on the
    triggers.
    """
    triggered_ids: set[str] = {SECRET_PRIVACY_RISK_ID}
    if routing_touched:
        triggered_ids.add(ROUTING_SURFACE_SAFETY_ID)
    spec_index = _builtin_spec_index()
    return tuple(
        spec_index[pid]
        for pid in BUILTIN_PROFILE_IDS
        if pid in triggered_ids
    )


def select_supplemental() -> tuple[ProfileSpec, ...]:
    """Return the tuple of profiles that run by default as supplemental layers.

    ADR 0033: ``darwin-skill-quality.v1 runs by default for review''
    and ``runtime-neutrality.v1`` is a portable review layer. Both
    are non-blocking by default; a future policy threshold can
    promote them to blocking without code changes (the threshold
    is part of the harness identity, hashed in by
    :func:`compute_evaluation_harness_sha`).
    """
    spec_index = _builtin_spec_index()
    return (
        spec_index[DARWIN_SKILL_QUALITY_ID],
        spec_index[RUNTIME_NEUTRALITY_ID],
    )


# --------------------------------------------------------------------------- #
# compute_evaluation_harness_sha                                              #
# --------------------------------------------------------------------------- #


def compute_evaluation_harness_sha(
    profile_specs: Sequence[ProfileSpec],
    *,
    disabled_profiles: Iterable[str] = (),
    config_hash: str = "",
) -> str:
    """Return the evaluation_harness_sha hex digest for ``profile_specs``.

    The digest is the SHA-256 of the canonical JSON encoding of
    the harness identity. The identity includes:

      * Every profile's id, version, and content_hash (sorted by
        id for determinism).
      * The configuration hash (e.g. policy thresholds).
      * The disabled-state of every profile, sorted.

    Hard-coded safety profiles cannot be disabled: a caller that
    tries to disable ``secret-privacy-risk`` or
    ``routing-surface-safety`` gets a hard ``ValueError`` rather
    than a silently-downgraded harness identity (ADR 0033).

    The digest is order-independent: two callers passing the same
    set of specs in different orders get the same hash.
    """
    disabled_set = frozenset(disabled_profiles)
    for forbidden in HARDCODED_SAFETY_PROFILE_IDS:
        if forbidden in disabled_set:
            raise ValueError(
                f"hard-coded safety profile {forbidden!r} cannot be "
                f"disabled; ADR 0033 forbids disabling hard-coded "
                f"safety profiles"
            )

    # Sort specs by id so the digest is independent of the caller's
    # iteration order. ADR 0033 names the identities, not the
    # order.
    sorted_specs = sorted(profile_specs, key=lambda spec: spec.id)

    # Build a stable dict shape for hashing. The shape is part of
    # the contract: a future change to the field set is a contract
    # change because every cached result computed under the old
    # shape would still hit the cache under the new shape, which
    # is exactly the failure mode the harness identity is meant to
    # prevent.
    identity: dict[str, Any] = {
        "schema": "metacrucible.evaluation_harness_sha.v1",
        "config_hash": config_hash,
        "profiles": [
            {
                "id": spec.id,
                "version": spec.version,
                "content_hash": spec.content_hash,
                "blocking": spec.blocking,
                "built_in": spec.built_in,
            }
            for spec in sorted_specs
        ],
        "disabled_profiles": sorted(disabled_set),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------- #
# evaluate_acceptance                                                         #
# --------------------------------------------------------------------------- #


def evaluate_acceptance(
    profile_results: Sequence[ProfileResult],
    *,
    profile_specs: Mapping[str, ProfileSpec],
) -> dict[str, Any]:
    """Aggregate per-profile results into an acceptance verdict.

    The verdict is a flat dict with three fields:

      * ``accepted`` (``bool``) — ``True`` iff every triggered
        blocking profile returned ``PASS`` (no ``FAIL`` /
        ``BLOCKED`` verdict).
      * ``blockers`` (``list[dict]``) — combined blockers from
        every blocking profile that produced a non-``PASS`` top-
        level status. Each entry is the profile's blocker dict.
      * ``supplemental_findings`` (``list[dict]``) — combined
        findings from every supplemental profile, regardless of
        pass/fail. Findings are reported but never block
        acceptance.

    A triggered blocking profile is identified by
    ``profile_specs[result.profile_id].blocking is True``. A
    non-blocking (``blocking=False``) profile's status is ignored
    for the ``accepted`` flag; only its ``findings`` surface on
    the verdict.

    Profiles whose id is missing from ``profile_specs`` are
    treated as non-blocking (defensive: a profile spec can be
    omitted without silently downgrading the verdict). Profiles
    whose result status is not one of the canonical
    :data:`_PROFILE_STATUSES` values are also treated as
    non-blocking and surface as a finding (defensive: a buggy
    profile cannot poison acceptance).
    """
    blockers: list[dict[str, str]] = []
    supplemental_findings: list[dict[str, str]] = []
    accepted = True

    for result in profile_results:
        spec = profile_specs.get(result.profile_id)
        is_blocking = bool(spec.blocking) if spec is not None else False

        # Blockers from blocking profiles: any non-PASS status
        # flips accepted to False and surfaces every blocker.
        if is_blocking and result.status != "PASS":
            accepted = False
            for blocker in result.blockers:
                blockers.append(dict(blocker))

        # Findings: every profile (blocking or not) reports its
        # findings as non-blocking supplemental information.
        # Defensive: a profile that emitted a blocker AND a
        # finding should not have its finding silently swallowed,
        # so we collect findings from every result.
        for finding in result.findings:
            supplemental_findings.append(dict(finding))

    return {
        "accepted": accepted,
        "blockers": blockers,
        "supplemental_findings": supplemental_findings,
    }


# --------------------------------------------------------------------------- #
# weakest_darwin_dimensions (Issue #22)                                        #
# --------------------------------------------------------------------------- #


def weakest_darwin_dimensions(
    result: ProfileResult,
    *,
    n: int = 3,
) -> tuple[Mapping[str, Any], ...]:
    """Return the ``n`` lowest-scoring Darwin dimensions, deterministically.

    The ranking is **ascending score** (worst first); ties are
    broken by **ascending dimension id** so the report is
    byte-stable across processes and Python versions. The
    default ``n=3`` matches the common "show me the three worst
    dimensions" report shape; callers can request any non-
    negative ``n``.

    Parameters
    ----------
    result:
        A :class:`ProfileResult` produced by the Darwin profile.
        Any other profile id raises :class:`ValueError` so a
        runtime bug cannot silently rank the wrong dimension
        set.
    n:
        Number of weakest dimensions to return. ``n=0`` returns
        an empty tuple; negative ``n`` raises :class:`ValueError`.

    Returns
    -------
    tuple
        A tuple of per-dimension score mappings (in the same
        shape the caller passed in) ordered from weakest to
        strongest among the returned slice.

    Raises
    ------
    ValueError
        If ``result.profile_id`` is not ``darwin-skill-quality``
        or if ``n`` is negative.
    """
    if result.profile_id != DARWIN_SKILL_QUALITY_ID:
        raise ValueError(
            f"weakest_darwin_dimensions requires a darwin-skill-quality "
            f"result; got profile_id={result.profile_id!r}"
        )
    if not isinstance(n, int) or n < 0:
        raise ValueError(
            f"weakest_darwin_dimensions: n must be a non-negative int; "
            f"got n={n!r}"
        )

    # Sort key is (score, id). ``float(d['score'])`` keeps the
    # contract permissive — callers may pass int or float scores
    # — and ``str(d['id'])`` defends against a non-str id
    # slipping through and breaking the tie-break ordering.
    sorted_scores = sorted(
        result.dimension_scores,
        key=lambda d: (float(d["score"]), str(d["id"])),
    )
    if n == 0:
        return ()
    return tuple(sorted_scores[:n])


# --------------------------------------------------------------------------- #
# Runtime-neutrality portability trigger (Issue #23)                          #
# --------------------------------------------------------------------------- #
#
# Pins the contract from ADR 0033 (``portability.target is a
# portability claim that controls runtime-neutrality checks [...]
# claude_code, oh_my_pi, shared_claude_layout, and runtime_neutral
# claims use progressively different language checks'') and the
# acceptance criteria from issue #23:
#
#   1. **Trigger based on portability.target**. The runtime-neutrality
#      profile is a supplemental review layer whose trigger is the
#      artifact input's ``portability.target`` field. Exactly four
#      values are accepted; any other value (typo, drift, unknown
#      runtime) is treated as a missing/bad trigger rather than a
#      silent pass.
#
#   2. **Invalid or missing portability.target is BLOCKED**. A
#      missing ``portability`` block, a missing ``target`` field,
#      or a ``target`` outside the four-value set returns a real
#      ``ProfileResult`` with ``status='BLOCKED'`` and at least one
#      blocker. The BLOCKED result is the framework's evidence
#      convention for an unresolved rule (ADR 0033: ``unresolved
#      required ambiguity blocks''), so a buggy artifact cannot
#      silently bypass the trigger and the failure flows into the
#      evidence verdict.
#
#   3. **Findings enter evidence via the existing profile evidence
#      conventions**. The runtime-neutrality result is a real
#      ``ProfileResult``; it slots into ``evaluate_acceptance`` and
#      the per-target finding surfaces on the verdict's
#      ``supplemental_findings`` list (the framework's evidence
#      convention), not via an ad hoc return channel.
#
# The four pinned target values are also the contract for
# :data:`RUNTIME_PORTABILITY_TARGETS`. Renaming or removing a value
# is a breaking change for any artifact that names the target
# (e.g. in a Skill routing surface); new values are an ADR-level
# contract change that must add a new target without dropping
# the existing four.

#: Pinned tuple of runtime-portability targets (Issue #23, ADR 0033).
#: Iteration order is the canonical order; the four values are
#: the trigger set for :func:`evaluate_runtime_neutrality` and
#: participate in stable evidence reports.
RUNTIME_PORTABILITY_TARGETS: tuple[str, ...] = (
    "claude_code",
    "oh_my_pi",
    "shared_claude_layout",
    "runtime_neutral",
)


def evaluate_runtime_neutrality(
    artifact: Mapping[str, Any],
) -> ProfileResult:
    """Evaluate the runtime-neutrality profile for ``artifact``.

    The trigger is ``artifact["portability"]["target"]``. The
    function reads the target and returns a real
    :class:`ProfileResult` so the result plugs into
    :func:`evaluate_acceptance` and the findings/blockers flow
    into the evidence verdict.

    Parameters
    ----------
    artifact:
        The artifact-shaped input mapping. The function looks up
        ``artifact["portability"]["target"]`` (the contract field
        per ADR 0033). Non-mapping inputs raise :class:`ValueError`
        so a programmer error cannot silently pass review.

    Returns
    -------
    ProfileResult
        A real :class:`ProfileResult` for the
        ``runtime-neutrality`` profile.

        * If ``portability.target`` is one of
          :data:`RUNTIME_PORTABILITY_TARGETS`, returns
          ``status='PASS'`` with a per-target finding that
          records the observed target so evidence and reports
          round-trip the claim.
        * Otherwise (missing ``portability`` block, missing
          ``target`` field, or an unknown target value),
          returns ``status='BLOCKED'`` with a blocker whose id
          is rooted at ``runtime-neutrality.target`` so the
          missing/bad trigger shows up on the verdict's
          ``blockers`` list.

    Raises
    ------
    ValueError
        If ``artifact`` is not a mapping. A missing trigger
        field is a BLOCKED result, not a programming error;
        a non-mapping ``artifact`` argument is a programming
        error.
    """
    if not isinstance(artifact, Mapping):
        raise ValueError(
            f"evaluate_runtime_neutrality requires a mapping input; "
            f"got {type(artifact).__name__}"
        )

    portability = artifact.get("portability")
    target: Any = None
    if isinstance(portability, Mapping):
        target = portability.get("target")

    if target not in RUNTIME_PORTABILITY_TARGETS:
        # Missing or invalid trigger. Surface as a BLOCKED result
        # with a blocker; the blocker is the evidence the
        # framework reports when the trigger could not be
        # resolved. The blocker id is stable so downstream
        # automation (judges, optimizers, reports) can group on
        # it.
        return ProfileResult(
            profile_id=RUNTIME_NEUTRALITY_ID,
            version=RUNTIME_NEUTRALITY_VERSION,
            status="BLOCKED",
            blockers=(
                {
                    "id": "runtime-neutrality.target",
                    "message": (
                        "portability.target must be one of "
                        f"{list(RUNTIME_PORTABILITY_TARGETS)!r}; "
                        f"got {target!r}"
                    ),
                    "target": target,
                },
            ),
        )

    # Valid trigger: emit a per-target finding that records the
    # observed target. The finding flows through
    # ``evaluate_acceptance`` as a supplemental finding so
    # downstream tools can report which portability claim the
    # profile observed. PASS status with a non-empty finding
    # tuple is the supplemental-review convention (ADR 0033).
    return ProfileResult(
        profile_id=RUNTIME_NEUTRALITY_ID,
        version=RUNTIME_NEUTRALITY_VERSION,
        status="PASS",
        findings=(
            {
                "id": f"runtime-neutrality.target.{target}",
                "message": (
                    f"portability.target={target!r} is in the "
                    f"runtime-neutrality allowed set"
                ),
                "target": target,
            },
        ),
    )



# --------------------------------------------------------------------------- #
# Routing-surface-safety evaluator (Issue #24)                                #
# --------------------------------------------------------------------------- #
#
# Pins the contract from ADR 0027 ("routing edit budget is capped at 1, and
# routing changes require explicit confirmation'') and ADR 0032 ("Routing
# revisions remain capped at one selected edit and require explicit
# confirmation before they can enter a candidate revision'') and the three
# acceptance criteria from issue #24:
#
#   1. **Triggered when routing is touched**. The
#      ``routing-surface-safety`` profile is selected by
#      :func:`select_triggers` when ``routing_touched=True``; the
#      evaluator below adds the *content* check (counting the
#      routing changes in the proposal and verifying the HITL
#      flag) so the profile produces a real
#      :class:`ProfileResult` that the rest of the framework can
#      consume.
#
#   2. **Can block acceptance**. The built-in
#      ``routing-surface-safety.v1`` spec is ``blocking=True``
#      (ADR 0033: hard-coded safety profile). When the evaluator
#      returns ``BLOCKED`` (cap exceeded and/or HITL missing),
#      feeding the result into :func:`evaluate_acceptance` with
#      the built-in spec must flip the verdict to
#      ``accepted=False`` and surface the blocker on the
#      verdict's ``blockers`` list.
#
#   3. **Aligns with routing cap=1 and HITL flow**. The
#      evaluator enforces two domain rules:
#
#      - :data:`ROUTING_SURFACE_CAP` (=1): the proposed
#        revision carries more than one routing change
#        (:data:`routing-surface-safety.cap-exceeded` blocker).
#      - HITL gate: a routing change lacks explicit human
#        confirmation (:data:`routing-surface-safety.hitl-required`
#        blocker).
#
# A clean proposal (zero routing changes, or exactly one
# routing change with ``human_confirmed=True``) yields ``PASS``
# with an optional per-change finding so evidence round-trips
# what the profile observed through
# :func:`evaluate_acceptance`.

#: Maximum number of routing surface edits per round (ADR 0027 /
#: ADR 0032: ``routing edit budget is capped at 1''). The cap
#: is the machine contract; the constant is part of the public
#: surface so callers (downstream tools, judges, optimizers)
#: can branch on the number without re-hardcoding 1.
ROUTING_SURFACE_CAP: int = 1


def evaluate_routing_surface_safety(
    proposal: Mapping[str, Any],
) -> ProfileResult:
    """Evaluate the routing-surface-safety profile for ``proposal``.

    The function reads the proposal's routing changes and
    confirmation flag and returns a real
    :class:`ProfileResult` so the result plugs into
    :func:`evaluate_acceptance` and the findings/blockers flow
    into the evidence verdict.

    Parameters
    ----------
    proposal:
        The proposal-shaped input mapping. The function looks
        up:

          * ``proposal["routing_changes"]`` — a sequence of
            routing surface changes (e.g. edits to ``name`` /
            ``description`` for Skills; ``name`` /
            ``description`` / ``tools`` / ``spawns`` /
            ``output`` for subagents). An empty sequence
            trivially passes the cap check.
          * ``proposal["human_confirmed"]`` — ``bool`` flag
            indicating whether the routing change has explicit
            human confirmation. A non-mapping ``proposal``
            argument is a programming error and raises
            :class:`ValueError`.

    Returns
    -------
    ProfileResult
        A real :class:`ProfileResult` for the
        ``routing-surface-safety`` profile.

        * If the proposal has zero routing changes, returns
          ``status='PASS'`` with no findings (a clean
          no-touch round).
        * If the proposal has exactly one routing change and
          ``human_confirmed=True``, returns ``status='PASS'``
          with a per-change finding so evidence round-trips
          the change.
        * If the proposal has more than one routing change,
          returns ``status='BLOCKED'`` with a
          ``routing-surface-safety.cap-exceeded`` blocker
          (the cap is :data:`ROUTING_SURFACE_CAP` = 1).
        * If the proposal has at least one routing change
          and ``human_confirmed=False``, returns
          ``status='BLOCKED'`` with a
          ``routing-surface-safety.hitl-required`` blocker
          (routing changes always require explicit
          confirmation).
        * Cap and HITL violations may co-occur; both blockers
          are surfaced so a reviewer can fix them
          independently.

    Raises
    ------
    ValueError
        If ``proposal`` is not a mapping. A missing routing
        change list or confirmation flag is a BLOCKED
        condition, not a programming error; a non-mapping
        ``proposal`` argument is a programming error.
    """
    if not isinstance(proposal, Mapping):
        raise ValueError(
            f"evaluate_routing_surface_safety requires a mapping "
            f"input; got {type(proposal).__name__}"
        )

    # Coerce the routing changes to a sequence. Anything that
    # is not a sequence (None, a scalar, a generator) is
    # treated as no-routing-changes; this matches the
    # framework's "be liberal in what you accept" reading of
    # proposal metadata.
    raw_changes = proposal.get("routing_changes", ())
    if not isinstance(raw_changes, (list, tuple, frozenset, set)):
        routing_changes: tuple[Mapping[str, Any], ...] = ()
    else:
        routing_changes = tuple(raw_changes)
    human_confirmed = bool(proposal.get("human_confirmed", False))

    blockers: list[Mapping[str, str]] = []
    findings: list[Mapping[str, Any]] = []

    # Cap check: more than ROUTING_SURFACE_CAP routing
    # changes is a cap violation. The blocker id is stable so
    # downstream automation (judges, optimizers, reports) can
    # group on it.
    if len(routing_changes) > ROUTING_SURFACE_CAP:
        blockers.append(
            {
                "id": "routing-surface-safety.cap-exceeded",
                "message": (
                    f"proposal carries {len(routing_changes)} "
                    f"routing changes; the routing edit budget is "
                    f"capped at {ROUTING_SURFACE_CAP} (ADR 0027 / "
                    "ADR 0032)"
                ),
                "change_count": len(routing_changes),
                "cap": ROUTING_SURFACE_CAP,
            }
        )

    # HITL check: at least one routing change without
    # explicit human confirmation is a HITL violation. The
    # blocker id is stable for downstream grouping.
    if routing_changes and not human_confirmed:
        blockers.append(
            {
                "id": "routing-surface-safety.hitl-required",
                "message": (
                    "routing changes require explicit human "
                    "confirmation before they can enter a "
                    "candidate revision (ADR 0032)"
                ),
                "change_count": len(routing_changes),
                "human_confirmed": False,
            }
        )

    # Findings on PASS: when the proposal carries exactly one
    # routing change and the cap/HITL gates are satisfied, a
    # per-change finding is emitted so evidence round-trips
    # what the profile observed through
    # ``evaluate_acceptance``.
    if (
        not blockers
        and len(routing_changes) == 1
        and human_confirmed
    ):
        change = routing_changes[0]
        field = ""
        if isinstance(change, Mapping):
            raw_field = change.get("field", "")
            if isinstance(raw_field, str):
                field = raw_field
        findings.append(
            {
                "id": "routing-surface-safety.change.recorded",
                "message": (
                    f"routing change field={field!r} is "
                    f"human-confirmed and within the cap=1 budget"
                ),
                "field": field,
                "human_confirmed": True,
            }
        )

    if blockers:
        return ProfileResult(
            profile_id=ROUTING_SURFACE_SAFETY_ID,
            version=ROUTING_SURFACE_SAFETY_VERSION,
            status="BLOCKED",
            blockers=tuple(blockers),
        )

    return ProfileResult(
        profile_id=ROUTING_SURFACE_SAFETY_ID,
        version=ROUTING_SURFACE_SAFETY_VERSION,
        status="PASS",
        findings=tuple(findings),
    )


# --------------------------------------------------------------------------- #
# Secret-privacy-risk evaluator (Issue #25)                                   #
# --------------------------------------------------------------------------- #
#
# Pins the contract from ADR 0033 (``secret-privacy-risk.v1 runs for
# every run (hard-coded) [...] hard-coded safety profile'') and the
# three acceptance criteria from issue #25:
#
#   1. **Runs on every run**. The :func:`select_triggers` helper
#      unconditionally adds :data:`SECRET_PRIVACY_RISK_ID` to the
#      triggered set; the evaluator below plugs into that
#      trigger so the profile produces a real
#      :class:`ProfileResult` for every artifact, regardless of
#      whether routing is touched.
#
#   2. **High-confidence secret risks block acceptance**. The
#      built-in :data:`SECRET_PRIVACY_RISK_ID` spec is
#      ``blocking=True`` (ADR 0033: hard-coded safety profile).
#      When the evaluator returns ``BLOCKED`` (an unreviewed
#      high-confidence match, or a reviewed match with a stale
#      hash binding), feeding the result into
#      :func:`evaluate_acceptance` with the built-in spec must
#      flip the verdict to ``accepted=False`` and surface the
#      ``secret-privacy-risk.*`` blocker on the verdict's
#      ``blockers`` list.
#
#   3. **Fake-secret fixture exception is reviewed and
#      hash-bound**. A reviewer can mark a candidate as a fake
#      fixture by listing it in the artifact's
#      ``reviewed_fake_secrets`` sequence. The exception has two
#      parts that must both be satisfied for the candidate to
#      pass:
#
#      * a *reviewed* marker — the candidate text appears in at
#        least one entry of ``reviewed_fake_secrets``;
#      * a *hash binding* — the entry's ``fixture_sha256`` is a
#        non-empty string that equals the SHA-256 hex digest of
#        the actual fixture text being allowed. The binding
#        pins the reviewer's approval to the fixture content so
#        any drift is detected.
#
#      A candidate listed in ``reviewed_fake_secrets`` but with
#      a stale or non-string ``fixture_sha256`` is treated as a
#      real secret (the reviewer's binding is broken; the
#      candidate is no longer trusted). A candidate not in the
#      list at all is also a real secret. Both cases BLOCK.
#
# The pattern library is intentionally small and high-
# confidence (low false positive rate). Adding a pattern is a
# contract change because the harness identity digest includes
# the profile's content hash; new patterns must be paired with
# a content-hash bump.

#: Built-in high-confidence secret/privacy pattern library (Issue
#: #25). Each entry is a ``(pattern_id, compiled_regex)`` pair;
#: the ``pattern_id`` is the machine-readable identifier that
#: shows up on blockers and findings so downstream automation
#: can group on it. Patterns are intentionally narrow (no
#: whitespace, no ambiguous prefixes) so the false-positive rate
#: stays low. The library is read-only after module load so the
#: harness identity digest is byte-stable.
_SECRET_PRIVACY_RISK_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # Canonical AWS access key id; the 20-character body
    # ``AKIA[0-9A-Z]{16}`` is a well-known high-confidence
    # shape that appears in AWS public docs and SDKs.
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    # GitHub personal access token (classic); 36 characters
    # following the ``ghp_`` prefix.
    (
        "github-personal-access-token",
        re.compile(r"ghp_[A-Za-z0-9]{36}"),
    ),
    # Stripe live secret key; the ``sk_live_`` prefix flags a
    # production (non-test) secret that should never appear in
    # a Skill or subagent source.
    (
        "stripe-live-secret-key",
        re.compile(r"sk_live_[A-Za-z0-9]{24,}"),
    ),
)


def _sha256_hex(text: str) -> str:
    """Return the SHA-256 hex digest of ``text`` UTF-8 bytes.

    The hash binding is a plain hex digest so a reviewer can
    audit the digest without running the code (the binding is
    part of the evidence convention; the reviewer pins the
    digest next to the fixture text in a test corpus).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evaluate_secret_privacy_risk(
    artifact: Mapping[str, Any],
) -> ProfileResult:
    """Evaluate the secret-privacy-risk profile for ``artifact``.

    The function reads the artifact's ``body`` (the text to
    scan) and ``reviewed_fake_secrets`` (the reviewer's
    hash-bound fake-fixture list) and returns a real
    :class:`ProfileResult` so the result plugs into
    :func:`evaluate_acceptance` and the blockers flow into the
    evidence verdict.

    Parameters
    ----------
    artifact:
        The artifact-shaped input mapping. The function looks
        up:

          * ``artifact["body"]`` — the text content to scan
            for high-confidence secret patterns. A non-str
            (None, missing, or wrong type) is treated as the
            empty string; a clean empty body trivially
            passes.
          * ``artifact["reviewed_fake_secrets"]`` — an optional
            sequence of mappings, each carrying a reviewed
            fake-fixture exception. The contract for each
            entry is:

              * ``match`` (str): the exact candidate text
                being allowed. The candidate is only
                considered reviewed if it appears verbatim
                in the body (so a typo or extra whitespace
                falls through to the unreviewed path).
              * ``fixture_sha256`` (str): the SHA-256 hex
                digest of the actual fixture text. The
                binding must equal ``sha256(match)``; a
                stale or non-string binding is treated as
                an invalid review.
              * ``fixture_id`` (str, optional): a reviewer-
                assigned identifier echoed on the blocker
                so a downstream report can link the
                binding failure back to the reviewer.

            An entry that is not a mapping, or is missing
            ``match`` / ``fixture_sha256`` as strings, is
            silently dropped from the reviewed set (a
            degenerate review is the same as no review).

        A non-mapping ``artifact`` argument is a programming
        error and raises :class:`ValueError`.

    Returns
    -------
    ProfileResult
        A real :class:`ProfileResult` for the
        ``secret-privacy-risk`` profile.

        * If the body has zero matches across the pattern
          library, returns ``status='PASS'`` (a clean body
          trivially passes).
        * If every match in the body has a corresponding
          ``reviewed_fake_secrets`` entry whose
          ``fixture_sha256`` equals the SHA-256 of the
          actual match text, returns ``status='PASS'``. The
          reviewed fake-fixture exception is *hash-bound*,
          so a fixture that drifts in content is no longer
          trusted even if it remains listed.
        * Otherwise returns ``status='BLOCKED'`` with one
          blocker per unreviewed or stale-binding match.
          The blocker id is rooted at
          ``secret-privacy-risk.`` so downstream automation
          can group on it. Two sub-ids are emitted:

          * ``secret-privacy-risk.reviewed-hash-mismatch``:
            the candidate is listed in
            ``reviewed_fake_secrets`` but the bound
            ``fixture_sha256`` does not match the actual
            SHA-256 of the candidate text. The reviewer's
            binding is stale (or was bound to the wrong
            fixture).
          * ``secret-privacy-risk.unreviewed-secret``: the
            candidate has no entry in
            ``reviewed_fake_secrets`` at all; this is a
            real secret.

    Raises
    ------
    ValueError
        If ``artifact`` is not a mapping. A missing body or
        an empty reviewed list is a normal PASS / BLOCKED
        condition, not a programming error; a non-mapping
        ``artifact`` argument is a programming error.
    """
    if not isinstance(artifact, Mapping):
        raise ValueError(
            f"evaluate_secret_privacy_risk requires a mapping input; "
            f"got {type(artifact).__name__}"
        )

    # ``body`` is the text to scan. A non-str (None, missing,
    # or a wrong type) is treated as the empty string so the
    # function does not have to special-case missing inputs
    # in tests or in callers that build the artifact
    # incrementally.
    raw_body = artifact.get("body")
    body = raw_body if isinstance(raw_body, str) else ""

    # ``reviewed_fake_secrets`` is the reviewer's hash-bound
    # list. A non-sequence (None, scalar, generator) is
    # treated as no review; this matches the framework's
    # "be liberal in what you accept" reading of
    # review metadata.
    raw_reviewed = artifact.get("reviewed_fake_secrets", ())
    if not isinstance(raw_reviewed, (list, tuple, frozenset, set)):
        reviewed_entries: tuple[Mapping[str, Any], ...] = ()
    else:
        reviewed_entries = tuple(raw_reviewed)

    # Walk the reviewed entries once and split them into two
    # sets:
    #   * ``reviewed_matches``: ``match -> fixture_id`` for
    #     entries whose hash binding is valid (the bound
    #     ``fixture_sha256`` equals ``sha256(match)``). A
    #     match in this set clears the candidate.
    #   * ``listed_matches``: ``match -> fixture_id`` for entries
    #     that name the match in ``match`` even if the hash
    #     binding is invalid. A match in this map but not in
    #     ``reviewed_matches`` triggers a
    #     ``reviewed-hash-mismatch`` blocker; the blocker carries
    #     ``listed_matches[candidate]`` (or ``<unknown>``) as
    #     its ``fixture_id`` so a downstream report can link the
    #     binding failure back to the reviewer.
    #
    # A degenerate entry (not a mapping, missing ``match``
    # or ``fixture_sha256`` as a non-empty string) is
    # silently dropped. Silently dropping a degenerate
    # review is safe: a missing or invalid review is the
    # same as no review, and the candidate then falls
    # through to the unreviewed-secret path (which BLOCKs).
    reviewed_matches: dict[str, str] = {}
    listed_matches: dict[str, str] = {}
    for entry in reviewed_entries:
        if not isinstance(entry, Mapping):
            continue
        match = entry.get("match")
        fixture_sha = entry.get("fixture_sha256")
        fixture_id_raw = entry.get("fixture_id", "")
        fixture_id = (
            fixture_id_raw
            if isinstance(fixture_id_raw, str) and fixture_id_raw
            else "<unknown>"
        )
        if not isinstance(match, str) or not isinstance(fixture_sha, str):
            # A non-string ``fixture_sha256`` is the
            # degenerate-review case the contract calls out:
            # it is not a valid SHA-256 hex digest and must
            # be treated as no review.
            continue
        listed_matches[match] = fixture_id
        if _sha256_hex(match) == fixture_sha:
            reviewed_matches[match] = fixture_id

    blockers: list[Mapping[str, str]] = []

    for pattern_id, pattern in _SECRET_PRIVACY_RISK_PATTERNS:
        for match in pattern.finditer(body):
            candidate = match.group(0)
            if candidate in reviewed_matches:
                # Reviewed and the hash binding is valid;
                # the fake-fixture exception clears the
                # candidate. Continue to the next match.
                continue
            if candidate in listed_matches:
                # The reviewer listed the candidate but the
                # bound ``fixture_sha256`` does not match
                # the SHA-256 of the actual candidate text.
                # The binding is stale (the fixture drifted
                # or the reviewer bound the wrong fixture);
                # the candidate is treated as a real
                # secret. The blocker id includes
                # ``fixture_id`` (when known) so a
                # downstream report can link the binding
                # failure back to the reviewer.
                fixture_id = listed_matches.get(
                    candidate, "<unknown>"
                )
                blockers.append(
                    {
                        "id": "secret-privacy-risk.reviewed-hash-mismatch",
                        "message": (
                            f"high-confidence {pattern_id!r} match "
                            f"{candidate!r} is listed in "
                            f"reviewed_fake_secrets but the bound "
                            f"fixture_sha256 does not match the "
                            f"SHA-256 of the actual fixture text; "
                            f"the reviewer's binding is stale "
                            f"(Issue #25 AC3)."
                        ),
                        "pattern_id": pattern_id,
                        "match": candidate,
                        "fixture_id": fixture_id,
                    }
                )
                continue
            # No reviewed entry at all: the candidate is a
            # real secret.
            blockers.append(
                {
                    "id": "secret-privacy-risk.unreviewed-secret",
                    "message": (
                        f"high-confidence {pattern_id!r} match "
                        f"{candidate!r} has no reviewed_fake_secrets "
                        f"entry; this is a real secret (Issue #25 AC2)."
                    ),
                    "pattern_id": pattern_id,
                    "match": candidate,
                }
            )

    if blockers:
        return ProfileResult(
            profile_id=SECRET_PRIVACY_RISK_ID,
            version=SECRET_PRIVACY_RISK_VERSION,
            status="BLOCKED",
            blockers=tuple(blockers),
        )

    return ProfileResult(
        profile_id=SECRET_PRIVACY_RISK_ID,
        version=SECRET_PRIVACY_RISK_VERSION,
        status="PASS",
    )




def evaluate_darwin_skill_quality(artifact: Mapping[str, Any]) -> ProfileResult:
    """Per-dimension Darwin 9-dimension rubric evaluator (Issue #22).

    Replaces the MVP placeholder that returned ``1.0`` for every
    dimension. The real evaluator scores each of the 9 dimensions
    by deterministic content analysis of the artifact's body and
    frontmatter; scores reflect the actual artifact and differ
    across inputs (an empty body scores 0.0 on every content
    dimension; a richly structured body scores 1.0 on the
    dimensions that are actually documented).

    Per-dimension scoring
    ---------------------

    Eight of the nine dimensions are scored by a shared
    :func:`_score_darwin_content_dimension` helper. The helper
    combines four signals into a value in ``[0.0, 1.0]``:

      1. **Section header presence** (max 0.4) -- the dimension
         has been *considered* in the artifact design.
      2. **Keyword diversity** (max 0.3) -- the dimension's
         vocabulary tokens appear across the body. Each unique
         token contributes ``0.1``; the slot caps at three
         tokens.
      3. **Structural elements** (max 0.2) -- bullets (``- ``)
         and fenced code blocks (```` ``` ````) operationalize
         the dimension. Each element contributes ``0.05``; the
         slot caps at four elements.
      4. **Length depth** (max 0.1) -- a 1000-character body
         earns the full bonus; anything shorter is prorated.

    ``runtime_neutrality`` is scored against the artifact's
    ``portability.target`` claim: ``1.0`` when the target is in
    :data:`RUNTIME_PORTABILITY_TARGETS`, ``0.0`` when the
    target is missing or unknown. This makes the
    runtime-neutrality dimension a real signal of the
    artifact's portability claim, not a body-content proxy.

    Result shape
    ------------

    The returned :class:`ProfileResult` always has
    ``status='PASS'``: Darwin is a supplemental review layer
    (ADR 0033) and a low score is a finding, not a blocker.
    ``dimension_scores`` carries one entry per
    :data:`DARWIN_DIMENSIONS` id in canonical order; each
    entry is a ``{id, score}`` mapping with ``score`` rounded
    to three decimal places.

    A non-mapping ``artifact`` argument is a programming error
    and raises :class:`ValueError`; a missing ``body`` field
    is graded honestly (an empty body scores 0.0 on every
    content dimension) rather than collapsing to the previous
    uniform 1.0.
    """
    if not isinstance(artifact, Mapping):
        raise ValueError(
            f"evaluate_darwin_skill_quality requires a mapping input; "
            f"got {type(artifact).__name__}"
        )

    body_obj = artifact.get("body") if isinstance(artifact, Mapping) else None
    body: str = body_obj if isinstance(body_obj, str) else ""
    body_lower = body.lower()

    portability = artifact.get("portability")
    runtime_target: Any = None
    if isinstance(portability, Mapping):
        runtime_target = portability.get("target")

    dimension_scores: list[Mapping[str, Any]] = []
    for dim in DARWIN_DIMENSIONS:
        if dim == "runtime_neutrality":
            # Portability.target is the contract: the dimension
            # is graded against the claim, not the prose. A
            # body that narrates "runtime-neutral" without
            # declaring the target scores 0.0; a body that
            # declares a target outside the allowed set also
            # scores 0.0; only a body that names an allowed
            # target scores 1.0.
            if runtime_target in RUNTIME_PORTABILITY_TARGETS:
                score: float = 1.0
            else:
                score = 0.0
        else:
            keywords, headers = _DARWIN_DIMENSION_SIGNALS[dim]
            score = _score_darwin_content_dimension(
                body=body,
                body_lower=body_lower,
                header_aliases=headers,
                keyword_tokens=keywords,
            )
        dimension_scores.append(
            {"id": dim, "score": round(score, 3)}
        )

    return ProfileResult(
        profile_id=DARWIN_SKILL_QUALITY_ID,
        version=DARWIN_SKILL_QUALITY_VERSION,
        status="PASS",
        dimension_scores=tuple(dimension_scores),
    )


#: Per-dimension keyword / section-header signals used by
#: :func:`evaluate_darwin_skill_quality` to score the eight
#: content-driven Darwin dimensions. ``runtime_neutrality`` is
#: excluded -- its signal is the ``portability.target`` claim,
#: not body content.
#:
#: Each entry is ``(keyword_tokens, section_header_aliases)``.
#: A section header is a substring of the body in lowercase;
#: the helper uses ``in`` (not anchored) so a header under any
#: level of Markdown heading still counts. Tokens are
#: exact-substring lowercase matches; short tokens like
#: ``"use"`` are avoided so prose-only artifacts do not
#: over-score.
_DARWIN_DIMENSION_SIGNALS: dict[
    str, tuple[tuple[str, ...], tuple[str, ...]]
] = {
    "trigger_clarity": (
        ("when to use", "use when", "triggers", "use this skill"),
        ("## when to use", "## use when", "## triggers", "## when not to use"),
    ),
    "input_contract": (
        ("input:", "inputs:", "parameter", "argument", "arguments:"),
        ("## input", "## inputs", "## parameters", "## arguments"),
    ),
    "output_contract": (
        ("output:", "outputs:", "returns:", "response format", "result format"),
        ("## output", "## outputs", "## returns", "## response format"),
    ),
    "invariants": (
        ("invariant", "invariants", "guarantee", "must not", "never"),
        ("## invariant", "## invariants", "## guarantees"),
    ),
    "failure_modes": (
        ("error", "errors", "failure", "exception", "fail"),
        ("## failure", "## failures", "## failure modes", "## errors"),
    ),
    "examples": (
        ("example", "for example", "e.g.", "sample"),
        ("## example", "## examples", "## usage example", "## sample"),
    ),
    "scope_boundaries": (
        ("scope", "out of scope", "do not use", "limits"),
        ("## scope", "## boundaries", "## limits", "## out of scope"),
    ),
    "evaluability": (
        ("check", "checks:", "evaluate", "verify", "expected"),
        ("## check", "## checks", "## evaluation", "## tests", "## verify"),
    ),
}


def _score_darwin_content_dimension(
    *,
    body: str,
    body_lower: str,
    header_aliases: tuple[str, ...],
    keyword_tokens: tuple[str, ...],
) -> float:
    """Score one content-driven Darwin dimension in ``[0.0, 1.0]``.

    The score is the sum of four bounded sub-scores so any
    single artifact can earn partial credit on a dimension
    even when it lacks a dedicated section, and so an
    artifact can never earn a uniform score across all
    dimensions (the inputs vary). See
    :func:`evaluate_darwin_skill_quality` for the slot
    breakdown.
    """
    if not body:
        return 0.0

    # 1. Section header presence. The slot caps at 0.4; a
    #    match in any of the dimension's aliases earns the
    #    full slot because the alias list is already a
    #    curated per-dimension vocabulary.
    section_score = 0.0
    for alias in header_aliases:
        if alias in body_lower:
            section_score = 0.4
            break

    # 2. Keyword diversity. The slot caps at 0.3 (three
    #    distinct tokens).
    keyword_hits = sum(
        1 for token in keyword_tokens if token in body_lower
    )
    keyword_score = min(0.3, keyword_hits * 0.1)

    # 3. Structural elements. Bullets (``- ``) and code
    #    fences (```` ``` ````) operationalize a dimension.
    #    The slot caps at 0.2 (four elements).
    bullet_count = body.count("\n- ") + body.count("\n  - ")
    fence_count = body.count("```") // 2
    structure_score = min(0.2, (bullet_count + fence_count) * 0.05)

    # 4. Length depth. A 1000-character body earns the
    #    full 0.1 bonus; shorter bodies are prorated.
    length_score = min(0.1, len(body) / 10000.0)

    return section_score + keyword_score + structure_score + length_score
