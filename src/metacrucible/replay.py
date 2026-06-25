"""Recorded-replay CI harness loader (Issue #45, ADR 0021, ADR 0036).

The replay harness lets CI exercise the full judge / optimizer orchestration
without contacting a live LLM. A replay fixture is a JSONL file in which
each line is a JSON object with a stable schema::

    {"schema_version": 1, "name": "judge_1", "response": <any JSON value>}
    {"schema_version": 1, "name": "judge_2", "response": <any JSON value>}
    {"schema_version": 1, "name": "optimizer", "responses": [<v1>, <v2>, ...]}

Each entry is a queue of recorded responses consumed in declared order.
:func:`build_judge_call_fns` and :func:`build_optimizer_call_fn` return
callables shaped to slot into the existing
:func:`metacrucible.provider_config.call_structured` /
:func:`metacrucible.provider_config.run_judge_evaluator` /
:func:`metacrucible.optimizer.run_optimizer_pipeline` plumbing, so a
replay-backed run is wired the same way as a live run.

The loader is intentionally pure: it does not import provider SDKs, does
not read API-key environment variables, does not open network sockets,
and does not sleep. The secret scan in :func:`scan_replay_text` /
:func:`load_replay` is the only validation step that looks at fixture
content (raw text + decoded string fields); ADR 0036 forbids real
secrets in fixtures and the loader rejects them with a stable
``secret_detected`` code.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "REPLAY_SCHEMA_VERSION",
    "Replay",
    "ReplayError",
    "ReplayExhausted",
    "build_judge_call_fns",
    "build_optimizer_call_fn",
    "load_replay",
    "scan_replay_text",
]

#: Pinned replay fixture schema version. Bumping this is a contract
#: change: existing fixtures with a different version are rejected
#: with :data:`ReplayError.code == "schema_mismatch"`.
REPLAY_SCHEMA_VERSION: int = 1

#: Error codes emitted on the :attr:`ReplayError.code` attribute.
#: Callers and tests branch on these values verbatim; renaming a
#: code is a contract change.
CODE_SCHEMA_MISMATCH: str = "schema_mismatch"
CODE_DUPLICATE_NAME: str = "duplicate_name"
CODE_MALFORMED_JSON: str = "malformed_json"
CODE_MISSING_FIELD: str = "missing_field"
CODE_BOTH_RESPONSE_SHAPES: str = "both_response_shapes"
CODE_EMPTY_RESPONSES: str = "empty_responses"
CODE_SECRET_DETECTED: str = "secret_detected"
CODE_MISSING_ENTRY: str = "missing_entry"

#: High-confidence secret patterns the replay loader rejects (ADR
#: 0036: fixtures must not contain real secrets). The set is the
#: subset of :data:`metacrucible.profiles._SECRET_PRIVACY_RISK_PATTERNS`
#: that the replay contract pins; inlining here keeps :mod:`profiles`
#: unchanged and lets the replay contract evolve independently of
#: the profile content hash.
_SECRET_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # AWS access key id; canonical 20-character ``AKIA[0-9A-Z]{16}``.
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    # GitHub personal access token (classic); 36 chars after ``ghp_``.
    ("github-personal-access-token", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    # Stripe live secret key; the ``sk_live_`` prefix flags a
    # production (non-test) secret.
    ("stripe-live-secret-key", re.compile(r"sk_live_[A-Za-z0-9]{24,}")),
)

# Decoded-string matches are reported on this sentinel line number
# because JSON decoding does not preserve a source line for a string
# value that was constructed (e.g. via \uXXXX escapes). The raw-text
# scan is the primary line-attributed source; the decoded scan is
# defense in depth.
_DECODED_LINE: int = 0


class ReplayError(ValueError):
    """Raised when a recorded-replay fixture is invalid or exhausted.

    The :attr:`code` attribute carries a stable machine identifier
    that callers and tests branch on verbatim. Rejections raised by
    :func:`load_replay` populate :attr:`pattern_id` and
    :attr:`line_number` for ``secret_detected`` and most structural
    errors; rejections raised by :class:`ReplayExhausted` populate
    :attr:`entry_name` and :attr:`call_index`.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        pattern_id: str | None = None,
        line_number: int | None = None,
        entry_name: str | None = None,
        call_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.pattern_id: str | None = pattern_id
        self.line_number: int | None = line_number
        self.entry_name: str | None = entry_name
        self.call_index: int | None = call_index

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"ReplayError(code={self.code!r}, "
            f"pattern_id={self.pattern_id!r}, "
            f"line_number={self.line_number!r}, "
            f"entry_name={self.entry_name!r}, "
            f"call_index={self.call_index!r})"
        )


