"""Tests for Issue #49 ``metacrucible cleanup`` subcommand.

Pins the support-command contract (ADR 0030 / 0035):

  - ``metacrucible cleanup <raw|cache> [--json] [--retention-days N]
    [--home PATH]`` parses via the central
    :func:`metacrucible.__main__._build_parser` and exposes the
    positional ``target`` plus three optional flags.
  - ``main(['cleanup', <target>, ...])`` dispatches to
    :func:`metacrucible.__main__.cmd_cleanup` and returns
    :data:`metacrucible.exit_codes.EXIT_OK` for successful prunes
    (including empty prunes).
  - Default ``--retention-days`` is :data:`metacrucible.storage.DEFAULT_RAW_RETENTION_DAYS`
    (30) for the ``raw`` target. The ``cache`` target ignores
    ``--retention-days`` and reports ``retention_days == 0``.
  - Raw prune keeps ``receipt.json``, ``summary.json``, and
    ``trajectory-digest.json``; the raw file is removed; a
    ``cleanup.json`` audit record is written next to the durable
    artifacts.
  - Cache prune removes every cache entry and appends a record to
    ``cache/cleanup.jsonl``.
  - ``cmd_cleanup`` never writes a BLOCKED evidence bundle
    (ADR 0035): a HOME-unset error path does not create
    ``$HOME/.metacrucible/`` on disk, and the source body has no
    reference to :func:`metacrucible.blocked_bundles.write_blocked_bundle`.
  - Negative ``--retention-days`` returns
    :data:`metacrucible.exit_codes.EXIT_USER_ERROR` with a clean
    ``metacrucible:`` line on ``stderr``.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import time
from pathlib import Path

import pytest

from metacrucible import __main__ as _main_mod
from metacrucible.__main__ import (
    _build_parser,
    cmd_cleanup,
    main,
)
from metacrucible.exit_codes import (
    EXIT_OK,
    EXIT_USER_ERROR,
)
from metacrucible.storage import (
    DEFAULT_RAW_RETENTION_DAYS,
    GLOBAL_DIR_NAME,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _age_file_to_past_retention(path: Path, days: int) -> None:
    """Set ``path``'s mtime to ``now - days * 86400`` so prune fires."""
    old = time.time() - (days * 86400)
    os.utime(path, (old, old))


def _isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Pin ``HOME`` to ``tmp_path / 'home'`` so user-global writes
    land in the temp dir instead of the developer's real
    ``~/.metacrucible/``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _build_cache_identity():
    """Build a real ``CacheIdentity`` for cache test setup."""
    from metacrucible.storage import CacheIdentity

    return CacheIdentity(
        artifact_sha="a" * 64,
        executable_case_sha="b" * 64,
        harness_sha="c" * 64,
        adapter_version="claude-code/0.4.1",
        model_identities={"control": "anthropic/claude-opus-4"},
        execution_boundary_id="eb-001",
    )


# --------------------------------------------------------------------------- #
# Parser surface                                                              #
# --------------------------------------------------------------------------- #


def test_cleanup_parser_accepts_raw_target_and_json(tmp_path: Path) -> None:
    """``cleanup raw --json`` parses with command, target, and json flag."""
    args = _build_parser().parse_args(["cleanup", "raw", "--json"])

    assert args.command == "cleanup"
    assert args.target == "raw"
    assert args.json is True
    # The default retention surface must be exposed even when --json is set.
    assert args.retention_days == DEFAULT_RAW_RETENTION_DAYS
    assert args.home is None


def test_cleanup_parser_accepts_cache_target(tmp_path: Path) -> None:
    """``cleanup cache`` parses with target=cache and json=False."""
    args = _build_parser().parse_args(["cleanup", "cache"])

    assert args.command == "cleanup"
    assert args.target == "cache"
    assert args.json is False
    assert args.home is None


