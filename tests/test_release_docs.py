"""Tests for Issue #48 Task 4: release-process docs in CHANGELOG + CONTRIBUTING.

The release-process documentation lives in two places:

- ``CHANGELOG.md`` — a release-tooling bullet must live under the
  existing ``[Unreleased]`` section's ``### Added`` list (Keep a
  Changelog shape). The bullet must reference the Mise ``build`` and
  ``release-gate`` tasks and the Trusted-Publishing release workflow
  so readers can find the documented procedure by searching the
  changelog.
- ``CONTRIBUTING.md`` — a ``## Releasing`` section must live after
  the existing ``## Developer commands`` section and document the
  Mise ``build`` / ``release-gate`` tasks, Trusted Publishing (OIDC)
  as the publish mechanism, and the ``v*`` tag convention that the
  release workflow uses.

These tests read both files as TEXT (no markdown or YAML library
import) and pin the structural + content invariants with string
assertions, matching the repo's pytest-only style. A regression that
moves the section, drops a required reference, or re-introduces
provider-secret language must fail here so the documented procedure
cannot silently drift from the underlying tooling.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
CONTRIBUTING_PATH = REPO_ROOT / "CONTRIBUTING.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading_regex: str) -> str | None:
    """Return the body of the first heading matched by ``heading_regex``.

    ``heading_regex`` matches the full heading line (including the
    ``##`` / ``###`` prefix). The returned body starts on the line
    after the heading and ends just before the next same-or-higher
    level heading. Returns ``None`` when no heading matches.
    """
    lines = text.splitlines()
    start: int | None = None
    prefix = ""
    for idx, line in enumerate(lines):
        match = re.match(heading_regex, line)
        if match:
            start = idx
            prefix = match.group(0).split(" ", 1)[0]
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(rf"{re.escape(prefix)}\s", line) or re.match(
            rf"{re.escape(prefix)}$", line
        ):
            break
        body.append(line)
    return "\n".join(body)


# --------------------------------------------------------------------------- #
# File presence                                                                #
# --------------------------------------------------------------------------- #


def test_changelog_md_exists_at_repo_root() -> None:
    """``CHANGELOG.md`` must live at the repo root."""
    assert CHANGELOG_PATH.is_file(), (
        f"expected {CHANGELOG_PATH.relative_to(REPO_ROOT)} to exist"
    )


def test_contributing_md_exists_at_repo_root() -> None:
    """``CONTRIBUTING.md`` must live at the repo root."""
    assert CONTRIBUTING_PATH.is_file(), (
        f"expected {CONTRIBUTING_PATH.relative_to(REPO_ROOT)} to exist"
    )


# --------------------------------------------------------------------------- #
# CHANGELOG — [Unreleased] ### Added release-tooling bullet                     #
# --------------------------------------------------------------------------- #


def test_changelog_has_unreleased_section() -> None:
    """``CHANGELOG.md`` must keep the ``## [Unreleased]`` heading."""
    text = _read(CHANGELOG_PATH)
    assert re.search(r"^## \[Unreleased\]\s*$", text, re.MULTILINE), (
        "CHANGELOG.md must declare a `## [Unreleased]` section"
    )


def test_changelog_unreleased_has_added_subsection() -> None:
    """The ``[Unreleased]`` section must contain an ``### Added`` list."""
    text = _read(CHANGELOG_PATH)
    unreleased_body = _section(text, r"^## \[Unreleased\]\s*$")
    assert unreleased_body is not None, "CHANGELOG.md missing `## [Unreleased]`"
    assert re.search(r"^### Added\s*$", unreleased_body, re.MULTILINE), (
        "`## [Unreleased]` must contain an `### Added` subsection"
    )


def test_changelog_unreleased_added_has_release_tooling_bullet() -> None:
    """The ``[Unreleased]`` ``### Added`` list must describe the release tooling.

    The bullet must mention both the ``build`` and ``release-gate``
    Mise tasks (the local release procedure) and the Trusted
    Publishing workflow (the publish step). Substring assertions are
    used so the test stays robust to editorial rewording of the
    surrounding sentence.
    """
    text = _read(CHANGELOG_PATH)
    unreleased_body = _section(text, r"^## \[Unreleased\]\s*$")
    assert unreleased_body is not None, "CHANGELOG.md missing `## [Unreleased]`"
    added_body = _section(unreleased_body, r"^### Added\s*$")
    assert added_body is not None, (
        "`## [Unreleased]` is missing an `### Added` subsection"
    )

    assert "`build`" in added_body, (
        "release-tooling bullet must mention the `build` Mise task"
    )
    assert "`release-gate`" in added_body, (
        "release-tooling bullet must mention the `release-gate` Mise task"
    )
    assert "Trusted Publishing" in added_body or "Trusted-Publishing" in added_body, (
        "release-tooling bullet must mention the Trusted Publishing workflow"
    )


def test_changelog_does_not_fabricate_versioned_section() -> None:
    """Task 4 must NOT add a fabricated ``## [<version>]`` section.

    The release gate (Task 2) decides release readiness; pre-creating
    a versioned section here would short-circuit the gate. The
    ``## [0.1.0]`` heading specifically must remain absent because
    ``pyproject.toml`` pins ``version = "0.1.0"`` and the gate looks
    for that exact heading.
    """
    text = _read(CHANGELOG_PATH)
    assert not re.search(r"^## \[0\.1\.0\]\s*$", text, re.MULTILINE), (
        "CHANGELOG.md must not pre-create a `## [0.1.0]` section; "
        "the release gate (Task 2) validates this at release time"
    )


# --------------------------------------------------------------------------- #
# CONTRIBUTING — ## Releasing section                                          #
# --------------------------------------------------------------------------- #


def test_contributing_has_releasing_section() -> None:
    """``CONTRIBUTING.md`` must declare a ``## Releasing`` section."""
    text = _read(CONTRIBUTING_PATH)
    assert re.search(r"^## Releasing\s*$", text, re.MULTILINE), (
        "CONTRIBUTING.md must declare a `## Releasing` section"
    )


def test_releasing_section_appears_after_developer_commands() -> None:
    """The ``## Releasing`` section must follow ``## Developer commands``.

    The release procedure is a workflow extension of the developer
    commands documented above it, so the section order must keep
    ``Releasing`` after ``Developer commands`` (and before ``Test
    layers``).
    """
    text = _read(CONTRIBUTING_PATH)
    dev_match = re.search(r"^## Developer commands\s*$", text, re.MULTILINE)
    rel_match = re.search(r"^## Releasing\s*$", text, re.MULTILINE)
    test_match = re.search(r"^## Test layers\s*$", text, re.MULTILINE)
    assert dev_match is not None, "missing `## Developer commands` section"
    assert rel_match is not None, "missing `## Releasing` section"
    assert test_match is not None, "missing `## Test layers` section"
    assert dev_match.start() < rel_match.start() < test_match.start(), (
        "`## Releasing` must appear after `## Developer commands` and "
        "before `## Test layers`"
    )


def test_releasing_section_names_build_task() -> None:
    """The ``## Releasing`` section must reference ``mise run build``."""
    text = _read(CONTRIBUTING_PATH)
    body = _section(text, r"^## Releasing\s*$")
    assert body is not None, "CONTRIBUTING.md missing `## Releasing`"
    assert "mise run build" in body, (
        "`## Releasing` must reference `mise run build` by exact task name"
    )


def test_releasing_section_names_release_gate_task() -> None:
    """The ``## Releasing`` section must reference ``mise run release-gate``."""
    text = _read(CONTRIBUTING_PATH)
    body = _section(text, r"^## Releasing\s*$")
    assert body is not None, "CONTRIBUTING.md missing `## Releasing`"
    assert "mise run release-gate" in body, (
        "`## Releasing` must reference `mise run release-gate` by exact task name"
    )


def test_releasing_section_documents_trusted_publishing() -> None:
    """The ``## Releasing`` section must document Trusted Publishing (OIDC).

    OIDC is the publish mechanism, so the section must mention
    ``Trusted Publishing`` and explicitly call out the OIDC variant
    so a reader cannot mistake it for a token-based flow.
    """
    text = _read(CONTRIBUTING_PATH)
    body = _section(text, r"^## Releasing\s*$")
    assert body is not None, "CONTRIBUTING.md missing `## Releasing`"
    assert "Trusted Publishing" in body or "Trusted-Publishing" in body, (
        "`## Releasing` must document Trusted Publishing"
    )
    assert "OIDC" in body, (
        "`## Releasing` must explicitly call out the OIDC publish mechanism"
    )


def test_releasing_section_documents_v_star_tag_convention() -> None:
    """The ``## Releasing`` section must reference the ``v*`` tag convention.

    The release workflow (``release.yml``) triggers on
    ``push: tags: ['v*']``; the docs must mirror that convention so a
    maintainer knows how to cut a release.
    """
    text = _read(CONTRIBUTING_PATH)
    body = _section(text, r"^## Releasing\s*$")
    assert body is not None, "CONTRIBUTING.md missing `## Releasing`"
    assert "`v*`" in body, (
        "`## Releasing` must reference the `v*` tag convention used by release.yml"
    )


def test_releasing_section_does_not_instruct_pypi_api_token() -> None:
    """The ``## Releasing`` section must NOT instruct setting a provider API key.

    Trusted Publishing (OIDC) is the documented publish mechanism; any
    instruction to configure ``PYPI_API_TOKEN`` (or another provider
    API key) for the release pipeline would push a future maintainer
    away from the secret-free OIDC path.
    """
    text = _read(CONTRIBUTING_PATH)
    body = _section(text, r"^## Releasing\s*$")
    assert body is not None, "CONTRIBUTING.md missing `## Releasing`"
    assert "PYPI_API_TOKEN" not in body, (
        "`## Releasing` must not instruct configuring PYPI_API_TOKEN; "
        "Trusted Publishing (OIDC) is the publish mechanism"
    )


def test_releasing_section_references_release_workflow() -> None:
    """The ``## Releasing`` section must reference the release workflow file.

    The release workflow (``release.yml``) is the canonical publish
    trigger; the docs must name the file so readers know where the
    OIDC / ``v*`` tag behavior lives.
    """
    text = _read(CONTRIBUTING_PATH)
    body = _section(text, r"^## Releasing\s*$")
    assert body is not None, "CONTRIBUTING.md missing `## Releasing`"
    assert "release.yml" in body, (
        "`## Releasing` must reference `.github/workflows/release.yml`"
    )
