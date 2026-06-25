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
from .preflight import check_skill_preflight, skill_preflight_prompt

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
    "SKILL_MATERIALIZE_NAME_MISSING_BLOCKER",
    "SKILL_MATERIALIZE_NAME_INVALID_BLOCKER",
    "SKILL_MATERIALIZE_BODY_MISSING_BLOCKER",
    "SKILL_MATERIALIZE_WRITE_FAILED_BLOCKER",
    "SkillMaterialization",
    "SkillPreflightRun",
    "materialize_skill",
    "build_skill_preflight_argv",
    "run_skill_preflight",
    "build_evidence_summary",
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
    """

    argv: list[str] = field(default_factory=list)
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    preflight: dict[str, Any] = field(default_factory=dict)


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
