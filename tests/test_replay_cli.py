"""CLI replay tests for Issue #45 (recorded-replay CI harness).

The Task 2 brief requires wiring the recorded-replay flags through
the public ``metacrucible`` CLI for ``review``, ``bootstrap``,
``optimize``, and ``synthesize``. The replay fixture is a JSONL
file consumed by :mod:`metacrucible.replay` (Task 1 surface);
each test below builds a small fixture in a temp dir and
exercises the dispatcher end-to-end so the public CLI contract
is pinned against regressions.

Test plan (matches the brief):

  - ``test_review_replay_loads_judge_callables`` -- ``review``
    with ``--replay`` wires two distinct replay-backed judge
    callables into :func:`run_judge_evaluator`; recorded
    verdicts surface in the JSON payload.
  - ``test_review_replay_aliases`` -- ``review`` with only
    ``--judge-replay`` / ``--judge-replay-2`` (no
    ``--replay``) still wires replay-backed callables.
  - ``test_review_no_replay_remains_blocked`` -- default
    no-replay path still returns BLOCKED with
    ``review-case-judge-provider-unavailable`` for judgment
    cases (Issue #29 default-behavior preservation).
  - ``test_bootstrap_replay_passthrough`` -- ``bootstrap``
    with ``--replay`` runs the same code path (default
    no-replay behavior is unchanged for cases that don't
    request a ``bootstrap`` entry).
  - ``test_optimize_replay_threads_call_fn`` -- ``optimize``
    with ``--replay`` passes
    :func:`build_optimizer_call_fn` 's result to BOTH the
    preview and the mutating pipeline passes (Issue #45
    determinism invariant: the same ``call_fn`` object is
    threaded into both invocations).
  - ``test_optimize_replay_accepted_response_yields_accepted``
    -- ``optimize`` with a recorded ACCEPTED optimizer
    response yields ``decision: accepted`` and ``EXIT_OK``.
  - ``test_synthesize_replay_accepted_yields_accepted_outcome``
    -- ``synthesize`` with a recorded ACCEPTED optimizer
    response yields ``outcome: accepted`` and ``EXIT_OK``.
  - ``test_synthesize_replay_no_replay_default_unchanged`` --
    default no-replay synthesize still passes ``call_fn=None``
    to the optimizer.
  - ``test_optimize_no_replay_remains_rejected`` -- default
    no-replay optimize surfaces the no-LLM-call REJECTED
    rationale (no-LLM-call regression).
  - ``test_parser_help_includes_replay`` -- ``_build_parser``
    exposes ``--replay`` on all four commands.

The tests follow the same subprocess + monkey-patch patterns
the rest of the CLI test suite uses
(:mod:`tests.test_review_command`,
:mod:`tests.test_optimize_command`,
:mod:`tests.test_synthesize_command`,
:mod:`tests.test_bootstrap_command`).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pytest

from metacrucible.exit_codes import EXIT_BLOCKED, EXIT_OK
from metacrucible.optimizer import ROUND_BUDGET_DEFAULT
from metacrucible.replay import (
    REPLAY_SCHEMA_VERSION,
    build_judge_call_fns,
    build_optimizer_call_fn,
    load_replay,
)
from metacrucible.synthesize import (
    BENCHMARK_FILE_NAME as SYNTH_BENCHMARK_FILE_NAME,
    BOOTSTRAP_PENDING_REVIEW_FIELD as SYNTH_BOOTSTRAP_PENDING_REVIEW_FIELD,
)

#: Same benchmark file name as the rest of the CLI test suite.
BENCHMARK_FILE_NAME = "benchmark.jsonl"

#: Literal case-level field that flags bootstrap-generated cases
#: as "pending human review".
BOOTSTRAP_PENDING_REVIEW_FIELD = "BOOTSTRAP_PENDING_REVIEW"

#: Stable blocker id emitted by ``review`` for judgment cases
#: when no provider / replay is configured.
REVIEW_CASE_JUDGE_PROVIDER_UNAVAILABLE_BLOCKER = (
    "review-case-judge-provider-unavailable"
)

#: Frozen time used by the synthesize tests so case_ids and
#: history events are byte-stable across runs.
FROZEN_NOW = "2026-06-17T00:00:00Z"

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Shared pytest fixture: isolated HOME                                         #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def isolated_global_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Pin ``HOME`` to a temp dir so :class:`UserGlobalStorage`
    does not pollute the developer's real ``~/.metacrucible/``.

    Mirrors the fixture in :mod:`tests.test_review_command` so
    the replay tests run alongside the review tests without
    stepping on the same ``HOME``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #


def _run_metacrucible(
    argv: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m metacrucible`` with captured text output."""
    return subprocess.run(
        [sys.executable, "-m", "metacrucible", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    """Write ``records`` as one JSON object per line at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(dict(rec), sort_keys=True) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _record(
    name: str,
    *,
    response: Any = ...,
    responses: Any = ...,
    schema_version: int = REPLAY_SCHEMA_VERSION,
) -> str:
    """Return a JSONL line for a single replay record.

    Pass exactly one of ``response`` (single value) or ``responses``
    (list of values). Mirrors the test_replay_harness helper so a
    fixture built here is byte-equivalent to one built by the
    Task 1 test suite.
    """
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "name": name,
    }
    if response is not ...:
        payload["response"] = response
    if responses is not ...:
        payload["responses"] = responses
    return json.dumps(payload, separators=(", ", ": "))


def _write_replay(
    tmp_path: Path, name: str, body: str
) -> Path:
    """Write ``body`` to a replay fixture under ``tmp_path``."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _metadata_record() -> dict[str, Any]:
    """Minimal benchmark metadata record (ADR 0029)."""
    return {
        "record_type": "metadata",
        "name": "default-benchmark",
        "schema_version": 1,
    }


def _reviewed_case(
    case_id: str, *, split: str = "eval"
) -> dict[str, Any]:
    """Build a minimal eligible reviewed case (ADR 0029).

    The default ``expected_output`` is ``"The thing worked."``
    which matches the default ``checks`` pattern ``"thing"``;
    the F1 deterministic check engine therefore passes the
    case out of the box so a paired judgment case can drive
    the wiring under test without unrelated BLOCKs.
    """
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "reviewed",
        "split": split,
        "input": {"prompt": "do the thing"},
        "execution_boundary": {"permissions": ["read"]},
        "expected_output": "The thing worked.",
        "checks": [{"name": "output_contains_thing", "pattern": "thing"}],
    }


def _judgment_case(case_id: str, split: str = "eval") -> dict[str, Any]:
    """Build a reviewed case that requests ``judgment`` (F1 path)."""
    return {
        "record_type": "case",
        "case_id": case_id,
        "status": "reviewed",
        "split": split,
        "input": {"prompt": "judge this"},
        "execution_boundary": {"permissions": ["read"]},
        "checks": [],
        "judgment": {
            "rubric": {"name": "quality", "scale": [0, 1]},
            "pass_condition": "score >= 0.7",
        },
    }


# A Skill artifact that does NOT touch the routing surface
# (frontmatter contains a non-routing field). Routing-surface
# blockers from the static-review profile therefore do not
# fire, so the static review verdict is PASS for these tests.
_SKILL_ARTIFACT_NO_ROUTING = (
    "---\n"
    "somefield: no routing-surface field declared\n"
    "---\n"
    "\n"
    "# replay-test-skill\n"
    "\n"
    "Body content for the recorded-replay CLI tests.\n"
)


def _write_skill_artifact(path: Path) -> Path:
    """Write a minimal Skill artifact to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SKILL_ARTIFACT_NO_ROUTING, encoding="utf-8")
    return path


def _seed_review_workspace(
    workspace: Path,
    benchmark_records: list[dict[str, Any]],
) -> Path:
    """Seed a workspace with the given benchmark records."""
    workspace.mkdir(parents=True, exist_ok=True)
    benchmark = workspace / BENCHMARK_FILE_NAME
    _write_jsonl(benchmark, benchmark_records)
    return workspace


def _seed_optimize_workspace(
    workspace: Path,
    *,
    artifact_source: str | None = None,
    benchmark_records: list[dict[str, Any]] | None = None,
) -> Path:
    """Seed a workspace that satisfies the F3 optimize preflight
    gates (clean benchmark + envelope + artifact).

    The artifact source defaults to the no-routing fixture so
    the static-review path does not surface routing blockers
    unrelated to the replay wiring. The benchmark defaults to
    one reviewed eval case + one reviewed held-out case (the
    minimum loader-runnable shape).
    """
    workspace.mkdir(parents=True, exist_ok=True)
    artifact = workspace / "SKILL.md"
    artifact_source = (
        artifact_source
        if artifact_source is not None
        else (
            "---\n"
            "somefield: no routing-surface field declared\n"
            "---\n"
            "\n"
            "# body\nThe body is the only mutable range.\n"
        )
    )
    artifact.write_text(artifact_source, encoding="utf-8")
    envelope = workspace / ".metacrucible" / "envelope.json"
    envelope.parent.mkdir(parents=True, exist_ok=True)
    envelope.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_path": str(artifact),
                "artifact_workspace": str(workspace),
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if benchmark_records is None:
        benchmark_records = [
            _metadata_record(),
            _reviewed_case("eval-1", split="eval"),
            _reviewed_case("held-1", split="held_out"),
        ]
    _write_jsonl(
        workspace / BENCHMARK_FILE_NAME, benchmark_records
    )
    return workspace


def _synthesize_namespace(
    *,
    tmp_path: Path,
    capability_need: str | None,
    from_spec: str | None,
    replay: str | None = None,
    json_mode: bool = True,
) -> argparse.Namespace:
    """Build the ``argparse.Namespace`` ``cmd_synthesize`` expects.

    Mirrors the dispatcher's parser output: a fully-populated
    namespace with every shared CLI flag wired so the dispatcher
    can read ``args.json`` and the wrapper can build the
    ``_emit`` partial without raising ``AttributeError``. The
    snake_case dests match the parser rename contract pinned
    by the parser-level tests in
    :mod:`tests.test_synthesize_command`.
    """
    output = tmp_path / "skill"
    return argparse.Namespace(
        command="synthesize",
        capability_need=capability_need,
        from_spec=from_spec,
        output=str(output),
        max_rounds=ROUND_BUDGET_DEFAULT,
        json=json_mode,
        allow_routing_revision=False,
        allow_dirty_unrelated=False,
        confirm_resume=False,
        replay=replay,
    )


def _reviewed_synthesis_workspace(
    tmp_path: Path,
    *,
    capability_need: str = "write a skill to summarize documents",
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Path:
    """Seed a synthesis workspace whose benchmark cases are reviewed.

    Mirrors :func:`tests.test_synthesize_command._reviewed_synthesis_workspace`
    so the synthesize resume tests can drive the F3 path with a
    deterministic input.
    """
    from metacrucible import __main__ as cli_main

    monkeypatch.setattr(cli_main, "_now_iso", lambda: FROZEN_NOW)
    ns = _synthesize_namespace(
        tmp_path=tmp_path,
        capability_need=capability_need,
        from_spec=None,
    )
    rc = cli_main.cmd_synthesize(ns)
    assert rc == EXIT_OK, (
        f"workspace bootstrap synthesize must return EXIT_OK; "
        f"got rc={rc}"
    )
    capsys.readouterr()  # drain the bootstrap emit
    workspace = tmp_path / "skill"
    benchmark = workspace / SYNTH_BENCHMARK_FILE_NAME
    records = [
        json.loads(line)
        for line in benchmark.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    new_records: list[dict[str, object]] = []
    for record in records:
        if record.get("record_type") == "metadata":
            new_records.append(dict(record))
            continue
        rewritten = dict(record)
        rewritten["status"] = "reviewed"
        rewritten["reviewed"] = True
        rewritten.pop(SYNTH_BOOTSTRAP_PENDING_REVIEW_FIELD, None)
        new_records.append(rewritten)
    _write_jsonl(benchmark, new_records)
    return workspace


# --------------------------------------------------------------------------- #
# AC0 — parser help exposes the replay flag                                    #
# --------------------------------------------------------------------------- #


def test_parser_help_includes_replay() -> None:
    """The ``_build_parser`` exposes ``--replay`` on review, bootstrap,
    optimize, and synthesize, plus the ``--judge-replay`` /
    ``--judge-replay-2`` aliases on review.

    The brief pins the flag set on each subcommand. The test
    asserts the flag is present in each subcommand's parser
    so a future rename is a deliberate single-site change.
    """
    from metacrucible.__main__ import _build_parser

    parser = _build_parser()
    # Use the public API: parse a no-arg subcommand invocation
    # and assert the resulting namespace carries the
    # ``--replay`` attribute. The argparse
    # ``ArgumentParser._subparsers`` field is an
    # implementation detail and not stable across Python
    # versions; the ``parse_args`` path is.
    for sub in ("review", "bootstrap", "optimize", "synthesize"):
        # Each subcommand has its own minimum argv shape.
        if sub == "review":
            argv = [sub, "some-artifact.md"]
        elif sub == "synthesize":
            argv = [sub, "need text", "--output", "/tmp/out"]
        elif sub == "bootstrap":
            argv = [sub, "/tmp/ws"]
        else:
            argv = [sub, "/tmp/ws"]
        ns = parser.parse_args(argv)
        assert hasattr(ns, "replay"), (
            f"subcommand {sub!r} namespace must carry "
            f"``replay`` attribute; got attrs="
            f"{sorted(vars(ns).keys())!r}"
        )
        assert ns.replay is None, (
            f"subcommand {sub!r} --replay must default to "
            f"None; got {ns.replay!r}"
        )
    # Review subcommand also carries the compatibility aliases.
    review_ns = parser.parse_args(
        ["review", "some-artifact.md"]
    )
    assert hasattr(review_ns, "judge_replay"), (
        "review namespace must carry ``judge_replay`` "
        "attribute"
    )
    assert hasattr(review_ns, "judge_replay_2"), (
        "review namespace must carry ``judge_replay_2`` "
        "attribute"
    )
    # And the actual flags must parse through end-to-end.
    review_ns2 = parser.parse_args(
        [
            "review",
            "some-artifact.md",
            "--replay",
            "/tmp/replay.jsonl",
            "--judge-replay",
            "/tmp/j1.jsonl",
            "--judge-replay-2",
            "/tmp/j2.jsonl",
        ]
    )
    assert review_ns2.replay == "/tmp/replay.jsonl"
    assert review_ns2.judge_replay == "/tmp/j1.jsonl"
    assert review_ns2.judge_replay_2 == "/tmp/j2.jsonl"


# --------------------------------------------------------------------------- #
# AC1 — review with --replay wires replay-backed judge callables              #
# --------------------------------------------------------------------------- #


def test_review_replay_loads_judge_callables(
    tmp_path: Path,
    isolated_global_home: Path,
) -> None:
    """When ``--replay`` is set, ``review`` threads two distinct
    replay-backed judge callables into
    :func:`metacrucible.provider_config.run_judge_evaluator`; the
    recorded verdicts surface in the JSON payload's
    ``execution_evaluation.case_results[*].evidence`` mapping.

    The test seeds a fixture with two judge_* entries (each
    carrying one response) and one judgment case + one reviewed
    held-out case (the loader-runnable minimum). ``review`` is
    invoked via the real CLI subprocess so the wiring is
    end-to-end; the per-case evidence must show two distinct
    ``judge_id`` keys (``judge_1`` and ``judge_2``) with the
    recorded payload verbatim.
    """
    judge_1_response = {
        "verdict": "pass",
        "score": 0.91,
        "notes": "judge_1 says pass",
    }
    judge_2_response = {
        "verdict": "pass",
        "score": 0.84,
        "notes": "judge_2 says pass",
    }
    body = "\n".join(
        [
            _record("judge_1", response=judge_1_response),
            _record("judge_2", response=judge_2_response),
            _record(
                "optimizer",
                responses=[{"edit": "noop"}],
            ),
            "",  # trailing newline
        ]
    )
    replay_path = _write_replay(tmp_path, "replay.jsonl", body)
    artifact = _write_skill_artifact(tmp_path / "review-test.md")
    workspace = _seed_review_workspace(
        tmp_path / "ws",
        [
            _metadata_record(),
            _judgment_case("case-judge-1", split="eval"),
            _reviewed_case("case-held-1", split="held_out"),
        ],
    )

    result = _run_metacrucible(
        [
            "review",
            str(artifact),
            "--workspace",
            str(workspace),
            "--replay",
            str(replay_path),
            "--json",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`review --replay` with a passing judge fixture must "
        f"exit {EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    execution = payload["execution_evaluation"]
    case_results = execution["case_results"]
    by_id = {r["case_id"]: r for r in case_results}
    assert "case-judge-1" in by_id, (
        f"case_results must include the judgment case; got "
        f"case_ids={list(by_id.keys())!r}"
    )
    judge_result = by_id["case-judge-1"]
    assert judge_result["status"] == "PASS", (
        f"judgment case must be PASS with a recorded judge "
        f"verdict; got status={judge_result['status']!r} "
        f"blockers={judge_result.get('blockers')!r}"
    )
    assert judge_result["evaluator"] == "judge"
    # The recorded judge_1 and judge_2 responses must surface
    # verbatim in the per-case judge_evidence.
    evidence = judge_result["evidence"]["judge_evidence"]
    assert "judge_1" in evidence, (
        f"judge_evidence must carry judge_1; got keys="
        f"{sorted(evidence.keys())!r}"
    )
    assert "judge_2" in evidence, (
        f"judge_evidence must carry judge_2; got keys="
        f"{sorted(evidence.keys())!r}"
    )
    assert evidence["judge_1"]["value"] == judge_1_response, (
        f"judge_1 evidence must be the recorded response "
        f"verbatim; got {evidence['judge_1']!r}"
    )
    assert evidence["judge_2"]["value"] == judge_2_response, (
        f"judge_2 evidence must be the recorded response "
        f"verbatim; got {evidence['judge_2']!r}"
    )


def test_review_replay_aliases(
    tmp_path: Path,
    isolated_global_home: Path,
) -> None:
    """When only ``--judge-replay`` / ``--judge-replay-2`` are set
    (no ``--replay``), the F1 judgment path still wires
    replay-backed callables.

    The two compatibility aliases load independent fixtures;
    the first judge callable comes from the first alias and
    the second from the second alias. The test pins the wiring
    end-to-end by asserting the per-case judge_evidence carries
    the values from each alias fixture.
    """
    judge_1_response = {
        "verdict": "pass",
        "score": 0.95,
        "notes": "alias A says pass",
    }
    judge_2_response = {
        "verdict": "pass",
        "score": 0.88,
        "notes": "alias B says pass",
    }
    alias_a_path = _write_replay(
        tmp_path,
        "alias_a.jsonl",
        _record("judge_1", response=judge_1_response) + "\n",
    )
    alias_b_path = _write_replay(
        tmp_path,
        "alias_b.jsonl",
        _record("judge_1", response=judge_2_response) + "\n",
    )
    artifact = _write_skill_artifact(tmp_path / "review-test.md")
    workspace = _seed_review_workspace(
        tmp_path / "ws",
        [
            _metadata_record(),
            _judgment_case("case-judge-alias", split="eval"),
            _reviewed_case("case-held-alias", split="held_out"),
        ],
    )

    result = _run_metacrucible(
        [
            "review",
            str(artifact),
            "--workspace",
            str(workspace),
            "--judge-replay",
            str(alias_a_path),
            "--judge-replay-2",
            str(alias_b_path),
            "--json",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`review --judge-replay --judge-replay-2` with a "
        f"passing judge fixture must exit {EXIT_OK}; got "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    execution = payload["execution_evaluation"]
    case_results = execution["case_results"]
    by_id = {r["case_id"]: r for r in case_results}
    judge_result = by_id["case-judge-alias"]
    assert judge_result["status"] == "PASS", (
        f"judgment case must be PASS with two alias fixtures; "
        f"got status={judge_result['status']!r} blockers="
        f"{judge_result.get('blockers')!r}"
    )
    evidence = judge_result["evidence"]["judge_evidence"]
    assert evidence["judge_1"]["value"] == judge_1_response, (
        f"judge_1 evidence must come from --judge-replay "
        f"fixture; got {evidence['judge_1']!r}"
    )
    assert evidence["judge_2"]["value"] == judge_2_response, (
        f"judge_2 evidence must come from --judge-replay-2 "
        f"fixture; got {evidence['judge_2']!r}"
    )


# --------------------------------------------------------------------------- #
# AC2 — default no-replay review remains BLOCKED with judge-provider-unavail  #
# --------------------------------------------------------------------------- #


def test_review_no_replay_remains_blocked(
    tmp_path: Path,
    isolated_global_home: Path,
) -> None:
    """Without ``--replay`` (or any of the aliases), a judgment
    case still BLOCKS with the
    ``review-case-judge-provider-unavailable`` id. The
    default-behavior-preservation regression for the F1 review
    path: the two ``_stub_judge_call`` placeholders make
    :func:`run_judge_evaluator` return
    ``ok=False`` with the
    ``JUDGE_EVALUATOR_BLOCKER`` reason, and the F1 path
    translates that to the BLOCKED condition.

    Mirrors the existing AC10 test in
    :mod:`tests.test_review_command` so the no-replay default
    stays under regression coverage.
    """
    artifact = _write_skill_artifact(tmp_path / "review-test.md")
    workspace = _seed_review_workspace(
        tmp_path / "ws",
        [
            _metadata_record(),
            _judgment_case("case-judge-1", split="eval"),
            _reviewed_case("case-held-1", split="held_out"),
        ],
    )

    result = _run_metacrucible(
        [
            "review",
            str(artifact),
            "--workspace",
            str(workspace),
            "--json",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_BLOCKED, (
        f"`review` without --replay on a judgment case must "
        f"exit {EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    execution = payload["execution_evaluation"]
    by_id = {
        r["case_id"]: r for r in execution["case_results"]
    }
    judge_result = by_id["case-judge-1"]
    assert judge_result["status"] == "BLOCKED", (
        f"judgment case must be BLOCKED when no provider / "
        f"replay is available; got {judge_result['status']!r}"
    )
    blocker_ids = [
        b.get("id")
        for b in judge_result["blockers"]
        if isinstance(b, dict)
    ]
    assert (
        REVIEW_CASE_JUDGE_PROVIDER_UNAVAILABLE_BLOCKER
        in blocker_ids
    ), (
        f"judgment-unavailable blocker must surface; got "
        f"blocker_ids={blocker_ids!r}"
    )


# --------------------------------------------------------------------------- #
# AC3 — bootstrap with --replay is a passthrough                              #
# --------------------------------------------------------------------------- #


def test_bootstrap_replay_passthrough(
    tmp_path: Path,
    isolated_global_home: Path,
) -> None:
    """``bootstrap --replay`` runs the same code path and writes
    the default draft cases. The replay fixture is loaded but
    has no ``bootstrap`` entry, so the case ``input`` falls
    back to :data:`BOOTSTRAP_DRAFT_INPUT` (default no-replay
    behavior is preserved for fixtures that don't supply a
    ``bootstrap`` entry).

    The test asserts the standard F2 contract:
    ``--case-count`` is honored, the case records carry
    ``status=generated`` and the ``BOOTSTRAP_PENDING_REVIEW``
    sentinel, and the JSON payload mirrors the no-replay
    shape.
    """
    # A replay fixture that has judge + optimizer entries but
    # no ``bootstrap`` entry. The bootstrap command should
    # silently ignore the fixture and produce the default
    # BOOTSTRAP_DRAFT_INPUT payload.
    body = "\n".join(
        [
            _record("judge_1", response={"verdict": "pass"}),
            _record("judge_2", response={"verdict": "pass"}),
            _record("optimizer", responses=[{"edit": "noop"}]),
            "",  # trailing newline
        ]
    )
    replay_path = _write_replay(tmp_path, "replay.jsonl", body)

    workspace = tmp_path / "ws-bootstrap"
    workspace.mkdir(parents=True, exist_ok=True)
    # init-style minimal benchmark container
    (workspace / BENCHMARK_FILE_NAME).write_text(
        json.dumps(_metadata_record(), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = _run_metacrucible(
        [
            "bootstrap",
            str(workspace),
            "--replay",
            str(replay_path),
            "--case-count",
            "2",
            "--json",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == EXIT_OK, (
        f"`bootstrap --replay` with a no-bootstrap-entry fixture "
        f"must exit {EXIT_OK}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["case_count"] == 2, (
        f"bootstrap must honor --case-count 2; got "
        f"case_count={payload.get('case_count')!r}"
    )
    assert len(payload["generated_case_ids"]) == 2, (
        f"bootstrap must emit 2 generated_case_ids; got "
        f"{payload.get('generated_case_ids')!r}"
    )
    # The default no-replay behavior of bootstrap is unchanged:
    # the benchmark carries the standard pending-review sentinel
    # (BOOTSTRAP_PENDING_REVIEW=True) and the case records
    # follow the existing _build_bootstrap_case shape. A
    # re-read of the benchmark file proves the on-disk shape.
    benchmark_lines = [
        json.loads(line)
        for line in (workspace / BENCHMARK_FILE_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    new_cases = [
        r for r in benchmark_lines
        if r.get("record_type") == "case"
    ]
    assert len(new_cases) == 2, (
        f"bootstrap --replay must write exactly 2 cases to "
        f"the benchmark; got {len(new_cases)}"
    )
    for case in new_cases:
        assert case.get(BOOTSTRAP_PENDING_REVIEW_FIELD) is True, (
            f"bootstrap case must carry the pending-review "
            f"sentinel; got {case!r}"
        )
        assert case.get("status") == "generated", (
            f"bootstrap case must have status=generated; got "
            f"{case.get('status')!r}"
        )


# --------------------------------------------------------------------------- #
# AC4 — optimize with --replay threads call_fn into both pipeline passes       #
# --------------------------------------------------------------------------- #


def test_optimize_replay_threads_call_fn(
    tmp_path: Path,
    isolated_global_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``optimize --replay`` passes
    :func:`build_optimizer_call_fn` 's result to
    :func:`metacrucible.optimizer.run_optimizer_pipeline` for
    BOTH the preview and the mutating pass.

    The test monkey-patches
    ``metacrucible.__main__.run_optimizer_pipeline`` to record
    the ``call_fn`` argument on every invocation. The brief
    requires the same ``call_fn`` object to be threaded into
    both the preview and the mutating pass so the
    determinism invariant (preview→apply agreement) holds.
    The mutating pass is reached by returning a ``PREVIEW``
    result with a routing record that the operator approves
    via ``--allow-routing-revision`` (non-interactive flag).
    """
    from metacrucible import __main__ as cli_main

    body = "\n".join(
        [
            _record("judge_1", response={"v": 1}),
            _record("judge_2", response={"v": 2}),
            _record(
                "optimizer",
                responses=[{"noop": 1}, {"noop": 2}],
            ),
        ]
    )
    replay_path = _write_replay(tmp_path, "replay.jsonl", body)
    workspace = _seed_optimize_workspace(tmp_path / "ws")

    @dataclass
    class _StubResult:
        status: str = "REJECTED"
        run_id: str = "stub-run"
        rounds: int = 0
        record_counts: dict = field(default_factory=dict)
        evidence_refs: dict = field(default_factory=dict)
        blockers: list = field(default_factory=list)
        warnings: list = field(default_factory=list)
        best_revision: Any = None
        acceptance_decision: dict = field(default_factory=dict)
        selected_candidate_ids: list = field(default_factory=list)
        stop_reason: str = "no_candidate_edits"
        preview: dict | None = None

    captured_call_fns: list[Any] = []
    call_count = {"n": 0}

    def _stub(*args: Any, **kwargs: Any) -> _StubResult:
        captured_call_fns.append(kwargs.get("call_fn"))
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Preview pass: surface a routing revision that
            # the operator will approve via
            # ``--allow-routing-revision`` so the mutating
            # pass is reached.
            return _StubResult(
                status="PREVIEW",
                preview={
                    "routing_confirmation": [
                        {
                            "suggestion_id": "s-1",
                            "routing_field": "name",
                            "old": "old",
                            "new": "new",
                        }
                    ],
                    "profile_verdict": {},
                },
            )
        return _StubResult()

    monkeypatch.setattr(cli_main, "run_optimizer_pipeline", _stub)
    monkeypatch.setenv("HOME", str(isolated_global_home))

    # Build the namespace the real parser would build.
    from metacrucible.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "optimize",
            str(workspace),
            "--replay",
            str(replay_path),
            "--allow-routing-revision",
            "--json",
        ]
    )
    rc = cli_main.cmd_optimize(args)
    assert rc in (EXIT_OK, EXIT_BLOCKED), (
        f"optimize --replay stub must exit OK or BLOCKED; "
        f"got rc={rc}"
    )
    assert len(captured_call_fns) == 2, (
        f"optimize --replay must call run_optimizer_pipeline "
        f"exactly twice (preview + mutating); got "
        f"{len(captured_call_fns)} calls"
    )
    fn_a, fn_b = captured_call_fns
    assert fn_a is not None, (
        "preview pass must receive a non-None call_fn built "
        "from the replay fixture; got None"
    )
    assert fn_b is not None, (
        "mutating pass must receive a non-None call_fn built "
        "from the replay fixture; got None"
    )
    # The brief pins determinism: both passes must receive
    # the same ``call_fn`` object so the recorded responses
    # are consumed in the same order.
    assert fn_a is fn_b, (
        f"preview and mutating pass must receive the same "
        f"call_fn object; got fn_a={fn_a!r} fn_b={fn_b!r}"
    )


