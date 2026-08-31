# Factory Droids

The Factory adapter manages custom models, Factory core defaults and sessions,
mission routing, and the built-in oh-my-droid extension.

## Registry shape

```yaml
clientConfig:
  factory:
    core:
      defaults:
        worker: {provider: surplus, model: deepseek-v4-flash}
        thinker: {provider: surplus, model: gpt-5.6-terra}
        orchestrator: {provider: surplus, model: gpt-5.6-terra}
        simple: {provider: surplus, model: deepseek-v4-flash}
        validator: {provider: surplus, model: gpt-5.6-terra}
      session:
        model: {provider: surplus, model: gpt-5.6-luna}
        reasoningEffort: max
        specModeModel: {provider: surplus, model: gpt-5.6-terra}
        specModeReasoningEffort: max
```

`defaults` requires exactly five roles: `worker`, `thinker`, `orchestrator`,
`simple`, and `validator`. Session and mission model fields may use portable
references or Factory-native IDs.

## Factory TUI session default

Factory stores session configuration in two representations. ModFig projects
the configured session fields to both:

```text
/session/...
/sessionDefaultSettings/...
```

The second table is the TUI default used when creating a new session. Both
representations are protected by ownership and drift checks. Restart or reopen
Factory after applying a change.

`droid exec` is a separate headless command path. Select its model explicitly:

```sh
droid exec \
  --model custom:gpt-5.6-luna--surplus \
  --reasoning-effort max \
  "Your prompt"
```

## Transport

Factory emits each model's effective provider unchanged:

- `openai` means the OpenAI Responses API and is probed at `/responses` before
  apply.
- `generic-chat-completion-api` means OpenAI-compatible Chat Completions.
- `anthropic` means the Anthropic Messages API; when the model declares a
  per-model `baseUrl`, that endpoint is probed at `<baseUrl>/v1/messages`
  before apply (e.g. Surplus's `https://api.surplusintelligence.ai/anthropic`).

Each model's `baseUrl` defaults to the provider `baseUrl`. A per-model
`baseUrl` overrides it for that model only:

```yaml
providers:
  surplus:
    name: Surplus
    targets: [factory]
    baseUrl: https://api.surplusintelligence.ai/v1
    apiKey: env.SURPLUS_API_KEY
    provider: anthropic
    enabled: true
    models:
      claude-sonnet-5:
        displayName: Claude Sonnet 5
        contextWindow: 1048576
        maxOutputTokens: 128000
        baseUrl: https://api.surplusintelligence.ai/anthropic
        enabled: true
```

Use `openai` only when the configured endpoint actually supports Responses.
Model names alone do not select a transport.

## oh-my-droid

The built-in extension maps installed droid names to Factory model IDs:

```yaml
clientConfig:
  factory:
    extensions:
      oh-my-droid:
        droids:
          architect: {provider: surplus, model: gpt-5.6-terra}
          executor: {provider: surplus, model: deepseek-v4-flash}
        prune: false
```

ModFig rewrites only the `model:` frontmatter field. With `prune: true`, it
removes only previously owned plugin-derived overrides. The installed plugin
inventory must contain every mapped droid.

## Apply

```sh
modfig validate --config ~/.config/modfig/config.yaml
modfig diff --target factory --config ~/.config/modfig/config.yaml
modfig apply --target factory --yes --config ~/.config/modfig/config.yaml
```

Factory changes are committed through the shared recoverable transaction. The
manifest records model, default, session, mission, and extension ownership.