class ReplayExhausted(ReplayError):
    """Raised when a replay entry has no more recorded responses.

    Distinct from :class:`ReplayError` so callers can branch on
    ``isinstance(exc, ReplayExhausted)`` to detect over-calls without
    parsing the message.
    """

    def __init__(self, entry_name: str, call_index: int) -> None:
        super().__init__(
            "replay_exhausted",
            (
                f"replay entry {entry_name!r} exhausted at call index "
                f"{call_index}; no more recorded responses available"
            ),
            entry_name=entry_name,
            call_index=call_index,
        )


class Replay:
    """A loaded recorded-replay fixture.

    Entries are queues of recorded responses consumed in declared
    order. :attr:`entry_names` exposes the declared name order so
    tests and CLI can iterate deterministically without re-parsing
    the source file.
    """

    def __init__(self, entry_queues: dict[str, list[Any]]) -> None:
        if not entry_queues:
            raise ReplayError(CODE_MISSING_FIELD, "replay has no entries")
        # Snapshot each queue so a caller mutating the input mapping
        # after construction cannot corrupt replay state.
        self._queues: dict[str, list[Any]] = {
            name: list(queue) for name, queue in entry_queues.items()
        }
        self._call_counts: dict[str, int] = {
            name: 0 for name in self._queues
        }
        self._entry_order: tuple[str, ...] = tuple(self._queues.keys())

    @property
    def entry_names(self) -> tuple[str, ...]:
        """Return the declared entry names in fixture order."""
        return self._entry_order

    def take(self, name: str) -> Any:
        """Consume and return the next recorded response for ``name``.

        Raises :class:`ReplayExhausted` when the entry's queue is
        empty, or :class:`ReplayError` with
        :data:`CODE_MISSING_ENTRY` when the entry is unknown.
        """
        if name not in self._queues:
            raise ReplayError(
                CODE_MISSING_ENTRY,
                f"replay has no entry named {name!r}",
                entry_name=name,
            )
        index = self._call_counts[name]
        queue = self._queues[name]
        if index >= len(queue):
            raise ReplayExhausted(name, index)
        value = queue[index]
        self._call_counts[name] = index + 1
        return value

    def take_many(self, name: str) -> list[Any]:
        """Consume and return all remaining recorded responses for ``name``.

        Raises :class:`ReplayExhausted` when the entry's queue is
        already empty, or :class:`ReplayError` with
        :data:`CODE_MISSING_ENTRY` when the entry is unknown.
        """
        if name not in self._queues:
            raise ReplayError(
                CODE_MISSING_ENTRY,
                f"replay has no entry named {name!r}",
                entry_name=name,
            )
        index = self._call_counts[name]
        queue = self._queues[name]
        remaining = queue[index:]
        if not remaining:
            raise ReplayExhausted(name, index)
        self._call_counts[name] = len(queue)
        return list(remaining)


def scan_replay_text(text: str) -> list[tuple[str, int]]:
    """Return ``[(pattern_id, line_number), ...]`` for every secret match.

    ``line_number`` is 1-indexed and points to the line on which the
    match starts. A pattern may match more than once on the same
    line; each match is reported in declaration order. The scan is
    pure (no I/O, no environment access) so tests can assert exact
    line attribution.
    """
    results: list[tuple[str, int]] = []
    for line_idx, line in enumerate(text.splitlines(), start=1):
        for pattern_id, regex in _SECRET_PATTERNS:
            if regex.search(line):
                results.append((pattern_id, line_idx))
    return results


def _scan_decoded(record: Any) -> list[tuple[str, int]]:
    """Recursively scan a decoded JSON record's string fields.

    Returned matches use :data:`_DECODED_LINE` (0) for the line
    number because decoded string values cannot be attributed to a
    specific source line. The raw-text scan is the primary
    line-attributed source; this scan is defense in depth for
    JSON-escaped secrets (e.g. ``\\u0041KIA...``).
    """
    results: list[tuple[str, int]] = []
    _scan_decoded_into(record, results)
    return results


def _scan_decoded_into(record: Any, results: list[tuple[str, int]]) -> None:
    if isinstance(record, str):
        for pattern_id, regex in _SECRET_PATTERNS:
            if regex.search(record):
                results.append((pattern_id, _DECODED_LINE))
        return
    if isinstance(record, list):
        for item in record:
            _scan_decoded_into(item, results)
        return
    if isinstance(record, dict):
        for value in record.values():
            _scan_decoded_into(value, results)


