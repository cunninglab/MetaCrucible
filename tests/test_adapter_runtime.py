"""Pure unit tests for the adapter runtime harness (Issue #46 Task 1).

This test file covers the importable, binary-free surface of
:mod:`metacrucible.adapter_runtime`:

  - :func:`metacrucible.adapter_runtime.materialize_skill` writes a
    valid ``.claude/skills/<name>/SKILL.md`` tree in a caller-supplied
    directory and never references the user's home.
  - :func:`metacrucible.adapter_runtime.build_skill_preflight_argv`
    emits the exact token shape pinned by the brief and ADR 0028.
  - :func:`metacrucible.adapter_runtime.build_evidence_summary`
    collapses a :class:`SkillPreflightRun` into the optimizer-friendly
    summary shape.
  - The subprocess method
    :func:`metacrucible.adapter_runtime.run_skill_preflight` is
    exercised through the ``run_subprocess`` test seam so no real
    ``claude`` binary is required.

The tests run on every ``mise run test`` invocation; they do not
depend on the local-real env gate.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

ADAPTER_MODULE = "metacrucible.adapter_runtime"
PREFLIGHT_MODULE = "metacrucible.preflight"
STREAM_JSON_MODULE = "metacrucible.claude_stream_json"
ARGV_NORMALIZE_MODULE = "metacrucible.argv_normalize"


# --------------------------------------------------------------------------- #
# Expected contract values                                                    #
# --------------------------------------------------------------------------- #

EXPECTED_ADAPTER_VERSION = "claude-code/0.4.1"

# Token order matches the brief verbatim (the ``--verbose`` runtime
# flag is injected by the subprocess method, not by the pure builder).
EXPECTED_ARGV_TOKENS_DEFAULT: list[str] = [
    "claude",
    "--bare",
    "--add-dir",
    "/tmp/isolated/.claude/skills",
    "--allowed-tools",
    "Read",
    "--permission-mode",
    "default",
    "-p",
    "--output-format",
    "stream-json",
]


# --------------------------------------------------------------------------- #
# Helpers / fixtures                                                          #
# --------------------------------------------------------------------------- #


def _blocker_ids(payload: Any) -> list[str]:
    """Return the list of blocker ids in a result, or empty if none.

    Accepts both ``dict`` and dataclass instances (the harness
    returns :class:`SkillMaterialization`, the stream-json
    parser returns ``dict``).
    """
    if payload is None:
        return []
    if hasattr(payload, "blockers"):
        blockers = payload.blockers
    elif isinstance(payload, dict):
        blockers = payload.get("blockers", [])
    else:
        return []
    if not isinstance(blockers, list):
        return []
    out: list[str] = []
    for blocker in blockers:
        if isinstance(blocker, dict) and isinstance(blocker.get("id"), str):
            out.append(blocker["id"])
    return out


@pytest.fixture(scope="module")
def adapter() -> Any:
    """Import the adapter runtime module; fail if it does not exist."""
    import importlib

    return importlib.import_module(ADAPTER_MODULE)


@pytest.fixture(scope="module")
def preflight() -> Any:
    """Import the preflight module (needed for sentinel contract tests)."""
    import importlib

    return importlib.import_module(PREFLIGHT_MODULE)


@pytest.fixture(scope="module")
def stream_json() -> Any:
    """Import the stream-json parser module."""
    import importlib

    return importlib.import_module(STREAM_JSON_MODULE)


@pytest.fixture(scope="module")
def argv_normalize() -> Any:
    """Import the argv normalize module (reviewed tools contract)."""
    import importlib

    return importlib.import_module(ARGV_NORMALIZE_MODULE)


# --------------------------------------------------------------------------- #
# Module surface                                                              #
# --------------------------------------------------------------------------- #


def test_adapter_module_exposes_materialize_skill(adapter: Any) -> None:
    """The harness must expose ``materialize_skill``."""
    assert hasattr(adapter, "materialize_skill"), (
        f"{ADAPTER_MODULE} must expose materialize_skill (Issue #46 Task 1)"
    )


def test_adapter_module_exposes_build_skill_preflight_argv(adapter: Any) -> None:
    """The harness must expose the pure argv builder."""
    assert hasattr(adapter, "build_skill_preflight_argv"), (
        f"{ADAPTER_MODULE} must expose build_skill_preflight_argv "
        "(Issue #46 Task 1)"
    )


def test_adapter_module_exposes_run_skill_preflight(adapter: Any) -> None:
    """The harness must expose the single subprocess method."""
    assert hasattr(adapter, "run_skill_preflight"), (
        f"{ADAPTER_MODULE} must expose run_skill_preflight "
        "(Issue #46 Task 1)"
    )


def test_adapter_module_exposes_build_evidence_summary(adapter: Any) -> None:
    """The harness must expose the result-shape helper."""
    assert hasattr(adapter, "build_evidence_summary"), (
        f"{ADAPTER_MODULE} must expose build_evidence_summary "
        "(Issue #46 Task 1)"
    )


def test_adapter_module_reuses_preflight_constants(adapter: Any) -> None:
    """The harness must not redefine the sentinel prefixes."""
    # The harness re-exports the sentinel contract by reusing the
    # preflight module's constants; assert they are the exact strings
    # the rest of the project branches on.
    assert adapter.__doc__ is not None
    assert "METACRUCIBLE_SKILL_DISCOVERABLE" in adapter.__doc__


# --------------------------------------------------------------------------- #
# Materialize                                                                 #
# --------------------------------------------------------------------------- #


def test_materialize_skill_writes_skill_md_tree(adapter: Any, tmp_path: Path) -> None:
    """``materialize_skill`` must create ``.claude/skills/<name>/SKILL.md``."""
    result = adapter.materialize_skill(
        skill_name="metacrucible-smoke",
        skill_body="# Metacrucible smoke\n\nSmoke test body.\n",
        output_dir=tmp_path,
    )
    assert result.ok is True
    assert result.blockers == []
    expected_skill_md = (
        tmp_path / ".claude" / "skills" / "metacrucible-smoke" / "SKILL.md"
    )
    assert Path(result.skill_md_path) == expected_skill_md
    assert expected_skill_md.is_file()
    text = expected_skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: metacrucible-smoke" in text
    assert "Smoke test body." in text


def test_materialize_skill_creates_skill_root(adapter: Any, tmp_path: Path) -> None:
    """``skill_root`` must point at the parent skills directory."""
    result = adapter.materialize_skill(
        skill_name="alpha",
        skill_body="body",
        output_dir=tmp_path,
    )
    assert result.ok is True
    assert Path(result.skill_root) == tmp_path / ".claude" / "skills"
    assert Path(result.skill_root).is_dir()


def test_materialize_skill_never_touches_user_home(
    adapter: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The materializer must not write under the user's real home."""
    # Force HOME / USERPROFILE to the scratch dir; the materializer
    # must still write *only* under the caller-supplied output_dir.
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    fake_home = tmp_path / "fake-home"
    result = adapter.materialize_skill(
        skill_name="no-home-write",
        skill_body="body",
        output_dir=tmp_path,
    )
    assert result.ok is True
    # The fake home must remain empty: nothing was written there.
    assert not fake_home.exists() or not any(fake_home.rglob("*"))


