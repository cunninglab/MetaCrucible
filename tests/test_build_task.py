"""Tests for Issue #48 Task 1: Mise `build` task + wheel/sdist artifact contract.

These tests pin the public behavior asserted by issue #48's PyPI release
tooling plan, Task 1:

  - `mise.toml` declares a `[tasks.build]` entry whose `run` invokes
    `uv build --wheel --sdist` (BOTH targets, never `--wheel` alone).
  - Running that build end-to-end produces both a
    `metacrucible-<version>-py3-none-any.whl` wheel and a
    `metacrucible-<version>.tar.gz` sdist.
  - The wheel's namelist excludes dev-only paths (`.sdd/`, `tests/`,
    `.venv/`, `fixtures/`) and never bundles any secret-bearing entry
    (no name containing `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
    `.env`). The exclusion is verified by reading the actual wheel
    contents via `zipfile.ZipFile(...).namelist()`, not by parsing
    the build log.

The build is executed against a `tmp_path` output directory so no
artifacts are left in the workspace tree. The end-to-end wheel
build dry-run already covered by
`tests/test_packaging_skeleton.py::test_wheel_build_succeeds` is
intentionally NOT duplicated here; this file extends coverage to the
joint wheel+sdist contract and the wheel exclusion surface.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MISE_TOML = REPO_ROOT / "mise.toml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
EXPECTED_VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _load_mise_toml() -> dict:
    with MISE_TOML.open("rb") as fh:
        return tomllib.load(fh)


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _project_version() -> str:
    version = _load_pyproject().get("project", {}).get("version")
    assert isinstance(version, str) and version, (
        "pyproject.toml [project].version must be a non-empty string"
    )
    return version


def _run_build(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run `uv build --wheel --sdist` against REPO_ROOT into ``tmp_path``.

    Mirrors the subprocess invocation shape used by
    ``tests/test_packaging_skeleton.py::test_wheel_build_succeeds``
    so the build is invoked identically whether it is triggered by
    `mise run build` or by the test directly.
    """
    return subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--sdist",
            "--out-dir",
            str(tmp_path),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
    )


# --------------------------------------------------------------------------- #
# mise.toml — [tasks.build] declaration                                        #
# --------------------------------------------------------------------------- #


def test_mise_toml_declares_build_task() -> None:
    """`mise.toml` must declare a `[tasks.build]` table with a non-empty `run`.

    A missing or empty `run` would silently make `mise run build` a
    no-op, defeating the release toolchain.
    """
    data = _load_mise_toml()
    tasks = data.get("tasks")
    assert isinstance(tasks, dict), "mise.toml must declare a [tasks] table"
    build = tasks.get("build")
    assert isinstance(build, dict), (
        "mise.toml must define [tasks.build] (issue #48 Task 1)"
    )
    run = build.get("run")
    assert isinstance(run, str) and run.strip(), (
        "[tasks.build].run must be a non-empty string so "
        "`mise run build` actually executes the build"
    )


def test_mise_toml_build_task_has_description() -> None:
    """`[tasks.build]` must carry a `description` field, matching the other tasks.

    The `install` / `test` / `test-replay` / `test-local-real` tasks
    all use a `description = "..."` field; the new `build` task must
    follow the same shape so `mise tasks` output stays consistent.
    """
    data = _load_mise_toml()
    build = data.get("tasks", {}).get("build")
    assert isinstance(build, dict), "mise.toml must define [tasks.build]"
    description = build.get("description")
    assert isinstance(description, str) and description.strip(), (
        "[tasks.build].description must be a non-empty string "
        "matching the other [tasks.*] entries"
    )


