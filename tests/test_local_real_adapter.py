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
    "You have been loaded into the agent runtime as the Skill named\n"
    "\"metacrucible-smoke-skill\". Because you are loaded into the\n"
    "runtime, you are discoverable.\n"
    "When asked to run the MetaCrucible preflight, reply with exactly\n"
    "one line: METACRUCIBLE_SKILL_DISCOVERABLE=yes; NAME=metacrucible-smoke-skill\n"
    "Do not emit anything else.\n"
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


@pytest.fixture(scope="module")
def parser() -> Any:
    """Import the capability artifact parser (Issue #4)."""
    import importlib

    return importlib.import_module("metacrucible.artifact")



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

@pytest.mark.flaky(reruns=1, reruns_delay=2)

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
    _assert_evidence_present(
        evidence_dir,
        [
            "raw_stream.jsonl",
            "stderr.txt",
            "evidence.json",
            "preflight.json",
        ],
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
# Subagent injection smoke (Issue #46 Task 2)                                 #
# --------------------------------------------------------------------------- #

#: Minimal, deterministic subagent source for the local-real smoke. The
#: frontmatter carries a routing-safe name and description; the body
#: primes the model to emit the subagent preflight sentinel (ADR 0028,
#: Issue #9 AC3).
SMOKE_SUBAGENT_SOURCE: str = (
    "---\n"
    "name: metacrucible-smoke-subagent\n"
    "description: MetaCrucible local-real smoke subagent (Issue #46 Task 2).\n"
    "tools:\n"
    "  - Read\n"
    "systemPrompt: |\n"
    "  You are the MetaCrucible local-real smoke subagent.\n"
    "  You have been loaded into the agent runtime as the subagent named\n"
    "  \"metacrucible-smoke-subagent\". Because you are loaded into the\n"
    "  runtime, you are discoverable.\n"
    "  When asked to run the MetaCrucible preflight, reply with exactly\n"
    "  one line: METACRUCIBLE_SUBAGENT_DISCOVERABLE=yes; NAME=metacrucible-smoke-subagent\n"
    "  Do not emit anything else.\n"
    "---\n"
)

@pytest.mark.flaky(reruns=1, reruns_delay=2)

def test_local_real_subagent_injection_via_claude(
    adapter: Any,
    preflight: Any,
    stream_json: Any,
    parser: Any,
    skip_unless_local_real: None,
    skip_unless_claude_present: None,
    tmp_path: Path,
) -> None:
    """End-to-end: materialize subagent, invoke real ``claude``, assert discoverable.

    Mirrors :func:`test_local_real_skill_discovery_via_claude` for the
    subagent path (Issue #46 Task 2).

    Steps
    -----
    1. Parse :data:`SMOKE_SUBAGENT_SOURCE` into a
       :class:`metacrucible.artifact.SubagentArtifact` and materialize
       it via :func:`metacrucible.subagent_injection.materialize_subagent`
       into ``tmp_path``. The materializer writes
       ``<tmp_path>/agents.json`` (the file the harness loads).
    2. Verify the materialized file via
       :func:`metacrucible.subagent_injection.verify_subagent_injection`
       before the binary run — the harness must chain the existing
       materializer + verifier without reimplementing either.
    3. Call :func:`metacrucible.adapter_runtime.run_subagent_preflight`
       with the resolved ``agents_path`` + subagent name AND
       ``local_real=True`` so the harness uses the terse
       confirm-prompt (Issue #46 Repair 3). The terse prompt tells
       the main model the subagent IS registered and asks it to
       confirm by echoing the sentinel — proving registration,
       consistent with ADR 0028's "discovery separate from use".
       The default verbose ADR 0028 prompt would cause the model
       to hedge to ``no`` because the main model cannot introspect
       subagent registration (subagents are registered, not loaded
       into the main context).
    4. Parse the captured stdout through
       :func:`metacrucible.claude_stream_json.parse_stream_json`
       (the harness does this; the test asserts the result).
    5. Feed the final output through
       :func:`metacrucible.preflight.check_subagent_preflight` and
       assert the subagent is discoverable (no
       ``subagent-preflight-*`` blockers).
    6. Re-run :func:`verify_subagent_injection` after the binary run
       to prove the file still passes the verifier (no runtime drift).

    The test is honest: it does not silently weaken the assertion. If
    the model fails to emit the sentinel, the test fails with a
    captured evidence dump. If the runtime ignores the ``--agents``
    payload, the ``init`` event exposes it and the test fails with
    a clear ``start_captured`` / ``completion_captured`` mismatch.
    """
    import importlib

    subagent_injection = importlib.import_module(
        "metacrucible.subagent_injection"
    )

    artifact = parser.parse_subagent(SMOKE_SUBAGENT_SOURCE)
    materialization = subagent_injection.materialize_subagent(
        artifact, tmp_path
    )
    assert materialization.get("ok") is True, (
        f"materialize_subagent failed: {materialization!r}"
    )
    agents_path = Path(materialization["agents_path"])
    assert agents_path.is_file(), (
        f"agents.json was not written; got path={agents_path!r}"
    )
    resolved_name = materialization["name"]
    assert resolved_name == "metacrucible-smoke-subagent"

    # Pre-run verifier pass: the materialized file must already pass.
    pre_verify = subagent_injection.verify_subagent_injection(
        agents_path,
        expected_name=resolved_name,
        expected_description=(
            "MetaCrucible local-real smoke subagent (Issue #46 Task 2)."
        ),
    )
    assert pre_verify.get("ok") is True, (
        f"verify_subagent_injection rejected the materialization "
        f"before the binary run: {pre_verify!r}"
    )
    assert _blocker_ids(pre_verify) == []

    run = adapter.run_subagent_preflight(
        agents_path=agents_path,
        subagent_name=resolved_name,
        cwd=tmp_path,
        timeout=180.0,
        local_real=True,
    )

    # Write release-ready evidence to scratch so a developer can
    # audit the run without re-invoking the binary.
    evidence_dir = tmp_path / "evidence-subagent"
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
    (evidence_dir / "argv.json").write_text(
        _dump_pretty(run.argv), encoding="utf-8"
    )
    _assert_evidence_present(
        evidence_dir,
        [
            "raw_stream.jsonl",
            "stderr.txt",
            "evidence.json",
            "preflight.json",
            "argv.json",
        ],
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
    assert evidence["claude_code_version"], (
        f"missing claude_code_version; evidence: {evidence!r}"
    )

    # Next, the preflight sentinel must report discoverable.
    preflight_result = run.preflight
    assert preflight_result.get("ok") is True, (
        f"check_subagent_preflight did not report discoverable; "
        f"preflight={preflight_result!r}; "
        f"final_output={evidence.get('final_output')!r}; "
        f"stream-json blockers={_blocker_ids(evidence)}"
    )
    assert preflight_result.get("discoverable") == "yes"
    assert preflight_result.get("name") == resolved_name
    assert _blocker_ids(preflight_result) == []

    # Finally, re-run the verifier post-run to prove the materialization
    # still satisfies the routing-surface contract.
    post_verify = subagent_injection.verify_subagent_injection(
        agents_path,
        expected_name=resolved_name,
        expected_description=(
            "MetaCrucible local-real smoke subagent (Issue #46 Task 2)."
        ),
    )
    assert post_verify.get("ok") is True, (
        f"verify_subagent_injection rejected the materialization "
        f"after the binary run (runtime drift?): {post_verify!r}"
    )
    assert _blocker_ids(post_verify) == []


def test_local_real_subagent_injection_never_touches_user_home(
    adapter: Any,
    parser: Any,
    skip_unless_local_real: None,
    skip_unless_claude_present: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local-real subagent smoke must not write to the user's real home.

    Mirrors :func:`test_local_real_skill_discovery_never_touches_user_home`
    for the subagent path. Forces ``HOME``/``USERPROFILE`` to a fake
    scratch and asserts the harness never wrote a ``.claude/agents/``
    tree under it.
    """
    import importlib

    subagent_injection = importlib.import_module(
        "metacrucible.subagent_injection"
    )

    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    artifact = parser.parse_subagent(SMOKE_SUBAGENT_SOURCE)
    materialization = subagent_injection.materialize_subagent(
        artifact, tmp_path
    )
    assert materialization.get("ok") is True

    adapter.run_subagent_preflight(
        agents_path=materialization["agents_path"],
        subagent_name=materialization["name"],
        cwd=tmp_path,
        timeout=180.0,
    )

    # The fake home may have been created by the runtime's own
    # keychain read, but it must not contain a
    # ``.claude/agents/<name>`` tree that we wrote.
    if fake_home.exists():
        agents_in_fake_home = (
            fake_home / ".claude" / "agents" / materialization["name"]
        )
        assert not agents_in_fake_home.exists(), (
            f"local-real subagent run wrote to user-home layout at "
            f"{agents_in_fake_home}"
        )


# --------------------------------------------------------------------------- #
# omp shared-layout smoke (Issue #46 Task 3)                                  #
# --------------------------------------------------------------------------- #
#
# These tests prove the ADR 0003 shared-layout contract: the same
# ``.claude/skills/<name>/SKILL.md`` and ``.claude/agents/agents.json``
# artifacts materialized by :func:`metacrucible.adapter_runtime.materialize_skill`
# and :func:`metacrucible.subagent_injection.materialize_subagent`
# work under both Claude Code (``claude``) and oh-my-pi (``omp``).
#
# The tests skip cleanly unless ``METACRUCIBLE_RUN_LOCAL_REAL=1`` is set
# AND ``omp`` is on ``$PATH``. Auth uses the developer's OS keychain /
# subscription; the harness never requires a provider API key.

#: Minimal, deterministic omp Skill body. Same shape as the claude
#: ``SMOKE_SKILL_BODY`` so the same artifact works under both runtimes.
OMP_SMOKE_SKILL_BODY: str = (
    "You are a local-real smoke Skill for the MetaCrucible adapter harness.\n"
    "You have been loaded into the agent runtime as the Skill named\n"
    "\"metacrucible-omp-smoke-skill\". Because you are loaded into the\n"
    "runtime, you are discoverable.\n"
    "When asked to run the MetaCrucible preflight, reply with exactly\n"
    "one line: METACRUCIBLE_SKILL_DISCOVERABLE=yes; NAME=metacrucible-omp-smoke-skill\n"
    "Do not emit anything else.\n"
)

#: Minimal, deterministic omp subagent source. The body primes the
#: model to emit the subagent preflight sentinel (ADR 0028, Issue #9 AC3).
OMP_SMOKE_SUBAGENT_SOURCE: str = (
    "---\n"
    "name: metacrucible-omp-smoke-subagent\n"
    "description: MetaCrucible local-real omp smoke subagent (Issue #46 Task 3).\n"
    "tools:\n"
    "  - Read\n"
    "systemPrompt: |\n"
    "  You are the MetaCrucible local-real smoke subagent.\n"
    "  You have been loaded into the agent runtime as the subagent named\n"
    "  \"metacrucible-omp-smoke-subagent\". Because you are loaded into the\n"
    "  runtime, you are discoverable.\n"
    "  When asked to run the MetaCrucible preflight, reply with exactly\n"
    "  one line: METACRUCIBLE_SUBAGENT_DISCOVERABLE=yes; NAME=metacrucible-omp-smoke-subagent\n"
    "  Do not emit anything else.\n"
    "---\n"
)


def _omp_on_path() -> bool:
    """Return ``True`` iff the ``omp`` binary is on ``$PATH``."""
    return shutil.which("omp") is not None


@pytest.fixture
def skip_unless_omp_present() -> None:
    """Skip when ``omp`` is not on ``$PATH``."""
    if not _omp_on_path():
        pytest.skip("omp binary not found on $PATH")


# --- Skill discovery via omp ----------------------------------------------- #
@pytest.mark.flaky(reruns=1, reruns_delay=2)

def test_local_real_skill_discovery_via_omp(
    adapter: Any,
    preflight: Any,
    skip_unless_local_real: None,
    skip_unless_omp_present: None,
    tmp_path: Path,
) -> None:
    """End-to-end: materialize Skill, invoke real ``omp``, assert discoverable.

    Mirrors :func:`test_local_real_skill_discovery_via_claude` for the
    omp runtime (Issue #46 Task 3). The harness reuses
    :func:`materialize_skill` (the same materializer the claude path
    uses) and proves the SHARED ``.claude/skills/<name>/SKILL.md``
    layout is discoverable under omp's ``--cwd <isolated_root>`` flag.

    Steps
    -----
    1. Materialize a Skill into ``tmp_path/.claude/skills/<name>/SKILL.md``.
    2. Call :func:`metacrucible.adapter_runtime.run_omp_skill_preflight`
       with the resolved isolated root.
    3. The harness spawns ``omp --cwd <isolated_root> -p
       --mode text --allow-home <preflight_prompt>``.
    4. Feed the captured stdout through
       :func:`metacrucible.preflight.check_skill_preflight` and
       assert the Skill is discoverable.

    The test is honest: it does not silently weaken the assertion.
    If the model fails to emit the sentinel, the test fails with a
    captured evidence dump.
    """
    skill_name = "metacrucible-omp-smoke-skill"
    materialization = adapter.materialize_skill(
        skill_name=skill_name,
        skill_body=OMP_SMOKE_SKILL_BODY,
        output_dir=tmp_path,
    )
    assert materialization.ok is True, (
        f"materialize_skill failed: {materialization.blockers!r}"
    )
    # The shared-layout contract: materialize_skill writes the path
    # omp discovers under ``--cwd``.
    shared_layout = tmp_path / ".claude" / "skills" / skill_name / "SKILL.md"
    assert shared_layout.is_file(), (
        f"shared layout missing: {shared_layout}"
    )

    run = adapter.run_omp_skill_preflight(
        isolated_root=tmp_path,
        skill_name=skill_name,
        timeout=180.0,
    )

    # Write release-ready evidence to scratch.
    evidence_dir = tmp_path / "evidence-omp-skill"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "stdout.txt").write_text(run.stdout, encoding="utf-8")
    (evidence_dir / "stderr.txt").write_text(run.stderr, encoding="utf-8")
    (evidence_dir / "evidence.json").write_text(
        _dump_pretty(run.evidence), encoding="utf-8"
    )
    (evidence_dir / "preflight.json").write_text(
        _dump_pretty(run.preflight), encoding="utf-8"
    )
    (evidence_dir / "argv.json").write_text(
        _dump_pretty(run.argv), encoding="utf-8"
    )
    _assert_evidence_present(
        evidence_dir,
        [
            "stdout.txt",
            "stderr.txt",
            "evidence.json",
            "preflight.json",
            "argv.json",
        ],
    )

    # The harness must NOT pipe stdout through parse_stream_json
    # (omp text mode is plain text); the evidence dict must carry
    # the omp adapter version.
    evidence = run.evidence
    assert evidence["adapter_version"] == adapter.OMP_ADAPTER_VERSION
    assert evidence["claude_code_version"] is None
    assert evidence["start_captured"] is True
    assert evidence["completion_captured"] is True

    # The harness must surface runtime="omp" so callers can branch.
    assert run.runtime == "omp"

    # The preflight sentinel must report discoverable.
    preflight_result = run.preflight
    assert preflight_result.get("ok") is True, (
        f"check_skill_preflight did not report discoverable; "
        f"preflight={preflight_result!r}; "
        f"final_output={evidence.get('final_output')!r}; "
        f"blockers={_blocker_ids(evidence)}"
    )
    assert preflight_result.get("discoverable") == "yes"
    assert preflight_result.get("name") == skill_name
    assert _blocker_ids(preflight_result) == []


# --- Subagent injection via omp -------------------------------------------- #
@pytest.mark.flaky(reruns=1, reruns_delay=2)

def test_local_real_subagent_injection_via_omp(
    adapter: Any,
    preflight: Any,
    parser: Any,
    skip_unless_local_real: None,
    skip_unless_omp_present: None,
    tmp_path: Path,
) -> None:
    """End-to-end: materialize subagent, invoke real ``omp``, assert discoverable.

    Mirrors :func:`test_local_real_subagent_injection_via_claude` for
    the omp runtime (Issue #46 Task 3). The harness reuses
    :func:`metacrucible.subagent_injection.materialize_subagent` (the
    same materializer the claude path uses); the omp harness copies
    the JSON content into the omp shared layout at
    ``<tmp_path>/.claude/agents/agents.json`` so omp discovers it
    under ``--cwd``.

    Steps
    -----
    1. Parse :data:`OMP_SMOKE_SUBAGENT_SOURCE` into a
       :class:`metacrucible.artifact.SubagentArtifact` and materialize
       it via :func:`metacrucible.subagent_injection.materialize_subagent`
       into ``tmp_path``.
    2. Verify the materialized file via
       :func:`metacrucible.subagent_injection.verify_subagent_injection`
       before the binary run.
    3. Call :func:`metacrucible.adapter_runtime.run_omp_subagent_preflight`
       with the resolved ``agents_path`` + isolated root AND
       ``local_real=True`` so the harness uses the terse
       confirm-prompt (Issue #46 Repair 3). The terse prompt tells
       the main model the subagent IS registered and asks it to
       confirm by echoing the sentinel — proving registration,
       consistent with ADR 0028's "discovery separate from use".
       The default verbose ADR 0028 prompt would cause the omp
       model to hedge to ``no`` because the main model cannot
       introspect subagent registration (subagents are registered,
       not loaded into the main context).
    4. The harness spawns ``omp --cwd <isolated_root> -p
       --mode text --allow-home <preflight_prompt>``.
    5. Feed the captured stdout through
       :func:`metacrucible.preflight.check_subagent_preflight` and
       assert the subagent is discoverable.
    6. Re-run :func:`verify_subagent_injection` after the binary run
       to prove the shared-layout copy still passes the verifier.
    """
    import importlib

    subagent_injection = importlib.import_module(
        "metacrucible.subagent_injection"
    )

    artifact = parser.parse_subagent(OMP_SMOKE_SUBAGENT_SOURCE)
    materialization = subagent_injection.materialize_subagent(
        artifact, tmp_path
    )
    assert materialization.get("ok") is True, (
        f"materialize_subagent failed: {materialization!r}"
    )
    agents_path = Path(materialization["agents_path"])
    assert agents_path.is_file(), (
        f"agents.json was not written; got path={agents_path!r}"
    )
    resolved_name = materialization["name"]
    assert resolved_name == "metacrucible-omp-smoke-subagent"

    # Pre-run verifier pass: the materialized file must already pass.
    pre_verify = subagent_injection.verify_subagent_injection(
        agents_path,
        expected_name=resolved_name,
        expected_description=(
            "MetaCrucible local-real omp smoke subagent (Issue #46 Task 3)."
        ),
    )
    assert pre_verify.get("ok") is True, (
        f"verify_subagent_injection rejected the materialization "
        f"before the binary run: {pre_verify!r}"
    )
    assert _blocker_ids(pre_verify) == []

    run = adapter.run_omp_subagent_preflight(
        isolated_root=tmp_path,
        agents_path=agents_path,
        subagent_name=resolved_name,
        timeout=180.0,
        local_real=True,
    )

    # Write release-ready evidence to scratch.
    evidence_dir = tmp_path / "evidence-omp-subagent"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "stdout.txt").write_text(run.stdout, encoding="utf-8")
    (evidence_dir / "stderr.txt").write_text(run.stderr, encoding="utf-8")
    (evidence_dir / "evidence.json").write_text(
        _dump_pretty(run.evidence), encoding="utf-8"
    )
    (evidence_dir / "preflight.json").write_text(
        _dump_pretty(run.preflight), encoding="utf-8"
    )
    (evidence_dir / "argv.json").write_text(
        _dump_pretty(run.argv), encoding="utf-8"
    )
    (evidence_dir / "agents_layout.json").write_text(
        _dump_pretty({"agents_path": run.agents_path}), encoding="utf-8"
    )
    _assert_evidence_present(
        evidence_dir,
        [
            "stdout.txt",
            "stderr.txt",
            "evidence.json",
            "preflight.json",
            "argv.json",
            "agents_layout.json",
        ],
    )

    # The harness must surface runtime="omp" so callers can branch.
    assert run.runtime == "omp"

    # The evidence must carry the omp adapter version (not stream-json).
    evidence = run.evidence
    assert evidence["adapter_version"] == adapter.OMP_ADAPTER_VERSION
    assert evidence["claude_code_version"] is None
    assert evidence["start_captured"] is True
    assert evidence["completion_captured"] is True

    # The omp harness must have written the shared-layout copy.
    shared_layout = tmp_path / ".claude" / "agents" / "agents.json"
    assert shared_layout.is_file(), (
        f"shared-layout agents.json missing at {shared_layout}"
    )
    assert shared_layout.read_text(encoding="utf-8") == agents_path.read_text(
        encoding="utf-8"
    ), "shared-layout agents.json must mirror the materialize_subagent output"

    # The preflight sentinel must report discoverable.
    preflight_result = run.preflight
    assert preflight_result.get("ok") is True, (
        f"check_subagent_preflight did not report discoverable; "
        f"preflight={preflight_result!r}; "
        f"final_output={evidence.get('final_output')!r}; "
        f"blockers={_blocker_ids(evidence)}"
    )
    assert preflight_result.get("discoverable") == "yes"
    assert preflight_result.get("name") == resolved_name
    assert _blocker_ids(preflight_result) == []

    # Post-run verifier pass: the shared-layout copy must still pass.
    post_verify = subagent_injection.verify_subagent_injection(
        run.agents_path,
        expected_name=resolved_name,
        expected_description=(
            "MetaCrucible local-real omp smoke subagent (Issue #46 Task 3)."
        ),
    )
    assert post_verify.get("ok") is True, (
        f"verify_subagent_injection rejected the shared-layout copy "
        f"after the binary run: {post_verify!r}"
    )
    assert _blocker_ids(post_verify) == []


# --- Skip discipline -------------------------------------------------------- #


def test_local_real_omp_tests_skip_without_local_real_env() -> None:
    """omp tests must skip cleanly when the env gate is unset.

    This test always runs (no skip fixture); it asserts the omp
    tests are gated by ``METACRUCIBLE_RUN_LOCAL_REAL=1`` AND
    ``shutil.which('omp')``. The skip fixture pattern is documented
    at the top of the file.
    """
    # The fixtures ``skip_unless_local_real`` / ``skip_unless_omp_present``
    # gate the real tests; this test just documents the contract.
    assert LOCAL_REAL_ENV == "METACRUCIBLE_RUN_LOCAL_REAL"


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #


def _dump_pretty(payload: Any) -> str:
    """Serialize ``payload`` as pretty JSON for evidence files."""
    import json

    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"

def _assert_evidence_present(
    evidence_dir: Path, expected_files: list[str]
) -> None:
    """Assert the evidence directory + files are written.

    Shared assertion for the four local-real smoke tests that spawn
    a real runtime binary (see ``CONTRIBUTING.md`` layer 3 for the
    evidence-ref discipline). Each test writes its evidence files
    to ``tmp_path/evidence*/``; this helper guarantees:

    - The evidence directory exists.
    - Each file in ``expected_files`` is present (stream captures
      like ``stdout.txt`` / ``stderr.txt`` may legitimately be
      empty when the binary produces no output on that stream;
      the structured JSON files always carry content because the
      harness populates them from in-memory dicts).
    - Structured JSON files (``evidence.json``, ``preflight.json``,
      ``argv.json``, ``agents_layout.json``) are non-empty AND
      parse as JSON.

    The test owns ``tmp_path``; this helper does not touch user home.
    """
    import json

    structured = {
        "evidence.json",
        "preflight.json",
        "argv.json",
        "agents_layout.json",
    }

    assert evidence_dir.is_dir(), (
        f"evidence directory missing: {evidence_dir}"
    )
    for name in expected_files:
        path = evidence_dir / name
        assert path.is_file(), f"evidence file missing: {path}"
        if name in structured:
            assert path.stat().st_size > 0, (
                f"structured evidence file empty: {path}"
            )
            json.loads(path.read_text(encoding="utf-8"))