def _raise_secret_error(matches: list[tuple[str, int]]) -> None:
    pattern_id, line_number = matches[0]
    raise ReplayError(
        CODE_SECRET_DETECTED,
        (
            f"replay fixture contains a high-confidence secret pattern "
            f"{pattern_id!r} on line {line_number}; "
            "fixtures must not contain real secrets (ADR 0036)"
        ),
        pattern_id=pattern_id,
        line_number=line_number,
    )


def _parse_record(raw_line: str, line_number: int) -> dict[str, Any]:
    stripped = raw_line.strip()
    if not stripped:
        # Treat empty / whitespace-only lines as missing records.
        # A line that exists but carries no payload is a contract
        # violation; the brief requires every line to be a record.
        raise ReplayError(
            CODE_MISSING_FIELD,
            f"line {line_number} is empty; expected a JSON record",
            line_number=line_number,
        )
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ReplayError(
            CODE_MALFORMED_JSON,
            (
                f"line {line_number} is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})"
            ),
            line_number=line_number,
        ) from exc
    if not isinstance(obj, dict):
        raise ReplayError(
            CODE_MALFORMED_JSON,
            (
                f"line {line_number} must decode to a JSON object; "
                f"got {type(obj).__name__}"
            ),
            line_number=line_number,
        )
    if "schema_version" not in obj:
        raise ReplayError(
            CODE_MISSING_FIELD,
            f"line {line_number} is missing required field 'schema_version'",
            line_number=line_number,
        )
    schema_version = obj["schema_version"]
    # ``bool`` is a subclass of ``int`` in Python; reject booleans
    # explicitly so a stray ``true`` does not silently pass.
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
    ):
        raise ReplayError(
            CODE_MISSING_FIELD,
            (
                f"line {line_number} 'schema_version' must be an int; "
                f"got {type(schema_version).__name__}"
            ),
            line_number=line_number,
        )
    if schema_version != REPLAY_SCHEMA_VERSION:
        raise ReplayError(
            CODE_SCHEMA_MISMATCH,
            (
                f"line {line_number} 'schema_version' is {schema_version}; "
                f"expected {REPLAY_SCHEMA_VERSION}"
            ),
            line_number=line_number,
        )
    if "name" not in obj:
        raise ReplayError(
            CODE_MISSING_FIELD,
            f"line {line_number} is missing required field 'name'",
            line_number=line_number,
        )
    name = obj["name"]
    if not isinstance(name, str) or not name:
        raise ReplayError(
            CODE_MISSING_FIELD,
            f"line {line_number} 'name' must be a non-empty string",
            line_number=line_number,
        )
    has_response = "response" in obj
    has_responses = "responses" in obj
    if has_response and has_responses:
        raise ReplayError(
            CODE_BOTH_RESPONSE_SHAPES,
            (
                f"line {line_number} record {name!r} carries both "
                "'response' and 'responses'; exactly one is allowed"
            ),
            line_number=line_number,
        )
    if not has_response and not has_responses:
        raise ReplayError(
            CODE_MISSING_FIELD,
            (
                f"line {line_number} record {name!r} is missing both "
                "'response' and 'responses'; exactly one is required"
            ),
            line_number=line_number,
        )
    if has_responses:
        responses = obj["responses"]
        if not isinstance(responses, list) or len(responses) == 0:
            raise ReplayError(
                CODE_EMPTY_RESPONSES,
                (
                    f"line {line_number} record {name!r} 'responses' must "
                    "be a non-empty list"
                ),
                line_number=line_number,
            )
        for index, item in enumerate(responses):
            if not isinstance(item, (str, int, float, bool, list, dict, type(None))):
                raise ReplayError(
                    CODE_MISSING_FIELD,
                    (
                        f"line {line_number} record {name!r} "
                        f"responses[{index}] is not a JSON-encodable value "
                        f"(got {type(item).__name__})"
                    ),
                    line_number=line_number,
                )
    else:
        response = obj["response"]
        if not isinstance(
            response, (str, int, float, bool, list, dict, type(None))
        ):
            raise ReplayError(
                CODE_MISSING_FIELD,
                (
                    f"line {line_number} record {name!r} 'response' is not "
                    f"a JSON-encodable value (got {type(response).__name__})"
                ),
                line_number=line_number,
            )
    return obj