def test_materialize_skill_blocks_missing_name(adapter: Any, tmp_path: Path) -> None:
    """An empty skill name must block materialization safely."""
    result = adapter.materialize_skill(
        skill_name="",
        skill_body="body",
        output_dir=tmp_path,
    )
    assert result.ok is False
    assert _blocker_ids(result) == [
        adapter.SKILL_MATERIALIZE_NAME_MISSING_BLOCKER
    ]
    assert result.skill_md_path == ""
    assert not (tmp_path / ".claude").exists()


def test_materialize_skill_blocks_invalid_name(adapter: Any, tmp_path: Path) -> None:
    """A name with path separators must block materialization safely."""
    result = adapter.materialize_skill(
        skill_name="../escape",
        skill_body="body",
        output_dir=tmp_path,
    )
    assert result.ok is False
    assert _blocker_ids(result) == [
        adapter.SKILL_MATERIALIZE_NAME_INVALID_BLOCKER
    ]
    # No escape: nothing under the caller-supplied output_dir.
    assert not (tmp_path / ".claude").exists()


def test_materialize_skill_blocks_missing_body(adapter: Any, tmp_path: Path) -> None:
    """An empty body must block materialization safely."""
    result = adapter.materialize_skill(
        skill_name="ok",
        skill_body="   ",
        output_dir=tmp_path,
    )
    assert result.ok is False
    assert _blocker_ids(result) == [
        adapter.SKILL_MATERIALIZE_BODY_MISSING_BLOCKER
    ]
    assert not (tmp_path / ".claude").exists()


def test_materialize_skill_is_idempotent(
    adapter: Any, tmp_path: Path
) -> None:
    """Re-materializing to the same name must overwrite cleanly."""
    first = adapter.materialize_skill(
        skill_name="repeat",
        skill_body="first body",
        output_dir=tmp_path,
    )
    second = adapter.materialize_skill(
        skill_name="repeat",
        skill_body="second body",
        output_dir=tmp_path,
    )
    assert first.ok is True
    assert second.ok is True
    text = Path(second.skill_md_path).read_text(encoding="utf-8")
    assert "second body" in text
    assert "first body" not in text


def test_materialize_skill_blocker_ids_are_stable_strings(adapter: Any) -> None:
    """The blocker ids must be the documented machine contract."""
    assert (
        adapter.SKILL_MATERIALIZE_NAME_MISSING_BLOCKER
        == "adapter-runtime-skill-name-missing"
    )
    assert (
        adapter.SKILL_MATERIALIZE_NAME_INVALID_BLOCKER
        == "adapter-runtime-skill-name-invalid"
    )
    assert (
        adapter.SKILL_MATERIALIZE_BODY_MISSING_BLOCKER
        == "adapter-runtime-skill-body-missing"
    )
    assert (
        adapter.SKILL_MATERIALIZE_WRITE_FAILED_BLOCKER
        == "adapter-runtime-skill-write-failed"
    )


# --------------------------------------------------------------------------- #
# Argv builder                                                                #
# --------------------------------------------------------------------------- #


def test_build_argv_matches_brief_token_shape(adapter: Any) -> None:
    """The builder must emit the exact tokens the brief pins."""
    argv = adapter.build_skill_preflight_argv(
        skill_root="/tmp/isolated/.claude/skills",
    )
    assert argv == EXPECTED_ARGV_TOKENS_DEFAULT


def test_build_argv_default_binary_is_claude(adapter: Any) -> None:
    """The default binary must be ``claude`` (Task 3 will override to ``omp``)."""
    argv = adapter.build_skill_preflight_argv(skill_root="/tmp/x")
    assert argv[0] == "claude"


def test_build_argv_honors_binary_parameter(adapter: Any) -> None:
    """The builder must allow a custom binary name for Task 3 (oh-my-pi)."""
    argv = adapter.build_skill_preflight_argv(
        skill_root="/tmp/isolated/.claude/skills",
        binary="omp",
    )
    assert argv[0] == "omp"
    # Rest of the shape is preserved.
    assert argv[1:] == EXPECTED_ARGV_TOKENS_DEFAULT[1:]


def test_build_argv_joins_multiple_allowed_tools(adapter: Any) -> None:
    """Multiple reviewed tools must be emitted as separate tokens."""
    argv = adapter.build_skill_preflight_argv(
        skill_root="/tmp/x",
        allowed_tools=("Read", "Grep", "Glob"),
    )
    assert "--allowed-tools" in argv
    tools_idx = argv.index("--allowed-tools")
    # Three tools must follow as individual tokens.
    assert argv[tools_idx + 1 : tools_idx + 4] == ["Read", "Grep", "Glob"]
    # And the rest of the shape is unchanged.
    assert argv[tools_idx + 4 :] == [
        "--permission-mode",
        "default",
        "-p",
        "--output-format",
        "stream-json",
    ]


def test_build_argv_uses_reviewed_tool_names(
    adapter: Any, argv_normalize: Any
) -> None:
    """The default allowed tools must come from the reviewed set."""
    argv = adapter.build_skill_preflight_argv(skill_root="/tmp/x")
    tools_idx = argv.index("--allowed-tools")
    tools = argv[tools_idx + 1 : argv.index("--permission-mode")]
    for tool in tools:
        assert tool in argv_normalize.REVIEWED_TOOL_NAMES, (
            f"default tool {tool!r} must be in REVIEWED_TOOL_NAMES"
        )


def test_build_argv_uses_bare_flag(adapter: Any) -> None:
    """``--bare`` must be the second token (ADR 0028)."""
    argv = adapter.build_skill_preflight_argv(skill_root="/tmp/x")
    assert "--bare" in argv
    assert argv.index("--bare") == 1


def test_build_argv_uses_permission_mode_default(adapter: Any) -> None:
    """``--permission-mode default`` must be the documented mode."""
    argv = adapter.build_skill_preflight_argv(skill_root="/tmp/x")
    assert "--permission-mode" in argv
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "default"


def test_build_argv_uses_stream_json_output(adapter: Any) -> None:
    """``--output-format stream-json`` must be the documented format."""
    argv = adapter.build_skill_preflight_argv(skill_root="/tmp/x")
    assert "--output-format" in argv
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"


def test_build_argv_uses_print_flag(adapter: Any) -> None:
    """``-p`` must be present for non-interactive mode."""
    argv = adapter.build_skill_preflight_argv(skill_root="/tmp/x")
    assert "-p" in argv


def test_build_argv_add_dir_value_is_skill_root(adapter: Any) -> None:
    """``--add-dir`` must carry the caller-supplied skill root."""
    argv = adapter.build_skill_preflight_argv(
        skill_root="/scratch/.claude/skills",
    )
    idx = argv.index("--add-dir")
    assert argv[idx + 1] == "/scratch/.claude/skills"


def test_build_argv_rejects_empty_binary(adapter: Any) -> None:
    """An empty binary name must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_skill_preflight_argv(skill_root="/tmp/x", binary="")


def test_build_argv_rejects_empty_permission_mode(adapter: Any) -> None:
    """An empty permission mode must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_skill_preflight_argv(
            skill_root="/tmp/x", permission_mode=""
        )


