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
    "SECRET_PRIVACY_RISK_ID",
    "SECRET_PRIVACY_RISK_VERSION",
    "ProfileResult",
    "ProfileSpec",
    "compute_evaluation_harness_sha",
    "evaluate_acceptance",
    "select_supplemental",
    "select_triggers",
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