def test_mise_toml_build_run_invokes_both_wheel_and_sdist() -> None:
    """`[tasks.build].run` must invoke `uv build --wheel --sdist` (BOTH targets).

    The release plan (issue #48) calls for a single Mise task that
    produces both artifacts. `--wheel` alone would skip the sdist and
    break the release gate's filename contract; `--sdist` alone would
    skip the wheel. The check is string-based on the task's `run` to
    catch silent narrowing of the build invocation.
    """
    data = _load_mise_toml()
    build = data.get("tasks", {}).get("build")
    assert isinstance(build, dict), "mise.toml must define [tasks.build]"
    run = build.get("run", "")
    assert isinstance(run, str), "[tasks.build].run must be a string"

    # Tokenize the run string on whitespace; both flags must appear as
    # whole tokens so a flag like `--no-wheel` does not satisfy `--wheel`.
    tokens = set(run.split())
    assert "uv" in tokens, (
        f"[tasks.build].run must start with `uv`; got {run!r}"
    )
    assert "--wheel" in tokens, (
        f"[tasks.build].run must include `--wheel`; got {run!r}"
    )
    assert "--sdist" in tokens, (
        f"[tasks.build].run must include `--sdist`; got {run!r}"
    )


# --------------------------------------------------------------------------- #
# Build end-to-end — wheel + sdist artifacts                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv is not on PATH; the build task needs uv as the build orchestrator",
)
def test_build_produces_wheel_and_sdist(tmp_path: Path) -> None:
    """`uv build --wheel --sdist` must produce both wheel and sdist artifacts.

    This is the joint artifact contract called out by issue #48 Task 1:
    the build invocation must yield both the wheel and the sdist for
    the configured project version, so the release workflow (Task 3)
    can upload either artifact to PyPI without a second build step.
    """
    version = _project_version()
    expected_wheel = f"metacrucible-{version}-py3-none-any.whl"
    expected_sdist = f"metacrucible-{version}.tar.gz"

    result = _run_build(tmp_path)
    assert result.returncode == 0, (
        f"`uv build --wheel --sdist` failed (rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    artifacts = sorted(p.name for p in tmp_path.iterdir())
    assert expected_wheel in artifacts, (
        f"expected wheel {expected_wheel!r} in {tmp_path}; "
        f"got artifacts: {artifacts}"
    )
    assert expected_sdist in artifacts, (
        f"expected sdist {expected_sdist!r} in {tmp_path}; "
        f"got artifacts: {artifacts}"
    )


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv is not on PATH; the build task needs uv as the build orchestrator",
)
def test_sdist_filename_encodes_project_version(tmp_path: Path) -> None:
    """The sdist filename must encode the project version from `pyproject.toml`.

    The release gate (Task 2) reads `pyproject.toml` for the version;
    the sdist's filename is the first observable confirmation that the
    build picked up the same version. The exact filename is
    `metacrucible-<version>.tar.gz`; we assert both the prefix and the
    encoded version so a hard-coded `metacrucible-0.1.0.tar.gz` literal
    cannot silently fall out of sync with the project version.
    """
    version = _project_version()
    result = _run_build(tmp_path)
    assert result.returncode == 0, (
        f"`uv build --wheel --sdist` failed (rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    sdists = sorted(tmp_path.glob("metacrucible-*.tar.gz"))
    assert sdists, (
        f"expected a metacrucible-*.tar.gz sdist in {tmp_path}; "
        f"got artifacts: {sorted(p.name for p in tmp_path.iterdir())}"
    )
    expected = f"metacrucible-{version}.tar.gz"
    assert any(s.name == expected for s in sdists), (
        f"expected sdist named {expected!r} (encoding version={version!r}); "
        f"got {[s.name for s in sdists]}"
    )

    # The sdist's internal top-level directory MUST also carry the
    # versioned name so downstream extract steps are stable. The
    # default hatchling sdist name is `metacrucible-<version>`.
    sdist_path = tmp_path / expected
    with tarfile.open(sdist_path, "r:gz") as tf:
        names = tf.getnames()
    prefix = f"metacrucible-{version}/"
    assert any(name.startswith(prefix) for name in names), (
        f"expected at least one sdist member under {prefix!r}; "
        f"got first few: {names[:5]}"
    )


# --------------------------------------------------------------------------- #
# Wheel exclusion — dev-only paths and secret-bearing entries                  #
# --------------------------------------------------------------------------- #


# Path segments that must NEVER appear inside the published wheel.
# `packages = ["src/metacrucible"]` already restricts the wheel to
# the package directory, so this list pins the *negative* surface:
# nothing under these prefixes is allowed to leak into the wheel.
EXCLUDED_PATH_PREFIXES: tuple[str, ...] = (
    ".sdd/",
    "tests/",
    ".venv/",
    "fixtures/",
)

# Substrings that must NEVER appear anywhere in the wheel namelist.
# These guard against accidental bundling of secret-bearing files
# such as `.env`, `ANTHROPIC_API_KEY.local`, or `OPENAI_API_KEY.txt`.
EXCLUDED_NAME_SUBSTRINGS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    ".env",
)


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv is not on PATH; the build task needs uv as the build orchestrator",
)
def test_wheel_excludes_dev_only_paths(tmp_path: Path) -> None:
    """The wheel namelist must contain no dev-only paths.

    Reads the actual wheel contents via `zipfile.ZipFile(...).namelist()`
    and asserts that no member path starts with `.sdd/`, `tests/`,
    `.venv/`, or `fixtures/`. Build-log parsing is intentionally NOT
    used because hatchling quietly skips paths outside the declared
    `packages = ["src/metacrucible"]`; only the wheel's true contents
    prove the exclusion.
    """
    version = _project_version()
    result = _run_build(tmp_path)
    assert result.returncode == 0, (
        f"`uv build --wheel --sdist` failed (rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    wheel = tmp_path / f"metacrucible-{version}-py3-none-any.whl"
    assert wheel.is_file(), f"expected wheel at {wheel}"

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    leaked = [
        n for n in names
        if any(n.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES)
    ]
    assert not leaked, (
        "wheel must not bundle any dev-only path; leaked members: "
        + ", ".join(sorted(leaked))
    )


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv is not on PATH; the build task needs uv as the build orchestrator",
)
def test_wheel_excludes_secret_bearing_entries(tmp_path: Path) -> None:
    """The wheel namelist must contain no secret-bearing entry names.

    The release plan calls the build "secret-free": no provider API
    key (or a `.env` file) may end up inside the published wheel. The
    check is substring-based against the actual wheel namelist so
    the test fails if any future change copies `.env`,
    `ANTHROPIC_API_KEY.local`, `OPENAI_API_KEY.txt`, or similar into
    `src/metacrucible/` (which IS part of the wheel via
    `packages = ["src/metacrucible"]`).
    """
    version = _project_version()
    result = _run_build(tmp_path)
    assert result.returncode == 0, (
        f"`uv build --wheel --sdist` failed (rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    wheel = tmp_path / f"metacrucible-{version}-py3-none-any.whl"
    assert wheel.is_file(), f"expected wheel at {wheel}"

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    leaked = [
        n for n in names
        if any(sub in n for sub in EXCLUDED_NAME_SUBSTRINGS)
    ]
    assert not leaked, (
        "wheel must not bundle any entry whose name contains a "
        "secret-bearing substring; leaked members: "
        + ", ".join(sorted(leaked))
    )


# --------------------------------------------------------------------------- #
# Sanity: `mise run build` command text does not introduce a competing env    #
# manager. The full no-competing-env-manager contract is pinned by             #
# tests/test_mise_toolchain.py::test_no_competing_env_manager_file; this      #
# test only guards the *string* of the build task for accidental               #
# shell-spawning of a competing tool.                                          #
# --------------------------------------------------------------------------- #


def test_build_task_run_does_not_spawn_competing_env_manager() -> None:
    """`[tasks.build].run` must not invoke a competing environment manager.

    The repo policy (see the header comment of `mise.toml` and
    `tests/test_mise_toolchain.py::COMPETING_ENV_FILES`) forbids
    adding pyenv / poetry / pipenv / conda / asdf invocations
    without a documented rationale. The `build` task should stay
    inside the `uv` build system.
    """
    data = _load_mise_toml()
    build = data.get("tasks", {}).get("build")
    assert isinstance(build, dict), "mise.toml must define [tasks.build]"
    run = build.get("run", "")
    assert isinstance(run, str), "[tasks.build].run must be a string"

    # Word-boundary search so e.g. "pyenv-foo" or `PATH` would not be
    # confused for an invocation. We only forbid top-level command
    # names that the existing COMPETING_ENV_FILES policy already lists.
    forbidden = ("pyenv", "poetry", "pipenv", "conda", "asdf")
    for tool in forbidden:
        assert not re.search(rf"(^|\s|/|\\){re.escape(tool)}(\s|$)", run), (
            f"[tasks.build].run must not invoke {tool!r}; got {run!r}"
        )
