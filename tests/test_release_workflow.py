"""Tests for Issue #48 Task 3: OIDC Trusted-Publishing release workflow.

These tests read ``.github/workflows/release.yml`` as text and assert
the contract the issue requires:

* The workflow triggers on ``push: tags: ['v*']`` and on
  ``workflow_dispatch`` only — it MUST NOT trigger on push to
  ``main`` or on ``pull_request``.
* The workflow uses ``pypa/gh-action-pypi-publish`` for the publish
  step and declares ``permissions: id-token: write`` (OIDC Trusted
  Publishing).
* The workflow contains NO ``password:`` field, NO ``PYPI_API_TOKEN``,
  NO ``ANTHROPIC_API_KEY``, NO ``OPENAI_API_KEY``, no ``secrets.``
  reference, and no live-LLM markers. OIDC is the only credential
  mechanism.
* The workflow runs ``mise run test`` (full suite, not just the
  replay subset), ``mise run install``, ``mise run build``, and
  ``mise run release-gate`` before publishing.
* The publish job is gated so it only publishes after the test,
  build, and gate steps succeed.

The tests stay string-only — no YAML library import — to keep the
existing test runtime dependency set (``pytest`` only) unchanged,
mirroring the helpers in ``tests/test_ci_workflow.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _read_workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _run_lines() -> list[str]:
    """Return the textual content of every ``run:`` block in the workflow.

    Multi-line ``run: |`` blocks are returned as the joined body so
    substrings inside the block remain visible to the assertions.
    """
    text = _read_workflow()
    run_lines: list[str] = []
    for match in re.finditer(
        r"^\s*run:\s*\|?\s*\n(?P<body>(?:[ \t]+.*\n)+)",
        text,
        re.MULTILINE,
    ):
        run_lines.append(match.group("body"))
    for match in re.finditer(
        r"^\s*run:\s*(?P<inline>[^\n]+)\s*$",
        text,
        re.MULTILINE,
    ):
        run_lines.append(match.group("inline"))
    return run_lines


# --------------------------------------------------------------------------- #
# File presence                                                                #
# --------------------------------------------------------------------------- #


def test_release_workflow_exists_at_repo_root() -> None:
    """The release workflow must live at the canonical GitHub Actions path."""
    assert WORKFLOW_PATH.is_file(), (
        f"release workflow missing at {WORKFLOW_PATH}"
    )


# --------------------------------------------------------------------------- #
# Triggers                                                                     #
# --------------------------------------------------------------------------- #


def test_workflow_triggers_on_tag_push() -> None:
    """The workflow must run on push of any ``v*`` tag."""
    text = _read_workflow()
    # Accept either flow-style (`v*`) or sequence-style (`['v*']`).
    assert re.search(
        r"^\s*push:\s*$",
        text,
        re.MULTILINE,
    ), "workflow must declare a `push:` trigger"
    assert re.search(
        r"^\s*tags:\s*$",
        text,
        re.MULTILINE,
    ), "workflow push trigger must declare `tags:`"
    assert re.search(
        r"""['"]?v\*['"]?""",
        text,
    ), "workflow push trigger must match `v*` tag pattern"


def test_workflow_triggers_on_workflow_dispatch() -> None:
    """The workflow must run on manual ``workflow_dispatch``."""
    text = _read_workflow()
    assert re.search(
        r"^\s*workflow_dispatch:\s*$",
        text,
        re.MULTILINE,
    ), "workflow must declare a `workflow_dispatch:` trigger"


def test_workflow_does_not_trigger_on_push_to_main() -> None:
    """The workflow MUST NOT trigger on push to the ``main`` branch."""
    text = _read_workflow()
    # The trigger block must not pin `branches: [main]` (or its
    # sequence / list-of-strings variants) for push.
    assert not re.search(
        r"^\s*branches:\s*\[?\s*['\"]?main['\"]?\s*\]?\s*$",
        text,
        re.MULTILINE,
    ), "release workflow must not gate on push to main"


def test_workflow_does_not_trigger_on_pull_request() -> None:
    """The workflow MUST NOT declare a ``pull_request`` trigger."""
    text = _read_workflow()
    assert not re.search(
        r"^\s*pull_request:\s*$",
        text,
        re.MULTILINE,
    ), "release workflow must not declare a pull_request trigger"


# --------------------------------------------------------------------------- #
# Permissions and OIDC Trusted Publishing                                      #
# --------------------------------------------------------------------------- #


def test_workflow_declares_oidc_permissions() -> None:
    """The workflow must declare ``permissions: id-token: write`` for OIDC."""
    text = _read_workflow()
    assert "id-token: write" in text, (
        "workflow must grant `id-token: write` for OIDC Trusted Publishing"
    )


def test_workflow_uses_pypi_publish_action() -> None:
    """The workflow must use ``pypa/gh-action-pypi-publish`` to publish."""
    text = _read_workflow()
    assert "pypa/gh-action-pypi-publish" in text, (
        "workflow must reference `pypa/gh-action-pypi-publish`"
    )


# --------------------------------------------------------------------------- #
# Mise action pin + canonical Mise tasks                                       #
# --------------------------------------------------------------------------- #


def test_workflow_uses_mise_action() -> None:
    """The workflow must install Mise via ``jdx/mise-action``."""
    text = _read_workflow()
    assert "jdx/mise-action" in text, (
        "release workflow must use `jdx/mise-action`"
    )


def test_workflow_pins_mise_action_major() -> None:
    """The Mise action must be pinned to a major version like ``@v4``."""
    text = _read_workflow()
    assert re.search(
        r"jdx/mise-action@v\d+",
        text,
    ), "Mise action must be pinned to a major version (e.g. `@v4`)"


