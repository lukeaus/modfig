# Visual Studio Code

The VS Code adapter targets the stable Microsoft VS Code default profile.

## Managed files

```text
~/Library/Application Support/Code/User/chatLanguageModels.json
~/Library/Application Support/Code/User/globalStorage/state.vscdb
```

On Linux the corresponding default-profile paths live under
`~/.config/Code/User`.

The JSON file contains provider/model metadata and references to Code input
secrets. The actual API keys are stored in Code's encrypted SecretStorage
database, not in `chatLanguageModels.json`.

## Reasoning controls

Declare reasoning support on the model:

```yaml
models:
  gpt-5.6-luna:
    displayName: GPT-5.6 Luna [Surplus]
    extensions:
      vscode:
        reasoningLevels: [low, medium, high, xhigh, max]
        defaultReasoningLevel: max
```

ModFig emits VS Code's `supportsReasoningEffort` and
`defaultReasoningEffort` model capabilities, plus the provider-level
`settings[model].reasoningEffort` value. This makes Thinking Effort visible and
persisted in the model picker.

## Provider selection

The built-in Copilot provider is separate from ModFig providers. A Copilot
quota or payment error does not test a ModFig key. Select the custom provider
and model in the Chat model picker, for example:

```text
Surplus → GPT-5.6 Luna [Surplus]
```

## Safe apply workflow

VS Code must be closed while ModFig writes its SQLite state. Capture a fresh
proof, apply, then reopen VS Code:

```sh
modfig vscode proof capture
modfig diff --target vscode --config ~/.config/modfig/config.yaml
modfig apply --target vscode --yes --config ~/.config/modfig/config.yaml
open -a "Visual Studio Code"
```

The adapter requires owner-only permissions on the managed JSON and database
files. It preserves foreign providers, models, and unknown SQLite rows.

## Troubleshooting

If a custom provider is present but requests fail:

1. Confirm the provider's `apiKey` is a Code input reference.
2. Confirm its encrypted row exists in `state.vscdb`.
3. Confirm the corresponding environment variable is set when applying.
4. Close VS Code and reapply if it was running during a write.
5. Choose the custom model instead of Copilot in the picker.
