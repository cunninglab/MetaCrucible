"""Tests for Issue #10: Claude Code subagent injection via ``--agents``.

Issue #10 pins the local-real adapter step for evaluating a
candidate subagent through Claude Code's headless ``--agents``
injection. The acceptance criteria are:

  - Candidate subagent is materialized safely.
  - Routing fields are respected.
  - Injection is verifiable in local-real smoke.

Per ADR 0028 the Claude Code adapter runs in ``--bare`` mode with
``--agents`` for subagents. The injection contract the rest of the
optimizer pipeline branches on is:

  - A parsed ``SubagentArtifact`` is mapped to the Claude Code
    ``--agents`` JSON shape (``{<agent-name>: {description, prompt,
    tools}}``).
  - The mapping is materialized to a JSON file at a known path so
    the local-real smoke harness can pass ``--agents <path>`` to
    the Claude Code binary (or load the file inline as the JSON
    value for ``--agents``).
  - The materialized file is round-trip-verifiable: the resolved
    name and description match the source routing surface
    verbatim, so a smoke pass that re-parses the file can detect
    any routing drift and fail loud.

These tests are the red step: the ``subagent_injection`` module
does not exist yet, so importing it must fail. Once the module
lands, the tests will turn green and pin the acceptance criteria
verbatim.

The implementation under test (not yet written) is expected to
live under ``metacrucible.subagent_injection`` and to expose at
least:

  - :func:`build_claude_code_agents_payload(artifact)` — pure
    mapping from ``SubagentArtifact`` to the ``--agents`` JSON
    shape.
  - :func:`materialize_subagent(artifact, output_dir)` —
    validate, build, and write the JSON file; return a result
    with ``ok`` / ``blockers`` / ``agents_path`` / ``name`` /
    ``routing_preserved`` matching the shape used by ``init
    --check``, ``promote``, and :func:`metacrucible.preflight.check_subagent_preflight`.
  - :func:`verify_subagent_injection(agents_path, expected_name,
    expected_description)` — read the materialized file back and
    confirm the routing surface matches the expectation. This is
    the local-real smoke verifier.

References
----------
- ADR 0028 (Claude Code adapter contract): runs in ``--bare`` mode
  with ``--add-dir`` for Skills and ``--agents`` for subagents.
- Issue #4 (Capability Artifact parser): produces the
  ``SubagentArtifact`` input the injection module consumes.
- Issue #9 (preflight sentinel): defines the
  ``METACRUCIBLE_SUBAGENT_DISCOVERABLE`` sentinel the smoke pass
  asserts on once the subagent is discoverable.
- Issue #10 acceptance criteria.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

INJECTION_MODULE = "metacrucible.subagent_injection"
PARSER_MODULE = "metacrucible.artifact"
PREFLIGHT_MODULE = "metacrucible.preflight"

# --------------------------------------------------------------------------- #
# Stable blocker ids for subagent injection.                                   #
# --------------------------------------------------------------------------- #
#
# These are the machine contract: the optimizer pipeline and the
# local-real smoke harness branch on the exact strings. Adding a
# new blocker id is a contract change; renaming an existing id is
# a breaking change.

SUBAGENT_INJECTION_NAME_MISSING_BLOCKER = "subagent-injection-name-missing"
SUBAGENT_INJECTION_NAME_INVALID_BLOCKER = "subagent-injection-name-invalid"
SUBAGENT_INJECTION_DESCRIPTION_MISSING_BLOCKER = (
    "subagent-injection-description-missing"
)
SUBAGENT_INJECTION_SYSTEM_PROMPT_MISSING_BLOCKER = (
    "subagent-injection-system-prompt-missing"
)
SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER = (
    "subagent-injection-routing-mutated"
)
SUBAGENT_INJECTION_WRITE_FAILED_BLOCKER = (
    "subagent-injection-write-failed"
)
SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER = (
    "subagent-injection-payload-invalid"
)

# --------------------------------------------------------------------------- #
# Sample subagent source used to exercise the injection pipeline.             #
# --------------------------------------------------------------------------- #

SUBAGENT_SOURCE: str = (
    "---\n"
    "name: researcher\n"
    "description: Searches and analyzes code repositories.\n"
    "tools:\n"
    "  - Read\n"
    "  - Grep\n"
    "  - Glob\n"
    "spawns:\n"
    "  - helper-agent\n"
    "output: json\n"
    "model: opus\n"
    "thinkingLevel: medium\n"
    "systemPrompt: |\n"
    "  You are a research subagent.\n"
    "  Investigate the codebase carefully.\n"
    "---\n"
    "\n"
    "Optional Markdown body after the frontmatter.\n"
)

# Claude Code ``--agents`` JSON shape (ADR 0028): the top-level
# object is keyed by the agent name; each value carries
# description, prompt, and tools. The injection module must emit
# exactly this shape so ``claude --agents '<json>'`` (or
# ``--agents <path>``) can load it as-is.
EXPECTED_AGENTS_PAYLOAD: dict[str, Any] = {
    "researcher": {
        "description": "Searches and analyzes code repositories.",
        "prompt": (
            "You are a research subagent.\n"
            "Investigate the codebase carefully.\n"
        ),
        "tools": ["Read", "Grep", "Glob"],
    }
}


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _blocker_ids(payload: Any) -> list[str]:
    """Return the list of blocker ids in a result, or empty if none."""
    if not isinstance(payload, dict):
        return []
    blockers = payload.get("blockers", [])
    if not isinstance(blockers, list):
        return []
    out: list[str] = []
    for blocker in blockers:
        if isinstance(blocker, dict) and isinstance(blocker.get("id"), str):
            out.append(blocker["id"])
        elif isinstance(blocker, str):
            out.append(blocker)
    return out


@pytest.fixture(scope="module")
def injection() -> Any:
    """Import the injection module; the test fails (red step) if it does not exist."""
    import importlib

    try:
        return importlib.import_module(INJECTION_MODULE)
    except ImportError as exc:
        pytest.fail(
            f"injection module {INJECTION_MODULE!r} is not implemented yet "
            f"(Issue #10 red step). Expected module exposing: "
            f"build_claude_code_agents_payload, materialize_subagent, "
            f"verify_subagent_injection. ImportError: {exc}"
        )


@pytest.fixture(scope="module")
def parser() -> Any:
    """Import the capability artifact parser (Issue #4)."""
    import importlib

    return importlib.import_module(PARSER_MODULE)


@pytest.fixture(scope="module")
def preflight() -> Any:
    """Import the preflight module (Issue #9)."""
    import importlib

    return importlib.import_module(PREFLIGHT_MODULE)


@pytest.fixture
def parsed_subagent(parser: Any) -> Any:
    """Return a parsed ``SubagentArtifact`` built from ``SUBAGENT_SOURCE``."""
    return parser.parse_subagent(SUBAGENT_SOURCE)


# --------------------------------------------------------------------------- #
# AC1 — Candidate subagent is materialized safely                             #
# --------------------------------------------------------------------------- #


def test_injection_module_exposes_build_payload(injection: Any) -> None:
    """The injection module must expose ``build_claude_code_agents_payload``."""
    assert hasattr(injection, "build_claude_code_agents_payload"), (
        f"{INJECTION_MODULE!r} must expose build_claude_code_agents_payload "
        f"(Issue #10 AC1); got attributes "
        f"{sorted(a for a in dir(injection) if not a.startswith('_'))!r}"
    )
    assert callable(injection.build_claude_code_agents_payload), (
        f"{INJECTION_MODULE!r}.build_claude_code_agents_payload must be callable"
    )


def test_injection_module_exposes_materialize_subagent(injection: Any) -> None:
    """The injection module must expose ``materialize_subagent``."""
    assert hasattr(injection, "materialize_subagent"), (
        f"{INJECTION_MODULE!r} must expose materialize_subagent "
        f"(Issue #10 AC1); got attributes "
        f"{sorted(a for a in dir(injection) if not a.startswith('_'))!r}"
    )
    assert callable(injection.materialize_subagent), (
        f"{INJECTION_MODULE!r}.materialize_subagent must be callable"
    )


def test_injection_module_exposes_verify_subagent_injection(
    injection: Any,
) -> None:
    """The injection module must expose ``verify_subagent_injection``."""
    assert hasattr(injection, "verify_subagent_injection"), (
        f"{INJECTION_MODULE!r} must expose verify_subagent_injection "
        f"(Issue #10 AC3); got attributes "
        f"{sorted(a for a in dir(injection) if not a.startswith('_'))!r}"
    )
    assert callable(injection.verify_subagent_injection), (
        f"{INJECTION_MODULE!r}.verify_subagent_injection must be callable"
    )


def test_build_claude_code_agents_payload_uses_subagent_name(
    injection: Any, parsed_subagent: Any
) -> None:
    """The top-level JSON key must be the resolved subagent name.

    Claude Code addresses subagents by the top-level key, so the
    routing surface ``name`` must be lifted into the JSON key
    position verbatim. A mismatch is a routing-surface violation
    the optimizer pipeline would silently accept.
    """
    payload = injection.build_claude_code_agents_payload(parsed_subagent)
    assert isinstance(payload, dict), (
        f"build_claude_code_agents_payload must return a dict; "
        f"got {type(payload).__name__}"
    )
    keys = list(payload.keys())
    assert keys == ["researcher"], (
        f"top-level --agents JSON must be keyed by the resolved "
        f"subagent name (routing surface); got keys={keys!r}"
    )


def test_build_claude_code_agents_payload_preserves_description(
    injection: Any, parsed_subagent: Any
) -> None:
    """The ``description`` routing field must be preserved verbatim."""
    payload = injection.build_claude_code_agents_payload(parsed_subagent)
    entry = payload["researcher"]
    assert entry.get("description") == (
        "Searches and analyzes code repositories."
    ), (
        f"--agents JSON must preserve the routing-surface description; "
        f"got description={entry.get('description')!r}"
    )


def test_build_claude_code_agents_payload_uses_system_prompt_as_prompt(
    injection: Any, parsed_subagent: Any
) -> None:
    """The Claude Code ``prompt`` field must carry the subagent system prompt.

    Per ADR 0019, the subagent ``systemPrompt`` is the body the
    optimizer edits. The Claude Code ``--agents`` JSON consumes it
    under the key ``prompt``; the mapping must propagate it
    faithfully. The artifact parser (Issue #4) trims trailing
    blank lines from block scalars, so the test pins the
    parser-trimmed shape, not the raw source.
    """
    payload = injection.build_claude_code_agents_payload(parsed_subagent)
    entry = payload["researcher"]
    expected_prompt = (
        "You are a research subagent.\n"
        "Investigate the codebase carefully."
    )
    assert entry.get("prompt") == expected_prompt, (
        f"--agents JSON prompt must carry the systemPrompt verbatim; "
        f"got prompt={entry.get('prompt')!r}"
    )


def test_build_claude_code_agents_payload_preserves_tools(
    injection: Any, parsed_subagent: Any
) -> None:
    """The ``tools`` routing field must be preserved as a list of strings."""
    payload = injection.build_claude_code_agents_payload(parsed_subagent)
    entry = payload["researcher"]
    assert entry.get("tools") == ["Read", "Grep", "Glob"], (
        f"--agents JSON must preserve the routing-surface tools list; "
        f"got tools={entry.get('tools')!r}"
    )


def test_build_claude_code_agents_payload_omits_execution_params(
    injection: Any, parsed_subagent: Any
) -> None:
    """Execution parameters must NOT leak into the ``--agents`` JSON shape.

    ``model`` and ``thinkingLevel`` are execution parameters
    (Issue #4 AC4). The MVP Claude Code ``--agents`` shape does
    not consume them, and silently copying them in would muddy
    the optimizer's mental model of which fields Claude Code
    will actually act on. The mapping must only emit the fields
    the shape actually defines.
    """
    payload = injection.build_claude_code_agents_payload(parsed_subagent)
    entry = payload["researcher"]
    for forbidden in ("model", "thinkingLevel", "spawns", "output"):
        assert forbidden not in entry, (
            f"--agents JSON entry must not carry execution/extra "
            f"routing field {forbidden!r}; got entry={entry!r}"
        )


# --------------------------------------------------------------------------- #
# AC2 — Routing fields are respected                                           #
# --------------------------------------------------------------------------- #


def test_materialize_subagent_writes_agents_json(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """``materialize_subagent`` must write a parseable ``--agents`` JSON file.

    The local-real smoke harness reads this file by path and
    passes it to ``claude --agents <path>`` (or inlines it as the
    ``--agents`` value). The file must exist, be valid JSON, and
    decode to a dict with exactly one top-level entry (the
    resolved subagent name).
    """
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert isinstance(result, dict), (
        f"materialize_subagent must return a dict; got {type(result).__name__}"
    )
    assert result.get("ok") is True, (
        f"materialize_subagent must succeed for a valid subagent; "
        f"got result={result!r}"
    )
    assert _blocker_ids(result) == [], (
        f"materialize_subagent must not emit blockers for a valid "
        f"subagent; got blockers={_blocker_ids(result)!r}"
    )
    agents_path = result.get("agents_path")
    assert agents_path, (
        f"materialize_subagent must report the materialized file path; "
        f"got result={result!r}"
    )
    agents_file = Path(agents_path)
    assert agents_file.is_file(), (
        f"materialize_subagent must write the --agents JSON file to "
        f"{agents_file}; the path was reported but no file exists"
    )
    payload = json.loads(agents_file.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert list(payload.keys()) == ["researcher"], (
        f"materialized --agents JSON must be keyed by the resolved "
        f"subagent name; got keys={list(payload.keys())!r}"
    )


def test_materialize_subagent_preserves_routing_fields(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """The materialized file's routing fields must match the source verbatim.

    AC2: routing fields are respected. A materialized file that
    drops the description, normalizes whitespace in the name, or
    otherwise mutates the routing surface is a silent violation
    of AC2. The smoke harness must be able to read the file back
    and compare the resolved name/description/tools to the
    source frontmatter without finding a single-byte drift.
    """
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert result.get("ok") is True, (
        f"materialize_subagent must succeed; got result={result!r}"
    )
    agents_file = Path(result["agents_path"])
    payload = json.loads(agents_file.read_text(encoding="utf-8"))
    entry = payload["researcher"]
    assert entry["description"] == (
        "Searches and analyzes code repositories."
    ), (
        f"materialized description must match the source routing "
        f"field verbatim; got {entry['description']!r}"
    )
    assert entry["tools"] == ["Read", "Grep", "Glob"], (
        f"materialized tools must match the source routing field "
        f"verbatim; got {entry['tools']!r}"
    )
    assert result.get("routing_preserved") is True, (
        f"materialize_subagent must report routing_preserved=True on a "
        f"clean materialization; got result={result!r}"
    )


def test_materialize_subagent_reports_resolved_name(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """The result must surface the resolved subagent name for downstream logs."""
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert result.get("ok") is True
    assert result.get("name") == "researcher", (
        f"materialize_subagent must report the resolved name; "
        f"got name={result.get('name')!r}"
    )


def test_materialize_subagent_blocks_missing_name(
    injection: Any, parser: Any, tmp_path: Path
) -> None:
    """A subagent with no ``name`` must block injection safely.

    AC1: candidate is materialized *safely*. Materializing an
    unnamed subagent would silently let Claude Code register an
    agent with a missing/empty identifier. The blocker must
    surface the stable ``subagent-injection-name-missing`` id so
    the optimizer pipeline can branch on it.
    """
    source = (
        "---\n"
        "description: Has no name field.\n"
        "systemPrompt: |\n"
        "  body\n"
        "---\n"
    )
    artifact = parser.parse_subagent(source)
    result = injection.materialize_subagent(artifact, tmp_path)
    assert result.get("ok") is False, (
        f"missing name must block injection; got result={result!r}"
    )
    assert SUBAGENT_INJECTION_NAME_MISSING_BLOCKER in _blocker_ids(result), (
        f"missing name must emit blocker id "
        f"{SUBAGENT_INJECTION_NAME_MISSING_BLOCKER!r}; "
        f"got blocker_ids={_blocker_ids(result)!r}"
    )


def test_materialize_subagent_blocks_invalid_name(
    injection: Any, parser: Any, tmp_path: Path
) -> None:
    """An unsafe name (whitespace, separators) must block injection.

    Claude Code agent names must be safe identifiers; the
    materializer must reject names that would either break the
    ``--agents`` JSON or escape the key namespace.
    """
    source = (
        "---\n"
        "name: bad name with spaces\n"
        "description: Has an unsafe name.\n"
        "systemPrompt: |\n"
        "  body\n"
        "---\n"
    )
    artifact = parser.parse_subagent(source)
    result = injection.materialize_subagent(artifact, tmp_path)
    assert result.get("ok") is False
    assert SUBAGENT_INJECTION_NAME_INVALID_BLOCKER in _blocker_ids(result), (
        f"invalid name must emit blocker id "
        f"{SUBAGENT_INJECTION_NAME_INVALID_BLOCKER!r}; "
        f"got blocker_ids={_blocker_ids(result)!r}"
    )


def test_materialize_subagent_blocks_missing_description(
    injection: Any, parser: Any, tmp_path: Path
) -> None:
    """A subagent with no ``description`` must block injection safely.

    The Claude Code ``--agents`` JSON requires a description;
    silently emitting an empty string would let Claude Code
    register an agent the user cannot identify.
    """
    source = (
        "---\n"
        "name: nameless-desc\n"
        "systemPrompt: |\n"
        "  body\n"
        "---\n"
    )
    artifact = parser.parse_subagent(source)
    result = injection.materialize_subagent(artifact, tmp_path)
    assert result.get("ok") is False
    assert SUBAGENT_INJECTION_DESCRIPTION_MISSING_BLOCKER in _blocker_ids(result), (
        f"missing description must emit blocker id "
        f"{SUBAGENT_INJECTION_DESCRIPTION_MISSING_BLOCKER!r}; "
        f"got blocker_ids={_blocker_ids(result)!r}"
    )


def test_materialize_subagent_blocks_missing_system_prompt(
    injection: Any, parser: Any, tmp_path: Path
) -> None:
    """A subagent with no systemPrompt and no body must block injection.

    Claude Code refuses to register an agent with no ``prompt``.
    A silent empty prompt is worse than a loud block.
    """
    source = (
        "---\n"
        "name: no-prompt\n"
        "description: missing prompt body\n"
        "---\n"
    )
    artifact = parser.parse_subagent(source)
    result = injection.materialize_subagent(artifact, tmp_path)
    assert result.get("ok") is False
    assert SUBAGENT_INJECTION_SYSTEM_PROMPT_MISSING_BLOCKER in _blocker_ids(
        result
    ), (
        f"missing system prompt must emit blocker id "
        f"{SUBAGENT_INJECTION_SYSTEM_PROMPT_MISSING_BLOCKER!r}; "
        f"got blocker_ids={_blocker_ids(result)!r}"
    )


def test_materialize_subagent_creates_output_dir(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """The materializer must create the output directory if it does not exist.

    A workspace initialization step that calls
    ``materialize_subagent`` before any directory exists must
    succeed; the contract is "the directory will exist after this
    call".
    """
    nested = tmp_path / "nested" / "agents"
    assert not nested.is_dir()
    result = injection.materialize_subagent(parsed_subagent, nested)
    assert result.get("ok") is True, (
        f"materialize_subagent must create the output dir; got result={result!r}"
    )
    assert nested.is_dir(), (
        f"materialize_subagent must create {nested}; the call reported "
        f"ok=True but the directory does not exist"
    )
    assert Path(result["agents_path"]).is_file()


# --------------------------------------------------------------------------- #
# AC3 — Injection is verifiable in local-real smoke                           #
# --------------------------------------------------------------------------- #


def test_verify_subagent_injection_accepts_clean_materialization(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """``verify_subagent_injection`` must accept a clean materialization.

    AC3 (positive): a file produced by ``materialize_subagent``
    that preserves the routing surface must verify cleanly. The
    smoke harness chains ``materialize`` → ``verify``; the
    verifier must not raise spurious blockers on the output the
    materializer just wrote.
    """
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert result.get("ok") is True
    agents_path = Path(result["agents_path"])
    verification = injection.verify_subagent_injection(
        agents_path,
        expected_name="researcher",
        expected_description="Searches and analyzes code repositories.",
    )
    assert isinstance(verification, dict), (
        f"verify_subagent_injection must return a dict; "
        f"got {type(verification).__name__}"
    )
    assert verification.get("ok") is True, (
        f"clean materialization must verify cleanly; "
        f"got verification={verification!r}"
    )
    assert _blocker_ids(verification) == []


def test_verify_subagent_injection_detects_mutated_name(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """The verifier must catch a routing field mutation in the file.

    AC2+AC3: a file that loses or mutates the routing-surface
    name must fail verification. The verifier is the local-real
    smoke pass's last line of defense against routing drift.
    """
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert result.get("ok") is True
    agents_path = Path(result["agents_path"])
    # Tamper with the materialized file: rename the top-level key.
    payload = json.loads(agents_path.read_text(encoding="utf-8"))
    entry = payload.pop("researcher")
    payload["someone-else"] = entry
    agents_path.write_text(json.dumps(payload), encoding="utf-8")
    verification = injection.verify_subagent_injection(
        agents_path,
        expected_name="researcher",
        expected_description="Searches and analyzes code repositories.",
    )
    assert verification.get("ok") is False, (
        f"verifier must catch a name mutation; got verification={verification!r}"
    )
    assert SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER in _blocker_ids(
        verification
    ), (
        f"name mutation must emit blocker id "
        f"{SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER!r}; "
        f"got blocker_ids={_blocker_ids(verification)!r}"
    )


def test_verify_subagent_injection_detects_mutated_description(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """The verifier must catch a routing-surface description mutation."""
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert result.get("ok") is True
    agents_path = Path(result["agents_path"])
    payload = json.loads(agents_path.read_text(encoding="utf-8"))
    payload["researcher"]["description"] = "Tampered description."
    agents_path.write_text(json.dumps(payload), encoding="utf-8")
    verification = injection.verify_subagent_injection(
        agents_path,
        expected_name="researcher",
        expected_description="Searches and analyzes code repositories.",
    )
    assert verification.get("ok") is False
    assert SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER in _blocker_ids(
        verification
    ), (
        f"description mutation must emit blocker id "
        f"{SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER!r}; "
        f"got blocker_ids={_blocker_ids(verification)!r}"
    )


def test_verify_subagent_injection_blocks_missing_file(
    injection: Any, tmp_path: Path
) -> None:
    """The verifier must surface a clear blocker when the file is absent."""
    missing = tmp_path / "does-not-exist.json"
    verification = injection.verify_subagent_injection(
        missing,
        expected_name="researcher",
        expected_description="any",
    )
    assert verification.get("ok") is False, (
        f"verifier must block on a missing file; got verification={verification!r}"
    )
    blockers = _blocker_ids(verification)
    assert blockers, (
        f"verifier must report at least one blocker on a missing file; "
        f"got verification={verification!r}"
    )


def test_verify_subagent_injection_blocks_malformed_file(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """The verifier must surface a clear blocker when the file is not JSON.

    A corrupted materialization (e.g. partial write, disk error)
    must not be silently accepted by the smoke pass.
    """
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert result.get("ok") is True
    agents_path = Path(result["agents_path"])
    agents_path.write_text("not a json document {", encoding="utf-8")
    verification = injection.verify_subagent_injection(
        agents_path,
        expected_name="researcher",
        expected_description="any",
    )
    blockers = _blocker_ids(verification)
    assert blockers, (
        f"malformed file must produce at least one blocker; "
        f"got verification={verification!r}"
    )


def test_verify_subagent_injection_blocks_wrong_shape(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """A file whose JSON shape is wrong must block the verifier."""
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert result.get("ok") is True
    agents_path = Path(result["agents_path"])
    # Top-level value is a list, not an object: not the
    # ``--agents`` shape.
    agents_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    verification = injection.verify_subagent_injection(
        agents_path,
        expected_name="researcher",
        expected_description="any",
    )
    assert verification.get("ok") is False
    assert _blocker_ids(verification), (
        f"wrong-shape file must produce at least one blocker; "
        f"got verification={verification!r}"
    )


def test_materialize_then_verify_roundtrip_preserves_sentinel_name(
    injection: Any,
    preflight: Any,
    parsed_subagent: Any,
    tmp_path: Path,
) -> None:
    """End-to-end: materialize, then build a preflight prompt from the resolved name.

    AC3 (local-real smoke): the resolved name surfaced by
    ``materialize_subagent`` must be the exact name a Claude Code
    preflight would probe. We use ``subagent_preflight_prompt``
    from Issue #9 to build the prompt and assert the resolved
    name appears verbatim, so the smoke harness can substitute it
    into ``--agents <path>`` without a separate discovery pass.
    """
    result = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert result.get("ok") is True
    resolved = result["name"]
    prompt = preflight.subagent_preflight_prompt(resolved)
    assert resolved in prompt, (
        f"preflight prompt for the resolved subagent name must carry "
        f"the name verbatim; got prompt={prompt!r} resolved={resolved!r}"
    )
    # The preflight sentinel prefix must also be present so the
    # smoke pass can parse the model's reply with the same code
    # path Issue #9 wired up.
    sentinel_prefix = getattr(preflight, "SUBAGENT_SENTINEL_PREFIX", None)
    assert sentinel_prefix, (
        "preflight module must expose SUBAGENT_SENTINEL_PREFIX (Issue #9 AC3)"
    )
    assert sentinel_prefix in prompt


def test_materialize_subagent_is_idempotent_on_existing_file(
    injection: Any, parsed_subagent: Any, tmp_path: Path
) -> None:
    """Re-materializing to the same path must overwrite cleanly (no stale state).

    A re-run of the smoke pass with a fresh optimizer revision
    must produce a fresh, routing-preserving materialization. A
    stale file with the same name must not be silently returned.
    """
    first = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert first.get("ok") is True
    second = injection.materialize_subagent(parsed_subagent, tmp_path)
    assert second.get("ok") is True, (
        f"re-materialization must succeed; got second={second!r}"
    )
    payload = json.loads(Path(second["agents_path"]).read_text(encoding="utf-8"))
    assert "researcher" in payload, (
        f"re-materialized file must still carry the resolved agent "
        f"key; got keys={list(payload.keys())!r}"
    )
    assert second["name"] == first["name"] == "researcher"


def test_injection_blocker_ids_are_stable_strings(injection: Any) -> None:
    """The injection module must expose the stable blocker-id constants.

    Downstream automation branches on the exact strings; tests
    pin them. Renaming an exported blocker id is a breaking
    change and must be paired with a migration plan.
    """
    expected = {
        "SUBAGENT_INJECTION_NAME_MISSING_BLOCKER": (
            SUBAGENT_INJECTION_NAME_MISSING_BLOCKER
        ),
        "SUBAGENT_INJECTION_NAME_INVALID_BLOCKER": (
            SUBAGENT_INJECTION_NAME_INVALID_BLOCKER
        ),
        "SUBAGENT_INJECTION_DESCRIPTION_MISSING_BLOCKER": (
            SUBAGENT_INJECTION_DESCRIPTION_MISSING_BLOCKER
        ),
        "SUBAGENT_INJECTION_SYSTEM_PROMPT_MISSING_BLOCKER": (
            SUBAGENT_INJECTION_SYSTEM_PROMPT_MISSING_BLOCKER
        ),
        "SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER": (
            SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER
        ),
        "SUBAGENT_INJECTION_WRITE_FAILED_BLOCKER": (
            SUBAGENT_INJECTION_WRITE_FAILED_BLOCKER
        ),
        "SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER": (
            SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER
        ),
    }
    for attr, value in expected.items():
        assert hasattr(injection, attr), (
            f"{INJECTION_MODULE!r} must expose {attr} as a stable "
            f"blocker-id constant; got attributes "
            f"{sorted(a for a in dir(injection) if not a.startswith('_'))!r}"
        )
        assert getattr(injection, attr) == value, (
            f"{INJECTION_MODULE!r}.{attr} must be exactly {value!r}; "
            f"got {getattr(injection, attr)!r}"
        )
