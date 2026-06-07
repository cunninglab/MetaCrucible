"""Claude Code ``--agents`` subagent injection (Issue #10).

This module materializes a parsed :class:`~metacrucible.artifact.SubagentArtifact`
into the JSON shape Claude Code consumes via its headless ``--agents``
flag, and provides a verifier that downstream tooling can run against
the materialized file as a local-real smoke pass.

Per ADR 0028 the Claude Code adapter runs in ``--bare`` mode with
``--agents`` for subagents. The shape Claude Code consumes is a
top-level object keyed by the resolved subagent name; each value
carries ``description``, ``prompt`` (the subagent's system prompt),
and an optional ``tools`` list::

    {
      "researcher": {
        "description": "Searches and analyzes code repositories.",
        "prompt": "You are a research subagent...",
        "tools": ["Read", "Grep", "Glob"]
      }
    }

The module exposes:

  - :func:`build_claude_code_agents_payload` — pure mapping from
    ``SubagentArtifact`` to the ``--agents`` JSON shape.
  - :func:`materialize_subagent` — validate routing surface, build
    the payload, and write it to ``<output_dir>/agents.json``;
    returns a result dict with the same shape used by
    ``init --check``, ``promote``, and
    :func:`metacrucible.preflight.check_subagent_preflight`.
  - :func:`verify_subagent_injection` — read the materialized
    file back and confirm the routing surface matches the
    expectation; this is the local-real smoke verifier.

The injection contract is intentionally narrow: only routing
surface fields (``name``, ``description``, ``tools``) and the
mutable system prompt are propagated. Execution parameters
(``model``, ``thinkingLevel``, ...) are deliberately not mapped
into the ``--agents`` JSON shape — Claude Code does not consume
them there, and silently copying them in would let optimizers
think Claude Code will act on a field it ignores.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .artifact import SubagentArtifact

__all__ = [
    "SUBAGENT_INJECTION_NAME_MISSING_BLOCKER",
    "SUBAGENT_INJECTION_NAME_INVALID_BLOCKER",
    "SUBAGENT_INJECTION_DESCRIPTION_MISSING_BLOCKER",
    "SUBAGENT_INJECTION_SYSTEM_PROMPT_MISSING_BLOCKER",
    "SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER",
    "SUBAGENT_INJECTION_WRITE_FAILED_BLOCKER",
    "SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER",
    "AGENTS_JSON_FILENAME",
    "SAFE_AGENT_NAME_RE",
    "build_claude_code_agents_payload",
    "materialize_subagent",
    "verify_subagent_injection",
]


# --------------------------------------------------------------------------- #
# Stable blocker ids                                                          #
# --------------------------------------------------------------------------- #
#
# Machine contract: the optimizer pipeline and the local-real smoke
# harness branch on the exact strings. Adding a new blocker id is a
# contract change; renaming an existing id is a breaking change and
# must be paired with a migration plan.

SUBAGENT_INJECTION_NAME_MISSING_BLOCKER: str = "subagent-injection-name-missing"
SUBAGENT_INJECTION_NAME_INVALID_BLOCKER: str = "subagent-injection-name-invalid"
SUBAGENT_INJECTION_DESCRIPTION_MISSING_BLOCKER: str = (
    "subagent-injection-description-missing"
)
SUBAGENT_INJECTION_SYSTEM_PROMPT_MISSING_BLOCKER: str = (
    "subagent-injection-system-prompt-missing"
)
SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER: str = (
    "subagent-injection-routing-mutated"
)
SUBAGENT_INJECTION_WRITE_FAILED_BLOCKER: str = (
    "subagent-injection-write-failed"
)
SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER: str = (
    "subagent-injection-payload-invalid"
)


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Name of the file written by :func:`materialize_subagent`. The
#: local-real smoke harness (Issue #46) loads this path verbatim
#: when invoking ``claude --agents <path>``.
AGENTS_JSON_FILENAME: str = "agents.json"

#: Safe subagent name shape. Claude Code addresses subagents by
#: the top-level JSON key, so the routing-surface ``name`` must
#: be a flat, non-empty identifier with no whitespace or path
#: separators. Allowing ``-`` and ``_`` mirrors Claude Code's own
#: naming conventions for subagents.
SAFE_AGENT_NAME_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


# --------------------------------------------------------------------------- #
# Pure mapping                                                                #
# --------------------------------------------------------------------------- #


def _coerce_tools(value: Any) -> list[str]:
    """Return ``value`` as a list of tool-name strings.

    The artifact parser coerces YAML block sequences to a Python
    ``list`` already (see :mod:`metacrucible.artifact`), but a
    caller may pass a single string ("Read"), ``None``, or a
    missing field. This helper normalizes all of those into the
    shape the Claude Code ``--agents`` JSON expects.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def build_claude_code_agents_payload(
    artifact: SubagentArtifact,
) -> dict[str, Any]:
    """Map a :class:`SubagentArtifact` to the Claude Code ``--agents`` JSON shape.

    The mapping lifts the routing-surface ``name`` into the
    top-level JSON key, propagates ``description`` and ``tools``
    verbatim, and uses the subagent's ``systemPrompt`` (or, when
    absent, the Markdown body) as Claude Code's ``prompt``.

    Execution parameters (``model``, ``thinkingLevel``,
    ``readSummarize``, ``blocking``, ``autoloadSkills``) and the
    non-prompt routing fields (``spawns``, ``output``) are
    deliberately **not** mapped: Claude Code does not consume
    them in the ``--agents`` JSON shape, and silently copying
    them in would let optimizers believe Claude Code will act on
    a field it ignores.
    """
    frontmatter: Mapping[str, Any] = artifact.frontmatter
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    system_prompt = frontmatter.get("systemPrompt") or artifact.body
    tools = _coerce_tools(frontmatter.get("tools"))
    entry: dict[str, Any] = {
        "description": description if isinstance(description, str) else "",
        "prompt": system_prompt if isinstance(system_prompt, str) else "",
    }
    if tools:
        entry["tools"] = tools
    return {str(name): entry}


