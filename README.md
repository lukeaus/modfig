# ModFig

<p align="center">
  <img src="docs/assets/modfig-logo.svg" alt="ModFig logo" width="160">
</p>

[![CI](https://github.com/lukeaus/modfig/actions/workflows/ci.yml/badge.svg)](https://github.com/lukeaus/modfig/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)
[![mypy: strict](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy-lang.org/)

## Overview

ModFig keeps LLM providers, models, endpoints, capabilities, and target
identities in one owner-only YAML registry. It validates and synchronizes that
registry to Factory Droids, Visual Studio Code, and Codex.

```text
registry.yaml -> ModFig -> Factory | VS Code | Codex
```

The registry stores environment-variable references, never resolved API keys.
Client updates use ownership-aware reconciliation and a recoverable host
transaction.

## Supported targets

| Target | Managed surface | Status |
| --- | --- | --- |
| `factory` | Custom models, defaults, sessions, missions, oh-my-droid | Transactional apply implemented |
| `vscode` | Custom endpoint providers and encrypted API-key rows | Apply implemented with stable-runtime proof |
| `chatgpt` | Codex provider profiles, catalogs, and default projection | Apply implemented with Codex runtime proof |

See the [runtime support matrix](docs/runtime-support.md) for command behavior,
proof requirements, and platform limits.

## Installation

ModFig requires Python 3.11 or later:

```sh
python3 -m pip install .
```

For development:

```sh
python3 -m pip install -e ".[dev]"
```

## Quick start

Create and validate a registry:

```sh
modfig init
modfig validate
```

Preview or apply one target:

```sh
modfig diff --target factory
modfig apply --target factory --yes
```

Use `--target vscode` or `--target chatgpt` for the other built-in adapters.
See the [CLI reference](docs/reference/cli.md) for configuration discovery,
validation modes, exit codes, and confirmation behavior.

## Registry

The normative contract is the
[ModFig Registry Specification 0.1](spec/modfig-registry-0.1.md), with its
[JSON Schema](spec/modfig-registry-0.1.schema.json) and
[conformance fixtures](spec/fixtures/).

The registry is discovered in this order:

1. `--config FILE`
2. `MODFIG_CONFIG`
3. `$XDG_CONFIG_HOME/modfig/config.yaml`, when absolute
4. `~/.config/modfig/config.yaml`
5. Legacy `~/.modfig.yaml`, only when the new default is absent

Minimal example:

```yaml
specVersion: "0.1"
providers:
  router:
    name: Router
    targets: [factory]
    baseUrl: https://api.example.com/v1
    apiKey: env.ROUTER_API_KEY
    provider: openai
    enabled: true
    models:
      primary:
        displayName: Primary
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
```

## Target guides

- [Codex and multiple provider profiles](docs/targets/codex.md)
- [Factory Droids](docs/targets/factory.md)
- [Visual Studio Code](docs/targets/vscode.md)

## Architecture and safety

Read the [architecture guide](docs/architecture.md) for adapter boundaries,
ownership, manifests, backups, journals, rollback, and recovery.

Important safety properties:

- secrets remain environment references in the registry;
- foreign client state is preserved;
- writes require proof and destination/version checks where the target needs it;
- failed transactions roll back when safe, otherwise leave recovery state;
- `validate` is offline; adapter-aware validation may resolve secrets and probe
  configured endpoints.

## Development

See [development and testing](docs/development.md) for setup, packaging,
linting, type checking, and repository conventions.

## Changelog

See [CHANGES.md](CHANGES.md).

## License

This project is licensed under the [MIT License](LICENSE).
