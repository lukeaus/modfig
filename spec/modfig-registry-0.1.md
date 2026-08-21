# ModFig Registry Specification 0.1

## Status and conformance

This document is the normative contract for `specVersion: "0.1"`. The JSON
Schema defines structural rules; the reference parser also enforces semantic
rules that JSON Schema cannot express. CLI and registry versions are independent.
CLI 0.1.0 accepts registry version `0.1` only.
See the [JSON Schema](modfig-registry-0.1.schema.json) and the
[conformance fixtures](fixtures/).

Registry 0.1 is an unpublished, breaking revision. Providers and models are
insertion-ordered YAML maps whose keys supply identity; the legacy list shape
and inline `key`/`model` fields are rejected. Factory model ids are always
computed; the model-level `extensions.factory` namespace is removed. Factory
`core.defaults` requires exactly five roles.

The registry can describe dynamic logical clients matching `^[a-z][a-z0-9-]*$`,
including the built-in `factory`, `vscode`, and `chatgpt` clients. This is a
configuration contract, not a runtime-support claim. Every client/OS/surface row
remains fail-closed until its capability proof is recorded.

The `chatgpt` target key maps each enabled provider key to a Codex CLI/TUI
profile artifact `${CODEX_HOME:-${HOME}/.codex}/<provider-key>.config.toml`
and a provider-scoped catalog
`${CODEX_HOME:-${HOME}/.codex}/modfig-<provider-key>-catalog.json`. Each
profile contains only that provider's enabled ChatGPT models and points to its
own catalog. Exactly one emitting provider must set
`extensions.chatgpt.default: true`; the GUI-active
`${CODEX_HOME:-${HOME}/.codex}/config.toml` contains only that provider's
managed table and catalog pointer. The registry uses `chatgpt` as the
canonical target name; the runtime client is Codex. Catalog entries keep the
wire `slug` equal to the registry catalog id and render `display_name` as
`<display name> [<provider name>]`, without repeating a suffix the display
name already has. Foreign TOML state is preserved, while managed stale files
are removed only when their recorded ownership hash still matches.

## Document shape

| Field | Type | Required | Nullable | Default | Rules |
| --- | --- | --- | --- | --- | --- |
| `specVersion` | string | yes | no | none | Exactly `"0.1"`. |
| `providers` | non-empty mapping | yes | no | none | Keys are provider identities (`^[A-Za-z0-9][A-Za-z0-9._-]*$`, excluding `--`); globally unique. Map order is projection order. |
| `clientConfig` | mapping | no | no | `{}` | Dynamic logical-client desired state; each client may contain `core` and `extensions`. |

Root extensions are not part of registry 0.1. YAML is parsed with a duplicate-key-aware safe loader. Duplicate keys, unsafe
YAML tags, unknown standard-owned fields, and an empty registry are invalid.

## Client configuration

Each `clientConfig.<logical-client>` mapping accepts only optional `core` and
`extensions`. Present component values are non-empty mappings. Extension names
match the logical ID grammar and cannot be `core`; external core and extension
mappings are otherwise opaque adapter-owned data.

Factory `core` is built in and accepts only `defaults`, `session`, and `mission`.
`defaults` requires exactly `worker`, `thinker`, `orchestrator`, `simple`, and
`validator`, each an exact `{provider, model}` portable reference. Session and
mission model fields accept portable references or exact `{factoryNative: <non-empty-id>}` references.
Reasoning effort is one of `off`, `none`, `low`, `medium`, `high`, or `max`.
The Factory adapter projects each configured `session` field to both Factory's
runtime session table and its `sessionDefaultSettings` TUI-default table.
Existing `sessionDefaultSettings` values are adopted only when the
corresponding runtime session field is already ModFig-owned; both
representations are then protected by ownership and drift checks.

Portable references resolve only to an enabled provider and enabled model whose
`targets` includes the consuming logical client. Factory-native references are
not portable references and are valid only in the enumerated Factory session and
mission model fields, never in `defaults`.
For VS Code, a model may declare `extensions.vscode.reasoningLevels` and an
optional `defaultReasoningLevel`; the adapter emits these as the model's
`supportsReasoningEffort`/`defaultReasoningEffort` capabilities and persists
the selected effort in the provider `settings` map.

