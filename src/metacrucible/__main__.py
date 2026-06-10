"""Console entrypoint for the ``metacrucible`` command.

Exposes :func:`main` as the ``metacrucible`` console script (declared
in ``pyproject.toml`` under ``[project.scripts]``) and is also invokable
as ``python -m metacrucible``. This module owns the CLI surface:

  - the skeleton flags (``--help`` / ``--version``) from Issue #3, and
  - the ``init`` subcommand from Issue #6, which creates the
    per-artifact ``.metacrucible/`` envelope/state plus an empty
    ``benchmark.jsonl`` container at the workspace root, and which
    exposes ``--check`` for a post-init validation pass that surfaces
    the ``missing-reviewed-case`` blocker (ADR 0029) on an empty
    benchmark.

The remaining MVP subcommands from ADR 0035 (``review``, ``bootstrap``,
``optimize``, ``synthesize``, ``inspect``, ``baseline create``,
``evaluate``) land in later waves per ``docs/roadmap.md``.

Exit codes
----------

The exact integer returned by :func:`main` is pinned by
:mod:`metacrucible.exit_codes`` so scripts and CI can branch on it
without re-deriving the matrix:

  - ``0`` — success.
  - ``1`` — argparse usage error (unknown subcommand, missing
    required positional/flag, or invalid argument).
  - ``2`` — semantic blocker (the command ran, but a precondition
    prevented the requested outcome).
  - ``3`` — uncaught exception past the command dispatcher; an
    English error message is written to stderr first.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .benchmark import SPLIT_EVAL, SPLIT_HELD_OUT
from .exit_codes import (
    EXIT_BLOCKED,
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_USER_ERROR,
)
from .promote import promote_case
from .storage import RepositoryStorage

__all__ = ["main"]

#: Name of the benchmark container at the workspace root. ADR 0025
#: pins the empty benchmark as a valid container; the loader
#: (Issue #7) reads this path by convention.
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Stable blocker id emitted by ``init --check`` when the benchmark
#: has no reviewed cases. Pinned by ADR 0029's "fixed small
#: machine-stable set" of invalid benchmark blocker codes.
MISSING_REVIEWED_CASE_BLOCKER = "missing-reviewed-case"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metacrucible",
        description=(
            "MetaCrucible: a workbench for improving portable agent "
            "capabilities through repeatable optimization, evaluation, "
            "and review loops."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"metacrucible {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser(
        "init",
        help=(
            "initialize an artifact workspace envelope and empty "
            "benchmark container (ADR 0035)"
        ),
    )
    init_parser.add_argument(
        "workspace",
        help="path to the artifact workspace (created if missing)",
    )
    init_parser.add_argument(
        "--check",
        action="store_true",
        help="validate an existing workspace without creating files",
    )
    init_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    init_parser.add_argument(
        "--no-isolation",
        action="store_true",
        help=(
            "skip copy-on-write workspace masking (Issue #13); "
            "requires --confirm-no-isolation and a TTY, or the "
            "METACRUCIBLE_ALLOW_NO_ISOLATION=1 env-var override"
        ),
    )
    init_parser.add_argument(
        "--confirm-no-isolation",
        action="store_true",
        help=(
            "explicit human confirmation that workspace masking is "
            "intentionally being disabled (Issue #13 AC3)"
        ),
    )
    promote_parser = subparsers.add_parser(
        "promote",
        help="promote a generated benchmark case after human review",
    )
    promote_parser.add_argument(
        "workspace",
        help="path to the artifact workspace",
    )
    promote_parser.add_argument(
        "--case-id",
        required=True,
        help="case_id of the generated benchmark case to promote",
    )
    promote_parser.add_argument(
        "--split",
        choices=[SPLIT_EVAL, SPLIT_HELD_OUT],
        required=True,
        help="reviewed split to assign to the promoted case",
    )
    promote_parser.add_argument(
        "--reviewed-by",
        required=True,
        help="human reviewer identity to record on the case",
    )
    promote_parser.add_argument(
        "--review-note",
        default="",
        help="human review note to record on the case",
    )
    promote_parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite benchmark.jsonl; default is dry-run",
    )
    promote_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a parseable JSON object on stdout",
    )
    return parser


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _default_envelope(workspace: Path) -> dict[str, Any]:
    return {
        "artifact_workspace": str(workspace),
        "created_at": _now_iso(),
    }


def _default_state() -> dict[str, Any]:
    return {
        "current_best_revision": None,
        "last_run_id": None,
    }


def _default_metadata_record() -> dict[str, Any]:
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
        "created_at": _now_iso(),
    }


def _read_benchmark_records(benchmark: Path) -> list[dict[str, Any]]:
    """Return all parseable JSON object records from a JSONL file.

    Lines that fail to parse or that do not decode as a JSON object
    are skipped: ``init --check`` is a non-destructive validator and
    must not crash on a malformed line.
    """
    if not benchmark.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in benchmark.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _reviewed_case_count(records: list[dict[str, Any]]) -> int:
    """Count case records that have been reviewed.

    A case record is any record whose ``record_type`` is one of
    ``case`` / ``case_eval`` / ``case_held_out`` (the discriminator
    set ADR 0029 reserves for benchmark case rows). A record counts
    as "reviewed" when ``reviewed`` is ``True`` or ``status`` is
    ``"reviewed"`` — the two machine-stable shapes the rest of the
    pipeline emits.
    """
    count = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("record_type")
        if rtype not in {"case", "case_eval", "case_held_out"}:
            continue
        if rec.get("reviewed") is True or rec.get("status") == "reviewed":
            count += 1
    return count


def _create_workspace(workspace: Path) -> dict[str, Any]:
    """Create envelope/state/benchmark if absent; return path map.

    Idempotent by design: existing files are left untouched so a
    second ``init`` on the same workspace does not silently mutate
    the envelope (ADR 0016 + ADR 0020).
    """
    storage = RepositoryStorage(workspace)
    created = False
    if not storage.envelope_path.is_file():
        storage.write_envelope(_default_envelope(workspace))
        created = True
    if not storage.state_path.is_file():
        storage.write_state(_default_state())
        created = True
    benchmark = workspace / BENCHMARK_FILE_NAME
    if not benchmark.is_file():
        benchmark.write_text(
            json.dumps(_default_metadata_record(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        created = True
    return {
        "workspace": workspace,
        "envelope_path": storage.envelope_path,
        "state_path": storage.state_path,
        "benchmark_path": benchmark,
        "created": created,
    }


def _check_workspace(workspace: Path) -> dict[str, Any]:
    """Validate a workspace; return blockers and the path map.

    ``RepositoryStorage`` is constructed so the path map reflects
    where the envelope/state *would* live; the validator does not
    write any files itself.
    """
    storage = RepositoryStorage(workspace)
    benchmark = workspace / BENCHMARK_FILE_NAME
    records = _read_benchmark_records(benchmark)
    blockers: list[dict[str, Any]] = []
    if _reviewed_case_count(records) == 0:
        blockers.append(
            {
                "id": MISSING_REVIEWED_CASE_BLOCKER,
                "message": (
                    "benchmark has no reviewed cases; "
                    "an empty benchmark is a valid container but "
                    "cannot be evaluated (ADR 0025, ADR 0029)"
                ),
            }
        )
    return {
        "workspace": workspace,
        "envelope_path": storage.envelope_path,
        "state_path": storage.state_path,
        "benchmark_path": benchmark,
        "ok": not blockers,
        "blockers": blockers,
    }


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    """Write ``payload`` to stdout in JSON or human form.

    The human form is a key/value summary that keeps the CLI's
    own prose English-only (Issue #27 task 27.4). User-controlled
    freeform text (currently ``review_note`` from ``promote``) is
    masked in the human surface so a multilingual review note
    never contaminates the English prose contract. The full
    value is preserved by ``--json`` for callers that need it.
    """
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key in sorted(payload.keys()):
        value = payload[key]
        if key == "blockers" and isinstance(value, list):
            if value:
                for blocker in value:
                    if isinstance(blocker, dict):
                        bid = blocker.get("id", "?")
                        msg = blocker.get("message", "")
                        print(f"- {bid}: {msg}")
                    else:
                        print(f"- {blocker}")
            else:
                print(f"{key}: (none)")
        elif key == "review_note":
            # User-controlled freeform text; the operator does not
            # need it echoed back as part of the English prose
            # surface, and a non-ASCII note would otherwise
            # contaminate the human-only contract. Use ``--json``
            # to retrieve the verbatim value.
            if isinstance(value, str) and value:
                print(
                    f"{key}: <{len(value)} chars, hidden in "
                    f"human output; use --json to view>"
                )
            else:
                print(f"{key}: (empty)")
        else:
            print(f"{key}: {value}")


def cmd_promote(args: argparse.Namespace) -> int:
    """Run the ``promote`` subcommand; return the process exit code."""
    workspace = Path(args.workspace).resolve()
    benchmark = workspace / BENCHMARK_FILE_NAME
    result = promote_case(
        benchmark,
        case_id=args.case_id,
        split=args.split,
        reviewed_by=args.reviewed_by,
        review_note=args.review_note,
        reviewed_at=_now_iso(),
        dry_run=not args.apply,
    )
    _emit(result, as_json=args.json)
    return EXIT_OK if not result["blockers"] else EXIT_BLOCKED


def cmd_init(args: argparse.Namespace) -> int:
    """Run the ``init`` subcommand; return the process exit code."""
    workspace = Path(args.workspace).resolve()
    if args.check:
        result = _check_workspace(workspace)
        payload = {
            "workspace": str(result["workspace"]),
            "envelope_path": str(result["envelope_path"]),
            "state_path": str(result["state_path"]),
            "benchmark_path": str(result["benchmark_path"]),
            "ok": result["ok"],
            "blockers": result["blockers"],
        }
        _emit(payload, as_json=args.json)
        return EXIT_OK if result["ok"] else EXIT_BLOCKED
    # ``--no-isolation`` gate (Issue #13 AC3+AC4). The flag is a
    # safety escape hatch for callers that intentionally want to
    # skip copy-on-write masking; the gate refuses the call unless
    # the caller passed ``--confirm-no-isolation`` AND either stdin
    # is a TTY or the explicit env-var override is set. The
    # validation lives in :mod:`metacrucible.workspace_isolation`.
    if getattr(args, "no_isolation", False):
        from .workspace_isolation import validate_no_isolation

        interactive = sys.stdin.isatty()
        gate = validate_no_isolation(
            confirmed=bool(getattr(args, "confirm_no_isolation", False)),
            interactive=interactive,
        )
        if not gate["ok"]:
            payload = {
                "workspace": str(workspace),
                "ok": gate["ok"],
                "blockers": gate["blockers"],
            }
            _emit(payload, as_json=args.json)
            return EXIT_BLOCKED
    paths = _create_workspace(workspace)
    # Boundary report (ADR 0031, Issue #13 AC1). When
    # ``--no-isolation`` is set the gate above has already passed,
    # so masking is intentionally skipped and the report is
    # recorded as ``masking: "skipped"`` so a reviewer can tell the
    # silent-skip from a successful plan.
    boundary_report: dict[str, Any]
    if getattr(args, "no_isolation", False):
        boundary_report = {
            "ok": True,
            "blockers": [],
            "masking": "skipped",
        }
    else:
        from .workspace_isolation import plan_workspace_mask

        boundary_report = plan_workspace_mask(workspace)
    payload = {
        "workspace": str(paths["workspace"]),
        "envelope_path": str(paths["envelope_path"]),
        "state_path": str(paths["state_path"]),
        "benchmark_path": str(paths["benchmark_path"]),
        "created": paths["created"],
        "boundary_report": boundary_report,
    }
    _emit(payload, as_json=args.json)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``metacrucible`` console script.

    Returns the process exit code, pinned by
    :mod:`metacrucible.exit_codes`. Argparse's ``--help`` /
    ``--version`` actions raise ``SystemExit`` to terminate; we
    catch those here and translate to a clean integer return value
    so the console-script wrapper and unit tests get a stable
    contract.

    Any uncaught exception past the command dispatcher is mapped
    to ``EXIT_INTERNAL_ERROR`` with a one-line English message on
    stderr; the caller treats this as a bug report.
    """
    parser = _build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        # Bare invocation: print a short banner so the CLI is useful
        # out of the box even before the MVP subcommands land.
        print(f"metacrucible {__version__}")
        print(
            "A workbench for improving portable agent capabilities. "
            "Run 'metacrucible --help' for usage."
        )
        return EXIT_OK
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        # Argparse raises SystemExit on --help / --version (code 0
        # or None) and on usage errors (code 2). Map success to
        # EXIT_OK; map any nonzero (i.e. usage error) to
        # EXIT_USER_ERROR so the contract stays distinct from the
        # blocked (2) and internal (3) codes.
        code = exc.code
        if code is None or int(code) == 0:
            return EXIT_OK
        return EXIT_USER_ERROR
    try:
        if getattr(args, "command", None) == "init":
            return cmd_init(args)
        if getattr(args, "command", None) == "promote":
            return cmd_promote(args)
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - exit-code firewall
        # Catch-all so an uncaught command-handler bug still
        # returns a stable code; the English message is the
        # diagnostic the caller reads.
        print(
            f"metacrucible: internal error: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