def load_replay(path: Path) -> Replay:
    """Load and validate a recorded-replay JSONL fixture from ``path``.

    The loader scans the raw fixture text for high-confidence secret
    patterns before parsing any record (cheaper reject path), then
    validates each record structurally and re-scans the decoded
    string fields as defense in depth. Any rejection raises
    :class:`ReplayError` with a stable :attr:`~ReplayError.code`.
    """
    text = Path(path).read_text(encoding="utf-8")
    raw_matches = scan_replay_text(text)
    if raw_matches:
        _raise_secret_error(raw_matches)
    entry_queues: dict[str, list[Any]] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            # Skip blank lines; a trailing newline at EOF is the
            # common JSONL artifact. Non-blank lines must carry a
            # record.
            continue
        record = _parse_record(raw_line, line_number)
        decoded_matches = _scan_decoded(record)
        if decoded_matches:
            _raise_secret_error(decoded_matches)
        name = record["name"]
        if name in entry_queues:
            raise ReplayError(
                CODE_DUPLICATE_NAME,
                f"line {line_number} duplicate entry name {name!r}",
                line_number=line_number,
            )
        if "responses" in record:
            entry_queues[name] = list(record["responses"])
        else:
            entry_queues[name] = [record["response"]]
    if not entry_queues:
        raise ReplayError(
            CODE_MISSING_FIELD,
            "replay fixture has no records",
        )
    return Replay(entry_queues)


def _judge_call_fn(
    replay: Replay, entry_name: str
) -> Callable[..., Any]:
    def call_fn(*, context: Any = None) -> Any:
        # ``context`` is accepted for compatibility with
        # ``run_judge_evaluator`` (which invokes
        # ``call_fn(context=<mapping>)``); the replay harness
        # ignores it and returns the next recorded response.
        del context
        return replay.take(entry_name)

    call_fn.__name__ = f"replay_judge_{entry_name}"
    call_fn.__qualname__ = call_fn.__name__
    return call_fn


def build_judge_call_fns(replay: Replay) -> list[Callable[..., Any]]:
    """Return exactly two distinct callables bound to judge entries.

    The first callable is bound to the entry named ``judge_1`` (or the
    first entry whose name starts with ``judge_``); the second to
    ``judge_2`` (or the second such entry). The two callables are
    distinct objects (``a is not b``) so the two-judge independence
    contract used by :func:`run_judge_evaluator` is preserved.

    Raises :class:`ReplayError` with
    :data:`CODE_MISSING_ENTRY` when the replay does not expose two
    judge entries.
    """
    judge_entries = [
        name for name in replay.entry_names if name.startswith("judge_")
    ]
    first: str | None
    second: str | None
    if "judge_1" in judge_entries:
        first = "judge_1"
    elif judge_entries:
        first = judge_entries[0]
    else:
        first = None
    if "judge_2" in judge_entries:
        second = "judge_2"
    elif len(judge_entries) >= 2:
        second = judge_entries[1]
    else:
        second = None
    if first is None or second is None:
        if first is None and second is None:
            missing = "judge_1/judge_2"
        elif first is None:
            missing = "judge_1"
        else:
            missing = "judge_2"
        raise ReplayError(
            CODE_MISSING_ENTRY,
            f"replay is missing required judge entry {missing!r}",
            entry_name=missing,
        )
    fn_a = _judge_call_fn(replay, first)
    fn_b = _judge_call_fn(replay, second)
    return [fn_a, fn_b]


def build_optimizer_call_fn(replay: Replay) -> Callable[..., Any]:
    """Return one callable bound to the ``optimizer`` entry.

    The callable consumes recorded responses in declared order and
    raises :class:`ReplayExhausted` when its queue is empty. It is
    shaped to slot into
    :func:`metacrucible.provider_config.call_structured` (which
    invokes ``call_fn(repair_context=...)``) and therefore into
    :func:`metacrucible.optimizer.run_optimizer_pipeline` indirectly.

    Raises :class:`ReplayError` with
    :data:`CODE_MISSING_ENTRY` when the replay does not expose an
    ``optimizer`` entry.
    """
    if "optimizer" not in replay.entry_names:
        raise ReplayError(
            CODE_MISSING_ENTRY,
            "replay is missing required entry 'optimizer'",
            entry_name="optimizer",
        )

    def call_fn(*, repair_context: Any = None) -> Any:
        # ``repair_context`` is accepted for compatibility with
        # ``call_structured`` (which invokes
        # ``call_fn(repair_context=<mapping or None>)``); the
        # replay harness ignores it and returns the next recorded
        # response.
        del repair_context
        return replay.take("optimizer")

    call_fn.__name__ = "replay_optimizer"
    call_fn.__qualname__ = call_fn.__name__
    return call_fn
