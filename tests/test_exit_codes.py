"""Pin the stable exit-code matrix for the ``metacrucible`` console entry.

Issue #27 task 27.1: ``metacrucible.exit_codes`` is the single source
of truth for the integer returned by the ``metacrucible`` console
script and ``python -m metacrucible``. This file pins the contract
end-to-end:

  - import-level: the constants are ``int``, strictly ordered
    ``EXIT_OK < EXIT_USER_ERROR < EXIT_BLOCKED < EXIT_INTERNAL_ERROR``,
    the ``CHECK_BLOCKED_EXIT_CODE`` alias equals ``EXIT_BLOCKED``,
    and the ``EXIT_CODES`` matrix mirrors the individual constants;
  - subprocess-level: the observable exit codes match the constants
    for bare invocation, ``--help`` / ``--version`` (0), argparse
    usage errors (1), the empty-benchmark blocker and the
    missing-case-id blocker (2).

The internal-error path (exit 3) is exercised by an import-level
test that drives the dispatcher with a stub command. Triggering it
through a subprocess would require monkey-patching the console
script, which is not worth the harness noise — the
:func:`metacrucible.__main__.main` source has a direct
``try/except Exception`` that returns ``EXIT_INTERNAL_ERROR`` after
writing a one-line English message to stderr, and the constant is
covered by the matrix assertion.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

from metacrucible.exit_codes import (
    CHECK_BLOCKED_EXIT_CODE,
    EXIT_BLOCKED,
    EXIT_CODES,
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_USER_ERROR,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_FILE_NAME = "benchmark.jsonl"


def _run(argv: Iterable[str]) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with captured text output."""
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(dict(rec), sort_keys=True) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _metadata_record() -> dict[str, Any]:
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
    }


def _generated_case(case_id: str) -> dict[str, Any]:
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "generated",
        "split": "eval",
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "checks": [{"name": "ok", "pattern": "ok"}],
    }


# --------------------------------------------------------------------------- #
# Import-level constants                                                      #
# --------------------------------------------------------------------------- #


def test_constants_are_ints() -> None:
    """Every exit code constant must be a plain ``int``."""
    for value in (EXIT_OK, EXIT_USER_ERROR, EXIT_BLOCKED, EXIT_INTERNAL_ERROR):
        assert isinstance(value, int), (
            f"exit code constant must be an int; got {type(value).__name__} "
            f"({value!r})"
        )


def test_constants_have_documented_values() -> None:
    """The matrix values are pinned: 0 / 1 / 2 / 3."""
    assert EXIT_OK == 0
    assert EXIT_USER_ERROR == 1
    assert EXIT_BLOCKED == 2
    assert EXIT_INTERNAL_ERROR == 3


def test_constants_are_strictly_ordered() -> None:
    """Order 0 < 1 < 2 < 3 lets ``min``/``max`` callers reason about the matrix."""
    assert EXIT_OK < EXIT_USER_ERROR < EXIT_BLOCKED < EXIT_INTERNAL_ERROR, (
        "exit codes must be strictly ordered "
        "EXIT_OK < EXIT_USER_ERROR < EXIT_BLOCKED < EXIT_INTERNAL_ERROR; "
        f"got {EXIT_OK}, {EXIT_USER_ERROR}, {EXIT_BLOCKED}, "
        f"{EXIT_INTERNAL_ERROR}"
    )


def test_check_blocked_exit_code_alias_equals_exit_blocked() -> None:
    """``CHECK_BLOCKED_EXIT_CODE`` is the compatibility alias for ``EXIT_BLOCKED``."""
    assert CHECK_BLOCKED_EXIT_CODE == EXIT_BLOCKED == 2


def test_exit_codes_matrix_matches_constants() -> None:
    """``EXIT_CODES`` mirrors the individual ``EXIT_*`` constants."""
    assert EXIT_CODES == {
        "EXIT_OK": EXIT_OK,
        "EXIT_USER_ERROR": EXIT_USER_ERROR,
        "EXIT_BLOCKED": EXIT_BLOCKED,
        "EXIT_INTERNAL_ERROR": EXIT_INTERNAL_ERROR,
    }


def test_exit_codes_matrix_keys_match_constant_names() -> None:
    """Matrix keys are the symbolic names callers branch on."""
    assert set(EXIT_CODES) == {
        "EXIT_OK",
        "EXIT_USER_ERROR",
        "EXIT_BLOCKED",
        "EXIT_INTERNAL_ERROR",
    }


# --------------------------------------------------------------------------- #
# Subprocess-level: success paths                                             #
# --------------------------------------------------------------------------- #


