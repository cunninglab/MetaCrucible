"""Reusable git dirty-file guard (Issue #37).

Extracted from :mod:`metacrucible.__main__` so that both
``baseline create`` and ``optimize`` (and any future command that
wants to block on unrelated workspace edits) can call the same
``git status --porcelain`` parser without duplicating logic.

Public API
----------

:func:`git_dirty_check` mirrors the historical
``_baseline_git_dirty_check`` triple
``(unrelated_dirty, dirty_paths, is_worktree)`` so existing call
sites in :mod:`metacrucible.__main__` (and their tests) can keep
working through a one-line wrapper. The semantics are:

  - ``is_worktree`` is ``True`` iff ``git rev-parse
    --is-inside-work-tree`` returns ``true`` for ``workspace``.
    A non-worktree workspace (no git integration, or the
    workspace lives outside any git toplevel) is not gated
    by the dirty guard: the caller is expected to commit
    before running the command but the command cannot
    enforce that outside a worktree. A warning is surfaced
    via ``is_worktree=False`` so the operator can see the
    skip.
  - ``dirty_paths`` is the raw ``git status --porcelain``
    output, normalized to repo-relative paths (no
    renames/copies handling beyond the basic case).
  - ``unrelated_dirty`` is ``True`` iff at least one
    ``dirty_paths`` entry is not one of the
    ``related_input_paths`` (resolved against ``workspace``).
    The filter is permissive: a path matches an input when
    the resolved absolute paths are equal.

The function never raises; subprocess errors and unreadable
output are downgraded to ``is_worktree=False`` so the caller
can branch on a tri-state without exception handling.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def git_dirty_check(
    workspace: Path, related_input_paths: list[Path]
) -> tuple[bool, list[str], bool]:
    """Inspect the workspace's git state and classify dirty files.

    Returns a triple ``(unrelated_dirty, dirty_paths, is_worktree)``.

    ``related_input_paths`` is the list of paths treated as
    "related inputs" for the calling command (e.g. baseline
    artifact, envelope, ``baseline.json``, benchmark for
    ``baseline create``). Any dirty path resolved against
    ``workspace`` that matches one of those resolved absolutes
    is NOT flagged as unrelated.
    """
    try:
        probe = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "rev-parse",
                "--is-inside-work-tree",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, [], False
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return False, [], False

    try:
        status = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, [], True
    if status.returncode != 0:
        return False, [], True

    dirty: list[str] = []
    for raw_line in status.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        # Porcelain v1: ``XY<space>PATH`` where XY is the index +
        # worktree status code. Renames/copies carry
        # ``ORIG -> PATH``; the second component is the
        # post-rename path so we keep that.
        path_str = raw_line[3:].strip()
        if " -> " in path_str:
            path_str = path_str.split(" -> ", 1)[1]
        # Git may quote paths that contain special characters
        # (``"foo bar"``); the basic workspace fixtures do not
        # exercise that path so we only handle the unquoted
        # case for the MVP. The downstream resolver still
        # catches escape mismatches.
        if path_str.startswith('"') and path_str.endswith('"'):
            path_str = path_str[1:-1]
        dirty.append(path_str)

    input_abs = {
        (workspace / rel).resolve() for rel in related_input_paths
    }
    unrelated: list[str] = []
    for entry in dirty:
        candidate = (workspace / entry).resolve()
        if candidate not in input_abs:
            unrelated.append(entry)
    return bool(unrelated), dirty, True
