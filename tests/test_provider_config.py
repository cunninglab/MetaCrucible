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

These tests are the red step. They pin the public surface and the
acceptance behaviors from issue #15. Once ``metacrucible.provider_config``
exists, the tests turn green.

References
----------
- ADR 0034 (control-plane provider configuration).
- Issue #15 acceptance criteria.
"""
from __future__ import annotations

import importlib
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
