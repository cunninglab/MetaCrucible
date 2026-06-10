"""Claude Code ``--output-format stream-json`` parser (Issue #12 / ADR 0028).

The runtime adapter (ADR 0028) runs Claude Code in ``--bare`` mode and
captures its line-delimited ``stream-json`` stdout. This module turns
that raw output into the evidence dict the receipt writer (ADR 0030)
pins into every evidence bundle. The evidence carries the minimum
fields required to classify a run:

  - start / completion: a ``system/init`` event and a ``result`` event
    were observed.
  - exit code / final output / stderr / error diagnostic: the inputs
    the adapter hands in plus the failure signal Claude Code writes
    onto the ``result`` event (``is_error=True`` or a non-success
    ``subtype``).
  - raw event count: the count of well-formed JSON objects on stdout.
  - adapter version / runtime version: the pinned versions downstream
    automation branches on verbatim.

Missing fields surface as machine-stable blocker ids; missing
optional fields (usage / tool calls) surface as warning ids per
ADR 0028 ("usage and tool-call details are recorded when present but
missing values are warnings rather than blockers").

The parser is robust against mixed stdout: non-JSON lines are
classified as diagnostics (sub-tool warnings, debug output) and
counted separately so they do not inflate the raw event count. The
count of malformed lines is exposed on the evidence dict as
``malformed_line_count`` so callers can see how much noise was
absorbed; it is a diagnostic, never a contribution to evidence.

Diagnostic, not evidence source of truth (ADR 0035)
---------------------------------------------------

The stream-json parser is the adapter-side producer of evidence
fields. Non-JSON lines are **diagnostics**: they are counted, they
may produce a blocker when the run is too noisy to classify, and
they are surfaced on the evidence dict for the caller's own
diagnostics — but they do **not** contribute to the receipt,
summary, or trajectory digest. The receipt / summary / trajectory
digest writers (ADR 0030) read only the well-formed fields on the
evidence dict; they never open or parse the raw stream-json log
themselves. Optional ``event_log_refs`` on a receipt are opaque
sibling-relative refs supplied by the caller, not parsed by
MetaCrucible for truth.

When the number of malformed lines exceeds a small threshold the
parser emits a blocker. The blocker is a *classification* signal:
"this output is too noisy to classify", not a claim that the
stream-json log is authoritative evidence.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Union

__all__ = ["ADAPTER_VERSION", "parse_stream_json"]


# --------------------------------------------------------------------------- #
# Adapter contract                                                            #
# --------------------------------------------------------------------------- #

#: Stable adapter identifier+version. The receipt writer (ADR 0030)
#: reads the adapter version off this constant rather than off a
#: parse result; the literal string is the pinned machine contract.
ADAPTER_VERSION: str = "claude-code/0.4.1"

#: Stable blocker ids. The strings are the machine contract; the
#: receipt writer and optimizer pipeline branch on them verbatim.
_BLOCKER_INIT_MISSING: str = "stream-json-init-missing"
_BLOCKER_RESULT_MISSING: str = "stream-json-result-missing"
_BLOCKER_MALFORMED_LINE: str = "stream-json-malformed-line"
_BLOCKER_RUNTIME_VERSION_MISSING: str = "stream-json-runtime-version-missing"
_BLOCKER_ADAPTER_VERSION_MISSING: str = "stream-json-adapter-version-missing"
_BLOCKER_ERROR_RESULT: str = "stream-json-error-result"

#: Stable warning ids. Per ADR 0028, missing usage / tool-call
#: details are warnings, not blockers; the ids let downstream code
#: filter the warning set out of the blocker set.
_WARNING_USAGE_MISSING: str = "stream-json-usage-missing"
_WARNING_TOOL_CALLS_MISSING: str = "stream-json-tool-calls-missing"

#: Threshold above which malformed lines flip the parser from
#: "diagnostic noise" to "blocker" — the output may now be hiding
#: real events behind the noise. A single stray debug line is below
#: the threshold and is silently absorbed.
_MALFORMED_LINE_BLOCKER_THRESHOLD: int = 2

#: Type alias for the accepted input shape. The adapter feeds either
#: a joined string (replay fixtures) or an iterator of lines (live
#: subprocess stdout); the parser treats them identically.
StreamInput = Union[str, Iterable[str]]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _iter_lines(text_or_lines: StreamInput) -> Iterable[str]:
    """Yield the input as a sequence of lines, regardless of input shape."""
    if isinstance(text_or_lines, str):
        return text_or_lines.splitlines()
    return text_or_lines


def _is_init_event(event: dict[str, Any]) -> bool:
    """A ``system/init`` event signals that the runtime initialised."""
    return event.get("type") == "system" and event.get("subtype") == "init"


def _is_result_event(event: dict[str, Any]) -> bool:
    """A ``result`` event is the canonical completion signal."""
    return event.get("type") == "result"


def _has_tool_use(event: dict[str, Any]) -> bool:
    """Return True iff ``event`` carries a ``tool_use`` content block."""
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in content
    )


def _error_diagnostic(result_event: dict[str, Any]) -> str:
    """Build the human ``error`` field from a failed ``result`` event."""
    subtype = result_event.get("subtype")
    if isinstance(subtype, str) and subtype and subtype != "success":
        return f"claude-code result subtype: {subtype}"
    return "claude-code result marked is_error"


# --------------------------------------------------------------------------- #
# Parser                                                                      #
# --------------------------------------------------------------------------- #


def parse_stream_json(
    text_or_lines: StreamInput,
    *,
    adapter_version: str = "",
    claude_code_version: str | None = None,
    exit_code: int = 0,
    stderr: str = "",
) -> dict[str, Any]:
    """Parse Claude Code ``stream-json`` output into an evidence dict.

    The runtime adapter (ADR 0028) feeds this parser either the joined
    stdout of a completed run (replay fixtures) or the line iterator
    of a live subprocess. The parser never raises on a non-JSON line:
    such lines are classified as diagnostics and counted separately
    so the raw event count reflects only well-formed JSON objects.

    Parameters
    ----------
    text_or_lines
        Either a single ``str`` of line-delimited JSON (replay
        fixtures) or an ``Iterable[str]`` of lines (live runs).
    adapter_version
        Caller-supplied adapter identifier+version (e.g.
        ``"claude-code/0.4.1"``). Empty / missing → blocker
        ``stream-json-adapter-version-missing``.
    claude_code_version
        Optional override for the Claude Code runtime version. When
        supplied, takes precedence over the ``claude_code_version``
        field on the ``system/init`` event. Used for downgrades and
        controlled experiments. Missing on both sides → blocker
        ``stream-json-runtime-version-missing``.
    exit_code
        Subprocess returncode read by the adapter. Surfaced verbatim.
    stderr
        Captured stderr text. Surfaced verbatim.

    Returns
    -------
    dict
        Evidence dict carrying:

        - ``start_captured`` / ``completion_captured`` (bool)
        - ``raw_event_count`` (int) — well-formed JSON events only
        - ``malformed_line_count`` (int) — non-JSON lines absorbed as
          diagnostics; they do not contribute to ``raw_event_count``
          and are never used as evidence. The count is exposed so
          callers (logs, dashboards) can see how much noise was
          absorbed; it is not a classification signal in itself.
        - ``final_output`` (str | None) — ``result.result`` field
        - ``exit_code`` (int) — caller-supplied
        - ``stderr`` (str) — caller-supplied
        - ``error`` (str | None) — non-empty when the run failed
        - ``adapter_version`` (str | None)
        - ``claude_code_version`` (str | None)
        - ``blockers`` (list[dict]) — ``{"id", "message"}`` items
        - ``warnings`` (list[dict]) — ``{"id", "message"}`` items
    """
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    start_captured = False
    completion_captured = False
    raw_event_count = 0
    malformed_line_count = 0
    final_output: str | None = None
    error: str | None = None
    seen_tool_use = False
    init_runtime_version: str | None = None
    result_event: dict[str, Any] | None = None

    for line in _iter_lines(text_or_lines):
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            malformed_line_count += 1
            continue
        if not isinstance(event, dict):
            malformed_line_count += 1
            continue
        raw_event_count += 1

        if _is_init_event(event):
            start_captured = True
            runtime_version = event.get("claude_code_version")
            if isinstance(runtime_version, str) and runtime_version:
                init_runtime_version = runtime_version
        elif _is_result_event(event):
            completion_captured = True
            result_event = event
            result_text = event.get("result")
            if isinstance(result_text, str):
                final_output = result_text
            subtype = event.get("subtype")
            is_error = bool(event.get("is_error"))
            subtype_signals_error = (
                isinstance(subtype, str) and subtype != "success"
            )
            if is_error or subtype_signals_error:
                error = _error_diagnostic(event)

        if _has_tool_use(event):
            seen_tool_use = True

    # Resolve effective runtime version: caller override > init event.
    if isinstance(claude_code_version, str) and claude_code_version:
        effective_runtime_version: str | None = claude_code_version
    elif init_runtime_version:
        effective_runtime_version = init_runtime_version
    else:
        effective_runtime_version = None

    # Build blocker list (machine-stable ids; do not rename without
    # an ADR — the receipt writer branches on the exact strings).
    if not start_captured:
        blockers.append(
            {
                "id": _BLOCKER_INIT_MISSING,
                "message": "no system/init event found in stream-json output",
            }
        )
    if not completion_captured:
        blockers.append(
            {
                "id": _BLOCKER_RESULT_MISSING,
                "message": "no result event found in stream-json output",
            }
        )
    if malformed_line_count >= _MALFORMED_LINE_BLOCKER_THRESHOLD:
        blockers.append(
            {
                "id": _BLOCKER_MALFORMED_LINE,
                "message": (
                    f"{malformed_line_count} non-JSON line(s) found in "
                    "stream-json output; output is too noisy to classify"
                ),
            }
        )
    if effective_runtime_version is None:
        blockers.append(
            {
                "id": _BLOCKER_RUNTIME_VERSION_MISSING,
                "message": (
                    "no claude_code_version in init event and no "
                    "caller override supplied"
                ),
            }
        )
    if not (isinstance(adapter_version, str) and adapter_version):
        blockers.append(
            {
                "id": _BLOCKER_ADAPTER_VERSION_MISSING,
                "message": "caller did not supply adapter_version",
            }
        )
    if result_event is not None and error is not None:
        blockers.append(
            {
                "id": _BLOCKER_ERROR_RESULT,
                "message": error,
            }
        )

    # Build warning list (ADR 0028: optional fields are warnings, not
    # blockers, so the receipt can still classify the run).
    if completion_captured and result_event is not None:
        if not isinstance(result_event.get("usage"), dict):
            warnings.append(
                {
                    "id": _WARNING_USAGE_MISSING,
                    "message": "result event did not include a usage block",
                }
            )
        if not seen_tool_use:
            warnings.append(
                {
                    "id": _WARNING_TOOL_CALLS_MISSING,
                    "message": "no tool_use blocks found across assistant events",
                }
            )

    return {
        "start_captured": start_captured,
        "completion_captured": completion_captured,
        "raw_event_count": raw_event_count,
        "malformed_line_count": malformed_line_count,
        "final_output": final_output,
        "exit_code": exit_code,
        "stderr": stderr,
        "error": error,
        "adapter_version": adapter_version if adapter_version else None,
        "claude_code_version": effective_runtime_version,
        "blockers": blockers,
        "warnings": warnings,
    }
