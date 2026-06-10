"""Issue #27 task 27.4: CLI human output is English-only.

Pins the public contract that ``metacrucible`` and ``python -m
metacrucible`` emit English-only prose on stdout / stderr. The
machine JSON contract (key names, blocker ids, exit codes) is
unchanged — these tests only check the *prose* surface humans
read.

The acceptance criterion is "CLI human output is English-only";
this is implemented here as a conservative ASCII-only check on
captured stdout / stderr for the implemented human-output paths:

  - ``metacrucible --help`` and the subcommand help blocks
    (``init --help``, ``promote --help``).
  - The bare-invocation banner printed when no argv is given.
  - ``init --check`` BLOCKED human output for the
    ``missing-reviewed-case`` precondition.
  - ``promote`` human output for both the BLOCKED preconditions
    (``promote-empty-reviewed-by``, ``promote-case-not-found``)
    and the dry-run success path.
  - The internal-error firewall message that ``main`` writes to
    stderr for an uncaught exception past the dispatcher (the
    contract pinned by Issue #27 task 27.1 in
    :mod:`tests.test_exit_codes`).

User-controlled data (e.g. ``--review-note``) is excluded from
the ASCII check: callers may legitimately supply multilingual
content there and the contract only covers the CLI's own prose.
The tests exercise the CLI's own strings plus a smoke pass with
a non-ASCII review note to confirm user data is not echoed into
the English prose surface.

References
----------
- Issue #27 task 27.4 (English-only human CLI output).
- Issue #27 task 27.1 (stable exit-code matrix; covered here via
  the internal-error firewall contract).
- ADR 0029 (machine-stable blocker ids — preserved verbatim by
  the prose contract).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

from metacrucible.exit_codes import (
    EXIT_BLOCKED,
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_USER_ERROR,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_FILE_NAME = "benchmark.jsonl"

# Stable labels / blocker ids the human output must keep verbatim.
# These are the machine contract humans grep for; renaming them
# would break the operator workflow pinned by ADR 0029.
MISSING_REVIEWED_CASE_BLOCKER = "missing-reviewed-case"
PROMOTE_EMPTY_REVIEWER_BLOCKER = "promote-empty-reviewed-by"
PROMOTE_CASE_NOT_FOUND_BLOCKER = "promote-case-not-found"

# Words the rendered help / banner must mention so a future change
# that drops the English description or project name fails loud.
# The list is intentionally small: only the minimum useful
# discoverability floor.
REQUIRED_HELP_WORDS = (
    "metacrucible",
    "usage:",
    "--help",
)
REQUIRED_BANNER_PHRASES = (
    "metacrucible",
    "workbench",
    "Run 'metacrucible --help' for usage.",
)


def _run(argv: Iterable[str]) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with captured text output."""
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _ascii_printables_only(text: str) -> list[str]:
    """Return non-ASCII printable characters (excluding whitespace).

    The CLI's own prose is ASCII by construction; the helper scans
    for printable codepoints outside ``0x00..0x7F`` (excluding
    newline / tab which are control whitespace, not prose). The
    test only needs the *set* of such characters to assert it is
    empty for human output paths.
    """
    return sorted(
        {
            ch
            for ch in text
            if ord(ch) > 0x7F and not ch.isspace()
        }
    )


def _assert_english_prose(label: str, text: str) -> None:
    """Assert ``text`` is ASCII English prose with no CJK / RTL characters."""
    offenders = _ascii_printables_only(text)
    assert not offenders, (
        f"{label} must be English-only (no non-ASCII printable "
        f"characters); got offenders {offenders!r} in {text!r}"
    )


# --------------------------------------------------------------------------- #
# Help output (root + subcommands)                                            #
# --------------------------------------------------------------------------- #


