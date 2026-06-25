"""Pure-logic unit tests for ``metacrucible.storage`` receipt builders (Issue #44).

This file pins the pure-logic surface used by the Evidence Bundle v1
builders (ADR 0024 / 0030) declared in
:mod:`metacrucible.storage`. The contract tests in
``tests/test_blocked_bundle_policy.py`` exercise these via the
filesystem write path; this file imports the builders directly so
their behavior is pinned in isolation:

  - :func:`compute_benchmark_digest`           — SHA-256 of full payload
  - :func:`compute_executable_benchmark_digest` — SHA-256 of eligible
    reviewed cases after masking volatile keys
  - :func:`_eligible_reviewed_cases`           — partition / filter
    helper shared by the two digest functions
  - :func:`_strip_volatile_for_executable`     — default case mask
  - :func:`build_receipt_payload`              — v1 receipt contract
  - :func:`build_summary_payload`              — v1 summary allowlist
    + scrub
  - :func:`build_trajectory_digest_payload`    — v1 trajectory digest
    truncation + scrub
  - :func:`_redact_trajectory_step`            — per-step scrub helper
  - :func:`_scrub_string`                      — single-string scrub
  - :func:`_scrub_summary_value`               — recursive scrub helper

Fixtures are inline dicts, strings, and obviously-fake API key
placeholders (``"sk-test-FAKE-1234"``) — no live model, network, LLM,
sleep, or subprocess calls. The fake API key is asserted to be
scrubbed by both :func:`_scrub_string` and :func:`_scrub_summary_value`
so a regression in the secret pattern cannot leak it into a shared
bundle. No filesystem dependencies beyond pytest's ``tmp_path`` where
useful (none required for this surface).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from metacrucible.storage import (
    DENY_KEYS,
    RECEIPT_DEFAULT_SUMMARY_REF,
    RECEIPT_DEFAULT_TRAJECTORY_DIGEST_REF,
    RECEIPT_REF_FIELDS,
    RECEIPT_REF_LIST_FIELDS,
    SCHEMA_VERSION,
    SUMMARY_ALLOWED_TOP_KEYS,
    _eligible_reviewed_cases,
    _REDACTED_PATH,
    _REDACTED_SECRET,
    _redact_trajectory_step,
    _scrub_string,
    _scrub_summary_value,
    _strip_volatile_for_executable,
    build_receipt_payload,
    build_summary_payload,
    build_trajectory_digest_payload,
    compute_benchmark_digest,
    compute_executable_benchmark_digest,
)


# --------------------------------------------------------------------------- #
# Shared fixtures                                                             #
# --------------------------------------------------------------------------- #

#: Obviously-fake API key. Matches the ``sk-[A-Za-z0-9_-]{8,}`` arm of
#: the secret regex — long enough (8+ chars after ``sk-``) to trigger
#: the scrubber but obviously not a real credential.
FAKE_API_KEY = "sk-test-FAKE-1234"
#: Second obviously-fake API key, exercising the ``sk-ant-`` arm.
FAKE_ANT_KEY = "sk-ant-FAKE-1234567890"
#: Obviously-fake GitHub PAT, exercising the ``ghp_`` arm.
FAKE_GITHUB_PAT = "ghp_FAKEPATnotreal0000"
#: Obviously-fake bearer token, exercising the ``Bearer\\s+`` arm.
FAKE_BEARER = "Bearer eyJFAKEtokenvalue0000"

#: A string that does NOT match any secret arm and should pass
#: through :func:`_scrub_string` unchanged (apart from any path
#: scrubbing).
SAFE_LABEL = "run-id-2024-01-01"


def _reviewed_case(
    case_id: str,
    *,
    content: str = "ok",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal eligible reviewed case record."""
    record: dict[str, Any] = {
        "case_id": case_id,
        "status": "reviewed",
        "content": content,
    }
    if extra:
        record.update(extra)
    return record


def _generated_case(case_id: str) -> dict[str, Any]:
    return {"case_id": case_id, "status": "generated", "content": "x"}


def _disabled_case(case_id: str) -> dict[str, Any]:
    return {"case_id": case_id, "status": "disabled", "content": "x"}


