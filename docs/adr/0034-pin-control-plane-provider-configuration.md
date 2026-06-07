# Pin control plane provider configuration

MetaCrucible separates control-plane model provider configuration from target runtime adapter configuration and resolves settings through built-in defaults, user config, project config, and CLI flags. Provider credentials are referenced only through environment variable names, structured-output support is probed and validated rather than trusted from configuration alone, and usage, cost, capability errors, and retries use provider-neutral schemas.

**Consequences**

- `control_plane`, `providers`, and `runtime_adapters` are separate config sections; the control plane chooses judge and optimizer providers and models, while runtime adapters configure target execution such as the Claude Code binary and mode.
- Config files may contain `api_key_env` references but must reject direct API key, token, secret, or password fields without echoing their values.
- Anthropic and OpenAI-compatible providers must pass a structured-output capability probe and every structured call must validate against JSON Schema with bounded repair retries.
- Provider-neutral usage records normalize input, output, cache, reasoning, tool-call, and cost fields while preserving provider raw usage by reference; missing usage or cost fields warn rather than block.
- Provider errors use a small stable taxonomy, retry only transient rate limit, timeout, unavailable, and limited unknown failures, and do not retry authentication, context overflow, unsupported JSON mode, or safety refusal errors.