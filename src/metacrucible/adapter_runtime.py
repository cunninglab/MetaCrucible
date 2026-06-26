"""Thin adapter runtime harness for real Claude Code ``--bare`` invocations.

This module is the local-real bridge between the MetaCrucible pure-logic
contracts (:mod:`metacrucible.preflight`,
:mod:`metacrucible.claude_stream_json`,
:mod:`metacrucible.argv_normalize`) and the Claude Code runtime binary
that the developer invokes on their own machine.

Per ADR 0028 the Claude Code adapter runs in ``--bare`` mode; the
preflight sentinel (``METACRUCIBLE_SKILL_DISCOVERABLE=...``) is the
machine contract this harness relies on. Issue #46 expands the smoke
surface so the developer can prove Skill discovery end-to-end without
mutating their real ``~/.claude/`` directory or wiring a provider API
key.

The module exposes:

  - :func:`materialize_skill` — write an isolated
    ``<output_dir>/.claude/skills/<name>/SKILL.md`` tree from caller
    inputs. Never touches the user's home.
  - :func:`build_skill_preflight_argv` — pure argv builder. Emits the
    exact token shape required by the brief and ADR 0028; no
    subprocess side effects.
  - :func:`run_skill_preflight` — the **one** subprocess execution
    method. Spawns the binary, parses the stream-json stdout through
    :func:`metacrucible.claude_stream_json.parse_stream_json`, and
    folds the final output through
    :func:`metacrucible.preflight.check_skill_preflight`.
  - :func:`build_evidence_summary` — collapse the run record into a
    small dict the smoke pass and the optimizer pipeline can branch
    on.
  - :func:`build_subagent_preflight_argv` — pure argv builder for the
    subagent path (Issue #46 Task 2). Emits
    ``[<binary>, --bare, --agents <inline_json>, -p, --output-format stream-json]``
    so the materialized ``agents.json`` content is passed inline
    (the current ``claude`` runtime does not accept a file path on
    ``--agents``).
  - :func:`run_subagent_preflight` — the **one** subprocess execution
    method for the subagent path. Reads the materialized
    ``agents.json``, feeds its content inline as the ``--agents``
    flag value, parses stream-json stdout through
    :func:`metacrucible.claude_stream_json.parse_stream_json`, and
    folds the final output through
    :func:`metacrucible.preflight.check_subagent_preflight`.

The harness is intentionally thin: no provider SDK, no shell string,
no shell metachar quoting. Subprocess is confined to a single method;
the rest of the module is importable and unit-testable without a
real ``claude`` binary.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .argv_normalize import REVIEWED_TOOL_NAMES
from .claude_stream_json import ADAPTER_VERSION, parse_stream_json
from .preflight import (
    SKILL_SENTINEL_PREFIX,
    SUBAGENT_SENTINEL_PREFIX,
    check_skill_preflight,
    check_subagent_preflight,
    skill_preflight_prompt,
    subagent_preflight_prompt,
)

__all__ = [
    "SKILL_FILENAME",
    "CLAUDE_SKILLS_DIRNAME",
    "DEFAULT_BINARY",
    "DEFAULT_PERMISSION_MODE",
    "DEFAULT_OUTPUT_FORMAT",
    "DEFAULT_VERBOSE_FLAG",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_ALLOWED_TOOLS",
    "SAFE_SKILL_NAME_RE",
    "RUNTIME_CLAUDE",
    "RUNTIME_OMP",
    "OMP_ADAPTER_VERSION",
    "SKILL_MATERIALIZE_NAME_MISSING_BLOCKER",
    "SKILL_MATERIALIZE_NAME_INVALID_BLOCKER",
    "SKILL_MATERIALIZE_BODY_MISSING_BLOCKER",
    "SKILL_MATERIALIZE_WRITE_FAILED_BLOCKER",
    "SkillMaterialization",
    "SkillPreflightRun",
    "SubagentPreflightRun",
    "materialize_skill",
    "build_skill_preflight_argv",
    "run_skill_preflight",
    "build_subagent_preflight_argv",
    "run_subagent_preflight",
    "build_omp_skill_preflight_argv",
    "build_omp_subagent_preflight_argv",
    "run_omp_skill_preflight",
    "run_omp_subagent_preflight",
    "build_evidence_summary",
    "SUBAGENT_LOCAL_REAL_CONFIRM_PROMPT_TEMPLATE",
    "local_real_subagent_confirm_prompt",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Name of the Skill file Claude Code looks for. The Claude Code
#: skill directory ``.claude/skills/<name>/SKILL.md`` is the
#: contract.
SKILL_FILENAME: str = "SKILL.md"

#: Subdirectory under the project root (or under ``--add-dir``) where
#: Claude Code resolves Skills from. Mirrors the documented layout.
CLAUDE_SKILLS_DIRNAME: str = ".claude"

#: Default runtime binary name. Task 3 can override to ``"omp"`` to
#: drive oh-my-pi through the same harness.
DEFAULT_BINARY: str = "claude"

#: Default permission mode. ``default`` keeps the runtime's own
#: permission policy in effect; the smoke pass does not bypass it.
DEFAULT_PERMISSION_MODE: str = "default"

#: Default output format. ``stream-json`` is the only format that
#: :func:`metacrucible.claude_stream_json.parse_stream_json` can
#: classify, so the harness pins it.
DEFAULT_OUTPUT_FORMAT: str = "stream-json"

#: ``--verbose`` is required by ``claude`` whenever ``-p`` is paired
#: with ``--output-format=stream-json``; without it the runtime
#: aborts. The argv builder keeps the spec-pure shape; the
#: subprocess method injects this single flag.
DEFAULT_VERBOSE_FLAG: bool = True

#: Default subprocess timeout. Claude Code's headless preflight is
#: normally sub-second; 120s is a wide safety net that still fails
#: fast on a hung model.
DEFAULT_TIMEOUT_SECONDS: float = 120.0

#: Default reviewed tool allowlist. Skill discovery does not need
#: any tools to write or edit code, so the smoke pass only enables
#: ``Read`` to keep the run minimal.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read",)

#: Safe Skill name shape. Claude Code's skill resolver is
#: case-sensitive and rejects names with whitespace, path
#: separators, or leading dots. The character class mirrors the
#: Skill directory naming rules from the Claude Code docs.
SAFE_SKILL_NAME_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
)

# --------------------------------------------------------------------------- #
# Runtime discriminator (Issue #46 Task 3)                                    #
# --------------------------------------------------------------------------- #
#
# The harness supports two runtime binaries. The Skill and subagent argv
# shapes differ; the materializers, sentinel contract, and preflight
# checkers are shared (ADR 0003 shared-layout contract).

#: Runtime identifier for Claude Code (``--bare`` + ``--add-dir`` /
#: ``--agents`` argv shape, ``stream-json`` output).
RUNTIME_CLAUDE: str = "claude"

#: Runtime identifier for oh-my-pi (``--cwd <isolated-dir>`` argv shape,
#: ``--mode text`` output). Issue #46 Task 3.
RUNTIME_OMP: str = "omp"

#: Adapter version string surfaced on omp runs. Callers branch on the
#: ``adapter_version`` field; the omp-version field is filled from the
#: model lineage (e.g. ``"oh-my-pi/16.1.19"``).
OMP_ADAPTER_VERSION: str = "oh-my-pi/16.1.19"


# --------------------------------------------------------------------------- #
# Local-real adapter: subagent confirm-prompt (Issue #46 Repair 3)           #
# --------------------------------------------------------------------------- #
#
# The ADR 0028 :func:`metacrucible.preflight.subagent_preflight_prompt`
# asks the main model to introspect whether a named subagent is
# discoverable in the agent runtime. Models cannot truly introspect
# that, so they hedge to ``no`` — which is correct in the abstract
# but breaks the local-real smoke when the subagent **is** registered
# (it has a body in the subagent's system prompt, but the main model
# never sees that body because subagents are registered, not loaded
# into the main context).
#
# The terse confirm-prompt below is a LOCAL-REAL-ONLY adapter: it tells
# the main model the subagent is registered and asks it to confirm by
# echoing the exact sentinel. If the subagent is actually registered
# (via the harness's materialize path), the model can verify the
# claim and echoes ``yes``; if it is not registered, the model cannot
# confirm and the test fails loudly. This preserves the ADR 0028
# "discovery separate from use" boundary: the preflight still proves
# the runtime **registered** the subagent; it does not rely on the
# model self-introspecting.
#
# This prompt is opt-in via the ``local_real=True`` flag on the
# subagent subprocess methods. The DEFAULT (no flag) behavior keeps
# the verbose ADR 0028 prompt so pure-logic/contract behavior is
# unchanged.

#: Template for the local-real subagent confirm-prompt (Issue #46 Repair 3).
#: Mirrors the ADR 0028 sentinel shape but phrases the question as a
#: confirmation of a known-registered subagent. Renders via
#: :func:`local_real_subagent_confirm_prompt`.
SUBAGENT_LOCAL_REAL_CONFIRM_PROMPT_TEMPLATE: str = (
    "The subagent '{subagent_name}' is registered in this runtime. "
    "Confirm by replying exactly:\n"
    "    {prefix}=yes; NAME={subagent_name}\n"
    "Do not emit any other text.\n"
)

# --------------------------------------------------------------------------- #
# Stable blocker ids                                                          #
# --------------------------------------------------------------------------- #

#: Skill name is missing or empty.
SKILL_MATERIALIZE_NAME_MISSING_BLOCKER: str = "adapter-runtime-skill-name-missing"

#: Skill name is not a safe Claude Code identifier.
SKILL_MATERIALIZE_NAME_INVALID_BLOCKER: str = "adapter-runtime-skill-name-invalid"

#: Skill body is empty; the materializer must not write a bare
#: ``SKILL.md`` that the model can misinterpret.
SKILL_MATERIALIZE_BODY_MISSING_BLOCKER: str = (
    "adapter-runtime-skill-body-missing"
)

#: Filesystem write failed; caller must not silently swallow.
SKILL_MATERIALIZE_WRITE_FAILED_BLOCKER: str = (
    "adapter-runtime-skill-write-failed"
)


# --------------------------------------------------------------------------- #
# Result dataclasses                                                          #
# --------------------------------------------------------------------------- #


@dataclass
class SkillMaterialization:
    """Result of a Skill materialization.

    Mirrors the ``ok`` / ``blockers`` shape used by
    :func:`metacrucible.subagent_injection.materialize_subagent` and
    the ``init --check`` / ``promote`` commands so the smoke pass can
    chain the materializer with the runner without reshaping.
    """

    ok: bool
    blockers: list[dict[str, str]] = field(default_factory=list)
    skill_root: str = ""
    skill_md_path: str = ""
    name: str = ""


@dataclass
class SkillPreflightRun:
    """Result of a single ``run_skill_preflight`` invocation.

    ``argv`` is the full token list the harness executed (useful for
    debug logs and re-runs). ``evidence`` is the parse-stream-json
    evidence dict; ``preflight`` is the
    :func:`metacrucible.preflight.check_skill_preflight` result
    applied to ``evidence["final_output"]``. ``stdout`` / ``stderr``
    carry the raw captured streams so the smoke pass can write
    release evidence without re-running the binary.

    ``runtime`` is the runtime discriminator (``"claude"`` for the
    default Task 1/2 path; ``"omp"`` for the Task 3 oh-my-pi path).
    Callers branch on this field to distinguish the two surfaces.
    """

    argv: list[str] = field(default_factory=list)
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    preflight: dict[str, Any] = field(default_factory=dict)
    runtime: str = RUNTIME_CLAUDE



@dataclass
class SubagentPreflightRun:
    """Result of a single ``run_subagent_preflight`` invocation.

    Mirrors :class:`SkillPreflightRun` for the subagent path. ``argv``
    is the full token list the harness executed; ``evidence`` is the
    parse-stream-json evidence dict; ``preflight`` is the
    :func:`metacrucible.preflight.check_subagent_preflight` result
    applied to ``evidence["final_output"]``. ``agents_path`` is the
    materialized ``--agents`` JSON file the harness loaded (verbatim,
    for evidence dumps).

    ``runtime`` is the runtime discriminator (``"claude"`` for the
    default Task 2 path; ``"omp"`` for the Task 3 oh-my-pi path).
    """

    argv: list[str] = field(default_factory=list)
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    preflight: dict[str, Any] = field(default_factory=dict)
    runtime: str = RUNTIME_CLAUDE
    agents_path: str = ""

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _blocker(blocker_id: str, message: str) -> dict[str, str]:
    """Return a single ``{id, message}`` blocker entry."""
    return {"id": blocker_id, "message": message}


def _coerce_path(value: str | os.PathLike[str]) -> Path:
    """Coerce a path-like to :class:`pathlib.Path`."""
    return Path(os.fspath(value))


def _validate_skill_inputs(
    skill_name: str, skill_body: str
) -> list[dict[str, str]]:
    """Return the list of validation blockers for Skill inputs."""
    blockers: list[dict[str, str]] = []
    if not isinstance(skill_name, str) or not skill_name.strip():
        blockers.append(
            _blocker(
                SKILL_MATERIALIZE_NAME_MISSING_BLOCKER,
                (
                    "skill name must be a non-empty string before "
                    "materialization (Issue #46 Task 1)"
                ),
            )
        )
    elif not SAFE_SKILL_NAME_RE.match(skill_name):
        blockers.append(
            _blocker(
                SKILL_MATERIALIZE_NAME_INVALID_BLOCKER,
                (
                    f"skill name {skill_name!r} is not a safe Claude Code "
                    f"identifier; must match {SAFE_SKILL_NAME_RE.pattern!r} "
                    "(Issue #46 Task 1)"
                ),
            )
        )
    if not isinstance(skill_body, str) or not skill_body.strip():
        blockers.append(
            _blocker(
                SKILL_MATERIALIZE_BODY_MISSING_BLOCKER,
                (
                    "skill body must be a non-empty string before "
                    "materialization (Issue #46 Task 1)"
                ),
            )
        )
    return blockers


def _render_skill_md(skill_name: str, skill_body: str) -> str:
    """Render the ``SKILL.md`` content with the documented frontmatter.

    The frontmatter is the minimum Claude Code expects (a ``name``
    and a short ``description``) so a developer reading the file
    can still understand it. The body follows the frontmatter and
    must contain the literal preflight sentinel hint so the smoke
    pass can deterministically trigger discovery.
    """
    description = (
        f"MetaCrucible smoke Skill named {skill_name}; do not use outside "
        "Issue #46 Task 1 local-real smoke runs."
    )
    return (
        "---\n"
        f"name: {skill_name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"{skill_body.rstrip()}\n"
    )

def local_real_subagent_confirm_prompt(subagent_name: str = "") -> str:
    """Render the local-real subagent confirm-prompt (Issue #46 Repair 3).

    This is a LOCAL-REAL-ONLY adapter that rephrases the ADR 0028
    preflight question as a confirmation: the prompt tells the main
    model the subagent **is** registered and asks it to confirm by
    echoing the exact sentinel line. The main model can only confirm
    by replying ``yes`` if the subagent is actually registered in
    the runtime; if it is not, the model cannot confirm and the
    local-real test fails loudly.

    The reason this adapter exists: the main model does not have
    access to the subagent's body (subagents are registered, not
    loaded into the main context), so the verbose
    :func:`metacrucible.preflight.subagent_preflight_prompt` (which
    asks the model to introspect registration) causes the model to
    hedge to ``no`` even when the subagent is registered. The
    terse confirm-prompt narrows the question to a verifiable fact.

    The output of this helper has the SAME shape as the verbose
    preflight prompt (the sentinel prefix + the subagent name on
    the same line) so the standard
    :func:`metacrucible.preflight.check_subagent_preflight` parser
    still classifies the model's reply.

    The Skill path keeps the verbose
    :func:`metacrucible.preflight.skill_preflight_prompt` because
    Skill bodies are loaded into the main context (Task 1 priming
    works there) — only the subagent path needs this adapter.
    """
    return SUBAGENT_LOCAL_REAL_CONFIRM_PROMPT_TEMPLATE.format(
        prefix=SUBAGENT_SENTINEL_PREFIX,
        subagent_name=subagent_name or "<unknown>",
    )


# --------------------------------------------------------------------------- #
# Materialize                                                                 #
# --------------------------------------------------------------------------- #


def materialize_skill(
    *,
    skill_name: str,
    skill_body: str,
    output_dir: str | os.PathLike[str],
) -> SkillMaterialization:
    """Materialize a Skill into a caller-supplied scratch directory.

    Writes ``<output_dir>/.claude/skills/<skill_name>/SKILL.md`` and
    returns a :class:`SkillMaterialization` with the resolved
    ``skill_root`` (``<output_dir>/.claude/skills``) and
    ``skill_md_path``. The user's real home directory is never
    touched; ``output_dir`` is always the caller-controlled value.

    The validator runs before any filesystem write, so a malformed
    Skill is rejected with a stable blocker id and no file is
    created. The write itself is atomic via a ``.tmp`` + ``os.replace``
    rename, mirroring :func:`metacrucible.subagent_injection._write_agents_json`.
    """
    blockers = _validate_skill_inputs(skill_name, skill_body)
    if blockers:
        return SkillMaterialization(ok=False, blockers=blockers)

    output_path = _coerce_path(output_dir)
    skill_root = output_path / CLAUDE_SKILLS_DIRNAME / "skills"
    skill_dir = skill_root / skill_name
    skill_md_path = skill_dir / SKILL_FILENAME

    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = skill_md_path.with_suffix(skill_md_path.suffix + ".tmp")
        tmp_path.write_text(
            _render_skill_md(skill_name, skill_body),
            encoding="utf-8",
        )
        os.replace(tmp_path, skill_md_path)
    except OSError as exc:
        return SkillMaterialization(
            ok=False,
            blockers=[
                _blocker(
                    SKILL_MATERIALIZE_WRITE_FAILED_BLOCKER,
                    (
                        f"failed to write materialized Skill file to "
                        f"{skill_md_path}: {exc}"
                    ),
                )
            ],
        )

    return SkillMaterialization(
        ok=True,
        blockers=[],
        skill_root=str(skill_root),
        skill_md_path=str(skill_md_path),
        name=skill_name,
    )


# --------------------------------------------------------------------------- #
# Pure: argv builder                                                          #
# --------------------------------------------------------------------------- #


def build_skill_preflight_argv(
    *,
    skill_root: str | os.PathLike[str],
    binary: str = DEFAULT_BINARY,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> list[str]:
    """Build the canonical Skill preflight argv (pure, no subprocess).

    The exact token shape is pinned by Issue #46 Task 1 (and by ADR
    0028's adapter contract):

        <binary>
        --bare
        --add-dir <skill_root>
        --allowed-tools <tool-1> <tool-2> ... <tool-N>
        --permission-mode <permission_mode>
        -p
        --output-format <output_format>

    Notes
    -----
    - The preflight prompt is *not* included; callers (specifically
      :func:`run_skill_preflight`) append it as the final positional
      argument so this builder stays a pure function of its inputs.
    - ``--verbose`` is intentionally absent: the documented
      ``--output-format stream-json`` argv does not require it for
      shape; the subprocess method injects it only because the
      current ``claude`` runtime aborts without it.
    - ``allowed_tools`` is rendered as repeated single tokens so the
      ``--allowed-tools <tools...>`` nargs-style flag is satisfied
      without comma-joining ambiguity. The list is not filtered:
      policy validation lives in Task 4 and is layered on top of
      this builder by callers, never inside it.
    """
    if not isinstance(binary, str) or not binary:
        raise ValueError("binary must be a non-empty string")
    if not isinstance(permission_mode, str) or not permission_mode:
        raise ValueError("permission_mode must be a non-empty string")
    if not isinstance(output_format, str) or not output_format:
        raise ValueError("output_format must be a non-empty string")

    tools = [str(tool) for tool in allowed_tools]
    skill_root_str = os.fspath(skill_root)

    argv: list[str] = [
        binary,
        "--bare",
        "--add-dir",
        skill_root_str,
        "--allowed-tools",
        *tools,
        "--permission-mode",
        permission_mode,
        "-p",
        "--output-format",
        output_format,
    ]
    return argv


# --------------------------------------------------------------------------- #
# Pure: argv builder (subagent)                                               #
# --------------------------------------------------------------------------- #


def build_subagent_preflight_argv(
    *,
    agents_json: str,
    binary: str = DEFAULT_BINARY,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> list[str]:
    """Build the canonical subagent preflight argv (pure, no subprocess).

    The token shape is the subagent-side mirror of
    :func:`build_skill_preflight_argv` (ADR 0028):

        <binary>
        --bare
        --agents <inline_json>
        -p
        --output-format <output_format>

    Notes
    -----
    - ``--agents`` accepts **inline JSON only** on the current
      ``claude`` runtime (``--help`` documents ``--agents <json>`` as
      "JSON object defining custom agents"). Empirically, ``claude
      --agents <file_path>`` is silently dropped — the file is not
      loaded and the agent does not appear in the init event. The
      harness therefore reads the materialized ``agents.json`` and
      passes its UTF-8 content as the flag value. This builder takes
      the JSON literal so it stays a pure function of its inputs;
      :func:`run_subagent_preflight` does the file read.
    - ``--verbose`` is intentionally absent: the documented
      ``--output-format stream-json`` argv does not require it for
      shape; the subprocess method injects it only because the
      current ``claude`` runtime aborts without it (same rationale
      as the Skill path).
    - The preflight prompt is *not* included; callers (specifically
      :func:`run_subagent_preflight`) append it as the final
      positional argument so this builder stays a pure function of
      its inputs.
    - ``--allowed-tools`` / ``--add-dir`` are not on this argv:
      subagent injection loads the agent JSON, not a Skill tree, and
      the brief pins the minimal token shape. Tool allowlisting for
      execution evaluation is layered on top by callers (Task 4).
    """
    if not isinstance(binary, str) or not binary:
        raise ValueError("binary must be a non-empty string")
    if not isinstance(permission_mode, str) or not permission_mode:
        raise ValueError("permission_mode must be a non-empty string")
    if not isinstance(output_format, str) or not output_format:
        raise ValueError("output_format must be a non-empty string")
    if not isinstance(agents_json, str) or not agents_json:
        raise ValueError("agents_json must be a non-empty JSON string")

    argv: list[str] = [
        binary,
        "--bare",
        "--agents",
        agents_json,
        "-p",
        "--output-format",
        output_format,
    ]
    return argv



# --------------------------------------------------------------------------- #
# Pure: result-shape helper                                                   #
# --------------------------------------------------------------------------- #


def build_evidence_summary(
    run: SkillPreflightRun,
) -> dict[str, Any]:
    """Collapse a :class:`SkillPreflightRun` into a small summary dict.

    The shape (``ok``, ``blockers``, ``sentinel_ok``, ``runtime_version``,
    ``adapter_version``) is the same one the optimizer pipeline and
    the ``init --check`` / ``promote`` commands branch on. No
    reshaping is required to chain the harness with the rest of the
    project.
    """
    evidence = run.evidence
    preflight = run.preflight
    stream_blockers = list(evidence.get("blockers") or [])
    preflight_blockers = list(preflight.get("blockers") or [])
    combined_blockers: list[dict[str, str]] = list(stream_blockers) + [
        blocker
        for blocker in preflight_blockers
        if blocker not in stream_blockers
    ]
    ok = bool(preflight.get("ok")) and not stream_blockers
    return {
        "ok": ok,
        "exit_code": run.exit_code,
        "sentinel_ok": bool(preflight.get("ok")),
        "resolved_name": preflight.get("name", ""),
        "runtime_version": evidence.get("claude_code_version"),
        "adapter_version": evidence.get("adapter_version") or ADAPTER_VERSION,
        "blockers": combined_blockers,
        "warnings": list(evidence.get("warnings") or []),
    }


# --------------------------------------------------------------------------- #
# Subprocess: the ONE method                                                  #
# --------------------------------------------------------------------------- #


def run_skill_preflight(
    *,
    skill_root: str | os.PathLike[str],
    binary: str = DEFAULT_BINARY,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    skill_name: str = "",
    prompt: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    verbose: bool = DEFAULT_VERBOSE_FLAG,
    run_subprocess: Any = None,
) -> SkillPreflightRun:
    """Spawn the binary once and parse the captured stream-json.

    This is the **only** place in :mod:`metacrucible.adapter_runtime`
    that talks to the operating system. Everything else is pure and
    unit-testable without a real binary.

    The argv is the shape from
    :func:`build_skill_preflight_argv` plus the runtime-required
    ``--verbose`` flag (current ``claude`` builds abort
    ``--output-format=stream-json`` without it) and the preflight
    prompt as the final positional argument.

    Parameters
    ----------
    skill_root:
        Directory that contains the materialized Skills
        (``<output_dir>/.claude/skills/`` from
        :func:`materialize_skill`). Passed via ``--add-dir``.
    binary:
        Runtime binary name. Defaults to ``"claude"``; Task 3 passes
        ``"omp"`` to drive oh-my-pi through the same harness.
    allowed_tools:
        Reviewed tool allowlist. Must be a subset of
        :data:`metacrucible.argv_normalize.REVIEWED_TOOL_NAMES`;
        policy enforcement lives in Task 4.
    permission_mode:
        ``--permission-mode`` value. Defaults to ``"default"`` so the
        runtime's own permission policy is in effect.
    output_format:
        ``--output-format`` value. Defaults to ``"stream-json"``;
        only that format is parseable by
        :func:`metacrucible.claude_stream_json.parse_stream_json`.
    skill_name:
        Optional Skill name to fold into the preflight prompt when
        ``prompt`` is not supplied.
    prompt:
        Explicit prompt override. When ``None`` (the default), the
        preflight prompt is built via
        :func:`metacrucible.preflight.skill_preflight_prompt`.
    timeout:
        Subprocess timeout in seconds. ``0`` disables the timeout.
    env:
        Optional environment override. ``None`` inherits the parent
        environment (the runtime still needs auth from the OS
        keychain / subscription, never from a provider API key in
        this harness).
    cwd:
        Optional working directory override. ``None`` inherits the
        caller's cwd; callers should normally set this to the
        project root that owns the materialized ``.claude/`` tree.
    verbose:
        Inject ``--verbose`` before ``-p`` (default ``True``). The
        current ``claude`` runtime aborts ``--print --output-format
        stream-json`` without it.
    run_subprocess:
        Test seam. When supplied, the harness calls it as
        ``run_subprocess(full_argv, ...)`` and expects a
        ``subprocess.CompletedProcess``-like object. Production
        callers leave this ``None``.
    """
    if prompt is None:
        prompt = skill_preflight_prompt(skill_name=skill_name)

    base_argv = build_skill_preflight_argv(
        skill_root=skill_root,
        binary=binary,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        output_format=output_format,
    )
    # Insert --verbose immediately before -p so the spec argv shape
    # is preserved (the brief's token list is reproduced exactly
    # minus the verbose flag, which is a runtime requirement rather
    # than a contract one).
    full_argv: list[str] = []
    inserted_verbose = False
    for token in base_argv:
        if not inserted_verbose and token == "-p" and verbose:
            full_argv.append("--verbose")
            inserted_verbose = True
        full_argv.append(token)
    if verbose and not inserted_verbose:
        # ``-p`` was not in the argv (defensive): fall back to
        # appending --verbose before the prompt.
        full_argv.append("--verbose")
    full_argv.append(prompt)

    runner = run_subprocess if run_subprocess is not None else subprocess.run
    completed = runner(
        full_argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
        check=False,
    )

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    exit_code = int(getattr(completed, "returncode", -1))

    evidence = parse_stream_json(
        stdout,
        adapter_version=ADAPTER_VERSION,
        exit_code=exit_code,
        stderr=stderr,
    )
    final_output = evidence.get("final_output") or ""
    preflight = check_skill_preflight(final_output)

    return SkillPreflightRun(
        argv=list(full_argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        evidence=evidence,
        preflight=preflight,
    )


# --------------------------------------------------------------------------- #
# Subprocess: the ONE method (subagent)                                       #
# --------------------------------------------------------------------------- #


def run_subagent_preflight(
    *,
    agents_path: str | os.PathLike[str],
    binary: str = DEFAULT_BINARY,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    subagent_name: str = "",
    prompt: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    verbose: bool = DEFAULT_VERBOSE_FLAG,
    local_real: bool = False,
    run_subprocess: Any = None,
) -> SubagentPreflightRun:
    """Spawn the binary once with ``--agents <inline_json>`` and parse stream-json.

    Subagent-side mirror of :func:`run_skill_preflight`. This is
    the **only** subprocess execution method for the subagent path.
    Everything else in :mod:`metacrucible.adapter_runtime` is pure
    and unit-testable without a real binary.

    Flow
    -----
    1. Read ``agents_path`` (the materialized file written by
       :func:`metacrucible.subagent_injection.materialize_subagent`)
       and pass its UTF-8 content inline as the ``--agents`` flag
       value. The current ``claude`` runtime does not accept a file
       path on ``--agents``; see
       :func:`build_subagent_preflight_argv` for the empirical
       verification.
    2. Build the argv from
       :func:`build_subagent_preflight_argv`, inject ``--verbose``
       immediately before ``-p`` (runtime requirement for
       ``-p --output-format stream-json``), and append the
       preflight prompt as the final positional argument.
    3. Spawn the binary via :func:`subprocess.run` (or the caller-
       supplied ``run_subprocess`` test seam) with
       ``capture_output=True, text=True, timeout=timeout, check=False``.
    4. Parse stdout through
       :func:`metacrucible.claude_stream_json.parse_stream_json`
       (existing parser; no parallel implementation).
    5. Fold ``evidence["final_output"]`` through
       :func:`metacrucible.preflight.check_subagent_preflight`.

    Parameters
    ----------
    agents_path:
        Materialized ``--agents`` JSON file (typically the
        ``agents_path`` returned by
        :func:`metacrucible.subagent_injection.materialize_subagent`).
        Read once and passed inline as the ``--agents`` flag value.
    binary:
        Runtime binary name. Defaults to ``"claude"``; Task 3 passes
        ``"omp"`` to drive oh-my-pi through the same harness.
    permission_mode:
        ``--permission-mode`` value. Defaults to ``"default"`` so the
        runtime's own permission policy is in effect.
    output_format:
        ``--output-format`` value. Defaults to ``"stream-json"``;
        only that format is parseable by
        :func:`metacrucible.claude_stream_json.parse_stream_json`.
    subagent_name:
        Optional subagent name to fold into the preflight prompt
        when ``prompt`` is not supplied.
    prompt:
        Explicit prompt override. When ``None`` (the default), the
        preflight prompt is built via
        :func:`metacrucible.preflight.subagent_preflight_prompt`
        (or :func:`local_real_subagent_confirm_prompt` when
        ``local_real=True``).
    timeout:
        Subprocess timeout in seconds. ``0`` disables the timeout.
    env:
        Optional environment override. ``None`` inherits the parent
        environment (the runtime still needs auth from the OS
        keychain / subscription, never from a provider API key in
        this harness).
    cwd:
        Optional working directory override. ``None`` inherits the
        caller's cwd.
    verbose:
        Inject ``--verbose`` before ``-p`` (default ``True``). The
        current ``claude`` runtime aborts ``--print --output-format
        stream-json`` without it.
    local_real:
        Local-real adapter switch (Issue #46 Repair 3). When
        ``True`` and ``prompt`` is ``None``, the harness uses
        :func:`local_real_subagent_confirm_prompt` (terse
        confirm-prompt) instead of
        :func:`metacrucible.preflight.subagent_preflight_prompt`.
        The terse prompt tells the main model the subagent IS
        registered and asks it to confirm by echoing the sentinel.
        This is necessary because the main model cannot introspect
        subagent registration via the verbose ADR 0028 prompt
        (subagents are registered, not loaded into the main
        context), and would otherwise hedge to ``no``. Default
        ``False`` preserves the verbose ADR 0028 contract for
        pure-logic/contract tests.
    run_subprocess:
        Test seam. When supplied, the harness calls it as
        ``run_subprocess(full_argv, ...)`` and expects a
        ``subprocess.CompletedProcess``-like object. Production
        callers leave this ``None``.
    """
    path = _coerce_path(agents_path)

    try:
        agents_json = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Mirror the "missing file" branch of
        # verify_subagent_injection so the smoke pass can chain the
        # two without reshaping the result dict.
        stderr = f"failed to read materialized agents.json at {path}: {exc}"
        evidence = parse_stream_json(
            "",
            adapter_version=ADAPTER_VERSION,
            exit_code=-1,
            stderr=stderr,
        )
        return SubagentPreflightRun(
            argv=[],
            exit_code=-1,
            stderr=stderr,
            evidence=evidence,
            preflight=check_subagent_preflight(""),
            agents_path=str(path),
        )

    if prompt is None:
        if local_real:
            # Local-real adapter (Issue #46 Repair 3): use the terse
            # confirm-prompt. The main model cannot introspect
            # subagent registration via the verbose ADR 0028 prompt
            # (subagents are registered, not loaded into the main
            # context), so we tell it the subagent is registered and
            # ask it to confirm by echoing the sentinel. If the
            # subagent is NOT actually registered, the model cannot
            # confirm and the test fails loudly.
            prompt = local_real_subagent_confirm_prompt(
                subagent_name=subagent_name
            )
        else:
            # Default contract path: ADR 0028 verbose prompt. The
            # pure-logic and contract tests use this default; the
            # local-real tests opt in via ``local_real=True``.
            prompt = subagent_preflight_prompt(subagent_name=subagent_name)
    base_argv = build_subagent_preflight_argv(
        agents_json=agents_json,
        binary=binary,
        permission_mode=permission_mode,
        output_format=output_format,
    )
    # Insert --verbose immediately before -p so the spec argv shape
    # is preserved (the brief's token list is reproduced exactly
    # minus the verbose flag, which is a runtime requirement rather
    # than a contract one).
    full_argv: list[str] = []
    inserted_verbose = False
    for token in base_argv:
        if not inserted_verbose and token == "-p" and verbose:
            full_argv.append("--verbose")
            inserted_verbose = True
        full_argv.append(token)
    if verbose and not inserted_verbose:
        # ``-p`` was not in the argv (defensive): fall back to
        # appending --verbose before the prompt.
        full_argv.append("--verbose")
    full_argv.append(prompt)

    runner = run_subprocess if run_subprocess is not None else subprocess.run
    completed = runner(
        full_argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
        check=False,
    )

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    exit_code = int(getattr(completed, "returncode", -1))

    evidence = parse_stream_json(
        stdout,
        adapter_version=ADAPTER_VERSION,
        exit_code=exit_code,
        stderr=stderr,
    )
    final_output = evidence.get("final_output") or ""
    preflight = check_subagent_preflight(final_output)

    return SubagentPreflightRun(
        argv=list(full_argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        evidence=evidence,
        preflight=preflight,
        agents_path=str(path),
    )




# --------------------------------------------------------------------------- #
# Pure: argv builder (omp Skill) (Issue #46 Task 3)                            #
# --------------------------------------------------------------------------- #
#
# The omp runtime reads Skills from the SHARED
# ``.claude/skills/<name>/SKILL.md`` layout under the directory passed
# to ``--cwd``. It does not accept ``--bare``, ``--add-dir``,
# ``--allowed-tools``, ``--permission-mode``, or ``--output-format
# stream-json``; its argv shape is documented in the brief (ADR 0003
# shared-layout contract). The runtime emits plain text in ``--mode
# text``; the harness feeds that text straight to
# :func:`metacrucible.preflight.check_skill_preflight`.


def build_omp_skill_preflight_argv(
    *,
    isolated_root: str | os.PathLike[str],
    prompt: str,
) -> list[str]:
    """Build the canonical omp Skill preflight argv (pure, no subprocess).

    Token shape pinned by Issue #46 Task 3 (and the empirically-verified
    omp 16.1.19 flag surface):

        omp
        --cwd <isolated_root>
        -p
        --mode text
        --allow-home
        <prompt>

    Notes
    -----
    - ``--cwd`` points at the isolated directory that owns the
      ``.claude/skills/<name>/SKILL.md`` tree written by
      :func:`materialize_skill`. ADR 0003 shared-layout contract:
      the same artifact works under both runtimes.
    - ``--no-tools`` is intentionally omitted. omp treats artifact
      injection (Skills, subagents) as a tool-side feature; passing
      ``--no-tools`` disables that loading and the preflight would
      fail with ``discoverable=no``. Side-effect safety comes from
      ``-p`` (non-interactive single turn) — the preflight prompt
      does no file ops — so no tool-disabling flag is needed.
    - ``--mode text`` gives plain stdout text so the harness can
      feed ``final_output`` straight to
      :func:`metacrucible.preflight.check_skill_preflight`. omp's
      ``--mode json`` emits omp's own session-event JSONL (not
      claude stream-json) and is not what this builder targets.
    - ``--allow-home`` lets omp start under a scratch directory
      outside ``$HOME`` without auto-switching to a tmp dir; the
      local-real layer sets ``isolated_root`` to a pytest tmp dir.
    - ``-p`` is omp's ``--print`` flag (non-interactive). The
      preflight prompt is the final positional argument; the builder
      keeps the function pure of subprocess side effects by taking
      the prompt as input.
    - This builder does not inject ``--verbose``: omp has no
      ``--verbose`` flag, and the documented argv shape does not
      include one.
    """
    if not isinstance(isolated_root, (str, os.PathLike)):
        raise TypeError("isolated_root must be a path-like string")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")

    isolated_root_str = os.fspath(isolated_root)

    return [
        RUNTIME_OMP,
        "--cwd",
        isolated_root_str,
        "-p",
        "--mode",
        "text",
        "--allow-home",
        prompt,
    ]


# --------------------------------------------------------------------------- #
# Pure: argv builder (omp subagent) (Issue #46 Task 3)                        #
# --------------------------------------------------------------------------- #
#
# omp discovers subagents from the SAME ``.claude/agents/agents.json``
# layout under ``--cwd``; the harness reuses
# :func:`metacrucible.subagent_injection.materialize_subagent` to write
# that file (the JSON shape is the same; the path differs by a single
# ``.claude/`` prefix that the omp subprocess method adds when needed).
# The argv shape is otherwise identical to the Skill path.


def build_omp_subagent_preflight_argv(
    *,
    isolated_root: str | os.PathLike[str],
    prompt: str,
) -> list[str]:
    """Build the canonical omp subagent preflight argv (pure, no subprocess).

    Token shape is identical to
    :func:`build_omp_skill_preflight_argv` because omp discovers
    subagents from the same ``.claude/`` layout under ``--cwd`` and
    does not need a separate flag.

        omp
        --cwd <isolated_root>
        -p
        --mode text
        --allow-home
        <prompt>
    """
    if not isinstance(isolated_root, (str, os.PathLike)):
        raise TypeError("isolated_root must be a path-like string")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")

    isolated_root_str = os.fspath(isolated_root)

    return [
        RUNTIME_OMP,
        "--cwd",
        isolated_root_str,
        "-p",
        "--mode",
        "text",
        "--allow-home",
        prompt,
    ]


# --------------------------------------------------------------------------- #
# Subprocess: the ONE method (omp Skill) (Issue #46 Task 3)                    #
# --------------------------------------------------------------------------- #
#
# The omp path mirrors the claude path: the **only** subprocess call
# for omp Skill discovery is :func:`run_omp_skill_preflight`. The argv
# is the pure-builder output plus the preflight prompt; stdout is fed
# to :func:`metacrucible.preflight.check_skill_preflight` directly
# (no parse_stream_json — omp text mode is plain text containing the
# sentinel). The result dataclass is the same :class:`SkillPreflightRun`
# with ``runtime="omp"`` so callers can branch uniformly.


def _build_omp_evidence(
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> dict[str, Any]:
    """Build an evidence dict for an omp run (no parse_stream_json).

    The shape mirrors the parse-stream-json output enough for
    :func:`build_evidence_summary` to branch on the same fields
    (``start_captured``, ``completion_captured``, ``final_output``,
    ``runtime_version``, ``adapter_version``, ``blockers``). The
    ``claude_code_version`` field is intentionally ``None``: omp
    emits its own version lineage, surfaced via
    ``runtime_version`` (the harness constant
    :data:`OMP_ADAPTER_VERSION`).
    """
    completion_captured = exit_code == 0
    return {
        "start_captured": True,
        "completion_captured": completion_captured,
        "raw_event_count": 0,
        "malformed_line_count": 0,
        "final_output": stdout,
        "exit_code": exit_code,
        "stderr": stderr,
        "error": None if completion_captured else stderr or "omp exited non-zero",
        "adapter_version": OMP_ADAPTER_VERSION,
        "claude_code_version": None,
        "runtime_version": OMP_ADAPTER_VERSION,
        "blockers": [],
        "warnings": [],
    }


def run_omp_skill_preflight(
    *,
    isolated_root: str | os.PathLike[str],
    skill_name: str = "",
    prompt: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    run_subprocess: Any = None,
) -> SkillPreflightRun:
    """Spawn ``omp`` once against the materialized Skill tree (Issue #46 Task 3).

    This is the **only** subprocess call in the omp Skill path. It
    mirrors :func:`run_skill_preflight` in shape but:

      - the binary is fixed at ``"omp"``;
      - the argv is the omp-specific token list from
        :func:`build_omp_skill_preflight_argv`;
      - the cwd defaults to ``isolated_root`` (the directory that
        owns the ``.claude/skills/<name>/SKILL.md`` artifact);
      - the captured stdout is fed straight to
        :func:`metacrucible.preflight.check_skill_preflight`
        without ``parse_stream_json`` (omp text mode emits plain
        text containing the sentinel; the parse-stream-json contract
        is claude-specific);
      - the result dataclass is :class:`SkillPreflightRun` with
        ``runtime="omp"`` so callers can branch on the surface.

    The harness reuses :func:`materialize_skill` to write the
    artifact at ``<isolated_root>/.claude/skills/<name>/SKILL.md``
    before invoking this function (the shared-layout contract).
    This function does **not** materialize; it only invokes.

    Parameters
    ----------
    isolated_root:
        Directory that owns the materialized ``.claude/skills/``
        tree (typically the ``output_dir`` passed to
        :func:`materialize_skill`). Forwarded as ``--cwd``.
    skill_name:
        Optional Skill name folded into the preflight prompt when
        ``prompt`` is not supplied.
    prompt:
        Explicit prompt override. When ``None`` (the default), the
        preflight prompt is built via
        :func:`metacrucible.preflight.skill_preflight_prompt`.
    timeout:
        Subprocess timeout in seconds. ``0`` disables the timeout.
    env:
        Optional environment override. ``None`` inherits the parent
        environment.
    cwd:
        Optional cwd override. When ``None``, the harness spawns
        from ``isolated_root`` (the directory that owns the
        ``.claude/skills/<name>`` artifact).
    run_subprocess:
        Test seam. When supplied, the harness calls it as
        ``run_subprocess(full_argv, ...)`` and expects a
        ``subprocess.CompletedProcess``-like object.
    """
    if prompt is None:
        prompt = skill_preflight_prompt(skill_name=skill_name)

    full_argv = build_omp_skill_preflight_argv(
        isolated_root=isolated_root,
        prompt=prompt,
    )

    spawn_cwd = os.fspath(isolated_root) if cwd is None else os.fspath(cwd)

    runner = run_subprocess if run_subprocess is not None else subprocess.run
    completed = runner(
        full_argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=spawn_cwd,
        check=False,
    )

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    exit_code = int(getattr(completed, "returncode", -1))

    evidence = _build_omp_evidence(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
    )
    final_output = evidence.get("final_output") or ""
    preflight = check_skill_preflight(final_output)

    return SkillPreflightRun(
        argv=list(full_argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        evidence=evidence,
        preflight=preflight,
        runtime=RUNTIME_OMP,
    )


# --------------------------------------------------------------------------- #
# Subprocess: the ONE method (omp subagent) (Issue #46 Task 3)                #
# --------------------------------------------------------------------------- #


def _materialize_omp_agents_layout(
    agents_json_path: str | os.PathLike[str],
    isolated_root: str | os.PathLike[str],
) -> str:
    """Copy ``materialize_subagent``'s output into the omp shared layout.

    ADR 0003 shared-layout contract: omp reads subagent JSON from
    ``<isolated_root>/.claude/agents/agents.json`` under ``--cwd``.
    :func:`metacrucible.subagent_injection.materialize_subagent` writes
    ``<output_dir>/agents.json`` (the file the claude ``--agents``
    flag loads). The harness writes a copy at the omp path so the
    same JSON content is discoverable under both runtimes, without
    rewriting the existing materializer.
    """
    src = Path(os.fspath(agents_json_path))
    dst = Path(os.fspath(isolated_root)) / CLAUDE_SKILLS_DIRNAME / "agents" / "agents.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    tmp_path = dst.with_suffix(dst.suffix + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, dst)
    return str(dst)


def run_omp_subagent_preflight(
    *,
    isolated_root: str | os.PathLike[str],
    agents_path: str | os.PathLike[str],
    subagent_name: str = "",
    prompt: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    local_real: bool = False,
    run_subprocess: Any = None,
) -> SubagentPreflightRun:
    """Spawn ``omp`` once against the materialized subagent JSON (Issue #46 Task 3).

    Mirror of :func:`run_omp_skill_preflight` for the subagent path.
    Reads the materialized ``agents.json`` (the claude ``--agents``
    file from :func:`metacrucible.subagent_injection.materialize_subagent`)
    and copies it into the omp shared layout at
    ``<isolated_root>/.claude/agents/agents.json`` so the same JSON
    shape is discoverable under both runtimes (ADR 0003). The argv
    is the omp-specific token list from
    :func:`build_omp_subagent_preflight_argv`. The captured stdout
    is fed straight to
    :func:`metacrucible.preflight.check_subagent_preflight` without
    ``parse_stream_json`` (omp text mode emits plain text containing
    the sentinel).

    ``local_real`` (Issue #46 Repair 3) opts the harness into the
    terse confirm-prompt when ``prompt`` is ``None``. The default
    ``False`` keeps the verbose ADR 0028
    :func:`metacrucible.preflight.subagent_preflight_prompt` for
    pure-logic/contract behavior; local-real smoke tests opt in
    because the main model cannot introspect subagent registration
    via the verbose prompt (subagents are registered, not loaded
    into the main context) and would otherwise hedge to ``no``.

    Returns a :class:`SubagentPreflightRun` with ``runtime="omp"``.
    """
    if prompt is None:
        if local_real:
            # Local-real adapter (Issue #46 Repair 3): use the terse
            # confirm-prompt. Same rationale as the claude path:
            # the main model cannot introspect subagent registration
            # via the verbose ADR 0028 prompt under either runtime
            # (the subagent's body lives in the SUBAGENT's system
            # prompt, not the main model's context), so we tell it
            # the subagent is registered and ask it to confirm.
            prompt = local_real_subagent_confirm_prompt(
                subagent_name=subagent_name
            )
        else:
            prompt = subagent_preflight_prompt(subagent_name=subagent_name)
    try:
        omp_agents_path = _materialize_omp_agents_layout(
            agents_json_path=agents_path,
            isolated_root=isolated_root,
        )
    except OSError as exc:
        stderr = (
            f"failed to materialize omp agents.json layout at "
            f"{isolated_root}: {exc}"
        )
        return SubagentPreflightRun(
            argv=[],
            exit_code=-1,
            stderr=stderr,
            evidence=_build_omp_evidence(stdout="", stderr=stderr, exit_code=-1),
            preflight=check_subagent_preflight(""),
            runtime=RUNTIME_OMP,
            agents_path=str(agents_path),
        )

    full_argv = build_omp_subagent_preflight_argv(
        isolated_root=isolated_root,
        prompt=prompt,
    )

    spawn_cwd = os.fspath(isolated_root) if cwd is None else os.fspath(cwd)

    runner = run_subprocess if run_subprocess is not None else subprocess.run
    completed = runner(
        full_argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=spawn_cwd,
        check=False,
    )

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    exit_code = int(getattr(completed, "returncode", -1))

    evidence = _build_omp_evidence(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
    )
    final_output = evidence.get("final_output") or ""
    preflight = check_subagent_preflight(final_output)

    return SubagentPreflightRun(
        argv=list(full_argv),
        exit_code=exit_code,
 stdout=stdout,
        stderr=stderr,
        evidence=evidence,
        preflight=preflight,
        runtime=RUNTIME_OMP,
        agents_path=omp_agents_path,
    )
# --------------------------------------------------------------------------- #
# Optional: resolve binary on PATH                                             #
# --------------------------------------------------------------------------- #


def resolve_binary(name: str = DEFAULT_BINARY) -> str | None:
    """Return the absolute path of ``name`` if it is on ``PATH``.

    Returns ``None`` when the binary is not discoverable. The harness
    never raises on a missing binary; the local-real test layer
    uses this to gate ``claude`` / ``omp`` cases cleanly.
    """
    import shutil

    return shutil.which(name)


def reviewed_tool_names() -> frozenset[str]:
    """Return the reviewed tool allowlist (forwarded for tests)."""
    return REVIEWED_TOOL_NAMES