def test_build_argv_rejects_empty_output_format(adapter: Any) -> None:
    """An empty output format must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_skill_preflight_argv(
            skill_root="/tmp/x", output_format=""
        )


def test_build_argv_accepts_empty_allowed_tools(adapter: Any) -> None:
    """The builder must accept an empty tool list (no tools follows ``--allowed-tools``)."""
    argv = adapter.build_skill_preflight_argv(
        skill_root="/tmp/x", allowed_tools=()
    )
    # --allowed-tools is still emitted, just with no following tool tokens.
    assert "--allowed-tools" in argv
    idx = argv.index("--allowed-tools")
    assert argv[idx + 1] == "--permission-mode"


# --------------------------------------------------------------------------- #
# Subprocess method via test seam                                             #
# --------------------------------------------------------------------------- #


class _FakeCompleted:
    """Drop-in replacement for ``subprocess.CompletedProcess``."""

    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_fake_runner(stdout: str, stderr: str, returncode: int) -> Any:
    """Build a fake ``run_subprocess`` that records the call and returns a fake result."""
    calls: list[dict[str, Any]] = []

    def _runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return _FakeCompleted(stdout=stdout, stderr=stderr, returncode=returncode)

    _runner.calls = calls  # type: ignore[attr-defined]
    return _runner


def test_run_skill_preflight_passes_skill_preflight_prompt(
    adapter: Any, preflight: Any
) -> None:
    """The harness must feed the preflight prompt to the binary as positional."""
    runner = _make_fake_runner(
        stdout="not-json\n", stderr="", returncode=0
    )
    adapter.run_skill_preflight(
        skill_root="/tmp/isolated/.claude/skills",
        run_subprocess=runner,
    )
    assert len(runner.calls) == 1  # type: ignore[attr-defined]
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    # The prompt must be the last positional token.
    expected_prompt = preflight.skill_preflight_prompt()
    assert full_argv[-1] == expected_prompt
    # And it must be the exact string from the preflight module.
    assert expected_prompt.startswith("You are running the MetaCrucible")


def test_run_skill_preflight_injects_verbose_flag(adapter: Any) -> None:
    """``--verbose`` must be inserted immediately before ``-p``."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    p_idx = full_argv.index("-p")
    # The token immediately preceding -p must be --verbose.
    assert full_argv[p_idx - 1] == "--verbose"


def test_run_skill_preflight_uses_capture_output(adapter: Any) -> None:
    """The harness must request captured stdout/stderr from the runner."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("check") is False


def test_run_skill_preflight_passes_timeout(adapter: Any) -> None:
    """The harness must forward a timeout to the runner."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
        timeout=7.5,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("timeout") == 7.5


def test_run_skill_preflight_parses_stream_json_through_existing_parser(
    adapter: Any, stream_json: Any
) -> None:
    """The harness must reuse ``parse_stream_json`` (no parallel parser)."""
    stdout = (
        '{"type":"system","subtype":"init","claude_code_version":"0.4.1"}\n'
        '{"type":"result","subtype":"success","result":"hello"}\n'
    )
    runner = _make_fake_runner(stdout=stdout, stderr="", returncode=0)
    run = adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    # The harness's evidence dict must carry the same keys the parser
    # pins; assert at least the AC fields.
    evidence = run.evidence
    assert "start_captured" in evidence
    assert "completion_captured" in evidence
    assert evidence["start_captured"] is True
    assert evidence["completion_captured"] is True
    assert evidence["final_output"] == "hello"
    assert evidence["adapter_version"] == EXPECTED_ADAPTER_VERSION
    assert evidence["claude_code_version"] == "0.4.1"


def test_run_skill_preflight_folds_final_output_through_check_skill_preflight(
    adapter: Any, preflight: Any
) -> None:
    """The harness must reuse ``check_skill_preflight`` (no parallel validator)."""
    sentinel = "METACRUCIBLE_SKILL_DISCOVERABLE=yes; NAME=metacrucible-smoke"
    stdout = (
        '{"type":"system","subtype":"init","claude_code_version":"0.4.1"}\n'
        f'{{"type":"result","subtype":"success","result":"{sentinel}"}}\n'
    )
    runner = _make_fake_runner(stdout=stdout, stderr="", returncode=0)
    run = adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    assert run.preflight.get("ok") is True
    assert run.preflight.get("name") == "metacrucible-smoke"
    # The harness's preflight dict must match the validator's output
    # byte-for-byte on the same input.
    expected = preflight.check_skill_preflight(sentinel)
    assert run.preflight == expected


def test_run_skill_preflight_propagates_exit_code(adapter: Any) -> None:
    """The harness must surface the subprocess exit code verbatim."""
    runner = _make_fake_runner(stdout="", stderr="boom", returncode=42)
    run = adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    assert run.exit_code == 42
    assert run.stderr == "boom"


def test_run_skill_preflight_captures_raw_stdout(adapter: Any) -> None:
    """The harness must capture the raw stdout for evidence writes."""
    stdout = "raw line 1\nraw line 2\n"
    runner = _make_fake_runner(stdout=stdout, stderr="", returncode=0)
    run = adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    assert run.stdout == stdout


def test_run_skill_preflight_honors_explicit_prompt(adapter: Any) -> None:
    """An explicit prompt override must be appended verbatim."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
        prompt="CUSTOM PROMPT",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    assert full_argv[-1] == "CUSTOM PROMPT"


def test_run_skill_preflight_default_prompt_uses_skill_name(
    adapter: Any, preflight: Any
) -> None:
    """The default prompt must fold the caller-supplied skill name in."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
        skill_name="my-skill",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected = preflight.skill_preflight_prompt(skill_name="my-skill")
    assert full_argv[-1] == expected