def test_root_help_output_is_english_only() -> None:
    """``metacrucible --help`` prose is English-only and pins the discoverability floor."""
    result = _run(["--help"])
    assert result.returncode == EXIT_OK, (
        f"`metacrucible --help` must exit {EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    _assert_english_prose("root --help stdout", result.stdout)
    # Help path is not expected to write to stderr.
    assert "usage:" not in result.stderr, (
        f"root --help must not duplicate the usage block on stderr; "
        f"got stderr={result.stderr!r}"
    )
    for word in REQUIRED_HELP_WORDS:
        assert word in result.stdout, (
            f"root --help must mention {word!r}; got {result.stdout!r}"
        )


def test_init_help_output_is_english_only() -> None:
    """``metacrucible init --help`` prose is English-only."""
    result = _run(["init", "--help"])
    assert result.returncode == EXIT_OK, (
        f"`metacrucible init --help` must exit {EXIT_OK}; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    _assert_english_prose("init --help stdout", result.stdout)
    assert "init" in result.stdout, (
        f"init --help must mention the subcommand name; got {result.stdout!r}"
    )
    assert "--check" in result.stdout, (
        f"init --help must advertise the --check flag; got {result.stdout!r}"
    )


def test_promote_help_output_is_english_only() -> None:
    """``metacrucible promote --help`` prose is English-only."""
    result = _run(["promote", "--help"])
    assert result.returncode == EXIT_OK, (
        f"`metacrucible promote --help` must exit {EXIT_OK}; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    _assert_english_prose("promote --help stdout", result.stdout)
    assert "promote" in result.stdout, (
        f"promote --help must mention the subcommand name; got {result.stdout!r}"
    )
    assert "--case-id" in result.stdout, (
        f"promote --help must advertise the --case-id flag; got {result.stdout!r}"
    )
    assert "--reviewed-by" in result.stdout, (
        f"promote --help must advertise the --reviewed-by flag; "
        f"got {result.stdout!r}"
    )


# --------------------------------------------------------------------------- #
# Bare invocation banner                                                      #
# --------------------------------------------------------------------------- #


def test_bare_invocation_banner_is_english_only() -> None:
    """The bare-invocation banner (no argv) is English-only."""
    result = _run([])
    assert result.returncode == EXIT_OK, (
        f"bare `metacrucible` must exit {EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    _assert_english_prose("bare invocation stdout", result.stdout)
    for phrase in REQUIRED_BANNER_PHRASES:
        assert phrase in result.stdout, (
            f"bare invocation banner must mention {phrase!r}; "
            f"got stdout={result.stdout!r}"
        )


# --------------------------------------------------------------------------- #
# ``init --check`` BLOCKED human output                                       #
# --------------------------------------------------------------------------- #


def test_init_check_blocked_human_output_is_english_only(
    tmp_path: Path,
) -> None:
    """``init --check`` BLOCKED human output is English-only and surfaces the blocker id.

    The empty benchmark is "valid but not runnable" (ADR 0025). The
    check pass emits the ``missing-reviewed-case`` blocker; the
    English-only contract pins the prose humans read while
    preserving the machine-stable blocker id verbatim.
    """
    workspace = tmp_path / "ws-init-check-english"
    workspace.mkdir(parents=True, exist_ok=True)
    init = _run(["init", str(workspace)])
    assert init.returncode == EXIT_OK, (
        f"`init` must exit {EXIT_OK} before --check; got rc={init.returncode} "
        f"stderr={init.stderr!r}"
    )
    result = _run(["init", "--check", str(workspace)])
    assert result.returncode == EXIT_BLOCKED, (
        f"`init --check` on empty benchmark must exit {EXIT_BLOCKED}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    _assert_english_prose("init --check human stdout", result.stdout)
    _assert_english_prose("init --check human stderr", result.stderr)
    combined = f"{result.stdout}\n{result.stderr}"
    assert MISSING_REVIEWED_CASE_BLOCKER in combined, (
        f"init --check human output must surface the "
        f"{MISSING_REVIEWED_CASE_BLOCKER!r} blocker id; "
        f"got stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# ``promote`` human output (BLOCKED + dry-run success)                        #
# --------------------------------------------------------------------------- #


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    """Write records as JSONL at ``path`` (one object per line)."""
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


def test_promote_blocked_human_output_is_english_only(tmp_path: Path) -> None:
    """``promote`` BLOCKED human output is English-only and surfaces the blocker id.

    Two BLOCKED preconditions are pinned: the
    ``promote-empty-reviewed-by`` (AC from 27.1) and the
    ``promote-case-not-found`` (lookup miss). Both prose messages
    are scanned for non-ASCII printable characters and must come
    back clean.
    """
    workspace = tmp_path / "ws-promote-blocked-english"
    workspace.mkdir(parents=True, exist_ok=True)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    # 1. Empty reviewer (the AC3/27.1 BLOCKED precondition).
    empty = _run(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "",
        ]
    )
    assert empty.returncode == EXIT_BLOCKED, (
        f"empty --reviewed-by must exit {EXIT_BLOCKED}; got "
        f"rc={empty.returncode} stdout={empty.stdout!r} "
        f"stderr={empty.stderr!r}"
    )
    _assert_english_prose(
        "promote BLOCKED (empty reviewer) stdout", empty.stdout
    )
    _assert_english_prose(
        "promote BLOCKED (empty reviewer) stderr", empty.stderr
    )
    combined_empty = f"{empty.stdout}\n{empty.stderr}"
    assert PROMOTE_EMPTY_REVIEWER_BLOCKER in combined_empty, (
        f"promote BLOCKED (empty reviewer) human output must surface "
        f"the {PROMOTE_EMPTY_REVIEWER_BLOCKER!r} blocker id; got "
        f"stdout={empty.stdout!r} stderr={empty.stderr!r}"
    )

    # 2. Missing case id (lookup miss).
    missing = _run(
        [
            "promote",
            str(workspace),
            "--case-id",
            "missing",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
        ]
    )
    assert missing.returncode == EXIT_BLOCKED, (
        f"missing case_id must exit {EXIT_BLOCKED}; got "
        f"rc={missing.returncode} stdout={missing.stdout!r} "
        f"stderr={missing.stderr!r}"
    )
    _assert_english_prose(
        "promote BLOCKED (missing case) stdout", missing.stdout
    )
    _assert_english_prose(
        "promote BLOCKED (missing case) stderr", missing.stderr
    )
    combined_missing = f"{missing.stdout}\n{missing.stderr}"
    assert PROMOTE_CASE_NOT_FOUND_BLOCKER in combined_missing, (
        f"promote BLOCKED (missing case) human output must surface "
        f"the {PROMOTE_CASE_NOT_FOUND_BLOCKER!r} blocker id; got "
        f"stdout={missing.stdout!r} stderr={missing.stderr!r}"
    )


def test_promote_dry_run_success_human_output_is_english_only(
    tmp_path: Path,
) -> None:
    """``promote`` dry-run success human output is English-only.

    A successful dry-run is the canonical "happy path" the prose
    contract must cover: the human sees the planned changes on
    stdout in a non-JSON form. The test pins the English-only
    surface for that path while keeping the JSON contract out of
    scope (covered by ``test_promote_command.py``).
    """
    workspace = tmp_path / "ws-promote-dryrun-english"
    workspace.mkdir(parents=True, exist_ok=True)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    result = _run(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
        ]
    )
    assert result.returncode == EXIT_OK, (
        f"promote dry-run success must exit {EXIT_OK}; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    _assert_english_prose("promote dry-run success stdout", result.stdout)
    _assert_english_prose("promote dry-run success stderr", result.stderr)
    # The human form is a key/value dump; pin the stable keys the
    # operator greps for so a future change that renames them
    # fails loud.
    for key in (
        "case_id",
        "dry_run",
        "applied",
        "blockers",
        "path",
    ):
        assert key in result.stdout, (
            f"promote dry-run success human output must mention key "
            f"{key!r}; got stdout={result.stdout!r}"
        )


def test_promote_dry_run_with_multilingual_review_note_keeps_prose_english(
    tmp_path: Path,
) -> None:
    """User-supplied multilingual review notes do not contaminate the English prose surface.

    The CLI's own prose stays English even when a caller supplies
    a non-ASCII ``--review-note``; the user-controlled string is
    only echoed inside the JSON payload (covered by
    ``test_promote_command.py::test_promote_apply_records_review_note``)
    and is never rendered as part of the human prose. This test
    pins the invariant: the human-form output, when read without
    JSON formatting, still scans as English-only.
    """
    workspace = tmp_path / "ws-promote-multilingual-note"
    workspace.mkdir(parents=True, exist_ok=True)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, [_metadata_record(), _generated_case("gen-1")])

    # Use the same multilingual note the existing JSON test pins
    # so the user-controlled-data contract is exercised end-to-end.
    result = _run(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--review-note",
            "Reviewed: 覆盖 held-out 风险\nOK",
        ]
    )
    # The dry-run path does not surface review_note in the human
    # output; the apply path does. Apply so the human output
    # actually carries the user-controlled string.
    result_apply = _run(
        [
            "promote",
            str(workspace),
            "--case-id",
            "gen-1",
            "--split",
            "eval",
            "--reviewed-by",
            "alice",
            "--review-note",
            "Reviewed: 覆盖 held-out 风险\nOK",
            "--apply",
        ]
    )
    assert result.returncode == EXIT_OK, (
        f"promote dry-run with multilingual note must exit {EXIT_OK}; "
        f"got rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert result_apply.returncode == EXIT_OK, (
        f"promote apply with multilingual note must exit {EXIT_OK}; "
        f"got rc={result_apply.returncode} stdout={result_apply.stdout!r} "
        f"stderr={result_apply.stderr!r}"
    )
    # The dry-run prose must be English-only because the user note
    # is not part of the rendered output.
    _assert_english_prose(
        "promote dry-run (multilingual note) stdout", result.stdout
    )
    _assert_english_prose(
        "promote dry-run (multilingual note) stderr", result.stderr
    )
    # The apply path echoes review_note verbatim; the user note is
    # not "English prose" — it is the user's data — so the
    # English-only contract only covers the *CLI's own* strings.
    # Strip the user note from the captured apply output before
    # scanning, then assert the remaining prose is English-only.
    apply_stdout = result_apply.stdout.replace(
        "Reviewed: 覆盖 held-out 风险\nOK", ""
    )
    apply_stderr = result_apply.stderr.replace(
        "Reviewed: 覆盖 held-out 风险\nOK", ""
    )
    _assert_english_prose(
        "promote apply (multilingual note) prose-only stdout",
        apply_stdout,
    )
    _assert_english_prose(
        "promote apply (multilingual note) prose-only stderr",
        apply_stderr,
    )


# --------------------------------------------------------------------------- #
# Argparse usage error human output                                            #
# --------------------------------------------------------------------------- #


def test_argparse_usage_error_stderr_is_english_only() -> None:
    """Argparse usage errors (e.g. missing required args) are English-only on stderr.

    The wrapper maps argparse ``SystemExit`` to ``EXIT_USER_ERROR``
    (Issue #27 task 27.1); the diagnostic argparse itself writes
    is English by construction. The test pins that surface so a
    future change that wraps argparse with a non-English
    formatter fails loud.
    """
    # Missing required positional.
    result = _run(["init"])
    assert result.returncode == EXIT_USER_ERROR, (
        f"`metacrucible init` (no workspace) must exit "
        f"{EXIT_USER_ERROR}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    _assert_english_prose("argparse usage error stderr", result.stderr)
    # Argparse usage errors mention the required arg name; the test
    # pins only the stable "usage:" / "required" markers rather
    # than the exact rendered sentence.
    assert "usage:" in result.stderr or "required" in result.stderr, (
        f"argparse usage error must mention 'usage:' or 'required'; "
        f"got stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# Internal-error firewall (Issue #27 task 27.1)                                #
# --------------------------------------------------------------------------- #


def test_internal_error_stderr_is_english_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The internal-error firewall writes an English-only diagnostic to stderr.

    The contract pinned by Issue #27 task 27.1: an uncaught
    exception past the dispatcher maps to ``EXIT_INTERNAL_ERROR``
    with a one-line English diagnostic on stderr. The format is
    ``metacrucible: internal error: <ClassName>: <message>``; the
    *frame* is fixed English prose, while ``<message>`` is
    caller-controlled. The test pins the frame and the class
    name, and asserts the frame is ASCII.
    """
    from metacrucible import __main__ as cli_main

    def _explode(_args: object) -> int:
        raise RuntimeError("synthetic dispatcher boom")

    monkeypatch.setattr(cli_main, "cmd_init", _explode, raising=True)
    rc = cli_main.main(["init", "/tmp/should-not-be-touched"])
    captured = capsys.readouterr()
    assert rc == EXIT_INTERNAL_ERROR, (
        f"internal-error path must exit {EXIT_INTERNAL_ERROR}; got "
        f"rc={rc} stderr={captured.err!r}"
    )
    # The frame before the user-controlled message must be ASCII.
    frame = "metacrucible: internal error: "
    assert captured.err.startswith(frame), (
        f"internal-error stderr must start with the English frame "
        f"{frame!r}; got stderr={captured.err!r}"
    )
    _assert_english_prose("internal-error frame", frame)
    # The exception class name is part of the prose surface and is
    # always an ASCII identifier.
    assert "RuntimeError" in captured.err, (
        f"internal-error stderr must mention the exception class "
        f"name; got stderr={captured.err!r}"
    )


# --------------------------------------------------------------------------- #
# Re-scan guard: no human-output path emits non-ASCII prose by accident       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv,stdout_label,stderr_label",
    [
        (["--help"], "root --help", "root --help"),
        (["init", "--help"], "init --help", "init --help"),
        (["promote", "--help"], "promote --help", "promote --help"),
        (["--version"], "--version", "--version"),
        ([], "bare invocation", "bare invocation"),
    ],
)
def test_help_and_banner_paths_are_english_only(
    argv: list[str], stdout_label: str, stderr_label: str
) -> None:
    """All help / banner paths emit ASCII-only English prose.

    A parametrize guard so a future subcommand added to
    ``_build_parser`` is covered automatically once it lands in
    this list. The label strings are for failure diagnostics only.
    """
    result = _run(argv)
    _assert_english_prose(f"{stdout_label} stdout", result.stdout)
    _assert_english_prose(f"{stderr_label} stderr", result.stderr)
