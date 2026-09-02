# Changelog

## Unreleased

- Reworked the registry into insertion-ordered provider/model maps with
  computed Factory IDs.
- Added five Factory default roles: `worker`, `thinker`, `orchestrator`,
  `simple`, and `validator`.
- Added provider-scoped Codex profiles and catalogs with a registry-selected
  default provider.
- Added Factory session, mission, and built-in oh-my-droid synchronization.
- Added stable VS Code custom-endpoint synchronization with encrypted
  SecretStorage rows and configurable Thinking Effort levels.
- Added manifest v3 ownership, conditional writes, backups, journals, rollback,
  and recovery.
- Added live Responses preflight for Factory models declared with
  `provider: openai`.
- Added per-model `baseUrl` endpoint overrides (OpenRouter Anthropic style) for
  Factory and VS Code projections, with a live Messages preflight for
  `anthropic` models that declare an explicit override endpoint.
- Raised the default Factory Responses probe timeout to 120 seconds
  (`MODFIG_PROBE_TIMEOUT` overrides it per-request).
- Split maintained documentation into target guides, CLI reference, runtime
  support, architecture, and development documentation.

## Notes

The `0.1` registry contract is unpublished and breaking. See the
[normative specification](spec/modfig-registry-0.1.md) for the current schema
and compatibility rules.
