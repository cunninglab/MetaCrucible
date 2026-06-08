"""Tests for Issue #15: layered control-plane provider configuration.

Issue #15 pins the contract from ADR 0034:

  - ``control_plane``, ``providers``, and ``runtime_adapters`` are
    three independent top-level config sections. The control plane
    picks ``judge`` and ``optimizer`` as ``{provider, model}`` pairs;
    ``providers`` holds Anthropic- and OpenAI-compatible provider
    shapes; ``runtime_adapters`` configures target execution (e.g.
    the Claude Code binary and mode) and must not be allowed to
    satisfy or overwrite the control-plane selection.
  - Config files may reference credentials through ``api_key_env``;
    direct ``api_key`` / ``token`` / ``secret`` / ``password``
    fields are rejected anywhere in config without echoing the
    secret value back to the caller.
  - The resolver accepts layered dict configs in this precedence:
    built-in defaults < user config < project config < CLI overrides.

References
----------
- ADR 0034 (control-plane provider configuration).
- Issue #15 acceptance criteria.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

PROVIDER_CONFIG_MODULE = "metacrucible.provider_config"

# --------------------------------------------------------------------------- #
# Expected contract                                                           #
# --------------------------------------------------------------------------- #

# Stable blocker ids the resolver must emit. These are the machine
# contract: tests and downstream automation branch on them verbatim.
EXPECTED_SECRET_FIELD_BLOCKER: str = "provider-config-secret-field-rejected"
EXPECTED_RUNTIME_LEAK_BLOCKER: str = (
    "provider-config-runtime-adapter-control-plane-leak"
)
EXPECTED_BLOCKERS: dict[str, str] = {
    "secret_field": EXPECTED_SECRET_FIELD_BLOCKER,
    "runtime_leak": EXPECTED_RUNTIME_LEAK_BLOCKER,
}

# A unique sentinel secret value used to verify that the error
# message does NOT echo the secret back to the caller. Any string
# works; this one is obviously a placeholder so a human reading
# the test understands the intent.
SECRET_SENTINEL_VALUE: str = "LEAKED-SECRET-VALUE-do-not-echo-9b3c7a"

# Built-in defaults we expect the resolver to ship out of the box.
# Issue #15 AC2: Anthropic and OpenAI-compatible shapes both supported.
EXPECTED_DEFAULT_PROVIDER_TYPES: frozenset[str] = frozenset(
    {"anthropic", "openai_compatible"}
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _blocker_ids(payload: Any) -> list[str]:
    """Return the list of blocker ids in a resolve result, or empty if none."""
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


@pytest.fixture(scope="module")
def provider_config() -> Any:
    """Import the provider_config module; the test fails (red step) if missing."""
    try:
        return importlib.import_module(PROVIDER_CONFIG_MODULE)
    except ImportError as exc:
        pytest.fail(
            f"provider_config module {PROVIDER_CONFIG_MODULE!r} is not "
            f"implemented yet (Issue #15 red step). Expected module "
            f"exposing: EXPECTED_BLOCKERS, SECRET_FIELD_BLOCKER, "
            f"RUNTIME_ADAPTER_LEAK_BLOCKER, DEFAULT_PROVIDER_CONFIG, "
            f"resolve_provider_config. ImportError: {exc}"
        )


# --------------------------------------------------------------------------- #
# Public surface (red step gate)                                              #
# --------------------------------------------------------------------------- #


def test_provider_config_module_exposes_required_surface(
    provider_config: Any,
) -> None:
    """The module must expose the public surface called out in issue #15."""
    for name in (
        "EXPECTED_BLOCKERS",
        "SECRET_FIELD_BLOCKER",
        "RUNTIME_ADAPTER_LEAK_BLOCKER",
        "DEFAULT_PROVIDER_CONFIG",
        "resolve_provider_config",
    ):
        assert hasattr(provider_config, name), (
            f"{PROVIDER_CONFIG_MODULE!r} must expose {name!r} "
            f"(Issue #15); got attributes "
            f"{sorted(a for a in dir(provider_config) if not a.startswith('_'))!r}"
        )


def test_provider_config_module_exposes_blocker_ids(
    provider_config: Any,
) -> None:
    """The stable blocker ids must match the values pinned by the contract."""
    assert provider_config.SECRET_FIELD_BLOCKER == EXPECTED_SECRET_FIELD_BLOCKER, (
        f"SECRET_FIELD_BLOCKER must be exactly "
        f"{EXPECTED_SECRET_FIELD_BLOCKER!r}; got "
        f"{provider_config.SECRET_FIELD_BLOCKER!r}"
    )
    assert (
        provider_config.RUNTIME_ADAPTER_LEAK_BLOCKER
        == EXPECTED_RUNTIME_LEAK_BLOCKER
    ), (
        f"RUNTIME_ADAPTER_LEAK_BLOCKER must be exactly "
        f"{EXPECTED_RUNTIME_LEAK_BLOCKER!r}; got "
        f"{provider_config.RUNTIME_ADAPTER_LEAK_BLOCKER!r}"
    )
    assert isinstance(provider_config.EXPECTED_BLOCKERS, dict)
    assert (
        provider_config.EXPECTED_BLOCKERS.get("secret_field")
        == EXPECTED_SECRET_FIELD_BLOCKER
    )
    assert (
        provider_config.EXPECTED_BLOCKERS.get("runtime_leak")
        == EXPECTED_RUNTIME_LEAK_BLOCKER
    )


# --------------------------------------------------------------------------- #
# Built-in defaults                                                           #
# --------------------------------------------------------------------------- #


def test_default_provider_config_has_three_top_level_sections(
    provider_config: Any,
) -> None:
    """Defaults must include control_plane, providers, runtime_adapters."""
    defaults = provider_config.DEFAULT_PROVIDER_CONFIG
    assert isinstance(defaults, Mapping), (
        f"DEFAULT_PROVIDER_CONFIG must be a mapping; got {type(defaults).__name__}"
    )
    for required in ("control_plane", "providers", "runtime_adapters"):
        assert required in defaults, (
            f"DEFAULT_PROVIDER_CONFIG must include {required!r} "
            f"(Issue #15 AC2/AC3); got keys={sorted(defaults.keys())!r}"
        )


def test_default_control_plane_selects_judge_and_optimizer(
    provider_config: Any,
) -> None:
    """Defaults must seed control_plane.judge and control_plane.optimizer as {provider, model}."""
    defaults = provider_config.DEFAULT_PROVIDER_CONFIG
    control_plane = defaults["control_plane"]
    assert isinstance(control_plane, Mapping)
    for role in ("judge", "optimizer"):
        assert role in control_plane, (
            f"DEFAULT_PROVIDER_CONFIG.control_plane must select {role!r} "
            f"(Issue #15); got keys={sorted(control_plane.keys())!r}"
        )
        pair = control_plane[role]
        assert isinstance(pair, Mapping), (
            f"control_plane.{role} must be a mapping; got {type(pair).__name__}"
        )
        assert "provider" in pair and "model" in pair, (
            f"control_plane.{role} must carry 'provider' and 'model' keys; "
            f"got keys={sorted(pair.keys())!r}"
        )


def test_default_providers_supports_anthropic_and_openai_compatible(
    provider_config: Any,
) -> None:
    """Defaults must ship at least one Anthropic and one OpenAI-compatible shape."""
    providers = provider_config.DEFAULT_PROVIDER_CONFIG["providers"]
    assert isinstance(providers, Mapping)
    provider_types = {
        name: spec.get("type") if isinstance(spec, Mapping) else None
        for name, spec in providers.items()
    }
    for expected in EXPECTED_DEFAULT_PROVIDER_TYPES:
        assert expected in provider_types.values(), (
            f"DEFAULT_PROVIDER_CONFIG.providers must include a {expected!r} "
            f"shape (Issue #15 AC2); got provider types={provider_types!r}"
        )


# --------------------------------------------------------------------------- #
# Layered resolution precedence                                                #
# --------------------------------------------------------------------------- #


def test_resolve_applies_precedence_defaults_user_project_cli(
    provider_config: Any,
) -> None:
    """Precedence: built-in defaults < user < project < CLI (later wins)."""
    defaults_override = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "default-judge"},
            "optimizer": {"provider": "anthropic", "model": "default-opt"},
        }
    }
    user_override = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "user-judge"},
        }
    }
    project_override = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "project-judge"},
        }
    }
    cli_override = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "cli-judge"},
        }
    }
    result = provider_config.resolve_provider_config(
        defaults=defaults_override,
        user=user_override,
        project=project_override,
        cli=cli_override,
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True, (
        f"clean layered resolve must succeed; got result={result!r}"
    )
    config = result["config"]
    assert config["control_plane"]["judge"]["model"] == "cli-judge", (
        f"CLI must win; got {config['control_plane']['judge']!r}"
    )
    # No project override for optimizer -> user/default chain.
    assert config["control_plane"]["optimizer"]["model"] == "default-opt", (
        f"optimizer must fall back to defaults when no override; got "
        f"{config['control_plane']['optimizer']!r}"
    )


def test_resolve_project_overrides_user_overrides_defaults(
    provider_config: Any,
) -> None:
    """Project config must override user config, which must override defaults."""
    defaults = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "DEFAULT"},
        }
    }
    user = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "USER"},
        }
    }
    project = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "PROJECT"},
        }
    }
    result = provider_config.resolve_provider_config(
        defaults=defaults, user=user, project=project
    )
    assert result.get("ok") is True
    assert result["config"]["control_plane"]["judge"]["model"] == "PROJECT"


# --------------------------------------------------------------------------- #
# Top-level sections stay separate                                            #
# --------------------------------------------------------------------------- #


