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
import datetime as _dt
import re
import sys
from pathlib import Path
from typing import Any, Callable

from .benchmark import (
    SPLIT_EVAL,
    SPLIT_HELD_OUT,
    STATUS_GENERATED,
    load_benchmark,
)
from .blocked_bundles import write_blocked_bundle
from .exit_codes import EXIT_BLOCKED, EXIT_OK
from .promote import _atomic_write_jsonl
from .optimizer import ROUND_BUDGET_DEFAULT as _ROUND_BUDGET_DEFAULT, run_optimizer_pipeline
from .replay import build_optimizer_call_fn, load_replay
from .storage import (
    RepositoryStorage,
    UserGlobalStorage,
    compute_benchmark_digest,
)

def _now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string.

    Mirror of :func:`metacrucible.__main__._now_iso` in form
    only -- the synthesize module is a leaf that does not
    import from :mod:`metacrucible.__main__` (that would
    create a circular import when invoked via
    ``python -m metacrucible``), so the helper is re-stated
    here. Tests freeze the clock by
    ``monkeypatch.setattr(synth_mod, "_now_iso", lambda: FROZEN)``
    to make the run_id byte-stable.
    """
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


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

#: ``outcome`` value for the Task 3 optimizer-accepted resume
#: path. Pinned alongside :data:`SYNTHESIZE_DRAFT_PENDING_REVIEW`
#: and :data:`SYNTHESIZE_ABORTED` so the synthesize command
#: exposes a small stable outcome vocabulary across the create
#: and resume paths.
SYNTHESIZE_ACCEPTED = "accepted"

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


# --------------------------------------------------------------------------- #
# Task 3 (Issue #41 / PRD F4) — optimizer resume path                         #
# --------------------------------------------------------------------------- #

#: Stable blocker id emitted when ``synthesize`` is re-invoked
#: against a path that exists on disk but is not a synthesis
#: workspace (no envelope, or ``envelope.source != 'synthesize'``).
#: The dispatcher refuses to silently clobber an existing
#: directory that does not carry the synthesis lineage; the
#: operator must remove or rename the target.
#:
#: Reuses the same string as
#: :data:`SYNTHESIZE_OUTPUT_EXISTS_BLOCKER` so a downstream
#: consumer can branch on a single blocker id regardless of
#: whether the rejected path is an empty directory, a file,
#: or a non-synthesis workspace tree.
SYNTHESIZE_NOT_A_WORKSPACE_BLOCKER = SYNTHESIZE_OUTPUT_EXISTS_BLOCKER

#: History event name appended to
#: ``<workspace>/.metacrucible/history.jsonl`` immediately
#: before the Task 3 optimizer call. Pinned alongside the
#: other synthesis-lineage events (Task 2:
#: ``synthesis_started`` / ``baseline_recorded`` /
#: ``generated_cases_created`` / ``synthesis_pending_review``)
#: so the audit trail can be sliced top-to-bottom.
SYNTHESIS_HISTORY_OPTIMIZER_STARTED = "synthesis_optimizer_started"

#: History event name appended immediately after the Task 3
#: optimizer call. Carries the final ``outcome`` (one of
#: ``accepted`` / ``aborted``) and the optimizer's
#: ``stop_reason`` and the run correlation fields
#: (``run_id``, ``rounds``) so a reviewer can reconstruct
#: the run timeline with run_id correlation.
SYNTHESIS_HISTORY_FINISHED = "synthesis_finished"

# --------------------------------------------------------------------------- #
# Task 4 (Issue #41 / PRD F4) - synthesize evaluation-stage BLOCKED bundle   #
# --------------------------------------------------------------------------- #

#: Run-type value written into the BLOCKED evidence bundle by
#: :func:`_write_synthesize_blocked_bundle`. Matches the ADR 0035
#: ``synthesize_evaluation_stage`` slot in
#: :data:`metacrucible.blocked_bundles.REQUIRES_BLOCKED_BUNDLE_CATEGORIES`
#: so the matrix routes the BLOCKED bundle write through
#: :func:`metacrucible.blocked_bundles.write_blocked_bundle`.
SYNTHESIZE_EVALUATION_BLOCKED_BUNDLE_RUN_TYPE = "synthesize_evaluation_stage"

#: Run-id prefix used when emitting the synthesize-evaluation-stage
#: BLOCKED evidence bundle. Mirrors the ``optimize`` / ``evaluate``
#: / ``baseline-create`` prefixes; downstream tooling can branch on
#: the prefix to distinguish synthesize-evaluation-stage bundles
#: from other BLOCKED categories.
SYNTHESIZE_BLOCKED_BUNDLE_RUN_ID_PREFIX = "synthesize"


def _write_synthesize_blocked_bundle(
    blockers: list[dict[str, object]],
) -> dict[str, str]:
    """Emit the ADR 0035 synthesize-evaluation-stage BLOCKED bundle.

    Best-effort: a write failure is logged to stderr and the
    function returns an empty ``dict`` so the caller
    (:func:`_synthesize_resume_branch`) still returns
    :data:`EXIT_BLOCKED`. The BLOCKED bundle is the *evidence*
    of the BLOCKED verdict, not the source of truth; the
    in-memory payload wins.

    Mirrors :func:`metacrucible.__main__._write_optimize_blocked_bundle`
    so the four BLOCKED-emitting commands share a single,
    predictable write contract.

    Parameters
    ----------
    blockers:
        Sequence of ``{id, message}`` mappings. Forwarded
        verbatim to :func:`metacrucible.blocked_bundles.write_blocked_bundle`
        which normalises them through the v1 contract.

    Returns
    -------
    A mapping of evidence-ref keys (``blocked_receipt``,
    ``blocked_summary``, ``blocked_trajectory_digest``) to
    the bundle's sibling-relative filenames, or an empty
    ``dict`` when the bundle write failed. The keys are
    namespaced with ``blocked_`` so they cannot collide with
    the optimizer's own ``evidence_refs`` keys when the
    caller merges the two mappings into the payload's
    ``evidence_refs`` field.
    """
    refs: dict[str, str] = {}
    try:
        global_store = UserGlobalStorage()
        run_id = (
            f"{SYNTHESIZE_BLOCKED_BUNDLE_RUN_ID_PREFIX}-"
            f"{_now_iso().replace(':', '').replace('-', '')}"
        )
        bundle_dir = write_blocked_bundle(
            global_store,
            run_id=run_id,
            run_type=SYNTHESIZE_EVALUATION_BLOCKED_BUNDLE_RUN_TYPE,
            blockers=blockers,
        )
        bundle_name = bundle_dir.name
        refs = {
            "blocked_receipt": f"{bundle_name}/receipt.json",
            "blocked_summary": f"{bundle_name}/summary.json",
            "blocked_trajectory_digest": (
                f"{bundle_name}/trajectory-digest.json"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        print(
            "metacrucible: failed to write synthesize BLOCKED "
            "bundle: " + type(exc).__name__ + ": " + str(exc),
            file=sys.stderr,
        )
    return refs


def load_synthesis_workspace(output: Path) -> dict[str, object]:
    """Load the synthesis-workspace files and resolve ``artifact_path``.

    Returns a mapping that the Task 3 resume path consumes:

      - ``workspace``: the resolved output directory (str),
      - ``envelope``: the parsed ``envelope.json`` (dict) or
        ``None`` when the file is absent,
      - ``state``: the parsed ``state.json`` (dict) or
        ``None`` when the file is absent,
      - ``benchmark``: the resolved ``benchmark.jsonl`` path,
      - ``artifact_path``: the resolved ``Path`` of the
        envelope-declared artifact (or ``None`` if the
        envelope does not declare one),
      - ``envelope_path`` / ``state_path``: the
        ``.metacrucible/`` paths, present so the history
        append helpers can re-use them,
      - ``blockers``: a list of ``{"id", "message"}`` dicts.
        Empty when the workspace is a well-formed synthesis
        workspace; non-empty when any of the required files
        are absent or the envelope does not declare
        ``source == "synthesize"``.

    The helper is read-only: it never mutates the workspace.
    """
    workspace = Path(output)
    envelope_path = workspace / ".metacrucible" / "envelope.json"
    state_path = workspace / ".metacrucible" / "state.json"
    benchmark_path = workspace / BENCHMARK_FILE_NAME
    blockers: list[dict[str, str]] = []

    envelope: dict[str, Any] | None = None
    if envelope_path.is_file():
        try:
            raw = json.loads(
                envelope_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            raw = None
        if isinstance(raw, dict):
            envelope = raw

    if envelope is None:
        blockers.append(
            {
                "id": SYNTHESIZE_NOT_A_WORKSPACE_BLOCKER,
                "message": (
                    f"output path {workspace} exists but does "
                    f"not carry a synthesis envelope at "
                    f"{envelope_path}; synthesize refuses to "
                    f"interpret a non-synthesis directory as "
                    f"a resume target"
                ),
            }
        )
    elif envelope.get("source") != "synthesize":
        # A different workspace lineage (e.g. ``init``,
        # ``bootstrap``) is on disk; refuse to recurse into
        # it as a synthesis workspace.
        blockers.append(
            {
                "id": SYNTHESIZE_NOT_A_WORKSPACE_BLOCKER,
                "message": (
                    f"envelope at {envelope_path} declares "
                    f"source={envelope.get('source')!r}; "
                    f"synthesize only resumes workspaces with "
                    f"source='synthesize'"
                ),
            }
        )

    state: dict[str, Any] | None = None
    if state_path.is_file():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = None
        if isinstance(raw, dict):
            state = raw

    artifact_path_value: Path | None = None
    if envelope is not None:
        for key in ("artifact_path", "canonical_source"):
            value = envelope.get(key)
            if isinstance(value, str) and value:
                artifact_path_value = Path(value)
                break

    if not benchmark_path.is_file() and not blockers:
        blockers.append(
            {
                "id": SYNTHESIZE_NOT_A_WORKSPACE_BLOCKER,
                "message": (
                    f"synthesis workspace at {workspace} is "
                    f"missing the benchmark file at "
                    f"{benchmark_path}; the workspace is "
                    f"incomplete and cannot be resumed"
                ),
            }
        )

    return {
        "workspace": str(workspace),
        "envelope": envelope,
        "state": state,
        "envelope_path": str(envelope_path),
        "state_path": str(state_path),
        "benchmark": str(benchmark_path),
        "artifact_path": artifact_path_value,
        "blockers": blockers,
    }


def benchmark_ready_for_optimization(
    benchmark_path: Path,
) -> tuple[bool, list[dict[str, object]]]:
    """Classify a benchmark against the F3 runnability contract.

    The F3 ``optimize`` command uses
    :func:`metacrucible.benchmark.load_benchmark` to partition
    the cases into the four ADR 0029 buckets and to surface
    the machine-stable blockers (duplicate ids, schema
    mismatch, missing reviewed eval / held-out, pending
    generated). The synthesize resume path reuses that
    classification but maps it to a small two-slot contract
    that the dispatcher can branch on:

      - ``(False, [])`` — the benchmark carries pending
        generated cases; the synthesize path must short-
        circuit to ``draft_pending_review`` and refuse to
        call the optimizer until the operator has reviewed
        and promoted the cases.
      - ``(True, [])`` — eligible eval and held-out counts
        are both non-zero and the loader returned no
        blockers; the benchmark is optimize-runnable, so
        the dispatcher can hand the workspace to the F3
        pipeline.
      - ``(False, blockers)`` — every other failure mode:
        the benchmark file is missing, the schema is wrong,
        cases are missing, or duplicate ids are present.
        The returned ``blockers`` list is the verbatim
        loader output so the dispatcher can surface them
        on the BLOCKED payload.
    """
    path = Path(benchmark_path)
    if not path.is_file():
        return False, [
            {
                "id": "missing-benchmark-file",
                "message": (
                    f"benchmark file {path} does not exist; "
                    f"synthesize cannot resume without a "
                    f"benchmark to optimize against"
                ),
            }
        ]
    result = load_benchmark(path)
    if result.pending_generated_cases:
        return False, []
    blockers: list[dict[str, object]] = [
        dict(b) for b in result.blockers
        if isinstance(b, dict)
    ]
    if blockers:
        return False, blockers
    if not result.eligible_eval_cases or not result.eligible_held_out_cases:
        # Defensive: load_benchmark surfaces the
        # missing-reviewed blockers above, but the
        # explicit check keeps the two-slot contract
        # honest if a future loader revision ever
        # returns a non-runnable result with empty
        # blockers (the contract is "no eligible
        # cases -> not ready").
        return False, [
            {
                "id": "missing-reviewed-cases",
                "message": (
                    "benchmark is not optimize-runnable: "
                    "no eligible reviewed eval and / or "
                    "held-out cases (ADR 0025)"
                ),
            }
        ]
    return True, []


def run_synthesis_optimizer(
    *,
    workspace: Path,
    benchmark_path: Path,
    artifact_path: Path,
    max_rounds: int = _ROUND_BUDGET_DEFAULT,
    allow_routing_revision: bool = False,
    allow_dirty_unrelated: bool = False,
    confirm_resume: bool = False,
    replay: str | None = None,
) -> Any:
    """Invoke the F3 optimizer pipeline from the synthesize resume path.

    Thin wrapper that pins the synthesize-side flags to the
    values the F3 command uses (mirrors
    :func:`metacrucible.__main__.cmd_optimize`'s first
    pipeline call):

      - ``call_fn=None`` (the MVP does not wire a real
        LLM; tests monkey-patch the imported
        :func:`metacrucible.optimizer.run_optimizer_pipeline`
        reference to inject a deterministic fake),
      - ``human_confirmed=False`` (the synthesize path
        reuses the routing-confirmation preview / gate
        flow owned by F3; the operator is not pre-
        confirming routing revisions at the synthesize
        step),
      - ``routing_confirmation_preview=True`` (F3 owns
        the cutover from the legacy ``--confirm-routing``
        flag to ``--allow-routing-revision``; the
        synthesize path delegates to that gate rather
        than re-implementing it).

    The ``max_rounds`` budget is forwarded verbatim from
    the dispatcher's ``--max-rounds`` flag (the F3 default
    is one round for the minimal safe MVP).

    The three F3 confirmation flags are threaded through
    the synthesize resume path so the CLI does not silently
    drop them after the parser-level dest flip:

      - ``allow_routing_revision``: when the preview pass
        returns ``status == "PREVIEW"`` (the pipeline
        surfaced at least one unconfirmed routing
        suggestion), the wrapper escalates to a mutating
        pass with ``human_confirmed=True`` only when this
        flag is ``True``. Without the flag the preview
        result stands and the BLOCKED verdict downstream
        carries the routing-revision blocker. Mirrors
        :func:`metacrucible.__main__.cmd_optimize`'s
        preview / apply cutover. The synthesize path is
        non-interactive (no TTY prompt), so the explicit
        CLI flag is the only escalation path.
      - ``allow_dirty_unrelated`` /
        ``confirm_resume``: forwarded verbatim so the
        caller (:func:`_synthesize_resume_branch`) can
        reflect them on the BLOCKED payload alongside
        :func:`cmd_optimize`'s shape; the synthesize
        resume path does not currently own a gate for
        either, but the kwargs are now part of the
        signature so the parser-level dest-flipping is
        observable end-to-end instead of silently
        dropped before the optimizer call.
    """
    replay_call_fn = (
        build_optimizer_call_fn(load_replay(Path(replay)))
        if replay
        else None
    )
    preview_result = run_optimizer_pipeline(
        workspace=Path(workspace),
        benchmark_path=Path(benchmark_path),
        artifact_path=Path(artifact_path),
        call_fn=replay_call_fn,
        max_rounds=max_rounds,
        human_confirmed=False,
        routing_confirmation_preview=True,
    )
    if (
        preview_result.status == "PREVIEW"
        and allow_routing_revision
    ):
        return run_optimizer_pipeline(
            workspace=Path(workspace),
            benchmark_path=Path(benchmark_path),
            artifact_path=Path(artifact_path),
            call_fn=replay_call_fn,
            max_rounds=max_rounds,
            human_confirmed=True,
            routing_confirmation_preview=False,
        )
    return preview_result


def _emit_pending_review_payload(
    *,
    workspace_path: Path,
    benchmark_path: Path,
    envelope: dict[str, Any] | None,
    state: dict[str, Any] | None,
    artifact_path_value: Path | None,
) -> dict[str, Any]:
    """Build the Task 3 ``draft_pending_review`` resume payload.

    Mirrors the Task 2 create-success payload shape so a
    downstream consumer cannot tell the difference between
    a fresh draft-pending-review and a re-invocation that
    short-circuited because the operator has not yet
    reviewed the generated cases.
    """
    generated_case_ids: list[str] = []
    if benchmark_path.is_file():
        for record in _read_benchmark_records(benchmark_path):
            if not isinstance(record, dict):
                continue
            if record.get("record_type") == "metadata":
                continue
            cid = record.get("case_id")
            if isinstance(cid, str) and cid:
                generated_case_ids.append(cid)
    baseline: dict[str, Any] = {}
    if isinstance(state, dict):
        state_baseline = state.get("baseline")
        if isinstance(state_baseline, dict):
            baseline = {
                "artifact_hash": str(
                    state_baseline.get("artifact_hash", "")
                ),
                "benchmark_hash": str(
                    state_baseline.get("benchmark_hash", "")
                ),
            }
    payload: dict[str, Any] = {
        "status": "OK",
        "outcome": SYNTHESIZE_DRAFT_PENDING_REVIEW,
        "workspace": str(workspace_path),
        "benchmark": str(benchmark_path),
        "generated_case_ids": generated_case_ids,
        "sentinel": BOOTSTRAP_PENDING_REVIEW_FIELD,
        "baseline": baseline,
        "blockers": [],
    }
    if artifact_path_value is not None:
        payload["artifact_path"] = str(artifact_path_value)
    elif isinstance(envelope, dict):
        env_artifact = envelope.get("artifact_path")
        if isinstance(env_artifact, str) and env_artifact:
            payload["artifact_path"] = env_artifact
    return payload


def _synthesize_resume_branch(
    args: Any,
    loaded: dict[str, object],
    *,
    emit: Callable[[dict[str, Any]], None],
    now: Callable[[], str],
    replay: str | None = None,
) -> int:
    """Dispatch the Task 3 resume path on a loaded synthesis workspace.

    Branch order:

      1. Pending generated cases -> ``draft_pending_review``
         payload + ``EXIT_OK`` (operator still needs to
         review + promote).
      2. Benchmark not ready (other loader blockers) ->
         ``aborted`` payload + ``EXIT_BLOCKED`` with the
         verbatim loader blockers.
      3. Benchmark ready -> invoke the F3 optimizer; map
         ``ACCEPTED + acceptance_decision.accepted`` to
         ``accepted`` + ``EXIT_OK``; every other completion
         maps to ``aborted`` + ``EXIT_BLOCKED``.

    History events ``synthesis_optimizer_started`` and
    ``synthesis_finished`` bracket the optimizer call.
    """
    workspace_path = Path(loaded["workspace"])
    benchmark_path = Path(loaded["benchmark"])
    envelope = loaded.get("envelope")
    state = loaded.get("state")
    artifact_path_value = loaded.get("artifact_path")

    ready, ready_blockers = benchmark_ready_for_optimization(
        benchmark_path
    )
    if not ready and not ready_blockers:
        payload = _emit_pending_review_payload(
            workspace_path=workspace_path,
            benchmark_path=benchmark_path,
            envelope=envelope if isinstance(envelope, dict) else None,
            state=state if isinstance(state, dict) else None,
            artifact_path_value=(
                artifact_path_value
                if isinstance(artifact_path_value, Path)
                else None
            ),
        )
        emit(payload)
        return EXIT_OK
    if not ready:
        payload = {
            "status": "BLOCKED",
            "outcome": SYNTHESIZE_ABORTED,
            "workspace": str(workspace_path),
            "benchmark": str(benchmark_path),
            "blockers": ready_blockers,
            "warnings": [],
        }
        if isinstance(artifact_path_value, Path):
            payload["artifact_path"] = str(artifact_path_value)
        emit(payload)
        return EXIT_BLOCKED

    if not isinstance(artifact_path_value, Path):
        blockers = [
            {
                "id": "synthesize-artifact-unresolved",
                "message": (
                    f"envelope at {loaded['envelope_path']} does "
                    f"not declare an artifact_path; synthesize "
                    f"cannot hand the workspace to the optimizer"
                ),
            }
        ]
        payload = {
            "status": "BLOCKED",
            "outcome": SYNTHESIZE_ABORTED,
            "workspace": str(workspace_path),
            "benchmark": str(benchmark_path),
            "blockers": blockers,
            "warnings": [],
        }
        emit(payload)
        return EXIT_BLOCKED

    storage = RepositoryStorage(workspace_path)
    storage.append_history(
        {
            "event": SYNTHESIS_HISTORY_OPTIMIZER_STARTED,
            "benchmark": str(benchmark_path),
            "artifact_path": str(artifact_path_value),
            "created_at": now(),
        }
    )

    max_rounds = int(
        getattr(args, "max_rounds", _ROUND_BUDGET_DEFAULT)
    )
    if max_rounds < 1:
        max_rounds = 1
    # The three F3 confirmation flags are threaded through the
    # synthesize resume path so the parser-level dest-flipping
    # is observable end-to-end instead of silently dropped
    # before the optimizer call (cross-task integration gap
    # surfaced by the F4 global review). ``allow_routing_revision``
    # controls the preview / apply cutover in
    # :func:`run_synthesis_optimizer`; ``allow_dirty_unrelated``
    # is reflected on the BLOCKED payload to mirror
    # :func:`metacrucible.__main__.cmd_optimize`; ``confirm_resume``
    # is recorded for symmetry even though the synthesize path
    # does not currently own an interrupted-run gate.
    allow_routing_revision = bool(
        getattr(args, "allow_routing_revision", False)
    )
    allow_dirty_unrelated = bool(
        getattr(args, "allow_dirty_unrelated", False)
    )
    confirm_resume = bool(
        getattr(args, "confirm_resume", False)
    )
    pipeline_result = run_synthesis_optimizer(
        workspace=workspace_path,
        benchmark_path=benchmark_path,
        artifact_path=artifact_path_value,
        max_rounds=max_rounds,
        allow_routing_revision=allow_routing_revision,
        allow_dirty_unrelated=allow_dirty_unrelated,
        confirm_resume=confirm_resume,
        replay=replay,
    )
    pipeline_status = str(getattr(pipeline_result, "status", ""))
    acceptance_decision = getattr(
        pipeline_result, "acceptance_decision", {}
    ) or {}
    accepted_flag = bool(acceptance_decision.get("accepted") is True)
    optimizer_blockers: list[dict[str, object]] = list(
        getattr(pipeline_result, "blockers", []) or []
    )
    if pipeline_status == "ACCEPTED" and accepted_flag:
        outcome = SYNTHESIZE_ACCEPTED
        status = "OK"
        exit_code = EXIT_OK
        blocked_bundle_refs: dict[str, str] = {}
    else:
        outcome = SYNTHESIZE_ABORTED
        status = "BLOCKED"
        exit_code = EXIT_BLOCKED
        # Task 4: write the ADR 0035 minimal BLOCKED evidence
        # bundle for the synthesize evaluation stage. Called
        # ONLY for evaluation-stage aborted/BLOCKED outcomes
        # after the optimizer stage has been reached (this
        # branch). Input validation, missing spec, empty spec,
        # ordinary pending-review draft creation, and
        # pre-optimizer benchmark blockers do NOT call the
        # bundle writer (per the Task 4 hard requirement).
        blocked_bundle_refs = _write_synthesize_blocked_bundle(
            optimizer_blockers,
        )
    payload = {
        "status": status,
        "outcome": outcome,
        "workspace": str(workspace_path),
        "benchmark": str(benchmark_path),
        "artifact_path": str(artifact_path_value),
        "run_id": str(getattr(pipeline_result, "run_id", "")),
        "rounds": int(getattr(pipeline_result, "rounds", 0)),
        "record_counts": dict(
            getattr(pipeline_result, "record_counts", {}) or {}
        ),
        "evidence_refs": dict(
            getattr(pipeline_result, "evidence_refs", {}) or {}
        ),
        "blockers": optimizer_blockers,
        "warnings": list(getattr(pipeline_result, "warnings", []) or []),
        "acceptance_decision": dict(acceptance_decision),
        "selected_candidate_ids": list(
            getattr(pipeline_result, "selected_candidate_ids", []) or []
        ),
        "stop_reason": str(
            getattr(pipeline_result, "stop_reason", "")
        ),
        # Mirror :func:`metacrucible.__main__.cmd_optimize`'s
        # BLOCKED payload shape so downstream consumers see the
        # same ``allow_dirty_unrelated`` field on the
        # synthesize-side BLOCKED records; ``confirm_resume`` is
        # recorded for symmetry so the dispatcher-level flag
        # reach is observable end-to-end (Issue #41 F4 global
        # review cross-task integration gap).
        "allow_dirty_unrelated": allow_dirty_unrelated,
        "confirm_resume": confirm_resume,
    }
    if blocked_bundle_refs:
        payload["evidence_refs"].update(blocked_bundle_refs)
    storage.append_history(
        {
            "event": SYNTHESIS_HISTORY_FINISHED,
            "outcome": outcome,
            "stop_reason": payload["stop_reason"],
            "status": status,
            "run_id": payload["run_id"],
            "rounds": payload["rounds"],
            "created_at": now(),
        }
    )
    emit(payload)
    return exit_code


def run_synthesize_command(
    args: Any,
    *,
    emit: Callable[[dict[str, Any]], None],
    now: Callable[[], str],
    replay: str | None = None,
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

    Order of operations (Task 3):

      1. Resolve ``--output`` to an absolute path.
      2. If the path already exists, attempt to load it as
         a synthesis workspace via
         :func:`load_synthesis_workspace`. A successful
         load dispatches the resume branch
         (:func:`_synthesize_resume_branch`); a failed load
         (no envelope, or ``envelope.source !=
         "synthesize"``) emits a BLOCKED payload carrying
         :data:`SYNTHESIZE_OUTPUT_EXISTS_BLOCKER` and
         returns :data:`EXIT_BLOCKED`. The existing
         Task 2 contract for non-synthesis paths is
         preserved verbatim.
      3. If the path does not exist, resolve the input
         pair (caller-supplied positional or ``--from``).
         Refusal → BLOCKED payload, ``EXIT_BLOCKED``.
      4. Create the workspace (draft source, envelope,
         state, baseline mapping, benchmark.jsonl,
         history). On success → OK payload with
         ``outcome='draft_pending_review'`` and
         ``EXIT_OK``.
    """
    output = Path(args.output)
    if not output.is_absolute():
        output = output.resolve()

    # Resume branch: ``output`` already exists. If it is a
    # well-formed synthesis workspace, hand it to the
    # resume dispatcher; if not, refuse to recurse into it
    # with the same blocker id the create path uses for an
    # existing non-workspace path.
    if output.exists():
        loaded = load_synthesis_workspace(output)
        if loaded["blockers"]:
            payload = {
                "status": "BLOCKED",
                "outcome": "blocked",
                "workspace": str(output),
                "generated_case_ids": [],
                "blockers": loaded["blockers"],
            }
            emit(payload)
            return EXIT_BLOCKED
        return _synthesize_resume_branch(
            args, loaded, emit=emit, now=now, replay=replay
        )

    # Create branch (Task 2): the path does not exist.
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
