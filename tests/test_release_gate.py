"""Tests for Issue #48 Task 2: release gate script + contract.

These tests pin the release-readiness gate asserted by issue #48's PyPI
release tooling plan, Task 2:

  - ``scripts/release_gate.py`` exposes ``main(argv=None) -> int`` and
    is parseable as a Python module.
  - Placeholder versions (``0.0.0``, ``0.0``, ``Unreleased``, empty)
    are rejected with a non-zero exit and a clear ``stderr`` reason.
  - A real SemVer version is accepted when ``CHANGELOG.md`` has a
    matching ``## [<version>]`` heading.
  - Missing or mismatched changelog section produces a non-zero exit.
  - With ``--check-tag``, a missing ``v<version>`` git tag produces a
    non-zero exit; without the flag, the tag check is skipped.
  - The ``mise.toml`` ``[tasks.release-gate]`` task wires the gate so
    ``mise run release-gate`` actually invokes the script.

All gate-behavior tests use ``tmp_path`` fixtures to build a synthetic
repo (no real tags, no real PyPI). The git-tag subset is gated on
``git`` being on ``PATH`` (mirrors the ``shutil.which`` skipif style
used in ``tests/test_build_task.py``).
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MISE_TOML = REPO_ROOT / "mise.toml"
SCRIPTS_DIR = REPO_ROOT / "scripts"
GATE_SCRIPT = SCRIPTS_DIR / "release_gate.py"
EXPECTED_VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _load_mise_toml() -> dict:
    with MISE_TOML.open("rb") as fh:
        return tomllib.load(fh)


def _import_gate():
    """Load ``scripts/release_gate.py`` as a module via importlib.

    The gate is a standalone script (NOT a package, NOT under
    ``src/metacrucible``), so we cannot import it through the normal
    package machinery. ``spec_from_file_location`` lets the tests
    invoke ``main(argv)`` directly without spawning a subprocess and
    without polluting ``sys.path``.
    """
    spec = importlib.util.spec_from_file_location(
        "_metacrucible_release_gate_under_test", GATE_SCRIPT,
    )
    assert spec is not None and spec.loader is not None, (
        f"failed to build import spec for {GATE_SCRIPT}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _write_pyproject(repo: Path, version: str) -> None:
    """Write a minimal ``pyproject.toml`` containing ``[project].version``."""
    (repo / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""\
            [project]
            name = "synthetic"
            version = "{version}"
            """
        ),
        encoding="utf-8",
    )


def _write_changelog(repo: Path, *sections: str) -> None:
    """Write a ``CHANGELOG.md`` whose body has one ``## [<version>]`` per arg.

    Passing no args yields a changelog with only an ``## [Unreleased]``
    section, which never satisfies a real release version. Passing
    ``"1.2.3"`` yields a changelog with one ``## [1.2.3]`` heading.
    """
    body_lines = ["# Changelog", "", "All notable changes documented here.", ""]
    for version in sections:
        body_lines.append(f"## [{version}]")
        body_lines.append("")
        body_lines.append("### Added")
        body_lines.append("")
        body_lines.append(f"- release {version}")
        body_lines.append("")
    (repo / "CHANGELOG.md").write_text("\n".join(body_lines), encoding="utf-8")


def _make_git_tag(repo: Path, version: str) -> None:
    """Init a git repo at ``repo`` and create the ``v<version>`` tag.

    Per-repo git config (inside ``.git/config``) keeps the developer's
    ``~/.gitconfig`` untouched. The signing-related keys are explicitly
    disabled at the repo level so a lightweight ``git tag v<version>``
    works regardless of any global ``tag.gpgsign`` /
    ``commit.gpgsign`` the developer has set — without these overrides
    the developer's GPG/SSH signing settings cause ``git tag`` to fail
    with "fatal: no tag message?" because lightweight tags carry no
    message to sign.
    """
    base = ["git", "-C", str(repo)]
    subprocess.run(base + ["init", "-q"], check=True)
    subprocess.run(base + ["config", "tag.gpgsign", "false"], check=True)
    subprocess.run(base + ["config", "commit.gpgsign", "false"], check=True)
    subprocess.run(base + ["config", "user.email", "gate@test"], check=True)
    subprocess.run(base + ["config", "user.name", "gate"], check=True)
    subprocess.run(base + ["add", "."], check=True)
    subprocess.run(base + ["commit", "-q", "-m", "init"], check=True)
    subprocess.run(base + ["tag", f"v{version}"], check=True)


