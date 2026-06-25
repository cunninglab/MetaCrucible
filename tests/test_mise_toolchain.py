"""Tests for Issue #1: project toolchain declared with Mise.

These tests verify the public repository behavior asserted by Issue #1:
  - `mise.toml` exists at the repo root so `mise install` is discoverable.
  - The config pins a concrete Python version under `[tools]`.
  - The config defines a `test` task so `mise run test` works.
  - No competing environment-manager file is introduced at the repo root.
    A competing file may only be added together with a rationale note inside
    `mise.toml`; this test guards the default state.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MISE_TOML = REPO_ROOT / "mise.toml"

# Files that would constitute a competing environment manager.
# `mise.toml` is the canonical toolchain declaration for this project; if
# any of these appear at the repo root, a rationale comment must be added
# in `mise.toml` explaining the coexistence and this list updated.
COMPETING_ENV_FILES: tuple[str, ...] = (
    ".python-version",   # pyenv
    "Pipfile",           # pipenv
    "Pipfile.lock",
    "poetry.lock",       # poetry
    "environment.yml",   # conda
    "conda-lock.yml",
    ".tool-versions",    # asdf
    "runtime.txt",       # heroku-style python pin
)


def _read_mise_toml() -> str:
    return MISE_TOML.read_text(encoding="utf-8")


def test_mise_toml_exists_at_repo_root() -> None:
    """`mise.toml` must live at the repo root for `mise install` to find it."""
    assert MISE_TOML.is_file(), f"expected {MISE_TOML.relative_to(REPO_ROOT)} to exist"


def test_mise_toml_declares_tools_section() -> None:
    """`mise.toml` must declare a `[tools]` section."""
    text = _read_mise_toml()
    assert re.search(r"^\[tools\]\s*$", text, re.MULTILINE), (
        "mise.toml must declare a [tools] section"
    )


def test_mise_toml_pins_python_version() -> None:
    """The Python tool pin must be a concrete `major.minor[.patch]` version.

    Reject floating values like `"latest"` or `"system"` — they break the
    reproducibility promise of `mise install`.
    """
    text = _read_mise_toml()
    match = re.search(r'^\s*python\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
    assert match, "mise.toml must pin a Python version under [tools]"
    version = match.group(1)
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", version), (
        f"Python pin must look like '3.12' or '3.12.4'; got {version!r}"
    )


def test_mise_toml_defines_test_task() -> None:
    """A `test` task must be defined so `mise run test` exercises the suite."""
    text = _read_mise_toml()
    inline_table = re.search(r'^\[tasks\.test\]\s*$', text, re.MULTILINE)
    array_entry = re.search(
        r'\[\[tasks\]\][\s\S]*?^name\s*=\s*"test"',
        text,
        re.MULTILINE,
    )
    assert inline_table or array_entry, (
        "mise.toml must define a [tasks.test] section (or [[tasks]] with "
        'name = "test")'
    )


def test_test_task_references_pytest() -> None:
    """The `test` task must actually invoke pytest so it runs the suite."""
    text = _read_mise_toml()
    assert "pytest" in text, (
        "the test task must invoke pytest so `mise run test` runs the suite"
    )


@pytest.mark.parametrize("filename", COMPETING_ENV_FILES)
def test_no_competing_env_manager_file(filename: str) -> None:
    """No competing environment-manager file is present at the repo root.

    If a future change needs one, it must be justified with a rationale
    comment in `mise.toml` and this list updated accordingly.
    """
    path = REPO_ROOT / filename
    assert not path.exists(), (
        f"competing env manager file {filename!r} found at repo root — "
        "remove it or document a rationale in mise.toml"
    )


def test_mise_toml_is_valid_toml() -> None:
    """`mise.toml` must parse as valid TOML so `mise install` can read it.

    Minimal structural guard so the config is never silently malformed.
    """
    with MISE_TOML.open("rb") as fh:
        tomllib.load(fh)



# Test files that `mise run test-replay` must keep together. The task
# exists to exercise the recorded-replay CI harness (issue #45) for
# `review`, `bootstrap`, `optimize`, and `synthesize`; any one of these
# four files becoming orphaned from the replay subset should fail CI.
REPLAY_TEST_FILES: tuple[str, ...] = (
    "tests/test_replay_harness.py",
    "tests/test_replay_cli.py",
    "tests/test_ci_workflow.py",
    "tests/test_mise_toolchain.py",
)


def _load_mise_toml() -> dict:
    with MISE_TOML.open("rb") as fh:
        return tomllib.load(fh)


def test_mise_toml_exposes_test_replay_task() -> None:
    """`mise.toml` must declare a `[tasks.test-replay]` entry with a non-empty `run`.

    Guards issue #45: removing the `test-replay` task or emptying its `run`
    string would silently disable the recorded-replay CI harness.
    """
    data = _load_mise_toml()
    tasks = data.get("tasks")
    assert isinstance(tasks, dict), "mise.toml must declare a [tasks] table"
    test_replay = tasks.get("test-replay")
    assert isinstance(test_replay, dict), (
        "mise.toml must define [tasks.test-replay] (issue #45 CI harness)"
    )
    run = test_replay.get("run")
    assert isinstance(run, str) and run.strip(), (
        "[tasks.test-replay].run must be a non-empty string so "
        "`mise run test-replay` actually executes pytest"
    )


def test_mise_toml_test_replay_references_replay_test_files() -> None:
    """The `test-replay` task must reference the four replay-related test files.

    Pins the recorded-replay subset so a refactor that renames or drops
    one of the four files fails CI rather than silently narrowing the
    replay coverage of `review` / `bootstrap` / `optimize` / `synthesize`.
    """
    data = _load_mise_toml()
    tasks = data.get("tasks")
    assert isinstance(tasks, dict), "mise.toml must declare a [tasks] table"
    test_replay = tasks.get("test-replay")
    assert isinstance(test_replay, dict), (
        "mise.toml must define [tasks.test-replay] (issue #45 CI harness)"
    )
    run = test_replay.get("run", "")
    assert isinstance(run, str), "[tasks.test-replay].run must be a string"
    missing = [path for path in REPLAY_TEST_FILES if path not in run]
    assert not missing, (
        "[tasks.test-replay].run must reference every replay test file so "
        "CI exercises the full replay subset; missing: "
        + ", ".join(missing)
    )