def test_run_skill_preflight_disable_verbose(adapter: Any) -> None:
    """Setting ``verbose=False`` must drop the ``--verbose`` injection."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
        verbose=False,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    assert "--verbose" not in full_argv


# --------------------------------------------------------------------------- #
# Evidence summary helper                                                     #
# --------------------------------------------------------------------------- #


def test_build_evidence_summary_collapses_clean_run(adapter: Any) -> None:
    """A clean run must collapse to ``ok=True`` with no blockers."""
    sentinel = "METACRUCIBLE_SKILL_DISCOVERABLE=yes; NAME=metacrucible-smoke"
    stdout = (
        '{"type":"system","subtype":"init","claude_code_version":"0.4.1"}\n'
        f'{{"type":"result","subtype":"success","result":"{sentinel}"}}\n'
    )
    runner = _make_fake_runner(stdout=stdout, stderr="", returncode=0)
    run = adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    summary = adapter.build_evidence_summary(run)
    assert summary["ok"] is True
    assert summary["sentinel_ok"] is True
    assert summary["exit_code"] == 0
    assert summary["resolved_name"] == "metacrucible-smoke"
    assert summary["runtime_version"] == "0.4.1"
    assert summary["adapter_version"] == EXPECTED_ADAPTER_VERSION
    assert summary["blockers"] == []


def test_build_evidence_summary_collects_blockers_on_missed_sentinel(
    adapter: Any,
) -> None:
    """A run with a missing sentinel must surface the preflight blocker."""
    stdout = (
        '{"type":"system","subtype":"init","claude_code_version":"0.4.1"}\n'
        '{"type":"result","subtype":"success","result":"no sentinel here"}\n'
    )
    runner = _make_fake_runner(stdout=stdout, stderr="", returncode=0)
    run = adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    summary = adapter.build_evidence_summary(run)
    assert summary["ok"] is False
    assert summary["sentinel_ok"] is False
    # The preflight blocker is the well-known Skill sentinel missing one.
    assert "skill-preflight-sentinel-missing" in [
        b.get("id") for b in summary["blockers"]
    ]


def test_build_evidence_summary_collects_stream_json_blockers(adapter: Any) -> None:
    """Stream-json blockers (init missing) must appear in the summary."""
    runner = _make_fake_runner(stdout="not json\n", stderr="", returncode=0)
    run = adapter.run_skill_preflight(
        skill_root="/tmp/x",
        run_subprocess=runner,
    )
    summary = adapter.build_evidence_summary(run)
    assert summary["ok"] is False
    ids = [b.get("id") for b in summary["blockers"]]
    # The parser must flag missing init and missing result.
    assert "stream-json-init-missing" in ids
    assert "stream-json-result-missing" in ids


# --------------------------------------------------------------------------- #
# Binary resolution                                                           #
# --------------------------------------------------------------------------- #


def test_resolve_binary_returns_none_for_missing(adapter: Any) -> None:
    """An unknown binary name must return ``None``, not raise."""
    assert adapter.resolve_binary("definitely-not-a-binary-xyz") is None


def test_resolve_binary_finds_claude_when_present(adapter: Any) -> None:
    """``resolve_binary("claude")`` must return a path when ``claude`` is on PATH."""
    if not _which("claude"):
        pytest.skip("claude not on PATH in this environment")
    path = adapter.resolve_binary("claude")
    assert path is not None
    assert os.path.basename(path) == "claude"


def _which(name: str) -> str | None:
    """Tiny ``shutil.which`` wrapper so the test stays import-free of shutil."""
    import shutil

    return shutil.which(name)


# --------------------------------------------------------------------------- #
# Subagent argv builder + result shape (Issue #46 Task 2)                     #
# --------------------------------------------------------------------------- #
#
# The subagent path is the mirror of the Skill path: a pure argv builder
# + a single subprocess method that reads the materialized ``agents.json``
# and passes its content inline as the ``--agents`` flag value. The
# current ``claude`` runtime does not accept a file path on ``--agents``;
# the empirical verification is recorded in
# :func:`metacrucible.adapter_runtime.build_subagent_preflight_argv`.

# Minimal, deterministic subagent JSON used as the builder input. It is
# inline (a string), not a file path, because the harness only emits
# the inline shape.
SUBAGENT_INLINE_JSON: str = (
    '{"smoke-agent": {"description": "smoke subagent", '
    '"prompt": "emit sentinel"}}'
)

# Exact token shape the brief pins (the ``--verbose`` runtime flag is
# injected by the subprocess method, not by the pure builder).
EXPECTED_SUBAGENT_ARGV_TOKENS_DEFAULT: list[str] = [
    "claude",
    "--bare",
    "--agents",
    SUBAGENT_INLINE_JSON,
    "-p",
    "--output-format",
    "stream-json",
]


def test_adapter_module_exposes_build_subagent_preflight_argv(
    adapter: Any,
) -> None:
    """The harness must expose the pure subagent argv builder."""
    assert hasattr(adapter, "build_subagent_preflight_argv")
    assert callable(adapter.build_subagent_preflight_argv)


def test_adapter_module_exposes_run_subagent_preflight(adapter: Any) -> None:
    """The harness must expose the subagent subprocess method."""
    assert hasattr(adapter, "run_subagent_preflight")
    assert callable(adapter.run_subagent_preflight)


def test_adapter_module_exposes_subagent_preflight_run_dataclass(
    adapter: Any,
) -> None:
    """The harness must expose the :class:`SubagentPreflightRun` dataclass."""
    assert hasattr(adapter, "SubagentPreflightRun")
    field_names = set(adapter.SubagentPreflightRun.__dataclass_fields__.keys())
    # The result must surface every artifact the smoke pass writes.
    assert {
        "argv",
        "exit_code",
        "stdout",
        "stderr",
        "evidence",
        "preflight",
        "agents_path",
    }.issubset(field_names)


# --- argv builder shape ----------------------------------------------------- #


def test_build_subagent_argv_matches_brief_token_shape(adapter: Any) -> None:
    """The builder must emit the exact tokens the brief pins."""
    argv = adapter.build_subagent_preflight_argv(agents_json=SUBAGENT_INLINE_JSON)
    assert argv == EXPECTED_SUBAGENT_ARGV_TOKENS_DEFAULT


def test_build_subagent_argv_default_binary_is_claude(adapter: Any) -> None:
    """The default binary must be ``claude`` (Task 3 will override to ``omp``)."""
    argv = adapter.build_subagent_preflight_argv(agents_json=SUBAGENT_INLINE_JSON)
    assert argv[0] == "claude"


def test_build_subagent_argv_honors_binary_parameter(adapter: Any) -> None:
    """The builder must allow a custom binary name for Task 3 (oh-my-pi)."""
    argv = adapter.build_subagent_preflight_argv(
        agents_json=SUBAGENT_INLINE_JSON,
        binary="omp",
    )
    assert argv[0] == "omp"
    # The rest of the token shape must be byte-for-byte identical.
    assert argv[1:] == EXPECTED_SUBAGENT_ARGV_TOKENS_DEFAULT[1:]


def test_build_subagent_argv_passes_agents_json_inline(adapter: Any) -> None:
    """The ``--agents`` flag value must be the inline JSON literal (not a path)."""
    argv = adapter.build_subagent_preflight_argv(agents_json=SUBAGENT_INLINE_JSON)
    agents_idx = argv.index("--agents")
    assert argv[agents_idx + 1] == SUBAGENT_INLINE_JSON
    assert argv[agents_idx + 1].startswith("{")
    assert argv[agents_idx + 1].endswith("}")


def test_build_subagent_argv_uses_bare_flag(adapter: Any) -> None:
    """``--bare`` must be the second token (ADR 0028)."""
    argv = adapter.build_subagent_preflight_argv(agents_json=SUBAGENT_INLINE_JSON)
    assert argv.index("--bare") == 1


def test_build_subagent_argv_uses_print_flag(adapter: Any) -> None:
    """``-p`` must be present for non-interactive mode."""
    argv = adapter.build_subagent_preflight_argv(agents_json=SUBAGENT_INLINE_JSON)
    assert "-p" in argv


def test_build_subagent_argv_uses_stream_json_output(adapter: Any) -> None:
    """``--output-format stream-json`` must be the documented format."""
    argv = adapter.build_subagent_preflight_argv(agents_json=SUBAGENT_INLINE_JSON)
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"


def test_build_subagent_argv_does_not_include_verbose(adapter: Any) -> None:
    """``--verbose`` is a runtime requirement; the pure builder must omit it."""
    argv = adapter.build_subagent_preflight_argv(agents_json=SUBAGENT_INLINE_JSON)
    assert "--verbose" not in argv


def test_build_subagent_argv_rejects_empty_binary(adapter: Any) -> None:
    """An empty binary name must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_subagent_preflight_argv(
            agents_json=SUBAGENT_INLINE_JSON,
            binary="",
        )


