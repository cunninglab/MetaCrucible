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
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
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

#: Exit code returned by ``init --check`` when validation surfaces
#: at least one blocker. Distinct from argparse's exit 2 so callers
#: can branch on the semantic outcome.
CHECK_BLOCKED_EXIT_CODE = 2


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
    """Write ``payload`` to stdout in JSON or human form."""
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
        else:
            print(f"{key}: {value}")

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
        return 0 if result["ok"] else CHECK_BLOCKED_EXIT_CODE
    paths = _create_workspace(workspace)
    payload = {
        "workspace": str(paths["workspace"]),
        "envelope_path": str(paths["envelope_path"]),
        "state_path": str(paths["state_path"]),
        "benchmark_path": str(paths["benchmark_path"]),
        "created": paths["created"],
    }
    _emit(payload, as_json=args.json)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``metacrucible`` console script.

    Returns the process exit code. Argparse's ``--help`` / ``--version``
    actions raise ``SystemExit`` to terminate; we catch those here and
    translate to a clean integer return value so the console-script
    wrapper and unit tests get a stable contract.
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
        return 0
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)
    if getattr(args, "command", None) == "init":
        return cmd_init(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
