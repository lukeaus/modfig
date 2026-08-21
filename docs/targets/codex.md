# Codex and multiple provider profiles

ModFig calls the registry target `chatgpt` because the adapter targets the
shared Codex configuration used by the Codex CLI, TUI, and GUI.

## Generated layout

For each enabled provider that targets `chatgpt`, ModFig writes:

```text
$CODEX_HOME/<provider-key>.config.toml
$CODEX_HOME/modfig-<provider-key>-catalog.json
```

It also projects the registry-marked default provider into:

```text
$CODEX_HOME/config.toml
```

For example, a registry with `surplus` and `openrouter` produces:

```text
~/.codex/surplus.config.toml
~/.codex/modfig-surplus-catalog.json
~/.codex/openrouter.config.toml
~/.codex/modfig-openrouter-catalog.json
~/.codex/config.toml
```

Each provider profile contains only that provider's models and points to its
own catalog. Profiles do not share catalog pointers.

## Default provider

Mark exactly one enabled ChatGPT provider as the default:

```yaml
providers:
  surplus:
    extensions:
      chatgpt:
        wireApi: responses
        default: true
```

The default is used for the base `config.toml` projection. It does not remove
the other provider profiles.

## Selecting a profile

Use a provider profile explicitly in the CLI or TUI:

```sh
codex --profile surplus
codex --profile openrouter
```

The GUI reads the default projection from `config.toml`. Set `CODEX_HOME` when
using an isolated account home:

```sh
CODEX_HOME=/path/to/codex-home codex --profile surplus
```

Restart Codex after applying changes so it reloads `config.toml`, profiles, and
catalogs.

## Model and reasoning metadata

The model wire ID is unchanged. The catalog adds the display label and the
reasoning levels declared by the model:

```yaml
models:
  gpt-5.6-luna:
    displayName: GPT-5.6 Luna [Surplus]
    extensions:
      chatgpt:
        reasoningLevels: [low, medium, high, xhigh, max]
```

`apiKey` remains an environment reference in the registry. Generated Codex
profiles use the environment-variable name; they do not contain the resolved
secret.

## Apply

```sh
modfig validate --config ~/.config/modfig/config.yaml
modfig diff --target chatgpt --config ~/.config/modfig/config.yaml
modfig apply --target chatgpt --yes --config ~/.config/modfig/config.yaml
```

Apply requires a fresh Codex runtime proof. If the proof is stale, capture a
new one while the Codex process is quiescent:

```sh
modfig chatgpt proof capture
```
