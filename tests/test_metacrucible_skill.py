from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skills" / "metacrucible" / "SKILL.md"


def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def test_skill_frontmatter_is_complete_wrapper() -> None:
    fields = frontmatter(skill_text())
    assert fields["name"] == "metacrucible"
    assert "SKE" + "LETON" not in fields["description"]
    assert "review" in fields["description"]
    assert "optimize" in fields["description"]
    assert "synthesize" in fields["description"]


def test_retired_wrapper_boundary_is_absent() -> None:
    text = skill_text()
    forbidden = [
        "SKE" + "LETON",
        "Issue" + " #" + "3",
        "complete UX is " + "tracked " + "separately",
        "out of " + "scope",
        "not implemented " + "yet",
    ]
    for phrase in forbidden:
        assert phrase not in text


PUBLIC_COMMANDS = ("review", "bootstrap", "optimize", "synthesize", "inspect")
REQUIRED_COMMAND_SUBSECTIONS = (
    "Purpose:",
    "Use when:",
    "Required inputs:",
    "Key flags:",
    "Example:",
    "Output and evidence:",
)


def section_for_command(text: str, command: str) -> str:
    heading = f"### `{command}`"
    start = text.index(heading)
    next_start = text.find("\n### `", start + len(heading))
    if next_start == -1:
        next_start = text.find("\n## ", start + len(heading))
    if next_start == -1:
        next_start = len(text)
    return text[start:next_start]


def test_public_command_sections_are_complete() -> None:
    text = skill_text()
    assert "## Command reference" in text
    for command in PUBLIC_COMMANDS:
        section = section_for_command(text, command)
        for label in REQUIRED_COMMAND_SUBSECTIONS:
            assert label in section, f"{command} missing {label}"
        assert f"python -m metacrucible {command}" in section


from metacrucible.exit_codes import (
    EXIT_BLOCKED,
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_USER_ERROR,
)


def test_exit_code_table_matches_cli_contract() -> None:
    text = skill_text()
    expected_rows = {
        EXIT_OK: "`EXIT_OK`",
        EXIT_USER_ERROR: "`EXIT_USER_ERROR`",
        EXIT_BLOCKED: "`EXIT_BLOCKED`",
        EXIT_INTERNAL_ERROR: "`EXIT_INTERNAL_ERROR`",
    }
    for code, label in expected_rows.items():
        assert label in text
        assert f"| {code} |" in text


def test_blocked_bundle_propagation_is_documented() -> None:
    text = skill_text()
    required = [
        "baseline create",
        "evaluate",
        "optimize",
        "evaluation-stage `synthesize`",
        "execution-requested `review`",
        "receipt.json",
        "summary.json",
        "trajectory-digest.json",
        "EXIT_BLOCKED",
        "do not retry automatically",
    ]
    for phrase in required:
        assert phrase in text
