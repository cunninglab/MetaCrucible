"""Tests for the recorded-replay CI harness loader (Issue #45, ADR 0021).

The replay harness is the foundation for CI's no-LLM orchestration
(ADR 0021, ADR 0028). These tests are pure-logic: no live LLM, no
network, no sleep, no real secrets. They cover the public surface
introduced by Issue #45 Task 1: ``load_replay``,
``build_judge_call_fns``, ``build_optimizer_call_fn``, the
:class:`Replay` class, the :class:`ReplayError` / :class:`ReplayExhausted`
exceptions, and the secret-scan helper :func:`scan_replay_text`.
"""
from __future__ import annotations

import os  # used only by the env-isolation test below
from pathlib import Path
from typing import Any, Callable

import pytest

from metacrucible import replay as replay_mod
from metacrucible.replay import (
    REPLAY_SCHEMA_VERSION,
    Replay,
    ReplayError,
    ReplayExhausted,
    build_judge_call_fns,
    build_optimizer_call_fn,
    load_replay,
    scan_replay_text,
)


# Canonical 20-character AWS access key id (AKIA + 16 chars).
_AWS_ACCESS_KEY_ID: str = "AKIAIOSFODNN7EXAMPLE"
# Canonical 40-character GitHub personal access token (ghp_ + 36 chars).
_GITHUB_PAT: str = "ghp_" + "1" * 36  # 40 chars total
# Canonical Stripe live secret key (sk_live_ + 24+ chars).
_STRIPE_LIVE_KEY: str = "sk_live_" + "x" * 24  # 33 chars total


def _write_fixture(
    tmp_path: Path,
    name: str,
    body: str,
) -> Path:
    """Write ``body`` to a JSONL fixture under ``tmp_path`` and return the path."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _record(
    name: str,
    *,
    schema_version: int = REPLAY_SCHEMA_VERSION,
    response: Any = ...,
    responses: Any = ...,
) -> str:
    """Return a JSONL line for a single record.

    Pass exactly one of ``response`` (single value) or ``responses``
    (list of values). The default ``response=...`` (``Ellipsis``)
    means "no response field"; callers that need a value pass it
    explicitly.
    """
    import json

    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "name": name,
    }
    if response is not ...:
        payload["response"] = response
    if responses is not ...:
        payload["responses"] = responses
    return json.dumps(payload, separators=(", ", ": "))


# --------------------------------------------------------------------------- #
# Public surface                                                              #
# --------------------------------------------------------------------------- #


def test_replay_module_exposes_required_public_names() -> None:
    """The public surface pinned in the task brief is exposed at module scope."""
    expected = {
        "REPLAY_SCHEMA_VERSION",
        "Replay",
        "ReplayError",
        "ReplayExhausted",
        "build_judge_call_fns",
        "build_optimizer_call_fn",
        "load_replay",
        "scan_replay_text",
    }
    actual = set(dir(replay_mod))
    missing = expected - actual
    assert not missing, (
        f"replay module is missing required public names: {sorted(missing)!r}"
    )


def test_replay_schema_version_is_pinned() -> None:
    """Issue #45 pins the replay schema version to 1."""
    assert REPLAY_SCHEMA_VERSION == 1


def test_replay_error_is_value_error_subclass() -> None:
    """ReplayError must be a ValueError so existing error-handling code
    that catches ``ValueError`` continues to work."""
    assert issubclass(ReplayError, ValueError)


def test_replay_exhausted_is_replay_error_subclass() -> None:
    """ReplayExhausted must be a ReplayError so over-call detection
    can branch on ``isinstance(exc, ReplayExhausted)``."""
    assert issubclass(ReplayExhausted, ReplayError)


# --------------------------------------------------------------------------- #
# Round-trip loading                                                          #
# --------------------------------------------------------------------------- #


def test_load_replay_round_trip_minimal_fixture(tmp_path: Path) -> None:
    """A minimal fixture (two judge_* entries + one optimizer) loads
    and exposes its entries in declared order."""
    body = "\n".join(
        [
            _record("judge_1", response={"verdict": "pass"}),
            _record("judge_2", response={"verdict": "pass"}),
            _record("optimizer", responses=[{"edit": 1}, {"edit": 2}]),
            "",  # trailing newline
        ]
    )
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    replay = load_replay(path)
    assert isinstance(replay, Replay)
    assert replay.entry_names == ("judge_1", "judge_2", "optimizer")


# --------------------------------------------------------------------------- #
# Structural rejections                                                       #
# --------------------------------------------------------------------------- #