def test_bare_invocation_exits_ok() -> None:
    """``metacrucible`` (no args) must print a banner and exit 0."""
    result = _run([])
    assert result.returncode == EXIT_OK, (
        f"bare `metacrucible` must exit {EXIT_OK} (EXIT_OK); "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_help_exits_ok() -> None:
    """``metacrucible --help`` must exit 0."""
    result = _run(["--help"])
    assert result.returncode == EXIT_OK, (
        f"`metacrucible --help` must exit {EXIT_OK} (EXIT_OK); "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_version_exits_ok() -> None:
    """``metacrucible --version`` must exit 0."""
    result = _run(["--version"])
    assert result.returncode == EXIT_OK, (
        f"`metacrucible --version` must exit {EXIT_OK} (EXIT_OK); "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# Subprocess-level: argparse usage errors (EXIT_USER_ERROR)                   #
# --------------------------------------------------------------------------- #


def test_unknown_subcommand_exits_user_error() -> None:
    """An unknown subcommand is an argparse usage error (exit 1)."""
    result = _run(["unknown-subcommand"])
    assert result.returncode == EXIT_USER_ERROR, (
        f"`metacrucible unknown-subcommand` must exit {EXIT_USER_ERROR} "
        f"(EXIT_USER_ERROR); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Argparse still writes its usage line to stderr; the wrapper
    # only changes the exit code, not the diagnostic.
    assert result.stderr.strip(), (
        f"`metacrucible unknown-subcommand` must write an argparse "
        f"diagnostic to stderr; got empty stderr"
    )


def test_init_missing_workspace_exits_user_error() -> None:
    """``metacrucible init`` (no workspace) is an argparse usage error."""
    result = _run(["init"])
    assert result.returncode == EXIT_USER_ERROR, (
        f"`metacrucible init` (missing required positional) must exit "
        f"{EXIT_USER_ERROR} (EXIT_USER_ERROR); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# Subprocess-level: blocked paths (EXIT_BLOCKED)                              #
# --------------------------------------------------------------------------- #


def test_init_check_empty_benchmark_exits_blocked(tmp_path: Path) -> None:
    """``init --check`` on a fresh workspace reports the empty-benchmark blocker."""
    workspace = tmp_path / "ws-empty"
    workspace.mkdir()
    init = _run(["init", str(workspace)])
    assert init.returncode == EXIT_OK, (
        f"`init` must exit {EXIT_OK} before --check; got rc={init.returncode} "
        f"stderr={init.stderr!r}"
    )
    result = _run(["init", "--check", str(workspace)])
    assert result.returncode == EXIT_BLOCKED, (
        f"`init --check` on empty benchmark must exit {EXIT_BLOCKED} "
        f"(EXIT_BLOCKED); got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_promote_missing_case_id_exits_blocked(tmp_path: Path) -> None:
    """``promote`` on a workspace without the requested case id is blocked."""
    workspace = tmp_path / "ws-promote-missing"
    workspace.mkdir()
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run(
        [
            "promote",
            str(workspace),
            "--case-id",
            "missing",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--apply",
            "--json",
        ],
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`promote --case-id missing` must exit {EXIT_BLOCKED} (EXIT_BLOCKED); "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# Subprocess-level: internal-error firewall (EXIT_INTERNAL_ERROR)             #
# --------------------------------------------------------------------------- #


def test_unexpected_command_exception_maps_to_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An uncaught exception past the dispatcher maps to ``EXIT_INTERNAL_ERROR`` with English stderr."""
    from metacrucible import __main__ as cli_main

    def _explode(_args: object) -> int:
        raise RuntimeError("synthetic dispatcher boom")

    # Patch the dispatcher's command registry so the first subcommand
    # call raises. We add a brand-new subcommand via the test-only
    # ``monkeypatch`` fixture; this never mutates the real parser.
    monkeypatch.setattr(cli_main, "cmd_init", _explode, raising=True)

    # Drive main() directly so the test stays in-process and the
    # ``capsys`` fixture captures the English stderr message.
    rc = cli_main.main(["init", "/tmp/should-not-be-touched"])
    captured = capsys.readouterr()

    assert rc == EXIT_INTERNAL_ERROR, (
        f"uncaught command exception must map to {EXIT_INTERNAL_ERROR} "
        f"(EXIT_INTERNAL_ERROR); got rc={rc} stderr={captured.err!r}"
    )
    assert "internal error" in captured.err, (
        f"unexpected-exception path must write an English 'internal error' "
        f"message to stderr; got stderr={captured.err!r}"
    )
    assert "RuntimeError" in captured.err, (
        f"unexpected-exception path must include the exception class name "
        f"in the stderr diagnostic; got stderr={captured.err!r}"
    )
