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
  :data:`STRUCTURED_OUTPUT_PROBE_BLOCKER` /
  :data:`STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER` /
  :data:`EXPECTED_BLOCKERS` — stable blocker ids the resolver
  emits on validation failure (machine contract; do not rename).
* :func:`resolve_provider_config` — accept layered dict configs
  in precedence (built-in defaults < user < project < CLI) and
  return the resolved config or a list of blockers.
* :func:`run_structured_output_probe` — run the structured-output
  capability probe for ``judge`` and ``optimizer`` (Issue #16).
* :func:`call_structured` — call an LLM and validate its response
  against a JSON Schema with bounded repair retries (Issue #17).

References
----------
- ADR 0034 (control-plane provider configuration).
- Issue #15, Issue #16, Issue #17 acceptance criteria.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

__all__ = [
    "EXPECTED_BLOCKERS",
    "EXPECTED_WARNINGS",
    "SECRET_FIELD_BLOCKER",
    "RUNTIME_ADAPTER_LEAK_BLOCKER",
    "STRUCTURED_OUTPUT_PROBE_BLOCKER",
    "STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER",
    "USAGE_MISSING_WARNING",
    "COST_MISSING_WARNING",
    "PROVIDER_USAGE_SCHEMA_VERSION",
    "DEFAULT_PROVIDER_CONFIG",
    "resolve_provider_config",
    "run_structured_output_probe",
    "call_structured",
    "record_provider_run_outcome",
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
# Stable blocker id emitted by :func:`run_structured_output_probe` when any
# role's structured-output capability probe fails (Issue #16, ADR 0034).
# The id is part of the machine contract: callers branch on it verbatim.
STRUCTURED_OUTPUT_PROBE_BLOCKER: str = (
    "provider-config-structured-output-probe-failed"
)

# Stable blocker id emitted by :func:`call_structured` when a structured
# provider response fails JSON Schema validation after the bounded
# repair retry budget is exhausted (Issue #17, ADR 0034). The id is
# part of the machine contract: callers branch on it verbatim.
STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER: str = (
    "provider-config-structured-output-schema-validation-failed"
)
# Stable warning ids emitted by :func:`record_provider_run_outcome`
# (Issue #18). Per the issue's AC2, missing usage/cost is a warning,
# not a blocker: recording is observation, not enforcement. The ids
# are the machine contract; downstream automation filters the
# warning set out of the blocker set the same way the stream-json
# parser does for ``stream-json-usage-missing``.
USAGE_MISSING_WARNING: str = "provider-usage-missing"
COST_MISSING_WARNING: str = "provider-cost-missing"
# Schema version stamped on the ``provider_usage`` block in
# ``state.json`` (Issue #18). Bumping this is a breaking change for
# any consumer of state.json that branches on the provider_usage
# shape.
PROVIDER_USAGE_SCHEMA_VERSION: int = 1
# Stable mapping from semantic key to stable warning id. Mirrors
# :data:`EXPECTED_BLOCKERS` for the warning side. Callers and
# downstream automation branch on the values verbatim; renaming
# any value is a breaking change and must be paired with a
# migration plan.
EXPECTED_BLOCKERS: dict[str, str] = {
    "secret_field": SECRET_FIELD_BLOCKER,
    "runtime_leak": RUNTIME_ADAPTER_LEAK_BLOCKER,
    "structured_output_probe": STRUCTURED_OUTPUT_PROBE_BLOCKER,
    "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER,
}
EXPECTED_WARNINGS: dict[str, str] = {
    "usage_missing": USAGE_MISSING_WARNING,
    "cost_missing": COST_MISSING_WARNING,
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



# --------------------------------------------------------------------------- #
# Public: structured-output capability probe (Issue #16, ADR 0034)            #
# --------------------------------------------------------------------------- #

# Provider types the default local probe recognizes. ADR 0034 pins
# Anthropic and OpenAI-compatible providers; anything else falls through
# to a probe-time failure unless the caller supplies a probe_fn that
# says otherwise.
_RECOGNIZED_PROVIDER_TYPES: frozenset[str] = frozenset(
    {"anthropic", "openai_compatible"}
)


def _default_structured_output_probe(
    provider_name: str,
    provider_spec: Mapping[str, Any],
    model: str,
) -> dict[str, Any]:
    """Default local probe: succeed iff ``provider_spec['type']`` is recognized.

    The default probe is deterministic and offline: it inspects the
    provider spec the resolver produced and returns success for the
    built-in shapes (``anthropic`` / ``openai_compatible``) and a
    well-formed failure for anything else. The real LLM round-trip
    lives in the caller-supplied ``probe_fn`` for production use;
    tests inject a deterministic local callback to drive both the
    success and the failure paths.
    """
    ptype: Any = None
    if isinstance(provider_spec, Mapping):
        ptype = provider_spec.get("type")
    raw: dict[str, Any] = {
        "provider_name": provider_name,
        "model": model,
        "type": ptype,
    }
    if ptype in _RECOGNIZED_PROVIDER_TYPES:
        return {
            "ok": True,
            "reason": None,
            "latency_ms": 0,
            "probe_kind": "local-static-ok",
            "raw": raw,
        }
    return {
        "ok": False,
        "reason": (
            f"provider type {ptype!r} is not in the recognized set "
            f"{sorted(_RECOGNIZED_PROVIDER_TYPES)!r}"
        ),
        "latency_ms": 0,
        "probe_kind": "local-static-fail",
        "raw": raw,
    }


def run_structured_output_probe(
    config: Mapping[str, Any],
    *,
    probe_fn: Callable[[str, Mapping[str, Any], str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the structured-output capability probe for ``judge`` and ``optimizer``.

    Issue #16 + ADR 0034: judge and optimizer structured-output use
    must require a successful capability probe first. The probe is
    supplied per-provider via the public ``probe_fn`` callback. The
    default probe is the deterministic local probe
    (:func:`_default_structured_output_probe`) that recognizes the
    built-in provider types and rejects unknown ones, with no
    network I/O and no mocks.

    Returns a result dict::

        {
            "ok": bool,
            "probe_evidence": {
                "judge": {
                    "role": "judge",
                    "provider": str | None,
                    "model": str | None,
                    "ok": bool,
                    "probe_kind": str,
                    "latency_ms": int | None,
                    "reason": str | None,
                    "raw": Any | None,
                },
                "optimizer": {...},
            },
            "blockers": [{"id": str, "message": str}],
        }

    On a blocked probe ``ok`` is ``False`` and ``blockers`` lists every
    role that failed (selector missing, unknown provider catalog entry,
    or probe_fn reported ``ok=False``). Error messages do not include
    ``api_key_env`` or any other credential reference (ADR 0034).
    Probe evidence lives at ``probe_evidence`` for every run regardless
    of success so a downstream receipt/evidence recorder has a stable
    place to find it.
    """
    if probe_fn is None:
        probe_fn = _default_structured_output_probe

    if not isinstance(config, Mapping):
        return {
            "ok": False,
            "probe_evidence": {
                "judge": _probe_evidence_entry(
                    "judge", None, None, False, "config-missing", None,
                    "config is not a mapping", None,
                ),
                "optimizer": _probe_evidence_entry(
                    "optimizer", None, None, False, "config-missing", None,
                    "config is not a mapping", None,
                ),
            },
            "blockers": [_blocker(
                STRUCTURED_OUTPUT_PROBE_BLOCKER,
                "structured-output probe input must be a mapping; got "
                f"{type(config).__name__} (Issue #16, ADR 0034)",
            )],
        }

    control_plane = config.get("control_plane", {})
    providers_catalog = config.get("providers", {})

    probe_evidence: dict[str, dict[str, Any]] = {}
    blockers: list[dict[str, str]] = []

    for role in ("judge", "optimizer"):
        selection: Any = (
            control_plane.get(role)
            if isinstance(control_plane, Mapping)
            else None
        )
        if not isinstance(selection, Mapping):
            blockers.append(_blocker(
                STRUCTURED_OUTPUT_PROBE_BLOCKER,
                f"control_plane.{role} selection is missing or invalid; "
                "structured-output probe cannot run (Issue #16, ADR 0034)",
            ))
            probe_evidence[role] = _probe_evidence_entry(
                role, None, None, False, "selection-missing", None,
                "control_plane selection missing or invalid", None,
            )
            continue

        provider_name: Any = selection.get("provider")
        model: Any = selection.get("model")
        if not isinstance(provider_name, str) or not isinstance(model, str):
            blockers.append(_blocker(
                STRUCTURED_OUTPUT_PROBE_BLOCKER,
                f"control_plane.{role} must carry 'provider' and 'model' "
                "strings; structured-output probe cannot run "
                "(Issue #16, ADR 0034)",
            ))
            probe_evidence[role] = _probe_evidence_entry(
                role, provider_name, model, False, "selection-malformed",
                None, "control_plane selection missing provider or model",
                None,
            )
            continue

        provider_spec: Any = (
            providers_catalog.get(provider_name)
            if isinstance(providers_catalog, Mapping)
            else None
        )
        if not isinstance(provider_spec, Mapping):
            blockers.append(_blocker(
                STRUCTURED_OUTPUT_PROBE_BLOCKER,
                f"provider {provider_name!r} is not present in the providers "
                f"catalog; structured-output probe cannot run for {role} "
                "(Issue #16, ADR 0034)",
            ))
            probe_evidence[role] = _probe_evidence_entry(
                role, provider_name, model, False, "provider-unknown", None,
                f"provider {provider_name!r} is not in the providers catalog",
                None,
            )
            continue

        probe_result: Any = probe_fn(provider_name, provider_spec, model)
        ok = bool(probe_result.get("ok")) if isinstance(probe_result, Mapping) else False
        reason_raw: Any = (
            probe_result.get("reason") if isinstance(probe_result, Mapping) else None
        )
        reason: str | None = reason_raw if isinstance(reason_raw, str) else None
        latency_raw: Any = (
            probe_result.get("latency_ms") if isinstance(probe_result, Mapping) else None
        )
        latency_ms: int | None = latency_raw if isinstance(latency_raw, int) else None
        kind_raw: Any = (
            probe_result.get("probe_kind") if isinstance(probe_result, Mapping) else None
        )
        probe_kind: str = (
            kind_raw if isinstance(kind_raw, str) and kind_raw else "probe-return-malformed"
        )
        raw: Any = (
            probe_result.get("raw") if isinstance(probe_result, Mapping) else None
        )

        probe_evidence[role] = _probe_evidence_entry(
            role, provider_name, model, ok, probe_kind, latency_ms,
            reason if not ok else None, raw,
        )

        if not ok:
            blockers.append(_blocker(
                STRUCTURED_OUTPUT_PROBE_BLOCKER,
                (
                    f"structured-output capability probe failed for {role} role "
                    f"(provider={provider_name!r}, model={model!r}): "
                    f"{reason!r}; judge/optimizer structured-output use is "
                    "blocked until the probe passes (Issue #16, ADR 0034)"
                ),
            ))

    return {
        "ok": not blockers,
        "probe_evidence": probe_evidence,
        "blockers": blockers,
    }


def _probe_evidence_entry(
    role: str,
    provider: Any,
    model: Any,
    ok: bool,
    probe_kind: str,
    latency_ms: int | None,
    reason: str | None,
    raw: Any,
) -> dict[str, Any]:
    """Return a normalized ``probe_evidence`` entry for a single role.

    Centralizing the entry shape keeps ``probe_evidence`` a stable
    contract for downstream receipt/evidence recording.
    """
    return {
        "role": role,
        "provider": provider,
        "model": model,
        "ok": ok,
        "probe_kind": probe_kind,
        "latency_ms": latency_ms,
        "reason": reason,
        "raw": raw,
    }



# --------------------------------------------------------------------------- #
# Public: structured-output JSON Schema validation + bounded repair          #
#          (Issue #17, ADR 0034)                                              #
# --------------------------------------------------------------------------- #

# JSON Schema type names we recognize in the structured-output
# contract. The validator only needs the subset the contract
# actually exercises (object/array/string/number/integer/boolean/
# null); unknown types are treated as a no-op pass so future
# JSON Schema revisions do not break the contract.
_SCHEMA_TYPE_NAMES: tuple[str, ...] = (
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
)


def _check_schema_type(value: Any, expected: str) -> bool:
    """Return True iff ``value`` matches the JSON Schema ``expected`` type.

    The JSON Schema ``integer`` and ``number`` types must reject
    booleans even though ``bool`` is a subclass of ``int`` in
    Python, so we explicitly exclude ``bool`` from the numeric
    checks.
    """
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_against_schema(
    value: Any,
    schema: Any,
    path: str,
    errors: list[str],
) -> None:
    """Recursively validate ``value`` against ``schema``, appending to ``errors``.

    The validator implements a minimal JSON Schema subset that
    covers the structured-output contract: ``type`` for any node,
    and ``properties`` / ``required`` / ``additionalProperties`` for
    object nodes. Anything outside the subset is ignored so the
    validator stays boring and dependency-free (Issue #17 keeps
    the implementation stdlib-only; ADR 0034 forbids new
    dependencies for the MVP).
    """
    if not isinstance(schema, Mapping):
        return

    expected_type_raw: Any = schema.get("type")
    if isinstance(expected_type_raw, str) and expected_type_raw in _SCHEMA_TYPE_NAMES:
        if not _check_schema_type(value, expected_type_raw):
            errors.append(
                f"{path}: expected type {expected_type_raw!r}, "
                f"got {type(value).__name__}"
            )
            return

    if (
        isinstance(expected_type_raw, str)
        and expected_type_raw == "object"
        and isinstance(value, Mapping)
    ):
        properties: Any = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for key, child_value in value.items():
                if key in properties:
                    _validate_against_schema(
                        child_value,
                        properties[key],
                        f"{path}.{key}",
                        errors,
                    )

        required: Any = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(
                        f"{path}: missing required property {key!r}"
                    )

        additional: Any = schema.get("additionalProperties", True)
        if additional is False and isinstance(properties, Mapping):
            for key in value:
                if key not in properties:
                    errors.append(
                        f"{path}: property {key!r} is not allowed "
                        "(additionalProperties=False)"
                    )

    if (
        isinstance(expected_type_raw, str)
        and expected_type_raw == "array"
        and isinstance(value, list)
    ):
        items: Any = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_against_schema(
                    item, items, f"{path}[{index}]", errors
                )


def call_structured(
    provider_name: str,
    provider_spec: Mapping[str, Any],
    model: str,
    schema: Mapping[str, Any],
    call_fn: Callable[..., Any],
    *,
    max_repair_attempts: int = 1,
) -> dict[str, Any]:
    """Call ``call_fn`` and validate its response against ``schema``.

    Issue #17 + ADR 0034: every structured provider response must
    be JSON-Schema-validated before the caller is allowed to act
    on it. When a response fails validation the same ``call_fn``
    is re-invoked with a ``repair_context`` mapping exposing
    ``schema`` and ``validation_errors`` so the LLM (or any
    caller-side repair logic) can self-correct. Repairs are
    bounded by ``max_repair_attempts``; once that budget is
    exhausted the function returns the stable
    :data:`STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER` and does
    not raise (Issue #17: schema validation failure is a
    result-level outcome, not an exception).

    The first call to ``call_fn`` is always issued with
    ``repair_context=None``. Every retry passes a mapping whose
    keys include at least ``schema`` and ``validation_errors``
    (extra keys are allowed but never required). ``call_fn`` is
    invoked as ``call_fn(repair_context=<mapping or None>)``.

    Parameters
    ----------
    provider_name:
        Logical provider name (e.g. ``"anthropic"``). Used only
        for blocker messages; never echoed as a credential
        reference (ADR 0034).
    provider_spec:
        Resolved provider spec from the providers catalog. Passed
        through unchanged on every call; not interpreted here.
    model:
        Model name passed through unchanged on every call.
    schema:
        JSON Schema dict the response must satisfy. Owned by the
        caller; we never mutate it.
    call_fn:
        Callable invoked as ``call_fn(repair_context=...)``. May
        be a thin wrapper around the provider SDK or any local
        test double.
    max_repair_attempts:
        Maximum number of repair retries after the initial call.
        ``0`` means "one call only" (no retries); ``1`` (the
        default) means "one initial call plus one retry". The
        total number of invocations is therefore
        ``1 + max_repair_attempts``.

    Returns
    -------
    dict
        ``{"ok": True, "value": <response>, "attempts": int,
        "validation_errors": [], "blockers": []}`` on success;
        ``{"ok": False, "value": None, "attempts": int,
        "validation_errors": <non-empty list>,
        "blockers": [{"id": stable, "message": non-empty str}]}``
        on final failure.
    """
    if max_repair_attempts < 0:
        max_repair_attempts = 0

    attempts = 0
    repair_context: Any = None
    last_errors: list[str] = []

    while True:
        attempts += 1
        response: Any = call_fn(repair_context=repair_context)
        errors: list[str] = []
        _validate_against_schema(response, schema, path="$", errors=errors)
        if not errors:
            return {
                "ok": True,
                "value": response,
                "attempts": attempts,
                "validation_errors": [],
                "blockers": [],
            }
        last_errors = errors
        if attempts > max_repair_attempts:
            break
        repair_context = {
            "schema": schema,
            "validation_errors": errors,
            "provider_name": provider_name,
            "provider_spec": provider_spec,
            "model": model,
        }

    return {
        "ok": False,
        "value": None,
        "attempts": attempts,
        "validation_errors": last_errors,
        "blockers": [
            _blocker(
                STRUCTURED_OUTPUT_SCHEMA_VALIDATION_BLOCKER,
                (
                    f"structured-output response from provider "
                    f"{provider_name!r} model {model!r} failed JSON Schema "
                    f"validation after {attempts} attempt(s); see "
                    f"validation_errors for details "
                    f"(Issue #17, ADR 0034)"
                ),
            ),
        ],
    }

# --------------------------------------------------------------------------- #
# Public: record provider run outcome (usage/cost) into state.json           #
#          (Issue #18, ADR 0034)                                              #
# --------------------------------------------------------------------------- #
#
# Issue #18 + ADR 0034: capture provider-neutral usage/cost data in
# ``state.json`` so a future optimizer/receipt consumer can correlate
# cost and tokens per run. The recorder is observation, not
# enforcement: missing usage or cost is a warning, never a blocker,
# and no hard cost cap is introduced.
#
# The on-disk shape in ``state.json`` is::
#
#     {
#         "schema_version": 1,
#         ...other state fields preserved...,
#         "provider_usage": {
#             "schema_version": <PROVIDER_USAGE_SCHEMA_VERSION>,
#             "runs": [
#                 {
#                     "run_id": "run-abc",
#                     "provider": "anthropic",
#                     "model": "claude-sonnet-4-5",
#                     "ts": "2026-06-08T00:00:00Z",
#                     "usage": {<raw provider usage dict, or None>},
#                     "cost": {<raw provider cost dict, or None>},
#                 },
#                 ...
#             ],
#         },
#     }
#
# The ``runs`` list is keyed by ``run_id`` for idempotency: a
# re-recorded run replaces its previous record (no double-count).
# Raw ``usage`` and ``cost`` payloads are preserved verbatim so
# Anthropic's ``input_tokens``/``output_tokens`` shape and OpenAI's
# ``prompt_tokens``/``completion_tokens`` shape are both accepted
# without coercion. Future ADR revisions can introduce
# provider-specific normalizers without changing this function.


def _now_iso_utc() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix.

    Local copy of the helper that lives in :mod:`metacrucible.storage`
    and :mod:`metacrucible.__main__`. We do not import those modules
    here to keep the dependency graph one-way: ``provider_config``
    is a leaf consumed by callers, not a consumer of ``storage``.
    """
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def record_provider_run_outcome(
    storage: Any,
    *,
    run_id: str,
    provider: str,
    model: str,
    usage: Mapping[str, Any] | None = None,
    cost: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Record a provider run's usage and cost into ``state.json``.

    Issue #18 + ADR 0034: provider-neutral usage/cost schema. The
    recorder is observation, not enforcement:

      - AC1: cost (and usage, when present) is recorded into the
        ``provider_usage`` block of ``state.json``.
      - AC2: missing ``usage`` or ``cost`` is a stable warning, never
        a blocker. The result's ``ok`` is ``True`` whenever the
        recorder successfully wrote ``state.json``; missing
        fields never cause ``ok=False``.
      - AC3: no hard cost cap is introduced. The function never
        compares ``cost`` to any budget, threshold, or limit.
        Cost is recorded, not policed.

    Parameters
    ----------
    storage:
        A :class:`metacrucible.storage.RepositoryStorage` instance
        that owns the ``state.json`` file. The recorder reads the
        current state, appends/replaces the ``provider_usage`` run
        record, and writes the merged state back atomically.
    run_id:
        Stable identifier for the run (e.g. ``"run-abc"``). The
        recorder is idempotent per ``run_id``: a re-recorded run
        replaces the previous record so a retry does not
        double-count tokens or cost.
    provider:
        Logical provider name (e.g. ``"anthropic"``,
        ``"openai_compatible"``). Stored verbatim; not
        normalized.
    model:
        Model name (e.g. ``"claude-sonnet-4-5"``). Stored verbatim.
    usage:
        Provider-neutral raw usage payload (any mapping). Common
        shapes include Anthropic's
        ``{"input_tokens": N, "output_tokens": M, ...}`` and
        OpenAI's ``{"prompt_tokens": N, "completion_tokens": M, ...}``.
        ``None`` (the default) means the provider did not supply
        a usage block; a :data:`USAGE_MISSING_WARNING` is emitted.
    cost:
        Provider-neutral raw cost payload (any mapping). Common
        shapes include ``{"usd": 0.001, "currency": "USD"}`` or
        ``{"input_cost_usd": ..., "output_cost_usd": ...}``.
        ``None`` (the default) means the provider did not supply
        a cost block; a :data:`COST_MISSING_WARNING` is emitted.
    timestamp:
        ISO-8601 timestamp to stamp on the run record. ``None``
        (the default) means "now" (UTC, ``Z`` suffix, second
        precision).

    Returns
    -------
    dict
        ``{"ok": True, "state": <written state dict>,
        "warnings": [{"id", "message"}, ...],
        "blockers": []}``. The ``blockers`` key is always an empty
        list (AC2: missing fields are never blockers). ``ok`` is
        always ``True`` when ``storage`` exposes the read/write
        methods; a missing ``storage`` or one without the right
        methods raises ``AttributeError`` to the caller (the
        recorder does not silently swallow contract violations).
    """
    if not hasattr(storage, "read_state") or not hasattr(storage, "write_state"):
        raise AttributeError(
            "record_provider_run_outcome requires a storage object with "
            "read_state() and write_state() methods "
            "(got {!r}; expected metacrucible.storage.RepositoryStorage)".format(
                type(storage).__name__
            )
        )
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(
            f"run_id must be a non-empty string; got {run_id!r}"
        )
    if not isinstance(provider, str) or not provider:
        raise ValueError(
            f"provider must be a non-empty string; got {provider!r}"
        )
    if not isinstance(model, str) or not model:
        raise ValueError(
            f"model must be a non-empty string; got {model!r}"
        )

    warnings: list[dict[str, str]] = []
    if not isinstance(usage, Mapping):
        warnings.append(
            {
                "id": USAGE_MISSING_WARNING,
                "message": (
                    f"provider run {run_id!r} did not include a usage "
                    "block; recording continues with usage=null"
                ),
            }
        )
        usage_payload: Mapping[str, Any] | None = None
    else:
        usage_payload = dict(usage)
    if not isinstance(cost, Mapping):
        warnings.append(
            {
                "id": COST_MISSING_WARNING,
                "message": (
                    f"provider run {run_id!r} did not include a cost "
                    "block; recording continues with cost=null"
                ),
            }
        )
        cost_payload: Mapping[str, Any] | None = None
    else:
        cost_payload = dict(cost)

    run_record: dict[str, Any] = {
        "run_id": run_id,
        "provider": provider,
        "model": model,
        "ts": timestamp if isinstance(timestamp, str) and timestamp else _now_iso_utc(),
        "usage": usage_payload,
        "cost": cost_payload,
    }

    # Read-modify-write state.json. Other state fields
    # (current_best_revision, last_run_id, ...) are preserved so
    # the rest of the CLI surface (init, promote, ...) keeps
    # working unchanged.
    try:
        existing_raw = storage.read_state()
    except Exception:
        existing_raw = {}
    existing: dict[str, Any] = (
        dict(existing_raw) if isinstance(existing_raw, Mapping) else {}
    )

    provider_usage_raw: Any = existing.get("provider_usage")
    if (
        not isinstance(provider_usage_raw, Mapping)
        or not isinstance(provider_usage_raw.get("runs"), list)
    ):
        provider_usage: dict[str, Any] = {
            "schema_version": PROVIDER_USAGE_SCHEMA_VERSION,
            "runs": [],
        }
    else:
        provider_usage = dict(provider_usage_raw)
        # Re-shape the runs list, keeping only dict entries.
        provider_usage["runs"] = [
            r for r in provider_usage["runs"] if isinstance(r, Mapping)
        ]

    # Idempotent per run_id: a re-recorded run replaces its previous
    # entry so a retry does not double-count tokens or cost.
    runs_list: list[Any] = [
        r
        for r in provider_usage["runs"]
        if isinstance(r, Mapping) and r.get("run_id") != run_id
    ]
    runs_list.append(run_record)
    provider_usage["runs"] = runs_list

    new_state = dict(existing)
    new_state["provider_usage"] = provider_usage
    storage.write_state(new_state)

    return {
        "ok": True,
        "state": new_state,
        "warnings": warnings,
        "blockers": [],
    }

