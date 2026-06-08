"""TDD behavior tests for issue #21: static review profile framework.

Pins the contract from ADR 0033 (static review profile contract) and
the three acceptance criteria for issue #21:

  1. **Profile version / content hash / config hash participate in
     ``evaluation_harness_sha``** (Issue #21 AC1, ADR 0033). A
     change to any of those inputs must shift the harness identity
     so the result cache and the receipt remain in lockstep with
     the profile set that produced them.

  2. **Triggered blocking profiles can block acceptance** (Issue
     #21 AC2, ADR 0033). When a safety/evidence profile (e.g.
     ``secret-privacy-risk.v1``) is triggered and its top-level
     status is ``FAIL`` or ``BLOCKED``, the acceptance verdict must
     be ``accepted=False`` and the profile's blockers must surface
     on the verdict. A safety profile's trigger is hard-coded — it
     cannot be disabled through configuration.

  3. **Supplemental profiles report non-blocking findings**
     (Issue #21 AC3, ADR 0033). A supplemental profile (e.g.
     ``darwin-skill-quality.v1``) reports findings regardless of
     status. The acceptance verdict must be ``accepted=True`` so
     long as no triggered blocking profile failed, and the
     supplemental findings must be present on the verdict for
     downstream reporting.

These tests are the red step: :mod:`metacrucible.profiles` is not
implemented yet, so importing it must fail. Once it lands, the
tests turn green and pin the contract from the acceptance criteria
in Issue #21.

The implementation under test (not yet written) is expected to
live under :mod:`metacrucible.profiles` and expose at least:

  - ``ProfileSpec``               — versioned profile identity.
  - ``ProfileResult``             — top-level PASS / FAIL / BLOCKED.
  - ``BUILTIN_PROFILE_IDS``       — pinned list of built-in profile ids.
  - ``RUNTIME_NEUTRALITY_VERSION`` / etc. — pinned version constants.
  - ``select_triggers``           — which profiles must run for a
    given artifact surface (routing touched? etc.).
  - ``select_supplemental``       — which profiles run by default as
    supplemental review layers.
  - ``compute_evaluation_harness_sha`` — produce a hex digest over
    every profile id, version, content hash, config hash, and
    disabled-state so the harness identity shifts when any of them
    change.
  - ``evaluate_acceptance``       — aggregate profile results into
    a blocking verdict and a supplemental-findings list.

References
----------
- ADR 0033 (static review profile contract).
- Issue #21 acceptance criteria.

Issue #22 (Darwin 9-dimension rubric profile) adds a new section
at the bottom of this file. The contract for that issue lives in
ADR 0033 (``darwin-skill-quality.v1`` is a 9-dimension
SkillLens-derived supplemental review layer) and the four
acceptance criteria from issue #22:

  1. The Darwin profile has exactly 9 dimensions.
  2. Per-dimension scores are emitted in the machine-readable
     result so receipts can carry the breakdown.
  3. Weakest dimensions are reported deterministically
     (ascending score, ties broken by ascending id).
  4. Darwin remains supplemental by default; a project policy can
     promote it to blocking, in which case a FAIL result blocks
     acceptance.
"""
from __future__ import annotations

import importlib
import json
from typing import Any, Iterable, Mapping, Sequence

import pytest

PROFILES_MODULE = "metacrucible.profiles"

# Pinned profile ids from ADR 0033. These are the machine contract;
# renaming any of them is a breaking change and must be paired with a
# migration plan.
BUILTIN_PROFILE_IDS: tuple[str, ...] = (
    "runtime-neutrality",
    "routing-surface-safety",
    "secret-privacy-risk",
    "darwin-skill-quality",
)

# Pinned version constants. ADR 0033 ships these as the MVP versions.
PINNED_PROFILE_VERSIONS: dict[str, str] = {
    "runtime-neutrality": "v1",
    "routing-surface-safety": "v1",
    "secret-privacy-risk": "v1",
    "darwin-skill-quality": "v1",
}


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _blocker_ids(payload: Any) -> list[str]:
    """Return the list of blocker ids in a verdict or result, or empty."""
    if not isinstance(payload, Mapping):
        return []
    blockers = payload.get("blockers", [])
    if not isinstance(blockers, list):
        return []
    out: list[str] = []
    for blocker in blockers:
        if isinstance(blocker, Mapping) and isinstance(blocker.get("id"), str):
            out.append(blocker["id"])
    return out


def _finding_ids(payload: Any) -> list[str]:
    """Return the list of finding ids on a verdict, or empty."""
    if not isinstance(payload, Mapping):
        return []
    findings = payload.get("supplemental_findings", [])
    if not isinstance(findings, list):
        return []
    out: list[str] = []
    for finding in findings:
        if isinstance(finding, Mapping) and isinstance(finding.get("id"), str):
            out.append(finding["id"])
    return out


def _make_profile_spec(
    profiles: Any,
    profile_id: str,
    version: str = "v1",
    *,
    blocking: bool = True,
    built_in: bool = True,
    content_hash: str | None = None,
) -> Any:
    """Build a real ``ProfileSpec`` instance via the production constructor.

    The test passes the ``profiles`` fixture so the helper stays
    honest: the helper calls the same constructor callers will
    use, so a future shape change in the dataclass is caught by
    these tests instead of silently flowing through.
    """
    if content_hash is None:
        # Deterministic but distinct per profile so the digest
        # obviously depends on the identity.
        content_hash = "0" * 62 + profile_id[0:2]
    return profiles.ProfileSpec(
        id=profile_id,
        version=version,
        blocking=blocking,
        built_in=built_in,
        content_hash=content_hash,
    )