def test_load_replay_rejects_missing_schema_version(tmp_path: Path) -> None:
    """A record without ``schema_version`` is rejected as missing_field."""
    body = '{"name": "judge_1", "response": {"x": 1}}\n'
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "missing_field"


def test_load_replay_rejects_wrong_schema_version(tmp_path: Path) -> None:
    """A record with a non-current ``schema_version`` is rejected as
    schema_mismatch."""
    body = _record("judge_1", schema_version=2, response={"x": 1}) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "schema_mismatch"


def test_load_replay_rejects_duplicate_name(tmp_path: Path) -> None:
    """Two records sharing the same ``name`` are rejected as duplicate_name."""
    body = "\n".join(
        [
            _record("judge_1", response={"x": 1}),
            _record("judge_1", response={"x": 2}),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "duplicate_name"


def test_load_replay_rejects_malformed_json(tmp_path: Path) -> None:
    """A line that is not valid JSON is rejected as malformed_json."""
    body = "{not valid json}\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "malformed_json"


def test_load_replay_rejects_both_response_shapes(tmp_path: Path) -> None:
    """A record carrying both ``response`` and ``responses`` is rejected
    as both_response_shapes."""
    import json

    payload = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "name": "judge_1",
        "response": {"x": 1},
        "responses": [{"x": 2}],
    }
    body = json.dumps(payload) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "both_response_shapes"


def test_load_replay_rejects_empty_responses(tmp_path: Path) -> None:
    """A record with ``responses=[]`` is rejected as empty_responses."""
    body = _record("optimizer", responses=[]) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "empty_responses"


# --------------------------------------------------------------------------- #
# Secret-pattern rejection (ADR 0036)                                         #
# --------------------------------------------------------------------------- #


def test_load_replay_rejects_aws_secret(tmp_path: Path) -> None:
    """An AWS access key id in a record's response is rejected as
    secret_detected, and the error's pattern_id / line_number match
    what :func:`scan_replay_text` would report."""
    body = "\n".join(
        [
            _record("judge_1", response={"leak": _AWS_ACCESS_KEY_ID}),
            _record("judge_2", response={"verdict": "pass"}),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    text = path.read_text(encoding="utf-8")
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "secret_detected"
    expected = scan_replay_text(text)
    assert expected, "scan_replay_text must report at least one match"
    assert (
        exc_info.value.pattern_id,
        exc_info.value.line_number,
    ) == expected[0]


def test_load_replay_rejects_github_pat(tmp_path: Path) -> None:
    """A GitHub personal access token in a record's response is rejected
    as secret_detected, with pattern_id / line_number matching
    :func:`scan_replay_text`."""
    body = "\n".join(
        [
            _record("judge_1", response={"leak": _GITHUB_PAT}),
            _record("judge_2", response={"verdict": "pass"}),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    text = path.read_text(encoding="utf-8")
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "secret_detected"
    expected = scan_replay_text(text)
    assert expected, "scan_replay_text must report at least one match"
    assert (
        exc_info.value.pattern_id,
        exc_info.value.line_number,
    ) == expected[0]


def test_load_replay_rejects_stripe_live_secret(tmp_path: Path) -> None:
    """A Stripe live secret key in a record's response is rejected as
    secret_detected, with pattern_id / line_number matching
    :func:`scan_replay_text`."""
    body = "\n".join(
        [
            _record("judge_1", response={"leak": _STRIPE_LIVE_KEY}),
            _record("judge_2", response={"verdict": "pass"}),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    text = path.read_text(encoding="utf-8")
    with pytest.raises(ReplayError) as exc_info:
        load_replay(path)
    assert exc_info.value.code == "secret_detected"
    expected = scan_replay_text(text)
    assert expected, "scan_replay_text must report at least one match"
    assert (
        exc_info.value.pattern_id,
        exc_info.value.line_number,
    ) == expected[0]


def test_scan_replay_text_returns_line_numbers() -> None:
    """``scan_replay_text`` returns 1-indexed line numbers for each match.

    Putting the same AWS key on different lines proves the line
    attribution is line-accurate, not just "1".
    """
    text = (
        "alpha\n"
        f"leak: {_AWS_ACCESS_KEY_ID}\n"  # line 2
        "beta\n"
        f"another leak: {_AWS_ACCESS_KEY_ID}\n"  # line 4
    )
    matches = scan_replay_text(text)
    line_numbers = [line for _pattern_id, line in matches]
    assert 2 in line_numbers
    assert 4 in line_numbers


# --------------------------------------------------------------------------- #
# Callable builders                                                           #
# --------------------------------------------------------------------------- #


def test_build_judge_call_fns_returns_two_distinct_callables(
    tmp_path: Path,
) -> None:
    """``build_judge_call_fns`` returns exactly two distinct callables,
    each with the entry name visible in ``__name__``."""
    body = "\n".join(
        [
            _record("judge_1", response={"v": 1}),
            _record("judge_2", response={"v": 2}),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    replay = load_replay(path)
    fns: list[Callable[..., Any]] = build_judge_call_fns(replay)
    assert len(fns) == 2
    assert fns[0] is not fns[1]
    assert "judge_1" in (fns[0].__name__ or "")
    assert "judge_2" in (fns[1].__name__ or "")


def test_build_judge_call_fns_advance_in_order(tmp_path: Path) -> None:
    """Each judge callable returns the next recorded response on the
    first call (the first fn returns judge_1's first response; the
    second fn returns judge_2's first response)."""
    body = "\n".join(
        [
            _record("judge_1", responses=[{"v": "a1"}, {"v": "a2"}]),
            _record("judge_2", responses=[{"v": "b1"}, {"v": "b2"}]),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    replay = load_replay(path)
    fns = build_judge_call_fns(replay)
    assert fns[0](context={"probe": True}) == {"v": "a1"}
    assert fns[1](context={"probe": True}) == {"v": "b1"}
    # Second call on each returns the second recorded response.
    assert fns[0](context={"probe": True}) == {"v": "a2"}
    assert fns[1](context={"probe": True}) == {"v": "b2"}


def test_build_optimizer_call_fn_consumes_in_order(tmp_path: Path) -> None:
    """The optimizer callable consumes its queue in declared order."""
    body = "\n".join(
        [
            _record("judge_1", response={"v": 1}),
            _record("judge_2", response={"v": 2}),
            _record(
                "optimizer",
                responses=[{"edit": 1}, {"edit": 2}, {"edit": 3}],
            ),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    replay = load_replay(path)
    call_fn = build_optimizer_call_fn(replay)
    assert call_fn(repair_context=None) == {"edit": 1}
    assert call_fn(repair_context={"schema": {}}) == {"edit": 2}
    assert call_fn(repair_context={"schema": {}}) == {"edit": 3}


def test_replay_callables_raise_on_exhaustion(tmp_path: Path) -> None:
    """Over-calling a judge callable raises ``ReplayExhausted`` with the
    entry name and current call index."""
    body = "\n".join(
        [
            _record("judge_1", response={"v": "only"}),
            _record("judge_2", response={"v": "only"}),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    replay = load_replay(path)
    fns = build_judge_call_fns(replay)
    # First call returns the recorded response.
    assert fns[0](context=None) == {"v": "only"}
    # Second call on the same entry exhausts the queue.
    with pytest.raises(ReplayExhausted) as exc_info:
        fns[0](context=None)
    assert exc_info.value.code == "replay_exhausted"
    assert exc_info.value.entry_name == "judge_1"
    assert exc_info.value.call_index == 1


def test_build_judge_call_fns_raises_when_entry_missing(
    tmp_path: Path,
) -> None:
    """A replay missing ``judge_2`` cannot produce two judge callables."""
    body = _record("judge_1", response={"v": 1}) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    replay = load_replay(path)
    with pytest.raises(ReplayError) as exc_info:
        build_judge_call_fns(replay)
    assert exc_info.value.code == "missing_entry"


# --------------------------------------------------------------------------- #
# Environment isolation                                                       #
# --------------------------------------------------------------------------- #


def test_replay_does_not_read_api_key_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay loader must not consult provider API-key environment
    variables. Removing them and loading a valid fixture must succeed
    and return the expected entry names."""
    # Defensive cleanup: even if the host env has these set, the
    # loader must not depend on them. ``raising=False`` so the test
    # passes on a host that never set them in the first place.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # ``os`` is referenced here only to make the env-isolation
    # contract obvious to a reviewer; the loader does not import or
    # read it.
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
    body = "\n".join(
        [
            _record("judge_1", response={"v": 1}),
            _record("judge_2", response={"v": 2}),
            _record("optimizer", responses=[{"edit": 1}]),
        ]
    ) + "\n"
    path = _write_fixture(tmp_path, "replay.jsonl", body)
    replay = load_replay(path)
    assert replay.entry_names == ("judge_1", "judge_2", "optimizer")
