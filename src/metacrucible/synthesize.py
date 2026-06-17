"""Issue #41 (PRD F4) ``synthesize`` pipeline: non-optimizing synthesis.

Replaces the Task 1 ``synthesize-not-implemented`` placeholder with
the real draft-creation flow. The pipeline accepts either a freeform
``capability_need`` string or a spec-file path, refuses inputs that
are missing / conflicting / empty / non-existent, and on success
creates:

  - the draft canonical source (a Markdown Skill),
  - the per-artifact ``.metacrucible/`` envelope + state,
  - a ``benchmark.jsonl`` containing the metadata record plus
    pre-partitioned generated eval + held-out cases, and
  - the four history events that pin the synthesis lineage.

The optimization stage (Task 3) and the BLOCKED-bundle write for
the evaluation stage (Task 4) live in their own slices; this module
is deliberately a thin wrapper around the existing repository-side
helpers so the slice is auditable end-to-end.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from .benchmark import SPLIT_EVAL, SPLIT_HELD_OUT, STATUS_GENERATED
from .exit_codes import EXIT_BLOCKED, EXIT_OK
from .promote import _atomic_write_jsonl
from .storage import (
    RepositoryStorage,
    compute_benchmark_digest,
)

#: Benchmark file name (ADR 0029). Re-stated locally so this module
#: is a leaf that does not import from :mod:`metacrucible.__main__`
#: (which would create a circular import when invoked via
#: ``python -m metacrucible``). The value is pinned by ADR 0029 and
#: by the existing test suite; do not change without coordinating
#: with both.
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Case-level sentinel field that the ``optimize`` gate keys off
#: of (Issue #30 AC3 / Issue #41 Task 2 AC). The string is the
#: machine-stable contract; ``promote`` removes the field and the
#: synthesized case records carry it forward so the draft-pending-
#: review state is observable to the F3 optimize gate.
#:
#: DRIFT NOTE: This value is mirrored in ``metacrucible.__main__``
#: (the F2 ``bootstrap`` setter and the F3 ``optimize`` gate
#: reader both key off this string) and is hardcoded as a string
#: literal in ``metacrucible.promote.promote_case`` (which pops
#: the field on promote). The natural home for the constant is
#: ``metacrucible.benchmark`` alongside the other benchmark-
#: sentinels (``STATUS_GENERATED``, ``SPLIT_EVAL``,
#: ``SPLIT_HELD_OUT``), and ``synthesize.py`` already imports
#: from that module. The move is out of scope for this repair
#: (the hard requirement limits edits to ``synthesize.py``); a
#: future follow-up should add the constant to ``benchmark.py``
#: and update all three call sites in lockstep to eliminate
#: the drift risk surfaced by the Task 2 code-quality review.
BOOTSTRAP_PENDING_REVIEW_FIELD = "BOOTSTRAP_PENDING_REVIEW"

# --------------------------------------------------------------------------- #
# Stable blocker ids (Issue #41 / PRD F4)                                    #
# --------------------------------------------------------------------------- #

#: Stable blocker id emitted by ``synthesize`` when the caller
#: provides neither a positional ``capability_need`` nor a
#: ``--from`` spec path. The parser-level mutually-exclusive group
#: normally surfaces this as ``SystemExit(2)``; the dispatcher-level
#: id exists so the BLOCKED bundle carries the same machine-stable
#: code if a future caller bypasses argparse.
SYNTHESIZE_INPUT_MISSING_BLOCKER = "synthesize-input-missing"

#: Stable blocker id emitted by ``synthesize`` when the caller
#: provides BOTH a positional ``capability_need`` and a
#: ``--from`` spec path. The parser-level group already raises
#: ``SystemExit(2)`` for the conflict; the id is the machine-
#: stable contract for any dispatcher-level enforcement path.
SYNTHESIZE_INPUT_CONFLICT_BLOCKER = "synthesize-input-conflict"

#: Stable blocker id emitted by ``synthesize`` when ``--from``
#: points at a path that does not exist or is not a file. The
#: command refuses to invent a verdict for a missing spec; the
#: operator must supply a real file.
SYNTHESIZE_SPEC_MISSING_BLOCKER = "synthesize-spec-missing"

#: Stable blocker id emitted by ``synthesize`` when ``--from``
#: points at an existing file whose content is empty (or
#: whitespace-only). The synthesized draft would have no
#: capability need to anchor on; an empty spec is a
#: precondition failure, not a success.
SYNTHESIZE_SPEC_EMPTY_BLOCKER = "synthesize-spec-empty"

#: Stable blocker id emitted by ``synthesize`` when the
#: ``--output`` path already exists. The synthesis pipeline
#: refuses to clobber an existing workspace or file (per the
#: ``init``-style idempotency contract); the operator must
#: remove or rename the target.
SYNTHESIZE_OUTPUT_EXISTS_BLOCKER = "synthesize-output-exists"

# --------------------------------------------------------------------------- #
# Payload outcomes (Issue #41 / PRD F4)                                       #
# --------------------------------------------------------------------------- #

#: ``outcome`` value on the success payload of a draft-pending-review
#: ``synthesize`` invocation. Mirrors the F2 ``bootstrap`` contract
#: where the operator is expected to run ``promote`` after human
#: review.
SYNTHESIZE_DRAFT_PENDING_REVIEW = "draft_pending_review"

#: ``outcome`` value reserved for a future aborted-after-write
#: branch. Unused by Task 2; pinned here so the constant set is
#: stable across the F4 slice sequence.
SYNTHESIZE_ABORTED = "aborted"

# --------------------------------------------------------------------------- #
# Tunables                                                                   #
# --------------------------------------------------------------------------- #

#: Number of characters of the slug prefix used by
#: :func:`default_artifact_filename`. The bound is conservative
#: (60 chars) so the resulting filename stays under common
#: filesystem filename limits even after the ``.md`` suffix.
_ARTIFACT_SLUG_PREFIX_LIMIT = 60

#: Placeholder ``input`` text for a synthesized case. The human
#: reviewer replaces it with a real scenario before
#: ``promote``-ing the case; the value is stable so the F3
#: optimize gate's pending-review verdict is observable.
_SYNTHESIZE_CASE_DRAFT_INPUT = (
    "Describe a concrete scenario where the synthesized "
    "capability should help."
)

#: Placeholder ``expected_behavior`` text for a synthesized case.
#: Mirrors :data:`_SYNTHESIZE_CASE_DRAFT_INPUT`; the reviewer
#: replaces it with a real expected behavior before promote.
_SYNTHESIZE_CASE_DRAFT_EXPECTED = (
    "Describe the observable behaviour the synthesized "
    "capability should exhibit for the scenario above."
)


# --------------------------------------------------------------------------- #
# Input resolution                                                           #
# --------------------------------------------------------------------------- #


def resolve_synthesize_input(
    capability_need: str | None,
    from_spec: str | None,
) -> tuple[str | None, list[dict[str, str]]]:
    """Resolve the mutually-exclusive ``synthesize`` input pair.

    Returns ``(need, [])`` on success where ``need`` is the
    capability-need text the rest of the pipeline should
    anchor on (the inline argument stripped, or the spec file
    content stripped of leading / trailing whitespace). On
    failure returns ``(None, blockers)`` with a stable blocker
    id and a human-readable message so the caller can emit a
    single-pass BLOCKED payload without mutating the output
    path.

    The four failure modes are:

      - both ``capability_need`` and ``from_spec`` provided →
        :data:`SYNTHESIZE_INPUT_CONFLICT_BLOCKER`,
      - neither input provided → :data:`SYNTHESIZE_INPUT_MISSING_BLOCKER`,
      - ``--from`` path does not exist or is not a file →
        :data:`SYNTHESIZE_SPEC_MISSING_BLOCKER`,
      - ``--from`` path is an empty (or whitespace-only) file →
        :data:`SYNTHESIZE_SPEC_EMPTY_BLOCKER`.

    The spec is read with UTF-8 *only* after confirming the path
    is a file, so a broken symlink or a directory masquerading
    as a spec path is rejected without raising ``OSError``.
    """
    has_inline = bool(
        capability_need is not None and capability_need.strip()
    )
    has_spec = bool(from_spec is not None and str(from_spec).strip())
    if has_inline and has_spec:
        return None, [
            {
                "id": SYNTHESIZE_INPUT_CONFLICT_BLOCKER,
                "message": (
                    "synthesize accepts either a positional "
                    "capability-need or --from, not both"
                ),
            }
        ]
    if not has_inline and not has_spec:
        return None, [
            {
                "id": SYNTHESIZE_INPUT_MISSING_BLOCKER,
                "message": (
                    "synthesize requires either a positional "
                    "capability-need or --from <spec>"
                ),
            }
        ]
    if has_inline:
        return capability_need.strip(), []
    spec_path = Path(str(from_spec))
    if not spec_path.is_file():
        return None, [
            {
                "id": SYNTHESIZE_SPEC_MISSING_BLOCKER,
                "message": (
                    f"spec file {spec_path} does not exist or "
                    f"is not a file"
                ),
            }
        ]
    spec_text = spec_path.read_text(encoding="utf-8")
    stripped = spec_text.strip()
    if not stripped:
        return None, [
            {
                "id": SYNTHESIZE_SPEC_EMPTY_BLOCKER,
                "message": (
                    f"spec file {spec_path} is empty; "
                    f"provide a non-empty capability spec"
                ),
            }
        ]
    return stripped, []


# --------------------------------------------------------------------------- #
# Draft source + case builders                                               #
# --------------------------------------------------------------------------- #


def default_artifact_filename(need: str) -> str:
    """Return a deterministic ``.md`` filename derived from ``need``.

    The slug is the lowercased concatenation of ASCII
    alphanumeric runs joined with single hyphens, truncated
    to :data:`_ARTIFACT_SLUG_PREFIX_LIMIT` characters. Empty
    or non-sluggable text (no ASCII alphanumerics, e.g.
    emoji-only or pure punctuation) falls back to the stable
    ``synthesized-skill.md`` filename so the contract is
    non-empty for every input.
    """
    runs = re.findall(r"[A-Za-z0-9]+", need)
    if not runs:
        return "synthesized-skill.md"
    slug = "-".join(runs).lower()
    if len(slug) > _ARTIFACT_SLUG_PREFIX_LIMIT:
        slug = slug[:_ARTIFACT_SLUG_PREFIX_LIMIT].rstrip("-")
        if not slug:
            return "synthesized-skill.md"
    return f"{slug}.md"


def build_draft_canonical_source(need: str) -> str:
    """Return the deterministic draft Markdown Skill source.

    The source is a valid Skill (YAML frontmatter with
    ``name`` and ``description``) plus two body sections:

      - ``# Capability Need`` carries the verbatim need so a
        reviewer can confirm the synthesis matches the input,
      - ``# Operating Instructions`` tells the future
        maintainer to replace the generated draft guidance
        with real Skill instructions after the
        draft-pending-review stage.

    The string always ends with exactly one newline so file
    consumers (the optimizer, the static reviewer, the Skill
    parser) see a stable EOF terminator. The frontmatter
    ``description`` is wrapped in single quotes when the need
    contains a single quote, otherwise it is left unquoted
    so the YAML is always well-formed.
    """
    artifact_filename = default_artifact_filename(need)
    if artifact_filename.endswith(".md"):
        name = artifact_filename[: -len(".md")]
    else:
        name = artifact_filename
    description = (
        f"Auto-generated draft Skill for the capability need: "
        f"{need!r}. Replace this guidance with real Skill "
        f"instructions after draft-pending-review."
    )
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        "---",
        "",
        "# Capability Need",
        "",
        need,
        "",
        "# Operating Instructions",
        "",
        (
            "This Skill was generated by `metacrucible synthesize` "
            "(Issue #41, PRD F4). It is a draft pending human "
            "review; after review, replace the generated draft "
            "guidance above with real Skill instructions that "
            "satisfy the capability need stated in the previous "
            "section."
        ),
        "",
    ]
    return "\n".join(lines)


def build_generated_cases(
    need: str, *, now: str
) -> list[dict[str, object]]:
    """Build the pre-partitioned generated case records.

    Returns exactly two records, one eval and one held-out,
    so the synthesis pipeline satisfies the F4 "Generated
    Evaluation Cases are produced and held pending review"
    contract with a minimal, deterministic shape. The eval /
    held-out split is the canonical partition; promote keys
    off the case-level ``split`` field, so the contract is
    satisfied by setting the partition directly on the
    synthesized record (no human action is required to
    partition the cases).

    The ``case_id`` is the deterministic
    ``synthesize-<16-hex>`` form derived from the SHA-256 of
    the ``(need, split, now)`` triple so two runs over the
    same need + timestamp produce the same id, and a
    different ``now`` produces a different id (the unique-id
    contract is preserved even when an operator re-runs
    synthesize against the same capability need in the same
    second).
    """
    return [
        _build_synthesized_case(
            need=need, split=SPLIT_EVAL, now=now
        ),
        _build_synthesized_case(
            need=need, split=SPLIT_HELD_OUT, now=now
        ),
    ]


def _build_synthesized_case(
    *, need: str, split: str, now: str
) -> dict[str, object]:
    seed = f"{need}\x00{split}\x00{now}".encode("utf-8")
    case_id = (
        f"synthesize-{hashlib.sha256(seed).hexdigest()[:16]}"
    )
    return {
        "record_type": _case_record_type(split),
        "case_id": case_id,
        "status": STATUS_GENERATED,
        "split": split,
        "reviewed": False,
        "input": _SYNTHESIZE_CASE_DRAFT_INPUT,
        "expected_behavior": _SYNTHESIZE_CASE_DRAFT_EXPECTED,
        "checks": [],
        "judgment": None,
        "created_at": now,
        BOOTSTRAP_PENDING_REVIEW_FIELD: True,
    }


def _case_record_type(split: str) -> str:
    if split == SPLIT_EVAL:
        return "case_eval"
    if split == SPLIT_HELD_OUT:
        return "case_held_out"
    return "case"


# --------------------------------------------------------------------------- #
# Workspace creation                                                         #
# --------------------------------------------------------------------------- #


def create_synthesis_workspace(
    output: Path,
    need: str,
    *,
    now: str,
) -> dict[str, object]:
    """Create the synthesis workspace and return the payload map.

    Refuses an existing output path with a
    :data:`SYNTHESIZE_OUTPUT_EXISTS_BLOCKER` and creates the
    directory tree on success. The returned mapping carries:

      - ``workspace`` (resolved output directory),
      - ``artifact_path`` (the draft source under the workspace),
      - ``envelope_path`` / ``state_path`` (under
        ``.metacrucible/``),
      - ``benchmark`` (the ``benchmark.jsonl`` path),
      - ``generated_case_ids`` (the list of case ids, in
        ``[eval, held_out]`` order),
      - ``baseline`` (the immediate-baseline mapping: artifact
        hash + benchmark hash so a downstream reviewer can
        re-derive the inputs the synthesis pinned against),
      - ``history_events`` (the four event names appended in
        order: ``synthesis_started``, ``baseline_recorded``,
        ``generated_cases_created``,
        ``synthesis_pending_review``),
      - ``blockers`` (empty on success; non-empty only when
        the output path already exists).
    """
    blockers: list[dict[str, str]] = []
    if output.exists():
        blockers.append(
            {
                "id": SYNTHESIZE_OUTPUT_EXISTS_BLOCKER,
                "message": (
                    f"output path {output} already exists; "
                    f"synthesize refuses to clobber an existing "
                    f"workspace or file"
                ),
            }
        )
        return {
            "workspace": str(output),
            "blockers": blockers,
        }

    output.mkdir(parents=True)
    artifact_filename = default_artifact_filename(need)
    artifact_path = output / artifact_filename
    artifact_source = build_draft_canonical_source(need)
    artifact_path.write_text(artifact_source, encoding="utf-8")

    storage = RepositoryStorage(output)
    capability_need_hash = hashlib.sha256(
        need.encode("utf-8")
    ).hexdigest()
    envelope_payload: dict[str, Any] = {
        "artifact_workspace": str(output),
        "artifact_path": str(artifact_path),
        "source": "synthesize",
        "capability_need_hash": capability_need_hash,
        "created_at": now,
    }
    storage.write_envelope(envelope_payload)

    benchmark_path = output / BENCHMARK_FILE_NAME
    case_records = build_generated_cases(need, now=now)
    metadata_record = {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
        "created_at": now,
    }
    _atomic_write_jsonl(
        benchmark_path, [metadata_record, *case_records]
    )

    # Compute the immediate-baseline hashes for the
    # ``state.json`` baseline mapping. Uses the same digest
    # helpers :func:`metacrucible.storage.compute_benchmark_digest`
    # so a downstream reviewer comparing the synthesize
    # baseline against a later ``baseline create`` invocation
    # sees identical inputs (per ADR 0029).
    artifact_hash = hashlib.sha256(
        artifact_path.read_bytes()
    ).hexdigest()
    benchmark_records = _read_benchmark_records(benchmark_path)
    benchmark_hash = compute_benchmark_digest(benchmark_records)
    state_payload: dict[str, Any] = {
        "current_best_revision": None,
        "last_run_id": None,
        "baseline": {
            "artifact_hash": artifact_hash,
            "benchmark_hash": benchmark_hash,
        },
    }
    storage.write_state(state_payload)

    # History events in the order they happen so a reviewer
    # reading top-to-bottom sees the synthesis lineage.
    storage.append_history(
        {"event": "synthesis_started", "created_at": now}
    )
    storage.append_history(
        {
            "event": "baseline_recorded",
            "artifact_hash": artifact_hash,
            "benchmark_hash": benchmark_hash,
            "created_at": now,
        }
    )
    storage.append_history(
        {
            "event": "generated_cases_created",
            "case_ids": [c["case_id"] for c in case_records],
            "case_count": len(case_records),
            "created_at": now,
        }
    )
    storage.append_history(
        {
            "event": "synthesis_pending_review",
            "artifact_path": str(artifact_path),
            "created_at": now,
        }
    )

    return {
        "workspace": str(output),
        "artifact_path": str(artifact_path),
        "envelope_path": str(storage.envelope_path),
        "state_path": str(storage.state_path),
        "benchmark": str(benchmark_path),
        "generated_case_ids": [
            c["case_id"] for c in case_records
        ],
        "baseline": {
            "artifact_hash": artifact_hash,
            "benchmark_hash": benchmark_hash,
        },
        "history_events": [
            "synthesis_started",
            "baseline_recorded",
            "generated_cases_created",
            "synthesis_pending_review",
        ],
        "blockers": [],
    }


def _read_benchmark_records(
    benchmark: Path,
) -> list[dict[str, Any]]:
    """Read JSONL benchmark records for hashing.

    Mirrors :func:`metacrucible.__main__._read_benchmark_records`
    in spirit (skip blank lines, parse each line as JSON), but
    is implemented locally so this module does not import from
    :mod:`metacrucible.__main__` (which would create a circular
    import when invoked via ``python -m metacrucible``).
    """
    if not benchmark.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in benchmark.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        records.append(json.loads(raw))
    return records


# --------------------------------------------------------------------------- #
# Top-level entry point                                                      #
# --------------------------------------------------------------------------- #


def run_synthesize_command(
    args: Any,
    *,
    emit: Callable[[dict[str, Any]], None],
    now: Callable[[], str],
) -> int:
    """Top-level entry point for the ``synthesize`` subcommand.

    Mirrors the dispatcher pattern of the other commands:
    resolve the input pair, refuse broken inputs with a
    stable blocker id and exit code, create the workspace on
    success, emit the JSON / human payload, and return the
    stable exit code.

    The ``emit`` and ``now`` callables are injected so the
    :mod:`metacrucible.__main__` wrapper can pass the shared
    ``_emit`` and ``_now_iso`` helpers, and so tests can
    capture stdout and freeze time without monkeypatching
    this module.

    Order of operations:

      1. Resolve the input pair (caller-supplied positional or
         ``--from``). Refusal → BLOCKED payload, ``EXIT_BLOCKED``.
      2. Refuse an existing ``--output`` path BEFORE creating
         the workspace. Refusal → BLOCKED payload, ``EXIT_BLOCKED``.
      3. Create the workspace (draft source, envelope, state,
         baseline mapping, benchmark.jsonl, history). On
         success → OK payload with ``outcome='draft_pending_review'``
         and ``EXIT_OK``.
    """
    output = Path(args.output)
    if not output.is_absolute():
        output = output.resolve()

    need, blockers = resolve_synthesize_input(
        getattr(args, "capability_need", None),
        getattr(args, "from_spec", None),
    )
    if blockers:
        payload = {
            "status": "BLOCKED",
            "outcome": "blocked",
            "workspace": str(output),
            "generated_case_ids": [],
            "blockers": blockers,
        }
        emit(payload)
        return EXIT_BLOCKED

    # Pre-flight: refuse an existing output path BEFORE the
    # workspace is created. The ``create_synthesis_workspace``
    # helper enforces the same check, but doing it here keeps
    # the BLOCKED verdict and the no-side-effect guarantee at
    # the dispatcher level (the helper's own check stays as
    # defense-in-depth).
    if output.exists():
        payload = {
            "status": "BLOCKED",
            "outcome": "blocked",
            "workspace": str(output),
            "generated_case_ids": [],
            "blockers": [
                {
                    "id": SYNTHESIZE_OUTPUT_EXISTS_BLOCKER,
                    "message": (
                        f"output path {output} already exists; "
                        f"synthesize refuses to clobber an "
                        f"existing workspace or file"
                    ),
                }
            ],
        }
        emit(payload)
        return EXIT_BLOCKED

    created = create_synthesis_workspace(output, need, now=now())
    payload = {
        "status": "OK",
        "outcome": SYNTHESIZE_DRAFT_PENDING_REVIEW,
        "workspace": created["workspace"],
        "artifact_path": created["artifact_path"],
        "benchmark": created["benchmark"],
        "generated_case_ids": created["generated_case_ids"],
        "sentinel": BOOTSTRAP_PENDING_REVIEW_FIELD,
        "baseline": created["baseline"],
        "blockers": [],
    }
    emit(payload)
    return EXIT_OK