def test_workflow_pins_mise_version() -> None:
    """The Mise action must be pinned to a concrete Mise version, not ``latest``."""
    text = _read_workflow()
    assert re.search(
        r"^\s*version:\s*\"?\S+\"?\s*$",
        text,
        re.MULTILINE,
    ), "release workflow must pin a concrete `version`"
    assert "latest" not in re.search(
        r"^\s*version:\s*\"?\S+\"?\s*$",
        text,
        re.MULTILINE,
    ).group(0), "version must not be `latest`"


def test_workflow_runs_mise_install() -> None:
    """The workflow must run ``mise install`` to provision Python and the venv."""
    text = _read_workflow()
    assert "mise install" in text, "workflow must run `mise install`"


def test_workflow_runs_install_task() -> None:
    """The workflow must run ``mise run install`` for the editable install."""
    text = _read_workflow()
    assert "mise run install" in text, "workflow must run `mise run install`"


def test_workflow_runs_test_task() -> None:
    """The workflow must run ``mise run test`` (full suite) before publishing."""
    text = _read_workflow()
    assert "mise run test" in text, "workflow must run `mise run test`"


def test_workflow_runs_build_task() -> None:
    """The workflow must run ``mise run build`` to produce wheel + sdist."""
    text = _read_workflow()
    assert "mise run build" in text, "workflow must run `mise run build`"


def test_workflow_runs_release_gate_task() -> None:
    """The workflow must run ``mise run release-gate`` before publishing."""
    text = _read_workflow()
    assert "mise run release-gate" in text, (
        "workflow must run `mise run release-gate`"
    )


# --------------------------------------------------------------------------- #
# Job ordering: publish only after test/build/gate succeed                     #
# --------------------------------------------------------------------------- #


def test_workflow_declares_a_single_release_job() -> None:
    """The workflow must define a single ``release`` job (no matrix)."""
    text = _read_workflow()
    assert re.search(
        r"^\s*release:\s*$",
        text,
        re.MULTILINE,
    ), "workflow must declare a `release:` job"
    assert not re.search(
        r"^\s*matrix:\s*$",
        text,
        re.MULTILINE,
    ), "release workflow must not use a `matrix:` strategy"


def _step_run_position(text: str, command: str) -> int:
    """Return the byte offset of the ``run: <command>`` step line.

    Matches indented ``run:`` entries that invoke the given Mise
    command as a bare string (i.e. the actual step body, not a
    name/comment that happens to mention the command).
    """
    match = re.search(
        rf"^\s*run:\s*{re.escape(command)}\s*$",
        text,
        re.MULTILINE,
    )
    assert match is not None, (
        f"workflow missing `run: {command}` step line"
    )
    return match.start()


def _step_uses_position(text: str, action: str) -> int:
    """Return the byte offset of the ``uses: <action>`` step line."""
    match = re.search(
        rf"^\s*uses:\s*{re.escape(action)}\S*\s*$",
        text,
        re.MULTILINE,
    )
    assert match is not None, (
        f"workflow missing `uses: {action}` step line"
    )
    return match.start()


def test_publish_step_runs_after_test_build_and_gate() -> None:
    """The PyPI publish step must appear after the test, build, and gate steps."""
    text = _read_workflow()
    publish_at = _step_uses_position(text, "pypa/gh-action-pypi-publish")
    for upstream in ("mise run test", "mise run build", "mise run release-gate"):
        upstream_at = _step_run_position(text, upstream)
        assert upstream_at < publish_at, (
            f"`run: {upstream}` must precede "
            "`uses: pypa/gh-action-pypi-publish`"
        )


def test_workflow_runs_on_ubuntu_latest() -> None:
    """The workflow must run on ``ubuntu-latest``."""
    text = _read_workflow()
    assert "ubuntu-latest" in text, (
        "release workflow must run on `ubuntu-latest`"
    )


# --------------------------------------------------------------------------- #
# Public-fork safety: no secret / live-LLM markers                             #
# --------------------------------------------------------------------------- #


def test_workflow_has_no_password_field() -> None:
    """The workflow must NOT contain a ``password:`` field."""
    text = _read_workflow()
    assert "password:" not in text, (
        "release workflow must not declare a `password:` field "
        "(OIDC is the only credential mechanism)"
    )


def test_workflow_has_no_pypi_api_token() -> None:
    """The workflow must NOT reference ``PYPI_API_TOKEN``."""
    text = _read_workflow()
    assert "PYPI_API_TOKEN" not in text, (
        "release workflow must not reference `PYPI_API_TOKEN`"
    )


def test_workflow_has_no_anthropic_api_key() -> None:
    """The workflow must NOT reference ``ANTHROPIC_API_KEY``."""
    text = _read_workflow()
    assert "ANTHROPIC_API_KEY" not in text, (
        "release workflow must not reference `ANTHROPIC_API_KEY`"
    )


def test_workflow_has_no_openai_api_key() -> None:
    """The workflow must NOT reference ``OPENAI_API_KEY``."""
    text = _read_workflow()
    assert "OPENAI_API_KEY" not in text, (
        "release workflow must not reference `OPENAI_API_KEY`"
    )


def test_workflow_has_no_secrets_reference() -> None:
    """The workflow must NOT reference any ``secrets.*`` context."""
    text = _read_workflow()
    assert "secrets." not in text, (
        "release workflow must not reference `secrets.*` "
        "(OIDC is the only credential mechanism)"
    )


def test_workflow_has_no_live_llm_marker() -> None:
    """The workflow must NOT contain a live-LLM or provider-secret marker."""
    text = _read_workflow()
    for marker in ("live-llm", "provider-secret", "live_llm"):
        assert marker not in text, (
            f"release workflow must not contain live-LLM marker `{marker}`"
        )