## Local adapter routing

The portable registry declares desired client state only. It cannot select an
adapter package, distribution, entry point, installation, network location,
path grant, or command. Those local trust decisions belong to
`$XDG_CONFIG_HOME/modfig/adapters.yaml` when `XDG_CONFIG_HOME` is non-empty and
absolute, otherwise `~/.config/modfig/adapters.yaml`. This local file has no
legacy fallback and must be a private, regular, effective-user-owned file.

Factory, oh-my-droid, VS Code, and ChatGPT have immutable built-in routes. A
third-party logical client has exactly one locally enabled primary `core` route;
an extension route is scoped to one logical client and owns exactly one
`extensions.<name>` mapping. Its `adapter` value is both the authoritative
entry-point name in the `modfig.adapters.v1` group and the adapter ID returned
by `describe()`. The selected distribution must own that exact entry point, and
route metadata must bind the exact logical client and component before the host
passes the component mapping to it.

A route carries structured read and write grant IDs. The host validates every
grant-relative snapshot and prospective write, performs all filesystem I/O,
locks, backups, journaling, manifest updates, rollback, and recovery. An
AdapterV1 performs planning only: `describe`, `validate`, `preflight`, `plan`,
`recheck`, and `verify`. An in-process Python adapter is
trusted in-process code, not a sandbox. It receives only its selected component
mapping, client-filtered model resolver, host facts, and host snapshots.

`clientConfig.factory.core` belongs to the built-in Factory adapter.
`clientConfig.factory.extensions.oh-my-droid` belongs to the built-in
oh-my-droid adapter. Its schema is exactly `{droids, prune}`, where `droids`
maps plugin droid names to portable Factory model references and `prune`
defaults to `false`. ModFig validates the mapping, discovers the installed
plugin inventory, rewrites only `model:` frontmatter, and records ownership for
scoped pruning. Other extension schemas remain adapter-owned.

## Commands and selection

`validate` is portable-registry validation only. It neither reads local routes
nor imports adapters nor reads client state. `validate --adapters` loads routes
only for configured components and invokes their adapter validation. The
built-in oh-my-droid validation reads its installed plugin inventory to confirm
mapped droids exist; third-party adapters receive no client-state I/O.
`diff` and `apply` load selected routes and use desired-or-owned selection:
`--target all` selects the canonical union of provider targets,
`clientConfig` clients, and manifest-owned clients. For each selected client it
reconciles configured and manifest-owned components, including an
owned-but-unconfigured extension.

Registry discovery prefers `--config`, then `MODFIG_CONFIG`, then
`$XDG_CONFIG_HOME/modfig/config.yaml` only when the XDG path is absolute, then
`~/.config/modfig/config.yaml`. When that default is absent it falls back to
legacy `~/.modfig.yaml` only for reading. `init` does not silently shadow a
legacy registry. Includes, `.d` fragments, and alternate registry shapes are
not supported.

## Examples

A model-only registry contains only the required provider catalog:

```yaml
specVersion: "0.1"
providers:
  router:
    name: Router
    targets: [factory]
    baseUrl: https://router.example/v1
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

Factory core configuration and the built-in oh-my-droid extension may coexist:

```yaml
clientConfig:
  factory:
    core:
      defaults:
        worker: {provider: router, model: primary}
        thinker: {provider: router, model: primary}
        orchestrator: {provider: router, model: primary}
        simple: {provider: router, model: primary}
        validator: {provider: router, model: primary}
      session:
        model: {provider: router, model: primary}
        reasoningEffort: high
        specModeModel: {factoryNative: claude-opus-4-8}
    extensions:
      oh-my-droid:
        droids:
          analyst: {provider: router, model: primary}
        prune: false
