"""Deterministic rule check engine (Issue #14).

Pins the rule-check engine contract (ADR 0026, Issue #14):

  * Deterministic checks run in per-case workspaces, not in the
    coordinator's cwd. A per-case workspace is a subpath of the
    parent workspace the engine was given; the check's resolved cwd
    is recorded on the result so a reviewer can audit the boundary.
  * Target boundary and check boundary are separate dataclass
    types. The :class:`TargetBoundary` mirrors the shape of the
    existing ``execution_boundary`` mapping (ADR 0028) and the
    :class:`CheckBoundary` carries the check-side commands. The
    check validator refuses a :class:`TargetBoundary` (and a raw
    dict) in place of a :class:`CheckBoundary`, so the two cannot be
    silently conflated.
  * A check that invokes a shell-like binary directly is a
    "complex shell" check and is BLOCKED unless the boundary cites
    a reviewed wrapper file. The wrapper must be a real file in a
    directory that contains the
    :data:`REVIEWED_WRAPPER_MARKER` sentinel.

Public surface
--------------

* :data:`EXPECTED_BLOCKERS` — machine-stable blocker id mapping.
* :data:`REVIEWED_WRAPPER_MARKER` — sentinel filename for reviewed
  wrapper directories.
* :data:`SHELL_BINARIES` — vocabulary of ``argv[0]`` names that
  count as complex shell.
* :class:`CheckBoundary` — check-side boundary dataclass.
* :class:`TargetBoundary` — target-side boundary dataclass
  (mirrors ``execution_boundary`` shape; not a substitute for the
  ADR 0028 normalizer, only a type-distinct representation).
* :func:`validate_check_boundary` — accept a :class:`CheckBoundary`
  and return a result dict; reject a :class:`TargetBoundary` /
  raw dict with the ``check-boundary-type-mismatch`` blocker.
* :func:`plan_check_workspace` — return a per-case workspace path
  under the parent for the given case_id.
* :func:`execute_check` — run a single check (by index) inside the
  per-case workspace; record the resolved cwd.

Result shape
------------

All validators return a dict of the same shape used by
:mod:`metacrucible.argv_normalize` and
:mod:`metacrucible.workspace_isolation`:

  - ``ok`` (bool) — ``True`` iff validation passes.
  - ``blockers`` (list[dict]) — empty when ``ok`` is ``True``;
    otherwise each entry is ``{"id": <blocker_id>, "message":
    <human>}``.
  - ``workspace`` (Path) — only on
    :func:`plan_check_workspace` success: the per-case path.
  - ``actual_cwd`` (str) — only on :func:`execute_check` success:
    the resolved cwd the check observed.
  - ``coordinator_cwd`` (str) — only on :func:`execute_check`
    success: the coordinator's cwd at call time, recorded so a
    reviewer can confirm ``actual_cwd != coordinator_cwd``.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from . import argv_normalize


__all__ = [
    "EXPECTED_BLOCKERS",
    "REVIEWED_WRAPPER_MARKER",
    "SHELL_BINARIES",
    "CheckBoundary",
    "TargetBoundary",
    "validate_check_boundary",
    "plan_check_workspace",
    "execute_check",
]

# --------------------------------------------------------------------------- #
# Stable blocker ids                                                          #
# --------------------------------------------------------------------------- #
#
# Machine contract: the optimizer pipeline, the rule-check runner,
# and downstream automation branch on these exact strings. Adding
# a new id is a contract change; renaming an existing id is a
# breaking change and must be paired with a migration plan.

CHECK_BOUNDARY_TYPE_BLOCKER: str = "rule-check-boundary-type-mismatch"
CHECK_COMPLEX_SHELL_BLOCKER: str = "rule-check-complex-shell-requires-wrapper"
CHECK_WRAPPER_MISSING_BLOCKER: str = "rule-check-wrapper-missing"
CHECK_WRAPPER_NOT_REVIEWED_BLOCKER: str = "rule-check-wrapper-not-reviewed"
CHECK_WORKSPACE_INVALID_BLOCKER: str = "rule-check-workspace-invalid"

EXPECTED_BLOCKERS: dict[str, str] = {
    "check_boundary_type": CHECK_BOUNDARY_TYPE_BLOCKER,
    "check_complex_shell": CHECK_COMPLEX_SHELL_BLOCKER,
    "check_wrapper_missing": CHECK_WRAPPER_MISSING_BLOCKER,
    "check_wrapper_not_reviewed": CHECK_WRAPPER_NOT_REVIEWED_BLOCKER,
    "check_workspace_invalid": CHECK_WORKSPACE_INVALID_BLOCKER,
}

# --------------------------------------------------------------------------- #
# Reviewed-wrapper sentinel                                                    #
# --------------------------------------------------------------------------- #
#
# A complex-shell check must invoke a wrapper that lives in a
# directory containing this marker. The marker is a stable string
# (not a JSON file) so any reviewer can confirm reviewedness with
# ``ls wrappers/REVIEWED_WRAPPER_MARKER``; the contents of the
# marker are intentionally free-form (typically a one-line note
# pointing at the ADR / issue).

REVIEWED_WRAPPER_MARKER: str = "REVIEWED_WRAPPER_MARKER"

# --------------------------------------------------------------------------- #
# Shell-like binaries (Issue #14 AC3)                                         #
# --------------------------------------------------------------------------- #
#
# A check whose ``argv[0]`` is one of these names is itself a
# complex-shell invocation. The engine refuses such argvs unless
# the boundary cites a reviewed wrapper, so the check becomes
# "run this reviewed wrapper via bash" — a simple argv.

SHELL_BINARIES: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "fish", "ksh", "csh", "tcsh", "dash"}
)

# --------------------------------------------------------------------------- #
# Boundary types                                                              #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TargetBoundary:
    """Target-side execution boundary (ADR 0028 shape).

    Mirrors the ``execution_boundary`` mapping the runtime adapter
    normalizes. Carries only target-side fields
    (``allowed_tools``, ``target_commands``, ``strict_read_paths``)
    so a caller cannot smuggle check-side intent through a
    target-side boundary.

    This dataclass is *distinct* from :class:`CheckBoundary`; the
    check validator refuses a :class:`TargetBoundary` in place of a
    :class:`CheckBoundary` with the ``check-boundary-type-mismatch``
    blocker id.
    """

    allowed_tools: tuple[str, ...] = ()
    target_commands: tuple[tuple[str, ...], ...] = ()
    strict_read_paths: bool = False

@dataclass(frozen=True)
class CheckBoundary:
    """Check-side execution boundary (Issue #14).

    A :class:`CheckBoundary` carries a tuple of conservative
    argv arrays. ``commands[i]`` is the argv the engine will pass
    to ``subprocess.run`` for case index ``i``. ``wrapper`` is the
    path to a reviewed wrapper file; it is required when any
    command's ``argv[0]`` is a shell-like binary
    (see :data:`SHELL_BINARIES`).

    Attributes
    ----------
    commands:
        Tuple of argv tuples. Each argv is a conservative
        shell-metachar-free array of strings.
    wrapper:
        Optional path to a reviewed wrapper file. When set, the
        engine validates the path exists and lives in a reviewed
        directory. Required for any check that invokes a
        shell-like binary directly.
    """

    commands: tuple[tuple[str, ...], ...]
    wrapper: str | None = None

# --------------------------------------------------------------------------- #
# Internal: result helpers                                                    #
# --------------------------------------------------------------------------- #

def _blocker(blocker_id: str, message: str) -> dict[str, str]:
    """Return a single ``{id, message}`` blocker entry."""
    return {"id": blocker_id, "message": message}

def _ok_result(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a clean ``ok=True`` result, optionally with extra payload."""
    result: dict[str, Any] = {"ok": True, "blockers": []}
    if extra:
        result.update(extra)
    return result

def _blocked_result(
    blockers: list[dict[str, str]],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a blocked ``ok=False`` result, optionally with extra payload."""
    result: dict[str, Any] = {"ok": False, "blockers": list(blockers)}
    if extra:
        result.update(extra)
    return result

# --------------------------------------------------------------------------- #
# Internal: case-id and path validation                                       #
# --------------------------------------------------------------------------- #

def _is_safe_case_id(case_id: Any) -> bool:
    """True if ``case_id`` is a flat, non-traversal relative identifier.

    A per-case workspace is built as ``parent / case_id`` (with a
    stable ``cases/`` prefix applied by :func:`plan_check_workspace`).
    The case_id is therefore a path segment: no separators, no
    parent-directory references, no leading dots, no interior
    whitespace, no ASCII control characters, and not a reserved
    name. The control-char and interior-whitespace checks are
    review hardening (Issue #14 follow-up): a NUL would crash
    ``Path.resolve()`` and an interior space would let a path
    with a literal space slip into the per-case workspace tree.
    """
    if not isinstance(case_id, str) or not case_id:
        return False
    if case_id != case_id.strip():
        return False
    if "/" in case_id or "\\" in case_id:
        return False
    if any(c.isspace() for c in case_id):
        return False
    # ASCII control chars (NUL, DEL, etc.) are not safe in a
    # path segment: NUL terminates a C string and the others
    # break a reviewer's tooling. Reject any character below
    # 0x20 or equal to 0x7F.
    for ch in case_id:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    if case_id in {".", ".."}:
        return False
    # Leading dots are reserved for hidden files / parent refs.
    if case_id.startswith("."):
        return False
    return True

def _resolve_wrapper(wrapper: str, base: Path | None) -> Path:
    """Resolve a wrapper path relative to ``base`` (or absolute)."""
    wrapper_path = Path(wrapper)
    if not wrapper_path.is_absolute() and base is not None:
        wrapper_path = (Path(base) / wrapper_path).resolve()
    else:
        wrapper_path = wrapper_path.resolve()
    return wrapper_path

def _wrapper_is_reviewed(wrapper_path: Path) -> bool:
    """True if ``wrapper_path`` lives in a directory containing the marker.

    The marker must be a real file, not a symlink: a symlinked
    marker would let a wrapper dir claim reviewed status by
    pointing at a file outside the wrapper dir (or at a file
    inside a deniable location). :meth:`Path.is_file` follows
    symlinks, so the symlink case is rejected explicitly here
    (Issue #14 AC3 review hardening).
    """
    parent = wrapper_path.parent
    if not parent.is_dir():
        return False
    marker = parent / REVIEWED_WRAPPER_MARKER
    if marker.is_symlink():
        return False
    return marker.is_file()

# --------------------------------------------------------------------------- #
# Internal: per-command gating                                                #
# --------------------------------------------------------------------------- #

def _command_is_complex_shell(argv: Sequence[str]) -> bool:
    """True if ``argv`` is a shell-like invocation (``bash``, ``sh``, ...)."""
    if not argv:
        return False
    head = argv[0]
    if not isinstance(head, str):
        return False
    # Match by basename so ``/bin/bash`` and ``bash`` both count.
    return Path(head).name in SHELL_BINARIES

def _argv_token_uses_metachar(token: str) -> bool:
    """True if ``token`` contains a shell metacharacter.

    Mirrors :data:`metacrucible.argv_normalize._SHELL_METACHARS` so
    a check command cannot smuggle a pipe, redirect, subshell, or
    background operator through argv. Embedded ``/..`` segments
    and leading ``..`` / ``/`` / ``~`` prefixes are *deliberately
    allowed* on the check side: checks must be able to invoke
    system tools (e.g. ``/usr/bin/python3``) and reviewed wrappers
    by absolute path, and the path-traversal rule that protects
    the target boundary is not the right defense for a check.
    The reviewed-wrapper gate is the check-side analog.
    """
    for ch in token:
        if ch in _SHELL_METACHARS:
            return True
    return False

def _argv_token_uses_glob(token: str) -> bool:
    """True if ``token`` contains a glob wildcard.

    Mirrors the wildcard rule from
    :mod:`metacrucible.argv_normalize`: a check command must not
    use a wildcard because the engine executes argv verbatim and
    the resolved files would be untraceable. A wrapper file is
    the only safe way to express a multi-file check.
    """
    if "*" in token or "?" in token:
        return True
    if "[" in token and "]" in token:
        return True
    return False

#: Shell metacharacters that a check argv must never contain.
#: Aliased to :data:`metacrucible.argv_normalize._SHELL_METACHARS`
#: so the check engine stays aligned with the target boundary's
#: safety vocabulary; the alias (not a copy) keeps the contract
#: from drifting if the target boundary's vocabulary grows. The
#: aliasing is the review hardening for Issue #14 (a previous
#: copy could silently diverge).
_SHELL_METACHARS: frozenset[str] = argv_normalize._SHELL_METACHARS

def _gate_check_command(
    argv: Sequence[str],
    *,
    wrapper: str | None,
    base: Path | None,
) -> list[dict[str, str]]:
    """Return a list of blockers for a single check command's argv.

    The gate runs three checks in order so a single failure mode is
    reported per command:

      1. Argv-shape safety: every token must be a string, free of
         shell metachars and globs. The check engine does NOT
         enforce the target-side path-traversal rule, because a
         check must be able to invoke system tools and reviewed
         wrappers by absolute path; the reviewed-wrapper gate is
         the check-side analog (Issue #14 AC3).
      2. If the argv is a shell-like binary, the wrapper gate
         fires: missing wrapper -> ``complex-shell-requires-wrapper``;
         unresolvable wrapper path -> ``wrapper-missing``;
         wrapper outside a reviewed dir -> ``wrapper-not-reviewed``.
      3. If the argv is not a shell-like binary, the wrapper
         attribute is ignored (a simple check is not required to
         cite a wrapper).
    """
    blockers: list[dict[str, str]] = []
    # 1. Argv-shape safety: metachar / glob only (no path-traversal
    #    gate; see the docstring above for the rationale).
    for token_index, token in enumerate(argv):
        if not isinstance(token, str):
            blockers.append(
                _blocker(
                    CHECK_COMPLEX_SHELL_BLOCKER,
                    (
                        f"check command token[{token_index}] must be a "
                        f"string; got {type(token).__name__} (Issue #14 AC3)"
                    ),
                )
            )
            return blockers
        if _argv_token_uses_metachar(token):
            blockers.append(
                _blocker(
                    CHECK_COMPLEX_SHELL_BLOCKER,
                    (
                        f"check command token[{token_index}]={token!r} "
                        "contains a shell metacharacter; a check command "
                        "must be a conservative argv (Issue #14 AC3)"
                    ),
                )
            )
            return blockers
        if _argv_token_uses_glob(token):
            blockers.append(
                _blocker(
                    CHECK_COMPLEX_SHELL_BLOCKER,
                    (
                        f"check command token[{token_index}]={token!r} "
                        "contains a glob wildcard; a check command must "
                        "be a conservative argv (Issue #14 AC3)"
                    ),
                )
            )
    # 2. Complex-shell gate (Issue #14 AC3): a check that invokes a
    #    shell-like binary directly is itself complex shell, and
    #    the engine refuses it unless the boundary cites a
    #    reviewed wrapper file. The wrapper path must resolve to
    #    a real file inside a directory containing the
    #    :data:`REVIEWED_WRAPPER_MARKER` sentinel.
    if _command_is_complex_shell(argv):
        if wrapper is None:
            blockers.append(
                _blocker(
                    CHECK_COMPLEX_SHELL_BLOCKER,
                    (
                        f"check command argv[0]={argv[0]!r} is a shell-like "
                        "binary; a complex shell check requires a "
                        f"reviewed wrapper file (pointed at by the "
                        f"{REVIEWED_WRAPPER_MARKER!r} marker) "
                        "(Issue #14 AC3)"
                    ),
                )
            )
            return blockers
        wrapper_path = _resolve_wrapper(wrapper, base)
        if not wrapper_path.is_file():
            blockers.append(
                _blocker(
                    CHECK_WRAPPER_MISSING_BLOCKER,
                    (
                        f"check complex-shell wrapper {wrapper_path!s} "
                        "does not exist; refusing to invoke a wrapper "
                        "that a reviewer cannot read (Issue #14 AC3)"
                    ),
                )
            )
            return blockers
        if not _wrapper_is_reviewed(wrapper_path):
            blockers.append(
                _blocker(
                    CHECK_WRAPPER_NOT_REVIEWED_BLOCKER,
                    (
                        f"check complex-shell wrapper {wrapper_path!s} "
                        f"is not in a directory containing "
                        f"{REVIEWED_WRAPPER_MARKER!r}; the wrapper must "
                        "live in a reviewed location (Issue #14 AC3)"
                    ),
                )
            )
    return blockers

# --------------------------------------------------------------------------- #
# Public: validate_check_boundary                                             #
# --------------------------------------------------------------------------- #

def validate_check_boundary(
    boundary: Any,
    *,
    base: Path | str | None = None,
) -> dict[str, Any]:
    """Validate a check-side boundary.

    The validator refuses any object that is not a
    :class:`CheckBoundary` instance. A :class:`TargetBoundary`, a
    raw dict, or any other shape is BLOCKED with the
    ``rule-check-boundary-type-mismatch`` blocker id (Issue #14 AC2
    conflation prevention).

    Parameters
    ----------
    boundary:
        The boundary to validate. Must be a :class:`CheckBoundary`
        instance; anything else is rejected.
    base:
        Optional base directory used to resolve a relative wrapper
        path. When ``None``, wrapper paths must be absolute. The
        helper does not require ``base`` to exist (the check is a
        static gate; existence is verified per-wrapper below).
    """
    if not isinstance(boundary, CheckBoundary):
        if isinstance(boundary, TargetBoundary):
            message_tail = (
                "a TargetBoundary was passed where a CheckBoundary is "
                "required; the two boundary types are distinct on "
                "purpose and cannot be conflated (Issue #14 AC2)"
            )
        elif isinstance(boundary, Mapping):
            message_tail = (
                "a raw mapping was passed where a CheckBoundary is "
                "required; the legacy ``execution_boundary`` shape is "
                "for the target-side normalizer (ADR 0028) and cannot "
                "be used as a check-side boundary (Issue #14 AC2)"
            )
        else:
            message_tail = (
                f"a {type(boundary).__name__} was passed where a "
                "CheckBoundary is required (Issue #14 AC2)"
            )
        return _blocked_result(
            [
                _blocker(
                    CHECK_BOUNDARY_TYPE_BLOCKER,
                    (
                        "validate_check_boundary requires a CheckBoundary "
                        f"instance; {message_tail}"
                    ),
                )
            ]
        )
    base_path = Path(base) if base is not None else None
    blockers: list[dict[str, str]] = []
    for argv in boundary.commands:
        cmd_blockers = _gate_check_command(
            argv, wrapper=boundary.wrapper, base=base_path
        )
        blockers.extend(cmd_blockers)
        # Stop at the first command-level failure so the message
        # is unambiguous for the reviewer.
        if cmd_blockers:
            return _blocked_result(cmd_blockers)
    if blockers:
        return _blocked_result(blockers)
    return _ok_result()

# --------------------------------------------------------------------------- #
# Public: plan_check_workspace                                                #
# --------------------------------------------------------------------------- #

def plan_check_workspace(
    parent: Path | str,
    case_id: Any,
) -> dict[str, Any]:
    """Plan a per-case workspace under ``parent`` for ``case_id``.

    The per-case workspace is a fixed-shape subpath
    (``<parent>/cases/<case_id>``). The plan does not create any
    directories; the engine creates the per-case workspace lazily
    on the first check that runs there. An unsafe ``case_id`` is
    BLOCKED with the ``rule-check-workspace-invalid`` id; the
    validator refuses path separators, ``.`` / ``..`` segments,
    leading-dot identifiers, and empty / non-string inputs so a
    caller cannot escape the parent tree.
    """
    if not _is_safe_case_id(case_id):
        return _blocked_result(
            [
                _blocker(
                    CHECK_WORKSPACE_INVALID_BLOCKER,
                    (
                        f"case_id={case_id!r} is not a safe per-case "
                        "identifier; case_id must be a non-empty, "
                        "non-traversal, non-dotfile relative name "
                        "(Issue #14 AC1)"
                    ),
                )
            ]
        )
    parent_path = Path(parent)
    case_workspace = (parent_path / "cases" / case_id).resolve()
    return _ok_result(
        extra={
            "workspace": case_workspace,
            "parent_workspace": parent_path.resolve(),
            "case_id": case_id,
        }
    )

# --------------------------------------------------------------------------- #
# Public: execute_check                                                       #
# --------------------------------------------------------------------------- #

def execute_check(
    boundary: Any,
    case_workspace: Path | str,
    *,
    index: int = 0,
    base: Path | str | None = None,
    timeout: float | None = 30.0,
) -> dict[str, Any]:
    """Execute a single check (by index) inside ``case_workspace``.

    The function first runs :func:`validate_check_boundary`; a
    blocked boundary yields a blocked result without invoking a
    subprocess. On success, the check's argv is run with
    ``cwd=case_workspace`` and the resolved cwd is recorded on the
    result so a reviewer can confirm the check ran in the
    per-case workspace, not the coordinator's cwd.

    Result shape (review hardening for Issue #14)
    ----------------------------------------------

    ``execute_check`` distinguishes three result shapes so a
    reviewer can tell "the check ran and failed" apart from
    "the engine refused to run the check":

      * ``ok=True, blockers=[]``                      -> check passed.
      * ``ok=False, blockers=[...]``                  -> check BLOCKED
        (validator refused, subprocess never ran, wrapper
        missing, or workspace invalid).
      * ``ok=False, blockers=[], returncode != 0``    -> check RAN
        and FAILED. The subprocess exited non-zero; the
        ``returncode``, ``stdout``, and ``stderr`` fields
        carry the verdict and the captured IO. The
        ``actual_cwd`` field is still recorded so a reviewer
        can confirm the check ran in the per-case workspace.

    A non-zero returncode is a *failed check*, not a blocked
    check: an empty ``blockers`` list means the validator
    approved the run and the negative result is the
    subprocess's own verdict. Reviewers branch on the
    ``ok`` / ``blockers`` / ``returncode`` triple to tell
    the three cases apart.

    Parameters
    ----------
    boundary:
        A :class:`CheckBoundary` instance. A wrong type is
        blocked with the ``check-boundary-type-mismatch`` blocker
        (Issue #14 AC2).
    case_workspace:
        The per-case workspace directory. The check runs with
        ``cwd=case_workspace``; the parent is responsible for
        having called :func:`plan_check_workspace` first.
    index:
        Index of the command inside ``boundary.commands`` to
        execute. Defaults to 0; out-of-range indices yield a
        blocked result with the workspace-invalid blocker.
    base:
        Optional base directory for resolving a relative wrapper
        path. Forwarded to :func:`validate_check_boundary`.
    timeout:
        Subprocess timeout in seconds. ``None`` disables the
        timeout (not recommended; the MVP default is 30s).
    """
    coordinator_cwd = Path.cwd()
    case_path = Path(case_workspace)
    # 1. Boundary validation first so a wrong-shape input is
    #    refused without spinning up a subprocess.
    boundary_result = validate_check_boundary(boundary, base=base)
    if not boundary_result["ok"]:
        return _blocked_result(
            boundary_result["blockers"],
            extra={
                "actual_cwd": None,
                "coordinator_cwd": str(coordinator_cwd),
                "returncode": None,
                "stdout": "",
                "stderr": "",
            },
        )
    # The validator ran; boundary is a CheckBoundary. Use a
    # typing cast rather than a runtime assert so the contract
    # holds under ``python -O`` (asserts stripped) and so static
    # type checkers see the narrow type without depending on
    # runtime control flow. The validator is the load-bearing
    # gate; this line is a type-narrowing annotation.
    boundary = cast(CheckBoundary, boundary)
    if not (0 <= index < len(boundary.commands)):
        return _blocked_result(
            [
                _blocker(
                    CHECK_WORKSPACE_INVALID_BLOCKER,
                    (
                        f"check index={index} is out of range for "
                        f"boundary with {len(boundary.commands)} command(s) "
                        "(Issue #14 AC1)"
                    ),
                )
            ],
            extra={
                "actual_cwd": None,
                "coordinator_cwd": str(coordinator_cwd),
                "returncode": None,
                "stdout": "",
                "stderr": "",
            },
        )
    if not case_path.is_dir():
        return _blocked_result(
            [
                _blocker(
                    CHECK_WORKSPACE_INVALID_BLOCKER,
                    (
                        f"case_workspace={case_path!s} is not an existing "
                        "directory; call plan_check_workspace first and "
                        "create the path before executing (Issue #14 AC1)"
                    ),
                )
            ],
            extra={
                "actual_cwd": None,
                "coordinator_cwd": str(coordinator_cwd),
                "returncode": None,
                "stdout": "",
                "stderr": "",
            },
        )
    argv = list(boundary.commands[index])
    try:
        completed = subprocess.run(
            argv,
            cwd=str(case_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _blocked_result(
            [
                _blocker(
                    CHECK_WORKSPACE_INVALID_BLOCKER,
                    (
                        f"check command {argv!r} exceeded timeout "
                        f"{timeout}s in case_workspace={case_path!s} "
                        "(Issue #14 AC1)"
                    ),
                )
            ],
            extra={
                "actual_cwd": str(case_path.resolve()),
                "coordinator_cwd": str(coordinator_cwd),
                "returncode": None,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
            },
        )
    except FileNotFoundError as exc:
        return _blocked_result(
            [
                _blocker(
                    CHECK_WORKSPACE_INVALID_BLOCKER,
                    (
                        f"check command {argv!r} could not be executed in "
                        f"case_workspace={case_path!s}: {exc} "
                        "(Issue #14 AC1)"
                    ),
                )
            ],
            extra={
                "actual_cwd": str(case_path.resolve()),
                "coordinator_cwd": str(coordinator_cwd),
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
            },
        )
    actual_cwd = str(case_path.resolve())
    return {
        "ok": completed.returncode == 0,
        "blockers": [],
        "actual_cwd": actual_cwd,
        "coordinator_cwd": str(coordinator_cwd),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
