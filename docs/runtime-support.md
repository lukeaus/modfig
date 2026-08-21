# Runtime support

ModFig has three built-in target adapters. Registry validation and planning are
portable; client mutation is target-specific and may require a current runtime
proof.

## Support matrix

| Target | Client surface | Current status |
| --- | --- | --- |
| `factory` | Factory CLI/TUI and personal oh-my-droid files | Transactional apply implemented. |
| `vscode` | Stable Microsoft VS Code default profile | Transactional apply implemented behind macOS/Linux runtime proof. |
| `chatgpt` | Codex CLI/TUI and GUI-backed `CODEX_HOME` | Provider profiles and catalogs implemented behind Codex runtime proof. |

The registry target name is `chatgpt`; the runtime product is Codex. The
`factory` target includes both Factory core settings and the built-in
`oh-my-droid` extension.

## Command behavior

| Command | Reads | Network/secrets | Writes |
| --- | --- | --- | --- |
| `validate` | Registry only | No | No |
| `validate --adapters` | Registry, routes, configured adapter state | May resolve secrets and probe enabled Factory models declared as `openai` | No |
| `diff` | Registry, routes, runtime proofs, and preflight state | Target-dependent proof/preflight checks | No |
| `apply` | All state required by the selected adapters | Target-dependent; Factory probes `openai` models before mutation | Selected client files and the ModFig manifest |

`diff` is a preflight check; it does not render a textual client diff.
`apply` is one host transaction. It snapshots destinations, creates a
recoverable backup and journal, writes conditionally, verifies the result, and
commits the manifest last. A failure rolls back when it is safe to do so.

## Target notes

### Factory

Factory model records, favorites, defaults, session settings, mission settings,
and built-in oh-my-droid frontmatter are reconciled transactionally. Existing
managed model changes produce an advisory warning; `--yes` acknowledges it
without prompting. Factory additions-only applies do not require a prompt.

Factory transport is declared by each model's effective `provider` value.
`openai` means Responses API and is probed before apply. Do not use it for an
endpoint that only supports Chat Completions.

### VS Code

The adapter is proof-bound to the stable Microsoft Code default profile. It
updates `chatLanguageModels.json` and Code's encrypted `state.vscdb` secret
rows. VS Code must be closed while applying; restart or reload it afterward.

### Codex

The adapter writes one profile and catalog per enabled ChatGPT provider, plus
the default-provider projection in `config.toml`. Codex must be restarted
after apply so it reloads the profiles and catalogs.

## Proof and platform limits

Proof records bind paths, client versions, file formats, secret mechanisms, and
restart behavior. A stale or mismatched proof fails closed before client
mutation. Windows is not a supported mutation platform in v0.1.

Runtime support is not implied by registry acceptance. A valid registry can
describe a provider/model combination that a particular endpoint or client
version does not support.
