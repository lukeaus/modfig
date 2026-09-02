# Architecture

ModFig separates portable desired state from client-specific mutation.

## End-to-end flow

```text
registry.yaml
    |
    v
parse and validate
    |
    v
select target clients and components
    |
    v
resolve built-in or explicitly enabled adapter route
    |
    v
preflight, proof, and destination declarations
    |
    v
snapshot under locks
    |
    v
adapter plan
    |
    v
backup + journal -> conditional writes -> verify -> manifest commit
```

The adapter plans desired bytes; the host owns filesystem I/O, locks, backups,
journals, rollback, recovery, and manifest updates.

## Portable registry

The registry contains:

- provider and model catalogs;
- target membership and enablement;
- target-specific model identities and capabilities;
- Factory defaults, session, mission, and oh-my-droid desired state.

It does not contain filesystem paths, adapter grants, commands, installed
package metadata, or secret values. See the
[normative registry specification](../spec/modfig-registry-0.1.md).

## Routes and adapters

Factory, VS Code, ChatGPT/Codex, and oh-my-droid use immutable built-in routes.
Other adapters require explicit local route configuration in
`adapters.yaml`. An adapter is planning-only:

1. `describe`
2. `validate`
3. `preflight`
4. `plan`
5. `recheck`
6. `verify`

The host validates every declared read/write grant and passes the adapter only
its component mapping, client-filtered model facts, proofs, and snapshots.

## Ownership and foreign state

The v3 manifest records ownership by client and component, including:

- destination and grant identity;
- preimage and written hashes;
- adapter provenance;
- target-specific ownership metadata.

Adapters reconcile only owned state, except Factory's `customModels` collection,
which is the explicit managed projection. Foreign settings and unknown
database rows are preserved.

## Transaction safety

Each mutating invocation:

1. acquires canonical locks;
2. rechecks the manifest and destination versions;
3. plans from immutable snapshots;
4. creates an owner-only backup and pending journal;
5. conditionally writes each declared artifact;
6. verifies every written artifact;
7. commits the manifest last;
8. removes the journal and backup only after success.

If a later step fails, the host restores changed destinations using expected
versions. If concurrent drift makes rollback unsafe, the journal and backup
remain for recovery rather than overwriting newer user state.

## Secret boundaries

The registry stores only references such as `env.OPENROUTER_API_KEY`. Plain
`validate` does not resolve them. Target adapters resolve secrets only inside
their proven mutation/preflight path:

- Factory sends a minimal Responses probe for `openai` models.
- VS Code stores provider keys in Code's own encrypted secret store.
- Codex profiles contain environment-variable names, not key values.

Secrets are excluded from manifests, proofs, logs, diffs, and error messages.

## Package boundaries

| Module | Responsibility |
| --- | --- |
| `registry.py` | Registry parsing and semantic validation |
| `app.py` | Target selection and host transaction coordination |
| `factory.py` | Factory model/settings projection |
| `oh_my_droid.py` | Built-in personal droid renderer |
| `vscode.py` | VS Code model and secret-row projection |
| `chatgpt.py` | Codex profile/catalog projection |
| `manifest.py`, `journal.py`, `backup.py`, `recovery.py` | Durable state and recovery |
| `adapter_routes.py`, `adapters.py` | Route and adapter contracts |
