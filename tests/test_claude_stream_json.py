"""Tests for Issue #12: Claude Code ``stream-json`` parser and evidence.

Issue #12 pins the public behavior of the Claude Code
``--output-format stream-json`` parser that the runtime adapter
(ADR 0028) feeds into the evidence bundle (ADR 0030). The parser
must capture the minimum evidence fields required to classify a
run, regardless of whether the run succeeded, errored, or was
truncated.

Acceptance criteria (Issue #12):

  - Captures start/completion (a ``system/init`` event and a
    ``result`` event).
  - Captures exit code, final output, stderr, and error diagnostic.
  - Captures raw event count.
  - Captures adapter version and runtime (``claude_code``) version.
  - Missing or unparseable values surface as machine-stable blocker
    ids; usage/tool details that are absent record as warnings, not
    blockers (ADR 0028: "usage and tool-call details are recorded
    when present but missing values are warnings rather than
    blockers").

These tests are the red step: ``metacrucible.claude_stream_json``
is not implemented yet, so importing it must fail. Once the parser
lands, the tests turn green and pin the exact field shape and
blocker ids that downstream automation (receipt/summary writers,
ADR 0030) branches on verbatim.

The implementation under test (not yet written) is expected to
live in ``src/metacrucible/claude_stream_json.py`` and expose at
least:

  - ``ADAPTER_VERSION`` — the runtime adapter identifier+version
    (string), e.g. ``"claude-code/0.4.1"``.
  - ``parse_stream_json(text, *, adapter_version, claude_code_version=None,
    exit_code=0, stderr="")`` — return an evidence dict (or
    dataclass-with-``as_dict()``) carrying the AC fields above plus
    a ``blockers`` list and a ``warnings`` list.

The blockers list carries stable snake_case ids drawn from the
fixed small machine-stable set pinned by Issue #12 / ADR 0028:

  - ``stream-json-init-missing``         - no ``system/init`` event found
  - ``stream-json-result-missing``       - no ``result`` event found
  - ``stream-json-malformed-line``       - a line was not valid JSON
  - ``stream-json-runtime-version-missing``
                                          - no ``claude_code_version`` in
                                            init event and no override
                                            supplied by the caller
  - ``stream-json-adapter-version-missing``
                                          - caller did not pass
                                            ``adapter_version``
  - ``stream-json-error-result``         - the ``result`` event signals
                                            an error (``is_error=True``)
                                            and the run should be
                                            classified as failed

References
----------
- ADR 0028 (Claude Code adapter contract): "MVP run evidence
  requires start/completion, exit code, final output, stderr or
  error diagnostics, raw event count, adapter version, and Claude
  Code version; usage and tool-call details are recorded when
  present but missing values are warnings rather than blockers."
- ADR 0030 (receipt / evidence bundle v1): the parser is the
  adapter-side producer of the evidence fields the bundle stores.
- Issue #12 acceptance criteria.
"""
from __future__ import annotations

import importlib
import json
from typing import Any, Iterable

import pytest

STREAM_JSON_MODULE = "metacrucible.claude_stream_json"

#: Stable adapter identifier+version (the parser must expose it as
#: a module constant so the receipt writer can quote it verbatim).
ADAPTER_VERSION = "claude-code/0.4.1"

#: Stable Claude Code runtime version used across the realistic
#: fixtures. Matches the field Claude Code emits in the
#: ``system/init`` event under the ``claude_code_version`` key.
RUNTIME_VERSION = "1.0.0"

#: Stable blocker ids the parser must emit. The exact strings are
#: the machine contract: tests assert on them verbatim so CI and
#: downstream automation can branch on them.
EXPECTED_BLOCKERS: dict[str, str] = {
    "init_missing": "stream-json-init-missing",
    "result_missing": "stream-json-result-missing",
    "malformed_line": "stream-json-malformed-line",
    "runtime_version_missing": "stream-json-runtime-version-missing",
    "adapter_version_missing": "stream-json-adapter-version-missing",
    "error_result": "stream-json-error-result",
}