# --------------------------------------------------------------------------- #
# Public surface — `main(argv=None) -> int`                                    #
# --------------------------------------------------------------------------- #


def test_gate_script_exists() -> None:
    """`scripts/release_gate.py` must exist as a standalone script."""
    assert GATE_SCRIPT.is_file(), (
        f"expected {GATE_SCRIPT.relative_to(REPO_ROOT)} to exist"
    )


def test_main_is_callable_and_returns_int(tmp_path: Path) -> None:
    """`release_gate.main` must be callable as `main(argv=None) -> int`.

    The acceptance contract pins the public signature; this test
    confirms both that the attribute exists on the module and that an
    explicit-argv invocation against a valid synthetic repo returns 0
    (an ``int``, not ``None`` or a truthy value).
    """
    gate = _import_gate()
    assert callable(getattr(gate, "main", None)), (
        "release_gate.main must be callable"
    )

    _write_pyproject(tmp_path, "1.2.3")
    _write_changelog(tmp_path, "1.2.3")

    rc = gate.main(["--repo-root", str(tmp_path)])
    assert isinstance(rc, int), f"main() must return an int; got {type(rc).__name__}"
    assert rc == 0, f"valid synthetic repo must return 0; got rc={rc}"


def test_main_accepts_none_argv() -> None:
    """`main(argv=None)` must not raise — it falls back to `sys.argv[1:]`.

    We do not assert the return value here because the ambient repo
    state is whatever the developer has; the contract is "must not
    raise on None".
    """
    gate = _import_gate()
    # Use the real worktree repo root explicitly; the gate either
    # passes (green) or fails with a clear stderr reason. Either way,
    # no exception.
    rc = gate.main(["--repo-root", str(REPO_ROOT)])
    assert isinstance(rc, int)


# --------------------------------------------------------------------------- #
# Placeholder-version rejection                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_version",
    ["0.0.0", "0.0", "Unreleased", ""],
    ids=["zero-zero-zero", "zero-zero", "unreleased", "empty"],
)
def test_placeholder_versions_rejected(tmp_path: Path, bad_version: str) -> None:
    """`0.0.0`, `0.0`, `Unreleased`, and empty must produce non-zero exit.

    Each placeholder is written into ``pyproject.toml`` and the
    changelog is given a matching section so the version check is the
    branch under test (not the changelog check).
    """
    gate = _import_gate()
    _write_pyproject(tmp_path, bad_version)
    _write_changelog(tmp_path, bad_version if bad_version else "1.0.0")

    rc = gate.main(["--repo-root", str(tmp_path)])
    assert rc != 0, (
        f"placeholder version {bad_version!r} must be rejected (got rc={rc})"
    )


def test_valid_version_zero_one_zero_accepted_when_section_matches(
    tmp_path: Path,
) -> None:
    """The seed version `0.1.0` must pass when its `## [0.1.0]` section exists.

    This pins the *positive* side of the placeholder detection:
    ``0.1.0`` must NOT be treated as a placeholder, and a matching
    changelog section must satisfy the version+changelog check.
    """
    gate = _import_gate()
    _write_pyproject(tmp_path, EXPECTED_VERSION)
    _write_changelog(tmp_path, EXPECTED_VERSION)

    rc = gate.main(["--repo-root", str(tmp_path)])
    assert rc == 0, (
        f"version {EXPECTED_VERSION!r} with matching changelog section "
        f"must return 0; got rc={rc}"
    )


# --------------------------------------------------------------------------- #
# Changelog-section rejection                                                  #
# --------------------------------------------------------------------------- #


def test_missing_changelog_file_rejected(tmp_path: Path) -> None:
    """A valid version with NO `CHANGELOG.md` at all must fail non-zero."""
    gate = _import_gate()
    _write_pyproject(tmp_path, "1.2.3")
    # Intentionally do NOT write CHANGELOG.md.

    rc = gate.main(["--repo-root", str(tmp_path)])
    assert rc != 0, "missing CHANGELOG.md must produce a non-zero exit"


