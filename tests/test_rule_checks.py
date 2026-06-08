"""TDD behavior tests for issue #14: deterministic rule check engine.

Pins the rule-check engine (ADR 0026, Issue #14). The module under
test is :mod:`metacrucible.rule_checks`. It owns three related jobs:

  1. **Per-case workspace isolation** (AC1) — deterministic checks
     execute in a per-case workspace under the parent workspace, not
     in the coordinator's cwd. The :func:`plan_check_workspace` helper
     returns the per-case path; :func:`execute_check` runs the check
     with ``cwd=case_workspace`` and records the actual cwd so a
     reviewer can confirm the check did not leak into the
     coordinator's tree.

  2. **Boundary type separation** (AC2) — the target-side
     execution boundary (``execution_boundary`` mapping, ADR 0028)
     and the check-side boundary are distinct dataclass types. The
     :func:`validate_check_boundary` helper refuses a
     :class:`TargetBoundary` (and the symmetric check refuses a
     :class:`CheckBoundary`); the two cannot be conflated at the
     validator boundary.

  3. **Complex shell gate** (AC3) — a check that invokes a
     shell-like binary directly (e.g. ``bash`` / ``sh``) without
     pointing at a reviewed wrapper is BLOCKED. The wrapper must
     be a real file in a directory that contains the
     :data:`REVIEWED_WRAPPER_MARKER` sentinel.

The tests cover the three acceptance criteria from issue #14:

  * AC1 — checks run in per-case workspace, not coordinator cwd.
  * AC2 — target boundary and check boundary are represented
    separately and cannot be conflated.
  * AC3 — complex shell requires reviewed wrapper.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

RULE_CHECKS_MODULE = "metacrucible.rule_checks"

#: Stable blocker ids the module must emit on each failure mode.
#: These strings are the machine contract: tests, the optimizer
#: pipeline, and downstream automation all branch on them verbatim.
#: Adding a new id is a contract change; renaming an existing id is
#: a breaking change and must be paired with a migration plan.
EXPECTED_BLOCKERS: dict[str, str] = {
    "check_boundary_type": "rule-check-boundary-type-mismatch",
    "check_complex_shell": "rule-check-complex-shell-requires-wrapper",
    "check_wrapper_missing": "rule-check-wrapper-missing",
    "check_wrapper_not_reviewed": "rule-check-wrapper-not-reviewed",
    "check_workspace_invalid": "rule-check-workspace-invalid",
}

#: Recognized shell-like binaries that count as complex shell
#: invocations when used as ``argv[0]`` of a check command.
SHELL_BINARIES: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "fish", "ksh", "csh", "tcsh", "dash"}
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _blocker_ids(payload: Any) -> list[str]:
    """Return the list of blocker ids in a result, or empty if none."""
    if not isinstance(payload, dict):
        return []
    blockers = payload.get("blockers", [])
    if not isinstance(blockers, list):
        return []
    out: list[str] = []
    for blocker in blockers:
        if isinstance(blocker, dict) and isinstance(blocker.get("id"), str):
            out.append(blocker["id"])
    return out

def _expect_ok(payload: Any, *, context: str) -> None:
    """Assert ``payload`` is a clean result with no blockers."""
    assert isinstance(payload, dict), (
        f"{context} must return a dict; got {type(payload).__name__}"
    )
    assert payload.get("ok") is True, (
        f"{context} must report ok=True; got payload={payload!r}"
    )
    assert _blocker_ids(payload) == [], (
        f"{context} must not emit blockers; got "
        f"blocker_ids={_blocker_ids(payload)!r}"
    )

def _expect_blocked(payload: Any, *, context: str) -> None:
    """Assert ``payload`` is a blocked result with at least one blocker."""
    assert isinstance(payload, dict), (
        f"{context} must return a dict; got {type(payload).__name__}"
    )
    assert payload.get("ok") is False, (
        f"{context} must report ok=False; got payload={payload!r}"
    )
    assert _blocker_ids(payload), (
        f"{context} must emit at least one blocker; got payload={payload!r}"
    )

def _expect_blocker(
    payload: Any, blocker_id: str, *, context: str = ""
) -> str:
    """Assert ``blocker_id`` is present; return the message string."""
    ids = _blocker_ids(payload)
    assert blocker_id in ids, (
        f"{context} must emit blocker id {blocker_id!r}; "
        f"got blocker_ids={ids!r}"
    )
    for blocker in payload.get("blockers", []):
        if isinstance(blocker, dict) and blocker.get("id") == blocker_id:
            message = blocker.get("message", "")
            assert isinstance(message, str) and message, (
                f"{context} blocker {blocker_id!r} must carry a non-empty "
                f"message; got message={message!r}"
            )
            return message
    return ""  # unreachable; the assert above fails first

def _write(path: Path, content: str = "") -> None:
    """Create ``path`` (parents included) and write ``content``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def _make_parent_workspace(tmp_path: Path) -> Path:
    """Create a representative parent workspace tree for check tests.

    The tree is intentionally minimal: the engine does not copy
    files, it just plans a per-case subdirectory. The parent must
    exist as a real directory so the per-case plan can resolve it.
    """
    parent = tmp_path / "parent_workspace"
    parent.mkdir(parents=True, exist_ok=True)
    return parent