def test_resolve_returns_three_independent_top_level_sections(
    provider_config: Any,
) -> None:
    """The resolved config must expose control_plane, providers, runtime_adapters."""
    result = provider_config.resolve_provider_config()
    assert result.get("ok") is True, (
        f"bare resolve must succeed; got result={result!r}"
    )
    config = result["config"]
    assert isinstance(config, Mapping)
    assert set(config.keys()) == {"control_plane", "providers", "runtime_adapters"}, (
        f"resolved config must have exactly the three top-level sections "
        f"(Issue #15 AC2/AC3); got keys={sorted(config.keys())!r}"
    )


def test_resolve_does_not_merge_runtime_adapters_into_control_plane(
    provider_config: Any,
) -> None:
    """Runtime adapter config must not bleed into control_plane or providers.

    The control-plane and runtime-adapter sections are siblings;
    a clean runtime-adapter entry must never reach the control
    plane or the providers section even though the sections are
    resolved by the same layered merge. (The leak-key rejection
    path is exercised by a separate test.)
    """
    user = {
        "runtime_adapters": {
            "claude_code": {
                "binary": "claude",
                "mode": "subscription",
            }
        },
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "real-judge"},
            "optimizer": {"provider": "anthropic", "model": "real-opt"},
        },
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is True, (
        f"clean layered resolve must succeed; got result={result!r}"
    )
    config = result["config"]
    # control_plane.judge must come from the control_plane layer.
    assert config["control_plane"]["judge"]["model"] == "real-judge", (
        f"control_plane.judge must be independent of runtime_adapters; got "
        f"{config['control_plane']['judge']!r}"
    )
    # providers must not include anything injected via runtime_adapters.
    assert config["providers"], "providers section must be populated by defaults"
    assert "claude_code" not in config["providers"], (
        f"providers must not be polluted by runtime_adapters content; got "
        f"providers={config['providers']!r}"
    )
    # runtime_adapters is preserved as-is.
    assert config["runtime_adapters"]["claude_code"]["mode"] == "subscription"


# --------------------------------------------------------------------------- #
# Runtime adapter must not satisfy control plane (rejection)                  #
# --------------------------------------------------------------------------- #


def test_resolve_rejects_runtime_adapters_claiming_control_plane_keys(
    provider_config: Any,
) -> None:
    """A runtime_adapters entry that injects judge/optimizer/providers must be rejected.

    Issue #15 AC3 + ADR 0034: runtime adapter config is for target
    execution, not for satisfying the control-plane selection. The
    resolver blocks the case with the runtime-adapter-control-plane-leak
    blocker id.
    """
    user = {
        "runtime_adapters": {
            "claude_code": {
                "binary": "claude",
                "judge": {"provider": "anthropic", "model": "x"},
            }
        }
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is False, (
        f"runtime_adapters.judge must be rejected; got result={result!r}"
    )
    assert EXPECTED_RUNTIME_LEAK_BLOCKER in _blocker_ids(result), (
        f"runtime-leak violation must emit blocker id "
        f"{EXPECTED_RUNTIME_LEAK_BLOCKER!r}; got blocker_ids="
        f"{_blocker_ids(result)!r}"
    )


# --------------------------------------------------------------------------- #
# Provider shapes                                                             #
# --------------------------------------------------------------------------- #


def test_providers_supports_anthropic_shape(provider_config: Any) -> None:
    """Anthropic shape: type=anthropic, api_key_env present."""
    user = {
        "providers": {
            "anthropic": {
                "type": "anthropic",
                "api_key_env": "ANTHROPIC_API_KEY",
            }
        }
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is True, (
        f"valid Anthropic shape must be accepted; got result={result!r}"
    )
    spec = result["config"]["providers"]["anthropic"]
    assert spec["type"] == "anthropic"
    assert spec["api_key_env"] == "ANTHROPIC_API_KEY"


def test_providers_supports_openai_compatible_shape(
    provider_config: Any,
) -> None:
    """OpenAI-compatible shape: type=openai_compatible, base_url + api_key_env."""
    user = {
        "providers": {
            "openai_compatible": {
                "type": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
            }
        }
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is True, (
        f"valid OpenAI-compatible shape must be accepted; got result={result!r}"
    )
    spec = result["config"]["providers"]["openai_compatible"]
    assert spec["type"] == "openai_compatible"
    assert spec["base_url"] == "https://api.openai.com/v1"
    assert spec["api_key_env"] == "OPENAI_API_KEY"


# --------------------------------------------------------------------------- #
# api_key_env is the only accepted credential form                            #
# --------------------------------------------------------------------------- #


def test_resolve_accepts_api_key_env_field(provider_config: Any) -> None:
    """api_key_env is the supported way to reference a credential."""
    user = {
        "providers": {
            "anthropic": {
                "type": "anthropic",
                "api_key_env": "MY_ANTHROPIC_KEY",
            }
        }
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is True, (
        f"api_key_env must be accepted; got result={result!r}"
    )


@pytest.mark.parametrize(
    "field_name",
    ["api_key", "token", "secret", "password"],
    ids=["api_key", "token", "secret", "password"],
)
def test_resolve_rejects_exact_secret_field_name(
    provider_config: Any, field_name: str
) -> None:
    """Field literally named api_key/token/secret/password must be rejected."""
    user = {
        "providers": {
            "anthropic": {
                "type": "anthropic",
                "api_key_env": "ANTHROPIC_API_KEY",
                field_name: SECRET_SENTINEL_VALUE,
            }
        }
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is False, (
        f"field {field_name!r} must be rejected; got result={result!r}"
    )
    assert EXPECTED_SECRET_FIELD_BLOCKER in _blocker_ids(result), (
        f"direct {field_name!r} field must emit blocker id "
        f"{EXPECTED_SECRET_FIELD_BLOCKER!r}; got blocker_ids="
        f"{_blocker_ids(result)!r}"
    )


@pytest.mark.parametrize(
    "field_name",
    [
        "client_api_key",
        "auth_token",
        "db_password",
        "client_secret",
    ],
    ids=["client_api_key", "auth_token", "db_password", "client_secret"],
)
def test_resolve_rejects_field_ending_with_secret_name(
    provider_config: Any, field_name: str
) -> None:
    """Field whose name ends with api_key/token/secret/password must be rejected."""
    user = {
        "providers": {
            "anthropic": {
                "type": "anthropic",
                "api_key_env": "ANTHROPIC_API_KEY",
                field_name: SECRET_SENTINEL_VALUE,
            }
        }
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is False, (
        f"field {field_name!r} must be rejected (suffix rule); "
        f"got result={result!r}"
    )
    assert EXPECTED_SECRET_FIELD_BLOCKER in _blocker_ids(result), (
        f"secret-suffix field {field_name!r} must emit blocker id "
        f"{EXPECTED_SECRET_FIELD_BLOCKER!r}; got blocker_ids="
        f"{_blocker_ids(result)!r}"
    )


def test_resolve_rejects_secret_field_anywhere_in_config(
    provider_config: Any,
) -> None:
    """Secret-field rejection must apply anywhere in the merged config, not just providers."""
    user = {
        "runtime_adapters": {
            "claude_code": {
                "binary": "claude",
                "auth_token": SECRET_SENTINEL_VALUE,
            }
        }
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is False
    assert EXPECTED_SECRET_FIELD_BLOCKER in _blocker_ids(result)


def test_resolve_does_not_echo_secret_value_in_error(
    provider_config: Any,
) -> None:
    """The error text must NOT contain the rejected secret value (ADR 0034)."""
    user = {
        "providers": {
            "anthropic": {
                "type": "anthropic",
                "api_key": SECRET_SENTINEL_VALUE,
            }
        }
    }
    result = provider_config.resolve_provider_config(user=user)
    assert result.get("ok") is False
    blob = str(result)
    assert SECRET_SENTINEL_VALUE not in blob, (
        f"rejected secret value must not appear in the resolver result "
        f"(ADR 0034); leaked value {SECRET_SENTINEL_VALUE!r} in {blob!r}"
    )
    # Also assert it's not in the merged config, which we deliberately
    # omit on a blocked resolve — but if a future refactor ever
    # returns partial config, the secret must not be in it either.
    config_blob = str(result.get("config", {}))
    assert SECRET_SENTINEL_VALUE not in config_blob, (
        f"secret value must not be present in resolved config when "
        f"validation fails; leaked in {config_blob!r}"
    )


# --------------------------------------------------------------------------- #
# CLI overrides win (smoke)                                                   #
# --------------------------------------------------------------------------- #


def test_cli_overrides_win_over_project_user_and_defaults(
    provider_config: Any,
) -> None:
    """Smoke: a CLI override applies even when every other layer disagrees."""
    defaults = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "DEFAULT"},
            "optimizer": {"provider": "anthropic", "model": "DEFAULT"},
        }
    }
    user = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "USER"},
        }
    }
    project = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "PROJECT"},
        }
    }
    cli = {
        "control_plane": {
            "judge": {"provider": "anthropic", "model": "CLI"},
            "optimizer": {"provider": "anthropic", "model": "CLI"},
        }
    }
    result = provider_config.resolve_provider_config(
        defaults=defaults, user=user, project=project, cli=cli
    )
    assert result.get("ok") is True
    assert result["config"]["control_plane"]["judge"]["model"] == "CLI"
    assert result["config"]["control_plane"]["optimizer"]["model"] == "CLI"


# --------------------------------------------------------------------------- #
# Issue #16: structured-output capability probe                              #
# --------------------------------------------------------------------------- #
# Issue #16 + ADR 0034 require that judge and optimizer structured-output use
# is gated on a successful capability probe. The probe function is supplied
# per-provider via a public callback (``probe_fn``) so production code can use
# a real LLM round-trip and tests can drive success and failure paths with a
# deterministic local function (no mocks).

# Stable blocker id emitted by ``run_structured_output_probe`` on a failed
# probe. The id is part of the machine contract; renaming is a breaking
# change.
EXPECTED_STRUCTURED_OUTPUT_PROBE_BLOCKER: str = (
    "provider-config-structured-output-probe-failed"
)