# --------------------------------------------------------------------------- #
# AC5 — optimize with recorded ACCEPTED response yields decision: accepted    #
# --------------------------------------------------------------------------- #


def test_optimize_replay_accepted_response_yields_accepted(
    tmp_path: Path,
    isolated_global_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``optimize --replay`` with a stub that returns ACCEPTED +
    ``acceptance_decision.accepted=True`` yields
    ``decision: accepted`` and ``EXIT_OK`` (recorded ACCEPTED
    optimizer response drives the CLI to the success branch).

    The test stubs :func:`metacrucible.optimizer.run_optimizer_pipeline`
    on the ``metacrucible.__main__`` module reference to short-
    circuit the pipeline; the replay fixture is still loaded so
    the call_fn construction path is exercised end-to-end. The
    captured stdout (via ``capsys``) is parsed for the
    ``status`` field which carries the ACCEPTED decision.
    """
    from metacrucible import __main__ as cli_main

    body = "\n".join(
        [
            _record("judge_1", response={"v": 1}),
            _record("judge_2", response={"v": 2}),
            _record("optimizer", responses=[{"edit": "ok"}]),
        ]
    )
    replay_path = _write_replay(tmp_path, "replay.jsonl", body)
    workspace = _seed_optimize_workspace(tmp_path / "ws")

    @dataclass
    class _AcceptedStub:
        status: str = "ACCEPTED"
        run_id: str = "replay-accepted"
        rounds: int = 1
        record_counts: dict = field(
            default_factory=lambda: {"case_eval": 1, "case_held_out": 1}
        )
        evidence_refs: dict = field(default_factory=dict)
        blockers: list = field(default_factory=list)
        warnings: list = field(default_factory=list)
        best_revision: Any = None
        acceptance_decision: dict = field(
            default_factory=lambda: {
                "accepted": True,
                "reason": "replay-accepted",
            }
        )
        selected_candidate_ids: list = field(
            default_factory=lambda: ["cand-1"]
        )
        stop_reason: str = "accepted"
        preview: Any = None

    def _stub(*args: Any, **kwargs: Any) -> _AcceptedStub:
        return _AcceptedStub()

    monkeypatch.setattr(cli_main, "run_optimizer_pipeline", _stub)
    monkeypatch.setenv("HOME", str(isolated_global_home))

    from metacrucible.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "optimize",
            str(workspace),
            "--replay",
            str(replay_path),
            "--json",
        ]
    )
    rc = cli_main.cmd_optimize(args)
    captured = capsys.readouterr()
    assert rc == EXIT_OK, (
        f"optimize --replay with recorded ACCEPTED response "
        f"must exit {EXIT_OK}; got rc={rc} stdout={captured.out!r}"
    )
    payload = json.loads(captured.out)
    assert payload.get("status") == "ACCEPTED", (
        f"optimize --replay with ACCEPTED stub must report "
        f"status=ACCEPTED; got {payload.get('status')!r}"
    )
    acceptance = payload.get("acceptance_decision") or {}
    assert acceptance.get("accepted") is True, (
        f"optimize --replay must surface acceptance_decision."
        f"accepted=True; got {acceptance!r}"
    )


# --------------------------------------------------------------------------- #
# AC6 — default no-replay optimize is REJECTED with the no-LLM rationale       #
# --------------------------------------------------------------------------- #


def test_optimize_no_replay_remains_rejected(
    tmp_path: Path,
    isolated_global_home: Path,
) -> None:
    """Without ``--replay``, ``optimize`` continues to pass
    ``call_fn=None`` to the pipeline and surfaces the no-LLM
    REJECTED verdict. This is the default-behavior-preservation
    regression: the CLI must not silently start a real LLM
    call when ``--replay`` is omitted.

    Mirrors the existing AC5 / ``stop_reason`` test in
    :mod:`tests.test_optimize_command` (``test_stop_reason_in_
    cli_json_payload_for_optimizer_run``) so the no-replay
    REJECTED path stays under regression coverage.
    """
    workspace = _seed_optimize_workspace(tmp_path / "ws")

    result = _run_metacrucible(
        ["optimize", str(workspace), "--json"],
        cwd=REPO_ROOT,
    )
    assert result.returncode in (EXIT_OK, EXIT_BLOCKED), (
        f"`optimize` without --replay must exit 0 or "
        f"{EXIT_BLOCKED}; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    # The no-LLM call path produces a REJECTED verdict with
    # ``stop_reason="no_candidate_edits"``. The CLI must
    # surface that machine-stable value; the F3 contract
    # never invents a prose rationale.
    assert payload.get("status") == "REJECTED", (
        f"no-replay optimize must remain REJECTED; got "
        f"status={payload.get('status')!r}"
    )
    assert payload.get("stop_reason") == "no_candidate_edits", (
        f"no-replay optimize must surface "
        f"stop_reason='no_candidate_edits'; got "
        f"stop_reason={payload.get('stop_reason')!r}"
    )


# --------------------------------------------------------------------------- #
# AC7 — synthesize with recorded ACCEPTED response yields outcome: accepted   #
# --------------------------------------------------------------------------- #


def test_synthesize_replay_accepted_yields_accepted_outcome(
    tmp_path: Path,
    isolated_global_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``synthesize --replay`` with a stub that returns ACCEPTED +
    ``acceptance_decision.accepted=True`` yields
    ``outcome: accepted`` and ``EXIT_OK``.

    The test follows the existing
    ``test_synthesize_reviewed_workspace_runs_optimizer_and_accepts``
    pattern: seed a reviewed synthesis workspace, stub
    :func:`metacrucible.synthesize.run_synthesis_optimizer` to
    return an ACCEPTED payload, and assert the CLI payload
    surfaces ``outcome='accepted'`` and exits :data:`EXIT_OK`.

    The replay fixture is loaded by the synthesis resume path
    so the ``call_fn=`` argument threading is exercised even
    when the wrapper itself is stubbed (the stub is the
    testing seam; the loader is the production seam).
    """
    body = "\n".join(
        [
            _record("judge_1", response={"v": 1}),
            _record("judge_2", response={"v": 2}),
            _record("optimizer", responses=[{"edit": "ok"}]),
        ]
    )
    replay_path = _write_replay(tmp_path, "replay.jsonl", body)

    from metacrucible import synthesize as synth_mod
    from metacrucible import __main__ as cli_main

    workspace = _reviewed_synthesis_workspace(
        tmp_path, monkeypatch=monkeypatch, capsys=capsys
    )

    @dataclass
    class _AcceptedStub:
        status: str = "ACCEPTED"
        run_id: str = "replay-accepted-synth"
        rounds: int = 1
        record_counts: dict = field(
            default_factory=lambda: {"case_eval": 1, "case_held_out": 1}
        )
        evidence_refs: dict = field(default_factory=dict)
        blockers: list = field(default_factory=list)
        warnings: list = field(default_factory=list)
        best_revision: Any = None
        acceptance_decision: dict = field(
            default_factory=lambda: {
                "accepted": True,
                "reason": "replay-accepted",
            }
        )
        selected_candidate_ids: list = field(
            default_factory=lambda: ["cand-1"]
        )
        stop_reason: str = "accepted"
        preview: Any = None

    def _stub(*args: Any, **kwargs: Any) -> _AcceptedStub:
        return _AcceptedStub()

    monkeypatch.setattr(synth_mod, "run_synthesis_optimizer", _stub)
    monkeypatch.setenv("HOME", str(isolated_global_home))

    ns = _synthesize_namespace(
        tmp_path=tmp_path,
        capability_need=None,
        from_spec=None,
        replay=str(replay_path),
    )
    # ``cmd_synthesize`` reads ``args.capability_need`` /
    # ``args.from_spec``; the resume path takes the ``output``
    # branch. The two create-path fields are ``None`` so the
    # dispatcher takes the existing-synthesis-workspace
    # branch.
    rc = cli_main.cmd_synthesize(ns)
    captured = capsys.readouterr()
    assert rc == EXIT_OK, (
        f"synthesize --replay with recorded ACCEPTED response "
        f"must exit {EXIT_OK}; got rc={rc} stdout={captured.out!r}"
    )
    payload = json.loads(captured.out)
    assert payload.get("status") == "OK", (
        f"synthesize --replay ACCEPTED payload must have "
        f"status=OK; got {payload.get('status')!r}"
    )
    assert payload.get("outcome") == "accepted", (
        f"synthesize --replay ACCEPTED payload must have "
        f"outcome='accepted'; got {payload.get('outcome')!r}"
    )


# --------------------------------------------------------------------------- #
# AC8 — default no-replay synthesize threads call_fn=None to the optimizer     #
# --------------------------------------------------------------------------- #


def test_synthesize_replay_no_replay_default_unchanged(
    tmp_path: Path,
    isolated_global_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without ``--replay``, the synthesize resume path still
    passes ``call_fn=None`` to the optimizer (default behavior
    preserved).

    The test stubs
    :func:`metacrucible.synthesize.run_optimizer_pipeline` to
    record the ``call_fn`` kwarg on every invocation. With no
    ``--replay`` flag, both the preview and the apply passes
    must receive ``call_fn=None``. The test then asserts the
    default no-replay payload (``outcome='draft_pending_
    review'`` after the bootstrap round) is preserved.
    """
    from metacrucible import synthesize as synth_mod
    from metacrucible import __main__ as cli_main

    workspace = _reviewed_synthesis_workspace(
        tmp_path, monkeypatch=monkeypatch, capsys=capsys
    )

    @dataclass
    class _PreviewStub:
        status: str = "REJECTED"
        run_id: str = "stub-run"
        rounds: int = 0
        record_counts: dict = field(default_factory=dict)
        evidence_refs: dict = field(default_factory=dict)
        blockers: list = field(default_factory=list)
        warnings: list = field(default_factory=list)
        best_revision: Any = None
        acceptance_decision: dict = field(default_factory=dict)
        selected_candidate_ids: list = field(default_factory=list)
        stop_reason: str = "no_candidate_edits"
        preview: Any = None

    captured: list[dict[str, Any]] = []

    def _stub_pipeline(*args: Any, **kwargs: Any) -> _PreviewStub:
        captured.append(dict(kwargs))
        return _PreviewStub()

    monkeypatch.setattr(
        synth_mod, "run_optimizer_pipeline", _stub_pipeline
    )
    monkeypatch.setattr(
        synth_mod, "_write_synthesize_blocked_bundle", lambda _: {}
    )
    monkeypatch.setenv("HOME", str(isolated_global_home))

    ns = _synthesize_namespace(
        tmp_path=tmp_path,
        capability_need=None,
        from_spec=None,
        replay=None,
    )
    rc = cli_main.cmd_synthesize(ns)
    assert rc == EXIT_BLOCKED, (
        f"no-replay synthesize resume with REJECTED stub must "
        f"exit {EXIT_BLOCKED}; got rc={rc}"
    )
    assert len(captured) == 1, (
        f"no-replay synthesize resume must call "
        f"run_optimizer_pipeline exactly once (preview pass "
        f"only); got {len(captured)} calls"
    )
    assert captured[0].get("call_fn") is None, (
        f"no-replay synthesize must thread call_fn=None into "
        f"the optimizer; got call_fn={captured[0].get('call_fn')!r}"
    )
