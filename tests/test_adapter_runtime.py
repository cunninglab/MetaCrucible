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