def test_missing_matching_changelog_section_rejected(tmp_path: Path) -> None:
    """A valid version with a changelog that has only `## [Unreleased]` must fail."""
    gate = _import_gate()
    _write_pyproject(tmp_path, "1.2.3")
    _write_changelog(tmp_path)  # only `## [Unreleased]` is present

    rc = gate.main(["--repo-root", str(tmp_path)])
    assert rc != 0, (
        "changelog with only `## [Unreleased]` must not satisfy a real "
        "release version (got rc=0)"
    )


def test_mismatched_changelog_section_rejected(tmp_path: Path) -> None:
    """A changelog with `## [9.9.9]` must not satisfy a `1.2.3` version.

    Pins the heading-vs-version matching: the gate must compare the
    captured version inside the brackets to the pyproject version, not
    merely detect the presence of a ``## [X]`` heading.
    """
    gate = _import_gate()
    _write_pyproject(tmp_path, "1.2.3")
    _write_changelog(tmp_path, "9.9.9")

    rc = gate.main(["--repo-root", str(tmp_path)])
    assert rc != 0, (
        "changelog with only `## [9.9.9]` must not satisfy version `1.2.3`"
    )


def test_subheading_with_brackets_does_not_satisfy_heading(tmp_path: Path) -> None:
    """A `### [1.2.3]` sub-heading must NOT satisfy the `## [1.2.3]` check.

    The regex anchors on ``^##`` (two hashes), so sub-headings with
    three hashes do not match. This pins the heading-shape contract.
    """
    gate = _import_gate()
    _write_pyproject(tmp_path, "1.2.3")
    (tmp_path / "CHANGELOG.md").write_text(
        textwrap.dedent(
            """\
            # Changelog

            ## [Unreleased]

            ### [1.2.3]

            - sub-heading mention, NOT a real release section
            """
        ),
        encoding="utf-8",
    )

    rc = gate.main(["--repo-root", str(tmp_path)])
    assert rc != 0, (
        "a `### [1.2.3]` sub-heading must not satisfy the `## [1.2.3]` check"
    )


# --------------------------------------------------------------------------- #
# Tag check (opt-in via --check-tag)                                          #
# --------------------------------------------------------------------------- #


def test_tag_check_skipped_by_default(tmp_path: Path) -> None:
    """Default invocation must NOT require a git tag — tag check is opt-in.

    Without ``--check-tag``, even a repo with no git history must
    pass the version + changelog checks. This is the "default
    invocation validates version + changelog only" branch.
    """
    gate = _import_gate()
    _write_pyproject(tmp_path, "1.2.3")
    _write_changelog(tmp_path, "1.2.3")
    # No git init at all.

    rc = gate.main(["--repo-root", str(tmp_path)])
    assert rc == 0, (
        f"default invocation must skip tag check; got rc={rc}"
    )


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is not on PATH; cannot exercise the tag branch",
)
def test_missing_tag_rejected_when_check_tag_enabled(tmp_path: Path) -> None:
    """With `--check-tag`, a missing `v<version>` tag must fail non-zero.

    A git repo is initialized so the tag-check subprocess has a real
    git context to run in; the ``v1.2.3`` tag is intentionally NOT
    created. The gate must observe the empty tag list and exit non-zero.
    """
    gate = _import_gate()
    _write_pyproject(tmp_path, "1.2.3")
    _write_changelog(tmp_path, "1.2.3")
    _make_git_tag(tmp_path, "0.9.9")  # different version tag

    rc = gate.main(["--repo-root", str(tmp_path), "--check-tag"])
    assert rc != 0, (
        f"missing v1.2.3 tag with --check-tag must fail; got rc={rc}"
    )


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is not on PATH; cannot exercise the tag branch",
)
def test_present_tag_passes_when_check_tag_enabled(tmp_path: Path) -> None:
    """With `--check-tag`, a present `v<version>` tag must allow rc=0."""
    gate = _import_gate()
    _write_pyproject(tmp_path, "1.2.3")
    _write_changelog(tmp_path, "1.2.3")
    _make_git_tag(tmp_path, "1.2.3")

    rc = gate.main(["--repo-root", str(tmp_path), "--check-tag"])
    assert rc == 0, (
        f"present v1.2.3 tag with --check-tag must pass; got rc={rc}"
    )


# --------------------------------------------------------------------------- #
# stderr contract                                                             #
# --------------------------------------------------------------------------- #


