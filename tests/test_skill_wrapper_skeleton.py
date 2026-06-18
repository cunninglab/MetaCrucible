"""Tests for Issue #3: agent-facing Skill wrapper skeleton.

These tests pin the public wrapper-skeleton behavior asserted by
Issue #3:

  - The Skill wrapper lives at ``skills/metacrucible/SKILL.md`` and
    parses as a Markdown file with YAML frontmatter.
  - The frontmatter advertises a stable ``name`` (``metacrucible``) and
    a non-empty ``description`` so agent runtimes can discover it.
  - The body documents an explicit invocation of the ``metacrucible``
    CLI stub, proving the wrapper can call the CLI.
  - The body is explicitly skeletal: it carries a SKELETON marker and
    a note that the complete UX is tracked separately.

The implementation under test is the file at
``skills/metacrucible/SKILL.md`` — this test file pins the contract
so a future change cannot silently turn the stub into a fake-complete
artifact.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "metacrucible"
SKILL_FILE = SKILL_DIR / "SKILL.md"

# Regex that captures the YAML frontmatter block at the top of the file.
FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)


def _read_skill_markdown() -> str:
    return SKILL_FILE.read_text(encoding="utf-8")


def _parse_skill_parts() -> tuple[dict[str, str], str]:
    """Split the skill file into ``(frontmatter_dict, body)``.

    The frontmatter is parsed line-by-line into a flat ``{key: value}``
    map. We deliberately avoid a YAML dependency: the skeleton only
    needs scalar ``name`` / ``description`` keys, and the existing test
    suite (see ``test_mise_toolchain.py``) uses regex parsing for the
    same reason.
    """
    text = _read_skill_markdown()
    match = FRONTMATTER_RE.match(text)
    assert match, (
        f"{SKILL_FILE.relative_to(REPO_ROOT)} must start with a YAML "
        f"frontmatter block delimited by '---' lines; got first 80 chars: "
        f"{text[:80]!r}"
    )
    front: dict[str, str] = {}
    for raw_line in match.group("front").splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = value.strip()
    return front, match.group("body")


# --------------------------------------------------------------------------- #
# File + frontmatter shape                                                     #
# --------------------------------------------------------------------------- #


def test_skill_directory_exists() -> None:
    """The wrapper must live at ``skills/metacrucible/`` per roadmap W1."""
    assert SKILL_DIR.is_dir(), (
        f"expected {SKILL_DIR.relative_to(REPO_ROOT)}/ to exist"
    )


def test_skill_markdown_file_exists() -> None:
    """The wrapper must be a file named ``SKILL.md`` inside the skill dir."""
    assert SKILL_FILE.is_file(), (
        f"expected {SKILL_FILE.relative_to(REPO_ROOT)} to exist"
    )


def test_skill_markdown_has_frontmatter_block() -> None:
    """``SKILL.md`` must start with a YAML frontmatter block."""
    front, _ = _parse_skill_parts()
    assert front, (
        f"{SKILL_FILE.relative_to(REPO_ROOT)} frontmatter must not be empty"
    )


def test_skill_frontmatter_has_name_and_description() -> None:
    """Frontmatter must carry ``name`` and ``description`` keys."""
    front, _ = _parse_skill_parts()
    assert "name" in front, (
        f"frontmatter must declare a 'name' key; got keys {sorted(front)!r}"
    )
    assert "description" in front, (
        f"frontmatter must declare a 'description' key; got keys {sorted(front)!r}"
    )
    assert front["name"], "frontmatter 'name' must be non-empty"
    assert front["description"], "frontmatter 'description' must be non-empty"


def test_skill_name_is_metacrucible() -> None:
    """The frontmatter ``name`` must be ``metacrucible`` so runtimes route it."""
    front, _ = _parse_skill_parts()
    assert front.get("name") == "metacrucible", (
        f"frontmatter 'name' must be 'metacrucible'; got {front.get('name')!r}"
    )


# --------------------------------------------------------------------------- #
# Wrapper body — invokes CLI stub                                              #
# --------------------------------------------------------------------------- #


def test_skill_body_invokes_cli_stub() -> None:
    """The wrapper body must show an explicit ``metacrucible`` CLI invocation.

    A Skill wrapper that "can call the CLI stub" must, at minimum,
    document a concrete call to the CLI. We look for an invocation
    pattern in the body (a fenced code block or a ``metacrucible ...``
    command line), which proves the wrapper hands work to the CLI
    instead of pretending to implement the surface itself.
    """
    _, body = _parse_skill_parts()
    # The body must contain a CLI invocation. Acceptable forms include
    # the console script (``metacrucible --help``), the module form
    # (``python -m metacrucible ...``), or a fenced code block that
    # includes either.
    has_console_invocation = bool(
        re.search(r"(?m)^\s*metacrucible\b[^\n]*$", body)
    )
    has_module_invocation = bool(
        re.search(r"python\s+-m\s+metacrucible\b", body)
    )
    has_fenced_invocation = bool(
        re.search(r"```[^\n]*\n[^\n]*metacrucible\b[^\n]*\n```", body)
    )
    assert has_console_invocation or has_module_invocation or has_fenced_invocation, (
        f"{SKILL_FILE.relative_to(REPO_ROOT)} body must document an "
        f"explicit invocation of the metacrucible CLI stub; got body:\n{body!r}"
    )


def test_skill_is_explicitly_skeletal() -> None:
    pytest.skip("retired by Issue #43; skeleton boundary is replaced")


def test_skill_documents_complete_ux_tracked_separately() -> None:
    pytest.skip("retired by Issue #43; complete UX is now in this file")