#!/usr/bin/env python3
"""Release gate for MetaCrucible (Issue #48 Task 2).

Standalone CLI that validates the release-readiness contract for the
PyPI release toolchain:

  - ``pyproject.toml`` ``[project] version`` is a real SemVer string
    (not a placeholder like ``0.0.0`` or ``Unreleased``).
  - ``CHANGELOG.md`` contains a Keep-a-Changelog ``## [<version>]``
    heading matching the version above (heading-shape match, not a
    free-text substring scan).
  - When invoked with ``--check-tag``, the ``v<version>`` git tag
    must also exist (default invocation skips the tag check so
    the gate can run in environments without a release tag).

Exits ``0`` on success, non-zero on failure, with a clear reason
written to ``stderr``. Not imported by the ``metacrucible`` package
and not exposed via ``[project.scripts]``; the Mise
``[tasks.release-gate]`` task is the public entrypoint.

The script is stdlib-only (``tomllib`` + ``argparse`` + ``re`` +
``subprocess``); no runtime dependency is added.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

__all__ = ["main", "parse_pyproject_version", "is_placeholder_version",
           "has_changelog_section", "has_git_tag"]

# Version strings treated as placeholders, not real releases.
# Includes the empty string and the typical ``Unreleased`` heading body
# that lives under ``## [Unreleased]`` in Keep a Changelog files.
# All comparisons are done after ``str.strip()`` so trailing whitespace
# in ``pyproject.toml`` does not let a placeholder sneak through.
PLACEHOLDER_VERSIONS: frozenset[str] = frozenset({
    "",
    "0.0",
    "0.0.0",
    "Unreleased",
})

# Keep a Changelog heading: ``## [<version>]`` on its own line.
# Anchored to line start so ``### [<version>]`` (sub-heading) and
# inline bracket mentions like ``see [0.1.0] for details`` do NOT
# match. The captured ``version`` group is compared for exact equality
# against the pyproject version, so ``## [0.1.0]`` only satisfies a
# requested version of ``"0.1.0"`` and not, say, ``"0.1"``.
_CHANGELOG_SECTION_RE = re.compile(
    r"^##\s*\[(?P<version>[^\]]+)\]\s*$",
    re.MULTILINE,
)


def parse_pyproject_version(pyproject: Path) -> str:
    """Return the ``[project].version`` string from ``pyproject.toml``.

    Raises:
        FileNotFoundError: if ``pyproject`` does not exist.
        KeyError: if the ``[project]`` table or its ``version`` key is
            missing or not a string.
        tomllib.TOMLDecodeError: if the file is not valid TOML.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, dict):
        raise KeyError("[project] table missing in pyproject.toml")
    version = project.get("version")
    if not isinstance(version, str):
        raise KeyError("[project].version missing or not a string in pyproject.toml")
    return version


def is_placeholder_version(version: str) -> bool:
    """Return True if ``version`` (after stripping whitespace) is a placeholder."""
    return version.strip() in PLACEHOLDER_VERSIONS


def has_changelog_section(changelog: Path, version: str) -> bool:
    """Return True if ``changelog`` has a Keep-a-Changelog heading for ``version``.

    Matches ``## [<version>]`` at the start of a line (NOT ``###``
    sub-headings, NOT inline mentions). Returns ``False`` if the file
    does not exist so the caller can produce a single, uniform
    "missing section" error message.
    """
    if not changelog.is_file():
        return False
    text = changelog.read_text(encoding="utf-8")
    return any(
        match.group("version") == version
        for match in _CHANGELOG_SECTION_RE.finditer(text)
    )


def has_git_tag(repo_root: Path, version: str) -> bool:
    """Return True if ``git tag --list "v<version>"`` yields at least one tag.

    Runs ``git`` from ``repo_root`` so a worktree-bound tag list is
    honored. Any subprocess error (git missing, non-zero exit, no
    output) yields ``False`` — the gate treats all such outcomes the
    same: "tag is not present, release not ready".
    """
    try:
        result = subprocess.run(
            ["git", "tag", "--list", f"v{version}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def _emit_failure(message: str) -> int:
    """Write ``message`` to stderr (prefixed) and return a non-zero exit code."""
    print(f"release-gate: {message}", file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-gate",
        description=(
            "Validate that the worktree is release-ready: "
            "version is real (not a placeholder), CHANGELOG has a "
            "matching `## [<version>]` section, and (with --check-tag) "
            "the v<version> git tag exists."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the repo root (default: current directory)",
    )
    parser.add_argument(
        "--check-tag",
        action="store_true",
        help=(
            "Also require that the `v<version>` git tag exists. "
            "Default invocation validates version + changelog only."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the release gate.

    Args:
        argv: Argument list (excluding the program name). ``None`` means
            ``sys.argv[1:]``. Tests pass an explicit list.

    Returns:
        ``0`` on success (all checks pass), non-zero on failure.
        The reason for failure is written to ``stderr`` with the
        ``release-gate:`` prefix; the caller is responsible for
        translating the return code into a process exit status.
    """
    args = _build_parser().parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    pyproject = repo_root / "pyproject.toml"
    changelog = repo_root / "CHANGELOG.md"

    # --- 1. version ---
    try:
        version = parse_pyproject_version(pyproject)
    except FileNotFoundError:
        return _emit_failure(f"pyproject.toml not found at {pyproject}")
    except KeyError as exc:
        return _emit_failure(f"pyproject.toml invalid: {exc}")
    except tomllib.TOMLDecodeError as exc:
        return _emit_failure(f"pyproject.toml is not valid TOML: {exc}")

    if is_placeholder_version(version):
        return _emit_failure(
            f"pyproject.toml [project].version={version!r} is a placeholder; "
            "set a real SemVer release version before tagging"
        )

    # --- 2. changelog section ---
    if not has_changelog_section(changelog, version):
        return _emit_failure(
            f"CHANGELOG.md missing `## [{version}]` section "
            "(Keep a Changelog shape); add the section before tagging"
        )

    # --- 3. tag (opt-in) ---
    if args.check_tag and not has_git_tag(repo_root, version):
        return _emit_failure(
            f"git tag `v{version}` not found in {repo_root}; "
            "create the tag before publishing"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())