# --------------------------------------------------------------------------- #
# Materialize                                                                 #
# --------------------------------------------------------------------------- #


def _blocker(blocker_id: str, message: str) -> dict[str, str]:
    """Return a single ``{id, message}`` blocker entry."""
    return {"id": blocker_id, "message": message}


def _validate_for_materialization(
    artifact: SubagentArtifact,
) -> list[dict[str, str]]:
    """Validate ``artifact`` for safe materialization; return blockers."""
    blockers: list[dict[str, str]] = []
    frontmatter: Mapping[str, Any] = artifact.frontmatter
    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        blockers.append(
            _blocker(
                SUBAGENT_INJECTION_NAME_MISSING_BLOCKER,
                (
                    "subagent frontmatter 'name' must be a non-empty string "
                    "before Claude Code --agents injection (Issue #10 AC1)"
                ),
            )
        )
    else:
        if not SAFE_AGENT_NAME_RE.match(name):
            blockers.append(
                _blocker(
                    SUBAGENT_INJECTION_NAME_INVALID_BLOCKER,
                    (
                        f"subagent name {name!r} is not a safe Claude Code "
                        f"agent identifier; must match {SAFE_AGENT_NAME_RE.pattern!r} "
                        "(Issue #10 AC1)"
                    ),
                )
            )
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        blockers.append(
            _blocker(
                SUBAGENT_INJECTION_DESCRIPTION_MISSING_BLOCKER,
                (
                    "subagent frontmatter 'description' must be a non-empty "
                    "string before Claude Code --agents injection (Issue #10 AC1)"
                ),
            )
        )
    system_prompt = frontmatter.get("systemPrompt") or artifact.body
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        blockers.append(
            _blocker(
                SUBAGENT_INJECTION_SYSTEM_PROMPT_MISSING_BLOCKER,
                (
                    "subagent must have a non-empty 'systemPrompt' (or "
                    "Markdown body) before Claude Code --agents injection "
                    "(Issue #10 AC1)"
                ),
            )
        )
    return blockers


