# Factory Droids

The Factory adapter manages custom models, Factory core defaults and sessions,
mission routing, and the built-in oh-my-droid extension.

## Registry shape

```yaml
clientConfig:
  factory:
    core:
      defaults:
        worker: {provider: openrouter, model: claude-opus-4-5}
        thinker: {provider: openrouter, model: gpt-5}
        orchestrator: {provider: openrouter, model: gpt-5}
        simple: {provider: openrouter, model: claude-opus-4-5}
        validator: {provider: openrouter, model: gpt-5}
      session:
        model: {provider: openrouter, model: gpt-5-mini}
        reasoningEffort: max
        specModeModel: {provider: openrouter, model: gpt-5}
        specModeReasoningEffort: max
```

`defaults` requires exactly five roles: `worker`, `thinker`, `orchestrator`,
`simple`, and `validator`. Session and mission model fields may use portable
references or Factory-native IDs.

## Sharing role models and efforts

The optional root `variables` bucket holds YAML anchors that any client
configuration reuses through aliases, so each role's model and reasoning
effort are written once:

```yaml
variables:
  worker:
    model: &worker_model
      provider: openrouter
      model: gpt-5-mini
    effort: &worker_effort max
  thinker:
    model: &thinker_model
      provider: openrouter
      model: gpt-5
    effort: &thinker_effort max
  orchestrator:
    model: &orchestrator_model
      provider: openrouter
      model: gpt-5
    effort: &orchestrator_effort max
  simple:
    model: &simple_model
      provider: openrouter
      model: gpt-5-mini
  validator:
    model: &validator_model
      provider: openrouter
      model: gpt-5
    effort: &validator_effort max

clientConfig:
  factory:
    core:
      defaults:
        worker: *worker_model
        thinker: *thinker_model
        orchestrator: *orchestrator_model
        simple: *simple_model
        validator: *validator_model
      session:
        model: *worker_model
        reasoningEffort: *worker_effort
        specModeModel: *thinker_model
        specModeReasoningEffort: *thinker_effort
      mission:
        orchestratorModel: *orchestrator_model
        orchestratorReasoningEffort: *orchestrator_effort
        workerModel: *worker_model
        workerReasoningEffort: *worker_effort
        validationWorkerModel: *validator_model
        validationWorkerReasoningEffort: *validator_effort
```

`variables` is an anchor bucket only: ModFig requires it to be a mapping and
never interprets its content, because the YAML loader expands the aliases
before the parser sees the values. Anchors must be defined before the first
alias that uses them. Because nothing reads the bucket, its grouping is
free-form; nesting `model` and `effort` under each role keeps one role's
settings together. `simple` has no effort anchor because Factory stores no
effort for `subagent` fields, which are model-only.

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
  --model custom:gpt-5-mini--openrouter \
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
  before apply (e.g. OpenRouter's scoped Anthropic endpoint
`https://openrouter.ai/api/v1/anthropic`).

Each model's `baseUrl` defaults to the provider `baseUrl`. A per-model
`baseUrl` overrides it for that model only:

```yaml
providers:
  openrouter:
    name: OpenRouter
    targets: [factory]
    baseUrl: https://openrouter.ai/api/v1
    apiKey: env.OPENROUTER_API_KEY
    provider: anthropic
    enabled: true
    models:
      claude-sonnet-5:
        displayName: Claude Sonnet 5
        contextWindow: 1048576
        maxOutputTokens: 128000
        baseUrl: https://openrouter.ai/api/v1/anthropic
        enabled: true
```

Use `openai` only when the configured endpoint actually supports Responses.
Model names alone do not select a transport.

## oh-my-droid

The built-in extension maps installed droid names to Factory model IDs. A droid
entry may instead use the inherit sentinel — the scalar `inherit` or exactly
`{model: inherit}` — which writes literal `model: inherit` frontmatter so the
droid follows the client default model:

```yaml
clientConfig:
  factory:
    extensions:
      oh-my-droid:
        droids:
          architect: {provider: openrouter, model: gpt-5}
          executor: {provider: openrouter, model: claude-opus-4-5}
          analyst: inherit
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