# Deterministic local probe callbacks used by the tests. They are
# plain functions defined in this test module; the production code
# receives them as ``probe_fn`` arguments and never reaches for any
# monkeypatch or mock library.


def _fake_probe_pass(
    provider_name: str,
    provider_spec: Mapping[str, Any],
    model: str,
) -> dict[str, Any]:
    """A fake probe that always succeeds and records what it saw."""
    return {
        "ok": True,
        "reason": None,
        "latency_ms": 1,
        "probe_kind": "fake-pass",
        "raw": {
            "provider_name": provider_name,
            "model": model,
            "type": provider_spec.get("type")
            if isinstance(provider_spec, Mapping)
            else None,
        },
    }


def _fake_probe_fail_judge(
    provider_name: str,
    provider_spec: Mapping[str, Any],
    model: str,
) -> dict[str, Any]:
    """A fake probe that always fails; the caller observes ok=False."""
    return {
        "ok": False,
        "reason": "synthetic refusal: model does not support tools",
        "latency_ms": 2,
        "probe_kind": "fake-fail",
        "raw": {
            "provider_name": provider_name,
            "model": model,
        },
    }


def test_provider_config_module_exposes_probe_surface(
    provider_config: Any,
) -> None:
    """Issue #16: module must expose the capability-probe entry point and blocker id."""
    for name in (
        "STRUCTURED_OUTPUT_PROBE_BLOCKER",
        "run_structured_output_probe",
    ):
        assert hasattr(provider_config, name), (
            f"{PROVIDER_CONFIG_MODULE!r} must expose {name!r} (Issue #16); "
            f"got attributes "
            f"{sorted(a for a in dir(provider_config) if not a.startswith('_'))!r}"
        )


def test_provider_config_module_exposes_probe_blocker_id(
    provider_config: Any,
) -> None:
    """The probe blocker id is part of the machine contract; it must be stable."""
    assert (
        provider_config.STRUCTURED_OUTPUT_PROBE_BLOCKER
        == EXPECTED_STRUCTURED_OUTPUT_PROBE_BLOCKER
    ), (
        f"STRUCTURED_OUTPUT_PROBE_BLOCKER must be exactly "
        f"{EXPECTED_STRUCTURED_OUTPUT_PROBE_BLOCKER!r}; got "
        f"{provider_config.STRUCTURED_OUTPUT_PROBE_BLOCKER!r}"
    )
    assert isinstance(provider_config.EXPECTED_BLOCKERS, dict)
    assert (
        provider_config.EXPECTED_BLOCKERS.get("structured_output_probe")
        == EXPECTED_STRUCTURED_OUTPUT_PROBE_BLOCKER
    ), (
        f"EXPECTED_BLOCKERS must include the structured_output_probe entry; "
        f"got {provider_config.EXPECTED_BLOCKERS!r}"
    )


def test_run_structured_output_probe_passes_with_passing_probe_fn(
    provider_config: Any,
) -> None:
    """A passing probe means judge/optimizer structured-output use is unblocked."""
    resolved = provider_config.resolve_provider_config()
    assert resolved.get("ok") is True, (
        f"baseline resolve must succeed; got {resolved!r}"
    )
    result = provider_config.run_structured_output_probe(
        resolved["config"], probe_fn=_fake_probe_pass
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True, (
        f"passing probe must yield ok=True; got result={result!r}"
    )
    assert result.get("blockers") == [], (
        f"passing probe must emit no blockers; got {result['blockers']!r}"
    )


def test_run_structured_output_probe_blocks_with_stable_blocker(
    provider_config: Any,
) -> None:
    """A failing probe must block structured-output use with the stable blocker id."""
    resolved = provider_config.resolve_provider_config()
    result = provider_config.run_structured_output_probe(
        resolved["config"], probe_fn=_fake_probe_fail_judge
    )
    assert result.get("ok") is False, (
        f"failing probe must yield ok=False; got result={result!r}"
    )
    assert EXPECTED_STRUCTURED_OUTPUT_PROBE_BLOCKER in _blocker_ids(result), (
        f"failed probe must emit blocker id "
        f"{EXPECTED_STRUCTURED_OUTPUT_PROBE_BLOCKER!r}; got "
        f"blocker_ids={_blocker_ids(result)!r}"
    )


def test_run_structured_output_probe_records_evidence_per_role(
    provider_config: Any,
) -> None:
    """Probe evidence must be present in a deterministic place per role."""
    resolved = provider_config.resolve_provider_config()
    result = provider_config.run_structured_output_probe(
        resolved["config"], probe_fn=_fake_probe_pass
    )
    evidence = result.get("probe_evidence")
    assert isinstance(evidence, dict), (
        f"probe_evidence must be a dict at a deterministic place; got "
        f"{evidence!r}"
    )
    for role in ("judge", "optimizer"):
        assert role in evidence, (
            f"probe_evidence must include {role!r}; got "
            f"keys={sorted(evidence.keys())!r}"
        )
        entry = evidence[role]
        assert isinstance(entry, dict)
        assert entry.get("role") == role
        assert entry.get("provider") == "anthropic", (
            f"default config selects anthropic for {role!r}; got "
            f"{entry.get('provider')!r}"
        )
        assert entry.get("model") == "claude-sonnet-4-5"
        assert entry.get("ok") is True
        assert entry.get("probe_kind") == "fake-pass"


def test_run_structured_output_probe_does_not_echo_credential_in_blocker(
    provider_config: Any,
) -> None:
    """The blocker message must not leak the env var name as a credential echo."""
    user = {
        "providers": {
            "anthropic": {
                "type": "anthropic",
                "api_key_env": "MY_SUPER_SECRET_KEY_NAME",
            }
        }
    }
    resolved = provider_config.resolve_provider_config(user=user)
    result = provider_config.run_structured_output_probe(
        resolved["config"], probe_fn=_fake_probe_fail_judge
    )
    blob = str(result)
    assert "MY_SUPER_SECRET_KEY_NAME" not in blob, (
        f"provider credential env-var name must not appear in probe result; "
        f"leaked in {blob!r}"
    )


def test_run_structured_output_probe_uses_default_probe_for_known_types(
    provider_config: Any,
) -> None:
    """The default probe must recognize the built-in provider types and pass."""
    resolved = provider_config.resolve_provider_config()
    result = provider_config.run_structured_output_probe(resolved["config"])
    assert result.get("ok") is True, (
        f"default probe on default config must succeed; got result={result!r}"
    )
    assert result.get("blockers") == []


def test_run_structured_output_probe_default_blocks_unknown_provider_type(
    provider_config: Any,
) -> None:
    """The default probe must reject an unrecognized provider type."""
    user = {
        "providers": {
            "weird_unknown_provider": {
                "type": "made_up_type",
                "api_key_env": "WEIRD_PROVIDER_KEY",
            }
        },
        "control_plane": {
            "judge": {
                "provider": "weird_unknown_provider",
                "model": "weird-1",
            },
            "optimizer": {
                "provider": "weird_unknown_provider",
                "model": "weird-1",
            },
        },
    }
    resolved = provider_config.resolve_provider_config(user=user)
    assert resolved.get("ok") is True, (
        f"resolver must accept a well-formed unknown type; got {resolved!r}"
    )
    result = provider_config.run_structured_output_probe(resolved["config"])
    assert result.get("ok") is False, (
        f"default probe must reject unknown provider type; got result={result!r}"
    )
    assert EXPECTED_STRUCTURED_OUTPUT_PROBE_BLOCKER in _blocker_ids(result)


# --------------------------------------------------------------------------- #
# Issue #17: JSON Schema validation + bounded repair retry                    #
# --------------------------------------------------------------------------- #
# Issue #17 + ADR 0034 require that every structured call's response is
# validated against a JSON Schema, and that a failing response is repaired
# via a bounded number of retry calls to the same ``call_fn``. Contract:
#
#   * ``call_structured(provider_name, provider_spec, model, schema, call_fn,
#       *, max_repair_attempts=1)`` is the public entry point.
#   * The first call to ``call_fn`` receives ``repair_context=None``; on
#     every retry the call_fn receives a mapping carrying
#     ``validation_errors`` and ``schema`` so the LLM/caller can self-correct.
#   * Retries stop after ``max_repair_attempts``; final failure returns the
#     stable STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER.
#   * On success: ``{"ok": True, "value": <validated>, "attempts": int,
#     "validation_errors": [], "blockers": []}``.
#   * On final failure: ``{"ok": False, "value": None, "attempts": int,
#     "validation_errors": non-empty list,
#     "blockers": [{"id": stable, "message": non-empty str}]}``.

# Stable blocker id emitted by ``call_structured`` on a final validation
# failure. The id is part of the machine contract; renaming is a breaking
# change.
EXPECTED_STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER: str = (
    "provider-config-structured-output-schema-validation-failed"
)

# A minimal JSON Schema for the call_structured fixtures. The schema is
# intentionally simple so any reasonable JSON Schema validator
# (jsonschema, pydantic, hand-rolled) will agree on pass/fail semantics.
_CALL_STRUCTURED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
    },
    "required": ["answer"],
    "additionalProperties": False,
}

_CALL_STRUCTURED_VALID_RESPONSE: dict[str, Any] = {"answer": "hello"}
_CALL_STRUCTURED_INVALID_RESPONSE: dict[str, Any] = {"answer": 42}


def _build_queued_call_fn(responses: list[Any]) -> tuple[Any, list[Any]]:
    """Build a deterministic ``call_fn`` that returns a queued value per call.

    The returned call_fn accepts the ``repair_context`` keyword (and, as
    a defensive fallback, a single positional argument) and records
    every call's ``repair_context``. The recorder is the second element
    of the returned tuple so each test can assert exactly what the
    production code passed.
    """
    recorded: list[Any] = []
    queue = list(responses)

    def call_fn(*args: Any, **kwargs: Any) -> Any:
        repair_context: Any = kwargs.get("repair_context")
        if repair_context is None and args:
            repair_context = args[0]
        recorded.append(repair_context)
        if not queue:
            raise AssertionError(
                f"call_fn invoked more times than queued responses "
                f"({len(responses)} queued; got call #{len(recorded)})"
            )
        return queue.pop(0)

    return call_fn, recorded