#: Stable warning ids the parser must emit for optional fields.
#: Per ADR 0028, missing usage/tool details are warnings, not
#: blockers; the test pins the exact id so downstream code can
#: filter warnings out of the blocker set.
EXPECTED_WARNINGS: dict[str, str] = {
    "usage_missing": "stream-json-usage-missing",
    "tool_calls_missing": "stream-json-tool-calls-missing",
}


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def stream_json_mod() -> Any:
    """Import the stream-json module; fail cleanly if it is missing.

    Mirrors the red-step pattern used in ``test_preflight_sentinel``
    and ``test_benchmark_loader``: the issue under test has not yet
    landed, so the import raises ``ModuleNotFoundError``; the test
    surfaces that as a clean failure pinned to the missing module
    name (not as an opaque traceback).
    """
    try:
        return importlib.import_module(STREAM_JSON_MODULE)
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"stream-json module {STREAM_JSON_MODULE!r} is not implemented "
            f"yet (Issue #12 red step). Expected at least: "
            f"ADAPTER_VERSION, parse_stream_json. ImportError: {exc}"
        )


def _emit(event: dict[str, Any]) -> str:
    """Encode ``event`` as one line of stream-json.

    Stable key ordering is irrelevant to Claude Code's parser, but
    ``sort_keys=True`` makes the fixtures diff-friendly in test
    failures and in stored replay fixtures (ADR 0028: "CI uses
    recorded replay fixtures").
    """
    return json.dumps(event, sort_keys=True)


def _make_init_event(
    *,
    runtime_version: str | None = RUNTIME_VERSION,
    session_id: str = "sess-001",
) -> dict[str, Any]:
    """Build a realistic ``system/init`` event (Claude Code stream-json)."""
    event: dict[str, Any] = {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "cwd": "/tmp/metacrucible",
        "tools": ["Bash", "Read"],
        "model": "claude-opus-4-1",
        "permissionMode": "default",
    }
    if runtime_version is not None:
        event["claude_code_version"] = runtime_version
    return event