def test_build_subagent_argv_rejects_empty_permission_mode(adapter: Any) -> None:
    """An empty permission mode must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_subagent_preflight_argv(
            agents_json=SUBAGENT_INLINE_JSON,
            permission_mode="",
        )


def test_build_subagent_argv_rejects_empty_output_format(adapter: Any) -> None:
    """An empty output format must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_subagent_preflight_argv(
            agents_json=SUBAGENT_INLINE_JSON,
            output_format="",
        )


def test_build_subagent_argv_rejects_empty_agents_json(adapter: Any) -> None:
    """An empty ``agents_json`` must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_subagent_preflight_argv(agents_json="")


def test_build_subagent_argv_rejects_non_string_agents_json(adapter: Any) -> None:
    """A non-string ``agents_json`` must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_subagent_preflight_argv(agents_json=123)  # type: ignore[arg-type]


# --- subprocess method via test seam ---------------------------------------- #


def _write_agents_json(path: Path) -> None:
    """Write a minimal ``agents.json`` file at ``path`` for the subprocess tests."""
    path.write_text(SUBAGENT_INLINE_JSON, encoding="utf-8")


def test_run_subagent_preflight_passes_subagent_preflight_prompt(
    adapter: Any, preflight: Any, tmp_path: Path
) -> None:
    """The harness must feed the subagent preflight prompt as final positional."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
        subagent_name="smoke-agent",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected_prompt = preflight.subagent_preflight_prompt(subagent_name="smoke-agent")
    assert full_argv[-1] == expected_prompt
    assert expected_prompt.startswith("You are running the MetaCrucible")


def test_run_subagent_preflight_injects_verbose_flag(
    adapter: Any, tmp_path: Path
) -> None:
    """``--verbose`` must be inserted immediately before ``-p``."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    p_idx = full_argv.index("-p")
    assert full_argv[p_idx - 1] == "--verbose"


def test_run_subagent_preflight_reads_agents_json_path(
    adapter: Any, tmp_path: Path
) -> None:
    """The harness must read the materialized ``agents.json`` from disk."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    agents_idx = full_argv.index("--agents")
    # The flag value must be the file contents, verbatim, not the path.
    assert full_argv[agents_idx + 1] == SUBAGENT_INLINE_JSON
    assert full_argv[agents_idx + 1] != str(agents_path)


def test_run_subagent_preflight_uses_capture_output(
    adapter: Any, tmp_path: Path
) -> None:
    """The harness must request captured stdout/stderr from the runner."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("check") is False


def test_run_subagent_preflight_passes_timeout(
    adapter: Any, tmp_path: Path
) -> None:
    """The harness must forward a timeout to the runner."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
        timeout=7.5,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("timeout") == 7.5


def test_run_subagent_preflight_parses_stream_json_through_existing_parser(
    adapter: Any, stream_json: Any, tmp_path: Path
) -> None:
    """The harness must reuse ``parse_stream_json`` (no parallel parser)."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    stdout = (
        '{"type":"system","subtype":"init","claude_code_version":"0.4.1"}\n'
        '{"type":"result","subtype":"success","result":"hello"}\n'
    )
    runner = _make_fake_runner(stdout=stdout, stderr="", returncode=0)
    run = adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    evidence = run.evidence
    assert evidence["start_captured"] is True
    assert evidence["completion_captured"] is True
    assert evidence["adapter_version"] == stream_json.ADAPTER_VERSION
    assert evidence["claude_code_version"] == "0.4.1"


def test_run_subagent_preflight_folds_final_output_through_check_subagent_preflight(
    adapter: Any, preflight: Any, tmp_path: Path
) -> None:
    """The harness must reuse ``check_subagent_preflight`` (no parallel validator)."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    sentinel = (
        f"{preflight.SUBAGENT_SENTINEL_PREFIX}=yes; NAME=smoke-agent"
    )
    stdout = (
        '{"type":"system","subtype":"init","claude_code_version":"0.4.1"}\n'
        f'{{"type":"result","subtype":"success","result":"{sentinel}"}}\n'
    )
    runner = _make_fake_runner(stdout=stdout, stderr="", returncode=0)
    run = adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    expected = preflight.check_subagent_preflight(sentinel)
    assert run.preflight == expected
    assert run.preflight.get("ok") is True
    assert run.preflight.get("discoverable") == "yes"
    assert run.preflight.get("name") == "smoke-agent"


def test_run_subagent_preflight_propagates_exit_code(
    adapter: Any, tmp_path: Path
) -> None:
    """The harness must surface the subprocess exit code verbatim."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="", stderr="boom", returncode=42)
    run = adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    assert run.exit_code == 42
    assert run.stderr == "boom"


def test_run_subagent_preflight_captures_raw_stdout(
    adapter: Any, tmp_path: Path
) -> None:
    """The harness must capture the raw stdout for evidence writes."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    stdout_blob = (
        '{"type":"system","subtype":"init","claude_code_version":"0.4.1"}\n'
    )
    runner = _make_fake_runner(stdout=stdout_blob, stderr="", returncode=0)
    run = adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    assert run.stdout == stdout_blob


def test_run_subagent_preflight_honors_explicit_prompt(
    adapter: Any, tmp_path: Path
) -> None:
    """An explicit prompt override must be appended verbatim."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
        prompt="CUSTOM PROMPT",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    assert full_argv[-1] == "CUSTOM PROMPT"


def test_run_subagent_preflight_default_prompt_uses_subagent_name(
    adapter: Any, preflight: Any, tmp_path: Path
) -> None:
    """The default prompt must fold the caller-supplied subagent name in."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
        subagent_name="my-agent",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected = preflight.subagent_preflight_prompt(subagent_name="my-agent")
    assert full_argv[-1] == expected


def test_local_real_subagent_confirm_prompt_renders_sentinel_and_name(
    adapter: Any, preflight: Any
) -> None:
    """The terse confirm-prompt helper must render the sentinel prefix + subagent name."""
    rendered = adapter.local_real_subagent_confirm_prompt(
        subagent_name="metacrucible-smoke-subagent"
    )
    # The sentinel prefix (from preflight) must be present so the
    # standard ``check_subagent_preflight`` parser classifies the
    # model's reply.
    assert preflight.SUBAGENT_SENTINEL_PREFIX in rendered
    # The subagent name must be folded into the prompt.
    assert "metacrucible-smoke-subagent" in rendered
    # The template constant must round-trip through the helper.
    assert (
        adapter.SUBAGENT_LOCAL_REAL_CONFIRM_PROMPT_TEMPLATE.format(
            prefix=preflight.SUBAGENT_SENTINEL_PREFIX,
            subagent_name="metacrucible-smoke-subagent",
        )
        == rendered
    )


def test_local_real_subagent_confirm_prompt_handles_empty_name(
    adapter: Any,
) -> None:
    """An empty subagent name must render ``<unknown>`` (defensive)."""
    rendered = adapter.local_real_subagent_confirm_prompt(subagent_name="")
    assert "<unknown>" in rendered


def test_run_subagent_preflight_default_uses_verbose_preflight_prompt(
    adapter: Any, preflight: Any, tmp_path: Path
) -> None:
    """The default path must keep the verbose ADR 0028 preflight prompt (no flag)."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
        subagent_name="my-agent",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected = preflight.subagent_preflight_prompt(subagent_name="my-agent")
    # Default (no local_real) must equal the verbose ADR 0028 prompt.
    assert full_argv[-1] == expected
    # And it must NOT be the terse confirm-prompt.
    terse = adapter.local_real_subagent_confirm_prompt(subagent_name="my-agent")
    assert full_argv[-1] != terse