def _write_agents_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically.

    Uses a ``tmp`` + ``os.replace`` rename in the same directory so
    the final write is atomic on POSIX, mirroring the pattern
    :mod:`metacrucible.storage` uses for ``envelope.json`` and
    ``state.json``. Independent-review hardening: a partial write
    cannot leave the file half-rendered because the rename either
    succeeds in full or does not happen at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def materialize_subagent(
    artifact: SubagentArtifact,
    output_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Materialize a candidate subagent for Claude Code ``--agents`` injection.

    AC1: candidate subagent is materialized safely. The function
    validates the routing surface (name, description, system
    prompt) before any filesystem write, so a malformed artifact
    is rejected with a stable blocker id and the file is never
    created. A clean materialization writes
    ``<output_dir>/agents.json`` and returns a result dict whose
    ``ok`` / ``blockers`` shape matches ``init --check`` and
    :func:`metacrucible.promote.promote_case`.

    Parameters
    ----------
    artifact:
        The parsed subagent artifact to materialize.
    output_dir:
        Directory the materialized file is written to. Created
        (with parents) if it does not exist.

    Returns
    -------
    dict
        ``ok`` (bool), ``blockers`` (list[dict]), ``agents_path``
        (str path to the written file or empty on block),
        ``name`` (resolved subagent name or empty on block),
        ``routing_preserved`` (always ``True`` on ok=True; the
        mapping is a literal lift so any drift would be a parser
        bug, not a runtime condition).
    """
    output_path = Path(output_dir)
    blockers = _validate_for_materialization(artifact)
    if blockers:
        return {
            "ok": False,
            "blockers": blockers,
            "agents_path": "",
            "name": "",
            "routing_preserved": False,
        }
    payload = build_claude_code_agents_payload(artifact)
    agents_path = output_path / AGENTS_JSON_FILENAME
    try:
        _write_agents_json(agents_path, payload)
    except OSError as exc:
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    SUBAGENT_INJECTION_WRITE_FAILED_BLOCKER,
                    (
                        f"failed to write materialized --agents JSON to "
                        f"{agents_path}: {exc}"
                    ),
                )
            ],
            "agents_path": "",
            "name": "",
            "routing_preserved": False,
        }
    name = next(iter(payload.keys()), "")
    return {
        "ok": True,
        "blockers": [],
        "agents_path": str(agents_path),
        "name": name,
        "routing_preserved": True,
    }


# --------------------------------------------------------------------------- #
# Verify (local-real smoke)                                                   #
# --------------------------------------------------------------------------- #


def verify_subagent_injection(
    agents_path: str | os.PathLike[str],
    *,
    expected_name: str,
    expected_description: str,
) -> dict[str, Any]:
    """Verify a materialized ``--agents`` JSON file preserves the routing surface.

    AC2+AC3: routing fields are respected, and the injection is
    verifiable in a local-real smoke pass. The verifier reads
    ``agents_path`` back, decodes the JSON, and confirms the
    top-level key equals ``expected_name`` and the entry's
    ``description`` equals ``expected_description`` byte-for-byte.

    Returns a dict with the same ``ok`` / ``blockers`` /
    ``agents_path`` shape as :func:`materialize_subagent` so the
    smoke pass can chain the two without reshaping.
    """
    path = Path(agents_path)
    if not path.is_file():
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER,
                    (
                        f"materialized --agents JSON does not exist at "
                        f"{path}; cannot verify routing surface (Issue #10 AC3)"
                    ),
                )
            ],
            "agents_path": str(path),
            "name": "",
            "discoverable": None,
        }
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER,
                    (
                        f"failed to read materialized --agents JSON at "
                        f"{path}: {exc}"
                    ),
                )
            ],
            "agents_path": str(path),
            "name": "",
            "discoverable": None,
        }
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER,
                    (
                        f"materialized --agents JSON at {path} is not valid "
                        f"JSON: {exc}"
                    ),
                )
            ],
            "agents_path": str(path),
            "name": "",
            "discoverable": None,
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER,
                    (
                        f"materialized --agents JSON at {path} must be a JSON "
                        f"object at the top level; got {type(payload).__name__}"
                    ),
                )
            ],
            "agents_path": str(path),
            "name": "",
            "discoverable": None,
        }
    if expected_name not in payload:
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER,
                    (
                        f"materialized --agents JSON at {path} does not "
                        f"contain the expected agent key {expected_name!r}; "
                        f"got keys {sorted(payload.keys())!r} (Issue #10 AC2+AC3)"
                    ),
                )
            ],
            "agents_path": str(path),
            "name": "",
            "discoverable": None,
        }
    entry = payload[expected_name]
    if not isinstance(entry, dict):
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    SUBAGENT_INJECTION_PAYLOAD_INVALID_BLOCKER,
                    (
                        f"materialized --agents JSON at {path} entry "
                        f"{expected_name!r} must be a JSON object; got "
                        f"{type(entry).__name__}"
                    ),
                )
            ],
            "agents_path": str(path),
            "name": "",
            "discoverable": None,
        }
    if entry.get("description") != expected_description:
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    SUBAGENT_INJECTION_ROUTING_MUTATED_BLOCKER,
                    (
                        f"materialized --agents JSON at {path} has a "
                        f"routing-surface description mismatch for "
                        f"{expected_name!r}: expected {expected_description!r}, "
                        f"got {entry.get('description')!r} (Issue #10 AC2+AC3)"
                    ),
                )
            ],
            "agents_path": str(path),
            "name": "",
            "discoverable": None,
        }
    return {
        "ok": True,
        "blockers": [],
        "agents_path": str(path),
        "name": expected_name,
        "discoverable": None,
    }