def _make_assistant_event(
    *,
    text: str = "Hello there.",
    session_id: str = "sess-001",
) -> dict[str, Any]:
    """Build a realistic ``assistant`` event with one text block."""
    return {
        "type": "assistant",
        "message": {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-opus-4-1",
            "stop_reason": "end_turn",
        },
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


def _make_user_event(
    prompt: str = "hi",
    *,
    session_id: str = "sess-001",
) -> dict[str, Any]:
    """Build a realistic ``user`` event with a string content payload."""
    return {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


def _make_result_event(
    *,
    result: str = "Hello there.",
    is_error: bool = False,
    subtype: str = "success",
    duration_ms: int = 1234,
    num_turns: int = 1,
    session_id: str = "sess-001",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a realistic ``result`` event.

    ``is_error=True`` plus a non-success ``subtype`` mirrors Claude
    Code's error_max_turns / error result shape.
    """
    event: dict[str, Any] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "duration_ms": duration_ms,
        "num_turns": num_turns,
        "result": result,
        "session_id": session_id,
    }
    if usage is not None:
        event["usage"] = usage
    return event


def _successful_run(
    *,
    final_output: str = "Hello there.",
    with_usage: bool = True,
    with_tool_calls: bool = False,
) -> str:
    """Build the line-delimited JSON for a successful Claude Code run."""
    events: list[dict[str, Any]] = [
        _make_init_event(),
        _make_user_event("hi"),
        _make_assistant_event(text=final_output),
    ]
    usage = (
        {"input_tokens": 11, "output_tokens": 7}
        if with_usage
        else None
    )
    events.append(
        _make_result_event(
            result=final_output,
            usage=usage,
        )
    )
    return "\n".join(_emit(e) for e in events) + "\n"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _blocker_ids(payload: Any) -> list[str]:
    """Return the list of blocker ids in a parse result, or empty if none."""
    if not isinstance(payload, dict):
        return []
    blockers = payload.get("blockers", [])
    if not isinstance(blockers, list):
        return []
    out: list[str] = []
    for blocker in blockers:
        if isinstance(blocker, dict) and isinstance(blocker.get("id"), str):
            out.append(blocker["id"])
    return out


def _warning_ids(payload: Any) -> list[str]:
    """Return the list of warning ids in a parse result, or empty if none."""
    if not isinstance(payload, dict):
        return []
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list):
        return []
    out: list[str] = []
    for warning in warnings:
        if isinstance(warning, dict) and isinstance(warning.get("id"), str):
            out.append(warning["id"])
    return out


# --------------------------------------------------------------------------- #
# Module surface                                                              #
# --------------------------------------------------------------------------- #


def test_stream_json_module_exposes_adapter_version_constant(
    stream_json_mod: Any,
) -> None:
    """The parser must expose the adapter identifier+version as a constant.

    The receipt writer (ADR 0030) reads the adapter version off the
    parser module; it must not have to read it out of a parse
    result. The constant is the pinned string the adapter writes
    into every evidence bundle.
    """
    assert hasattr(stream_json_mod, "ADAPTER_VERSION"), (
        f"{STREAM_JSON_MODULE!r} must expose ADAPTER_VERSION "
        f"(Issue #12 / ADR 0028); got attributes "
        f"{sorted(a for a in dir(stream_json_mod) if not a.startswith('_'))!r}"
    )
    assert isinstance(stream_json_mod.ADAPTER_VERSION, str), (
        f"ADAPTER_VERSION must be a str; got "
        f"{type(stream_json_mod.ADAPTER_VERSION).__name__}"
    )
    # Pinned format: "<runtime-name>/<semver>". The exact string is
    # the receipt-side machine contract.
    assert "/" in stream_json_mod.ADAPTER_VERSION, (
        f"ADAPTER_VERSION must follow '<runtime-name>/<semver>' shape; "
        f"got {stream_json_mod.ADAPTER_VERSION!r}"
    )


def test_stream_json_module_exposes_parse_function(stream_json_mod: Any) -> None:
    """The parser must expose a ``parse_stream_json`` callable."""
    assert hasattr(stream_json_mod, "parse_stream_json"), (
        f"{STREAM_JSON_MODULE!r} must expose parse_stream_json "
        f"(Issue #12); got attributes "
        f"{sorted(a for a in dir(stream_json_mod) if not a.startswith('_'))!r}"
    )
    assert callable(stream_json_mod.parse_stream_json), (
        f"parse_stream_json must be callable; got "
        f"{type(stream_json_mod.parse_stream_json).__name__}"
    )


# --------------------------------------------------------------------------- #
# AC1 — Start / completion captured                                            #
# --------------------------------------------------------------------------- #


def test_parse_stream_json_captures_start_event(stream_json_mod: Any) -> None:
    """AC1: a ``system/init`` event must be reported as start captured.

    The evidence dict carries a ``start_captured`` boolean. The
    adapter uses it to distinguish a run that was wired up (init
    observed) from a run that crashed before the agent runtime
    even initialized (no init event, regardless of exit code).
    """
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert isinstance(evidence, dict), (
        f"parse_stream_json must return a dict; got "
        f"{type(evidence).__name__}"
    )
    assert evidence.get("start_captured") is True, (
        f"a run with a system/init event must report start_captured=True; "
        f"got evidence={evidence!r}"
    )


def test_parse_stream_json_captures_completion_event(stream_json_mod: Any) -> None:
    """AC1: a ``result`` event must be reported as completion captured."""
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("completion_captured") is True, (
        f"a run with a result event must report completion_captured=True; "
        f"got evidence={evidence!r}"
    )


def test_parse_stream_json_missing_init_emits_blocker(stream_json_mod: Any) -> None:
    """AC1: a run with no ``system/init`` event must surface a blocker.

    The adapter classifies a run without an init event as
    BLOCKED-start-missing, so the optimizer pipeline can short-
    circuit rather than treat the missing init as a successful
    run that happened to lack a result.
    """
    text = "\n".join(
        _emit(e)
        for e in [_make_user_event("hi"), _make_assistant_event(), _make_result_event()]
    ) + "\n"
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("start_captured") is False, (
        f"a run with no init event must report start_captured=False; "
        f"got evidence={evidence!r}"
    )
    assert EXPECTED_BLOCKERS["init_missing"] in _blocker_ids(evidence), (
        f"missing init event must emit blocker id "
        f"{EXPECTED_BLOCKERS['init_missing']!r}; got "
        f"blocker_ids={_blocker_ids(evidence)!r}"
    )


def test_parse_stream_json_missing_result_emits_blocker(
    stream_json_mod: Any,
) -> None:
    """AC1: a run with no ``result`` event must surface a blocker."""
    text = "\n".join(
        _emit(e)
        for e in [
            _make_init_event(),
            _make_user_event("hi"),
            _make_assistant_event(),
        ]
    ) + "\n"
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("completion_captured") is False, (
        f"a run with no result event must report completion_captured=False; "
        f"got evidence={evidence!r}"
    )
    assert EXPECTED_BLOCKERS["result_missing"] in _blocker_ids(evidence), (
        f"missing result event must emit blocker id "
        f"{EXPECTED_BLOCKERS['result_missing']!r}; got "
        f"blocker_ids={_blocker_ids(evidence)!r}"
    )


# --------------------------------------------------------------------------- #
# AC2 — Exit code / final output / stderr / error captured                     #
# --------------------------------------------------------------------------- #


def test_parse_stream_json_captures_exit_code(stream_json_mod: Any) -> None:
    """AC2: the parser must surface the exit code supplied by the adapter.

    The adapter runs the subprocess and reads its returncode; the
    parser accepts that as a structured input (``exit_code=``) and
    records it verbatim on the evidence dict. The receipt writer
    (ADR 0030) reads it from there.
    """
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION, exit_code=0
    )
    assert evidence.get("exit_code") == 0, (
        f"exit_code=0 must be surfaced as 0; got "
        f"exit_code={evidence.get('exit_code')!r}"
    )

    evidence_nonzero = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION, exit_code=137
    )
    assert evidence_nonzero.get("exit_code") == 137, (
        f"exit_code=137 must be surfaced as 137; got "
        f"exit_code={evidence_nonzero.get('exit_code')!r}"
    )


def test_parse_stream_json_captures_final_output(stream_json_mod: Any) -> None:
    """AC2: the parser must surface the ``result`` field of the result event.

    The final output is the agent's last assistant text that the
    run completed with. Claude Code puts the canonical final
    output on the ``result`` event under the ``result`` key; the
    parser copies it onto the evidence dict.
    """
    text = _successful_run(final_output="adapter-evidence final answer")
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("final_output") == "adapter-evidence final answer", (
        f"final_output must equal the result event's 'result' field; "
        f"got final_output={evidence.get('final_output')!r}"
    )


def test_parse_stream_json_captures_stderr(stream_json_mod: Any) -> None:
    """AC2: the parser must accumulate the ``stderr`` supplied by the adapter.

    Claude Code writes diagnostic noise (debug lines, sub-tool
    warnings) to stderr. The adapter captures stderr and hands it
    to the parser; the parser records it on the evidence dict.
    """
    text = _successful_run()
    stderr_text = "DEBUG adapter sub-tool warning: skill-not-found\n"
    evidence = stream_json_mod.parse_stream_json(
        text,
        adapter_version=ADAPTER_VERSION,
        stderr=stderr_text,
    )
    assert evidence.get("stderr") == stderr_text, (
        f"stderr must be surfaced verbatim; got "
        f"stderr={evidence.get('stderr')!r}"
    )


def test_parse_stream_json_captures_error_diagnostic(stream_json_mod: Any) -> None:
    """AC2: the parser must surface the error diagnostic for a failed run.

    A failed run is signalled by a ``result`` event with
    ``is_error=True`` (or a non-success ``subtype``). The parser
    pulls the error diagnostic out of the result event and records
    it on the evidence dict so the receipt can quote it.
    """
    events = [
        _make_init_event(),
        _make_user_event("hi"),
        _make_assistant_event(text="partial"),
        _make_result_event(
            result="",
            is_error=True,
            subtype="error_max_turns",
            num_turns=10,
        ),
    ]
    text = "\n".join(_emit(e) for e in events) + "\n"
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("error") not in (None, ""), (
        f"a failed run must surface a non-empty 'error' diagnostic; "
        f"got error={evidence.get('error')!r}"
    )
    assert EXPECTED_BLOCKERS["error_result"] in _blocker_ids(evidence), (
        f"a failed run must emit blocker id "
        f"{EXPECTED_BLOCKERS['error_result']!r}; got "
        f"blocker_ids={_blocker_ids(evidence)!r}"
    )


# --------------------------------------------------------------------------- #
# AC3 — Raw event count                                                       #
# --------------------------------------------------------------------------- #


def test_parse_stream_json_captures_raw_event_count(stream_json_mod: Any) -> None:
    """AC3: the parser must report the count of stream-json events seen.

    The count covers every well-formed JSON object on a line; it
    is the raw event count the adapter pins in the evidence
    bundle, not the count of ``assistant`` or ``result`` events.
    """
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    # The successful fixture has init + user + assistant + result
    # = 4 events.
    assert evidence.get("raw_event_count") == 4, (
        f"raw_event_count must equal 4 for the canonical successful "
        f"fixture; got raw_event_count={evidence.get('raw_event_count')!r}"
    )


def test_parse_stream_json_raw_event_count_excludes_malformed_lines(
    stream_json_mod: Any,
) -> None:
    """AC3: the raw event count counts JSON events, not stderr noise.

    Non-JSON lines are diagnostics (sub-tool warnings, debug
    output) that the adapter captures as stderr. They must not
    inflate the raw event count: the count is the count of
    well-formed JSON objects the parser was able to read.
    """
    text = (
        _emit(_make_init_event())
        + "\n"
        + "this is not JSON and came in on stdout by mistake\n"
        + _emit(_make_user_event("hi"))
        + "\n"
        + "{not even close to json\n"
        + _emit(_make_assistant_event())
        + "\n"
        + _emit(_make_result_event())
        + "\n"
    )
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    # 4 well-formed events; the two malformed lines must not count.
    assert evidence.get("raw_event_count") == 4, (
        f"raw_event_count must count only well-formed JSON events; "
        f"got raw_event_count={evidence.get('raw_event_count')!r} "
        f"(malformed lines must not be counted)"
    )


# --------------------------------------------------------------------------- #
# AC4 — Adapter / runtime versions                                            #
# --------------------------------------------------------------------------- #


def test_parse_stream_json_captures_adapter_version(stream_json_mod: Any) -> None:
    """AC4: the parser must record the adapter version supplied by the caller.

    The adapter version is the identifier+version of this parser
    (e.g. ``claude-code/0.4.1``). It is the value the caller passes
    in; the parser records it on the evidence dict.
    """
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("adapter_version") == ADAPTER_VERSION, (
        f"adapter_version must equal the caller-supplied value; got "
        f"adapter_version={evidence.get('adapter_version')!r}"
    )


def test_parse_stream_json_captures_runtime_version_from_init(
    stream_json_mod: Any,
) -> None:
    """AC4: the parser must read the runtime version out of the init event.

    Claude Code writes its version into the ``system/init`` event
    under the ``claude_code_version`` key. The parser surfaces it
    on the evidence dict; the receipt writer reads it from there.
    """
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("claude_code_version") == RUNTIME_VERSION, (
        f"claude_code_version must be read from the init event; got "
        f"claude_code_version={evidence.get('claude_code_version')!r}"
    )


def test_parse_stream_json_runtime_version_override_is_recorded(
    stream_json_mod: Any,
) -> None:
    """AC4: caller-supplied runtime version takes precedence over init event.

    When the adapter is pinned to a different Claude Code build
    than the one the init event advertises (downgrade, controlled
    experiment), the caller's override wins. The parser must
    surface the caller-supplied value and the override must not be
    lost in the init event's value.
    """
    text = _emit(_make_init_event(runtime_version=RUNTIME_VERSION)) + "\n"
    evidence = stream_json_mod.parse_stream_json(
        text,
        adapter_version=ADAPTER_VERSION,
        claude_code_version="1.2.3-experiment",
    )
    assert evidence.get("claude_code_version") == "1.2.3-experiment", (
        f"caller-supplied claude_code_version must override the init "
        f"event value; got "
        f"claude_code_version={evidence.get('claude_code_version')!r}"
    )


def test_parse_stream_json_missing_runtime_version_emits_blocker(
    stream_json_mod: Any,
) -> None:
    """AC4: missing runtime version must block; the adapter needs it.

    Without a runtime version the receipt cannot pin which Claude
    Code build produced the run, so the evidence is incomplete and
    the run is blocked. The blocker id is machine-stable so the
    adapter can branch on it.
    """
    text = (
        _emit(_make_init_event(runtime_version=None))
        + "\n"
        + _emit(_make_result_event())
        + "\n"
    )
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("claude_code_version") in (None, ""), (
        f"a run whose init has no claude_code_version and no override "
        f"must not invent one; got "
        f"claude_code_version={evidence.get('claude_code_version')!r}"
    )
    assert EXPECTED_BLOCKERS["runtime_version_missing"] in _blocker_ids(
        evidence
    ), (
        f"missing runtime version must emit blocker id "
        f"{EXPECTED_BLOCKERS['runtime_version_missing']!r}; got "
        f"blocker_ids={_blocker_ids(evidence)!r}"
    )


def test_parse_stream_json_missing_adapter_version_emits_blocker(
    stream_json_mod: Any,
) -> None:
    """AC4: missing adapter version (caller forgot) must block.

    The parser cannot invent its own adapter version; if the
    caller omits it, the evidence is incomplete and the run is
    blocked. The blocker id is machine-stable.
    """
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(text, adapter_version="")
    assert evidence.get("adapter_version") in (None, ""), (
        f"missing adapter_version must not be invented; got "
        f"adapter_version={evidence.get('adapter_version')!r}"
    )
    assert EXPECTED_BLOCKERS["adapter_version_missing"] in _blocker_ids(
        evidence
    ), (
        f"missing adapter_version must emit blocker id "
        f"{EXPECTED_BLOCKERS['adapter_version_missing']!r}; got "
        f"blocker_ids={_blocker_ids(evidence)!r}"
    )


# --------------------------------------------------------------------------- #
# Non-JSON stderr / noise line handling                                       #
# --------------------------------------------------------------------------- #


def test_parse_stream_json_malformed_lines_are_warnings_not_blockers_for_pure_diagnostics(
    stream_json_mod: Any,
) -> None:
    """Non-JSON diagnostic lines surface as a warning when the run still completes.

    Claude Code may emit a debug line on stdout between events
    (older versions did; some sub-tools still do). The parser must
    not crash and must not block the run solely on noise; the
    run is still classified by the init / result pair. The parser
    records the malformed lines as a warning (or, in the
    single-line case, as a blocker) — but the *run*'s evidence is
    populated from the well-formed events.

    Two malformed lines is the threshold that flips the warning
    into a blocker (the parser is now looking at noisy output that
    may be hiding real events). The single-line case records a
    warning so a single stray debug print does not block the run.
    """
    text = (
        _emit(_make_init_event())
        + "\n"
        + "single stray debug line\n"
        + _emit(_make_user_event("hi"))
        + "\n"
        + _emit(_make_assistant_event())
        + "\n"
        + _emit(_make_result_event())
        + "\n"
    )
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    # The well-formed run evidence is still populated.
    assert evidence.get("start_captured") is True, (
        f"noise between events must not lose the init event; got "
        f"start_captured={evidence.get('start_captured')!r}"
    )
    assert evidence.get("completion_captured") is True, (
        f"noise between events must not lose the result event; got "
        f"completion_captured={evidence.get('completion_captured')!r}"
    )


def test_parse_stream_json_malformed_lines_do_not_crash(
    stream_json_mod: Any,
) -> None:
    """The parser must not raise on non-JSON lines; it must capture and continue.

    A run that produced any non-JSON stdout lines (mixed-in
    diagnostics) is still a run the adapter can classify. The
    parser returns an evidence dict; it does not propagate an
    exception to the caller.
    """
    text = (
        _emit(_make_init_event())
        + "\n"
        + "{not even close to json\n"
        + "another stray line\n"
        + _emit(_make_result_event())
        + "\n"
    )
    # Must not raise. If the implementation crashes on a non-JSON
    # line, this test fails with the original exception (not a
    # clean assertion message), which is the right signal: the
    # parser is not robust against mixed stdout.
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert isinstance(evidence, dict), (
        f"parse_stream_json must return a dict even on mixed "
        f"stdout/stderr; got {type(evidence).__name__}"
    )


# --------------------------------------------------------------------------- #
# Issue #27 task 27.3: JSONL logs are diagnostics, not evidence               #
# --------------------------------------------------------------------------- #


def test_parse_stream_json_clean_run_reports_zero_malformed_line_count(
    stream_json_mod: Any,
) -> None:
    """A well-formed run must report ``malformed_line_count == 0``.

    The field is the diagnostic counterpart of ``raw_event_count``:
    a clean run that produced only JSON events has zero
    diagnostics. Exposing the count makes the "JSONL is
    diagnostics, not evidence" contract observable; the count
    is part of the evidence dict, not a hidden internal.
    """
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert "malformed_line_count" in evidence, (
        f"evidence dict must expose malformed_line_count (Issue #27 "
        f"task 27.3); got keys {sorted(evidence.keys())!r}"
    )
    assert evidence["malformed_line_count"] == 0, (
        f"a clean run must report zero diagnostics; got "
        f"malformed_line_count={evidence.get('malformed_line_count')!r}"
    )


def test_parse_stream_json_malformed_lines_increment_diagnostic_count_only(
    stream_json_mod: Any,
) -> None:
    """Non-JSON lines must show up in ``malformed_line_count`` and
    MUST NOT show up in ``raw_event_count``.

    This is the contract: malformed lines are *diagnostics*. The
    count is exposed so callers can see how much noise was
    absorbed; the well-formed event count is independent and is
    the only count that reflects the run's evidence. A non-JSON
    line that the parser absorbed must add 1 to
    ``malformed_line_count`` and 0 to ``raw_event_count``.
    """
    text = (
        _emit(_make_init_event())
        + "\n"
        + "first stray debug line\n"
        + _emit(_make_user_event("hi"))
        + "\n"
        + "second stray debug line\n"
        + _emit(_make_assistant_event())
        + "\n"
        + "third stray debug line\n"
        + _emit(_make_result_event())
        + "\n"
    )
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    # Three non-JSON lines were absorbed.
    assert evidence.get("malformed_line_count") == 3, (
        f"three non-JSON lines must show as three diagnostics; got "
        f"malformed_line_count={evidence.get('malformed_line_count')!r}"
    )
    # Four well-formed events: init + user + assistant + result.
    assert evidence.get("raw_event_count") == 4, (
        f"raw_event_count must NOT count non-JSON lines; got "
        f"raw_event_count={evidence.get('raw_event_count')!r}"
    )


def test_parse_stream_json_single_stray_diagnostic_does_not_block(
    stream_json_mod: Any,
) -> None:
    """A single non-JSON line is a diagnostic, not a blocker.

    The threshold is the classifier: at the threshold the output
    is too noisy to trust; below it, the run still classifies
    from the well-formed events. A single stray debug line is
    below threshold; the run must still report start and
    completion and must not emit the
    ``stream-json-malformed-line`` blocker.
    """
    text = (
        _emit(_make_init_event())
        + "\n"
        + "single stray debug line\n"
        + _emit(_make_user_event("hi"))
        + "\n"
        + _emit(_make_assistant_event())
        + "\n"
        + _emit(_make_result_event())
        + "\n"
    )
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    # The diagnostic is recorded...
    assert evidence.get("malformed_line_count") == 1, (
        f"a single non-JSON line must increment the diagnostic count; "
        f"got malformed_line_count={evidence.get('malformed_line_count')!r}"
    )
    # ...but the run still classifies from the well-formed events.
    assert evidence.get("start_captured") is True, (
        f"a single stray line must not lose the init event; got "
        f"start_captured={evidence.get('start_captured')!r}"
    )
    assert evidence.get("completion_captured") is True, (
        f"a single stray line must not lose the result event; got "
        f"completion_captured={evidence.get('completion_captured')!r}"
    )
    # ...and is below the noise threshold, so no blocker.
    assert EXPECTED_BLOCKERS["malformed_line"] not in _blocker_ids(evidence), (
        f"a single stray line is below the noise threshold and must "
        f"not emit the malformed-line blocker; got "
        f"blocker_ids={_blocker_ids(evidence)!r}"
    )


def test_parse_stream_json_threshold_malformed_lines_classifies_run(
    stream_json_mod: Any,
) -> None:
    """At/above the noise threshold the parser emits a classifier
    blocker; the run's classification is still observable.

    The blocker is a *classification* signal — the output is too
    noisy to trust — not a claim that the stream-json log is
    authoritative evidence. The well-formed fields are still
    populated from the events the parser did manage to read; the
    caller (the receipt writer) branches on the blocker id, not
    on the stream-json log content.
    """
    text = (
        _emit(_make_init_event())
        + "\n"
        + "stray line one\n"
        + "stray line two\n"
        + _emit(_make_user_event("hi"))
        + "\n"
        + _emit(_make_assistant_event())
        + "\n"
        + _emit(_make_result_event())
        + "\n"
    )
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    # The diagnostic count is the number of non-JSON lines.
    assert evidence.get("malformed_line_count") == 2, (
        f"two non-JSON lines must show as two diagnostics; got "
        f"malformed_line_count={evidence.get('malformed_line_count')!r}"
    )
    # The well-formed fields are still populated — init/result
    # were observed, so the run can still be classified.
    assert evidence.get("start_captured") is True, (
        f"the init event is well-formed and must still be captured; "
        f"got start_captured={evidence.get('start_captured')!r}"
    )
    assert evidence.get("completion_captured") is True, (
        f"the result event is well-formed and must still be "
        f"captured; got "
        f"completion_captured={evidence.get('completion_captured')!r}"
    )
    # The blocker is present. The message is a classification
    # signal, not a claim that the stream-json log is authoritative.
    blockers = _blocker_ids(evidence)
    assert EXPECTED_BLOCKERS["malformed_line"] in blockers, (
        f"at-threshold noise must emit the malformed-line blocker; "
        f"got blocker_ids={blockers!r}"
    )
    # Pull the actual blocker entry and assert the message is
    # the "too noisy to classify" classification signal, not
    # an authoritative-evidence claim.
    msg = next(
        b["message"]
        for b in evidence["blockers"]
        if b.get("id") == EXPECTED_BLOCKERS["malformed_line"]
    )
    assert "classify" in msg, (
        f"malformed-line blocker message must describe the output as "
        f"too noisy to classify (a classification signal, not an "
        f"authoritative-evidence claim); got {msg!r}"
    )


def test_parse_stream_json_diagnostic_lines_do_not_appear_in_blockers_below_threshold(
    stream_json_mod: Any,
) -> None:
    """A run with diagnostic lines but a clean init/result pair
    must not list the diagnostic as a blocker.

    The diagnostic is recorded on ``malformed_line_count`` and
    visible to the caller; the receipt / summary / trajectory
    digest are populated from the well-formed events. Diagnostic
    noise is not a blocker below the threshold; the run
    classifies as "completed with noise" rather than "blocked
    by noise".
    """
    text = (
        _emit(_make_init_event())
        + "\n"
        + "stray debug line\n"
        + _emit(_make_user_event("hi"))
        + "\n"
        + _emit(_make_assistant_event())
        + "\n"
        + _emit(_make_result_event())
        + "\n"
    )
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert evidence.get("malformed_line_count") == 1
    assert evidence.get("start_captured") is True
    assert evidence.get("completion_captured") is True
    # The only non-trivial blocker ids are the ones that reflect
    # missing init/result/runner-version. The diagnostic noise
    # does not contribute a blocker entry.
    blocker_id_set = set(_blocker_ids(evidence))
    assert EXPECTED_BLOCKERS["malformed_line"] not in blocker_id_set, (
        f"below-threshold diagnostic noise must not appear as a "
        f"blocker; got blocker_ids={blocker_id_set!r}"
    )


# --------------------------------------------------------------------------- #
# Optional-field handling: warnings, not blockers (ADR 0028)                   #
# --------------------------------------------------------------------------- #


def test_parse_stream_json_missing_usage_is_a_warning_not_a_blocker(
    stream_json_mod: Any,
) -> None:
    """ADR 0028: missing usage details are a warning, not a blocker."""
    text = _successful_run(with_usage=False)
    evidence = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert EXPECTED_WARNINGS["usage_missing"] in _warning_ids(evidence), (
        f"missing usage details must emit warning id "
        f"{EXPECTED_WARNINGS['usage_missing']!r}; got "
        f"warning_ids={_warning_ids(evidence)!r}"
    )
    assert EXPECTED_WARNINGS["usage_missing"] not in _blocker_ids(evidence), (
        f"missing usage details must NOT emit a blocker; got "
        f"blocker_ids={_blocker_ids(evidence)!r}"
    )


def test_parse_stream_json_full_successful_run_has_no_blockers(
    stream_json_mod: Any,
) -> None:
    """A clean, well-formed successful run must produce zero blockers.

    All ACs are satisfied (start, completion, exit code, final
    output, stderr, event count, adapter version, runtime
    version); the parser must not invent blockers on a clean run.
    """
    text = _successful_run()
    evidence = stream_json_mod.parse_stream_json(
        text,
        adapter_version=ADAPTER_VERSION,
        stderr="",
        exit_code=0,
    )
    assert _blocker_ids(evidence) == [], (
        f"a clean successful run must not produce blockers; got "
        f"blocker_ids={_blocker_ids(evidence)!r}"
    )


# --------------------------------------------------------------------------- #
# Input-shape flexibility                                                      #
# --------------------------------------------------------------------------- #


def test_parse_stream_json_accepts_iterable_of_lines(
    stream_json_mod: Any,
) -> None:
    """The parser must accept an iterable of lines, not just a str.

    The adapter feeds the parser an iterator over subprocess
    stdout lines, not a single joined string, so it can stream
    long runs without buffering. The parser must accept both
    shapes; the evidence shape is identical.
    """
    text = _successful_run()
    lines: Iterable[str] = text.splitlines()
    evidence_iter = stream_json_mod.parse_stream_json(
        lines, adapter_version=ADAPTER_VERSION
    )
    evidence_str = stream_json_mod.parse_stream_json(
        text, adapter_version=ADAPTER_VERSION
    )
    assert isinstance(evidence_iter, dict)
    # The observable fields the AC cares about must match across
    # the two input shapes.
    for key in (
        "start_captured",
        "completion_captured",
        "raw_event_count",
        "final_output",
        "adapter_version",
        "claude_code_version",
    ):
        assert evidence_iter.get(key) == evidence_str.get(key), (
            f"line-iterable and str input must produce identical "
            f"evidence for {key!r}; got {evidence_iter.get(key)!r} vs "
            f"{evidence_str.get(key)!r}"
        )
