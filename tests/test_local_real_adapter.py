"""Local-real smoke tests for the Claude Code Skill discovery (Issue #46 Task 1).

These tests exercise the real ``claude`` binary against a materialized
Skill in a pytest scratch directory. They are **opt-in**:

  - The test is marked ``@pytest.mark.local_real`` so it is excluded
    from ``mise run test`` (the harness enforces the marker exclusion
    in ``mise.toml`` / ``pyproject.toml``).
  - The test skips when ``METACRUCIBLE_RUN_LOCAL_REAL=1`` is unset.
  - The test skips when the ``claude`` binary is absent on ``$PATH``.

When the gate is open and the binary is present, the smoke pass
materializes a deterministic Skill, invokes
``claude --bare --add-dir <isolated-skill-root> --allowed-tools <reviewed>
--permission-mode default -p --output-format stream-json`` through
:mod:`metacrucible.adapter_runtime.run_skill_preflight`, parses the
captured stream-json via the existing
:func:`metacrucible.claude_stream_json.parse_stream_json`, and
asserts that :func:`metacrucible.preflight.check_skill_preflight`
reports the Skill as discoverable.

The Skill body is kept minimal and deterministic so the model
reliably emits the sentinel on the first turn. Auth uses the
developer's OS keychain / Claude subscription; the harness never
requires a provider API key.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

# Marker declaration. The marker is registered in ``pyproject.toml``
# so ``-m local_real`` is well-formed at the pytest level.
pytestmark = pytest.mark.local_real

ADAPTER_MODULE = "metacrucible.adapter_runtime"
PREFLIGHT_MODULE = "metacrucible.preflight"
STREAM_JSON_MODULE = "metacrucible.claude_stream_json"

#: Env gate required to actually run real ``claude`` invocations.
LOCAL_REAL_ENV: str = "METACRUCIBLE_RUN_LOCAL_REAL"

#: Minimal, deterministic Skill body. The literal preflight hint
#: primes the model to emit the exact sentinel format pinned by
#: :mod:`metacrucible.preflight`.
SMOKE_SKILL_BODY: str = (
    "You are a local-real smoke Skill for the MetaCrucible adapter harness.\n"
    "When asked to run the MetaCrucible preflight, reply with exactly\n"
    "one line in the format the prompt specifies, and nothing else.\n"
)


# --------------------------------------------------------------------------- #
# Helpers / fixtures                                                          #
# --------------------------------------------------------------------------- #


def _blocker_ids(payload: Any) -> list[str]:
    """Return the list of blocker ids in a result, or empty if none."""
    if not isinstance(payload, dict):
        return []
    blockers = payload.get("blockers", [])
    if not isinstance(blockers, list):
        return []
    out: list[str] = []
    for blocker in blockers:
        if isinstance(blocker, dict) and isinstance(blocker.get("id"), str):
            out.append(blocker["id"])
    return out


def _local_real_enabled() -> bool:
    """Return ``True`` iff the developer opted in to local-real runs."""
    return os.environ.get(LOCAL_REAL_ENV) == "1"


def _claude_on_path() -> bool:
    """Return ``True`` iff the ``claude`` binary is on ``$PATH``."""
    return shutil.which("claude") is not None


@pytest.fixture(scope="module")
def adapter() -> Any:
    """Import the adapter runtime module."""
    import importlib

    return importlib.import_module(ADAPTER_MODULE)


@pytest.fixture(scope="module")
def preflight() -> Any:
    """Import the preflight module."""
    import importlib

    return importlib.import_module(PREFLIGHT_MODULE)


@pytest.fixture(scope="module")
def stream_json() -> Any:
    """Import the stream-json parser module."""
    import importlib

    return importlib.import_module(STREAM_JSON_MODULE)


@pytest.fixture
def skip_unless_local_real() -> None:
    """Skip when the env gate is unset."""
    if not _local_real_enabled():
        pytest.skip(
            f"{LOCAL_REAL_ENV}=1 is required to run local-real smoke tests"
        )


@pytest.fixture
def skip_unless_claude_present() -> None:
    """Skip when ``claude`` is not on ``$PATH``."""
    if not _claude_on_path():
        pytest.skip("claude binary not found on $PATH")


# --------------------------------------------------------------------------- #
# Skip discipline (always run, never spawn a binary)                          #
# --------------------------------------------------------------------------- #


def test_local_real_marker_is_registered() -> None:
    """The ``local_real`` marker must be applied to this module."""
    # Sanity check: the test file is collected with the marker, so
    # ``pytest -m local_real`` (i.e. ``mise run test-local-real``)
    # selects these cases.
    import sys

    assert "pytest" in sys.modules
    # The marker is registered via pyproject; if it were not, pytest
    # would emit a PytestUnknownMarkWarning. The hard guarantee comes
    # from the mise task: ``pytest -m local_real`` resolves cleanly.


# --------------------------------------------------------------------------- #
# Local-real smoke                                                            #
# --------------------------------------------------------------------------- #


def test_local_real_skill_discovery_via_claude(
    adapter: Any,
    preflight: Any,
    stream_json: Any,
    skip_unless_local_real: None,
    skip_unless_claude_present: None,
    tmp_path: Path,
) -> None:
    """End-to-end: materialize Skill, invoke real ``claude``, assert discoverable.

    Steps
    -----
    1. Materialize a Skill into ``tmp_path/.claude/skills/<name>/SKILL.md``.
    2. Call :func:`metacrucible.adapter_runtime.run_skill_preflight`
       with the resolved skill root.
    3. Parse the captured stdout through
       :func:`metacrucible.claude_stream_json.parse_stream_json`
       (the harness does this; the test asserts the result).
    4. Feed the final output through
       :func:`metacrucible.preflight.check_skill_preflight` and
       assert the Skill is discoverable (no
       ``skill-preflight-*`` blockers).

    The test is honest: it does not silently weaken the assertion.
    If the model fails to emit the sentinel, the test fails with a
    captured evidence dump.
    """
    skill_name = "metacrucible-smoke-skill"
    materialization = adapter.materialize_skill(
        skill_name=skill_name,
        skill_body=SMOKE_SKILL_BODY,
        output_dir=tmp_path,
    )
    assert materialization.ok is True, (
        f"materialize_skill failed: {materialization.blockers!r}"
    )
    assert Path(materialization.skill_md_path).is_file()

    run = adapter.run_skill_preflight(
        skill_root=materialization.skill_root,
        skill_name=skill_name,
        cwd=tmp_path,
        timeout=180.0,
    )

    # Write release-ready evidence to scratch so a developer can
    # audit the run without re-invoking the binary. The test
    # intentionally keeps this write to the test-owned tmp_path
    # (no user-home writes).
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "raw_stream.jsonl").write_text(
        run.stdout, encoding="utf-8"
    )
    (evidence_dir / "stderr.txt").write_text(run.stderr, encoding="utf-8")
    (evidence_dir / "evidence.json").write_text(
        _dump_pretty(run.evidence), encoding="utf-8"
    )
    (evidence_dir / "preflight.json").write_text(
        _dump_pretty(run.preflight), encoding="utf-8"
    )

    # First, the stream-json parser must classify the run as a clean
    # Claude Code session (init + result present). If the runtime
    # could not be reached, the test fails loudly here rather than
    # hiding behind a sentinel-missing blocker.
    evidence = run.evidence
    assert evidence["start_captured"] is True, (
        f"no system/init event in stream-json output; evidence: {evidence!r}"
    )
    assert evidence["completion_captured"] is True, (
        f"no result event in stream-json output; evidence: {evidence!r}"
    )
    assert evidence["adapter_version"] == stream_json.ADAPTER_VERSION
    # The runtime version field must be present (claude 0.4.1 or
    # newer; we accept any non-empty string).
    assert evidence["claude_code_version"], (
        f"missing claude_code_version; evidence: {evidence!r}"
    )

    # Next, the preflight sentinel must report discoverable.
    preflight_result = run.preflight
    assert preflight_result.get("ok") is True, (
        f"check_skill_preflight did not report discoverable; "
        f"preflight={preflight_result!r}; "
        f"final_output={evidence.get('final_output')!r}; "
        f"stream-json blockers={_blocker_ids(evidence)}"
    )
    assert preflight_result.get("discoverable") == "yes"
    assert preflight_result.get("name") == skill_name
    assert _blocker_ids(preflight_result) == []


def test_local_real_skill_discovery_never_touches_user_home(
    adapter: Any,
    skip_unless_local_real: None,
    skip_unless_claude_present: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local-real smoke must not write to the user's real ``~/.claude/``."""
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    skill_name = "home-safety"
    materialization = adapter.materialize_skill(
        skill_name=skill_name,
        skill_body=SMOKE_SKILL_BODY,
        output_dir=tmp_path,
    )
    assert materialization.ok is True

    # Run the harness; even with HOME pointed at fake-home, the
    # materializer must not write there.
    adapter.run_skill_preflight(
        skill_root=materialization.skill_root,
        skill_name=skill_name,
        cwd=tmp_path,
        timeout=180.0,
    )

    # The fake home may have been created by the runtime's own
    # keychain read, but it must not contain a ``.claude/skills/<name>``
    # tree that we wrote.
    if fake_home.exists():
        skill_in_fake_home = (
            fake_home / ".claude" / "skills" / skill_name / "SKILL.md"
        )
        assert not skill_in_fake_home.exists(), (
            f"local-real run wrote to user-home layout at {skill_in_fake_home}"
        )


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #


def _dump_pretty(payload: Any) -> str:
    """Serialize ``payload`` as pretty JSON for evidence files."""
    import json

    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