def test_run_subagent_preflight_local_real_uses_confirm_prompt(
    adapter: Any, tmp_path: Path
) -> None:
    """``local_real=True`` must switch the harness to the terse confirm-prompt."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
        subagent_name="my-agent",
        local_real=True,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected = adapter.local_real_subagent_confirm_prompt(subagent_name="my-agent")
    # local_real=True must equal the terse confirm-prompt.
    assert full_argv[-1] == expected
    # And it must NOT be the verbose ADR 0028 prompt.
    from metacrucible.preflight import subagent_preflight_prompt
    assert full_argv[-1] != subagent_preflight_prompt(subagent_name="my-agent")


def test_run_subagent_preflight_local_real_does_not_break_explicit_prompt(
    adapter: Any, tmp_path: Path
) -> None:
    """An explicit ``prompt=`` must win over the ``local_real`` flag."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
        prompt="EXPLICIT PROMPT",
        local_real=True,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    assert full_argv[-1] == "EXPLICIT PROMPT"

def test_run_subagent_preflight_disable_verbose(
    adapter: Any, tmp_path: Path
) -> None:
    """Setting ``verbose=False`` must drop the ``--verbose`` injection."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
        verbose=False,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    assert "--verbose" not in full_argv


def test_run_subagent_preflight_records_agents_path(
    adapter: Any, tmp_path: Path
) -> None:
    """The result must surface the materialized ``agents.json`` path verbatim."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    run = adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    assert run.agents_path == str(agents_path)


def test_run_subagent_preflight_blocks_on_missing_file(adapter: Any) -> None:
    """A missing ``agents.json`` must surface a clear blocker, not crash."""
    missing = Path("/nonexistent/path/does/not/exist/agents.json")
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    run = adapter.run_subagent_preflight(
        agents_path=missing,
        run_subprocess=runner,
    )
    # No subprocess should have been spawned when the file is missing.
    assert runner.calls == []  # type: ignore[attr-defined]
    assert run.agents_path == str(missing)
    assert run.exit_code == -1
    assert "failed to read materialized agents.json" in run.stderr


def test_run_subagent_preflight_argv_does_not_include_subprocess_seam(
    adapter: Any, tmp_path: Path
) -> None:
    """The recorded ``argv`` is what would be spawned; no test-only artifacts."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    run = adapter.run_subagent_preflight(
        agents_path=agents_path,
        run_subprocess=runner,
    )
    # The pure builder emits 7 tokens; the runner appends --verbose and
    # the prompt, so the final argv must be 9 tokens (not 11+, which
    # would mean a fake-kwargs leak).
    assert len(run.argv) == 9
    assert run.argv[0] == "claude"
    assert run.argv[-1].startswith("You are running the MetaCrucible")


# --------------------------------------------------------------------------- #
# omp shared-layout argv builders (Issue #46 Task 3)                          #
# --------------------------------------------------------------------------- #
#
# The omp runtime reads Skills and subagents from the SHARED
# ``.claude/skills/<name>/SKILL.md`` and ``.claude/agents/agents.json``
# layout under ``--cwd``. The harness emits the pure token shape pinned
# by the brief; tests below prove the shape without spawning the binary.

OMP_PROMPT: str = "Run the MetaCrucible Skill preflight now."

EXPECTED_OMP_ARGV_TOKENS_DEFAULT: list[str] = [
    "omp",
    "--cwd",
    "/tmp/isolated",
    "-p",
    "--mode",
    "text",
    "--allow-home",
    OMP_PROMPT,
]


def test_adapter_module_exposes_omp_runtime_constants(adapter: Any) -> None:
    """The harness must expose the omp runtime discriminator constants."""
    assert adapter.RUNTIME_CLAUDE == "claude"
    assert adapter.RUNTIME_OMP == "omp"
    assert adapter.OMP_ADAPTER_VERSION == "oh-my-pi/16.1.19"


def test_adapter_module_exposes_omp_argv_builders(adapter: Any) -> None:
    """The harness must expose the omp Skill and subagent argv builders."""
    assert hasattr(adapter, "build_omp_skill_preflight_argv")
    assert callable(adapter.build_omp_skill_preflight_argv)
    assert hasattr(adapter, "build_omp_subagent_preflight_argv")
    assert callable(adapter.build_omp_subagent_preflight_argv)


def test_adapter_module_exposes_omp_subprocess_methods(adapter: Any) -> None:
    """The harness must expose the omp Skill and subagent subprocess methods."""
    assert hasattr(adapter, "run_omp_skill_preflight")
    assert callable(adapter.run_omp_skill_preflight)
    assert hasattr(adapter, "run_omp_subagent_preflight")
    assert callable(adapter.run_omp_subagent_preflight)


def test_skill_preflight_run_dataclass_has_runtime_field(adapter: Any) -> None:
    """``SkillPreflightRun`` must surface the runtime discriminator."""
    fields = set(adapter.SkillPreflightRun.__dataclass_fields__.keys())
    assert "runtime" in fields


def test_subagent_preflight_run_dataclass_has_runtime_field(adapter: Any) -> None:
    """``SubagentPreflightRun`` must surface the runtime discriminator."""
    fields = set(adapter.SubagentPreflightRun.__dataclass_fields__.keys())
    assert "runtime" in fields


# --- omp Skill argv builder -------------------------------------------------- #


def test_build_omp_skill_argv_matches_brief_token_shape(adapter: Any) -> None:
    """The omp Skill argv builder must emit the exact token shape pinned by the brief."""
    argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt=OMP_PROMPT,
    )
    assert argv == EXPECTED_OMP_ARGV_TOKENS_DEFAULT


def test_build_omp_skill_argv_uses_cwd_flag(adapter: Any) -> None:
    """``--cwd`` must carry the isolated root as the second token after the binary."""
    argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/scratch/.claude-root",
        prompt=OMP_PROMPT,
    )
    assert argv[0] == "omp"
    assert argv[1] == "--cwd"
    assert argv[2] == "/scratch/.claude-root"


def test_build_omp_skill_argv_omits_no_tools(adapter: Any) -> None:
    """``--no-tools`` must NOT be present: it disables omp artifact injection.

    omp treats Skills and subagents as a tool-side feature; passing
    ``--no-tools`` stops the runtime from loading
    ``.claude/skills/<name>/SKILL.md`` and
    ``.claude/agents/agents.json``, which makes the preflight's
    ``discoverable=yes`` check impossible. Side-effect safety for the
    smoke run comes from ``-p`` (non-interactive single turn), not from
    a tool-disabling flag.
    """
    argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt=OMP_PROMPT,
    )
    assert "--no-tools" not in argv, (
        f"omp argv must not include --no-tools (defeats artifact injection); got {argv!r}"
    )


def test_build_omp_skill_argv_uses_text_mode(adapter: Any) -> None:
    """``--mode text`` must be present so stdout is plain text (not json)."""
    argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt=OMP_PROMPT,
    )
    idx = argv.index("--mode")
    assert argv[idx + 1] == "text"


def test_build_omp_skill_argv_uses_allow_home(adapter: Any) -> None:
    """``--allow-home`` must be present so omp does not auto-switch to a tmp dir."""
    argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt=OMP_PROMPT,
    )
    assert "--allow-home" in argv


def test_build_omp_skill_argv_prompt_is_positional(adapter: Any) -> None:
    """The preflight prompt must be the final positional token (omp takes it as arg)."""
    argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt="CUSTOM PROMPT",
    )
    assert argv[-1] == "CUSTOM PROMPT"


def test_build_omp_skill_argv_uses_print_flag(adapter: Any) -> None:
    """``-p`` must be present for non-interactive mode."""
    argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt=OMP_PROMPT,
    )
    assert "-p" in argv


def test_build_omp_skill_argv_does_not_include_claude_only_flags(adapter: Any) -> None:
    """The omp argv must not include ``--bare`` / ``--add-dir`` / ``--output-format``."""
    argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt=OMP_PROMPT,
    )
    for forbidden in ("--bare", "--add-dir", "--output-format", "--allowed-tools", "--verbose"):
        assert forbidden not in argv, (
            f"omp argv must not include {forbidden!r}; got {argv!r}"
        )


def test_build_omp_skill_argv_rejects_empty_prompt(adapter: Any) -> None:
    """An empty prompt must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_omp_skill_preflight_argv(
            isolated_root="/tmp/isolated",
            prompt="",
        )


