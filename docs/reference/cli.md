# CLI reference

## Commands

| Command | Purpose |
| --- | --- |
| `modfig init [--config FILE]` | Create an owner-only starter registry. |
| `modfig validate [--config FILE]` | Validate only the portable registry. |
| `modfig validate --adapters [--config FILE]` | Validate configured routes and adapter state. |
| `modfig diff [--config FILE] [--target TARGET]` | Run target preflight without writing. |
| `modfig apply [--config FILE] [--target TARGET] [--yes]` | Apply one recoverable transaction. |
| `modfig adapter enable ...` | Explicitly enable a verified third-party adapter route. |

`TARGET` is `factory`, `vscode`, `chatgpt`, or `all`; it defaults to `all`.

## Configuration discovery

The registry path is resolved in this order:

1. `--config FILE`
2. `MODFIG_CONFIG`
3. `$XDG_CONFIG_HOME/modfig/config.yaml` when the XDG path is absolute
4. `~/.config/modfig/config.yaml`
5. `~/.modfig.yaml`, only when the new default is absent

An explicit path or `MODFIG_CONFIG` never falls back. The registry must be an
owner-only regular file.

## Apply confirmation

Factory applies may run without `--yes` when there are no existing managed
model updates/removals. If such changes exist, the CLI prints the affected
model IDs and accepts only `y` or `yes`. `--yes` prints the same warning and
continues without prompting.

Explicit non-Factory applies require `--yes`.

## Validation modes

`validate` is offline and does not read routes, client state, or environment
secrets.

`validate --adapters` loads selected routes and may resolve configured secrets.
It also probes every enabled Factory-targeted model whose effective provider is
`openai` at `/responses`. Missing credentials, transport errors, timeouts,
non-200 responses, and unusable output fail validation.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Command completed successfully. |
| `1` | Registry, route, proof, preflight, or other expected failure. |
| `3` | A required environment secret is missing or empty. |

## Common workflows

```sh
# Validate only the registry.
modfig validate --config ~/.config/modfig/config.yaml

# Check one target without writing.
modfig diff --target factory --config ~/.config/modfig/config.yaml

# Apply one target.
modfig apply --target factory --yes --config ~/.config/modfig/config.yaml

# Apply every selected target in one transaction.
modfig apply --target all --yes --config ~/.config/modfig/config.yaml
```

For target-specific commands, see the
[Codex](../targets/codex.md), [Factory](../targets/factory.md), and
[VS Code](../targets/vscode.md) guides.