def test_cleanup_parser_default_retention_days_is_storage_default(
    tmp_path: Path,
) -> None:
    """Default ``--retention-days`` mirrors ``DEFAULT_RAW_RETENTION_DAYS``."""
    args = _build_parser().parse_args(["cleanup", "raw"])

    assert args.retention_days == DEFAULT_RAW_RETENTION_DAYS
    # The constant is the live source of truth; pin the literal value too
    # so a future silent change to the default is caught.
    assert args.retention_days == 30


def test_cleanup_parser_accepts_home_override(tmp_path: Path) -> None:
    """``--home PATH`` is captured as a string and overrides $HOME at runtime."""
    fake_home = tmp_path / "explicit-home"
    args = _build_parser().parse_args(
        ["cleanup", "raw", "--home", str(fake_home)]
    )

    assert args.command == "cleanup"
    assert args.target == "raw"
    assert args.home == str(fake_home)


def test_cleanup_parser_rejects_unknown_target(tmp_path: Path) -> None:
    """An unknown target must fail argparse with a usage error."""
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["cleanup", "bogus"])
    # Argparse's usage-error code is 2; main() maps it to EXIT_USER_ERROR
    # (1). The parser itself raises SystemExit(2) directly.
    assert exc_info.value.code == 2


def test_cleanup_parser_accepts_explicit_retention_days(tmp_path: Path) -> None:
    """``--retention-days N`` parses as int."""
    args = _build_parser().parse_args(
        ["cleanup", "raw", "--retention-days", "7"]
    )

    assert args.retention_days == 7


# --------------------------------------------------------------------------- #
# main() dispatch                                                             #
# --------------------------------------------------------------------------- #


def test_cleanup_dispatch_routes_to_cmd_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main(['cleanup', 'raw'])`` reaches ``cmd_cleanup`` and returns
    the fake's return code.
    """
    calls: list[argparse.Namespace] = []

    def fake_cleanup(args: argparse.Namespace) -> int:
        calls.append(args)
        return EXIT_OK

    monkeypatch.setattr(_main_mod, "cmd_cleanup", fake_cleanup)

    rc = main(["cleanup", "raw"])

    assert rc == EXIT_OK
    assert len(calls) == 1, (
        f"cmd_cleanup must be called exactly once; got {len(calls)} calls"
    )
    assert calls[0].command == "cleanup"
    assert calls[0].target == "raw"


# --------------------------------------------------------------------------- #
# Raw cleanup: keeps durable artifacts, removes old raw                       #
# --------------------------------------------------------------------------- #


def test_cleanup_raw_prunes_old_evidence_keeps_durable_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Raw prune must remove aged raw evidence while preserving receipt,
    summary, and trajectory-digest (ADR 0030). The JSON payload must
    report target, retention, and removed count.
    """
    home = _isolated_home(tmp_path, monkeypatch)
    from metacrucible.storage import UserGlobalStorage

    store = UserGlobalStorage()
    store.write_receipt("run-001", {"run_id": "run-001", "status": "PASS"})
    store.write_summary(
        "run-001", {"run_id": "run-001", "counts": {"pass": 1}}
    )
    store.write_trajectory_digest(
        "run-001",
        {
            "run_id": "run-001",
            "steps": [{"index": 0, "text": "hello"}],
        },
    )
    raw_path = store.write_raw_evidence(
        "run-001", "transcript.jsonl", "raw bytes"
    )
    # Push the raw file's mtime past the 30-day cutoff.
    _age_file_to_past_retention(raw_path, days=60)

    rc = main(["cleanup", "raw", "--json"])
    captured = capsys.readouterr()

    assert rc == EXIT_OK

    payload = json.loads(captured.out)
    assert payload["target"] == "raw"
    assert payload["retention_days"] == 30
    assert payload["removed_count"] >= 1
    assert any("transcript.jsonl" in p for p in payload["removed_paths"])

    bundle = home / GLOBAL_DIR_NAME / "evidence" / "run-001"
    assert (bundle / "receipt.json").is_file(), (
        "raw prune must NOT delete receipt.json (ADR 0030)"
    )
    assert (bundle / "summary.json").is_file(), (
        "raw prune must NOT delete summary.json (ADR 0030)"
    )
    assert (bundle / "trajectory-digest.json").is_file(), (
        "raw prune must NOT delete trajectory-digest.json (ADR 0030)"
    )
    assert (bundle / "cleanup.json").is_file(), (
        "raw prune must write a per-bundle cleanup.json audit record"
    )
    assert not (bundle / "raw").exists(), (
        "raw prune must remove the now-empty raw/ directory"
    )