# --- omp subagent argv builder ---------------------------------------------- #


def test_build_omp_subagent_argv_matches_skill_shape(adapter: Any) -> None:
    """omp discovers subagents from the same ``.claude/`` layout; argv shape matches Skill."""
    skill_argv = adapter.build_omp_skill_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt=OMP_PROMPT,
    )
    subagent_argv = adapter.build_omp_subagent_preflight_argv(
        isolated_root="/tmp/isolated",
        prompt=OMP_PROMPT,
    )
    assert subagent_argv == skill_argv


def test_build_omp_subagent_argv_rejects_empty_prompt(adapter: Any) -> None:
    """An empty prompt must fail loudly."""
    with pytest.raises(ValueError):
        adapter.build_omp_subagent_preflight_argv(
            isolated_root="/tmp/isolated",
            prompt="",
        )


# --- omp subprocess method (Skill) via test seam ---------------------------- #


def test_run_omp_skill_preflight_passes_skill_preflight_prompt(
    adapter: Any, preflight: Any
) -> None:
    """The omp Skill harness must feed the preflight prompt to the binary as positional."""
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_omp_skill_preflight(
        isolated_root="/tmp/isolated",
        run_subprocess=runner,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected_prompt = preflight.skill_preflight_prompt()
    assert full_argv[-1] == expected_prompt


def test_run_omp_skill_preflight_uses_capture_output(adapter: Any) -> None:
    """The omp subprocess must request captured stdout/stderr from the runner."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_omp_skill_preflight(
        isolated_root="/tmp/isolated",
        run_subprocess=runner,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("check") is False


def test_run_omp_skill_preflight_passes_timeout(adapter: Any) -> None:
    """The omp subprocess must forward a timeout to the runner."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_omp_skill_preflight(
        isolated_root="/tmp/isolated",
        run_subprocess=runner,
        timeout=7.5,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("timeout") == 7.5


def test_run_omp_skill_preflight_spawns_from_isolated_root(adapter: Any) -> None:
    """The omp subprocess must default its cwd to the isolated root."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_omp_skill_preflight(
        isolated_root="/scratch/isolated",
        run_subprocess=runner,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("cwd") == "/scratch/isolated"

def test_run_omp_skill_preflight_folds_text_through_check_skill_preflight(
    adapter: Any, preflight: Any
) -> None:
    """The omp harness must reuse ``check_skill_preflight`` (no parallel validator)."""
    sentinel = "METACRUCIBLE_SKILL_DISCOVERABLE=yes; NAME=metacrucible-smoke"
    runner = _make_fake_runner(stdout=sentinel, stderr="", returncode=0)
    run = adapter.run_omp_skill_preflight(
        isolated_root="/tmp/isolated",
        run_subprocess=runner,
    )
    expected = preflight.check_skill_preflight(sentinel)
    assert run.preflight == expected
    assert run.preflight.get("ok") is True
    assert run.preflight.get("name") == "metacrucible-smoke"


def test_run_omp_skill_preflight_does_not_parse_stream_json(
    adapter: Any, stream_json: Any
) -> None:
    """The omp harness must NOT pipe stdout through ``parse_stream_json`` (text mode)."""
    runner = _make_fake_runner(stdout="plain text sentinel\n", stderr="", returncode=0)
    run = adapter.run_omp_skill_preflight(
        isolated_root="/tmp/isolated",
        run_subprocess=runner,
    )
    # final_output must be the raw stdout verbatim, not stream-json parsed.
    assert run.evidence["final_output"] == "plain text sentinel\n"
    # omp has its own adapter_version (no claude_code_version in the evidence).
    assert run.evidence["adapter_version"] == "oh-my-pi/16.1.19"
    assert run.evidence["claude_code_version"] is None


def test_run_omp_skill_preflight_records_runtime_field(adapter: Any) -> None:
    """The result must carry ``runtime="omp"`` so callers can branch uniformly."""
    runner = _make_fake_runner(stdout="ok", stderr="", returncode=0)
    run = adapter.run_omp_skill_preflight(
        isolated_root="/tmp/isolated",
        run_subprocess=runner,
    )
    assert run.runtime == "omp"


def test_run_omp_skill_preflight_default_prompt_uses_skill_name(
    adapter: Any, preflight: Any
) -> None:
    """The omp harness must fold the caller-supplied skill name into the preflight prompt."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_omp_skill_preflight(
        isolated_root="/tmp/isolated",
        run_subprocess=runner,
        skill_name="my-skill",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected = preflight.skill_preflight_prompt(skill_name="my-skill")
    assert full_argv[-1] == expected


def test_run_omp_skill_preflight_honors_explicit_prompt(adapter: Any) -> None:
    """An explicit prompt override must be appended verbatim."""
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_omp_skill_preflight(
        isolated_root="/tmp/isolated",
        run_subprocess=runner,
        prompt="CUSTOM PROMPT",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    assert full_argv[-1] == "CUSTOM PROMPT"


# --- omp subprocess method (subagent) via test seam ------------------------- #


def test_run_omp_subagent_preflight_passes_subagent_preflight_prompt(
    adapter: Any, preflight: Any, tmp_path: Path
) -> None:
    """The omp subagent harness must feed the preflight prompt as final positional."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
        subagent_name="smoke-agent",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected_prompt = preflight.subagent_preflight_prompt(subagent_name="smoke-agent")
    assert full_argv[-1] == expected_prompt


def test_run_omp_subagent_preflight_copies_agents_to_shared_layout(
    adapter: Any, tmp_path: Path
) -> None:
    """The harness must write the subagent JSON into the SHARED omp layout."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
    )
    omp_layout = tmp_path / ".claude" / "agents" / "agents.json"
    assert omp_layout.is_file(), (
        f"shared-layout omp agents.json was not written at {omp_layout}"
    )
    assert omp_layout.read_text(encoding="utf-8") == SUBAGENT_INLINE_JSON


def test_run_omp_subagent_preflight_uses_capture_output(
    adapter: Any, tmp_path: Path
) -> None:
    """The omp subagent subprocess must request captured stdout/stderr."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("check") is False


def test_run_omp_subagent_preflight_spawns_from_isolated_root(
    adapter: Any, tmp_path: Path
) -> None:
    """The omp subagent subprocess must default its cwd to the isolated root."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
    )
    kwargs = runner.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("cwd") == str(tmp_path)


