"""Pure-logic unit tests for the apply / rollback helpers (Issue #44).

These tests exercise the named pure-logic helpers in
:mod:`metacrucible.optimizer` directly without going through the
broader contract tests in :mod:`tests.test_optimize_command`. Each
helper is pinned in isolation so a future change to the
apply / rollback path cannot hide behind the public end-to-end
contract:

  - :func:`metacrucible.optimizer.apply_patch_revision` — pure
    re-emit of an artifact after a candidate Patch Revision
    (``Skill`` and ``subagent`` paths).
  - :func:`metacrucible.optimizer._split_artifact_text` — the
    thin frontmatter split used by the apply path (NB-4
    parity-tested against the parser in
    :mod:`tests.test_optimize_command`).
  - :func:`metacrucible.optimizer._join_skill_text` — Skill
    frontmatter + body re-emit.
  - :func:`metacrucible.optimizer._join_subagent_text` —
    subagent frontmatter + systemPrompt + body re-emit.
  - :func:`metacrucible.optimizer._rollback_artifact_text` —
    the best-effort disk rollback (writes the original bytes
    back so a rejected candidate cannot corrupt the artifact).

Fixtures are inline strings with obviously fake placeholders —
no real secrets, no LLM, network, sleep, or subprocess calls —
so the suite runs deterministically under ``pytest -q``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from metacrucible.artifact import (
    MutableRange,
    _content_hash_for,
    parse_skill,
    parse_subagent,
)
from metacrucible.optimizer import (
    PatchRevision,
    _join_skill_text,
    _join_subagent_text,
    _rollback_artifact_text,
    _split_artifact_text,
    apply_patch_revision,
)


# --------------------------------------------------------------------------- #
# Sample sources (obviously fake, no real secrets)                            #
# --------------------------------------------------------------------------- #

SKILL_SOURCE = (
    "---\n"
    "name: example-skill\n"
    "description: Demo skill used by apply-patch pure-logic tests.\n"
    "---\n"
    "\n"
    "# example-skill\n"
    "\n"
    "Skill body. Edit me.\n"
)

SUBAGENT_SOURCE = (
    "---\n"
    "name: example-subagent\n"
    "description: Demo subagent for apply-patch pure-logic tests.\n"
    "tools:\n"
    "  - search\n"
    "  - fetch\n"
    "spawns:\n"
    "  - helper\n"
    "output: json\n"
    "model: opus\n"
    "thinkingLevel: medium\n"
    "readSummarize: concise\n"
    "blocking: true\n"
    "autoloadSkills: false\n"
    "systemPrompt: |\n"
    "  You are a helpful subagent.\n"
    "  Edit me with the body.\n"
    "---\n"
    "\n"
    "Optional Markdown body after frontmatter.\n"
)

EMPTY_BODY_SKILL_SOURCE = (
    "---\n"
    "name: empty-body-skill\n"
    "description: Skill with no Markdown body.\n"
    "---\n"
)


# --------------------------------------------------------------------------- #
# Parser-backed range fixtures                                                #
# --------------------------------------------------------------------------- #


def _skill_body_range(source: str) -> MutableRange:
    """Return the single body mutable range for the fixture Skill source.

    The optimizer treats the parser as the single producer of
    :class:`MutableRange`; using :func:`parse_skill` keeps the
    ``content_hash`` consistent with what the parser would have
    emitted in a real run.
    """
    parsed = parse_skill(source)
    assert len(parsed.mutable_ranges) == 1
    return parsed.mutable_ranges[0]


def _subagent_ranges(source: str) -> list[MutableRange]:
    """Return the (systemPrompt, body) mutable ranges for a subagent.

    The MVP parser pins ``range_id==0`` to ``systemPrompt`` and
    ``range_id==1`` to the body by positional convention; the
    apply path relies on the same coupling, so the fixture
    mirrors it.
    """
    parsed = parse_subagent(source)
    # The MVP subagent contract: exactly two mutable ranges,
    # systemPrompt first, body second.
    assert len(parsed.mutable_ranges) == 2
    return list(parsed.mutable_ranges)


def _subagent_system_prompt_text(source: str) -> str:
    """Return the canonical systemPrompt text for a fixture subagent."""
    return _subagent_ranges(source)[0].text


# --------------------------------------------------------------------------- #
# _split_artifact_text                                                       #
# --------------------------------------------------------------------------- #


class TestSplitArtifactText:
    """Pin the frontmatter split used by the apply / rollback path."""

    def test_splits_skill_frontmatter_and_body(self) -> None:
        front, body = _split_artifact_text(SKILL_SOURCE)
        # Frontmatter keeps the inner text only (no `---` delimiters,
        # no trailing newline). The helper strips exactly one trailing
        # `\n` from the frontmatter span.
        assert front == (
            "name: example-skill\n"
            "description: Demo skill used by apply-patch pure-logic tests."
        )
        # Body starts with the first blank line after the closing
        # delimiter; the helper preserves that leading whitespace.
        assert body.startswith("\n# example-skill\n")
        assert "Skill body. Edit me.\n" in body

    def test_splits_subagent_frontmatter_and_body(self) -> None:
        front, body = _split_artifact_text(SUBAGENT_SOURCE)
        # The subagent frontmatter keeps the systemPrompt block
        # scalar in place; the helper does not normalize it.
        assert "name: example-subagent" in front
        assert "systemPrompt: |" in front
        assert "  You are a helpful subagent." in front
        assert body.startswith("\nOptional Markdown body after frontmatter.\n")

    def test_empty_body_skill_still_splits(self) -> None:
        front, body = _split_artifact_text(EMPTY_BODY_SKILL_SOURCE)
        assert front == (
            "name: empty-body-skill\n"
            "description: Skill with no Markdown body."
        )
        # Empty body source has no content after the closing
        # delimiter, so the body span is the empty string.
        assert body == ""

    def test_missing_frontmatter_returns_empty_front_and_full_source(self) -> None:
        """Defensive branch: source with no YAML frontmatter.

        The parser already rejects such sources upstream, so the
        helper falls back to ``("", source)`` instead of raising.
        """
        bare = "Just a Markdown body with no frontmatter.\n"
        front, body = _split_artifact_text(bare)
        assert front == ""
        assert body == bare

    def test_split_and_join_round_trip_for_skill(self) -> None:
        """Splitting a Skill source then re-joining it yields the original."""
        front, body = _split_artifact_text(SKILL_SOURCE)
        assert _join_skill_text(front, body) == SKILL_SOURCE

    def test_split_and_join_round_trip_for_subagent(self) -> None:
        """Splitting a subagent source then re-joining it yields the original."""
        front, body = _split_artifact_text(SUBAGENT_SOURCE)
        sp_text = _subagent_system_prompt_text(SUBAGENT_SOURCE)
        assert _join_subagent_text(front, sp_text, body) == SUBAGENT_SOURCE


# --------------------------------------------------------------------------- #
# _join_skill_text                                                            #
# --------------------------------------------------------------------------- #


class TestJoinSkillText:
    """Pin the Skill frontmatter + body re-emit shape.

    The helper emits the canonical ``---\\n<front>\\n---\\n<body>``
    shape, regardless of whether ``front`` carries a trailing
    newline. When the caller passes an empty ``front`` the
    helper returns ``body`` unchanged.
    """

    def test_wraps_front_and_body_with_delimiters(self) -> None:
        front = "name: foo\ndescription: bar"
        body = "\n# foo\n\nBody text.\n"
        joined = _join_skill_text(front, body)
        # The helper always inserts exactly one newline between
        # the frontmatter text and the closing `---` delimiter,
        # so callers can pass frontmatter with or without a
        # trailing newline.
        assert joined == f"---\n{front}\n---\n{body}"

    def test_front_with_trailing_newline_still_round_trips(self) -> None:
        """A trailing newline on frontmatter is collapsed to one separator."""
        front = "name: foo\ndescription: bar\n"
        body = "body content"
        joined = _join_skill_text(front, body)
        # Two newlines: one from the helper's own separator, one
        # the caller-supplied trailing newline — and then the
        # closing delimiter.
        assert joined == f"---\n{front}\n---\n{body}"

    def test_empty_front_returns_body_unchanged(self) -> None:
        body = "# plain markdown body\n"
        assert _join_skill_text("", body) == body

    def test_empty_front_with_empty_body_returns_empty(self) -> None:
        assert _join_skill_text("", "") == ""

    def test_empty_body_still_emits_delimiters(self) -> None:
        """An empty body should still keep the frontmatter wrapped."""
        front = "name: foo"
        joined = _join_skill_text(front, "")
        assert joined == f"---\n{front}\n---\n"

    def test_preserves_frontmatter_block_scalar(self) -> None:
        """The helper does not normalize frontmatter whitespace."""
        front = "name: foo\n\ndescription: |\n  multi\n  line"
        body = "body"
        assert _join_skill_text(front, body) == f"---\n{front}\n---\n{body}"


# --------------------------------------------------------------------------- #
# _join_subagent_text                                                         #
# --------------------------------------------------------------------------- #


class TestJoinSubagentText:
    """Pin the subagent frontmatter + systemPrompt + body re-emit shape."""

    def test_empty_system_prompt_falls_back_to_skill_join(self) -> None:
        """No systemPrompt range → behave exactly like a Skill re-emit."""
        front = "name: foo\ndescription: bar"
        body = "\nMarkdown body.\n"
        assert _join_subagent_text(front, "", body) == _join_skill_text(front, body)

    def test_appends_system_prompt_block_when_frontmatter_lacks_one(self) -> None:
        front = "name: foo\ndescription: bar"
        system_prompt = "You are a helpful subagent.\n"
        body = "Body after frontmatter.\n"
        joined = _join_subagent_text(front, system_prompt, body)
        # The helper inserts a `systemPrompt: |` block scalar at
        # the end of the frontmatter with two-space-indented body
        # lines, then the closing delimiter + body.
        expected_front = (
            "name: foo\n"
            "description: bar\n"
            "systemPrompt: |\n"
            "  You are a helpful subagent."
        )
        assert joined == f"---\n{expected_front}\n---\n{body}"

    def test_replaces_existing_system_prompt_block(self) -> None:
        front = (
            "name: foo\n"
            "description: bar\n"
            "systemPrompt: |\n"
            "  OLD prompt line one.\n"
            "  OLD prompt line two.\n"
            "other: keep"
        )
        new_prompt = "NEW prompt line.\n"
        body = "Body.\n"
        joined = _join_subagent_text(front, new_prompt, body)
        # The old block scalar is gone, the new one is in the
        # same position, and unrelated keys (`other:`) survive.
        assert "OLD prompt" not in joined
        assert "  NEW prompt line." in joined
        assert "other: keep" in joined
        # The block scalar header is `systemPrompt: |` exactly.
        assert "systemPrompt: |\n  NEW prompt line." in joined
        # Body stays after the closing delimiter.
        assert joined.endswith(f"---\n{body}")

    def test_multiline_system_prompt_is_indented_with_two_spaces(self) -> None:
        front = "name: foo\ndescription: bar"
        system_prompt = "line one\nline two\nline three"
        body = "Body.\n"
        joined = _join_subagent_text(front, system_prompt, body)
        assert "systemPrompt: |" in joined
        assert "  line one" in joined
        assert "  line two" in joined
        assert "  line three" in joined

    def test_preserves_unrelated_frontmatter_keys(self) -> None:
        front = (
            "name: foo\n"
            "description: bar\n"
            "tools:\n"
            "  - search\n"
            "  - fetch\n"
            "spawns:\n"
            "  - helper\n"
            "output: json\n"
            "model: opus\n"
            "thinkingLevel: medium\n"
            "readSummarize: concise\n"
            "blocking: true\n"
            "autoloadSkills: false"
        )
        joined = _join_subagent_text(front, "You are a subagent.", "Body.\n")
        # Every unrelated key survives the round trip.
        for key in (
            "name: foo",
            "description: bar",
            "tools:",
            "  - search",
            "  - fetch",
            "spawns:",
            "  - helper",
            "output: json",
            "model: opus",
            "thinkingLevel: medium",
            "readSummarize: concise",
            "blocking: true",
            "autoloadSkills: false",
        ):
            assert key in joined, f"missing key: {key!r}"

    def test_subagent_round_trip_preserves_source_byte_for_byte(self) -> None:
        """A subagent source split through the apply path round-trips."""
        front, body = _split_artifact_text(SUBAGENT_SOURCE)
        sp_text = _subagent_system_prompt_text(SUBAGENT_SOURCE)
        assert _join_subagent_text(front, sp_text, body) == SUBAGENT_SOURCE


# --------------------------------------------------------------------------- #
# apply_patch_revision (Skill)                                                #
# --------------------------------------------------------------------------- #


class TestApplyPatchRevisionSkill:
    """Pin the Skill apply path: single body mutable range."""

    def test_skill_replaces_body_with_per_range_text(self) -> None:
        body_range = _skill_body_range(SKILL_SOURCE)
        new_body = "\n# example-skill (revised)\n\nNew body.\n"
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="skill",
            base_artifact_text=SKILL_SOURCE,
            per_range_text={body_range.range_id: new_body},
            mutable_ranges=[body_range],
        )
        # Frontmatter preserved verbatim; body is the replacement.
        assert "name: example-skill" in result
        assert "description: Demo skill used by apply-patch pure-logic tests." in result
        assert new_body in result
        # The original body text is gone after the apply.
        assert "Skill body. Edit me.\n" not in result

    def test_skill_with_no_replacement_leaves_body_unchanged(self) -> None:
        """A missing per_range_text entry must leave the body intact."""
        body_range = _skill_body_range(SKILL_SOURCE)
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="skill",
            base_artifact_text=SKILL_SOURCE,
            per_range_text={},
            mutable_ranges=[body_range],
        )
        # Round-trip: split + join of the original source.
        assert result == SKILL_SOURCE

    def test_skill_with_empty_mutable_ranges_leaves_artifact_unchanged(self) -> None:
        """No mutable ranges → no body replacement can happen."""
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="skill",
            base_artifact_text=SKILL_SOURCE,
            per_range_text={0: "should not be used"},
            mutable_ranges=[],
        )
        assert result == SKILL_SOURCE

    def test_skill_does_not_touch_routing_surface(self) -> None:
        """The apply path must never mutate routing-surface fields."""
        body_range = _skill_body_range(SKILL_SOURCE)
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="skill",
            base_artifact_text=SKILL_SOURCE,
            per_range_text={body_range.range_id: "REPLACED BODY"},
            mutable_ranges=[body_range],
        )
        # Routing-surface fields are preserved byte-for-byte.
        assert "name: example-skill" in result
        assert "description: Demo skill used by apply-patch pure-logic tests." in result

    def test_skill_empty_body_range_replacement_becomes_empty_body(self) -> None:
        """Replacing the body with the empty string strips the body."""
        body_range = _skill_body_range(EMPTY_BODY_SKILL_SOURCE)
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="skill",
            base_artifact_text=EMPTY_BODY_SKILL_SOURCE,
            per_range_text={body_range.range_id: ""},
            mutable_ranges=[body_range],
        )
        # Frontmatter stays, body becomes empty. The closing
        # delimiter is the final `---` of the rebuilt source.
        assert "name: empty-body-skill" in result
        assert result.endswith("---\n")

    def test_skill_base_artifact_path_is_ignored(self) -> None:
        """The function is pure; the path argument is unused for the return value."""
        body_range = _skill_body_range(SKILL_SOURCE)
        result_a = apply_patch_revision(
            base_artifact_path="/tmp/a/SKILL.md",
            artifact_kind="skill",
            base_artifact_text=SKILL_SOURCE,
            per_range_text={body_range.range_id: "NEW BODY"},
            mutable_ranges=[body_range],
        )
        result_b = apply_patch_revision(
            base_artifact_path="/tmp/b/SKILL.md",
            artifact_kind="skill",
            base_artifact_text=SKILL_SOURCE,
            per_range_text={body_range.range_id: "NEW BODY"},
            mutable_ranges=[body_range],
        )
        assert result_a == result_b

    def test_skill_replacement_uses_skill_join_shape(self) -> None:
        """The Skill re-emit must go through the Skill join path."""
        body_range = _skill_body_range(SKILL_SOURCE)
        new_body = "REPLACED"
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="skill",
            base_artifact_text=SKILL_SOURCE,
            per_range_text={body_range.range_id: new_body},
            mutable_ranges=[body_range],
        )
        # The Skill apply path does NOT fold in a systemPrompt
        # block scalar; the frontmatter must be a single block
        # followed by the new body.
        assert "systemPrompt:" not in result
        # The body is exactly the replacement.
        assert result.endswith(f"---\n{new_body}")


# --------------------------------------------------------------------------- #
# apply_patch_revision (subagent)                                             #
# --------------------------------------------------------------------------- #


class TestApplyPatchRevisionSubagent:
    """Pin the subagent apply path: systemPrompt (range 0) + body (range 1)."""

    def test_subagent_replaces_body_keeps_system_prompt(self) -> None:
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        system_prompt_range, body_range = ranges[0], ranges[1]
        new_body = "\n# revised body\n\nNew content.\n"
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={body_range.range_id: new_body},
            mutable_ranges=[system_prompt_range, body_range],
        )
        # The systemPrompt block scalar is preserved.
        assert "You are a helpful subagent." in result
        assert "Edit me with the body." in result
        # The body is replaced.
        assert new_body in result
        assert "Optional Markdown body after frontmatter." not in result

    def test_subagent_replaces_system_prompt_keeps_body(self) -> None:
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        system_prompt_range, body_range = ranges[0], ranges[1]
        new_prompt = "You are the new subagent.\nWith a fresh system prompt."
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={system_prompt_range.range_id: new_prompt},
            mutable_ranges=[system_prompt_range, body_range],
        )
        # The systemPrompt block scalar carries the new text.
        assert "You are the new subagent." in result
        assert "With a fresh system prompt." in result
        # The old prompt text is gone.
        assert "You are a helpful subagent." not in result
        # The body is preserved.
        assert "Optional Markdown body after frontmatter." in result

    def test_subagent_replaces_both_ranges(self) -> None:
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        system_prompt_range, body_range = ranges[0], ranges[1]
        new_prompt = "Fresh prompt."
        new_body = "\n# fresh body\n\nFresh content.\n"
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={
                system_prompt_range.range_id: new_prompt,
                body_range.range_id: new_body,
            },
            mutable_ranges=[system_prompt_range, body_range],
        )
        assert "Fresh prompt." in result
        assert "# fresh body" in result
        assert "Fresh content." in result
        # The old prompt and body text are gone.
        assert "You are a helpful subagent." not in result
        assert "Optional Markdown body after frontmatter." not in result

    def test_subagent_with_no_replacements_round_trips(self) -> None:
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={},
            mutable_ranges=list(ranges),
        )
        # No replacements → output equals the input source.
        assert result == SUBAGENT_SOURCE

    def test_subagent_skips_replacement_for_missing_range_id(self) -> None:
        """An unknown range_id in per_range_text is ignored, not raised."""
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={999: "should be ignored"},
            mutable_ranges=list(ranges),
        )
        # The unknown range_id is dropped and the source is unchanged.
        assert result == SUBAGENT_SOURCE

    def test_subagent_empty_mutable_ranges_keeps_front_and_body(self) -> None:
        """No mutable ranges → neither systemPrompt nor body is replaced."""
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={
                0: "should not be used",
                1: "should not be used",
            },
            mutable_ranges=[],
        )
        # The systemPrompt block and body survive byte-for-byte.
        assert "You are a helpful subagent." in result
        assert "Optional Markdown body after frontmatter." in result

    def test_subagent_uses_first_mutable_range_as_system_prompt(self) -> None:
        """The first mutable range's text is the source of the systemPrompt.

        The apply path reads ``mutable_ranges[0].text`` as the
        systemPrompt seed when no replacement is provided for
        range_id==0, so the rebuilt source must still carry the
        original systemPrompt text.
        """
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        system_prompt_range, body_range = ranges[0], ranges[1]
        # Provide a body replacement only; the systemPrompt seed
        # comes from the first mutable range itself.
        new_body = "\nBody swap.\n"
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={body_range.range_id: new_body},
            mutable_ranges=[system_prompt_range, body_range],
        )
        # The systemPrompt text from the first mutable range
        # survives in the rebuilt frontmatter.
        assert "You are a helpful subagent." in result
        assert "Edit me with the body." in result
        # The body is replaced.
        assert new_body in result

    def test_subagent_replace_system_prompt_via_range_id_zero(self) -> None:
        """NB-3 contract: range_id==0 is the subagent systemPrompt."""
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        system_prompt_range, body_range = ranges[0], ranges[1]
        # range_id==0 must hit the systemPrompt slot, not the body.
        new_prompt = "zero-range subagent prompt"
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={0: new_prompt},
            mutable_ranges=[system_prompt_range, body_range],
        )
        assert new_prompt in result
        # The body is untouched.
        assert "Optional Markdown body after frontmatter." in result

    def test_subagent_replace_body_via_range_id_one(self) -> None:
        """NB-3 contract: range_id==1 is the subagent body."""
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        system_prompt_range, body_range = ranges[0], ranges[1]
        new_body = "\nbody via range_id==1\n"
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text={1: new_body},
            mutable_ranges=[system_prompt_range, body_range],
        )
        assert new_body in result
        # The systemPrompt is untouched.
        assert "You are a helpful subagent." in result


# --------------------------------------------------------------------------- #
# apply_patch_revision — PatchRevision compatibility                          #
# --------------------------------------------------------------------------- #


class TestApplyPatchRevisionWithPatchRevision:
    """Cross-check apply_patch_revision against the PatchRevision shape.

    PatchRevision is the in-memory candidate that the merge plan
    produces; the apply path consumes a flat ``per_range_text``
    mapping. This group keeps the mapping shape honest by
    building a PatchRevision and passing its ``per_range_text``
    dict straight through.
    """

    def test_skill_apply_uses_patch_revision_per_range_text(self) -> None:
        body_range = _skill_body_range(SKILL_SOURCE)
        new_body = "\n# new\n\nFrom PatchRevision.\n"
        revision = PatchRevision(
            run_id="opt-test-run",
            round_id="opt-test-round",
            per_range_text={body_range.range_id: new_body},
            base_hashes={body_range.range_id: body_range.content_hash},
        )
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="skill",
            base_artifact_text=SKILL_SOURCE,
            per_range_text=revision.per_range_text,
            mutable_ranges=[body_range],
        )
        assert new_body in result
        assert "Skill body. Edit me.\n" not in result

    def test_subagent_apply_uses_patch_revision_per_range_text(self) -> None:
        ranges = _subagent_ranges(SUBAGENT_SOURCE)
        system_prompt_range, body_range = ranges[0], ranges[1]
        new_prompt = "From PatchRevision."
        new_body = "\nBody from PatchRevision.\n"
        revision = PatchRevision(
            run_id="opt-test-run",
            round_id="opt-test-round",
            per_range_text={
                system_prompt_range.range_id: new_prompt,
                body_range.range_id: new_body,
            },
            base_hashes={
                system_prompt_range.range_id: system_prompt_range.content_hash,
                body_range.range_id: body_range.content_hash,
            },
        )
        result = apply_patch_revision(
            base_artifact_path="unused",
            artifact_kind="subagent",
            base_artifact_text=SUBAGENT_SOURCE,
            per_range_text=revision.per_range_text,
            mutable_ranges=[system_prompt_range, body_range],
        )
        assert "From PatchRevision." in result
        assert "Body from PatchRevision." in result


# --------------------------------------------------------------------------- #
# _rollback_artifact_text                                                     #
# --------------------------------------------------------------------------- #


class TestRollbackArtifactText:
    """Pin the disk rollback helper.

    The helper is best-effort and writes the original bytes back
    to disk. It must overwrite any candidate text the apply path
    may have staged.
    """

    def test_rollback_writes_saved_bytes_to_disk(self, tmp_path: Path) -> None:
        artifact = tmp_path / "SKILL.md"
        artifact.write_bytes(b"---\nname: original\n---\n# original\n")
        original_bytes = artifact.read_bytes()

        # Simulate a candidate write that we now want to roll back.
        artifact.write_bytes(b"---\nname: candidate\n---\n# candidate\n")

        saved = b"---\nname: preserved\n---\n# preserved\n"
        _rollback_artifact_text(
            base_artifact_path=artifact,
            saved_text=saved,
        )
        assert artifact.read_bytes() == saved
        # The original bytes we read at the top of the test are
        # different from the saved bytes; confirm the helper did
        # not somehow round-trip to the original by accident.
        assert saved != original_bytes

    def test_rollback_overwrites_existing_content(self, tmp_path: Path) -> None:
        """The helper must overwrite whatever is currently on disk."""
        artifact = tmp_path / "SUBAGENT.md"
        artifact.write_bytes(b"corrupt candidate text")
        saved = b"---\nname: ok\n---\nrestored body\n"
        _rollback_artifact_text(
            base_artifact_path=artifact,
            saved_text=saved,
        )
        assert artifact.read_bytes() == saved

    def test_rollback_creates_file_if_missing(self, tmp_path: Path) -> None:
        """Best-effort rollback: if the file vanished, write it back."""
        artifact = tmp_path / "MISSING.md"
        assert not artifact.exists()
        saved = b"restored content"
        _rollback_artifact_text(
            base_artifact_path=artifact,
            saved_text=saved,
        )
        assert artifact.read_bytes() == saved

    def test_rollback_preserves_byte_for_byte_contents(self, tmp_path: Path) -> None:
        """Round-trip the original bytes through a write/rollback cycle."""
        artifact = tmp_path / "SKILL.md"
        original = SKILL_SOURCE.encode("utf-8")
        # Sanity check: the fixture is exactly what we think it is.
        assert hashlib.sha256(original).hexdigest() == hashlib.sha256(
            SKILL_SOURCE.encode("utf-8")
        ).hexdigest()
        artifact.write_bytes(original)

        # Simulate a candidate write.
        artifact.write_bytes(b"junk")
        assert artifact.read_bytes() != original

        _rollback_artifact_text(
            base_artifact_path=artifact,
            saved_text=original,
        )
        assert artifact.read_bytes() == original

    def test_rollback_with_empty_saved_bytes_clears_file(self, tmp_path: Path) -> None:
        artifact = tmp_path / "SKILL.md"
        artifact.write_bytes(b"candidate text")
        _rollback_artifact_text(
            base_artifact_path=artifact,
            saved_text=b"",
        )
        assert artifact.read_bytes() == b""

    def test_rollback_with_non_utf8_bytes(self, tmp_path: Path) -> None:
        """The helper writes raw bytes; non-UTF-8 payloads are preserved."""
        artifact = tmp_path / "BIN.md"
        # A binary blob that is not valid UTF-8.
        blob = bytes(range(256))
        _rollback_artifact_text(
            base_artifact_path=artifact,
            saved_text=blob,
        )
        assert artifact.read_bytes() == blob

    def test_rollback_accepts_pathlib_path(self, tmp_path: Path) -> None:
        """The helper coerces the path argument to ``Path``."""
        artifact = tmp_path / "SKILL.md"
        artifact.write_bytes(b"junk")
        saved = b"original"
        # Pass a ``Path`` instance directly. The helper wraps its
        # argument with ``Path(...)`` so ``str`` would also work,
        # but the optimizer's caller passes a ``Path``.
        _rollback_artifact_text(
            base_artifact_path=artifact,
            saved_text=saved,
        )
        assert artifact.read_bytes() == saved

    def test_rollback_preserves_content_hash(self, tmp_path: Path) -> None:
        """The saved bytes round-trip to the same SHA-256 the parser would compute."""
        artifact = tmp_path / "SKILL.md"
        original = SKILL_SOURCE.encode("utf-8")
        expected_hash = _content_hash_for(text=SKILL_SOURCE)
        artifact.write_bytes(original)
        artifact.write_bytes(b"junk")
        _rollback_artifact_text(
            base_artifact_path=artifact,
            saved_text=original,
        )
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == expected_hash