# --------------------------------------------------------------------------- #
# Cache cleanup: removes entries, appends log                                 #
# --------------------------------------------------------------------------- #


def test_cleanup_cache_prunes_entries_and_appends_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cache prune must remove every entry, append ``cache/cleanup.jsonl``,
    and report ``retention_days == 0`` in the JSON payload.
    """
    home = _isolated_home(tmp_path, monkeypatch)
    from metacrucible.storage import UserGlobalStorage

    store = UserGlobalStorage()
    store.cache_put(_build_cache_identity(), {"result": "PASS", "score": 0.91})

    cache_dir = home / GLOBAL_DIR_NAME / "cache"
    assert any(cache_dir.glob("*.json")), (
        "precondition: setup must place a cache entry on disk"
    )

    rc = main(["cleanup", "cache", "--json"])
    captured = capsys.readouterr()

    assert rc == EXIT_OK
    payload = json.loads(captured.out)
    assert payload["target"] == "cache"
    assert payload["retention_days"] == 0
    assert payload["removed_count"] >= 1
    assert payload["removed_paths"], "cache prune must list removed paths"

    # The cache entry must be gone.
    assert not any(cache_dir.glob("*.json")), (
        f"cache prune must remove every *.json in {cache_dir}"
    )

    # The append-only log must contain a record for this pass.
    log_path = cache_dir / "cleanup.jsonl"
    assert log_path.is_file(), (
        f"cache prune must append to {log_path.relative_to(home)}"
    )
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln]
    assert lines, "cache cleanup log must contain at least one record"
    last = json.loads(lines[-1])
    assert last["schema_version"] == 1
    assert last["removed_count"] >= 1
    assert "pruned_at" in last


# --------------------------------------------------------------------------- #
# Empty prune                                                                 #
# --------------------------------------------------------------------------- #


def test_cleanup_raw_empty_prune_returns_exit_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raw prune with nothing to remove must return EXIT_OK with
    ``removed_count == 0`` and an empty ``removed_paths`` list.
    """
    _isolated_home(tmp_path, monkeypatch)

    rc = main(["cleanup", "raw", "--json"])
    captured = capsys.readouterr()

    assert rc == EXIT_OK
    payload = json.loads(captured.out)
    assert payload["target"] == "raw"
    assert payload["retention_days"] == 30
    assert payload["removed_count"] == 0
    assert payload["removed_paths"] == []


# --------------------------------------------------------------------------- #
# Human output                                                                #
# --------------------------------------------------------------------------- #


def test_cleanup_human_output_includes_stable_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human (non-JSON) output must include the stable keys rendered by
    :func:`metacrucible.__main__._emit`.
    """
    _isolated_home(tmp_path, monkeypatch)

    rc = main(["cleanup", "raw"])
    captured = capsys.readouterr()

    assert rc == EXIT_OK
    assert "target: raw" in captured.out
    assert "removed_count: 0" in captured.out
    assert "retention_days: 30" in captured.out


# --------------------------------------------------------------------------- #
# HOME / argument error handling                                              #
# --------------------------------------------------------------------------- #


def test_cleanup_home_unset_returns_user_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``cleanup`` with no ``--home`` and no ``$HOME`` set must return
    ``EXIT_USER_ERROR`` and a clean ``metacrucible:`` line on stderr
    that names the storage precondition.
    """
    monkeypatch.delenv("HOME", raising=False)

    rc = main(["cleanup", "raw"])
    captured = capsys.readouterr()

    assert rc == EXIT_USER_ERROR
    assert captured.err.startswith("metacrucible: "), (
        f"stderr must start with the stable 'metacrucible: ' prefix; "
        f"got: {captured.err!r}"
    )
    assert "HOME" in captured.err
    assert captured.out == "", (
        "error path must not emit a payload to stdout"
    )

    # And no evidence dir was created in the (non-existent) HOME tree.
    # ``tmp_path`` is the only writable location we ever touched.
    assert list(tmp_path.rglob("evidence")) == [], (
        "error path must not write a BLOCKED evidence bundle anywhere on disk"
    )