def test_run_omp_subagent_preflight_folds_text_through_check_subagent_preflight(
    adapter: Any, preflight: Any, tmp_path: Path
) -> None:
    """The omp subagent harness must reuse ``check_subagent_preflight`` (no parallel validator)."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    sentinel = (
        f"{preflight.SUBAGENT_SENTINEL_PREFIX}=yes; NAME=smoke-agent"
    )
    runner = _make_fake_runner(stdout=sentinel, stderr="", returncode=0)
    run = adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
    )
    expected = preflight.check_subagent_preflight(sentinel)
    assert run.preflight == expected
    assert run.preflight.get("ok") is True


def test_run_omp_subagent_preflight_records_runtime_field(
    adapter: Any, tmp_path: Path
) -> None:
    """The omp subagent result must carry ``runtime="omp"``."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="", stderr="", returncode=0)
    run = adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
    )
    assert run.runtime == "omp"


def test_run_omp_subagent_preflight_blocks_on_missing_file(
    adapter: Any, tmp_path: Path
) -> None:
    """A missing ``agents.json`` must surface a clear blocker, not crash."""
    missing = tmp_path / "does-not-exist-agents.json"
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    run = adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=missing,
        run_subprocess=runner,
    )
    assert runner.calls == []  # type: ignore[attr-defined]
    assert run.runtime == "omp"
    assert "omp agents.json" in run.stderr

def test_run_omp_subagent_preflight_default_uses_verbose_preflight_prompt(
    adapter: Any, preflight: Any, tmp_path: Path
) -> None:
    """The omp default path must keep the verbose ADR 0028 preflight prompt (no flag)."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
        subagent_name="smoke-agent",
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected = preflight.subagent_preflight_prompt(subagent_name="smoke-agent")
    # Default (no local_real) must equal the verbose ADR 0028 prompt.
    assert full_argv[-1] == expected
    # And it must NOT be the terse confirm-prompt.
    terse = adapter.local_real_subagent_confirm_prompt(subagent_name="smoke-agent")
    assert full_argv[-1] != terse


def test_run_omp_subagent_preflight_local_real_uses_confirm_prompt(
    adapter: Any, tmp_path: Path
) -> None:
    """``local_real=True`` must switch the omp harness to the terse confirm-prompt."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
        subagent_name="smoke-agent",
        local_real=True,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    expected = adapter.local_real_subagent_confirm_prompt(subagent_name="smoke-agent")
    # local_real=True must equal the terse confirm-prompt.
    assert full_argv[-1] == expected
    # And it must NOT be the verbose ADR 0028 prompt.
    from metacrucible.preflight import subagent_preflight_prompt
    assert full_argv[-1] != subagent_preflight_prompt(subagent_name="smoke-agent")


def test_run_omp_subagent_preflight_local_real_does_not_break_explicit_prompt(
    adapter: Any, tmp_path: Path
) -> None:
    """An explicit ``prompt=`` must win over the omp ``local_real`` flag."""
    agents_path = tmp_path / "agents.json"
    _write_agents_json(agents_path)
    runner = _make_fake_runner(stdout="not-json\n", stderr="", returncode=0)
    adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        run_subprocess=runner,
        prompt="EXPLICIT PROMPT",
        local_real=True,
    )
    full_argv = runner.calls[0]["argv"]  # type: ignore[attr-defined]
    assert full_argv[-1] == "EXPLICIT PROMPT"


# --- shared-layout artifact paths (the ADR 0003 contract) ------------------- #


def test_materialize_skill_writes_into_omp_shared_layout(
    adapter: Any, tmp_path: Path
) -> None:
    """``materialize_skill`` writes the SHARED layout omp reads from ``--cwd``."""
    # The omp harness points ``--cwd`` at ``isolated_root`` and relies
    # on ``<isolated_root>/.claude/skills/<name>/SKILL.md`` being
    # discoverable. ``materialize_skill`` writes exactly that path —
    # the brief's "shared layout" contract — so no parallel materializer
    # is needed for the omp Skill path.
    result = adapter.materialize_skill(
        skill_name="shared-layout",
        skill_body="body",
        output_dir=tmp_path,
    )
    assert result.ok is True
    shared_layout = tmp_path / ".claude" / "skills" / "shared-layout" / "SKILL.md"
    assert shared_layout.is_file()
    assert Path(result.skill_md_path) == shared_layout


def test_subagent_injection_materializes_into_omp_compatible_json(
    adapter: Any, tmp_path: Path
) -> None:
    """``materialize_subagent`` writes JSON whose shape omp consumes (ADR 0003)."""
    import importlib

    subagent_injection = importlib.import_module("metacrucible.subagent_injection")
    parser = importlib.import_module("metacrucible.artifact")

    artifact = parser.parse_subagent(SMOKE_SUBAGENT_SOURCE_FOR_TEST)
    materialization = subagent_injection.materialize_subagent(artifact, tmp_path)
    assert materialization.get("ok") is True
    # The materializer writes the file the claude ``--agents`` flag
    # loads; the omp harness copies it into the omp shared layout.
    agents_path = Path(materialization["agents_path"])
    assert agents_path.is_file()
    assert json.loads(agents_path.read_text(encoding="utf-8")).get(
        "metacrucible-omp-test"
    ) is not None


SMOKE_SUBAGENT_SOURCE_FOR_TEST: str = (
    "---\n"
    "name: metacrucible-omp-test\n"
    "description: MetaCrucible omp shared-layout test subagent.\n"
    "tools:\n"
    "  - Read\n"
    "systemPrompt: |\n"
    "  echo subagent body.\n"
    "---\n"
)

# Use stdlib json (already available via _dump_pretty pattern elsewhere).
import json
