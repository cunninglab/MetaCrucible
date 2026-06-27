"""Tests for Issue #45 Task 3: CI workflow pinning.

These tests read ``.github/workflows/ci.yml`` as text and assert the
contract the issue requires:

* The workflow installs Python and runs the suite through Mise via
  ``jdx/mise-action`` (not bare ``python`` / ``pip`` / ``pytest``).
* The workflow exposes the canonical Mise tasks: ``mise install``,
  ``mise run install``, ``mise run test``, and ``mise run test-replay``.
* The workflow contains no provider-secret references
  (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``) and no live-LLM markers
  (``live-llm``, ``provider-secret``) so public forks do not require
  configured secrets.
* The replay test command covers review, bootstrap, optimize, and
  synthesize so CI proves the full recorded-replay path runs.

The tests stay string-only — no YAML library import — to keep the
existing test runtime dependency set (``pytest`` only) unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


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
# File presence and Mise wiring                                                #
# --------------------------------------------------------------------------- #


def test_workflow_exists_at_repo_root() -> None:
    """The CI workflow must live at the canonical GitHub Actions path."""
    assert WORKFLOW_PATH.is_file(), (
        f"expected {WORKFLOW_PATH.relative_to(REPO_ROOT)} to exist"
    )


def test_workflow_uses_mise_action() -> None:
    """The workflow must install Mise via ``jdx/mise-action``."""
    text = _read_workflow()
    assert "jdx/mise-action" in text, (
        "workflow must use jdx/mise-action so Mise is the canonical "
        "toolchain (ADR 0021)"
    )


def test_workflow_pins_mise_action_major() -> None:
    """The Mise action must be pinned to a major version like ``@v4``."""
    text = _read_workflow()
    assert re.search(r"jdx/mise-action@v\d+", text), (
        "jdx/mise-action must be pinned to a concrete major version "
        "(e.g. jdx/mise-action@v4)"
    )


def test_workflow_pins_mise_version() -> None:
    """The Mise action must be pinned to a concrete Mise version, not ``latest``."""
    text = _read_workflow()
    match = re.search(r"^\s*version:\s*\"?([^\n#\"]+)\"?\s*$", text, re.MULTILINE)
    assert match, "workflow must set version: <pinned version> under the mise-action step"
    version = match.group(1).strip().strip('"').strip("'")
    assert version, "version value must not be empty"
    assert version.lower() != "latest", (
        f"version must be pinned, got {version!r}"
    )


# --------------------------------------------------------------------------- #
# Canonical Mise tasks in CI                                                   #
# --------------------------------------------------------------------------- #


def test_workflow_runs_mise_install() -> None:
    """The workflow must run ``mise install`` to provision Python and the venv."""
    text = _read_workflow()
    assert "mise install" in text, "workflow must run `mise install`"


def test_workflow_runs_install_task() -> None:
    """The workflow must run ``mise run install`` for the editable install."""
    text = _read_workflow()
    assert "mise run install" in text, (
        "workflow must run `mise run install` to install the package"
    )


def test_workflow_runs_test_task() -> None:
    """The workflow must run ``mise run test`` to exercise the suite."""
    text = _read_workflow()
    assert "mise run test" in text, "workflow must run `mise run test`"


def test_workflow_runs_test_replay_task() -> None:
    """The workflow must run ``mise run test-replay`` for the CI replay subset."""
    text = _read_workflow()
    assert "mise run test-replay" in text, (
        "workflow must run `mise run test-replay` so CI exercises the "
        "recorded-replay harness (issue #45, ADR 0021)"
    )


def test_workflow_does_not_call_bare_pytest_or_pip() -> None:
    """The workflow must route commands through Mise rather than bare tools.

    A bare ``pip install`` or ``pytest`` step would bypass the canonical
    toolchain declared in ``mise.toml`` and would let CI drift from the
    developer's environment.
    """
    run_text = "\n".join(_run_lines())
    for forbidden in ("pip install", "python -m pytest"):
        assert forbidden not in run_text, (
            f"workflow must not run {forbidden!r} directly; route through Mise"
        )


# --------------------------------------------------------------------------- #
# Public-fork safety: no live-LLM markers                                      #
# --------------------------------------------------------------------------- #


def test_workflow_has_no_anthropic_api_key() -> None:
    """The workflow must not reference ``ANTHROPIC_API_KEY``."""
    text = _read_workflow()
    assert "ANTHROPIC_API_KEY" not in text, (
        "workflow must not reference ANTHROPIC_API_KEY; CI is recorded-"
        "replay only (issue #45, ADR 0021, ADR 0036)"
    )


def test_workflow_has_no_openai_api_key() -> None:
    """The workflow must not reference ``OPENAI_API_KEY``."""
    text = _read_workflow()
    assert "OPENAI_API_KEY" not in text, (
        "workflow must not reference OPENAI_API_KEY; CI is recorded-"
        "replay only (issue #45, ADR 0021, ADR 0036)"
    )


def test_workflow_has_no_live_llm_marker() -> None:
    """The workflow must not contain a live-LLM or provider-secret marker."""
    text = _read_workflow()
    for marker in ("live-llm", "provider-secret"):
        assert marker not in text, (
            f"workflow must not contain {marker!r}; CI is recorded-replay only"
        )


def test_workflow_has_no_secrets_block() -> None:
    """The workflow must not declare a top-level ``secrets:`` block.

    ``secrets:`` is how GitHub Actions explicitly references repository
    secrets; its absence is a strong signal that no provider credentials
    are wired in.
    """
    text = _read_workflow()
    assert not re.search(r"^\s*secrets:\s*$", text, re.MULTILINE), (
        "workflow must not declare a secrets: block (public-fork safe)"
    )


# --------------------------------------------------------------------------- #
# Triggers and runner                                                          #
# --------------------------------------------------------------------------- #


def test_workflow_triggers_on_push_pr_and_manual() -> None:
    """The workflow must run on push, pull_request, and manual dispatch."""
    text = _read_workflow()
    assert re.search(r"^\s*push:\s*$", text, re.MULTILINE), (
        "workflow must declare `on: push`"
    )
    assert re.search(r"^\s*pull_request:\s*$", text, re.MULTILINE), (
        "workflow must declare `on: pull_request`"
    )
    assert "workflow_dispatch" in text, (
        "workflow must declare `on: workflow_dispatch`"
    )


def test_workflow_runs_on_ubuntu_latest() -> None:
    """The workflow must run on ``ubuntu-latest`` (Linux x86_64)."""
    text = _read_workflow()
    assert "ubuntu-latest" in text, (
        "workflow must run on ubuntu-latest (Linux x86_64 only, no matrix)"
    )


def test_workflow_has_no_matrix_strategy() -> None:
    """The workflow must not use a ``matrix:`` strategy (single Linux job)."""
    text = _read_workflow()
    assert not re.search(r"^\s*matrix:\s*$", text, re.MULTILINE), (
        "workflow must not declare a matrix: strategy (single Linux x86_64 job)"
    )


# --------------------------------------------------------------------------- #
# Replay coverage markers                                                      #
# --------------------------------------------------------------------------- #


def test_workflow_replay_test_covers_all_four_commands() -> None:
    """The replay test step must mention review, bootstrap, optimize, synthesize.

    The acceptance criterion is that CI "covers review/bootstrap/optimize/
    synthesize". The four command names must appear in the workflow's
    ``run:`` lines (as a comment in the multi-line block) so a future
    refactor that drops the replay test does not silently lose coverage.
    """
    run_text = "\n".join(_run_lines())
    for command in ("review", "bootstrap", "optimize", "synthesize"):
        assert command in run_text, (
            f"workflow's replay test step must mention {command!r} in a "
            f"run: block so CI proves that command is covered"
        )