def _minimal_receipt(**overrides: Any) -> dict[str, Any]:
    """Build a minimal valid v1 receipt input payload."""
    base: dict[str, Any] = {
        "run_id": "run-2024-01-01-001",
        "run_type": "evaluate",
        "status": "ok",
        "artifact": {"name": "demo", "version": "0.1.0"},
        "envelope": {"kind": "cli", "command": "metacrucible evaluate"},
        "benchmark_sha": "a" * 64,
        "executable_benchmark_sha": "b" * 64,
        "evaluation_harness": {"name": "eval", "version": "0.1.0"},
        "optimizer_harness": {"name": "opt", "version": "0.1.0"},
        "runtime_adapter": {"name": "adapter", "version": "0.1.0"},
        "model_identities": [{"provider": "fake", "model": "fake-1"}],
        "execution_boundary_id": "demo.check.v1",
        "execution_boundary_object": {"id": "demo.check.v1"},
        "case_result_refs": ["case-001.json", "case-002.json"],
        "event_log_refs": ["events.jsonl"],
        "blockers": [],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# compute_benchmark_digest                                                    #
# --------------------------------------------------------------------------- #


class TestComputeBenchmarkDigest:
    """Pin the full-payload benchmark digest (ADR 0024)."""

    def test_returns_sha256_hex_64_chars(self) -> None:
        payload = {"cases": [_reviewed_case("c1"), _generated_case("c2")]}
        digest = compute_benchmark_digest(payload)
        assert isinstance(digest, str)
        assert len(digest) == 64
        # SHA-256 hex characters only.
        assert all(c in "0123456789abcdef" for c in digest)

    def test_matches_manual_canonical_json(self) -> None:
        payload = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        expected = hashlib.sha256(encoded).hexdigest()
        assert compute_benchmark_digest(payload) == expected

    def test_key_order_does_not_affect_digest(self) -> None:
        left = {"a": 1, "b": 2, "c": [3, 2, 1]}
        right = {"c": [3, 2, 1], "b": 2, "a": 1}
        assert compute_benchmark_digest(left) == compute_benchmark_digest(right)

    def test_value_change_moves_digest(self) -> None:
        base = _reviewed_case("c1", content="ok")
        changed = _reviewed_case("c1", content="different")
        assert compute_benchmark_digest(base) != compute_benchmark_digest(changed)

    def test_generated_and_disabled_cases_move_digest(self) -> None:
        """The full hash covers *all* records (ADR 0024)."""
        base = [_reviewed_case("c1")]
        with_generated = base + [_generated_case("c2")]
        with_disabled = base + [_disabled_case("c2")]
        base_hash = compute_benchmark_digest(base)
        assert compute_benchmark_digest(with_generated) != base_hash
        assert compute_benchmark_digest(with_disabled) != base_hash

    def test_non_serializable_value_stringified(self) -> None:
        """``default=str`` lets non-JSON values through."""
        payload = {"a": 1, "set_value": {1, 2, 3}}
        # Should not raise — the set is stringified by ``default=str``.
        digest = compute_benchmark_digest(payload)
        assert isinstance(digest, str) and len(digest) == 64

    def test_empty_payload_has_stable_digest(self) -> None:
        assert compute_benchmark_digest({}) == compute_benchmark_digest({})


# --------------------------------------------------------------------------- #
# _strip_volatile_for_executable                                               #
# --------------------------------------------------------------------------- #


class TestStripVolatileForExecutable:
    """Pin the default case mask used by the executable digest."""

    def test_drops_mtime_ctime_atime(self) -> None:
        case = _reviewed_case("c1", extra={"mtime": 1.0, "ctime": 2.0, "atime": 3.0})
        out = _strip_volatile_for_executable(case)
        assert "mtime" not in out
        assert "ctime" not in out
        assert "atime" not in out

    def test_drops_timestamp_recorded_at(self) -> None:
        case = _reviewed_case("c1", extra={"timestamp": "x", "recorded_at": "y"})
        out = _strip_volatile_for_executable(case)
        assert "timestamp" not in out
        assert "recorded_at" not in out

    def test_drops_source_path_abs_path(self) -> None:
        case = _reviewed_case("c1", extra={"source_path": "/a", "abs_path": "/b"})
        out = _strip_volatile_for_executable(case)
        assert "source_path" not in out
        assert "abs_path" not in out

    def test_drops_model_output_raw_output_transcript(self) -> None:
        case = _reviewed_case(
            "c1",
            extra={"model_output": "raw", "raw_output": "raw", "transcript": "raw"},
        )
        out = _strip_volatile_for_executable(case)
        assert "model_output" not in out
        assert "raw_output" not in out
        assert "transcript" not in out

    def test_keeps_case_id_status_content(self) -> None:
        case = _reviewed_case("c1", content="important")
        out = _strip_volatile_for_executable(case)
        assert out["case_id"] == "c1"
        assert out["status"] == "reviewed"
        assert out["content"] == "important"

    def test_keeps_unknown_extra_fields(self) -> None:
        case = _reviewed_case("c1", extra={"custom_field": 42, "reviewer": "alice"})
        out = _strip_volatile_for_executable(case)
        assert out["custom_field"] == 42
        assert out["reviewer"] == "alice"

    def test_returns_new_dict(self) -> None:
        case = _reviewed_case("c1")
        out = _strip_volatile_for_executable(case)
        assert out is not case
        # Original keeps the volatile keys (it never had any here, but
        # the contract is "return a new dict").
        assert "case_id" in case

    def test_content_change_moves_stripped_output(self) -> None:
        a = _reviewed_case("c1", content="alpha")
        b = _reviewed_case("c1", content="beta")
        assert _strip_volatile_for_executable(a) != _strip_volatile_for_executable(b)


# --------------------------------------------------------------------------- #
# _eligible_reviewed_cases                                                     #
# --------------------------------------------------------------------------- #


class TestEligibleReviewedCases:
    """Pin the partition + filter shared by both digest functions."""

    def test_jsonl_list_with_metadata_record(self) -> None:
        payload = [
            {"record_type": "metadata", "schema_version": 1},
            _reviewed_case("c1"),
            _generated_case("c2"),
            _reviewed_case("c3"),
            _disabled_case("c4"),
        ]
        out = _eligible_reviewed_cases(payload)
        ids = [c["case_id"] for c in out]
        assert ids == ["c1", "c3"]
        # Each entry is a *copy* — mutating must not affect the input.
        out[0]["case_id"] = "MUTATED"
        assert payload[1]["case_id"] == "c1"

    def test_bare_list_of_records(self) -> None:
        payload = [_reviewed_case("c1"), _generated_case("c2")]
        out = _eligible_reviewed_cases(payload)
        assert [c["case_id"] for c in out] == ["c1"]

    def test_mapping_with_cases_key(self) -> None:
        payload = {
            "schema_version": 1,
            "cases": [_reviewed_case("c1"), _generated_case("c2")],
        }
        out = _eligible_reviewed_cases(payload)
        assert [c["case_id"] for c in out] == ["c1"]

    def test_mapping_with_records_key(self) -> None:
        payload = {
            "schema_version": 1,
            "records": [_reviewed_case("c1"), _disabled_case("c2")],
        }
        out = _eligible_reviewed_cases(payload)
        assert [c["case_id"] for c in out] == ["c1"]

    def test_pre_partitioned_payload_eval_only(self) -> None:
        payload = {
            "eligible_eval_cases": [_reviewed_case("c1"), _reviewed_case("c2")],
        }
        out = _eligible_reviewed_cases(payload)
        assert [c["case_id"] for c in out] == ["c1", "c2"]

    def test_pre_partitioned_payload_held_out_only(self) -> None:
        payload = {
            "eligible_held_out_cases": [_reviewed_case("h1")],
        }
        out = _eligible_reviewed_cases(payload)
        assert [c["case_id"] for c in out] == ["h1"]

    def test_pre_partitioned_payload_both(self) -> None:
        payload = {
            "eligible_eval_cases": [_reviewed_case("e1")],
            "eligible_held_out_cases": [_reviewed_case("h1")],
        }
        out = _eligible_reviewed_cases(payload)
        assert [c["case_id"] for c in out] == ["e1", "h1"]

    def test_mapping_with_no_list_key_returns_empty(self) -> None:
        assert _eligible_reviewed_cases({"schema_version": 1}) == []

    def test_non_list_non_mapping_payload_returns_empty(self) -> None:
        assert _eligible_reviewed_cases("not a payload") == []
        assert _eligible_reviewed_cases(42) == []
        assert _eligible_reviewed_cases(None) == []

    def test_non_mapping_records_skipped(self) -> None:
        payload = [
            _reviewed_case("c1"),
            "not a mapping",
            None,
            _reviewed_case("c2"),
        ]
        out = _eligible_reviewed_cases(payload)
        assert [c["case_id"] for c in out] == ["c1", "c2"]

    def test_non_mapping_pre_partitioned_entries_skipped(self) -> None:
        payload = {
            "eligible_eval_cases": [_reviewed_case("c1"), "junk", None],
        }
        out = _eligible_reviewed_cases(payload)
        assert [c["case_id"] for c in out] == ["c1"]


# --------------------------------------------------------------------------- #
# compute_executable_benchmark_digest                                          #
# --------------------------------------------------------------------------- #


class TestComputeExecutableBenchmarkDigest:
    """Pin the eligible-only executable hash (ADR 0024 / 0029)."""

    def test_returns_sha256_hex_64_chars(self) -> None:
        payload = [_reviewed_case("c1")]
        digest = compute_executable_benchmark_digest(payload)
        assert isinstance(digest, str) and len(digest) == 64

    def test_ignores_generated_cases(self) -> None:
        base = [_reviewed_case("c1", content="ok")]
        with_gen = base + [_generated_case("c2", )]
        h_base = compute_executable_benchmark_digest(base)
        h_with = compute_executable_benchmark_digest(with_gen)
        assert h_base == h_with

    def test_ignores_disabled_cases(self) -> None:
        base = [_reviewed_case("c1", content="ok")]
        with_dis = base + [_disabled_case("c2")]
        h_base = compute_executable_benchmark_digest(base)
        h_with = compute_executable_benchmark_digest(with_dis)
        assert h_base == h_with

    def test_ignores_metadata_record(self) -> None:
        without_meta = [_reviewed_case("c1")]
        with_meta = [{"record_type": "metadata", "schema_version": 1}] + without_meta
        assert compute_executable_benchmark_digest(with_meta) == compute_executable_benchmark_digest(without_meta)

    def test_content_change_moves_digest(self) -> None:
        a = [_reviewed_case("c1", content="alpha")]
        b = [_reviewed_case("c1", content="beta")]
        assert compute_executable_benchmark_digest(a) != compute_executable_benchmark_digest(b)

    def test_default_mask_drops_volatile_keys(self) -> None:
        """Mtime-only changes must not move the executable hash."""
        without = [_reviewed_case("c1", content="ok")]
        with_mtime = [_reviewed_case("c1", content="ok", extra={"mtime": 1.0})]
        assert compute_executable_benchmark_digest(without) == compute_executable_benchmark_digest(with_mtime)

    def test_default_mask_drops_model_output(self) -> None:
        without = [_reviewed_case("c1", content="ok")]
        with_output = [_reviewed_case("c1", content="ok", extra={"model_output": "secret-prompt"})]
        assert compute_executable_benchmark_digest(without) == compute_executable_benchmark_digest(with_output)

    def test_custom_mask_fn(self) -> None:
        """A caller-supplied mask can drop *additional* fields."""

        def drop_everything(case: dict[str, Any]) -> dict[str, Any]:
            return {}

        a = [_reviewed_case("c1", content="alpha")]
        b = [_reviewed_case("c1", content="beta")]
        # With the "drop everything" mask, both hashes collapse to one.
        assert compute_executable_benchmark_digest(a, mask_fn=drop_everything) == compute_executable_benchmark_digest(b, mask_fn=drop_everything)

    def test_differs_from_full_benchmark_digest(self) -> None:
        """A volatile-only change must move the full hash but not the executable hash."""
        base = [_reviewed_case("c1", content="ok")]
        with_mtime = [_reviewed_case("c1", content="ok", extra={"mtime": 1.0})]
        assert compute_benchmark_digest(base) != compute_benchmark_digest(with_mtime)
        assert compute_executable_benchmark_digest(base) == compute_executable_benchmark_digest(with_mtime)

    def test_empty_payload_digest_is_stable(self) -> None:
        assert compute_executable_benchmark_digest([]) == compute_executable_benchmark_digest([])

    def test_no_eligible_cases(self) -> None:
        payload = [_generated_case("c1"), _disabled_case("c2")]
        # Stable: same as empty list (both yield no eligible cases).
        assert compute_executable_benchmark_digest(payload) == compute_executable_benchmark_digest([])


# --------------------------------------------------------------------------- #
# _scrub_string                                                                #
# --------------------------------------------------------------------------- #


class TestScrubString:
    """Pin the single-string scrubber (paths + secrets).

    The scrubber does *in-place* substitution: only the matched
    substring is replaced with the marker. The surrounding text
    (whitespace, prefix label) survives. Tests assert the contract
    "marker is present, raw secret is absent" rather than expecting
    the entire string to equal the marker.
    """

    def test_unix_absolute_path_is_redacted(self) -> None:
        out = _scrub_string("see /etc/passwd for details")
        assert _REDACTED_PATH in out
        assert "/etc/passwd" not in out

    def test_home_rooted_path_is_redacted(self) -> None:
        out = _scrub_string("config at ~/.metacrucible/config.json")
        assert _REDACTED_PATH in out
        assert "~/.metacrucible/config.json" not in out

    def test_dollar_home_path_is_redacted(self) -> None:
        out = _scrub_string("dump at $HOME/.metacrucible")
        assert _REDACTED_PATH in out
        assert "$HOME/.metacrucible" not in out

    def test_windows_drive_path_is_redacted(self) -> None:
        out = _scrub_string("see C:\\Users\\alice\\file.txt")
        assert _REDACTED_PATH in out
        assert "C:\\Users\\alice\\file.txt" not in out

    def test_openai_style_key_is_redacted(self) -> None:
        out = _scrub_string(f"key={FAKE_API_KEY}")
        assert _REDACTED_SECRET in out
        assert FAKE_API_KEY not in out

    def test_anthropic_style_key_is_redacted(self) -> None:
        out = _scrub_string(f"key={FAKE_ANT_KEY}")
        assert _REDACTED_SECRET in out
        assert FAKE_ANT_KEY not in out

    def test_github_pat_is_redacted(self) -> None:
        out = _scrub_string(f"Authorization: token {FAKE_GITHUB_PAT}")
        assert _REDACTED_SECRET in out
        assert FAKE_GITHUB_PAT not in out

    def test_bearer_token_is_redacted(self) -> None:
        out = _scrub_string(f"header: {FAKE_BEARER}")
        assert _REDACTED_SECRET in out
        # The "Bearer " prefix is consumed by the redaction.
        assert FAKE_BEARER not in out

    def test_safe_string_unchanged(self) -> None:
        assert _scrub_string(SAFE_LABEL) == SAFE_LABEL
        assert _scrub_string("plain text with no secrets") == "plain text with no secrets"

    def test_combined_path_and_secret(self) -> None:
        # The ABS_PATH lookbehind requires a non-identifier char before
        # the slash, so we use whitespace delimiters throughout.
        out = _scrub_string(
            f"file /etc/passwd token {FAKE_API_KEY}"
        )
        assert _REDACTED_PATH in out
        assert _REDACTED_SECRET in out
        assert "/etc/passwd" not in out
        assert FAKE_API_KEY not in out

    def test_empty_string_passthrough(self) -> None:
        assert _scrub_string("") == ""

    def test_fake_api_key_alone_is_scrubbed(self) -> None:
        """The bare fake API key string (nothing else) collapses to the marker."""
        out = _scrub_string(FAKE_API_KEY)
        assert out == _REDACTED_SECRET
        assert FAKE_API_KEY not in out


# --------------------------------------------------------------------------- #
# _scrub_summary_value                                                         #
# --------------------------------------------------------------------------- #


class TestScrubSummaryValue:
    """Pin the recursive scrubber (deny keys + string scrub).

    A key named ``"path"`` would be dropped by the deny-key filter
    (it is in :data:`DENY_KEYS`), so tests that want to assert
    path-scrubbing inside a mapping use a non-deny key name such as
    ``"leak_path"`` and put the absolute path in the *value*.
    """

    def test_scrubs_string_value_with_secret(self) -> None:
        out = _scrub_summary_value(f"key={FAKE_API_KEY}")
        # The scrubber replaces only the matched substring in place.
        assert _REDACTED_SECRET in out
        assert FAKE_API_KEY not in out

    def test_scrubs_string_value_with_path(self) -> None:
        out = _scrub_summary_value("/etc/passwd leaked here")
        assert _REDACTED_PATH in out
        assert "/etc/passwd" not in out

    def test_recurses_into_mapping(self) -> None:
        # Use non-deny-key names so the keys survive; the secret
        # substring is what should be scrubbed, not the key itself.
        value = {
            "ok": SAFE_LABEL,
            "leak": f"token={FAKE_API_KEY}",
            "nested": {"leak_path": "/etc/passwd", "keep": "fine"},
        }
        out = _scrub_summary_value(value)
        assert out["ok"] == SAFE_LABEL
        # The secret is replaced in place; the surrounding text survives.
        assert _REDACTED_SECRET in out["leak"]
        assert FAKE_API_KEY not in out["leak"]
        assert _REDACTED_PATH in out["nested"]["leak_path"]
        assert "/etc/passwd" not in out["nested"]["leak_path"]
        assert out["nested"]["keep"] == "fine"

    def test_recurses_into_list(self) -> None:
        value = [SAFE_LABEL, f"token={FAKE_API_KEY}", "/etc/passwd"]
        out = _scrub_summary_value(value)
        assert out[0] == SAFE_LABEL
        assert _REDACTED_SECRET in out[1]
        assert FAKE_API_KEY not in out[1]
        assert _REDACTED_PATH in out[2]
        assert "/etc/passwd" not in out[2]

    def test_drops_deny_keys_in_mapping(self) -> None:
        value = {
            "ok": "keep me",
            "transcript": "raw model text",
            "raw_events": ["e1"],
            "model_output": "leak",
            "local_path": "/etc/passwd",
        }
        out = _scrub_summary_value(value)
        assert out == {"ok": "keep me"}

    def test_drops_all_deny_keys(self) -> None:
        """Every key in ``DENY_KEYS`` must be dropped from a mapping."""
        value: dict[str, Any] = {k: f"value-for-{k}" for k in DENY_KEYS}
        value["keep"] = SAFE_LABEL
        out = _scrub_summary_value(value)
        assert out == {"keep": SAFE_LABEL}
        # None of the deny-key values survive.
        for k in DENY_KEYS:
            assert k not in out

    def test_passes_through_non_string_scalars(self) -> None:
        assert _scrub_summary_value(42) == 42
        assert _scrub_summary_value(3.14) == 3.14
        assert _scrub_summary_value(True) is True
        assert _scrub_summary_value(None) is None

    def test_deeply_nested_recursion(self) -> None:
        value = {"a": {"b": [{"c": f"token={FAKE_API_KEY}"}]}}
        out = _scrub_summary_value(value)
        # The match is replaced in place; only the matched substring
        # becomes the marker.
        assert out["a"]["b"][0]["c"] == f"token={_REDACTED_SECRET}"
        assert FAKE_API_KEY not in out["a"]["b"][0]["c"]

    def test_mixed_deny_keys_and_secret_strings(self) -> None:
        value = {
            "transcript": f"raw text containing {FAKE_API_KEY}",
            "status": f"using {FAKE_API_KEY} here",
        }
        out = _scrub_summary_value(value)
        # The deny key is dropped entirely; the allow-listed string is
        # scrubbed in place.
        assert "transcript" not in out
        assert _REDACTED_SECRET in out["status"]
        assert FAKE_API_KEY not in out["status"]

    def test_fake_api_key_fixture_is_scrubbed_at_top_level(self) -> None:
        """The fake API key fixture, fed in at the top level, collapses to the marker."""
        assert _scrub_summary_value(FAKE_API_KEY) == _REDACTED_SECRET


# --------------------------------------------------------------------------- #
# build_summary_payload                                                        #
# --------------------------------------------------------------------------- #


class TestBuildSummaryPayload:
    """Pin the v1 summary allowlist + scrub contract (ADR 0030)."""

    def test_stamps_schema_version(self) -> None:
        out = build_summary_payload({})
        assert out["schema_version"] == SCHEMA_VERSION

    def test_caller_schema_version_is_overridden(self) -> None:
        out = build_summary_payload({"schema_version": 999})
        assert out["schema_version"] == SCHEMA_VERSION

    def test_keeps_only_allowed_top_level_keys(self) -> None:
        payload = {
            "aggregate_status": "ok",
            "status": "ok",
            "counts": {"reviewed": 5},
            "split_summaries": {"eval": {"n": 5}},
            "weakest_dimensions": [{"name": "x"}],
            "accepted_revision_id": "rev-1",
            "best_revision_id": "rev-1",
            "blockers": [],
            "warnings": [],
            "cost_summary": {"usd": 0.0},
            "duration": 1.0,
            "transcript": "raw leak",  # not allowed at top level
            "model_output": "raw leak",  # not allowed at top level
            "custom_field": "should be dropped",
        }
        out = build_summary_payload(payload)
        # Every allowed key was kept.
        for key in SUMMARY_ALLOWED_TOP_KEYS:
            assert key in out
        # Disallowed top-level keys were dropped.
        for forbidden in ("transcript", "model_output", "custom_field"):
            assert forbidden not in out

    def test_summary_allowed_keys_match_pinned_set(self) -> None:
        """Keys kept are exactly those in the allowlist (plus schema_version)."""
        full_input: dict[str, Any] = dict.fromkeys(
            SUMMARY_ALLOWED_TOP_KEYS, "value"
        )
        full_input["transcript"] = "raw"  # must be dropped
        full_input["custom"] = "x"  # must be dropped
        out = build_summary_payload(full_input)
        assert set(out.keys()) == SUMMARY_ALLOWED_TOP_KEYS | {"schema_version"}
        # Sanity: the dropped keys did not sneak through.
        assert "transcript" not in out
        assert "custom" not in out

    def test_scrubs_path_in_string_field(self) -> None:
        out = build_summary_payload({"status": "ran in /etc/passwd"})
        assert "/etc/passwd" not in out["status"]
        assert _REDACTED_PATH in out["status"]

    def test_scrubs_fake_api_key_in_string_field(self) -> None:
        """The summary scrubber MUST redact the fake API key fixture."""
        out = build_summary_payload(
            {"status": f"used key {FAKE_API_KEY} for run"}
        )
        assert FAKE_API_KEY not in out["status"]
        assert _REDACTED_SECRET in out["status"]

    def test_scrubs_fake_api_key_in_nested_mapping(self) -> None:
        out = build_summary_payload(
            {"cost_summary": {"usd": 1.0, "note": f"key={FAKE_API_KEY}"}}
        )
        assert FAKE_API_KEY not in out["cost_summary"]["note"]
        assert _REDACTED_SECRET in out["cost_summary"]["note"]

    def test_scrubs_fake_api_key_in_nested_list(self) -> None:
        out = build_summary_payload(
            {"warnings": [f"watch {FAKE_API_KEY}", "fine"]}
        )
        assert _REDACTED_SECRET in out["warnings"][0]
        assert FAKE_API_KEY not in out["warnings"][0]
        assert out["warnings"][1] == "fine"

    def test_drops_deny_keys_in_nested_mapping(self) -> None:
        out = build_summary_payload(
            {
                "split_summaries": {
                    "eval": {
                        "n": 5,
                        "transcript": "raw leak",
                        "model_output": "raw leak",
                        "ok": SAFE_LABEL,
                    }
                }
            }
        )
        nested = out["split_summaries"]["eval"]
        assert nested == {"n": 5, "ok": SAFE_LABEL}

    def test_drops_non_string_non_collection_scalars_unchanged(self) -> None:
        out = build_summary_payload({"counts": {"reviewed": 5, "n": 0}})
        assert out["counts"] == {"reviewed": 5, "n": 0}

    def test_fake_api_key_fixture_present_and_scrubbed_in_summary(self) -> None:
        """End-to-end check on the fake API key fixture (the contract pin).

        The fixture MUST appear in the input; it MUST NOT appear in the
        serialized output. The redaction marker MUST appear in the
        serialized output instead.
        """
        payload = {
            "aggregate_status": "ok",
            "status": f"ok: token={FAKE_API_KEY}",
            "warnings": [f"key leaked: {FAKE_API_KEY}"],
        }
        out = build_summary_payload(payload)
        # The fake key never appears anywhere in the serialized output.
        serialized = json.dumps(out, default=str)
        assert FAKE_API_KEY not in serialized
        assert _REDACTED_SECRET in serialized


# --------------------------------------------------------------------------- #
# build_receipt_payload                                                        #
# --------------------------------------------------------------------------- #


class TestBuildReceiptPayload:
    """Pin the v1 receipt contract (ADR 0030)."""

    def test_stamps_schema_version(self) -> None:
        out = build_receipt_payload(_minimal_receipt())
        assert out["schema_version"] == SCHEMA_VERSION

    def test_caller_schema_version_is_overridden(self) -> None:
        out = build_receipt_payload(
            _minimal_receipt(schema_version=999)
        )
        assert out["schema_version"] == SCHEMA_VERSION

    def test_default_summary_ref_applied(self) -> None:
        out = build_receipt_payload(_minimal_receipt())
        assert out["summary_ref"] == RECEIPT_DEFAULT_SUMMARY_REF

    def test_default_trajectory_digest_ref_applied(self) -> None:
        out = build_receipt_payload(_minimal_receipt())
        assert out["trajectory_digest_ref"] == RECEIPT_DEFAULT_TRAJECTORY_DIGEST_REF

    def test_custom_default_ref_overrides(self) -> None:
        out = build_receipt_payload(
            _minimal_receipt(),
            default_summary_ref="alt-summary.json",
            default_trajectory_digest_ref="alt-traj.json",
        )
        assert out["summary_ref"] == "alt-summary.json"
        assert out["trajectory_digest_ref"] == "alt-traj.json"

    def test_caller_refs_preserved_when_provided(self) -> None:
        out = build_receipt_payload(
            _minimal_receipt(
                summary_ref="my-summary.json",
                trajectory_digest_ref="my-traj.json",
            )
        )
        assert out["summary_ref"] == "my-summary.json"
        assert out["trajectory_digest_ref"] == "my-traj.json"

    def test_ref_fields_are_validated_keys(self) -> None:
        assert "summary_ref" in RECEIPT_REF_FIELDS
        assert "trajectory_digest_ref" in RECEIPT_REF_FIELDS

    def test_ref_list_fields_are_validated_keys(self) -> None:
        assert "case_result_refs" in RECEIPT_REF_LIST_FIELDS
        assert "event_log_refs" in RECEIPT_REF_LIST_FIELDS

    def test_case_result_refs_passed_through(self) -> None:
        out = build_receipt_payload(
            _minimal_receipt(case_result_refs=["a.json", "b.json"])
        )
        assert out["case_result_refs"] == ["a.json", "b.json"]

    def test_event_log_refs_passed_through(self) -> None:
        out = build_receipt_payload(
            _minimal_receipt(event_log_refs=["e1.jsonl", "e2.jsonl"])
        )
        assert out["event_log_refs"] == ["e1.jsonl", "e2.jsonl"]

    def test_rejects_absolute_summary_ref(self) -> None:
        with pytest.raises(ValueError, match="summary_ref"):
            build_receipt_payload(_minimal_receipt(summary_ref="/etc/summary.json"))

    def test_rejects_home_rooted_summary_ref(self) -> None:
        with pytest.raises(ValueError, match="summary_ref"):
            build_receipt_payload(_minimal_receipt(summary_ref="~/summary.json"))

    def test_rejects_path_separator_in_summary_ref(self) -> None:
        with pytest.raises(ValueError, match="summary_ref"):
            build_receipt_payload(_minimal_receipt(summary_ref="nested/summary.json"))

    def test_rejects_parent_traversal_in_summary_ref(self) -> None:
        with pytest.raises(ValueError, match="summary_ref"):
            build_receipt_payload(_minimal_receipt(summary_ref=".."))

    def test_rejects_empty_summary_ref(self) -> None:
        with pytest.raises(ValueError, match="summary_ref"):
            build_receipt_payload(_minimal_receipt(summary_ref=""))

    def test_rejects_null_byte_in_summary_ref(self) -> None:
        with pytest.raises(ValueError, match="summary_ref"):
            build_receipt_payload(_minimal_receipt(summary_ref="bad\x00name.json"))

    def test_rejects_untrimmed_summary_ref(self) -> None:
        with pytest.raises(ValueError, match="summary_ref"):
            build_receipt_payload(_minimal_receipt(summary_ref=" summary.json "))

    def test_rejects_absolute_trajectory_digest_ref(self) -> None:
        with pytest.raises(ValueError, match="trajectory_digest_ref"):
            build_receipt_payload(
                _minimal_receipt(trajectory_digest_ref="/etc/traj.json")
            )

    def test_rejects_path_separator_in_trajectory_digest_ref(self) -> None:
        with pytest.raises(ValueError, match="trajectory_digest_ref"):
            build_receipt_payload(
                _minimal_receipt(trajectory_digest_ref="nested/traj.json")
            )

    def test_rejects_non_string_summary_ref(self) -> None:
        with pytest.raises(ValueError, match="summary_ref"):
            build_receipt_payload(_minimal_receipt(summary_ref=42))  # type: ignore[arg-type]

    def test_rejects_non_list_case_result_refs(self) -> None:
        with pytest.raises(ValueError, match="case_result_refs"):
            build_receipt_payload(
                _minimal_receipt(case_result_refs="not-a-list")  # type: ignore[arg-type]
            )

    def test_validates_each_item_in_case_result_refs(self) -> None:
        with pytest.raises(ValueError, match="case_result_refs"):
            build_receipt_payload(
                _minimal_receipt(case_result_refs=["ok.json", "/abs.json"])
            )

    def test_validates_each_item_in_event_log_refs(self) -> None:
        with pytest.raises(ValueError, match="event_log_refs"):
            build_receipt_payload(
                _minimal_receipt(event_log_refs=["ok.jsonl", "nested/ok.jsonl"])
            )

    def test_passes_through_known_contract_fields(self) -> None:
        payload = _minimal_receipt()
        out = build_receipt_payload(payload)
        for key in (
            "run_id",
            "run_type",
            "status",
            "artifact",
            "envelope",
            "benchmark_sha",
            "executable_benchmark_sha",
            "evaluation_harness",
            "optimizer_harness",
            "runtime_adapter",
            "model_identities",
            "execution_boundary_id",
            "execution_boundary_object",
            "blockers",
        ):
            assert out[key] == payload[key]

    def test_keeps_unknown_extension_fields(self) -> None:
        """The v1 contract is "validate the listed fields, do not forbid
        unknown ones" — extra keys survive."""
        out = build_receipt_payload(
            _minimal_receipt(custom_extension={"x": 1}, signature="abc")
        )
        assert out["custom_extension"] == {"x": 1}
        assert out["signature"] == "abc"

    def test_does_not_mutate_input(self) -> None:
        payload = _minimal_receipt()
        snapshot = json.dumps(payload, sort_keys=True, default=str)
        build_receipt_payload(payload)
        assert json.dumps(payload, sort_keys=True, default=str) == snapshot


# --------------------------------------------------------------------------- #
# _redact_trajectory_step                                                      #
# --------------------------------------------------------------------------- #


class TestRedactTrajectoryStep:
    """Pin the per-step scrub + deny-key drop."""

    def test_non_mapping_returned_unchanged(self) -> None:
        assert _redact_trajectory_step("not a step") == "not a step"
        assert _redact_trajectory_step(42) == 42
        assert _redact_trajectory_step(None) is None

    def test_scrubs_string_fields(self) -> None:
        step = {"action": f"call api with {FAKE_API_KEY}", "status": "ok"}
        out = _redact_trajectory_step(step)
        # The scrubber replaces only the matched substring in place.
        assert _REDACTED_SECRET in out["action"]
        assert FAKE_API_KEY not in out["action"]
        assert out["status"] == "ok"

    def test_scrubs_absolute_path_in_text(self) -> None:
        step = {"text": "wrote /etc/passwd to disk"}
        out = _redact_trajectory_step(step)
        assert _REDACTED_PATH in out["text"]
        assert "/etc/passwd" not in out["text"]

    def test_drops_deny_keys(self) -> None:
        step = {
            "step": 1,
            "action": "do thing",
            "transcript": "raw text",
            "model_output": "raw",
            "raw_events": ["e1"],
            "stdout": "log",
            "stderr": "log",
        }
        out = _redact_trajectory_step(step)
        assert "transcript" not in out
        assert "model_output" not in out
        assert "raw_events" not in out
        assert "stdout" not in out
        assert "stderr" not in out
        assert out["step"] == 1
        assert out["action"] == "do thing"

    def test_drops_all_deny_keys(self) -> None:
        step: dict[str, Any] = {k: f"v-{k}" for k in DENY_KEYS}
        step["step"] = 1
        out = _redact_trajectory_step(step)
        assert out == {"step": 1}

    def test_keeps_non_string_scalars(self) -> None:
        step = {"step": 1, "ok": True, "score": 0.95}
        out = _redact_trajectory_step(step)
        assert out == {"step": 1, "ok": True, "score": 0.95}

    def test_does_not_recurse_into_nested_mapping(self) -> None:
        """``_redact_trajectory_step`` does NOT recurse — only top-level
        keys are inspected. (Recursion is delegated to the digest
        builder for non-step mapping values.)"""
        step = {
            "step": 1,
            "payload": {
                "transcript": "raw leak",  # would be stripped if we recursed
                "text": f"key={FAKE_API_KEY}",  # would be scrubbed if we recursed
            },
        }
        out = _redact_trajectory_step(step)
        # The nested mapping survives unchanged.
        assert out["payload"] == step["payload"]
        assert "transcript" in out["payload"]
        assert FAKE_API_KEY in out["payload"]["text"]

    def test_returns_new_dict(self) -> None:
        step = {"step": 1, "action": "x"}
        out = _redact_trajectory_step(step)
        assert out is not step
        assert "step" in step  # input untouched


# --------------------------------------------------------------------------- #
# build_trajectory_digest_payload                                              #
# --------------------------------------------------------------------------- #


class TestBuildTrajectoryDigestPayload:
    """Pin the v1 trajectory digest builder (ADR 0030)."""

    def test_stamps_schema_version(self) -> None:
        out = build_trajectory_digest_payload({})
        assert out["schema_version"] == SCHEMA_VERSION

    def test_caller_schema_version_is_overridden(self) -> None:
        out = build_trajectory_digest_payload({"schema_version": 999})
        assert out["schema_version"] == SCHEMA_VERSION

    def test_steps_pass_through_with_redaction(self) -> None:
        payload = {
            "steps": [
                {"step": 1, "action": "first", "transcript": "raw"},
                {"step": 2, "action": "second"},
            ]
        }
        out = build_trajectory_digest_payload(payload)
        assert len(out["steps"]) == 2
        assert out["steps"][0]["action"] == "first"
        assert "transcript" not in out["steps"][0]
        assert out["steps"][1]["step"] == 2
        assert "steps_truncated" not in out

    def test_steps_capped_by_max_steps(self) -> None:
        payload = {
            "steps": [
                {"step": 1, "action": "a"},
                {"step": 2, "action": "b"},
                {"step": 3, "action": "c"},
            ]
        }
        out = build_trajectory_digest_payload(payload, max_steps=2)
        assert len(out["steps"]) == 2
        assert out["steps"][0]["step"] == 1
        assert out["steps"][1]["step"] == 2
        assert out["steps_truncated"] is True

    def test_steps_not_truncated_when_under_cap(self) -> None:
        payload = {
            "steps": [
                {"step": 1, "action": "a"},
                {"step": 2, "action": "b"},
            ]
        }
        out = build_trajectory_digest_payload(payload, max_steps=5)
        assert len(out["steps"]) == 2
        assert "steps_truncated" not in out

    def test_per_step_text_truncation(self) -> None:
        payload = {
            "steps": [{"step": 1, "text": "abcdefghij" * 10}],
        }
        out = build_trajectory_digest_payload(payload, max_text_chars=20)
        text = out["steps"][0]["text"]
        assert text.startswith("abcdefghijabcdefghij")  # first 20 chars
        assert "truncated at 20 chars" in text
        assert out["steps_truncated"] is True

    def test_per_step_text_under_cap_passes_through(self) -> None:
        payload = {
            "steps": [{"step": 1, "text": "short text"}],
        }
        out = build_trajectory_digest_payload(payload, max_text_chars=100)
        assert out["steps"][0]["text"] == "short text"
        assert "steps_truncated" not in out

    def test_scrubs_secret_in_step_text(self) -> None:
        payload = {
            "steps": [
                {"step": 1, "text": f"using key {FAKE_API_KEY}"},
            ]
        }
        out = build_trajectory_digest_payload(payload)
        assert FAKE_API_KEY not in out["steps"][0]["text"]
        assert _REDACTED_SECRET in out["steps"][0]["text"]

    def test_scrubs_secret_in_step_action(self) -> None:
        payload = {
            "steps": [
                {"step": 1, "action": f"call {FAKE_API_KEY}"},
            ]
        }
        out = build_trajectory_digest_payload(payload)
        assert _REDACTED_SECRET in out["steps"][0]["action"]

    def test_scrubs_path_in_step_text(self) -> None:
        payload = {"steps": [{"step": 1, "text": "open /etc/passwd"}]}
        out = build_trajectory_digest_payload(payload)
        assert _REDACTED_PATH in out["steps"][0]["text"]
        assert "/etc/passwd" not in out["steps"][0]["text"]

    def test_drops_deny_keys_in_each_step(self) -> None:
        payload = {
            "steps": [
                {
                    "step": 1,
                    "action": "do",
                    "transcript": "raw",
                    "model_output": "raw",
                    "raw_events": ["e1"],
                }
            ]
        }
        out = build_trajectory_digest_payload(payload)
        step = out["steps"][0]
        assert "transcript" not in step
        assert "model_output" not in step
        assert "raw_events" not in step
        assert step["step"] == 1
        assert step["action"] == "do"

    def test_scrubs_string_top_level_fields(self) -> None:
        payload = {
            "title": f"ran with {FAKE_API_KEY}",
            "status": "ok",
        }
        out = build_trajectory_digest_payload(payload)
        assert _REDACTED_SECRET in out["title"]
        assert out["status"] == "ok"

    def test_scrubs_string_in_top_level_list_of_strings(self) -> None:
        payload = {
            "tags": [f"key={FAKE_API_KEY}", "safe"],
        }
        out = build_trajectory_digest_payload(payload)
        assert _REDACTED_SECRET in out["tags"][0]
        assert FAKE_API_KEY not in out["tags"][0]
        assert out["tags"][1] == "safe"

    def test_passes_through_non_string_non_collection_top_level_scalars(
        self,
    ) -> None:
        payload = {"duration_ms": 1234, "ok": True, "score": 0.5}
        out = build_trajectory_digest_payload(payload)
        assert out["duration_ms"] == 1234
        assert out["ok"] is True
        assert out["score"] == 0.5

    def test_non_list_steps_value_passes_through(self) -> None:
        """If ``steps`` is not a list, the value is treated as a generic
        field and passed through (string-scrubbed if it's a string)."""
        payload = {"steps": "not-a-list"}
        out = build_trajectory_digest_payload(payload)
        assert out["steps"] == "not-a-list"

    def test_combined_truncation_and_scrub(self) -> None:
        # The secret is scrubbed *first*, then the result is truncated.
        # A long surrounding text ensures the redacted marker survives
        # the truncation at 20 chars (the marker itself is 18 chars).
        payload = {
            "steps": [
                {
                    "step": 1,
                    # After scrub: "prefix [redacted:secret] suffix xxx..."
                    "text": f"prefix {FAKE_API_KEY} suffix " + ("x" * 100),
                },
                {"step": 2, "text": "short"},
            ]
        }
        out = build_trajectory_digest_payload(
            payload, max_steps=1, max_text_chars=20
        )
        assert len(out["steps"]) == 1
        text = out["steps"][0]["text"]
        # The raw API key must never appear in the truncated output.
        assert FAKE_API_KEY not in text
        # And the truncation marker must be appended.
        assert "truncated at 20 chars" in text
        assert out["steps_truncated"] is True

    def test_top_level_mapping_scrubs_recursively(self) -> None:
        payload = {
            "meta": {"leak": f"key={FAKE_API_KEY}", "transcript": "raw"},
        }
        out = build_trajectory_digest_payload(payload)
        assert _REDACTED_SECRET in out["meta"]["leak"]
        assert FAKE_API_KEY not in out["meta"]["leak"]
        # ``transcript`` is a deny key, so it is dropped by the recursive
        # scrubber — the top-level mapping path uses
        # ``_scrub_summary_value`` which applies the deny filter.
        assert "transcript" not in out["meta"]
