"""Resolve layered control-plane provider config (Issue #15, ADR 0034).

Per ADR 0034 the control plane, the providers catalog, and the
runtime-adapter configuration are three independent top-level
sections. The control plane picks ``judge`` and ``optimizer`` as
``{provider, model}`` pairs; the ``providers`` catalog holds
Anthropic- and OpenAI-compatible provider shapes; ``runtime_adapters``
configures target execution (e.g. the Claude Code binary and mode)
and must not be allowed to satisfy or overwrite the control-plane
selection.

Provider credentials are referenced only through ``api_key_env``;
direct ``api_key`` / ``token`` / ``secret`` / ``password`` fields
are rejected anywhere in the merged config, and the rejection
message never includes the rejected value (ADR 0034).

Public surface
--------------

The module exposes:

* :data:`DEFAULT_PROVIDER_CONFIG` — built-in defaults for
  ``control_plane``, ``providers``, and ``runtime_adapters``.
* :data:`SECRET_FIELD_BLOCKER` /
  :data:`RUNTIME_ADAPTER_LEAK_BLOCKER` /
  :data:`EXPECTED_BLOCKERS` — stable blocker ids the resolver
  emits on validation failure (machine contract; do not rename).
* :func:`resolve_provider_config` — accept layered dict configs
  in precedence (built-in defaults < user < project < CLI) and
  return the resolved config or a list of blockers.

References
----------
- ADR 0034 (control-plane provider configuration).
- Issue #15 acceptance criteria.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

__all__ = [
    "EXPECTED_BLOCKERS",
    "SECRET_FIELD_BLOCKER",
    "RUNTIME_ADAPTER_LEAK_BLOCKER",
    "DEFAULT_PROVIDER_CONFIG",
    "resolve_provider_config",
]

# --------------------------------------------------------------------------- #
# Top-level sections a config dict must contain. Anything else at the top     #
# level is forwarded through unchanged so future ADR revisions can add new   #
# sections without breaking the MVP.                                         #
# --------------------------------------------------------------------------- #

_TOP_LEVEL_SECTIONS: tuple[str, ...] = (
    "control_plane",
    "providers",
    "runtime_adapters",
)

# Field names that signal direct credential material. ``api_key_env``
# is the only supported way to reference a credential; direct
# ``api_key`` / ``token`` / ``secret`` / ``password`` fields are
# rejected (ADR 0034). The rule is "exact match or ends with
# ``_<secret_name>``" so legitimate field names like ``api_key_env``
# (ends with ``_env``), ``model_id``, or ``client_id`` are
# unaffected.
_SECRET_FIELD_NAMES: tuple[str, ...] = (
    "api_key",
    "token",
    "secret",
    "password",
)

# Control-plane keys that have no business appearing inside
# ``runtime_adapters`` (Issue #15 AC3 + ADR 0034). When these
# keys appear under a runtime-adapter entry the resolver refuses
# the request with the runtime-leak blocker id.
_RUNTIME_LEAK_KEYS: tuple[str, ...] = (
    "judge",
    "optimizer",
    "providers",
)

# Stable blocker ids (machine contract). Tests and downstream
# automation branch on these exact strings; renaming an id is a
# breaking change and must be paired with a migration plan.
SECRET_FIELD_BLOCKER: str = "provider-config-secret-field-rejected"
RUNTIME_ADAPTER_LEAK_BLOCKER: str = (
    "provider-config-runtime-adapter-control-plane-leak"
)

EXPECTED_BLOCKERS: dict[str, str] = {
    "secret_field": SECRET_FIELD_BLOCKER,
    "runtime_leak": RUNTIME_ADAPTER_LEAK_BLOCKER,
}

# Built-in defaults for the resolver. The defaults are the
# shipped out-of-the-box experience; user/project/CLI overrides
# win when present (ADR 0034).
DEFAULT_PROVIDER_CONFIG: dict[str, Any] = {
    "control_plane": {
        "judge": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
        },
        "optimizer": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
        },
    },
    "providers": {
        "anthropic": {
            "type": "anthropic",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
        "openai_compatible": {
            "type": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
        },
    },
    "runtime_adapters": {
        "claude_code": {
            "binary": "claude",
            "mode": "subscription",
        },
    },
}


# --------------------------------------------------------------------------- #
# Internal: classification and merge helpers                                  #
# --------------------------------------------------------------------------- #


def _is_secret_field_name(field_name: str) -> bool:
    """Return True iff ``field_name`` names direct credential material.

    The rule is "exact match or ends with ``_<secret>``" so
    legitimate names like ``api_key_env``, ``client_id``, or
    ``model_name`` are unaffected.
    """
    if not isinstance(field_name, str):
        return False
    for secret in _SECRET_FIELD_NAMES:
        if field_name == secret or field_name.endswith("_" + secret):
            return True
    return False


def _blocker(blocker_id: str, message: str) -> dict[str, str]:
    """Return a single ``{id, message}`` blocker entry."""
    return {"id": blocker_id, "message": message}


def _deep_merge(
    base: dict[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` and return ``base``.

    Mapping values are merged key-by-key; non-mapping values from
    ``override`` replace the base value. ``base`` is mutated and
    returned for convenience. This keeps the precedence semantics
    simple: a later layer's value fully replaces a non-mapping
    base value, while a later mapping recursively extends the
    base.
    """
    for key, override_value in override.items():
        base_value = base.get(key)
        if (
            isinstance(base_value, Mapping)
            and isinstance(override_value, Mapping)
        ):
            _deep_merge(base_value, override_value)
        else:
            base[key] = override_value
    return base