def test_cleanup_home_override_creates_root_under_provided_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--home <path>`` must create ``<path>/.metacrucible/`` even when
    ``$HOME`` is unset, and return EXIT_OK.
    """
    monkeypatch.delenv("HOME", raising=False)
    explicit = tmp_path / "explicit-home"

    rc = main(["cleanup", "raw", "--home", str(explicit), "--json"])
    captured = capsys.readouterr()

    assert rc == EXIT_OK
    assert (explicit / GLOBAL_DIR_NAME).is_dir(), (
        f"--home override must create {explicit / ('.' + GLOBAL_DIR_NAME)}"
    )
    payload = json.loads(captured.out)
    assert payload["target"] == "raw"
    assert payload["retention_days"] == 30


def test_cleanup_negative_retention_returns_user_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--retention-days -1`` must return ``EXIT_USER_ERROR`` with a
    clean ``metacrucible:`` stderr line that names the bad flag.
    """
    _isolated_home(tmp_path, monkeypatch)

    rc = main(["cleanup", "raw", "--retention-days", "-1"])
    captured = capsys.readouterr()

    assert rc == EXIT_USER_ERROR
    assert captured.err.startswith("metacrucible: "), (
        f"stderr must start with the stable 'metacrucible: ' prefix; "
        f"got: {captured.err!r}"
    )
    assert "--retention-days" in captured.err
    assert captured.out == "", (
        "error path must not emit a payload to stdout"
    )


# --------------------------------------------------------------------------- #
# BLOCKED-bundle contract (ADR 0035)                                           #
# --------------------------------------------------------------------------- #


def test_cleanup_never_references_write_blocked_bundle() -> None:
    """Static source guarantee: ``cmd_cleanup`` must never import or
    call :func:`metacrucible.blocked_bundles.write_blocked_bundle`.

    Cleanup is a support command (ADR 0035) and stays free of BLOCKED
    evidence bundles.
    """
    source = inspect.getsource(cmd_cleanup)
    assert "write_blocked_bundle" not in source, (
        "cmd_cleanup must not call write_blocked_bundle; the cleanup "
        "command is contractually a support command (ADR 0035) and "
        "must not emit BLOCKED evidence bundles"
    )


def test_cleanup_error_paths_never_create_evidence_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every cleanup error path that returns ``EXIT_USER_ERROR`` must
    not create any evidence bundle on disk. This pins the runtime
    half of the ADR 0035 contract.
    """
    # Path 1: HOME unset with no --home override.
    monkeypatch.delenv("HOME", raising=False)
    rc = main(["cleanup", "raw"])
    assert rc == EXIT_USER_ERROR
    assert list(tmp_path.rglob(".metacrucible")) == [], (
        "HOME-unset error path must not create any .metacrucible tree"
    )

    # Path 2: negative retention with isolated HOME.
    home = _isolated_home(tmp_path, monkeypatch)
    rc = main(["cleanup", "raw", "--retention-days", "-1"])
    assert rc == EXIT_USER_ERROR
    assert not (home / GLOBAL_DIR_NAME / "evidence").exists(), (
        f"negative-retention error path must not create "
        f"{home / ('.' + GLOBAL_DIR_NAME) / 'evidence'}"
    )