def test_call_structured_module_exposes_blocker_constant_and_entry(
    provider_config: Any,
) -> None:
    """Issue #17: module must expose the validation blocker constant and entry point."""
    for name in (
        "STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER",
        "call_structured",
    ):
        assert hasattr(provider_config, name), (
            f"{PROVIDER_CONFIG_MODULE!r} must expose {name!r} (Issue #17); "
            f"got attributes "
            f"{sorted(a for a in dir(provider_config) if not a.startswith('_'))!r}"
        )


def test_call_structured_blocker_id_matches_pinned_value(
    provider_config: Any,
) -> None:
    """The validation blocker id is part of the machine contract; it must be stable."""
    assert (
        provider_config.STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER
        == EXPECTED_STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER
    ), (
        f"STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER must be exactly "
        f"{EXPECTED_STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER!r}; got "
        f"{provider_config.STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER!r}"
    )
    assert isinstance(provider_config.EXPECTED_BLOCKERS, dict)
    assert (
        provider_config.EXPECTED_BLOCKERS.get("structured_output_schema")
        == EXPECTED_STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER
    ), (
        f"EXPECTED_BLOCKERS must include the structured_output_schema entry "
        f"with the stable id; got {provider_config.EXPECTED_BLOCKERS!r}"
    )