def _make_reviewed_wrapper(
    tmp_path: Path, *, name: str = "count_lines.sh"
) -> tuple[Path, Path]:
    """Create a reviewed-wrappers dir with a marker and a wrapper file.

    Returns ``(wrappers_dir, wrapper_path)``. The directory contains
    the :data:`REVIEWED_WRAPPER_MARKER` sentinel and the wrapper
    script lives inside.
    """
    wrappers_dir = tmp_path / "wrappers"
    wrappers_dir.mkdir(parents=True, exist_ok=True)
    _write(
        wrappers_dir / "REVIEWED_WRAPPER_MARKER",
        "# reviewed: see ADR 0035 / Issue #14 AC3\n",
    )
    wrapper_path = wrappers_dir / name
    _write(
        wrapper_path,
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "echo 'complex-shell-ok'\n",
    )
    return wrappers_dir, wrapper_path

@pytest.fixture(scope="module")
def rule_checks() -> Any:
    """Import the rule_checks module; fail (red step) if absent."""
    try:
        return importlib.import_module(RULE_CHECKS_MODULE)
    except ImportError as exc:
        pytest.fail(
            f"rule_checks module {RULE_CHECKS_MODULE!r} is not implemented "
            f"yet (Issue #14 red step). Expected module exposing: "
            f"validate_check_boundary, plan_check_workspace, "
            f"execute_check, CheckBoundary, TargetBoundary, "
            f"REVIEWED_WRAPPER_MARKER, and the EXPECTED_BLOCKERS ids. "
            f"ImportError: {exc}"
        )

# --------------------------------------------------------------------------- #
# Module surface                                                              #
# --------------------------------------------------------------------------- #

def test_rule_checks_module_exposes_required_surface(
    rule_checks: Any,
) -> None:
    """AC1+AC2+AC3: the public surface must exist (TDD red step gate)."""
    for name in (
        "EXPECTED_BLOCKERS",
        "REVIEWED_WRAPPER_MARKER",
        "CheckBoundary",
        "TargetBoundary",
        "validate_check_boundary",
        "plan_check_workspace",
        "execute_check",
    ):
        assert hasattr(rule_checks, name), (
            f"{RULE_CHECKS_MODULE!r} must expose {name!r} (Issue #14); "
            f"got attributes "
            f"{sorted(a for a in dir(rule_checks) if not a.startswith('_'))!r}"
        )

def test_rule_checks_blocker_ids_match_pinned_contract(
    rule_checks: Any,
) -> None:
    """AC1+AC2+AC3: every blocker id the tests branch on must exist."""
    blockers = rule_checks.EXPECTED_BLOCKERS
    assert isinstance(blockers, dict), (
        f"EXPECTED_BLOCKERS must be a dict; got {type(blockers).__name__}"
    )
    for key, expected in EXPECTED_BLOCKERS.items():
        assert blockers.get(key) == expected, (
            f"EXPECTED_BLOCKERS[{key!r}] must equal {expected!r}; "
            f"got {blockers.get(key)!r}"
        )

def test_rule_checks_reviewed_wrapper_marker_is_stable_string(
    rule_checks: Any,
) -> None:
    """AC3: the marker name is a machine-stable contract."""
    marker = rule_checks.REVIEWED_WRAPPER_MARKER
    assert isinstance(marker, str) and marker, (
        f"REVIEWED_WRAPPER_MARKER must be a non-empty str; got {marker!r}"
    )
    # Pin the exact marker so wrappers dirs can be located by name
    # in a stable way across modules and reviewers.
    assert marker == "REVIEWED_WRAPPER_MARKER", (
        f"REVIEWED_WRAPPER_MARKER must equal 'REVIEWED_WRAPPER_MARKER' "
        f"(Issue #14 AC3 contract); got {marker!r}"
    )

# --------------------------------------------------------------------------- #
# AC2 — boundary types are separate and cannot be conflated                   #
# --------------------------------------------------------------------------- #

def test_check_boundary_and_target_boundary_are_distinct_types(
    rule_checks: Any,
) -> None:
    """AC2: CheckBoundary and TargetBoundary are distinct dataclass types.

    The type system is the primary defense against conflation; tests
    must reject any merge where one type is silently used in the
    other's slot.
    """
    check_cls = rule_checks.CheckBoundary
    target_cls = rule_checks.TargetBoundary
    assert isinstance(check_cls, type) and isinstance(target_cls, type), (
        f"CheckBoundary and TargetBoundary must both be classes; "
        f"got CheckBoundary={check_cls!r}, TargetBoundary={target_cls!r}"
    )
    assert check_cls is not target_cls, (
        "CheckBoundary and TargetBoundary must be distinct classes; "
        "the same class would let callers pass one where the other is "
        "required (conflation, Issue #14 AC2)"
    )