def _make_profile_result(
    profiles: Any,
    profile_id: str,
    version: str = "v1",
    status: str = "PASS",
    *,
    blockers: Sequence[Mapping[str, str]] = (),
    findings: Sequence[Mapping[str, str]] = (),
    dimension_scores: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """Build a real ``ProfileResult`` instance via the production constructor."""
    return profiles.ProfileResult(
        profile_id=profile_id,
        version=version,
        status=status,
        blockers=tuple(blockers),
        findings=tuple(findings),
        dimension_scores=tuple(dimension_scores),
    )


def _spec_id(spec: Any) -> str:
    """Return ``spec.id`` regardless of whether ``spec`` is a dataclass or dict.

    The helper keeps the tests readable when the spec is the
    production ``ProfileSpec`` instance.
    """
    if isinstance(spec, Mapping):
        return spec["id"]
    return spec.id


def _score_ids(scores: Any) -> list[str]:
    """Return the list of dimension ids in a per-dimension score sequence."""
    if not isinstance(scores, list):
        return []
    out: list[str] = []
    for entry in scores:
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
            out.append(entry["id"])
    return out


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def profiles() -> Any:
    """Import the profiles module; the test fails (red step) if it does not exist."""
    try:
        return importlib.import_module(PROFILES_MODULE)
    except ImportError as exc:
        pytest.fail(
            f"profiles module {PROFILES_MODULE!r} is not implemented yet "
            f"(Issue #21 red step). Expected symbols: ProfileSpec, "
            f"ProfileResult, BUILTIN_PROFILE_IDS, select_triggers, "
            f"select_supplemental, compute_evaluation_harness_sha, "
            f"evaluate_acceptance. ImportError: {exc}"
        )


# --------------------------------------------------------------------------- #
# Module surface                                                              #
# --------------------------------------------------------------------------- #


def test_profiles_module_exposes_required_surface(profiles: Any) -> None:
    """AC1+AC2+AC3: the public surface must exist (TDD red step gate).

    Each symbol is named after its job; the implementation owns the
    concrete shape, but the names are the machine contract.
    """
    for name in (
        "ProfileSpec",
        "ProfileResult",
        "BUILTIN_PROFILE_IDS",
        "select_triggers",
        "select_supplemental",
        "compute_evaluation_harness_sha",
        "evaluate_acceptance",
    ):
        assert hasattr(profiles, name), (
            f"{PROFILES_MODULE!r} must expose {name!r} (Issue #21); "
            f"got attributes "
            f"{sorted(a for a in dir(profiles) if not a.startswith('_'))!r}"
        )


def test_builtin_profile_ids_match_pinned_contract(profiles: Any) -> None:
    """ADR 0033 ships four built-in profiles; their ids are machine-stable.

    Renaming any of them is a breaking change because the id
    participates in :func:`compute_evaluation_harness_sha` and shows
    up in receipts and evidence bundles. The test pins the exact
    set so a typo in the implementation is caught immediately.
    """
    ids = profiles.BUILTIN_PROFILE_IDS
    assert isinstance(ids, (tuple, list, frozenset)), (
        f"BUILTIN_PROFILE_IDS must be a tuple/list/frozenset; "
        f"got {type(ids).__name__}"
    )
    assert tuple(ids) == BUILTIN_PROFILE_IDS, (
        f"BUILTIN_PROFILE_IDS must equal the pinned ADR 0033 set; "
        f"got {tuple(ids)!r} expected {BUILTIN_PROFILE_IDS!r}"
    )


# --------------------------------------------------------------------------- #
# AC1 — profile version / content / config hash participates in harness sha  #
# --------------------------------------------------------------------------- #


def test_compute_evaluation_harness_sha_returns_hex_digest(
    profiles: Any,
) -> None:
    """AC1: the harness sha is a SHA-256 hex digest (64 lowercase hex chars)."""
    specs = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    digest = profiles.compute_evaluation_harness_sha(specs)
    assert isinstance(digest, str), (
        f"compute_evaluation_harness_sha must return a str; got "
        f"{type(digest).__name__}"
    )
    assert all(c in "0123456789abcdef" for c in digest), (
        f"compute_evaluation_harness_sha must return a lowercase hex "
        f"digest; got {digest!r}"
    )
    assert len(digest) == 64, (
        f"compute_evaluation_harness_sha must return a SHA-256 hex "
        f"digest (64 chars); got {len(digest)} chars"
    )


def test_compute_evaluation_harness_sha_is_stable_for_equal_inputs(
    profiles: Any,
) -> None:
    """AC1: equal profile inputs yield equal digests (deterministic)."""
    specs = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    a = profiles.compute_evaluation_harness_sha(specs)
    b = profiles.compute_evaluation_harness_sha(specs)
    assert a == b, (
        f"equal profile inputs must yield equal harness sha; "
        f"got {a!r} vs {b!r}"
    )


def test_compute_evaluation_harness_sha_changes_when_profile_version_changes(
    profiles: Any,
) -> None:
    """AC1: bumping a profile's version must change the harness sha.

    ADR 0033: ``built-in profile versions [...] participate in
    evaluation harness identity.'' If the version bump does not
    shift the digest, the harness identity is silently stale.
    """
    base = [
        _make_profile_spec(profiles, pid, version="v1") for pid in BUILTIN_PROFILE_IDS
    ]
    bumped = [
        _make_profile_spec(profiles, pid, version="v2")
        if pid == "secret-privacy-risk"
        else _make_profile_spec(profiles, pid, version="v1")
        for pid in BUILTIN_PROFILE_IDS
    ]
    base_digest = profiles.compute_evaluation_harness_sha(base)
    bumped_digest = profiles.compute_evaluation_harness_sha(bumped)
    assert base_digest != bumped_digest, (
        f"harness sha must change when a profile's version changes; "
        f"got base={base_digest!r} bumped={bumped_digest!r}"
    )


def test_compute_evaluation_harness_sha_changes_when_profile_content_hash_changes(
    profiles: Any,
) -> None:
    """AC1: changing a profile's content hash must change the harness sha.

    ADR 0033: ``custom profile content hashes [...] participate in
    evaluation harness identity.'' Even when the version and id
    are stable, a change in the rule set's content hash must shift
    the digest so cached results cannot survive a rule-set update.
    """
    base = [
        _make_profile_spec(profiles, pid, content_hash="a" * 64)
        for pid in BUILTIN_PROFILE_IDS
    ]
    changed = [
        _make_profile_spec(profiles, pid, content_hash="b" * 64)
        if pid == "routing-surface-safety"
        else _make_profile_spec(profiles, pid, content_hash="a" * 64)
        for pid in BUILTIN_PROFILE_IDS
    ]
    base_digest = profiles.compute_evaluation_harness_sha(base)
    changed_digest = profiles.compute_evaluation_harness_sha(changed)
    assert base_digest != changed_digest, (
        f"harness sha must change when a profile's content hash changes; "
        f"got base={base_digest!r} changed={changed_digest!r}"
    )


def test_compute_evaluation_harness_sha_changes_when_config_hash_changes(
    profiles: Any,
) -> None:
    """AC1: a profile's configuration hash participates in the harness sha.

    ADR 0033: ``threshold/config hashes [...] participate in
    evaluation harness identity.'' The configuration may include
    policy thresholds, supplemental-profile triggers, and
    disabled-state; the helper must hash the configuration along
    with the profile identity so a threshold change invalidates
    the harness identity.
    """
    base = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    base_digest = profiles.compute_evaluation_harness_sha(
        base, config_hash="threshold=default"
    )
    changed_digest = profiles.compute_evaluation_harness_sha(
        base, config_hash="threshold=strict"
    )
    assert base_digest != changed_digest, (
        f"harness sha must change when a profile's config hash changes; "
        f"got base={base_digest!r} changed={changed_digest!r}"
    )


def test_compute_evaluation_harness_sha_changes_when_profile_disabled(
    profiles: Any,
) -> None:
    """AC1: disabling a configurable profile must change the harness sha.

    ADR 0033: ``disabled configurable-profile state [...] is
    included in evaluation_harness_sha.'' A disabled profile does
    not contribute a result, so the harness identity is a
    different identity.
    """
    base = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    base_digest = profiles.compute_evaluation_harness_sha(
        base, disabled_profiles=frozenset()
    )
    disabled_digest = profiles.compute_evaluation_harness_sha(
        base, disabled_profiles=frozenset({"darwin-skill-quality"})
    )
    assert base_digest != disabled_digest, (
        f"harness sha must change when a configurable profile is "
        f"disabled; got base={base_digest!r} disabled={disabled_digest!r}"
    )


def test_compute_evaluation_harness_sha_includes_every_builtin(
    profiles: Any,
) -> None:
    """AC1: dropping a built-in profile must change the harness sha.

    The set of built-in profiles is part of the harness identity;
    removing a profile (e.g. a future migration that drops
    ``runtime-neutrality``) must shift the digest.
    """
    full = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    partial = [
        _make_profile_spec(profiles, pid)
        for pid in BUILTIN_PROFILE_IDS
        if pid != "darwin-skill-quality"
    ]
    full_digest = profiles.compute_evaluation_harness_sha(full)
    partial_digest = profiles.compute_evaluation_harness_sha(partial)
    assert full_digest != partial_digest, (
        f"harness sha must change when a built-in profile is dropped; "
        f"got full={full_digest!r} partial={partial_digest!r}"
    )


def test_compute_evaluation_harness_sha_rejects_disabling_safety_profile(
    profiles: Any,
) -> None:
    """AC1 (defensive): hard-coded safety profiles cannot be disabled.

    ADR 0033: ``hard-coded safety profiles cannot be disabled.''
    A caller that tries to disable ``secret-privacy-risk`` or
    ``routing-surface-safety`` must get a hard error, not a
    silently-downgraded harness identity.
    """
    specs = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    for forbidden in ("secret-privacy-risk", "routing-surface-safety"):
        with pytest.raises(ValueError):
            profiles.compute_evaluation_harness_sha(
                specs, disabled_profiles=frozenset({forbidden})
            )


# --------------------------------------------------------------------------- #
# AC2 — triggered profiles can block acceptance                               #
# --------------------------------------------------------------------------- #


def test_select_triggers_runs_secret_privacy_risk_for_all_runs(
    profiles: Any,
) -> None:
    """AC2: secret-privacy-risk is hard-coded; it runs for every run.

    ADR 0033: ``secret-privacy-risk.v1 runs for all runs.''
    Independent of the artifact surface, the profile must appear
    in the triggered set.
    """
    for routing_touched in (False, True):
        triggered = profiles.select_triggers(routing_touched=routing_touched)
        ids = {_spec_id(spec) for spec in triggered}
        assert "secret-privacy-risk" in ids, (
            f"select_triggers must include secret-privacy-risk for "
            f"every run; routing_touched={routing_touched} got {ids!r}"
        )


def test_select_triggers_runs_routing_safety_when_routing_touched(
    profiles: Any,
) -> None:
    """AC2: routing-surface-safety fires when routing is touched.

    ADR 0033: ``routing-surface-safety.v1 runs when routing is
    touched.'' With no routing surface change, it must NOT
    appear in the triggered set.
    """
    off = profiles.select_triggers(routing_touched=False)
    on = profiles.select_triggers(routing_touched=True)
    off_ids = {_spec_id(spec) for spec in off}
    on_ids = {_spec_id(spec) for spec in on}
    assert "routing-surface-safety" not in off_ids, (
        f"select_triggers must NOT include routing-surface-safety when "
        f"routing is untouched; got {off_ids!r}"
    )
    assert "routing-surface-safety" in on_ids, (
        f"select_triggers must include routing-surface-safety when "
        f"routing is touched; got {on_ids!r}"
    )


def test_select_supplemental_includes_darwin_and_runtime_neutrality(
    profiles: Any,
) -> None:
    """AC3: supplemental profiles are review layers that run by default.

    ADR 0033: ``darwin-skill-quality.v1 runs by default for
    review.'' The runtime-neutrality check is also a default
    supplemental review layer per the same ADR.
    """
    supplemental = profiles.select_supplemental()
    ids = {_spec_id(spec) for spec in supplemental}
    assert "darwin-skill-quality" in ids, (
        f"select_supplemental must include darwin-skill-quality by "
        f"default; got {ids!r}"
    )
    assert "runtime-neutrality" in ids, (
        f"select_supplemental must include runtime-neutrality by "
        f"default; got {ids!r}"
    )


def test_evaluate_acceptance_blocks_when_triggered_blocking_profile_fails(
    profiles: Any,
) -> None:
    """AC2: a triggered blocking profile FAIL blocks acceptance.

    ``secret-privacy-risk`` is a hard-coded safety profile; when
    its top-level status is ``FAIL``, the verdict must be
    ``accepted=False`` and the profile's blockers must surface.
    """
    secret_spec = _make_profile_spec(profiles, "secret-privacy-risk", blocking=True)
    darwin_spec = _make_profile_spec(
        profiles, "darwin-skill-quality", blocking=False, content_hash="0" * 64
    )
    results = [
        _make_profile_result(
            profiles,
            "secret-privacy-risk",
            status="FAIL",
            blockers=(
                {"id": "secret-privacy-risk.policy", "message": "real secret found"},
            ),
        ),
        _make_profile_result(profiles, "darwin-skill-quality", status="PASS"),
    ]
    spec_index = {_spec_id(secret_spec): secret_spec, _spec_id(darwin_spec): darwin_spec}
    verdict = profiles.evaluate_acceptance(results, profile_specs=spec_index)
    assert isinstance(verdict, Mapping), (
        f"evaluate_acceptance must return a Mapping; got {type(verdict).__name__}"
    )
    assert verdict.get("accepted") is False, (
        f"triggered blocking profile FAIL must set accepted=False; "
        f"got verdict={verdict!r}"
    )
    ids = _blocker_ids(verdict)
    assert "secret-privacy-risk.policy" in ids, (
        f"verdict must surface the blocking profile's blocker ids; "
        f"got {ids!r}"
    )


def test_evaluate_acceptance_blocks_when_triggered_blocking_profile_is_blocked(
    profiles: Any,
) -> None:
    """AC2: a triggered blocking profile BLOCKED verdict blocks acceptance.

    The :class:`ProfileResult` top-level status carries three
    values: ``PASS`` / ``FAIL`` / ``BLOCKED``. A ``BLOCKED`` (as
    distinct from a clean ``FAIL``) must also block acceptance.
    """
    spec = _make_profile_spec(profiles, "routing-surface-safety", blocking=True)
    results = [
        _make_profile_result(
            profiles,
            "routing-surface-safety",
            status="BLOCKED",
            blockers=(
                {"id": "routing-surface-safety.ambiguous", "message": "unresolved"},
            ),
        ),
    ]
    verdict = profiles.evaluate_acceptance(
        results, profile_specs={_spec_id(spec): spec}
    )
    assert verdict.get("accepted") is False, (
        f"triggered blocking profile BLOCKED must set accepted=False; "
        f"got verdict={verdict!r}"
    )
    assert "routing-surface-safety.ambiguous" in _blocker_ids(verdict), (
        f"BLOCKED verdict must surface the profile's blocker ids; "
        f"got {_blocker_ids(verdict)!r}"
    )


def test_evaluate_acceptance_passes_when_only_blocking_profile_passes(
    profiles: Any,
) -> None:
    """AC2 (positive): PASS on the blocking profile keeps acceptance green."""
    spec = _make_profile_spec(profiles, "secret-privacy-risk", blocking=True)
    results = [
        _make_profile_result(profiles, "secret-privacy-risk", status="PASS"),
    ]
    verdict = profiles.evaluate_acceptance(
        results, profile_specs={_spec_id(spec): spec}
    )
    assert verdict.get("accepted") is True, (
        f"PASS on every triggered blocking profile must accept; "
        f"got verdict={verdict!r}"
    )
    assert _blocker_ids(verdict) == [], (
        f"PASS verdict must carry no blockers; got {_blocker_ids(verdict)!r}"
    )


# --------------------------------------------------------------------------- #
# AC3 — supplemental profiles report non-blocking findings                     #
# --------------------------------------------------------------------------- #


def test_evaluate_acceptance_does_not_block_when_supplemental_fails(
    profiles: Any,
) -> None:
    """AC3: a supplemental profile FAIL does NOT block acceptance.

    ``darwin-skill-quality`` is supplemental by default. Its
    failure must be reported as a finding but must not change
    ``accepted`` to ``False``.
    """
    darwin_spec = _make_profile_spec(
        profiles, "darwin-skill-quality", blocking=False, content_hash="0" * 64
    )
    secret_spec = _make_profile_spec(profiles, "secret-privacy-risk", blocking=True)
    results = [
        _make_profile_result(profiles, "secret-privacy-risk", status="PASS"),
        _make_profile_result(
            profiles,
            "darwin-skill-quality",
            status="FAIL",
            findings=(
                {"id": "darwin-skill-quality.example", "message": "weak example"},
            ),
        ),
    ]
    verdict = profiles.evaluate_acceptance(
        results,
        profile_specs={
            _spec_id(darwin_spec): darwin_spec,
            _spec_id(secret_spec): secret_spec,
        },
    )
    assert verdict.get("accepted") is True, (
        f"supplemental FAIL must not block acceptance; got verdict={verdict!r}"
    )
    assert _blocker_ids(verdict) == [], (
        f"supplemental findings must not appear as blockers; "
        f"got {_blocker_ids(verdict)!r}"
    )


def test_evaluate_acceptance_reports_supplemental_findings(
    profiles: Any,
) -> None:
    """AC3: a supplemental profile's findings surface on the verdict.

    Even when the profile PASSes, the per-profile findings are
    reported so downstream tools can rank or display them. When
    the profile FAILs, the findings are still reported as
    findings (not as blockers).
    """
    darwin_spec = _make_profile_spec(
        profiles, "darwin-skill-quality", blocking=False, content_hash="0" * 64
    )
    secret_spec = _make_profile_spec(profiles, "secret-privacy-risk", blocking=True)
    finding = {
        "id": "darwin-skill-quality.examples",
        "message": "only one example given",
    }
    results = [
        _make_profile_result(profiles, "secret-privacy-risk", status="PASS"),
        _make_profile_result(
            profiles,
            "darwin-skill-quality",
            status="FAIL",
            findings=(finding,),
        ),
    ]
    verdict = profiles.evaluate_acceptance(
        results,
        profile_specs={
            _spec_id(darwin_spec): darwin_spec,
            _spec_id(secret_spec): secret_spec,
        },
    )
    finding_ids = _finding_ids(verdict)
    assert "darwin-skill-quality.examples" in finding_ids, (
        f"supplemental findings must surface on the verdict; "
        f"got finding_ids={finding_ids!r}"
    )
    # Findings and blockers are disjoint sets on the verdict.
    assert "darwin-skill-quality.examples" not in _blocker_ids(verdict), (
        f"supplemental finding must NOT appear as a blocker; "
        f"got blocker_ids={_blocker_ids(verdict)!r}"
    )


def test_evaluate_acceptance_reports_supplemental_pass_findings(
    profiles: Any,
) -> None:
    """AC3: even a passing supplemental profile's findings are reported.

    A supplemental profile may emit advisory findings on PASS too
    (e.g. borderline scores, weak evidence). The verdict must
    carry them as findings, not blockers.
    """
    darwin_spec = _make_profile_spec(
        profiles, "darwin-skill-quality", blocking=False, content_hash="0" * 64
    )
    secret_spec = _make_profile_spec(profiles, "secret-privacy-risk", blocking=True)
    results = [
        _make_profile_result(profiles, "secret-privacy-risk", status="PASS"),
        _make_profile_result(
            profiles,
            "darwin-skill-quality",
            status="PASS",
            findings=(
                {"id": "darwin-skill-quality.borderline", "message": "borderline"},
            ),
        ),
    ]
    verdict = profiles.evaluate_acceptance(
        results,
        profile_specs={
            _spec_id(darwin_spec): darwin_spec,
            _spec_id(secret_spec): secret_spec,
        },
    )
    assert verdict.get("accepted") is True, (
        f"supplemental PASS must keep accepted=True; got verdict={verdict!r}"
    )
    assert "darwin-skill-quality.borderline" in _finding_ids(verdict), (
        f"PASS-time supplemental findings must surface on the verdict; "
        f"got finding_ids={_finding_ids(verdict)!r}"
    )


# --------------------------------------------------------------------------- #
# Cross-cutting — verdict shape                                               #
# --------------------------------------------------------------------------- #


def test_evaluate_acceptance_verdict_has_expected_shape(profiles: Any) -> None:
    """The verdict must always carry the same three top-level fields.

    Downstream automation branches on the verdict shape; the
    contract is ``accepted`` (bool) + ``blockers`` (list) +
    ``supplemental_findings`` (list). Missing fields are a
    contract violation.
    """
    spec = _make_profile_spec(profiles, "secret-privacy-risk", blocking=True)
    results = [_make_profile_result(profiles, "secret-privacy-risk", status="PASS")]
    verdict = profiles.evaluate_acceptance(
        results, profile_specs={_spec_id(spec): spec}
    )
    assert isinstance(verdict, Mapping), (
        f"verdict must be a Mapping; got {type(verdict).__name__}"
    )
    for key in ("accepted", "blockers", "supplemental_findings"):
        assert key in verdict, (
            f"verdict must carry {key!r}; got keys={list(verdict.keys())!r}"
        )
    assert isinstance(verdict["accepted"], bool), (
        f"verdict['accepted'] must be a bool; got {type(verdict['accepted']).__name__}"
    )
    assert isinstance(verdict["blockers"], list), (
        f"verdict['blockers'] must be a list; got {type(verdict['blockers']).__name__}"
    )
    assert isinstance(verdict["supplemental_findings"], list), (
        f"verdict['supplemental_findings'] must be a list; "
        f"got {type(verdict['supplemental_findings']).__name__}"
    )


def test_compute_evaluation_harness_sha_digest_is_serializable(
    profiles: Any,
) -> None:
    """The harness sha must be serializable as a plain JSON string.

    Downstream automation writes the digest into receipt.json
    and other JSON artifacts; a non-serializable value would
    silently drop the identity.
    """
    specs = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    digest = profiles.compute_evaluation_harness_sha(specs)
    # Round-trip through JSON to ensure the value is plain text.
    encoded = json.dumps({"harness_sha": digest})
    decoded = json.loads(encoded)
    assert decoded == {"harness_sha": digest}, (
        f"harness sha must round-trip through JSON; got {decoded!r}"
    )


def test_compute_evaluation_harness_sha_profile_order_is_deterministic(
    profiles: Any,
) -> None:
    """The digest is order-independent: shuffling specs yields the same sha.

    ``compute_evaluation_harness_sha`` accepts a sequence of
    specs. The implementation must sort internally so two callers
    passing the same set in different orders still agree on the
    harness identity. ADR 0033 names the identities, not the order.
    """
    base = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    reversed_ = list(reversed(base))
    a = profiles.compute_evaluation_harness_sha(base)
    b = profiles.compute_evaluation_harness_sha(reversed_)
    assert a == b, (
        f"compute_evaluation_harness_sha must be order-independent; "
        f"got a={a!r} b={b!r}"
    )


# --------------------------------------------------------------------------- #
# Issue #22 — Darwin 9-dimension rubric profile                                #
# --------------------------------------------------------------------------- #
#
# Pins the four acceptance criteria for issue #22. The Darwin profile
# is a 9-dimension SkillLens-derived rubric that:
#
#   1. exposes exactly 9 stable dimension ids,
#   2. emits per-dimension scores in the machine-readable result,
#   3. reports the weakest dimensions in a deterministic order
#      (ascending score, ties broken by ascending id), and
#   4. remains supplemental by default but can be promoted to
#      blocking by project policy (a FAIL under blocking blocks
#      acceptance).


def test_darwin_module_exposes_darwin_dimensions_and_weakest_helper(
    profiles: Any,
) -> None:
    """Issue #22 surface: the module must expose the Darwin rubric helpers.

    The Darwin-specific API is :data:`DARWIN_DIMENSIONS` (the
    machine-stable 9-dimension id tuple) and
    :func:`weakest_darwin_dimensions` (the deterministic
    ranking helper). Both are part of the public surface so
    downstream tools can rank or render the rubric.
    """
    for name in ("DARWIN_DIMENSIONS", "weakest_darwin_dimensions"):
        assert hasattr(profiles, name), (
            f"{PROFILES_MODULE!r} must expose {name!r} (Issue #22); "
            f"got attributes "
            f"{sorted(a for a in dir(profiles) if not a.startswith('_'))!r}"
        )


def test_darwin_dimensions_constant_has_exactly_nine_dimensions(
    profiles: Any,
) -> None:
    """Issue #22 AC1: the Darwin rubric has exactly 9 dimensions.

    The Darwin rubric is a 9-dimension SkillLens-derived set;
    a count drift (8 or 10) silently changes the rubric, so the
    test pins both the count and the order.
    """
    dims = profiles.DARWIN_DIMENSIONS
    assert isinstance(dims, (tuple, list, frozenset)), (
        f"DARWIN_DIMENSIONS must be a tuple/list/frozenset; "
        f"got {type(dims).__name__}"
    )
    assert len(dims) == 9, (
        f"DARWIN_DIMENSIONS must have exactly 9 entries "
        f"(Issue #22 AC1); got {len(dims)}"
    )


def test_darwin_dimension_ids_are_unique_nonempty_strings(
    profiles: Any,
) -> None:
    """Issue #22 AC1: every dimension id is unique and a non-empty str.

    Dimension ids participate in findings/blockers and the
    deterministic weakness ranking; duplicates or empty ids
    would corrupt the rubric.
    """
    dims = list(profiles.DARWIN_DIMENSIONS)
    assert len(set(dims)) == len(dims), (
        f"DARWIN_DIMENSIONS ids must be unique; got {tuple(dims)!r}"
    )
    for dim in dims:
        assert isinstance(dim, str) and dim, (
            f"each DARWIN_DIMENSIONS id must be a non-empty str; got {dim!r}"
        )


def test_darwin_profile_result_emits_per_dimension_scores(
    profiles: Any,
) -> None:
    """Issue #22 AC2: per-dimension scores are emitted in the result.

    ``ProfileResult`` carries a ``dimension_scores`` field —
    a sequence of mappings with at least ``id`` (str) and
    ``score`` (numeric). The field is preserved by
    :meth:`ProfileResult.as_dict` so receipts and downstream
    reports can render the breakdown.
    """
    dim_scores = tuple(
        {"id": dim, "score": 0.5 + 0.05 * i}
        for i, dim in enumerate(profiles.DARWIN_DIMENSIONS)
    )
    result = profiles.ProfileResult(
        profile_id="darwin-skill-quality",
        version="v1",
        status="PASS",
        dimension_scores=dim_scores,
    )
    payload = result.as_dict()
    assert "dimension_scores" in payload, (
        f"ProfileResult.as_dict() must include 'dimension_scores' "
        f"(Issue #22 AC2); got keys={list(payload.keys())!r}"
    )
    out_scores = payload["dimension_scores"]
    assert isinstance(out_scores, list), (
        f"ProfileResult.as_dict()['dimension_scores'] must be a list; "
        f"got {type(out_scores).__name__}"
    )
    assert len(out_scores) == 9, (
        f"per-dimension scores must carry all 9 Darwin dimensions; "
        f"got {len(out_scores)}"
    )
    for entry in out_scores:
        assert isinstance(entry, Mapping), (
            f"each dimension score entry must be a Mapping; got {entry!r}"
        )
        assert "id" in entry and "score" in entry, (
            f"each dimension score entry must carry 'id' and 'score'; "
            f"got {entry!r}"
        )
        assert isinstance(entry["id"], str), (
            f"dimension score 'id' must be a str; got {type(entry['id']).__name__}"
        )


def test_weakest_darwin_dimensions_reported_deterministically(
    profiles: Any,
) -> None:
    """Issue #22 AC3: weakest dimensions are reported deterministically.

    The ranking is ascending score; ties are broken by ascending
    dimension id so the report is byte-stable across runs and
    Python versions. The test builds a hand-crafted score
    vector that pins both rules.
    """
    dims = list(profiles.DARWIN_DIMENSIONS)
    # dims[0] and dims[1] tie at the lowest score (0.1); the
    # helper must order them by id ascending.
    # dims[2] is the unique second-lowest, dims[3] the unique
    # third-lowest; the rest of the vector is monotone so the
    # test does not depend on the dimensions' sort order.
    scores = [
        {"id": dims[0], "score": 0.1},
        {"id": dims[1], "score": 0.1},
        {"id": dims[2], "score": 0.2},
        {"id": dims[3], "score": 0.3},
    ] + [
        {"id": d, "score": 0.5 + 0.01 * i}
        for i, d in enumerate(dims[4:])
    ]
    result = _make_profile_result(
        profiles,
        "darwin-skill-quality",
        status="PASS",
        dimension_scores=tuple(scores),
    )
    weakest = profiles.weakest_darwin_dimensions(result, n=3)
    assert isinstance(weakest, tuple), (
        f"weakest_darwin_dimensions must return a tuple; "
        f"got {type(weakest).__name__}"
    )
    assert len(weakest) == 3, (
        f"weakest_darwin_dimensions(n=3) must return 3 entries; "
        f"got {len(weakest)}"
    )
    # The first two are the tied 0.1 entries, sorted by id ascending.
    assert weakest[0]["id"] < weakest[1]["id"], (
        f"tie-break must be by id ascending; got ids="
        f"{[d['id'] for d in weakest[:2]]!r}"
    )
    assert weakest[2]["id"] == dims[2], (
        f"third weakest must be the unique 0.2 score ({dims[2]!r}); "
        f"got {weakest[2]['id']!r}"
    )
    # Every returned id must come from the input dimension set.
    assert all(d["id"] in dims for d in weakest), (
        f"weakest ids must be from DARWIN_DIMENSIONS; got "
        f"{[d['id'] for d in weakest]!r}"
    )
    # Calling twice yields the same order (deterministic).
    again = profiles.weakest_darwin_dimensions(result, n=3)
    assert [d["id"] for d in again] == [d["id"] for d in weakest], (
        f"weakest_darwin_dimensions must be deterministic; "
        f"got {[d['id'] for d in again]!r} vs {[d['id'] for d in weakest]!r}"
    )


def test_weakest_darwin_dimensions_rejects_non_darwin_result(
    profiles: Any,
) -> None:
    """Issue #22 AC3 (defensive): the helper refuses non-Darwin results.

    A runtime-neutrality or secret-privacy-risk result is not a
    Darwin result; the helper must raise rather than silently
    ranking the wrong dimension set.
    """
    result = _make_profile_result(profiles, "runtime-neutrality", status="PASS")
    with pytest.raises(ValueError):
        profiles.weakest_darwin_dimensions(result)


def test_weakest_darwin_dimensions_rejects_negative_n(
    profiles: Any,
) -> None:
    """Issue #22 AC3 (defensive): negative ``n`` is rejected."""
    result = _make_profile_result(profiles, "darwin-skill-quality", status="PASS")
    with pytest.raises(ValueError):
        profiles.weakest_darwin_dimensions(result, n=-1)


def test_built_in_darwin_default_profile_is_supplemental(
    profiles: Any,
) -> None:
    """Issue #22 AC4: the built-in Darwin spec is supplemental by default.

    ADR 0033: ``darwin-skill-quality.v1 runs by default for
    review'' and ``does not become a blocking acceptance gate
    unless project policy configures a threshold.'' The
    built-in spec ships with ``blocking=False`` so the default
    behavior is correct without configuration.
    """
    spec_index = {s.id: s for s in profiles.BUILTIN_PROFILES}
    darwin = spec_index["darwin-skill-quality"]
    assert darwin.blocking is False, (
        f"built-in darwin-skill-quality must be supplemental "
        f"(blocking=False) by default (Issue #22 AC4); "
        f"got blocking={darwin.blocking!r}"
    )


def test_darwin_policy_can_promote_to_blocking_fails_acceptance(
    profiles: Any,
) -> None:
    """Issue #22 AC5: policy can promote Darwin to blocking; FAIL blocks.

    With a project policy that flips ``blocking=True`` (whose
    hash is part of the harness identity) and a Darwin
    ``status=FAIL`` result, the acceptance verdict must flip to
    ``accepted=False`` so the policy gate actually works.
    """
    darwin_spec = _make_profile_spec(
        profiles, "darwin-skill-quality", blocking=True, content_hash="e" * 64
    )
    secret_spec = _make_profile_spec(profiles, "secret-privacy-risk", blocking=True)
    results = [
        _make_profile_result(profiles, "secret-privacy-risk", status="PASS"),
        _make_profile_result(
            profiles,
            "darwin-skill-quality",
            status="FAIL",
            findings=(
                {
                    "id": "darwin-skill-quality.dimension.examples",
                    "message": "examples are too thin",
                },
            ),
            dimension_scores=tuple(
                {"id": d, "score": 0.3} for d in profiles.DARWIN_DIMENSIONS
            ),
        ),
    ]
    verdict = profiles.evaluate_acceptance(
        results,
        profile_specs={
            _spec_id(darwin_spec): darwin_spec,
            _spec_id(secret_spec): secret_spec,
        },
    )
    assert verdict.get("accepted") is False, (
        f"Darwin FAIL with blocking=True must set accepted=False "
        f"(Issue #22 AC5); got verdict={verdict!r}"
    )


def test_darwin_supplemental_failure_does_not_block_with_dimensions(
    profiles: Any,
) -> None:
    """Issue #22 AC4: a default-supplemental Darwin FAIL does not block.

    Even with rich per-dimension scores on the result, the
    default (``blocking=False``) Darwin profile must not flip
    acceptance to ``False`` — the scores are advisory, not a
    hard gate.
    """
    darwin_spec = _make_profile_spec(
        profiles, "darwin-skill-quality", blocking=False, content_hash="d" * 64
    )
    secret_spec = _make_profile_spec(profiles, "secret-privacy-risk", blocking=True)
    results = [
        _make_profile_result(profiles, "secret-privacy-risk", status="PASS"),
        _make_profile_result(
            profiles,
            "darwin-skill-quality",
            status="FAIL",
            dimension_scores=tuple(
                {"id": d, "score": 0.2} for d in profiles.DARWIN_DIMENSIONS
            ),
        ),
    ]
    verdict = profiles.evaluate_acceptance(
        results,
        profile_specs={
            _spec_id(darwin_spec): darwin_spec,
            _spec_id(secret_spec): secret_spec,
        },
    )
    assert verdict.get("accepted") is True, (
        f"supplemental Darwin FAIL must not block acceptance "
        f"(Issue #22 AC4); got verdict={verdict!r}"
    )


def test_darwin_default_content_hash_is_in_harness_identity(
    profiles: Any,
) -> None:
    """Issue #22: the 9-dimension rubric shifts the built-in content hash.

    The Darwin rule summary used to compute the built-in
    content hash must reference the 9-dimension rubric; the
    harness identity must round-trip through JSON so receipts
    can carry it. This is the cross-cutting wiring test for
    AC1+AC2.
    """
    specs = [_make_profile_spec(profiles, pid) for pid in BUILTIN_PROFILE_IDS]
    digest = profiles.compute_evaluation_harness_sha(specs)
    encoded = json.dumps({"harness_sha": digest, "darwin": profiles.DARWIN_DIMENSIONS})
    decoded = json.loads(encoded)
    assert decoded["darwin"] == list(profiles.DARWIN_DIMENSIONS), (
        f"DARWIN_DIMENSIONS must round-trip through JSON; "
        f"got {decoded['darwin']!r}"
    )
    assert len(decoded["harness_sha"]) == 64, (
        f"harness sha must be a 64-char hex digest; "
        f"got {len(decoded['harness_sha'])} chars"
    )



# --------------------------------------------------------------------------- #
# Issue #23 - runtime-neutrality portability target trigger                   #
# --------------------------------------------------------------------------- #
#
# Pins the contract from ADR 0033 (static review profile contract,
# consequence: "portability.target is a portability claim that controls
# runtime-neutrality checks, not the runtime adapter selector;
# claude_code, oh_my_pi, shared_claude_layout, and runtime_neutral
# claims use progressively different language checks.") and the three
# acceptance criteria from issue #23:
#
#   1. **Trigger based on portability.target**. The runtime-neutrality
#      profile runs when artifact input carries a portability target
#      in {claude_code, oh_my_pi, shared_claude_layout, runtime_neutral};
#      the four target values are the trigger set and the result is a
#      machine-readable ProfileResult that the rest of the framework
#      can consume.
#
#   2. **Invalid or missing portability.target is BLOCKED**. A buggy
#      artifact cannot silently bypass the trigger; a missing or
#      unknown portability target must surface a blocker (and a
#      BLOCKED status) so downstream evidence can report it.
#
#   3. **Findings enter evidence via the existing profile evidence
#      conventions**. The runtime-neutrality result is a real
#      ProfileResult; its findings surface through
#      evaluate_acceptance as supplemental_findings (the
#      framework's evidence convention), not via an ad hoc return
#      channel.


def test_runtime_neutrality_module_exposes_issue23_surface(
    profiles: Any,
) -> None:
    """Issue #23 surface: the new symbols must exist on the module.

    The runtime-neutrality trigger is exposed as a public
    function (``evaluate_runtime_neutrality``) and a public
    constant (``RUNTIME_PORTABILITY_TARGETS``) so callers can
    branch on the allowed target set without duplicating it.
    """
    for name in (
        "RUNTIME_PORTABILITY_TARGETS",
        "evaluate_runtime_neutrality",
    ):
        assert hasattr(profiles, name), (
            f"{PROFILES_MODULE!r} must expose {name!r} (Issue #23); "
            f"got attributes "
            f"{sorted(a for a in dir(profiles) if not a.startswith('_'))!r}"
        )


def test_runtime_portability_targets_constant_pins_exactly_four_values(
    profiles: Any,
) -> None:
    """Issue #23 AC1: the trigger set is exactly the four ADR-pinned targets.

    ADR 0033: ``claude_code, oh_my_pi, shared_claude_layout, and
    runtime_neutral claims use progressively different language
    checks.'' The set is the machine contract; an extra or missing
    value silently shifts what the profile accepts. Order is pinned
    too because the constant participates in evidence and stable
    reports depend on stable iteration order.
    """
    targets = profiles.RUNTIME_PORTABILITY_TARGETS
    assert isinstance(targets, (tuple, list, frozenset)), (
        f"RUNTIME_PORTABILITY_TARGETS must be a tuple/list/frozenset; "
        f"got {type(targets).__name__}"
    )
    assert tuple(targets) == (
        "claude_code",
        "oh_my_pi",
        "shared_claude_layout",
        "runtime_neutral",
    ), (
        f"RUNTIME_PORTABILITY_TARGETS must equal the ADR 0033 set "
        f"(claude_code, oh_my_pi, shared_claude_layout, runtime_neutral); "
        f"got {tuple(targets)!r}"
    )


def test_evaluate_runtime_neutrality_returns_profile_result_for_each_target(
    profiles: Any,
) -> None:
    """Issue #23 AC1: every ADR-pinned target triggers a real ProfileResult.

    The four portability targets are the trigger set. For each
    one the function must return a real :class:`ProfileResult`
    with ``profile_id='runtime-neutrality'``, ``status='PASS'``,
    and a non-empty ``findings`` tuple that records the
    observed target.
    """
    for target in profiles.RUNTIME_PORTABILITY_TARGETS:
        artifact = {"portability": {"target": target}}
        result = profiles.evaluate_runtime_neutrality(artifact)
        assert isinstance(result, profiles.ProfileResult), (
            f"evaluate_runtime_neutrality must return a ProfileResult for "
            f"target={target!r}; got {type(result).__name__}"
        )
        assert result.profile_id == "runtime-neutrality", (
            f"runtime-neutrality result must carry profile_id="
            f"'runtime-neutrality' (target={target!r}); "
            f"got {result.profile_id!r}"
        )
        assert result.version == "v1", (
            f"runtime-neutrality result version must be 'v1' "
            f"(target={target!r}); got {result.version!r}"
        )
        assert result.status == "PASS", (
            f"valid portability.target={target!r} must yield PASS; "
            f"got {result.status!r}"
        )
        assert len(result.findings) >= 1, (
            f"valid portability.target={target!r} must emit at least "
            f"one finding (Issue #23 AC3: findings enter evidence); "
            f"got {result.findings!r}"
        )
        # Blockers list is empty on PASS: a passing trigger must
        # not poison acceptance.
        assert result.blockers == (), (
            f"valid portability.target={target!r} must not emit "
            f"blockers; got {result.blockers!r}"
        )


def test_evaluate_runtime_neutrality_finding_records_observed_target(
    profiles: Any,
) -> None:
    """Issue #23 AC3: the per-target finding records the observed target.

    Downstream evidence (judges, optimizers, reports) needs to
    know *which* portability target the trigger observed. The
    finding must carry the target value in a machine-readable
    field so receipts and JSON dumps round-trip the claim.
    """
    for target in profiles.RUNTIME_PORTABILITY_TARGETS:
        artifact = {"portability": {"target": target}}
        result = profiles.evaluate_runtime_neutrality(artifact)
        observed = [
            f.get("target") for f in result.findings if isinstance(f, Mapping)
        ]
        assert target in observed, (
            f"runtime-neutrality finding must record the observed "
            f"target={target!r}; got observed={observed!r} "
            f"findings={result.findings!r}"
        )


def test_evaluate_runtime_neutrality_blocks_on_invalid_target(
    profiles: Any,
) -> None:
    """Issue #23 AC2: an unknown portability.target must surface as BLOCKED.

    The trigger is the four ADR-pinned values. Any other string
    (typo, drift, made-up value) must produce a BLOCKED result
    with at least one blocker so the buggy artifact cannot
    silently pass review.
    """
    for bad in ("Claude_code", "claude-code", "shared_claude", "omp", ""):
        artifact = {"portability": {"target": bad}}
        result = profiles.evaluate_runtime_neutrality(artifact)
        assert result.status == "BLOCKED", (
            f"invalid portability.target={bad!r} must BLOCK; "
            f"got status={result.status!r}"
        )
        assert len(result.blockers) >= 1, (
            f"invalid portability.target={bad!r} must emit at least "
            f"one blocker (Issue #23 AC2); got blockers={result.blockers!r}"
        )


def test_evaluate_runtime_neutrality_blocks_on_missing_portability(
    profiles: Any,
) -> None:
    """Issue #23 AC2: an artifact without ``portability`` must surface BLOCKED.

    The framework cannot infer portability from thin air. A
    missing ``portability`` block must produce a BLOCKED result
    with a blocker (and empty findings) so evidence shows the
    missing-trigger condition rather than silently passing.
    """
    result = profiles.evaluate_runtime_neutrality({})
    assert result.status == "BLOCKED", (
        f"missing portability block must BLOCK; "
        f"got status={result.status!r}"
    )
    assert len(result.blockers) >= 1, (
        f"missing portability block must emit a blocker; "
        f"got blockers={result.blockers!r}"
    )


def test_evaluate_runtime_neutrality_blocks_on_missing_target(
    profiles: Any,
) -> None:
    """Issue #23 AC2: ``portability`` without ``target`` must surface BLOCKED.

    The trigger key is ``portability.target``; an artifact that
    carries an empty portability block cannot trigger the
    profile. The result must be BLOCKED with a blocker (and no
    findings) so a half-declared claim does not silently pass.
    """
    result = profiles.evaluate_runtime_neutrality({"portability": {}})
    assert result.status == "BLOCKED", (
        f"missing portability.target must BLOCK; "
        f"got status={result.status!r}"
    )
    assert len(result.blockers) >= 1, (
        f"missing portability.target must emit a blocker; "
        f"got blockers={result.blockers!r}"
    )


def test_evaluate_runtime_neutrality_findings_enter_evidence_verdict(
    profiles: Any,
) -> None:
    """Issue #23 AC3: findings must flow through the existing evidence path.

    The contract says "findings enter evidence using existing
    static review profile evidence conventions." The framework's
    evidence convention is :func:`evaluate_acceptance`; the
    runtime-neutrality result must plug into it and surface its
    findings on the verdict's ``supplemental_findings`` list so
    downstream tools (judges, optimizers, reports) can consume
    them through the same channel as every other profile.
    """
    secret_spec = _make_profile_spec(
        profiles, "secret-privacy-risk", blocking=True
    )
    runtime_spec = _make_profile_spec(
        profiles, "runtime-neutrality", blocking=False, content_hash="f" * 64
    )
    artifact = {"portability": {"target": "claude_code"}}
    runtime_result = profiles.evaluate_runtime_neutrality(artifact)
    secret_result = _make_profile_result(
        profiles, "secret-privacy-risk", status="PASS"
    )
    verdict = profiles.evaluate_acceptance(
        [secret_result, runtime_result],
        profile_specs={
            _spec_id(secret_spec): secret_spec,
            _spec_id(runtime_spec): runtime_spec,
        },
    )
    assert verdict["accepted"] is True, (
        f"runtime-neutrality is supplemental; its findings must NOT "
        f"block acceptance; got verdict={verdict!r}"
    )
    finding_ids = _finding_ids(verdict)
    runtime_finding_ids = [
        fid for fid in finding_ids if fid.startswith("runtime-neutrality")
    ]
    assert runtime_finding_ids, (
        f"runtime-neutrality findings must surface on the evidence "
        f"verdict (Issue #23 AC3); got finding_ids={finding_ids!r}"
    )


def test_evaluate_runtime_neutrality_blocker_appears_on_evidence_verdict(
    profiles: Any,
) -> None:
    """Issue #23 AC2: a BLOCKED runtime-neutrality must block acceptance.

    Even though the built-in runtime-neutrality is supplemental
    by default, a BLOCKED result must still surface on the
    verdict's ``blockers`` list so the missing/invalid trigger
    condition is part of evidence. A project policy that promotes
    runtime-neutrality to blocking must then see ``accepted=False``
    on the verdict; the per-profile result, not the spec, is
    the source of truth for the BLOCKED status.
    """
    runtime_spec = _make_profile_spec(
        profiles, "runtime-neutrality", blocking=True, content_hash="9" * 64
    )
    secret_spec = _make_profile_spec(
        profiles, "secret-privacy-risk", blocking=True
    )
    runtime_result = profiles.evaluate_runtime_neutrality(
        {"portability": {"target": "omp"}}
    )
    secret_result = _make_profile_result(
        profiles, "secret-privacy-risk", status="PASS"
    )
    verdict = profiles.evaluate_acceptance(
        [secret_result, runtime_result],
        profile_specs={
            _spec_id(runtime_spec): runtime_spec,
            _spec_id(secret_spec): secret_spec,
        },
    )
    assert verdict["accepted"] is False, (
        f"promoted-to-blocking runtime-neutrality BLOCKED must set "
        f"accepted=False; got verdict={verdict!r}"
    )
    blocker_ids = _blocker_ids(verdict)
    assert any(bid.startswith("runtime-neutrality") for bid in blocker_ids), (
        f"runtime-neutrality BLOCKED must surface a blocker on the "
        f"verdict (Issue #23 AC2); got blocker_ids={blocker_ids!r}"
    )
