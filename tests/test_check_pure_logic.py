"""Pure-logic unit tests for the check-boundary helpers (Issue #44).

These tests exercise the named pure-logic helpers in
:mod:`metacrucible.rule_checks` directly without going through the
broader contract tests in :mod:`tests.test_rule_checks`. Each helper
is pinned in isolation so a future change to the check-boundary
engine cannot hide behind the public end-to-end contract:

  - :func:`metacrucible.rule_checks._is_safe_case_id` — flat,
    non-traversal relative identifier gate.
  - :func:`metacrucible.rule_checks._argv_token_uses_metachar` —
    shell metacharacter scan on a single argv token.
  - :func:`metacrucible.rule_checks._argv_token_uses_glob` — glob
    wildcard scan on a single argv token.
  - :func:`metacrucible.rule_checks._command_is_complex_shell` —
    ``argv[0]`` is one of the shell-like binaries.
  - :func:`metacrucible.rule_checks._resolve_wrapper` — wrapper
    path resolution relative to a base directory.
  - :func:`metacrucible.rule_checks._wrapper_is_reviewed` — marker
    sentinel gate (real file, not a symlink).
  - :func:`metacrucible.rule_checks.validate_check_boundary` —
    top-level boundary validator; happy path and stable blocker
    ids.

Fixtures are inline strings, pathlib ``Path`` objects, and
obviously-fake placeholders — no real secrets, no LLM, network,
sleep, or subprocess calls — so the suite runs deterministically
under ``pytest -q``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from metacrucible.rule_checks import (
    CHECK_BOUNDARY_TYPE_BLOCKER,
    CHECK_COMPLEX_SHELL_BLOCKER,
    CHECK_WRAPPER_MISSING_BLOCKER,
    CHECK_WRAPPER_NOT_REVIEWED_BLOCKER,
    CheckBoundary,
    EXPECTED_BLOCKERS,
    REVIEWED_WRAPPER_MARKER,
    SHELL_BINARIES,
    TargetBoundary,
    _argv_token_uses_glob,
    _argv_token_uses_metachar,
    _command_is_complex_shell,
    _is_safe_case_id,
    _resolve_wrapper,
    _wrapper_is_reviewed,
    validate_check_boundary,
)


# --------------------------------------------------------------------------- #
# _is_safe_case_id                                                            #
# --------------------------------------------------------------------------- #


class TestIsSafeCaseId:
    """Pin the case-id safety gate used by ``plan_check_workspace``."""

    def test_simple_alnum_is_safe(self) -> None:
        """A plain alphanumeric case_id is safe."""
        assert _is_safe_case_id("case-001") is True
        assert _is_safe_case_id("A") is True
        assert _is_safe_case_id("0") is True
        assert _is_safe_case_id("mixed_underscore") is True

    def test_dash_and_underscore_are_safe(self) -> None:
        """Dashes and underscores are path-segment safe."""
        assert _is_safe_case_id("a-b-c") is True
        assert _is_safe_case_id("a_b_c") is True
        assert _is_safe_case_id("123-abc_XYZ") is True

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",               # empty
            None,             # not a str
            42,               # int
            ["case-A"],       # list
            {"id": "case-A"}, # dict
        ],
    )
    def test_non_string_or_empty_is_unsafe(self, bad_id: Any) -> None:
        """Non-string or empty case_id is rejected."""
        assert _is_safe_case_id(bad_id) is False

    @pytest.mark.parametrize(
        "bad_id",
        [
            " ",
            " case-A",
            "case-A ",
            "\tcase-A",
            "case-A\n",
        ],
    )
    def test_leading_trailing_or_interior_whitespace_is_unsafe(
        self, bad_id: str,
    ) -> None:
        """Any whitespace in the case_id is rejected (path segment safety)."""
        assert _is_safe_case_id(bad_id) is False

    def test_interior_space_is_unsafe(self) -> None:
        """An interior space is rejected (review hardening)."""
        assert _is_safe_case_id("case A") is False

    @pytest.mark.parametrize(
        "bad_id",
        [
            "case/A",        # forward slash
            "case\\A",       # backslash
            "a/b",
            "a\\b",
        ],
    )
    def test_separators_are_unsafe(self, bad_id: str) -> None:
        """Path separators in the case_id are rejected."""
        assert _is_safe_case_id(bad_id) is False

    @pytest.mark.parametrize(
        "bad_id",
        [
            "\x00case",      # NUL
            "case\x00",
            "case\x01A",     # SOH
            "case\x1f",      # unit separator
            "case\x7f",      # DEL
        ],
    )
    def test_control_chars_are_unsafe(self, bad_id: str) -> None:
        """ASCII control chars (NUL, DEL, etc.) are rejected."""
        assert _is_safe_case_id(bad_id) is False

    @pytest.mark.parametrize(
        "bad_id",
        [".", ".."],
    )
    def test_dot_and_dotdot_are_unsafe(self, bad_id: str) -> None:
        """The reserved names ``.`` and ``..`` are rejected."""
        assert _is_safe_case_id(bad_id) is False

    @pytest.mark.parametrize(
        "bad_id",
        [
            ".hidden",
            "..hidden",
            ".case-A",
        ],
    )
    def test_leading_dot_is_unsafe(self, bad_id: str) -> None:
        """A leading dot is reserved for hidden files / parent refs."""
        assert _is_safe_case_id(bad_id) is False


# --------------------------------------------------------------------------- #
# _argv_token_uses_metachar                                                   #
# --------------------------------------------------------------------------- #


class TestArgvTokenUsesMetachar:
    """Pin the per-token shell metacharacter scan."""

    def test_safe_token_has_no_metachar(self) -> None:
        """A plain token has no metacharacter."""
        assert _argv_token_uses_metachar("hello") is False
        assert _argv_token_uses_metachar("--flag") is False
        assert _argv_token_uses_metachar("path/to/file") is False
        assert _argv_token_uses_metachar("/usr/bin/python3") is False
        assert _argv_token_uses_metachar("") is False

    @pytest.mark.parametrize(
        "token",
        [
            "a|b",       # pipe
            "a&b",       # background / and-list
            "a;b",       # separator
            "a>b",       # redirect out
            "a<b",       # redirect in
            "a$b",       # variable expansion
            "a`b",       # command substitution (backtick)
            "a~b",       # tilde mid-token
            "a!b",       # history expansion
            "a\\b",      # backslash escape
            "a(b",       # subshell open
            "a)b",       # subshell close
            "a{b",       # brace open
            "a}b",       # brace close
            "a\nb",      # newline
        ],
    )
    def test_each_metachar_is_detected(self, token: str) -> None:
        """Each character in ``_SHELL_METACHARS`` is detected on its own."""
        assert _argv_token_uses_metachar(token) is True

    def test_metachar_anywhere_in_token(self) -> None:
        """A metachar anywhere (start, middle, end) triggers the gate."""
        assert _argv_token_uses_metachar("|start") is True
        assert _argv_token_uses_metachar("mid|") is True
        assert _argv_token_uses_metachar("a|b|c") is True

    def test_wildcard_is_not_a_metachar(self) -> None:
        """Wildcards are caught by the glob gate, not the metachar gate."""
        assert _argv_token_uses_metachar("a*") is False
        assert _argv_token_uses_metachar("a?") is False
        assert _argv_token_uses_metachar("a[1]") is False


# --------------------------------------------------------------------------- #
# _argv_token_uses_glob                                                       #
# --------------------------------------------------------------------------- #


class TestArgvTokenUsesGlob:
    """Pin the per-token glob wildcard scan."""

    def test_safe_token_has_no_glob(self) -> None:
        """A plain token has no glob wildcard."""
        assert _argv_token_uses_glob("hello") is False
        assert _argv_token_uses_glob("--flag") is False
        assert _argv_token_uses_glob("path/to/file") is False
        assert _argv_token_uses_glob("") is False

    def test_asterisk_is_glob(self) -> None:
        """``*`` is a glob wildcard."""
        assert _argv_token_uses_glob("a*") is True
        assert _argv_token_uses_glob("*.txt") is True
        assert _argv_token_uses_glob("**") is True

    def test_question_mark_is_glob(self) -> None:
        """``?`` is a glob wildcard."""
        assert _argv_token_uses_glob("a?b") is True
        assert _argv_token_uses_glob("?") is True

    def test_bracket_pair_is_glob(self) -> None:
        """A token with both ``[`` and ``]`` is a glob bracket pair."""
        assert _argv_token_uses_glob("a[1]b") is True
        assert _argv_token_uses_glob("file[abc].txt") is True

    def test_unbalanced_bracket_is_not_glob(self) -> None:
        """A lone ``[`` or ``]`` is NOT a glob (no bracket pair)."""
        assert _argv_token_uses_glob("a[") is False
        assert _argv_token_uses_glob("a]") is False
        assert _argv_token_uses_glob("[unclosed") is False
        assert _argv_token_uses_glob("unopened]") is False

    def test_metachar_is_not_a_glob(self) -> None:
        """Shell metachars are caught by the metachar gate, not the glob gate."""
        assert _argv_token_uses_glob("a|b") is False
        assert _argv_token_uses_glob("a$b") is False
        assert _argv_token_uses_glob("a;b") is False


# --------------------------------------------------------------------------- #
# _command_is_complex_shell                                                   #
# --------------------------------------------------------------------------- #


class TestCommandIsComplexShell:
    """Pin the shell-like-binary detector."""

    def test_empty_argv_is_not_complex_shell(self) -> None:
        """An empty argv is not a complex shell invocation."""
        assert _command_is_complex_shell([]) is False

    def test_bare_name_shell_binaries_are_complex_shell(self) -> None:
        """Each shell-like binary in ``SHELL_BINARIES`` triggers the gate."""
        for binary in SHELL_BINARIES:
            assert _command_is_complex_shell([binary]) is True, (
                f"{binary!r} should be a complex shell invocation"
            )

    def test_absolute_path_shell_binaries_are_complex_shell(self) -> None:
        """Absolute paths to shell-like binaries still trigger the gate
        (matched by basename, not the full path)."""
        assert _command_is_complex_shell(["/bin/bash"]) is True
        assert _command_is_complex_shell(["/usr/bin/zsh"]) is True
        assert _command_is_complex_shell(["/usr/local/bin/fish"]) is True

    def test_non_shell_binary_is_not_complex_shell(self) -> None:
        """A non-shell argv[0] is not a complex shell invocation."""
        assert _command_is_complex_shell(["python"]) is False
        assert _command_is_complex_shell(["/usr/bin/python3"]) is False
        assert _command_is_complex_shell(["echo"]) is False
        assert _command_is_complex_shell(["grep"]) is False

    def test_non_string_head_is_not_complex_shell(self) -> None:
        """A non-string argv[0] is not a complex shell invocation."""
        assert _command_is_complex_shell([None]) is False
        assert _command_is_complex_shell([42]) is False
        assert _command_is_complex_shell([["bash"]]) is False

    def test_shell_like_name_with_extra_suffix_is_not_complex_shell(
        self,
    ) -> None:
        """``basher`` is NOT ``bash``; the basename must match the vocabulary."""
        assert _command_is_complex_shell(["basher"]) is False
        assert _command_is_complex_shell(["/usr/bin/basher"]) is False
        assert _command_is_complex_shell(["bashful"]) is False

    def test_args_after_shell_name_do_not_change_verdict(self) -> None:
        """Trailing argv tokens do not change the complex-shell verdict."""
        assert _command_is_complex_shell(["bash", "-c", "echo hi"]) is True
        assert _command_is_complex_shell(["sh", "/tmp/x.sh"]) is True


# --------------------------------------------------------------------------- #
# _resolve_wrapper                                                            #
# --------------------------------------------------------------------------- #


class TestResolveWrapper:
    """Pin the wrapper-path resolver used by the complex-shell gate."""

    def test_absolute_wrapper_resolves_unchanged(self, tmp_path: Path) -> None:
        """An absolute wrapper path resolves to its absolute form."""
        wrapper_file = tmp_path / "wrapper.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        resolved = _resolve_wrapper(str(wrapper_file), None)
        assert resolved.is_absolute()
        assert resolved == wrapper_file.resolve()

    def test_relative_wrapper_with_base_resolves_under_base(
        self, tmp_path: Path,
    ) -> None:
        """A relative wrapper path resolves under ``base``."""
        wrappers = tmp_path / "wrappers"
        wrappers.mkdir()
        wrapper_file = wrappers / "run.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        base = tmp_path
        resolved = _resolve_wrapper("wrappers/run.sh", base)
        assert resolved == wrapper_file.resolve()
        assert resolved.is_absolute()

    def test_relative_wrapper_without_base_stays_relative(
        self, tmp_path: Path,
    ) -> None:
        """A relative wrapper path with ``base=None`` is resolved as-is.

        The path may not exist on disk; ``Path.resolve()`` still returns
        an absolute path under the current working directory.
        """
        resolved = _resolve_wrapper("wrappers/run.sh", None)
        # Path.resolve() returns an absolute path even for non-existent
        # files. Compare against ``Path("wrappers/run.sh").resolve()``
        # so the test is cwd-independent.
        assert resolved == Path("wrappers/run.sh").resolve()
        assert resolved.is_absolute()

    def test_absolute_wrapper_ignores_base(self, tmp_path: Path) -> None:
        """When the wrapper is absolute, ``base`` is not consulted."""
        wrapper_file = tmp_path / "wrapper.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        other_base = tmp_path / "somewhere_else"
        other_base.mkdir()
        resolved = _resolve_wrapper(str(wrapper_file), other_base)
        assert resolved == wrapper_file.resolve()

    def test_resolve_is_idempotent(self, tmp_path: Path) -> None:
        """Resolving a resolved path is a no-op."""
        wrapper_file = tmp_path / "wrapper.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        once = _resolve_wrapper(str(wrapper_file), None)
        twice = _resolve_wrapper(str(once), None)
        assert once == twice


# --------------------------------------------------------------------------- #
# _wrapper_is_reviewed                                                        #
# --------------------------------------------------------------------------- #


class TestWrapperIsReviewed:
    """Pin the reviewed-wrapper marker sentinel gate."""

    def test_missing_parent_dir_is_not_reviewed(self, tmp_path: Path) -> None:
        """A wrapper whose parent does not exist is not reviewed."""
        ghost = tmp_path / "does" / "not" / "exist" / "wrapper.sh"
        assert _wrapper_is_reviewed(ghost) is False

    def test_parent_dir_without_marker_is_not_reviewed(
        self, tmp_path: Path,
    ) -> None:
        """A wrapper in a real dir without the marker is not reviewed."""
        wrappers = tmp_path / "wrappers"
        wrappers.mkdir()
        wrapper_file = wrappers / "run.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        assert _wrapper_is_reviewed(wrapper_file) is False

    def test_parent_dir_with_marker_file_is_reviewed(self, tmp_path: Path) -> None:
        """A wrapper in a dir containing the marker file is reviewed."""
        wrappers = tmp_path / "wrappers"
        wrappers.mkdir()
        marker = wrappers / REVIEWED_WRAPPER_MARKER
        marker.write_text("reviewed\n", encoding="utf-8")
        wrapper_file = wrappers / "run.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        assert _wrapper_is_reviewed(wrapper_file) is True

    def test_marker_as_symlink_is_not_reviewed(self, tmp_path: Path) -> None:
        """A symlink named like the marker is rejected (review hardening).

        A symlinked marker would let a wrapper dir claim reviewed
        status by pointing at a file outside the wrapper dir.
        """
        wrappers = tmp_path / "wrappers"
        wrappers.mkdir()
        wrapper_file = wrappers / "run.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        # Create a real marker file in another dir, then symlink to it.
        outside = tmp_path / "outside"
        outside.mkdir()
        real_marker = outside / REVIEWED_WRAPPER_MARKER
        real_marker.write_text("reviewed (but in wrong dir)\n", encoding="utf-8")
        symlinked_marker = wrappers / REVIEWED_WRAPPER_MARKER
        try:
            symlinked_marker.symlink_to(real_marker)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        # The symlink must NOT grant reviewed status, even though
        # ``is_file()`` follows the link.
        assert _wrapper_is_reviewed(wrapper_file) is False

    def test_marker_directory_not_a_file_is_not_reviewed(
        self, tmp_path: Path,
    ) -> None:
        """A directory named like the marker is not a real marker file."""
        wrappers = tmp_path / "wrappers"
        wrappers.mkdir()
        # Create a directory with the marker name (not a file).
        (wrappers / REVIEWED_WRAPPER_MARKER).mkdir()
        wrapper_file = wrappers / "run.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        assert _wrapper_is_reviewed(wrapper_file) is False

    def test_marker_in_sibling_dir_does_not_grant_reviewed(
        self, tmp_path: Path,
    ) -> None:
        """A marker in a sibling dir does not grant reviewed status
        to a wrapper in a different (reviewed-looking) dir."""
        reviewed = tmp_path / "wrappers"
        reviewed.mkdir()
        (reviewed / REVIEWED_WRAPPER_MARKER).write_text(
            "reviewed\n", encoding="utf-8",
        )
        unreviewed = tmp_path / "other"
        unreviewed.mkdir()
        wrapper_file = unreviewed / "run.sh"
        wrapper_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        assert _wrapper_is_reviewed(wrapper_file) is False


# --------------------------------------------------------------------------- #
# validate_check_boundary — stable blocker ids                                #
# --------------------------------------------------------------------------- #


class TestValidateCheckBoundaryBlockerIds:
    """Pin the stable blocker ids emitted by ``validate_check_boundary``.

    These strings are the machine contract: tests, the optimizer
    pipeline, and downstream automation all branch on them verbatim.
    A rename is a breaking change; an unknown id is a contract bug.
    """

    def test_expected_blockers_dict_contains_required_keys(self) -> None:
        """The expected-blockers dict has the full set of stable keys."""
        required_keys = {
            "check_boundary_type",
            "check_complex_shell",
            "check_wrapper_missing",
            "check_wrapper_not_reviewed",
            "check_workspace_invalid",
        }
        assert required_keys.issubset(EXPECTED_BLOCKERS.keys())
        assert (
            EXPECTED_BLOCKERS["check_boundary_type"]
            == CHECK_BOUNDARY_TYPE_BLOCKER
        )
        assert (
            EXPECTED_BLOCKERS["check_complex_shell"]
            == CHECK_COMPLEX_SHELL_BLOCKER
        )
        assert (
            EXPECTED_BLOCKERS["check_wrapper_missing"]
            == CHECK_WRAPPER_MISSING_BLOCKER
        )
        assert (
            EXPECTED_BLOCKERS["check_wrapper_not_reviewed"]
            == CHECK_WRAPPER_NOT_REVIEWED_BLOCKER
        )

    def test_blocker_ids_match_pinned_strings(self) -> None:
        """Each blocker id constant has its pinned string value."""
        assert (
            CHECK_BOUNDARY_TYPE_BLOCKER
            == "rule-check-boundary-type-mismatch"
        )
        assert (
            CHECK_COMPLEX_SHELL_BLOCKER
            == "rule-check-complex-shell-requires-wrapper"
        )
        assert (
            CHECK_WRAPPER_MISSING_BLOCKER
            == "rule-check-wrapper-missing"
        )
        assert (
            CHECK_WRAPPER_NOT_REVIEWED_BLOCKER
            == "rule-check-wrapper-not-reviewed"
        )

    def test_target_boundary_is_rejected_with_type_blocker(self) -> None:
        """A ``TargetBoundary`` is rejected with the type-mismatch id.

        Conflation prevention (Issue #14 AC2): target and check
        boundaries are distinct types and cannot be interchanged.
        """
        target = TargetBoundary(
            allowed_tools=("Read",),
            target_commands=(("python", "--version"),),
        )
        result = validate_check_boundary(target)
        assert result["ok"] is False
        ids = [b["id"] for b in result["blockers"]]
        assert CHECK_BOUNDARY_TYPE_BLOCKER in ids
        # Exactly one blocker, the type-mismatch one.
        assert len(result["blockers"]) == 1

    def test_raw_dict_is_rejected_with_type_blocker(self) -> None:
        """A raw mapping is rejected with the type-mismatch id."""
        raw = {"commands": [["python", "--version"]]}
        result = validate_check_boundary(raw)
        assert result["ok"] is False
        ids = [b["id"] for b in result["blockers"]]
        assert CHECK_BOUNDARY_TYPE_BLOCKER in ids

    @pytest.mark.parametrize(
        "bogus",
        [None, 42, "string", ["list"], ("tuple",), object()],
    )
    def test_non_check_boundary_is_rejected_with_type_blocker(
        self, bogus: Any,
    ) -> None:
        """Arbitrary non-``CheckBoundary`` values are rejected with
        the type-mismatch blocker id."""
        result = validate_check_boundary(bogus)
        assert result["ok"] is False
        ids = [b["id"] for b in result["blockers"]]
        assert CHECK_BOUNDARY_TYPE_BLOCKER in ids

    def test_complex_shell_without_wrapper_uses_complex_shell_blocker(
        self,
    ) -> None:
        """A complex-shell argv with no wrapper cites the
        complex-shell-requires-wrapper blocker id."""
        boundary = CheckBoundary(
            commands=(("bash", "-c", "echo hi"),),
            wrapper=None,
        )
        result = validate_check_boundary(boundary)
        assert result["ok"] is False
        ids = [b["id"] for b in result["blockers"]]
        assert CHECK_COMPLEX_SHELL_BLOCKER in ids
        assert CHECK_WRAPPER_MISSING_BLOCKER not in ids

    def test_complex_shell_with_nonexistent_wrapper_uses_missing_blocker(
        self, tmp_path: Path,
    ) -> None:
        """A wrapper path that does not exist on disk cites the
        wrapper-missing blocker id."""
        base = tmp_path / "wrappers"
        base.mkdir()
        (base / REVIEWED_WRAPPER_MARKER).write_text(
            "reviewed\n", encoding="utf-8",
        )
        ghost_wrapper = "wrappers/does_not_exist.sh"
        boundary = CheckBoundary(
            commands=(("bash", "-c", "echo hi"),),
            wrapper=ghost_wrapper,
        )
        result = validate_check_boundary(boundary, base=base)
        assert result["ok"] is False
        ids = [b["id"] for b in result["blockers"]]
        assert CHECK_WRAPPER_MISSING_BLOCKER in ids
        assert CHECK_COMPLEX_SHELL_BLOCKER not in ids

    def test_complex_shell_with_unreviewed_wrapper_uses_not_reviewed_blocker(
        self, tmp_path: Path,
    ) -> None:
        """A wrapper in a non-reviewed dir cites the
        wrapper-not-reviewed blocker id."""
        base = tmp_path / "wrappers"
        base.mkdir()
        # NOTE: no marker file in ``base`` — wrapper is not reviewed.
        wrapper_path = base / "run.sh"
        wrapper_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        boundary = CheckBoundary(
            commands=(("bash", "-c", "echo hi"),),
            wrapper=str(wrapper_path),
        )
        result = validate_check_boundary(boundary, base=base)
        assert result["ok"] is False
        ids = [b["id"] for b in result["blockers"]]
        assert CHECK_WRAPPER_NOT_REVIEWED_BLOCKER in ids
        assert CHECK_WRAPPER_MISSING_BLOCKER not in ids

    def test_shell_metachar_argv_uses_complex_shell_blocker(self) -> None:
        """A metachar in any argv token cites the complex-shell
        blocker id (Issue #14 AC3 argv-shape safety)."""
        boundary = CheckBoundary(
            commands=(("python", "arg|with-metachar"),),
            wrapper=None,
        )
        result = validate_check_boundary(boundary)
        assert result["ok"] is False
        ids = [b["id"] for b in result["blockers"]]
        assert CHECK_COMPLEX_SHELL_BLOCKER in ids

    def test_glob_argv_uses_complex_shell_blocker(self) -> None:
        """A glob wildcard in any argv token cites the complex-shell
        blocker id (Issue #14 AC3 argv-shape safety)."""
        boundary = CheckBoundary(
            commands=(("python", "*.txt"),),
            wrapper=None,
        )
        result = validate_check_boundary(boundary)
        assert result["ok"] is False
        ids = [b["id"] for b in result["blockers"]]
        assert CHECK_COMPLEX_SHELL_BLOCKER in ids


# --------------------------------------------------------------------------- #
# validate_check_boundary — happy path                                        #
# --------------------------------------------------------------------------- #


class TestValidateCheckBoundaryHappyPath:
    """Pin the happy-path result shape of ``validate_check_boundary``."""

    def test_simple_check_boundary_with_no_wrapper_is_ok(self) -> None:
        """A simple (non-shell) argv with no wrapper is OK."""
        boundary = CheckBoundary(
            commands=(("python", "--version"),),
            wrapper=None,
        )
        result = validate_check_boundary(boundary)
        assert result == {"ok": True, "blockers": []}

    def test_empty_commands_tuple_is_ok(self) -> None:
        """A ``CheckBoundary`` with no commands at all is OK."""
        boundary = CheckBoundary(commands=(), wrapper=None)
        result = validate_check_boundary(boundary)
        assert result == {"ok": True, "blockers": []}

    def test_multiple_simple_commands_are_ok(self) -> None:
        """Multiple simple argvs in a single boundary are OK."""
        boundary = CheckBoundary(
            commands=(
                ("python", "--version"),
                ("grep", "-c", "x", "file.txt"),
                ("echo", "hello"),
            ),
            wrapper=None,
        )
        result = validate_check_boundary(boundary)
        assert result == {"ok": True, "blockers": []}

    def test_complex_shell_with_reviewed_wrapper_is_ok(
        self, tmp_path: Path,
    ) -> None:
        """A complex-shell check with a reviewed wrapper is OK."""
        base = tmp_path / "wrappers"
        base.mkdir()
        (base / REVIEWED_WRAPPER_MARKER).write_text(
            "reviewed\n", encoding="utf-8",
        )
        wrapper_path = base / "run.sh"
        wrapper_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        boundary = CheckBoundary(
            commands=(("bash", "-c", "echo hi"),),
            wrapper=str(wrapper_path),
        )
        result = validate_check_boundary(boundary, base=base)
        assert result == {"ok": True, "blockers": []}

    def test_complex_shell_with_relative_reviewed_wrapper_is_ok(
        self, tmp_path: Path,
    ) -> None:
        """A relative wrapper path that resolves to a reviewed file is OK."""
        base = tmp_path / "wrappers"
        base.mkdir()
        (base / REVIEWED_WRAPPER_MARKER).write_text(
            "reviewed\n", encoding="utf-8",
        )
        wrapper_path = base / "run.sh"
        wrapper_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        boundary = CheckBoundary(
            commands=(("bash", "-c", "echo hi"),),
            wrapper="run.sh",
        )
        result = validate_check_boundary(boundary, base=base)
        assert result == {"ok": True, "blockers": []}

    def test_stops_at_first_failing_command(self) -> None:
        """Validation stops at the first command that emits a blocker."""
        boundary = CheckBoundary(
            commands=(
                ("python", "--version"),  # OK
                ("python", "arg|with-metachar"),  # BLOCKS
                ("python", "*.txt"),  # would also block, but never reached
            ),
            wrapper=None,
        )
        result = validate_check_boundary(boundary)
        assert result["ok"] is False
        # Exactly one blocker from the failing command.
        assert len(result["blockers"]) == 1
        assert result["blockers"][0]["id"] == CHECK_COMPLEX_SHELL_BLOCKER
        # The message references the offending token index (1).
        assert "token[1]" in result["blockers"][0]["message"]