def test_validate_check_boundary_rejects_target_boundary(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC2: a TargetBoundary must be REJECTED in the check validator.

    Conflation prevention: a caller that accidentally passes a
    TargetBoundary (the existing execution_boundary shape) where a
    CheckBoundary is required must be blocked with a stable id, not
    silently coerced.
    """
    target = rule_checks.TargetBoundary(
        allowed_tools=("Bash", "Read"),
        target_commands=(("ls", "-la"),),
    )
    result = rule_checks.validate_check_boundary(target)
    _expect_blocked(result, context="validate_check_boundary(TargetBoundary)")
    _expect_blocker(
        result,
        EXPECTED_BLOCKERS["check_boundary_type"],
        context="TargetBoundary passed to check validator",
    )

def test_validate_check_boundary_accepts_check_boundary(
    rule_checks: Any,
) -> None:
    """AC2 (positive): a CheckBoundary with simple commands is OK."""
    check = rule_checks.CheckBoundary(
        commands=(("cat", "out.txt"),),
    )
    result = rule_checks.validate_check_boundary(check)
    _expect_ok(result, context="validate_check_boundary(CheckBoundary)")

def test_validate_check_boundary_rejects_raw_dict(
    rule_checks: Any,
) -> None:
    """AC2 (defensive): a raw dict cannot masquerade as a CheckBoundary.

    The plain-dict shape is the ``execution_boundary`` mapping from
    ADR 0028. The check validator must refuse the dict so a caller
    cannot bypass the type-system defense by passing the legacy
    mapping shape.
    """
    legacy = {"allowed_tools": ["Bash"], "target_commands": [["ls"]]}
    result = rule_checks.validate_check_boundary(legacy)
    _expect_blocked(
        result, context="validate_check_boundary(legacy dict)"
    )
    _expect_blocker(
        result,
        EXPECTED_BLOCKERS["check_boundary_type"],
        context="legacy dict passed to check validator",
    )

def test_check_boundary_does_not_carry_target_boundary_fields(
    rule_checks: Any,
) -> None:
    """AC2: a CheckBoundary must not carry target-only fields.

    ``allowed_tools`` and ``strict_read_paths`` are target-side
    concepts (ADR 0028). The check boundary must not silently accept
    them — that would let a caller smuggle target-side intent
    through a check-side boundary.
    """
    check = rule_checks.CheckBoundary(commands=(("ls",),))
    forbidden = {"allowed_tools", "strict_read_paths", "target_commands"}
    leaked = forbidden.intersection(vars(check))
    assert not leaked, (
        f"CheckBoundary must not carry target-side fields; "
        f"leaked={leaked!r} (Issue #14 AC2)"
    )

def test_target_boundary_does_not_carry_check_boundary_fields(
    rule_checks: Any,
) -> None:
    """AC2: a TargetBoundary must not carry check-only fields.

    ``commands`` (check-side) on a target boundary is a conflation
    vector; the validator must reject any object that mixes the two
    shapes.
    """
    target = rule_checks.TargetBoundary(
        allowed_tools=("Bash",),
        target_commands=(("ls",),),
    )
    forbidden = {"commands", "complex_shell_commands"}
    leaked = forbidden.intersection(vars(target))
    assert not leaked, (
        f"TargetBoundary must not carry check-side fields; "
        f"leaked={leaked!r} (Issue #14 AC2)"
    )

# --------------------------------------------------------------------------- #
# AC1 — per-case workspace isolation                                          #
# --------------------------------------------------------------------------- #

def test_plan_check_workspace_returns_per_case_subpath(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC1: per-case workspace is a subdir of the parent, distinct from it."""
    parent = _make_parent_workspace(tmp_path)
    result = rule_checks.plan_check_workspace(parent, "case-001")
    _expect_ok(result, context="plan_check_workspace(case-001)")
    case_workspace = result.get("workspace")
    assert isinstance(case_workspace, Path), (
        f"plan_check_workspace must return a Path under 'workspace'; "
        f"got {case_workspace!r} of type {type(case_workspace).__name__}"
    )
    assert case_workspace != parent, (
        f"per-case workspace must be distinct from the parent; "
        f"got case_workspace={case_workspace!r} == parent={parent!r}"
    )
    # The per-case workspace must be a subpath of the parent so a
    # reviewer can audit the boundary in a single tree.
    assert parent in case_workspace.parents or case_workspace.parent == parent, (
        f"per-case workspace must live under the parent workspace; "
        f"got parent={parent!r} case_workspace={case_workspace!r}"
    )

def test_plan_check_workspace_includes_case_id_in_path(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC1: the per-case path carries the case_id so each case is distinct."""
    parent = _make_parent_workspace(tmp_path)
    a = rule_checks.plan_check_workspace(parent, "case-A")
    b = rule_checks.plan_check_workspace(parent, "case-B")
    _expect_ok(a, context="plan_check_workspace(case-A)")
    _expect_ok(b, context="plan_check_workspace(case-B)")
    pa = a["workspace"]
    pb = b["workspace"]
    assert pa != pb, (
        f"distinct case ids must yield distinct per-case paths; "
        f"got pa={pa!r} pb={pb!r}"
    )
    assert "case-A" in str(pa) and "case-B" in str(pb), (
        f"per-case path must include the case_id; got pa={pa!r} pb={pb!r}"
    )

def test_plan_check_workspace_rejects_unsafe_case_id(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC1 (defensive): a case_id with path traversal is BLOCKED.

    A case_id is joined under the parent workspace to build the
    per-case path. A ``..`` segment or absolute path would let a
    caller escape the parent tree; the gate refuses such ids.
    """
    parent = _make_parent_workspace(tmp_path)
    for bad in ("../escape", "..", "/etc/passwd", "a/b", "a\\b"):
        result = rule_checks.plan_check_workspace(parent, bad)
        assert result.get("ok") is False, (
            f"plan_check_workspace must reject unsafe case_id {bad!r}; "
            f"got result={result!r}"
        )
        _expect_blocker(
            result,
            EXPECTED_BLOCKERS["check_workspace_invalid"],
            context=f"unsafe case_id {bad!r}",
        )

def test_execute_check_runs_in_per_case_workspace(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC1: a check observes its own per-case cwd, not the coordinator's.

    A small support script is written to the per-case workspace
    (its content is not validated). The check is
    ``python <support_script>``; the script writes its resolved cwd
    to ``out.txt``. The test reads ``out.txt`` and asserts the
    recorded cwd equals the per-case workspace, not the
    coordinator's cwd.
    """
    import sys

    parent = _make_parent_workspace(tmp_path)
    plan = rule_checks.plan_check_workspace(parent, "case-cwd")
    _expect_ok(plan, context="plan_check_workspace(case-cwd)")
    case_workspace = plan["workspace"]
    case_workspace.mkdir(parents=True, exist_ok=True)
    support_script = case_workspace / "_record_cwd.py"
    support_script.write_text(
        "import os\n"
        "open('out.txt', 'w').write(os.getcwd())\n",
        encoding="utf-8",
    )
    check = rule_checks.CheckBoundary(
        commands=(
            (sys.executable, str(support_script)),
        ),
    )
    result = rule_checks.execute_check(check, case_workspace, index=0)
    assert result.get("ok") is True, (
        f"execute_check must succeed for a simple python check; "
        f"got result={result!r}"
    )
    actual_cwd = result.get("actual_cwd")
    assert actual_cwd is not None, (
        f"execute_check must record the actual cwd of the check; "
        f"got result={result!r}"
    )
    assert Path(actual_cwd).resolve() == case_workspace.resolve(), (
        f"check must run with cwd=case_workspace; "
        f"expected {case_workspace.resolve()!r}, got actual_cwd={actual_cwd!r}"
    )
    # Coordinator cwd is a different directory in the temp tree; the
    # check must not have inherited it.
    assert Path(actual_cwd).resolve() != Path.cwd().resolve(), (
        f"check must NOT run in coordinator cwd; "
        f"actual_cwd={actual_cwd!r} coordinator_cwd={Path.cwd()!r}"
    )
    # The check wrote its observed cwd to out.txt; the recorded cwd
    # must match the file contents (defense in depth: even if the
    # engine were buggy, the file-on-disk shows the real cwd).
    written = (case_workspace / "out.txt").read_text(encoding="utf-8").strip()
    assert Path(written).resolve() == case_workspace.resolve(), (
        f"check file on disk must show case_workspace as cwd; "
        f"wrote {written!r}, expected {case_workspace.resolve()!r}"
    )

def test_execute_check_blocked_when_complex_shell_without_wrapper(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC3: a complex-shell check (bash) is BLOCKED without a wrapper.

    The check validator must refuse ``[bash, ...]`` argv without a
    reviewed wrapper, so the engine never executes raw shell from a
    case boundary.
    """
    parent = _make_parent_workspace(tmp_path)
    plan = rule_checks.plan_check_workspace(parent, "case-shell")
    case_workspace = plan["workspace"]
    case_workspace.mkdir(parents=True, exist_ok=True)
    check = rule_checks.CheckBoundary(
        commands=(("bash", "-c", "echo hi"),),
    )
    result = rule_checks.execute_check(check, case_workspace, index=0)
    _expect_blocked(
        result, context="execute_check(bash without wrapper)"
    )
    _expect_blocker(
        result,
        EXPECTED_BLOCKERS["check_complex_shell"],
        context="complex shell without reviewed wrapper",
    )

# --------------------------------------------------------------------------- #
# AC3 — complex shell requires reviewed wrapper                               #
# --------------------------------------------------------------------------- #

def test_complex_shell_check_blocked_without_wrapper(
    rule_checks: Any,
) -> None:
    """AC3 (validator): a CheckBoundary with bash argv is BLOCKED.

    A check that invokes a shell-like binary directly is a complex
    shell check and must be refused unless the boundary cites a
    reviewed wrapper file.
    """
    check = rule_checks.CheckBoundary(commands=(("bash", "-c", "echo x"),))
    result = rule_checks.validate_check_boundary(check)
    _expect_blocked(
        result, context="validate_check_boundary(bash, no wrapper)"
    )
    _expect_blocker(
        result,
        EXPECTED_BLOCKERS["check_complex_shell"],
        context="bash without reviewed wrapper",
    )

def test_simple_check_does_not_require_wrapper(
    rule_checks: Any,
) -> None:
    """AC3 (positive): a non-shell argv check is OK without a wrapper.

    Conservative argvs that do not invoke a shell-like binary are
    simple checks and are accepted without a wrapper. This is the
    common case: ``cat``, ``grep``, ``diff``, ``python <script>``.
    """
    check = rule_checks.CheckBoundary(
        commands=(("cat", "out.txt"), ("grep", "-c", "x", "f.txt")),
    )
    result = rule_checks.validate_check_boundary(check)
    _expect_ok(result, context="validate_check_boundary(simple)")

def test_complex_shell_check_blocked_with_nonexistent_wrapper(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC3: pointing at a missing wrapper file is BLOCKED.

    A wrapper that does not exist on disk is unreviewable; the
    engine refuses the boundary rather than silently substituting a
    fallback.
    """
    check = rule_checks.CheckBoundary(
        commands=(("bash", str(tmp_path / "no-such-wrapper.sh")),),
        wrapper=str(tmp_path / "no-such-wrapper.sh"),
    )
    result = rule_checks.validate_check_boundary(check, base=tmp_path)
    _expect_blocked(
        result, context="validate_check_boundary(missing wrapper)"
    )
    # Either the missing-file gate or the not-reviewed gate may fire
    # depending on which check runs first; both are valid failures.
    ids = _blocker_ids(result)
    assert (
        EXPECTED_BLOCKERS["check_wrapper_missing"] in ids
        or EXPECTED_BLOCKERS["check_wrapper_not_reviewed"] in ids
    ), (
        f"missing/nonexistent wrapper must emit a wrapper blocker; "
        f"got blocker_ids={ids!r}"
    )

def test_complex_shell_check_blocked_with_unreviewed_wrapper(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC3: a wrapper in a non-reviewed dir is BLOCKED.

    The wrapper file exists, but the directory does not contain
    :data:`REVIEWED_WRAPPER_MARKER`, so the wrapper is not on a
    review-approved path. The engine refuses rather than executing
    unreviewed code.
    """
    wrapper_dir = tmp_path / "unreviewed"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = wrapper_dir / "evil.sh"
    _write(wrapper_path, "#!/usr/bin/env bash\necho evil\n")
    # Note: no REVIEWED_WRAPPER_MARKER in the dir.
    check = rule_checks.CheckBoundary(
        commands=(("bash", str(wrapper_path)),),
        wrapper=str(wrapper_path),
    )
    result = rule_checks.validate_check_boundary(
        check, base=tmp_path
    )
    _expect_blocked(
        result, context="validate_check_boundary(unreviewed wrapper)"
    )
    _expect_blocker(
        result,
        EXPECTED_BLOCKERS["check_wrapper_not_reviewed"],
        context="wrapper in non-reviewed dir",
    )

def test_complex_shell_check_passes_with_reviewed_wrapper(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC3 (positive): bash + a reviewed wrapper is OK.

    The wrapper file lives in a directory containing
    :data:`REVIEWED_WRAPPER_MARKER`. The boundary declares the
    wrapper; the validator accepts it.
    """
    _wrappers_dir, wrapper_path = _make_reviewed_wrapper(tmp_path)
    check = rule_checks.CheckBoundary(
        commands=(("bash", str(wrapper_path)),),
        wrapper=str(wrapper_path),
    )
    result = rule_checks.validate_check_boundary(
        check, base=tmp_path
    )
    _expect_ok(
        result, context="validate_check_boundary(bash + reviewed wrapper)"
    )

def test_execute_check_with_reviewed_wrapper_runs_in_workspace(
    rule_checks: Any, tmp_path: Path
) -> None:
    """AC1+AC3: a reviewed-wrapper check actually runs in case workspace.

    The check is ``bash <reviewed_wrapper.sh>`` and writes the
    observed cwd to ``out.txt`` so a reviewer can confirm the check
    ran in the per-case workspace, not the coordinator's cwd.
    """
    _wrappers_dir, wrapper_path = _make_reviewed_wrapper(
        tmp_path, name="record_cwd.sh"
    )
    # Overwrite the wrapper body to record the cwd in the per-case workspace.
    wrapper_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "pwd > out.txt\n",
        encoding="utf-8",
    )
    parent = _make_parent_workspace(tmp_path / "parent")
    plan = rule_checks.plan_check_workspace(parent, "case-reviewed")
    _expect_ok(plan, context="plan_check_workspace(case-reviewed)")
    case_workspace = plan["workspace"]
    case_workspace.mkdir(parents=True, exist_ok=True)
    check = rule_checks.CheckBoundary(
        commands=(("bash", str(wrapper_path)),),
        wrapper=str(wrapper_path),
    )
    result = rule_checks.execute_check(check, case_workspace, index=0)
    assert result.get("ok") is True, (
        f"reviewed-wrapper check must succeed; got result={result!r}"
    )
    actual_cwd = result.get("actual_cwd")
    assert Path(actual_cwd).resolve() == case_workspace.resolve(), (
        f"reviewed-wrapper check must run in case workspace; "
        f"expected {case_workspace.resolve()!r}, got {actual_cwd!r}"
    )
    # The wrapper wrote out.txt with its own observed cwd.
    written = (case_workspace / "out.txt").read_text(encoding="utf-8").strip()
    assert Path(written).resolve() == case_workspace.resolve(), (
        f"wrapper on disk must show case_workspace as cwd; "
        f"wrote {written!r}, expected {case_workspace.resolve()!r}"
    )

# --------------------------------------------------------------------------- #
# argv-level safety — shell metachars / wildcards in argv                     #
# --------------------------------------------------------------------------- #

def test_check_boundary_rejects_shell_metachar_argv(
    rule_checks: Any,
) -> None:
    """CheckBoundary rejects shell-metachar tokens.

    A token with a shell metachar (e.g. ``;``) is rejected with a
    stable blocker id, mirroring the target boundary's
    conservative-argv rule.
    """
    check = rule_checks.CheckBoundary(commands=(("echo", "a;b"),))
    result = rule_checks.validate_check_boundary(check)
    _expect_blocked(result, context="validate_check_boundary(metachar)")
    assert _blocker_ids(result), (
        f"metachar argv must emit a blocker; got {result!r}"
    )

def test_check_boundary_rejects_wildcard_argv(
    rule_checks: Any,
) -> None:
    """CheckBoundary rejects wildcards.

    A wildcard argv is a complex check that must go through a
    reviewed wrapper; the engine refuses the bare wildcard.
    """
    check = rule_checks.CheckBoundary(commands=(("rm", "*"),))
    result = rule_checks.validate_check_boundary(check)
    _expect_blocked(result, context="validate_check_boundary(wildcard)")
    assert _blocker_ids(result), (
        f"wildcard argv must emit a blocker; got {result!r}"
    )

# --------------------------------------------------------------------------- #
# Review hardening (Issue #14 follow-up)                                      #
# --------------------------------------------------------------------------- #
#
# These tests pin the post-review contracts called out by independent
# review of the Issue #14 implementation. Each test captures one
# hardening concern; together they lock the engine against the failure
# modes the review surfaced.
def test_plan_check_workspace_rejects_control_chars_in_case_id(
    rule_checks: Any, tmp_path: Path
) -> None:
    """Control chars in case_id (NUL, DEL, etc.) must BLOCK, not raise.

    A case_id with an embedded ASCII control character is a
    path-segment injection: NUL is a C-string terminator, DEL /
    other control bytes break a reviewer's tooling, and the
    resulting ``<parent>/cases/<case_id>`` path would be unsafe
    to feed to a subprocess. The validator must return a
    blocked result with the ``rule-check-workspace-invalid``
    id; it must NOT raise an exception, accept the path, or
    silently coerce the case_id.
    """
    parent = _make_parent_workspace(tmp_path)
    for bad in (
        "case\x00id",  # NUL
        "case\x01id",  # SOH
        "case\x0bid",  # vertical tab
        "case\x0cid",  # form feed
        "case\x0did",  # carriage return
        "case\x1bid",  # ESC
        "case\x7fid",  # DEL
    ):
        try:
            result = rule_checks.plan_check_workspace(parent, bad)
        except Exception as exc:
            pytest.fail(
                f"plan_check_workspace must return a blocked result for "
                f"control-char case_id={bad!r}; raised "
                f"{type(exc).__name__}: {exc} "
                f"(Issue #14 review hardening)"
            )
        assert isinstance(result, dict), (
            f"plan_check_workspace must return a dict for case_id={bad!r}; "
            f"got {type(result).__name__}={result!r}"
        )
        assert result.get("ok") is False, (
            f"plan_check_workspace must reject control-char case_id={bad!r}; "
            f"got result={result!r}"
        )
        _expect_blocker(
            result,
            EXPECTED_BLOCKERS["check_workspace_invalid"],
            context=f"control-char case_id {bad!r}",
        )
        # The rejected case_id must not appear in any accepted path
        # field of the result (defense in depth).
        workspace = result.get("workspace")
        if isinstance(workspace, Path):
            assert bad not in str(workspace), (
                f"rejected case_id={bad!r} must not leak into a workspace "
                f"path; got workspace={workspace!r}"
            )


def test_plan_check_workspace_rejects_interior_whitespace_case_id(
    rule_checks: Any, tmp_path: Path
) -> None:
    """Interior whitespace in case_id must BLOCK, not produce a spaced path.

    A case_id with an interior space / tab / newline is a path
    injection: the engine would build a path that contains
    whitespace, which an operator's tooling might split on or
    which a downstream check would not be able to quote safely.
    The validator must refuse such case_ids.
    """
    parent = _make_parent_workspace(tmp_path)
    for bad in ("case id", "case\tid", "case\nid"):
        try:
            result = rule_checks.plan_check_workspace(parent, bad)
        except Exception as exc:
            pytest.fail(
                f"plan_check_workspace must return a blocked result for "
                f"interior-whitespace case_id={bad!r}; raised "
                f"{type(exc).__name__}: {exc} "
                f"(Issue #14 review hardening)"
            )
        _expect_blocked(
            result, context=f"interior-whitespace case_id {bad!r}"
        )
        _expect_blocker(
            result,
            EXPECTED_BLOCKERS["check_workspace_invalid"],
            context=f"interior-whitespace case_id {bad!r}",
        )
        workspace = result.get("workspace")
        if isinstance(workspace, Path):
            assert bad not in str(workspace), (
                f"rejected whitespace case_id={bad!r} must not leak into "
                f"a workspace path; got workspace={workspace!r}"
            )


def test_reviewed_wrapper_marker_symlink_does_not_grant_reviewed_status(
    rule_checks: Any, tmp_path: Path
) -> None:
    """A symlink named REVIEWED_WRAPPER_MARKER must NOT grant reviewed.

    The marker must be a real file inside the wrapper directory.
    A symlink (regardless of what it points at) would let a
    wrapper dir trivially claim reviewedness by symlinking the
    marker to any file — including files outside the wrapper
    dir that an operator could swap under the engine. The gate
    must therefore reject the symlink case (Issue #14 AC3
    review hardening).
    """
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    # Create a real target file outside the wrapper dir so the
    # symlink has a real file to follow.
    real_target = tmp_path / "innocent.txt"
    real_target.write_text("not a marker\n", encoding="utf-8")
    # Place a symlink named REVIEWED_WRAPPER_MARKER in the
    # wrapper dir pointing at the real file.
    marker_link = wrapper_dir / rule_checks.REVIEWED_WRAPPER_MARKER
    marker_link.symlink_to(real_target)
    wrapper_path = wrapper_dir / "wrapper.sh"
    wrapper_path.write_text(
        "#!/usr/bin/env bash\necho x\n", encoding="utf-8"
    )
    check = rule_checks.CheckBoundary(
        commands=(("bash", str(wrapper_path)),),
        wrapper=str(wrapper_path),
    )
    result = rule_checks.validate_check_boundary(check, base=tmp_path)
    _expect_blocked(
        result, context="validate_check_boundary(symlinked marker)"
    )
    _expect_blocker(
        result,
        EXPECTED_BLOCKERS["check_wrapper_not_reviewed"],
        context="symlinked marker must not grant reviewed",
    )


def test_rule_checks_shell_metachars_match_argv_normalize(
    rule_checks: Any,
) -> None:
    """_SHELL_METACHARS must stay in lockstep with argv_normalize.

    The check engine and the target-boundary normalizer share
    the same shell-metachar vocabulary: a token that is safe on
    the target side must be safe on the check side and vice
    versa. Drift would silently let a check accept a token the
    target boundary refuses (or block a safe one). The simplest
    prevention is an alias; the test pins the contract
    regardless of how the alias is implemented.
    """
    argv_normalize = importlib.import_module("metacrucible.argv_normalize")
    assert hasattr(rule_checks, "_SHELL_METACHARS"), (
        "rule_checks must expose _SHELL_METACHARS (Issue #14 review "
        "hardening); the constant pins the check-side metachar vocabulary"
    )
    assert hasattr(argv_normalize, "_SHELL_METACHARS"), (
        "argv_normalize must expose _SHELL_METACHARS; the test depends "
        "on the constant to verify the contract"
    )
    rule_set = rule_checks._SHELL_METACHARS
    argv_set = argv_normalize._SHELL_METACHARS
    assert rule_set == argv_set, (
        f"rule_checks._SHELL_METACHARS must equal "
        f"argv_normalize._SHELL_METACHARS to prevent drift "
        f"(Issue #14 review hardening); "
        f"got rule_checks={sorted(rule_set)!r} "
        f"argv_normalize={sorted(argv_set)!r}"
    )
    # Pin a small subset of the canonical high-risk chars so a
    # silent vocabulary change (e.g. dropping ``|``) cannot pass
    # the contract test above by accident.
    for canonical in ("|", "&", ";", ">", "<", "$", "`", "!", "\n"):
        assert canonical in rule_set, (
            f"shell metachar vocabulary must include {canonical!r}; "
            f"got {sorted(rule_set)!r}"
        )


def test_execute_check_nonzero_return_is_failed_check_result(
    rule_checks: Any, tmp_path: Path
) -> None:
    """Non-zero subprocess returncode is a *failed check* (ok=False, blockers=[]).

    execute_check distinguishes three result shapes:

      * ok=True, blockers=[]                   -> check passed
      * ok=False, blockers=[...]               -> check BLOCKED
        (validator refused, subprocess never ran, or wrapper missing)
      * ok=False, blockers=[], returncode != 0 -> check RAN and FAILED
        (subprocess exited non-zero; reviewer sees the captured
        stdout / stderr and the actual cwd)

    This test pins the third shape: a non-zero returncode must
    surface as a failed check (empty blockers, captured IO,
    actual_cwd set), NOT as a blocked check with a fake
    blocker. A reviewer must be able to trust that an empty
    ``blockers`` list means the validator approved the run and
    the negative result is the subprocess's own verdict.
    """
    import sys

    parent = _make_parent_workspace(tmp_path)
    plan = rule_checks.plan_check_workspace(parent, "case-fail")
    _expect_ok(plan, context="plan_check_workspace(case-fail)")
    case_workspace = plan["workspace"]
    case_workspace.mkdir(parents=True, exist_ok=True)
    # The support script writes a known marker and exits 7 so
    # we can assert the exact returncode, the empty-blocker
    # shape, and the captured stdout.
    support_script = case_workspace / "_fail.py"
    support_script.write_text(
        "import sys\n"
        "print('check-failed-by-design')\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    check = rule_checks.CheckBoundary(
        commands=((sys.executable, str(support_script)),),
    )
    result = rule_checks.execute_check(check, case_workspace, index=0)
    assert isinstance(result, dict), (
        f"execute_check must return a dict; got {type(result).__name__}"
    )
    assert result.get("ok") is False, (
        f"non-zero exit must yield ok=False; got result={result!r}"
    )
    assert result.get("blockers") == [], (
        f"failed-check result must carry empty blockers (a non-zero "
        f"returncode is a failed check, not a validation block); "
        f"got blockers={result.get('blockers')!r} "
        f"(Issue #14 review hardening)"
    )
    assert result.get("returncode") == 7, (
        f"failed-check result must record the actual returncode; "
        f"got returncode={result.get('returncode')!r}"
    )
    assert "check-failed-by-design" in (result.get("stdout") or ""), (
        f"failed-check result must capture stdout; "
        f"got stdout={result.get('stdout')!r}"
    )
    # The actual cwd is recorded so a reviewer can confirm the
    # check ran in the per-case workspace, even on failure.
    actual_cwd = result.get("actual_cwd")
    assert actual_cwd is not None and Path(actual_cwd).resolve() == case_workspace.resolve(), (
        f"failed-check result must still record actual_cwd; "
        f"got actual_cwd={actual_cwd!r}, "
        f"expected={case_workspace.resolve()!r}"
    )


def test_execute_check_handles_wrong_boundary_type_without_assertion_error(
    rule_checks: Any, tmp_path: Path
) -> None:
    """execute_check must not raise AssertionError for wrong-type boundary.

    Review hardening: the engine must refuse wrong-type boundaries
    (None, raw dict, TargetBoundary, str, int, ...) via
    validate_check_boundary and return a blocked result, NOT a
    runtime ``assert isinstance(boundary, CheckBoundary)``. The
    assert is not the primary check; the test pins the contract
    that the validator is the load-bearing gate.
    """
    parent = _make_parent_workspace(tmp_path)
    plan = rule_checks.plan_check_workspace(parent, "case-bad-boundary")
    _expect_ok(plan, context="plan_check_workspace(case-bad-boundary)")
    case_workspace = plan["workspace"]
    case_workspace.mkdir(parents=True, exist_ok=True)
    for bad in (
        None,
        {"allowed_tools": ["Bash"]},   # raw dict
        rule_checks.TargetBoundary(),  # target boundary
        "a string",                    # wrong type
        42,                            # wrong type
    ):
        try:
            result = rule_checks.execute_check(bad, case_workspace, index=0)
        except AssertionError as exc:
            pytest.fail(
                f"execute_check must not raise AssertionError for wrong-"
                f"type boundary {bad!r}; the wrong-type path must be "
                f"handled by validate_check_boundary, not a runtime "
                f"assert (Issue #14 review hardening). "
                f"AssertionError: {exc}"
            )
        assert isinstance(result, dict), (
            f"execute_check must return a dict for wrong-type boundary "
            f"{bad!r}; got {type(result).__name__}"
        )
        assert result.get("ok") is False, (
            f"execute_check must return ok=False for wrong-type boundary "
            f"{bad!r}; got result={result!r}"
        )
        assert result.get("blockers"), (
            f"execute_check must emit at least one blocker for wrong-type "
            f"boundary {bad!r}; got result={result!r}"
        )