```

The built-in oh-my-droid adapter preserves frontmatter and body bytes other than
the `model:` field. `prune: true` deletes only previously owned plugin-derived
overrides. A third-party primary mapping is adapter-owned:

```yaml
clientConfig:
  cursor:
    core:
      profile: work
```

`clientConfig.cursor.core` is valid only when a local route binds the Cursor
primary adapter. ModFig does not interpret `profile` or other third-party
component fields.

## Provider fields

| Field | Type | Required | Nullable | Default | Rules |
| --- | --- | --- | --- | --- | --- |
| `name` | non-empty string | yes | no | none | Human-readable only. |
| `targets` | non-empty list | yes | no | none | Unique logical client IDs matching `^[a-z][a-z0-9-]*$`. |
| `baseUrl` | absolute URL | yes | no | none | No credentials or fragment. HTTPS is required except HTTP for `localhost`, `127.0.0.1`, or `::1`. |
| `apiKey` | string | yes | no | none | Matches `^env\.[A-Za-z_][A-Za-z0-9_]*$`; validation does not resolve it. |
| `provider` | string | no | yes | absent | One of `openai`, `anthropic`, `generic-chat-completion-api`; `null` means absent. The declared transport; emitted unchanged. |
| `enabled` | boolean | yes | no | none | Disabled providers remain structurally validated but emit nothing. |
| `models` | non-empty mapping | yes | no | none | Keys are model identities, unique within the provider (including disabled models). Map order is emission order. |
| `extensions` | mapping | no | yes | `{}` | May contain only `chatgpt`. |

The provider identity is its map key under `providers`, matching
`^[A-Za-z0-9][A-Za-z0-9._-]*$` and excluding `--`. Inline `key:` fields are
rejected.

### Provider ChatGPT extension

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `extensions.chatgpt.providerId` | string | no | Matches `^modfig-[A-Za-z0-9][A-Za-z0-9._-]*$`, case-sensitive. |
| `extensions.chatgpt.wireApi` | string | no | Literal `responses`. |
| `extensions.chatgpt.default` | boolean | no | Exactly one enabled provider emitting ChatGPT models must set this to `true`; it selects the GUI base projection. |

Unknown provider extension names and unknown fields inside `chatgpt` are invalid.
Root, provider, and model `extensions` mappings may be `null` as absence. A
present nested target mapping (`chatgpt` or `vscode`) must be a mapping, not
`null`; fields inside it are also non-null when present.

## Model fields

| Field | Type | Required | Nullable | Default | Rules |
| --- | --- | --- | --- | --- | --- |
| `displayName` | non-empty string | yes | no | none | Human-readable model name. |
| `contextWindow` | positive integer | yes | no | none | At least `maxOutputTokens`. |
| `maxOutputTokens` | positive integer | yes | no | none | Must not exceed `contextWindow`. |
| `maxInputTokens` | non-negative integer | no | yes | `contextWindow - maxOutputTokens` | `null` means absent. |
| `provider` | non-empty string | no | yes | absent | Factory effective-provider override only; `null` means absent. It never selects ChatGPT transport. |
| `noImageSupport` | boolean | no | yes | `false` | `null` means absent. |
| `toolCalling` | boolean | no | yes | `false` | `null` means absent. |
| `favourite` | boolean | no | yes | `false` | `null` means absent. |
| `enabled` | boolean | yes | no | none | Disabled models remain structurally validated but emit nothing. |
| `extensions` | mapping | no | yes | `{}` | May contain only `vscode` and `chatgpt`. |

The model identity is its map key under `provider.models`. Inline `model:`
fields are rejected. Model keys may contain `/`, spaces, and other non-empty
strings; they are used verbatim in references and slugified for Factory ids.

The Factory effective provider remains model-level `provider`, then provider-level
`provider`, then `generic-chat-completion-api`. This value does not participate
in ChatGPT transport selection.

## Target identities and extensions

| Target | Default identity | Override | Collision scope |
| --- | --- | --- | --- |
| Factory | `custom:<slugified model>--<provider key>` | none (computed only) | Global across enabled Factory emission. |
| VS Code | exact `model` | `extensions.vscode.id` | Within each enabled VS Code provider. |
| ChatGPT provider | `modfig-<provider key>` | provider `extensions.chatgpt.providerId` | Global across enabled ChatGPT-emitting providers. |
| ChatGPT catalog | exact `model` | model `extensions.chatgpt.catalogId` | Global across enabled ChatGPT-emitting models. |

Factory ids are derived, never stored: `custom:<slugify(model)>--<provider-key>`,
where `slugify` lower-cases the model name and replaces every run of characters
outside `[A-Za-z0-9._-]` with a single `-` (for example `OpenAI/GPT 5` becomes
`openai-gpt-5`). The model-level `extensions.factory` namespace is removed;
declaring it is rejected. VS Code ids are non-empty. ChatGPT provider ids use
the pattern above. ChatGPT catalog ids are case-sensitive, non-empty, and
contain no whitespace or control characters.

Model extensions are strict:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `extensions.vscode.id` | non-empty string | no | VS Code nested model identity. |
| `extensions.vscode.reasoningLevels` | list of strings | no | VS Code Thinking Effort levels exposed for this model. Values are `low`, `medium`, `high`, `xhigh`, or `max`; the list must be non-empty and unique when present. |
| `extensions.vscode.defaultReasoningLevel` | string | no | Default VS Code Thinking Effort. It must be one of the declared `reasoningLevels`. |
| `extensions.chatgpt.catalogId` | string | no | Valid ChatGPT catalog identity. |
| `extensions.chatgpt.reasoningLevels` | list of strings | no | Codex effort levels advertised for this model. Values are `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`; the list must be non-empty and unique when present. |

Explicit identities are validated even on disabled records. Collision and
transport checks include only records that would emit: both provider and model
must be enabled and the target must be selected.

## Transport compatibility

The `provider` field declares the endpoint's actual transport and is emitted
unchanged. ModFig performs no static, name-based Factory transport validation:
a GPT-family model is accepted under `generic-chat-completion-api` (Surplus and
other Chat Completions proxies) and under `openai` (direct OpenAI or
Responses-capable proxies such as vibeproxy). The live Responses probe (run by
`validate --adapters` and `apply` preflight) enforces `openai` declarations.

ChatGPT transport is provider-scoped and fail-closed:

- provider-level `provider: openai` implies Responses transport;
- `extensions.chatgpt.wireApi: responses` explicitly opts any non-Anthropic
  provider into the Codex Responses transport, including a provider whose
  Factory transport remains `generic-chat-completion-api`;
- `anthropic` is invalid for enabled ChatGPT emission;
- model-level `provider`, model name, endpoint, and effective Factory provider
  never determine ChatGPT transport.

## Target emission and secrets

A model emits to a target only when its provider and model are enabled and the
provider selects that target. ChatGPT projection is limited by this contract to
the logical provider/catalog identities, endpoint, Responses transport, and the
environment-variable name from `apiKey`. No catalog path, pointer, file format,
profile, active selection, OS, or surface field exists in registry 0.1 because
those runtime details are not yet proven.

`apiKey` is a reference, never a secret value. Validation checks syntax without
reading the environment. A target may project only the reference semantics
proven for that client. ChatGPT projection uses the variable name, not the
resolved value. Resolved values must never appear in ModFig registry files,
metadata, diagnostics, diffs, journals, command arguments, or proof records.

## File, ownership, and runtime gates

Registry and ModFig metadata safety, ownership reconciliation, confirmation,
transaction, backup, and recovery requirements remain fail-closed. Foreign
client state is preserved; Factory `customModels` are the managed projection,
so every `custom:` entry is replaced or removed to match the registry while
non-custom Factory state is preserved.

Factory, VS Code, and ChatGPT mutation is governed by target-specific runtime
gates. A target cannot mutate when its required version, path/schema, secret
transport, restart, authenticated request, foreign-state preservation, or
recovery proof is not recorded. Until that target's proof is accepted, its
`diff` and `apply` must fail before reading client state, resolving secrets,
creating metadata, or writing files. Registry acceptance alone never means a
client target is supported.