def _collect_secret_field_violations(
    node: Any,
    path: str,
    out: list[tuple[str, str]],
) -> None:
    """Walk ``node`` and append ``(path, field_name)`` for every secret field.

    The walk recurses into mappings and into lists. The ``path``
    argument is the dotted JSON-path-style location of ``node``
    inside the merged config (empty string at the root). The
    field name reported is the offending key, not the path, so
    the blocker message can name the field directly without ever
    quoting the value (ADR 0034).
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            if _is_secret_field_name(str(key)):
                out.append((child_path, str(key)))
            _collect_secret_field_violations(value, child_path, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child_path = f"{path}[{index}]"
            _collect_secret_field_violations(value, child_path, out)


def _collect_runtime_leak_violations(
    node: Any,
    path: str,
    out: list[tuple[str, str]],
) -> None:
    """Walk ``node`` and collect control-plane key leaks inside ``runtime_adapters``.

    A "leak" is a key in :data:`_RUNTIME_LEAK_KEYS` (``judge``,
    ``optimizer``, ``providers``) found at least one level deep
    inside the ``runtime_adapters`` subtree. The top-level
    ``runtime_adapters`` section name itself is not a leak; only
    its descendant keys are.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (
                key in _RUNTIME_LEAK_KEYS
                and child_path.startswith("runtime_adapters.")
            ):
                out.append((child_path, str(key)))
            _collect_runtime_leak_violations(value, child_path, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child_path = f"{path}[{index}]"
            _collect_runtime_leak_violations(value, child_path, out)


# --------------------------------------------------------------------------- #
# Public: resolve_provider_config                                             #
# --------------------------------------------------------------------------- #


def resolve_provider_config(
    *,
    defaults: Mapping[str, Any] | None = None,
    user: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
    cli: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve layered provider config in precedence: defaults < user < project < cli.

    Returns a result dict::

        {
            "ok": bool,
            "config": {
                "control_plane": {...},
                "providers": {...},
                "runtime_adapters": {...},
            },
            "blockers": [{"id": str, "message": str}],
        }

    The resolver refuses any direct ``api_key`` / ``token`` /
    ``secret`` / ``password`` field anywhere in the merged config
    and refuses ``runtime_adapters`` entries that try to satisfy
    the control-plane selection by injecting ``judge`` /
    ``optimizer`` / ``providers`` keys (Issue #15 AC3, ADR 0034).

    On a blocked resolve the returned ``config`` is ``{}`` and
    ``blockers`` is the list of violations, each with a stable
    id. The error messages do not include the rejected field
    value (ADR 0034).
    """
    # Deep-copy the defaults so a mutation from the deep merge
    # cannot leak into the module-level constant.
    merged: dict[str, Any] = copy.deepcopy(DEFAULT_PROVIDER_CONFIG)
    for layer in (defaults, user, project, cli):
        if layer is None:
            continue
        if not isinstance(layer, Mapping):
            return {
                "ok": False,
                "config": {},
                "blockers": [
                    _blocker(
                        SECRET_FIELD_BLOCKER,
                        "config layer must be a mapping; "
                        f"got {type(layer).__name__} (Issue #15)",
                    )
                ],
            }
        _deep_merge(merged, layer)

    blockers: list[dict[str, str]] = []

    secret_violations: list[tuple[str, str]] = []
    _collect_secret_field_violations(merged, "", secret_violations)
    for field_path, field_name in secret_violations:
        blockers.append(
            _blocker(
                SECRET_FIELD_BLOCKER,
                (
                    f"config field {field_path!r} uses a direct credential "
                    f"name {field_name!r}; use 'api_key_env' to reference "
                    "a credential by environment variable "
                    "(ADR 0034, Issue #15 AC1)"
                ),
            )
        )

    leak_violations: list[tuple[str, str]] = []
    _collect_runtime_leak_violations(merged, "", leak_violations)
    for field_path, field_name in leak_violations:
        blockers.append(
            _blocker(
                RUNTIME_ADAPTER_LEAK_BLOCKER,
                (
                    f"{field_path!r} sets {field_name!r} inside "
                    "runtime_adapters; runtime adapter config must not "
                    "satisfy or overwrite control-plane provider config "
                    "(Issue #15 AC3, ADR 0034)"
                ),
            )
        )

    if blockers:
        return {"ok": False, "config": {}, "blockers": blockers}
    return {"ok": True, "config": merged, "blockers": []}
