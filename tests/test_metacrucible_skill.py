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