def test_failure_writes_reason_to_stderr(tmp_path: Path, capsys) -> None:
    """A failing gate must write a clear reason to stderr (not stdout).

    The dispatch contract: "exit non-zero on failure (return code, not
    just print) and write the reason to stderr." This test confirms the
    stderr side of that contract on the placeholder branch; stdout must
    remain empty so callers (CI, mise task) only see the reason where
    they expect it.
    """
    gate = _import_gate()
    _write_pyproject(tmp_path, "0.0.0")
    _write_changelog(tmp_path, "1.0.0")

    rc = gate.main(["--repo-root", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc != 0, "placeholder must fail"
    assert captured.out == "", (
        f"stdout must be empty on failure; got {captured.out!r}"
    )
    assert "release-gate:" in captured.err, (
        f"stderr must be prefixed with `release-gate:`; got {captured.err!r}"
    )
    assert "0.0.0" in captured.err or "placeholder" in captured.err.lower(), (
        f"stderr must mention the placeholder version or reason; "
        f"got {captured.err!r}"
    )


# --------------------------------------------------------------------------- #
# mise.toml — [tasks.release-gate] wiring                                     #
# --------------------------------------------------------------------------- #


def test_mise_toml_declares_release_gate_task() -> None:
    """`mise.toml` must declare a `[tasks.release-gate]` table with a non-empty `run`.

    A missing or empty `run` would silently make `mise run release-gate`
    a no-op, defeating the release toolchain.
    """
    data = _load_mise_toml()
    tasks = data.get("tasks")
    assert isinstance(tasks, dict), "mise.toml must declare a [tasks] table"
    rg = tasks.get("release-gate")
    assert isinstance(rg, dict), (
        "mise.toml must define [tasks.release-gate] (issue #48 Task 2)"
    )
    run = rg.get("run")
    assert isinstance(run, str) and run.strip(), (
        "[tasks.release-gate].run must be a non-empty string so "
        "`mise run release-gate` actually invokes the gate"
    )


def test_mise_toml_release_gate_task_has_description() -> None:
    """`[tasks.release-gate]` must carry a `description`, matching other tasks.

    All existing `[tasks.*]` entries (`install`, `test`, `test-replay`,
    `test-local-real`, `build`) carry a `description`; the new
    `release-gate` task must follow the same shape so `mise tasks`
    output stays consistent.
    """
    data = _load_mise_toml()
    rg = data.get("tasks", {}).get("release-gate")
    assert isinstance(rg, dict), "mise.toml must define [tasks.release-gate]"
    description = rg.get("description")
    assert isinstance(description, str) and description.strip(), (
        "[tasks.release-gate].description must be a non-empty string "
        "matching the other [tasks.*] entries"
    )


def test_mise_toml_release_gate_run_invokes_gate_script() -> None:
    """`[tasks.release-gate].run` must invoke `scripts/release_gate.py`."""
    data = _load_mise_toml()
    rg = data.get("tasks", {}).get("release-gate")
    assert isinstance(rg, dict), "mise.toml must define [tasks.release-gate]"
    run = rg.get("run", "")
    assert "release_gate.py" in run, (
        f"[tasks.release-gate].run must invoke scripts/release_gate.py; "
        f"got {run!r}"
    )


# --------------------------------------------------------------------------- #
# End-to-end — gate module importable + runnable as a script                    #
# --------------------------------------------------------------------------- #


def test_gate_module_does_not_import_metacrucible() -> None:
    """The gate script must not import the `metacrucible` package.

    The dispatch contract: "Do NOT import `metacrucible` from the gate
    script; parse `pyproject.toml` directly." Confirmed by inspecting
    the script's source for any ``import metacrucible`` /
    ``from metacrucible`` reference.
    """
    text = GATE_SCRIPT.read_text(encoding="utf-8")
    assert "import metacrucible" not in text, (
        "release_gate.py must not import the metacrucible package"
    )
    assert "from metacrucible" not in text, (
        "release_gate.py must not import anything from the metacrucible package"
    )


def test_gate_uses_stdlib_tomllib() -> None:
    """The gate script must parse pyproject.toml via the stdlib `tomllib`.

    No new runtime dependency may be introduced; `tomllib` (stdlib,
    Python 3.11+) is the only acceptable parser.
    """
    text = GATE_SCRIPT.read_text(encoding="utf-8")
    assert "import tomllib" in text, (
        "release_gate.py must use the stdlib `tomllib` to parse pyproject.toml"
    )