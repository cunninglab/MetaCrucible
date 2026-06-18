"""Tests for Issue #42 / PRD F5 ``metacrucible inspect`` subcommand.

Task 1 pins only the public parser surface, the ``main()`` dispatch
branch, and the read-only contract that a missing path never writes
a BLOCKED evidence bundle:

  - ``metacrucible inspect <path> [--json]`` parses via the central
    :func:`metacrucible.__main__._build_parser` and exposes the
    ``path`` positional plus the ``--json`` flag.
  - ``main(['inspect', <artifact>, '--json'])`` dispatches to
    :func:`metacrucible.__main__.cmd_inspect` and returns
    :data:`metacrucible.exit_codes.EXIT_OK` for the temporary
    Task 1 payload.
  - A missing path is reported to ``stderr`` and returns a
    non-zero exit code without creating
    ``$HOME/.metacrucible/evidence/`` on disk.
  - ``cmd_inspect`` never imports or calls
    :func:`metacrucible.blocked_bundles.write_blocked_bundle`; the
    missing-path branch must stay free of any BLOCKED-bundle write.

Later tasks replace the temporary ``status: ok`` payload with the
full revision-history / acceptance-decision reader pinned by the
F5 acceptance criteria; the read-only / no-bundle contract proven
here must stay true throughout.
"""
from __future__ import annotations

import argparse
import inspect

import pytest

from metacrucible.__main__ import (
    _build_parser,
    cmd_inspect,
    main,
)
from metacrucible.exit_codes import EXIT_OK, EXIT_USER_ERROR


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def isolated_global_home(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Pin ``HOME`` to a temp dir so any user-global storage write
    would land in ``tmp_path`` instead of the developer's real
    ``~/.metacrucible/``.

    Mirrors the fixture in :mod:`tests.test_review_command` so the
    new tests can run alongside the review tests without
    stepping on the same ``HOME``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    return str(fake_home)


# --------------------------------------------------------------------------- #
# Parser surface                                                              #
# --------------------------------------------------------------------------- #


def test_inspect_parser_accepts_path(tmp_path) -> None:
    from metacrucible.__main__ import _build_parser

    artifact = tmp_path / "artifact.md"
    args = _build_parser().parse_args(["inspect", str(artifact)])

    assert args.command == "inspect"
    assert args.path == str(artifact)
    assert args.json is False


def test_inspect_parser_accepts_json(tmp_path) -> None:
    from metacrucible.__main__ import _build_parser

    artifact = tmp_path / "artifact.md"
    args = _build_parser().parse_args(
        ["inspect", str(artifact), "--json"]
    )

    assert args.command == "inspect"
    assert args.path == str(artifact)
    assert args.json is True


# --------------------------------------------------------------------------- #
# Read-only contract: missing path → no BLOCKED bundle                        #
# --------------------------------------------------------------------------- #


def test_inspect_missing_path_does_not_write_evidence(
    tmp_path,
    isolated_global_home: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing path must return EXIT_USER_ERROR, report to stderr,
    and NOT create ``$HOME/.metacrucible/evidence/`` on disk.

    This pins the F5 acceptance bullet "No files are modified" for
    the negative-input branch and proves that ``cmd_inspect`` does
    not call :func:`metacrucible.blocked_bundles.write_blocked_bundle`.
    """
    missing = tmp_path / "does-not-exist.md"

    # ``isolated_global_home`` is ``tmp_path / "home"``; resolve to a
    # concrete Path so the post-condition check is readable.
    from pathlib import Path

    home = Path(isolated_global_home)
    evidence_root = home / ".metacrucible" / "evidence"

    args = argparse.Namespace(path=str(missing), json=True)

    rc = cmd_inspect(args)

    captured = capsys.readouterr()
    assert rc != 0, (
        "missing path must return a non-zero exit code; "
        f"got rc={rc}"
    )
    assert rc == EXIT_USER_ERROR
    assert "inspect path" in captured.err
    assert "does not exist" in captured.err
    assert not evidence_root.exists(), (
        f"missing-path branch must not create {evidence_root}; "
        "the inspect command is contractually read-only"
    )
    assert captured.out == "", (
        "missing-path branch must not emit a payload to stdout"
    )


def test_inspect_does_not_reference_write_blocked_bundle() -> None:
    """Static guarantee that ``cmd_inspect`` stays free of the
    BLOCKED-bundle writer even after future refactors.

    The Task 1 contract pins inspect as a read-only command; this
    test fails loudly if a later task accidentally imports
    :func:`metacrucible.blocked_bundles.write_blocked_bundle` into
    the ``cmd_inspect`` source body.
    """
    source = inspect.getsource(cmd_inspect)
    assert "write_blocked_bundle" not in source, (
        "cmd_inspect must not call write_blocked_bundle; the inspect "
        "command is contractually read-only (PRD F5 'No files are "
        "modified')"
    )


# --------------------------------------------------------------------------- #
# main() dispatch                                                             #
# --------------------------------------------------------------------------- #


def test_inspect_dispatch_smoke_returns_exit_ok(
    tmp_path,
    isolated_global_home: str,
) -> None:
    """``main(['inspect', <artifact>, '--json'])`` reaches
    :func:`cmd_inspect` and returns :data:`EXIT_OK` for the
    temporary Task 1 payload.

    The artifact path must exist for the thin wrapper to return
    the success branch; the packet specifies a sibling
    ``.metacrucible/`` directory so the wrapper's workspace-path
    computation matches the canonical convention.
    """
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# stub artifact\n", encoding="utf-8")
    workspace = tmp_path / ".metacrucible"
    workspace.mkdir()

    rc = main(["inspect", str(artifact), "--json"])

    assert rc == EXIT_OK