def test_call_structured_passes_when_response_is_valid(
    provider_config: Any,
) -> None:
    """A first-attempt valid response returns the value with no repair and no blockers."""
    call_fn, recorded = _build_queued_call_fn([_CALL_STRUCTURED_VALID_RESPONSE])
    result = provider_config.call_structured(
        "anthropic",
        {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
        "claude-sonnet-4-5",
        _CALL_STRUCTURED_SCHEMA,
        call_fn,
    )
    assert isinstance(result, dict), (
        f"call_structured must return a dict result; got {type(result).__name__}"
    )
    assert result.get("ok") is True, (
        f"valid first-attempt response must yield ok=True; got result={result!r}"
    )
    assert result.get("value") == _CALL_STRUCTURED_VALID_RESPONSE, (
        f"value must round-trip the validated response; got "
        f"{result.get('value')!r}"
    )
    assert result.get("validation_errors") == [], (
        f"success must report an empty validation_errors list; got "
        f"{result.get('validation_errors')!r}"
    )
    assert result.get("blockers") == [], (
        f"success must emit no blockers; got {result.get('blockers')!r}"
    )
    assert result.get("attempts") == 1, (
        f"first-attempt success must have attempts=1; got "
        f"{result.get('attempts')!r}"
    )
    assert len(recorded) == 1, (
        f"first-attempt success must call call_fn exactly once; got "
        f"{len(recorded)} calls"
    )
    assert recorded[0] is None, (
        f"first call must receive repair_context=None; got {recorded[0]!r}"
    )


def test_call_structured_repairs_invalid_response_and_passes_on_retry(
    provider_config: Any,
) -> None:
    """An invalid first response triggers a retry whose repair_context exposes errors + schema."""
    call_fn, recorded = _build_queued_call_fn(
        [_CALL_STRUCTURED_INVALID_RESPONSE, _CALL_STRUCTURED_VALID_RESPONSE]
    )
    result = provider_config.call_structured(
        "anthropic",
        {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
        "claude-sonnet-4-5",
        _CALL_STRUCTURED_SCHEMA,
        call_fn,
        max_repair_attempts=1,
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True, (
        f"retry-with-valid-response must yield ok=True; got result={result!r}"
    )
    assert result.get("value") == _CALL_STRUCTURED_VALID_RESPONSE, (
        f"value must be the repaired response; got {result.get('value')!r}"
    )
    assert result.get("attempts") == 2, (
        f"first invalid + one retry must yield attempts=2; got "
        f"{result.get('attempts')!r}"
    )
    assert result.get("blockers") == [], (
        f"successful repair must emit no blockers; got {result.get('blockers')!r}"
    )
    assert len(recorded) == 2, (
        f"expected exactly 2 calls (initial + 1 retry); got {len(recorded)}"
    )
    assert recorded[0] is None, (
        f"first call must receive repair_context=None; got {recorded[0]!r}"
    )
    repair_context = recorded[1]
    assert isinstance(repair_context, Mapping), (
        f"retry call must receive a mapping as repair_context; got "
        f"{type(repair_context).__name__}"
    )
    assert "validation_errors" in repair_context, (
        f"repair_context must include 'validation_errors' for the LLM/caller "
        f"to self-correct; got keys={sorted(repair_context.keys())!r}"
    )
    validation_errors = repair_context["validation_errors"]
    assert isinstance(validation_errors, list) and validation_errors, (
        f"repair_context['validation_errors'] must be a non-empty list; got "
        f"{validation_errors!r}"
    )
    assert "schema" in repair_context, (
        f"repair_context must include the 'schema' to validate against on "
        f"retry; got keys={sorted(repair_context.keys())!r}"
    )
    assert repair_context["schema"] == _CALL_STRUCTURED_SCHEMA, (
        f"repair_context['schema'] must round-trip the JSON Schema; got "
        f"{repair_context['schema']!r}"
    )


def test_call_structured_stops_after_max_repair_attempts_with_stable_blocker(
    provider_config: Any,
) -> None:
    """A persistently invalid response must stop after max_repair_attempts and return the blocker."""
    max_repair_attempts = 2
    call_fn, recorded = _build_queued_call_fn(
        [_CALL_STRUCTURED_INVALID_RESPONSE] * (1 + max_repair_attempts)
    )
    result = provider_config.call_structured(
        "anthropic",
        {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
        "claude-sonnet-4-5",
        _CALL_STRUCTURED_SCHEMA,
        call_fn,
        max_repair_attempts=max_repair_attempts,
    )
    assert isinstance(result, dict)
    assert result.get("ok") is False, (
        f"final-failure must yield ok=False; got result={result!r}"
    )
    assert result.get("value") is None, (
        f"final-failure must have value=None; got {result.get('value')!r}"
    )
    validation_errors = result.get("validation_errors")
    assert isinstance(validation_errors, list) and validation_errors, (
        f"final-failure must report a non-empty validation_errors list; got "
        f"{validation_errors!r}"
    )
    assert result.get("attempts") == 1 + max_repair_attempts, (
        f"attempts must equal 1 initial + max_repair_attempts retries; got "
        f"{result.get('attempts')!r}"
    )
    assert len(recorded) == 1 + max_repair_attempts, (
        f"call_fn must be invoked 1 + max_repair_attempts times; got "
        f"{len(recorded)} calls"
    )
    blockers = result.get("blockers")
    assert isinstance(blockers, list) and blockers, (
        f"final-failure must emit at least one blocker; got {blockers!r}"
    )
    blocker_ids = [
        b.get("id") for b in blockers if isinstance(b, dict)
    ]
    assert (
        EXPECTED_STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER in blocker_ids
    ), (
        f"final-failure must emit blocker id "
        f"{EXPECTED_STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER!r}; got "
        f"{blocker_ids!r}"
    )
    for blocker in blockers:
        assert isinstance(blocker, dict), (
            f"every blocker must be a dict; got {type(blocker).__name__}"
        )
        assert (
            isinstance(blocker.get("id"), str) and blocker["id"]
        ), (
            f"every blocker must have a non-empty id; got {blocker!r}"
        )
        message = blocker.get("message")
        assert isinstance(message, str) and message, (
            f"every blocker must carry a non-empty message string; got "
            f"{message!r}"
        )


def test_call_structured_does_not_retry_when_max_repair_attempts_is_zero(
    provider_config: Any,
) -> None:
    """max_repair_attempts=0 means: no repair retry at all, even on invalid responses."""
    call_fn, recorded = _build_queued_call_fn([_CALL_STRUCTURED_INVALID_RESPONSE])
    result = provider_config.call_structured(
        "anthropic",
        {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
        "claude-sonnet-4-5",
        _CALL_STRUCTURED_SCHEMA,
        call_fn,
        max_repair_attempts=0,
    )
    assert isinstance(result, dict)
    assert result.get("ok") is False, (
        f"max_repair_attempts=0 with an invalid response must yield ok=False; "
        f"got result={result!r}"
    )
    assert result.get("attempts") == 1, (
        f"max_repair_attempts=0 must yield attempts=1; got "
        f"{result.get('attempts')!r}"
    )
    assert len(recorded) == 1, (
        f"max_repair_attempts=0 must not call call_fn more than once; got "
        f"{len(recorded)} calls"
    )
    assert recorded[0] is None, (
        f"first call must still receive repair_context=None; got "
        f"{recorded[0]!r}"
    )
    assert (
        EXPECTED_STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER in _blocker_ids(result)
    ), (
        f"max_repair_attempts=0 final-failure must still emit the stable "
    )

def test_provider_config_module_exposes_record_provider_run_outcome_surface(
    provider_config: Any,
) -> None:
    """Issue #18: module must expose the provider-run-outcome recorder surface.

    The recorder writes provider-neutral usage/cost data into
    ``state.json``. The module must expose:

      - ``USAGE_MISSING_WARNING`` / ``COST_MISSING_WARNING`` — stable
        warning ids emitted when usage or cost is missing on a run.
      - ``record_provider_run_outcome`` — the public entry point.
    """
    for name in (
        "USAGE_MISSING_WARNING",
        "COST_MISSING_WARNING",
        "record_provider_run_outcome",
    ):
        assert hasattr(provider_config, name), (
            f"{PROVIDER_CONFIG_MODULE!r} must expose {name!r} (Issue #18); "
            f"got attributes "
            f"{sorted(a for a in dir(provider_config) if not a.startswith('_'))!r}"
        )


def test_record_provider_run_outcome_writes_provider_usage_to_state_json(
    provider_config: Any, tmp_path: Path
) -> None:
    """Issue #18 AC1: cost (and usage) is recorded into state.json.

    The recorder must round-trip the raw usage and cost payloads
    into a ``provider_usage`` block in ``state.json`` so a
    downstream receipt writer can correlate cost and tokens per
    run. Other state fields (current_best_revision, last_run_id)
    must be preserved so the rest of the CLI surface keeps
    working unchanged.
    """
    import importlib
    storage_mod = importlib.import_module("metacrucible.storage")
    repo = storage_mod.RepositoryStorage(tmp_path)
    repo.write_state(
        {
            "current_best_revision": "rev-001",
            "last_run_id": "run-prev",
        }
    )
    result = provider_config.record_provider_run_outcome(
        repo,
        run_id="run-abc",
        provider="anthropic",
        model="claude-sonnet-4-5",
        usage={"input_tokens": 11, "output_tokens": 7},
        cost={"usd": 0.00123, "currency": "USD"},
        timestamp="2026-06-08T00:00:00Z",
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True
    # The on-disk state.json must carry the new block.
    state_path = tmp_path / ".metacrucible" / "state.json"
    assert state_path.is_file(), (
        f"state.json must be written to {state_path}; "
        f"state.json contents={state_path.read_text(encoding='utf-8') if state_path.exists() else 'missing'}"
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload.get("current_best_revision") == "rev-001", (
        f"existing state fields must be preserved; got "
        f"current_best_revision={payload.get('current_best_revision')!r}"
    )
    assert payload.get("last_run_id") == "run-prev", (
        f"existing last_run_id must be preserved; got "
        f"last_run_id={payload.get('last_run_id')!r}"
    )
    provider_usage = payload.get("provider_usage")
    assert isinstance(provider_usage, dict), (
        f"provider_usage block must be a dict on state.json; got "
        f"{provider_usage!r}"
    )
    runs = provider_usage.get("runs")
    assert isinstance(runs, list) and len(runs) == 1, (
        f"provider_usage.runs must be a list with one entry; got {runs!r}"
    )
    record = runs[0]
    assert record.get("run_id") == "run-abc"
    assert record.get("provider") == "anthropic"
    assert record.get("model") == "claude-sonnet-4-5"
    assert record.get("ts") == "2026-06-08T00:00:00Z"
    # Raw usage / cost must round-trip verbatim (provider-neutral).
    assert record.get("usage") == {
        "input_tokens": 11,
        "output_tokens": 7,
    }, (
        f"usage payload must round-trip verbatim; got {record.get('usage')!r}"
    )
    assert record.get("cost") == {
        "usd": 0.00123,
        "currency": "USD",
    }, (
        f"cost payload must round-trip verbatim; got {record.get('cost')!r}"
    )

def test_record_provider_run_outcome_missing_usage_and_cost_warns_not_blocks(
    provider_config: Any, tmp_path: Path
) -> None:
    """Issue #18 AC2: missing usage/cost is a warning, never a blocker.

    The recorder must still write ``state.json`` when the caller
    omits ``usage`` or ``cost`` (or both). The result must be
    ``ok=True`` (recording succeeded) with stable warning ids, and
    the ``blockers`` list must be empty (issue AC2: warn, do not
    block). The on-disk record must still appear in
    ``provider_usage.runs`` with ``usage`` / ``cost`` set to
    ``null`` so the schema is stable for downstream consumers.
    """
    import importlib
    storage_mod = importlib.import_module("metacrucible.storage")
    repo = storage_mod.RepositoryStorage(tmp_path)
    result = provider_config.record_provider_run_outcome(
        repo,
        run_id="run-no-usage",
        provider="anthropic",
        model="claude-sonnet-4-5",
    )
    assert isinstance(result, dict)
    # AC2: ok=True because the recording itself succeeded. The
    # missing usage/cost is a warning, not a failure.
    assert result.get("ok") is True, (
        f"missing usage/cost must NOT cause ok=False (AC2); got "
        f"result={result!r}"
    )
    # Both missing fields emit their stable warning id.
    warnings = result.get("warnings")
    assert isinstance(warnings, list) and warnings, (
        f"missing usage/cost must emit at least one warning; got {warnings!r}"
    )
    warning_ids = [
        w.get("id") for w in warnings if isinstance(w, dict)
    ]
    assert provider_config.USAGE_MISSING_WARNING in warning_ids, (
        f"missing usage must emit stable warning id "
        f"{provider_config.USAGE_MISSING_WARNING!r}; got {warning_ids!r}"
    )
    assert provider_config.COST_MISSING_WARNING in warning_ids, (
        f"missing cost must emit stable warning id "
        f"{provider_config.COST_MISSING_WARNING!r}; got {warning_ids!r}"
    )
    # AC2 (the critical half): blockers must be empty so a missing
    # field is *never* a blocker.
    assert result.get("blockers") == [], (
        f"missing usage/cost must NOT emit blockers (AC2); got "
        f"blockers={result.get('blockers')!r}"
    )
    # The on-disk state.json still carries the run record so a
    # downstream consumer can see the gap.
    state_path = tmp_path / ".metacrucible" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    runs = payload["provider_usage"]["runs"]
    assert len(runs) == 1
    record = runs[0]
    assert record.get("run_id") == "run-no-usage"
    # usage and cost are recorded as None so the schema is stable
    # (the field is always present; the value is the gap signal).
    assert record.get("usage") is None, (
        f"missing usage must be recorded as null; got "
        f"usage={record.get('usage')!r}"
    )
    assert record.get("cost") is None, (
        f"missing cost must be recorded as null; got "
        f"cost={record.get('cost')!r}"
    )


def test_record_provider_run_outcome_only_usage_missing_warns_only_usage(
    provider_config: Any, tmp_path: Path
) -> None:
    """AC2 granularity: only the missing field warns, not the present one.

    When the caller supplies cost but omits usage, the result
    carries exactly the USAGE_MISSING_WARNING — not the
    COST_MISSING_WARNING. The on-disk record reflects the same
    asymmetry: usage is null, cost round-trips.
    """
    import importlib
    storage_mod = importlib.import_module("metacrucible.storage")
    repo = storage_mod.RepositoryStorage(tmp_path)
    result = provider_config.record_provider_run_outcome(
        repo,
        run_id="run-only-cost",
        provider="openai_compatible",
        model="gpt-4o",
        cost={"usd": 0.0042, "currency": "USD"},
    )
    assert result.get("ok") is True
    warning_ids = [
        w.get("id") for w in result.get("warnings", [])
        if isinstance(w, dict)
    ]
    assert provider_config.USAGE_MISSING_WARNING in warning_ids, (
        f"missing usage must emit USAGE_MISSING_WARNING; got {warning_ids!r}"
    )
    assert provider_config.COST_MISSING_WARNING not in warning_ids, (
        f"present cost must NOT emit COST_MISSING_WARNING; got {warning_ids!r}"
    )
    state_path = tmp_path / ".metacrucible" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["provider_usage"]["runs"][0]
    assert record.get("usage") is None
    assert record.get("cost") == {"usd": 0.0042, "currency": "USD"}

def test_record_provider_run_outcome_no_cost_cap_is_enforced(
    provider_config: Any, tmp_path: Path
) -> None:
    """Issue #18 AC3: no hard cost cap is introduced.

    The recorder must accept an arbitrarily large cost payload
    without comparing it to any budget, threshold, or limit and
    without raising. The result is ``ok=True`` and the run is
    written to state.json verbatim. This is the negative-space
    companion to AC2: the recorder is observation, not
    enforcement, on both the missing-field axis and the
    over-budget axis.
    """
    import importlib
    storage_mod = importlib.import_module("metacrucible.storage")
    repo = storage_mod.RepositoryStorage(tmp_path)
    huge_cost = {"usd": 9_999_999.99, "currency": "USD"}
    huge_usage = {
        "input_tokens": 10**9,
        "output_tokens": 10**9,
    }
    result = provider_config.record_provider_run_outcome(
        repo,
        run_id="run-huge",
        provider="anthropic",
        model="claude-opus-4",
        usage=huge_usage,
        cost=huge_cost,
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True, (
        f"a huge cost payload must NOT be refused (AC3); got result={result!r}"
    )
    assert result.get("blockers") == [], (
        f"a huge cost payload must NOT emit blockers (AC3); got "
        f"blockers={result.get('blockers')!r}"
    )
    # The cost is recorded verbatim. No threshold, no clamp, no
    # redaction.
    state_path = tmp_path / ".metacrucible" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["provider_usage"]["runs"][0]
    assert record.get("cost") == huge_cost, (
        f"huge cost must round-trip verbatim (no cap, no clamp); got "
        f"cost={record.get('cost')!r}"
    )
    assert record.get("usage") == huge_usage, (
        f"huge usage must round-trip verbatim; got "
        f"usage={record.get('usage')!r}"
    )


def test_record_provider_run_outcome_is_idempotent_per_run_id(
    provider_config: Any, tmp_path: Path
) -> None:
    """Re-recording the same run_id replaces the previous entry, not appends.

    A retry of the same provider run (e.g. transient network error
    followed by a successful retry) must not double-count tokens
    or cost in ``state.json``. The recorder must treat ``run_id``
    as the stable key: a second call with the same ``run_id``
    replaces the previous record and keeps the runs list at length
    1.
    """
    import importlib
    storage_mod = importlib.import_module("metacrucible.storage")
    repo = storage_mod.RepositoryStorage(tmp_path)
    first = provider_config.record_provider_run_outcome(
        repo,
        run_id="run-retry",
        provider="anthropic",
        model="claude-sonnet-4-5",
        usage={"input_tokens": 10, "output_tokens": 5},
        cost={"usd": 0.0001, "currency": "USD"},
    )
    assert first.get("ok") is True
    second = provider_config.record_provider_run_outcome(
        repo,
        run_id="run-retry",
        provider="anthropic",
        model="claude-sonnet-4-5",
        usage={"input_tokens": 20, "output_tokens": 10},
        cost={"usd": 0.0002, "currency": "USD"},
    )
    assert second.get("ok") is True
    state_path = tmp_path / ".metacrucible" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    runs = payload["provider_usage"]["runs"]
    assert len(runs) == 1, (
        f"re-recording the same run_id must replace, not append; got "
        f"len(runs)={len(runs)}; runs={runs!r}"
    )
    record = runs[0]
    # The *second* call's values win because they reflect the
    # successful retry.
    assert record.get("usage") == {"input_tokens": 20, "output_tokens": 10}
    assert record.get("cost") == {"usd": 0.0002, "currency": "USD"}


def test_record_provider_run_outcome_appends_distinct_run_ids(
    provider_config: Any, tmp_path: Path
) -> None:
    """Distinct run_ids accumulate; the recorder is a runs log, not a singleton."""
    import importlib
    storage_mod = importlib.import_module("metacrucible.storage")
    repo = storage_mod.RepositoryStorage(tmp_path)
    for i in range(3):
        result = provider_config.record_provider_run_outcome(
            repo,
            run_id=f"run-{i}",
            provider="anthropic",
            model="claude-sonnet-4-5",
            usage={"input_tokens": i, "output_tokens": i},
            cost={"usd": 0.001 * i, "currency": "USD"},
        )
        assert result.get("ok") is True
    state_path = tmp_path / ".metacrucible" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    runs = payload["provider_usage"]["runs"]
    assert len(runs) == 3, (
        f"three distinct run_ids must produce three records; got "
        f"len(runs)={len(runs)}; runs={runs!r}"
    )
    run_ids = [r.get("run_id") for r in runs]
    assert run_ids == ["run-0", "run-1", "run-2"], (
        f"records must be ordered by insertion; got run_ids={run_ids!r}"
    )

def test_provider_config_module_exposes_expected_warnings(
    provider_config: Any,
) -> None:
    """Issue #18: module must expose EXPECTED_WARNINGS (machine contract)."""
    assert hasattr(provider_config, "EXPECTED_WARNINGS"), (
        f"{PROVIDER_CONFIG_MODULE!r} must expose EXPECTED_WARNINGS "
        f"(Issue #18); got attributes "
        f"{sorted(a for a in dir(provider_config) if not a.startswith('_'))!r}"
    )
    assert isinstance(provider_config.EXPECTED_WARNINGS, dict)
    assert (
        provider_config.EXPECTED_WARNINGS.get("usage_missing")
        == provider_config.USAGE_MISSING_WARNING
    )
    assert (
        provider_config.EXPECTED_WARNINGS.get("cost_missing")
        == provider_config.COST_MISSING_WARNING
    )


def test_record_provider_run_outcome_accepts_openai_shape(
    provider_config: Any, tmp_path: Path
) -> None:
    """Issue #18: provider-neutral — OpenAI's prompt_tokens/completion_tokens shape.

    The recorder must not normalize or coerce usage/cost payloads.
    Anthropic's ``input_tokens``/``output_tokens`` and OpenAI's
    ``prompt_tokens``/``completion_tokens`` are both accepted and
    round-tripped verbatim so a future ADR can introduce
    provider-specific normalizers without changing this function.
    """
    import importlib
    storage_mod = importlib.import_module("metacrucible.storage")
    repo = storage_mod.RepositoryStorage(tmp_path)
    openai_usage = {
        "prompt_tokens": 42,
        "completion_tokens": 17,
        "total_tokens": 59,
    }
    openai_cost = {
        "input_cost_usd": 0.001,
        "output_cost_usd": 0.003,
        "currency": "USD",
    }
    result = provider_config.record_provider_run_outcome(
        repo,
        run_id="run-openai",
        provider="openai_compatible",
        model="gpt-4o",
        usage=openai_usage,
        cost=openai_cost,
    )
    assert result.get("ok") is True
    state_path = tmp_path / ".metacrucible" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["provider_usage"]["runs"][0]
    assert record.get("usage") == openai_usage, (
        f"OpenAI usage shape must round-trip verbatim; got "
        f"usage={record.get('usage')!r}"
    )
    assert record.get("cost") == openai_cost, (
        f"OpenAI cost shape must round-trip verbatim; got "
        f"cost={record.get('cost')!r}"
    )

# --------------------------------------------------------------------------- #
# Issue #19: provider error taxonomy + bounded retry policy                   #
# --------------------------------------------------------------------------- #
# Issue #19 + ADR 0034: every provider call must go through a stable error
# taxonomy (transient / auth / context overflow / safety refusal / unknown)
# and a bounded retry policy. Transient errors are the only ones that get
# retried; auth, context overflow, and safety refusal are terminal. Every
# retry leaves a public record in the result's ``retries`` list so a
# downstream receipt/evidence recorder can correlate the retries with the
# final outcome — the evidence is never hidden in logs only.

# Mapping from semantic category to the public error class and the stable
# blocker id the retry function must emit. Centralizing the mapping keeps
# the slice-2 parametrized test and the production code in lockstep.
_NON_RETRYABLE_ERROR_CASES: dict[str, tuple[str, str]] = {
    "auth": ("ProviderAuthError", "PROVIDER_AUTH_BLOCKER"),
    "context_overflow": (
        "ProviderContextOverflowError", "PROVIDER_CONTEXT_OVERFLOW_BLOCKER"
    ),
    "safety_refusal": (
        "ProviderSafetyRefusalError", "PROVIDER_SAFETY_REFUSAL_BLOCKER"
    ),
}


def test_call_provider_with_retry_retries_transient_errors(
    provider_config: Any,
) -> None:
    """Issue #19 AC1: a transient provider error is retried.

    A fake ``call_fn`` that raises ``ProviderTransientError`` twice and
    then returns a value must be retried until it succeeds. The result
    must report ``ok=True``, the returned value, and the total number
    of attempts (1 initial + N retries). The retry must be bounded by
    ``max_attempts``; no infinite loops are allowed.
    """
    calls: list[int] = []

    def call_fn() -> dict[str, str]:
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise provider_config.ProviderTransientError(
                f"transient failure #{len(calls)}"
            )
        return {"result": "ok"}

    result = provider_config.call_provider_with_retry(
        call_fn, max_attempts=5
    )
    assert isinstance(result, dict), (
        f"call_provider_with_retry must return a dict result; got "
        f"{type(result).__name__}"
    )
    assert result.get("ok") is True, (
        f"transient errors must be retried; got result={result!r}"
    )
    assert result.get("value") == {"result": "ok"}, (
        f"value must be the successful call_fn return; got "
        f"{result.get('value')!r}"
    )
    assert result.get("attempts") == 3, (
        f"transient retry-then-success must yield attempts=3 "
        f"(2 failures + 1 success); got {result.get('attempts')!r}"
    )
    assert len(calls) == 3, (
        f"call_fn must be invoked exactly 3 times; got {len(calls)} calls"
    )


@pytest.mark.parametrize(
    "category",
    sorted(_NON_RETRYABLE_ERROR_CASES.keys()),
    ids=sorted(_NON_RETRYABLE_ERROR_CASES.keys()),
)
def test_call_provider_with_retry_does_not_retry_non_transient_errors(
    provider_config: Any, category: str
) -> None:
    """Issue #19 AC2: auth/context-overflow/safety-refusal are not retried.

    A fake ``call_fn`` that raises one of the non-retryable
    provider error classes must be invoked exactly once. The
    result must report ``ok=False``, ``attempts == 1``, and a
    stable blocker id identifying the error category. The retry
    policy must never silently retry these categories.
    """
    error_class_name, blocker_const_name = _NON_RETRYABLE_ERROR_CASES[category]
    error_cls = getattr(provider_config, error_class_name)
    expected_blocker_id = getattr(provider_config, blocker_const_name)

    calls: list[int] = []

    def call_fn() -> None:
        calls.append(1)
        raise error_cls(f"non-retryable {category}")

    result = provider_config.call_provider_with_retry(
        call_fn, max_attempts=5
    )
    assert isinstance(result, dict), (
        f"call_provider_with_retry must return a dict result; got "
        f"{type(result).__name__}"
    )
    assert result.get("ok") is False, (
        f"{error_class_name} must yield ok=False; got result={result!r}"
    )
    assert result.get("attempts") == 1, (
        f"{error_class_name} must NOT be retried (attempts=1); got "
        f"{result.get('attempts')!r}"
    )
    assert len(calls) == 1, (
        f"call_fn must be invoked exactly once for {error_class_name}; "
        f"got {len(calls)} calls"
    )
    blockers = result.get("blockers")
    assert isinstance(blockers, list) and blockers, (
        f"non-retryable {error_class_name} must emit at least one blocker; "
        f"got {blockers!r}"
    )
    blocker_ids = [
        b.get("id") for b in blockers if isinstance(b, dict)
    ]
    assert expected_blocker_id in blocker_ids, (
        f"non-retryable {error_class_name} must emit blocker id "
        f"{expected_blocker_id!r}; got {blocker_ids!r}"
    )
    for blocker in blockers:
        assert isinstance(blocker, dict), (
            f"every blocker must be a dict; got {type(blocker).__name__}"
        )
        assert (
            isinstance(blocker.get("id"), str) and blocker["id"]
        ), (
            f"every blocker must have a non-empty id; got {blocker!r}"
        )
        assert (
            isinstance(blocker.get("message"), str)
            and blocker["message"]
        ), (
            f"every blocker must carry a non-empty message; got "
            f"{blocker!r}"
        )


def test_call_provider_with_retry_records_retry_evidence(
    provider_config: Any,
) -> None:
    """Issue #19 AC3: retry evidence is recorded on the public result.

    When a transient error is retried until the bounded budget
    is exhausted, every attempt must leave a record in the
    result's ``retries`` list. Each record exposes the attempt
    number, the error category, the error class name, and the
    error message. The evidence is part of the public result —
    a downstream receipt/evidence recorder can read it without
    scraping logs.
    """
    def call_fn() -> None:
        raise provider_config.ProviderTransientError("boom")

    result = provider_config.call_provider_with_retry(
        call_fn, max_attempts=3
    )
    assert isinstance(result, dict), (
        f"call_provider_with_retry must return a dict result; got "
        f"{type(result).__name__}"
    )
    assert result.get("ok") is False, (
        f"persistent transient errors must yield ok=False; got "
        f"result={result!r}"
    )
    assert result.get("attempts") == 3, (
        f"max_attempts=3 must yield attempts=3; got "
        f"{result.get('attempts')!r}"
    )
    retries = result.get("retries")
    assert isinstance(retries, list), (
        f"retry evidence must be exposed as a 'retries' list on the "
        f"public result (Issue #19 AC3); got {retries!r}"
    )
    assert len(retries) == 3, (
        f"retries list must have one entry per attempt; got "
        f"{len(retries)} entries for attempts=3"
    )
    for i, entry in enumerate(retries, start=1):
        assert isinstance(entry, dict), (
            f"each retry evidence entry must be a dict; got "
            f"{type(entry).__name__}"
        )
        assert entry.get("attempt") == i, (
            f"retry evidence entry must carry its attempt number; got "
            f"entry={entry!r} at index {i-1}"
        )
        assert entry.get("category") == "transient", (
            f"retry evidence must record the error category; got "
            f"entry={entry!r}"
        )
        assert entry.get("error_class") == "ProviderTransientError", (
            f"retry evidence must record the error class name; got "
            f"entry={entry!r}"
        )
        assert entry.get("message") == "boom", (
            f"retry evidence must record the error message; got "
            f"entry={entry!r}"
        )
    blockers = result.get("blockers")
    assert isinstance(blockers, list) and blockers, (
        f"retry-exhausted failure must emit a blocker; got {blockers!r}"
    )
    blocker_ids = [
        b.get("id") for b in blockers if isinstance(b, dict)
    ]
    assert (
        provider_config.PROVIDER_RETRY_EXHAUSTED_BLOCKER in blocker_ids
    ), (
        f"retry-exhausted must emit blocker id "
        f"{provider_config.PROVIDER_RETRY_EXHAUSTED_BLOCKER!r}; got "
        f"{blocker_ids!r}"
    )


# --------------------------------------------------------------------------- #
# Issue #20: two-independent-LLM-judge evaluator                              #
# --------------------------------------------------------------------------- #
# Issue #20 + ADR 0010/0011 require two independent control-plane LLM judges
# for execution evaluation. The judges share the same model selection
# (``control_plane.judge``) but each gets its own context, its own call, and
# its own evidence record. The judge evaluator must NOT pull credentials or
# runtime config from ``runtime_adapters`` (the target runtime) and must NOT
# pick the optimizer selection as a fallback. The two evidence records must
# be independent (distinct dicts, distinct call contexts, distinct attempts)
# so downstream evidence recording can correlate each judge with its
# call without aliasing.
#
# Tests inject two deterministic ``call_fn`` doubles; production code calls
# real LLM SDKs. No mocks are used.

# Stable blocker id emitted by ``run_judge_evaluator`` when the judge
# selection is missing or the call_fns sequence is malformed. Part of
# the machine contract; renaming is a breaking change.
EXPECTED_JUDGE_EVALUATOR_BLOCKER: str = (
    "provider-config-judge-evaluator-blocked"
)


def _build_dual_judge_call_fns(
    responses: list[Any],
) -> tuple[list[Any], list[dict[str, Any]], list[int]]:
    """Build two independent ``call_fn`` doubles that record their context.

    The returned tuple is ``(call_fns, recorded_contexts, call_counts)``:

    * ``call_fns`` is a list of two callables; each is invoked exactly
      once in order. They are NOT aliases — the production code must
      see two distinct callables.
    * ``recorded_contexts`` is a list that grows in order of invocation
      so a test can assert what context each judge actually saw.
    * ``call_counts`` is a list of two ints so a test can assert each
      judge was invoked exactly once (and that one judge did not
      silently short-circuit the other).

    Each ``call_fn`` pops the next response from ``responses``; a
    misconfigured test that over-invokes the judges raises
    ``AssertionError`` rather than silently passing.
    """
    recorded_contexts: list[dict[str, Any]] = []
    call_counts_local: list[int] = [0, 0]
    queue = list(responses)

    def make_call_fn(slot: int) -> Any:
        def call_fn(context: Any) -> Any:
            call_counts_local[slot] += 1
            recorded_contexts.append(context)
            if not queue:
                raise AssertionError(
                    f"judge call_fn[{slot}] invoked more times than "
                    f"queued responses"
                )
            return queue.pop(0)
        return call_fn

    return [make_call_fn(0), make_call_fn(1)], recorded_contexts, call_counts_local


def test_provider_config_module_exposes_judge_evaluator_surface(
    provider_config: Any,
) -> None:
    """Issue #20: module must expose the judge-evaluator entry point + blocker id."""
    for name in (
        "JUDGE_EVALUATOR_BLOCKER",
        "run_judge_evaluator",
    ):
        assert hasattr(provider_config, name), (
            f"{PROVIDER_CONFIG_MODULE!r} must expose {name!r} (Issue #20); "
            f"got attributes "
            f"{sorted(a for a in dir(provider_config) if not a.startswith('_'))!r}"
        )


def test_judge_evaluator_blocker_id_matches_pinned_value(
    provider_config: Any,
) -> None:
    """The judge-evaluator blocker id is part of the machine contract."""
    assert (
        provider_config.JUDGE_EVALUATOR_BLOCKER
        == EXPECTED_JUDGE_EVALUATOR_BLOCKER
    ), (
        f"JUDGE_EVALUATOR_BLOCKER must be exactly "
        f"{EXPECTED_JUDGE_EVALUATOR_BLOCKER!r}; got "
        f"{provider_config.JUDGE_EVALUATOR_BLOCKER!r}"
    )
    assert isinstance(provider_config.EXPECTED_BLOCKERS, dict)
    assert (
        provider_config.EXPECTED_BLOCKERS.get("judge_evaluator")
        == EXPECTED_JUDGE_EVALUATOR_BLOCKER
    ), (
        f"EXPECTED_BLOCKERS must include the judge_evaluator entry with "
        f"the stable id; got {provider_config.EXPECTED_BLOCKERS!r}"
    )


def test_run_judge_evaluator_invokes_two_distinct_call_fns(
    provider_config: Any,
) -> None:
    """Issue #20 AC3: the two judges are two independent calls.

    The evaluator must call each of the two call_fns exactly once. If
    one call_fn is invoked twice (alias) or the second is never called
    (short-circuit), the judges are not independent and the AC fails.
    """
    resolved = provider_config.resolve_provider_config()
    assert resolved.get("ok") is True, (
        f"baseline resolve must succeed; got {resolved!r}"
    )
    call_fns, _recorded, call_counts = _build_dual_judge_call_fns(
        [{"judgment": "A"}, {"judgment": "B"}]
    )
    result = provider_config.run_judge_evaluator(
        resolved["config"],
        trajectory_digest={"summary": "artifact did X"},
        rubric={"criteria": "1: clarity"},
        call_fns=call_fns,
    )
    assert isinstance(result, dict), (
        f"run_judge_evaluator must return a dict; got {type(result).__name__}"
    )
    assert call_counts == [1, 1], (
        f"each judge call_fn must be invoked exactly once; got counts="
        f"{call_counts!r}"
    )


def test_run_judge_evaluator_emits_two_independent_judge_evidence_records(
    provider_config: Any,
) -> None:
    """Issue #20 AC3: the two evidence records are independent dicts.

    The two ``judge_evidence`` entries must be distinct dict objects
    (not aliases of the same dict, not shared mutable state). The
    index/role fields must distinguish them and the recorded value
    must reflect the call_fn that produced it.
    """
    resolved = provider_config.resolve_provider_config()
    call_fns, _recorded, _counts = _build_dual_judge_call_fns(
        [{"judgment": "A"}, {"judgment": "B"}]
    )
    result = provider_config.run_judge_evaluator(
        resolved["config"],
        trajectory_digest={"summary": "artifact did X"},
        rubric={"criteria": "1: clarity"},
        call_fns=call_fns,
    )
    evidence = result.get("judge_evidence")
    assert isinstance(evidence, dict), (
        f"judge_evidence must be a dict at a deterministic place on the "
        f"result; got {evidence!r}"
    )
    assert set(evidence.keys()) >= {"judge_1", "judge_2"}, (
        f"judge_evidence must carry two deterministic keys judge_1 and "
        f"judge_2; got {sorted(evidence.keys())!r}"
    )
    record_1 = evidence["judge_1"]
    record_2 = evidence["judge_2"]
    assert isinstance(record_1, dict) and isinstance(record_2, dict), (
        f"each judge_evidence record must be a dict; got "
        f"{type(record_1).__name__} and {type(record_2).__name__}"
    )
    assert record_1 is not record_2, (
        f"the two judge_evidence records must be distinct dict objects, "
        f"not the same object (Issue #20 AC3); got id(record_1)="
        f"{id(record_1)} id(record_2)={id(record_2)}"
    )
    assert record_1.get("value") == {"judgment": "A"}, (
        f"judge_1 record must carry the response of the first call_fn; "
        f"got {record_1.get('value')!r}"
    )
    assert record_2.get("value") == {"judgment": "B"}, (
        f"judge_2 record must carry the response of the second call_fn; "
        f"got {record_2.get('value')!r}"
    )
    # Mutating one record must not affect the other (independent state).
    record_1["value"] = {"judgment": "MUTATED"}
    assert record_2.get("value") == {"judgment": "B"}, (
        f"judge_2 record must not share mutable state with judge_1; "
        f"mutating judge_1.value leaked into judge_2.value="
        f"{record_2.get('value')!r}"
    )


def test_run_judge_evaluator_passes_independent_contexts_to_each_judge(
    provider_config: Any,
) -> None:
    """Issue #20 AC3: the two judges receive distinct context objects.

    The context the function passes to each ``call_fn`` must be a
    fresh dict per judge. Aliasing the same context object across
    both judges would couple their inputs and break the
    "independent" guarantee.
    """
    resolved = provider_config.resolve_provider_config()
    call_fns, recorded_contexts, _counts = _build_dual_judge_call_fns(
        [{"judgment": "A"}, {"judgment": "B"}]
    )
    result = provider_config.run_judge_evaluator(
        resolved["config"],
        trajectory_digest={"summary": "artifact did X"},
        rubric={"criteria": "1: clarity"},
        call_fns=call_fns,
    )
    assert result.get("ok") is True, (
        f"two-passing-judges must yield ok=True; got result={result!r}"
    )
    assert len(recorded_contexts) == 2, (
        f"both judges must have been invoked; got {len(recorded_contexts)} "
        f"recorded contexts"
    )
    ctx_1 = recorded_contexts[0]
    ctx_2 = recorded_contexts[1]
    assert isinstance(ctx_1, dict) and isinstance(ctx_2, dict), (
        f"each judge context must be a mapping; got "
        f"{type(ctx_1).__name__} and {type(ctx_2).__name__}"
    )
    assert ctx_1 is not ctx_2, (
        f"the two judge contexts must be distinct dict objects, not the "
        f"same object (Issue #20 AC3); got id(ctx_1)={id(ctx_1)} "
        f"id(ctx_2)={id(ctx_2)}"
    )
    # Mutating one context must not leak into the other.
    ctx_1["_leak_marker"] = "MUTATED"
    assert "_leak_marker" not in ctx_2, (
        f"judge contexts must not share mutable state; mutating ctx_1 "
        f"leaked into ctx_2: {ctx_2!r}"
    )


def test_run_judge_evaluator_does_not_use_optimizer_selection(
    provider_config: Any,
) -> None:
    """Issue #20 AC2: judge selection is independent from optimizer.

    Even if the optimizer selection is malformed, missing, or points
    at a provider that does not exist, the judge evaluator must
    succeed because it never reads ``control_plane.optimizer``. A
    same-context self-evaluation bias is exactly what ADR 0011
    forbids.
    """
    user = {
        "control_plane": {
            "judge": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
            },
            # Optimizer points at a provider the catalog does not
            # contain. The judge evaluator must still work.
            "optimizer": {
                "provider": "DOES_NOT_EXIST",
                "model": "broken",
            },
        },
    }
    resolved = provider_config.resolve_provider_config(user=user)
    assert resolved.get("ok") is True, (
        f"resolver must accept a malformed optimizer selection (the "
        f"judge evaluator must not depend on it); got {resolved!r}"
    )
    call_fns, _recorded, _counts = _build_dual_judge_call_fns(
        [{"judgment": "ok"}, {"judgment": "fine"}]
    )
    result = provider_config.run_judge_evaluator(
        resolved["config"],
        trajectory_digest={"summary": "artifact did X"},
        rubric={"criteria": "1: clarity"},
        call_fns=call_fns,
    )
    assert result.get("ok") is True, (
        f"judge evaluator must succeed independently of optimizer "
        f"selection; got result={result!r}"
    )
    evidence = result.get("judge_evidence")
    assert isinstance(evidence, dict)
    for key in ("judge_1", "judge_2"):
        record = evidence[key]
        assert record.get("provider") == "anthropic", (
            f"{key} evidence must use the judge selection "
            f"(provider=anthropic); got {record.get('provider')!r} "
            f"(Issue #20 AC2: judge context independent from optimizer)"
        )
        assert record.get("model") == "claude-sonnet-4-5", (
            f"{key} evidence must use the judge selection model; got "
            f"{record.get('model')!r}"
        )


def test_run_judge_evaluator_does_not_use_runtime_adapters(
    provider_config: Any,
) -> None:
    """Issue #20 AC1: judge is not the target runtime.

    The ``runtime_adapters`` section configures target execution
    (the Claude Code binary, etc.). The judge evaluator must not
    read or depend on it; a missing or malformed runtime_adapters
    must not block the judge from running.
    """
    user = {
        "control_plane": {
            "judge": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
            },
        },
        "runtime_adapters": {
            # Garbage adapter — must not influence the judge path.
            "claude_code": {
                "binary": "definitely-not-claude",
                "mode": "subscription",
                "_judge_attempt": "this must not leak into the judge call",
            },
        },
    }
    resolved = provider_config.resolve_provider_config(user=user)
    assert resolved.get("ok") is True, (
        f"baseline resolve must succeed even with weird runtime_adapters; "
        f"got {resolved!r}"
    )
    call_fns, recorded_contexts, _counts = _build_dual_judge_call_fns(
        [{"judgment": "ok"}, {"judgment": "fine"}]
    )
    result = provider_config.run_judge_evaluator(
        resolved["config"],
        trajectory_digest={"summary": "artifact did X"},
        rubric={"criteria": "1: clarity"},
        call_fns=call_fns,
    )
    assert result.get("ok") is True, (
        f"judge evaluator must succeed independently of runtime_adapters; "
        f"got result={result!r}"
    )
    # The recorded context for each judge must not contain any
    # runtime_adapters field (Issue #20 AC1).
    for ctx in recorded_contexts:
        assert isinstance(ctx, dict)
        assert "runtime_adapters" not in ctx, (
            f"judge context must not include runtime_adapters (the judge "
            f"is not the target runtime); got context={ctx!r}"
        )
        assert "runtime_adapter" not in ctx, (
            f"judge context must not include any runtime_adapter field; "
            f"got context={ctx!r}"
        )


def test_run_judge_evaluator_blocks_when_judge_selection_missing(
    provider_config: Any,
) -> None:
    """Issue #20: a missing judge selection is a stable blocker, never an exception."""
    user = {
        "control_plane": {
            # judge deliberately nulled out (override with None).
            # The user layer replaces the default control_plane.judge
            # selection with None; the resolver still passes (it only
            # rejects runtime-leak and secret fields, not a None
            # selection); the judge evaluator then blocks.
            "judge": None,
            "optimizer": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
            },
        },
    }
    resolved = provider_config.resolve_provider_config(user=user)
    assert resolved.get("ok") is True, (
        f"resolver must accept a None judge selection; got {resolved!r}"
    )
    assert resolved["config"]["control_plane"].get("judge") is None, (
        f"resolved config must have control_plane.judge=None; got "
        f"{resolved['config'].get('control_plane')!r}"
    )
    call_fns, _recorded, _counts = _build_dual_judge_call_fns(
        [{"judgment": "A"}, {"judgment": "B"}]
    )
    result = provider_config.run_judge_evaluator(
        resolved["config"],
        trajectory_digest={"summary": "x"},
        rubric={"criteria": "y"},
        call_fns=call_fns,
    )
    assert isinstance(result, dict)
    assert result.get("ok") is False, (
        f"missing judge selection must yield ok=False; got {result!r}"
    )
    assert (
        EXPECTED_JUDGE_EVALUATOR_BLOCKER in _blocker_ids(result)
    ), (
        f"missing judge selection must emit blocker id "
        f"{EXPECTED_JUDGE_EVALUATOR_BLOCKER!r}; got "
        f"blocker_ids={_blocker_ids(result)!r}"
    )
    # Neither judge should have been invoked when the selection is
    # missing — evidence should not be fabricated.
    evidence = result.get("judge_evidence")
    assert evidence is None or evidence == {}, (
        f"no judge evidence must be produced when the selection is "
        f"missing; got {evidence!r}"
    )


def test_run_judge_evaluator_requires_exactly_two_call_fns(
    provider_config: Any,
) -> None:
    """Issue #20: the judge evaluator is two-judge, not one, not three."""
    resolved = provider_config.resolve_provider_config()
    call_fns, _recorded, _counts = _build_dual_judge_call_fns(
        [{"judgment": "A"}, {"judgment": "B"}]
    )
    # 1 call_fn is a contract violation — the evaluator is two-judge.
    result_one = provider_config.run_judge_evaluator(
        resolved["config"],
        trajectory_digest={"summary": "x"},
        rubric={"criteria": "y"},
        call_fns=call_fns[:1],
    )
    assert result_one.get("ok") is False, (
        f"exactly one call_fn must yield ok=False; got {result_one!r}"
    )
    assert (
        EXPECTED_JUDGE_EVALUATOR_BLOCKER in _blocker_ids(result_one)
    ), (
        f"exactly one call_fn must emit the stable blocker id; got "
        f"blocker_ids={_blocker_ids(result_one)!r}"
    )
    # 3 call_fns is also a contract violation.
    result_three = provider_config.run_judge_evaluator(
        resolved["config"],
        trajectory_digest={"summary": "x"},
        rubric={"criteria": "y"},
        call_fns=call_fns + [call_fns[0]],
    )
    assert result_three.get("ok") is False, (
        f"three call_fns must yield ok=False; got {result_three!r}"
    )
    assert (
        EXPECTED_JUDGE_EVALUATOR_BLOCKER in _blocker_ids(result_three)
    ), (
        f"three call_fns must emit the stable blocker id; got "
        f"blocker_ids={_blocker_ids(result_three)!r}"
    )
