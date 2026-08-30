from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from functools import partial
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .adapter_routes import (
    AdapterRoute,
    AdapterRouteError,
    AdapterRoutes,
    builtin_adapter_routes,
    load_adapter_routes,
    merge_adapter_routes,
    resolve_adapter_routes_path,
)
from .adapters import (
    AbsentDestination,
    AdapterContext,
    AdapterPlanContext,
    AdapterV1,
    AdapterValidationContext,
    ArtifactIdentity,
    ArtifactPlan,
    ArtifactSnapshot,
    PreflightDeclaration,
    ResolvedModel,
    RuntimeProof,
    discover_adapter_entry_points,
    load_enabled_adapter,
    preflight_declaration_sha256,
    validate_plan_against_declarations,
)
from .backup import (
    BackupArtifact,
    BackupRequest,
    create_backup_set,
    remove_backup_set,
    validate_backup_set,
)
from .clients import chatgpt, factory, vscode
from .clients.factory.extensions import oh_my_droid
from .clients.vscode.db import DatabasePaths, snapshot_members
from .components import Component, ExtensionComponent
from .errors import AppError
from .journal import InvocationJournal, TransactionArtifact, save_journal
from .locking import operation_locks
from .manifest import (
    AdapterProvenance,
    ClientOwnership,
    ComponentOwnership,
    OwnedArtifact,
    OwnershipManifest,
    OwnershipManifestSnapshot,
    load_ownership_manifest,
    load_ownership_manifest_snapshot,
    ownership_manifest_bytes,
    ownership_manifest_owned_components,
)
from .platform import PrivateParentMissingError
from .registry import ModelReference, Registry, RegistryValidationError, load_registry
from .storage import (
    FileVersion,
    conditional_delete,
    conditional_write_bytes,
    inspect_private_file,
    read_private_bytes,
    resolve_config_path,
)

TARGET_ORDER = ("factory", "vscode", "chatgpt")
EXPORTERS = {"factory": factory, "vscode": vscode, "chatgpt": chatgpt}
_LOGICAL_CLIENT_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def selected_targets(target: str) -> tuple[str, ...]:
    return TARGET_ORDER if target == "all" else (target,)


def load_valid_registry(config: str | None) -> Registry:
    return load_registry(resolve_config_path(config).path)


def validate_logical_client(target: str) -> str:
    if target == "all" or _LOGICAL_CLIENT_RE.fullmatch(target):
        return target
    raise AppError(f"invalid logical client {target!r}: must match {_LOGICAL_CLIENT_RE.pattern!r}")


def declared_clients(registry: Registry) -> frozenset[str]:
    return frozenset(target for provider in registry.providers for target in provider.targets)


def configured_clients(registry: Registry) -> frozenset[str]:
    return frozenset(registry.client_config)


def selected_clients(
    target: str,
    registry: Registry,
    owned_components: Mapping[str, Collection[Component]],
) -> tuple[str, ...]:
    if target != "all":
        return (target,)
    union = declared_clients(registry) | configured_clients(registry) | frozenset(owned_components)
    builtins = tuple(client for client in TARGET_ORDER if client in union)
    third_parties = tuple(client for client in sorted(union - set(TARGET_ORDER)))
    return builtins + third_parties


def adapter_plan_context(
    logical_client: str, component: Component, registry: Registry
) -> AdapterPlanContext:
    config = registry.client_component(logical_client, component) or {}
    models = tuple(
        ResolvedModel(
            provider_key=provider.key,
            base_url=provider.base_url,
            api_key_reference=provider.api_key_reference,
            model=model.model,
            display_name=model.display_name,
            max_output_tokens=model.max_output_tokens,
            effective_provider=model.effective_provider,
            no_image_support=model.no_image_support,
            favourite=model.favourite,
            factory_id=model.factory_id(provider.key),
            vscode_id=model.vscode_id(),
            vscode_reasoning_levels=model.vscode_reasoning_levels,
            vscode_default_reasoning_level=model.vscode_default_reasoning_level,
            max_input_tokens=model.max_input_tokens,
            tool_calling=model.tool_calling,
            provider_name=provider.name,
            chatgpt_provider_id=provider.chatgpt_provider_id(),
            chatgpt_wire_api=provider.chatgpt_wire_api(),
            chatgpt_catalog_id=model.chatgpt_catalog_id(),
            chatgpt_reasoning_levels=model.chatgpt_reasoning_levels,
            chatgpt_default=provider.chatgpt_default(),
            context_window=model.context_window,
            factory_extra_args=model.factory_extra_args(),
            factory_extra_headers=model.factory_extra_headers(),
            vscode_extra_args=model.vscode_extra_args(),
            vscode_extra_headers=model.vscode_extra_headers(),
            chatgpt_http_headers=provider.chatgpt_http_headers(),
        )
        for provider, model in registry.emitted_models(logical_client)
    )
    by_reference = {(model.provider_key, model.model): model for model in models}

    def resolve(reference: ModelReference) -> ResolvedModel:
        try:
            return by_reference[(reference.provider_key, reference.model_name)]
        except KeyError:
            raise RegistryValidationError(
                f"provider {reference.provider_key!r} does not target {logical_client!r}"
            ) from None

    return AdapterPlanContext(logical_client, component, config, models, resolve)


def selected_components(
    logical_client: str,
    registry: Registry,
    owned_components: Mapping[str, Collection[Component]],
) -> tuple[Component, ...]:
    config = registry.client_config.get(logical_client)
    has_core = bool(registry.emitted_models(logical_client)) or (
        config is not None and config.core is not None
    )
    extension_names: set[str] = set()
    if config is not None:
        extension_names.update(config.extensions)
    for component in owned_components.get(logical_client, ()):
        if component == "core":
            has_core = True
        else:
            extension_names.add(component.name)
    ordered: list[Component] = ["core"] if has_core else []
    ordered.extend(ExtensionComponent(name) for name in sorted(extension_names))
    return tuple(ordered)


def resolve_manifest_path(
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    # ponytail: single canonical manifest location; env override can come if needed.
    home_path = Path.home() if home is None else home
    environment = os.environ if environ is None else environ
    configured = environment.get("MODFIG_MANIFEST")
    if configured:
        return Path(configured).expanduser()
    return home_path / ".modfig" / "manifest.json"


def load_owned_components(
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Mapping[str, tuple[Component, ...]]:
    path = resolve_manifest_path(environ, home)
    try:
        manifest = load_ownership_manifest(path)
    except PrivateParentMissingError:
        # Missing manifest (including missing parent) is an empty v3 state.
        return MappingProxyType({})
    return ownership_manifest_owned_components(manifest)


def _builtin_adapter_routes(*, include_chatgpt: bool = True) -> AdapterRoutes:
    configured_vscode_root = _configured_vscode_user_data_root()
    if configured_vscode_root is not None:
        return builtin_adapter_routes(
            environ=os.environ,
            vscode_user_data_root=configured_vscode_root,
            include_chatgpt=include_chatgpt,
        )
    return builtin_adapter_routes(
        environ=os.environ,
        vscode_user_data_root=vscode.discover_vscode_user_data_root(),
        include_chatgpt=include_chatgpt,
    )


def validate_adapters(config: str | None) -> None:
    registry = load_valid_registry(config)
    needed: list[tuple[str, Component]] = []
    for logical_client, client_config in registry.client_config.items():
        if client_config.core is not None:
            needed.append((logical_client, "core"))
        needed.extend(
            (logical_client, ExtensionComponent(name)) for name in client_config.extensions
        )
    needs_chatgpt = any(client == "chatgpt" for client, _ in needed)
    builtins = _builtin_adapter_routes(include_chatgpt=needs_chatgpt)
    builtin_routes = {(route.logical_client, route.component): route for route in builtins}
    external_needed = [binding for binding in needed if binding not in builtin_routes]
    routes = builtins
    entry_points: Mapping[str, importlib.metadata.EntryPoint] = {}
    if external_needed:
        path = resolve_adapter_routes_path(os.environ, Path.home())
        routes = merge_adapter_routes(builtins, load_adapter_routes(path))
        entry_points = discover_adapter_entry_points()
    for logical_client, component in needed:
        route = builtin_routes.get((logical_client, component))
        if route is None:
            route = routes.route(logical_client, component)
        adapter = _adapter_for_route(route, entry_points)
        config_value = registry.client_component(logical_client, component)
        assert config_value is not None
        context = AdapterValidationContext(
            logical_client,
            component,
            partial(registry.resolve_model, logical_client=logical_client),
        )
        adapter.validate(config_value, context)
        if (
            logical_client == "chatgpt"
            and component == "core"
            and route.adapter_id == "modfig.chatgpt"
            and route.builtin
        ):
            _preflight_builtin_chatgpt(route, registry)
    # ponytail: the live Responses probe runs only here (validate --adapters)
    # and in apply preflight; plain `validate` never reaches this path.
    factory.probe_factory_responses(registry, os.environ)


def preflight_targets(targets: Sequence[str]) -> None:
    failures = []
    selected = tuple(targets)
    for target in selected:
        if EXPORTERS[target] is factory:
            continue
        try:
            EXPORTERS[target].preflight()
        except AppError:
            if len(selected) == 1:
                raise
            failures.append(f"{target}: unavailable")
    if failures:
        raise AppError(f"target preflight failed: {'; '.join(failures)}")


def _merged_adapter_routes(*, include_chatgpt: bool = True) -> AdapterRoutes:
    builtins = _builtin_adapter_routes(include_chatgpt=include_chatgpt)
    path = resolve_adapter_routes_path(os.environ, Path.home())
    if not path.exists():
        return builtins
    return merge_adapter_routes(builtins, load_adapter_routes(path))


def _require_enabled_route(
    routes: AdapterRoutes, logical_client: str, component: Component
) -> AdapterRoute:
    try:
        route = routes.route(logical_client, component)
    except AdapterRouteError as exc:
        raise AppError(str(exc)) from exc
    if not route.enabled:
        raise AppError(f"adapter route for {logical_client!r} {component!r} is disabled")
    return route


def preflight_selection(
    clients: tuple[str, ...],
    registry: Registry,
    owned: Mapping[str, Collection[Component]],
) -> None:
    routes = _merged_adapter_routes(include_chatgpt="chatgpt" in clients)
    resolved: list[tuple[str, tuple[tuple[Component, AdapterRoute], ...]]] = []
    failures: list[str] = []
    single = len(clients) == 1
    for client in clients:
        try:
            resolved.append((client, _resolve_selected_routes(client, registry, owned, routes)))
        except AppError:
            if single:
                raise
            failures.append(f"{client}: unavailable")
    if failures:
        raise AppError(f"target preflight failed: {'; '.join(failures)}")

    for client, component_routes in resolved:
        try:
            _preflight_one_client(client, component_routes, registry)
        except AppError:
            if single:
                raise
            failures.append(f"{client}: unavailable")
    if failures:
        raise AppError(f"target preflight failed: {'; '.join(failures)}")


def _resolve_selected_routes(
    client: str,
    registry: Registry,
    owned: Mapping[str, Collection[Component]],
    routes: AdapterRoutes,
) -> tuple[tuple[Component, AdapterRoute], ...]:
    components = selected_components(client, registry, owned)
    _require_enabled_route(routes, client, "core")
    return tuple(
        (component, _require_enabled_route(routes, client, component)) for component in components
    )


def _preflight_one_client(
    client: str,
    component_routes: Collection[tuple[Component, AdapterRoute]],
    registry: Registry,
) -> None:
    components = {component for component, _ in component_routes}
    if client in EXPORTERS and EXPORTERS[client] is not factory and "core" in components:
        core_route = next(route for component, route in component_routes if component == "core")
        if client == "vscode" and core_route.adapter_id == "modfig.vscode" and core_route.builtin:
            _preflight_builtin_vscode(core_route)
        elif (
            client == "chatgpt" and core_route.adapter_id == "modfig.chatgpt" and core_route.builtin
        ):
            _preflight_builtin_chatgpt(core_route, registry)
        else:
            EXPORTERS[client].preflight()
    external_routes = [
        (component, route) for component, route in component_routes if not route.builtin
    ]
    if not external_routes:
        for component, route in component_routes:
            if route.adapter_id != "modfig.oh_my_droid":
                continue
            adapter = _adapter_for_route(route, {})
            config = registry.client_component(client, component) or {}
            adapter.validate(
                config,
                AdapterValidationContext(
                    client,
                    component,
                    partial(registry.resolve_model, logical_client=client),
                ),
            )
            adapter.preflight(AdapterContext(client, component))
        return
    # ponytail: routes are resolved for every selected client before this import phase.
    entry_points = discover_adapter_entry_points()
    for component, route in external_routes:
        adapter = load_enabled_adapter(route, entry_points=entry_points)
        adapter.preflight(AdapterContext(client, component))
    for component, route in component_routes:
        if route.adapter_id != "modfig.oh_my_droid":
            continue
        adapter = _adapter_for_route(route, {})
        config = registry.client_component(client, component) or {}
        adapter.validate(
            config,
            AdapterValidationContext(
                client,
                component,
                partial(registry.resolve_model, logical_client=client),
            ),
        )
        adapter.preflight(AdapterContext(client, component))


def _preflight_builtin_vscode(route: AdapterRoute) -> None:
    declaration = vscode.adapter.preflight(AdapterContext("vscode", "core"))
    destinations: dict[tuple[str, Component, ArtifactIdentity], Path] = {
        ("vscode", "core", write.artifact): _grant_destination(route, write.artifact, write=True)
        for write in declaration.prospective_writes
    }
    proof = _vscode_runtime_proof(
        declaration,
        destinations,
        _load_public_vscode_proof(),
    )
    runtime = proof.provenance
    if not isinstance(runtime, vscode.VSCodeRuntime):
        raise AppError("VS Code runtime proof is unavailable")
    vscode.preflight(runtime)


def _chatgpt_manifest_ownership() -> Mapping[str, object]:
    try:
        manifest = load_ownership_manifest(resolve_manifest_path())
    except (AppError, PrivateParentMissingError):
        return {}
    record = _component_record(manifest, "chatgpt", "core")
    return _chatgpt_record_ownership(record)


def _chatgpt_record_ownership(record: ComponentOwnership | None) -> Mapping[str, object]:
    if record is None:
        return {}
    ownership = dict(record.ownership)
    raw_hashes = ownership.get("artifactHashes", {})
    hashes = dict(raw_hashes) if isinstance(raw_hashes, Mapping) else {}
    for artifact in record.artifacts:
        path = str(artifact.artifact_path)
        previous = hashes.get(path)
        if previous is not None and previous != artifact.written_sha256:
            raise AppError(f"ChatGPT ownership hash disagrees with manifest for {path!r}")
        hashes[path] = artifact.written_sha256
    ownership["artifactHashes"] = hashes
    return ownership


def _preflight_builtin_chatgpt(route: AdapterRoute, registry: Registry) -> None:
    plan_context = adapter_plan_context("chatgpt", "core", registry)
    declaration = chatgpt.adapter.preflight(
        AdapterContext("chatgpt", "core", plan_context.models, _chatgpt_manifest_ownership())
    )
    proof = _load_public_chatgpt_proof()
    _chatgpt_runtime_proof(declaration, route, proof)


def _chatgpt_proof_path() -> Path:
    configured = os.environ.get("MODFIG_CHATGPT_PROOF")
    if configured:
        return Path(configured).expanduser().absolute()
    return (Path.home() / ".modfig" / "chatgpt-runtime-proof.json").absolute()


def _load_public_chatgpt_proof() -> RuntimeProof:
    return chatgpt.load_chatgpt_runtime_proof(_chatgpt_proof_path())


def _chatgpt_runtime_proof(
    declaration: PreflightDeclaration,
    route: AdapterRoute,
    proof: RuntimeProof | None,
) -> RuntimeProof:
    if proof is None or not isinstance(proof.provenance, chatgpt.ChatGPTRuntime):
        raise AppError("ChatGPT runtime proof is unavailable")
    destination = _grant_destination(route, chatgpt._CHATGPT_BASE_ARTIFACT, write=True)
    if proof.provenance.config_path != destination:
        raise AppError("ChatGPT runtime proof is bound to a different config path")
    if proof.provenance.codex_home != destination.parent:
        raise AppError("ChatGPT runtime proof is bound to a different codex home")
    return RuntimeProof(
        dict(proof.facts),
        preflight_declaration_sha256(declaration),
        provenance=proof.provenance,
    )


def diff(config: str | None, target: str) -> None:
    validated = validate_logical_client(target)
    registry = load_valid_registry(config)
    owned = load_owned_components()
    clients = selected_clients(validated, registry, owned)
    preflight_selection(clients, registry, owned)


def preflight_only_apply(config: str | None, target: str, yes: bool) -> None:
    diff(config, target)
    del yes


def _acknowledge_factory_warning(affected_model_ids: Sequence[str], yes: bool) -> None:
    if not affected_model_ids:
        return
    print("WARNING: existing ModFig-managed Factory models will be updated or removed:")
    for model_id in affected_model_ids:
        print(f"  {model_id}")
    print("If any listed model is in use, shut down Factory before continuing.")
    print("This is advisory; acknowledgement does not confirm Factory shutdown.")
    if yes:
        return
    if not os.isatty(0):
        raise AppError("Factory warning acknowledgement required")
    try:
        answer = input("Continue? [y/N] ")
    except EOFError:
        raise AppError("Factory warning acknowledgement required") from None
    if answer.strip().lower() not in {"y", "yes"}:
        raise AppError("Factory warning acknowledgement required")


def _adapter_for_route(
    route: AdapterRoute,
    entry_points: Mapping[str, importlib.metadata.EntryPoint],
) -> AdapterV1:
    if route.builtin:
        if route.adapter_id == "modfig.factory":
            return factory.adapter  # type: ignore[return-value]
        if route.adapter_id == "modfig.vscode":
            return vscode.adapter  # type: ignore[return-value]
        if route.adapter_id == "modfig.chatgpt":
            return chatgpt.adapter  # type: ignore[return-value]
        if route.adapter_id == "modfig.oh_my_droid":
            return oh_my_droid.adapter
        raise AppError(f"unknown builtin adapter: {route.adapter_id}")
    return load_enabled_adapter(route, entry_points=entry_points)


def _grant_destination(route: AdapterRoute, identity: ArtifactIdentity, *, write: bool) -> Path:
    grants = route.write_grants if write else route.read_grants
    grant = next((item for item in grants if item.grant_id == identity.grant_id), None)
    if grant is None:
        direction = "write" if write else "read"
        raise AppError(f"artifact {identity!r} is outside the route {direction} grants")
    if grant.kind == "file":
        if identity.relative_path != PurePosixPath(grant.root.name):
            raise AppError("file grant artifact path must equal the granted file name")
        return grant.root.absolute()
    scope = grant.relative_scope
    assert scope is not None
    if scope != PurePosixPath(".") and not identity.relative_path.is_relative_to(scope):
        raise AppError("directory grant artifact path is outside relative scope")
    return grant.root.joinpath(*identity.relative_path.parts).absolute()


def _require_unique_destinations(destinations: Collection[Path]) -> None:
    values = tuple(destinations)
    if len(values) != len(set(values)):
        raise AppError("duplicate or overlapping destination")
    for index, path in enumerate(values):
        for other in values[index + 1 :]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise AppError("duplicate or overlapping destination")


def _component_record(
    manifest: OwnershipManifest, client: str, component: Component
) -> ComponentOwnership | None:
    return next(
        (
            record
            for record in manifest.clients.get(client, ClientOwnership()).components
            if record.component == component
        ),
        None,
    )


def _validate_record_route(record: ComponentOwnership | None, route: AdapterRoute) -> None:
    if record is None:
        return
    if (
        record.adapter.adapter_id != route.adapter_id
        or record.adapter.distribution != route.distribution
    ):
        raise AppError("manifest adapter provenance does not match selected route")
    for artifact in record.artifacts:
        if route.adapter_id == "modfig.chatgpt" and artifact.grant_id in {
            "chatgpt-config",
            "chatgpt-catalog",
            "chatgpt-base",
        }:
            artifact_identity = ArtifactIdentity("chatgpt-home", artifact.artifact_path)
        else:
            artifact_identity = ArtifactIdentity(artifact.grant_id, artifact.artifact_path)
        _grant_destination(
            route,
            artifact_identity,
            write=True,
        )


def _replace_record(
    manifest: OwnershipManifest,
    client: str,
    replacement: ComponentOwnership | None,
    component: Component,
) -> OwnershipManifest:
    clients = dict(manifest.clients)
    records = [
        record
        for record in clients.get(client, ClientOwnership()).components
        if record.component != component
    ]
    if replacement is not None:
        records.append(replacement)
    if records:
        ordered = tuple(sorted(records, key=lambda item: str(item.component)))
        clients[client] = ClientOwnership(ordered)
    else:
        clients.pop(client, None)
    return OwnershipManifest(manifest.registry_sha256, manifest.selected_targets_sha256, clients)


def _adapter_provenance(
    route: AdapterRoute,
    entry_points: Mapping[str, importlib.metadata.EntryPoint],
) -> AdapterProvenance:
    if route.builtin:
        return AdapterProvenance(route.adapter_id, route.distribution)
    entry_point = entry_points.get(route.adapter_id)
    if entry_point is None or entry_point.dist is None:
        raise AppError(f"adapter entry point is unavailable: {route.adapter_id}")
    version = getattr(entry_point.dist, "version", None)
    if version is None:
        try:
            version = importlib.metadata.version(route.distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise AppError(f"adapter distribution is unavailable: {route.distribution}") from exc
    if not isinstance(version, str) or not version:
        raise AppError(f"adapter distribution version is invalid: {route.adapter_id}")
    return AdapterProvenance(route.adapter_id, route.distribution, version)


def _validate_external_owned_artifact(
    route: AdapterRoute,
    record: ComponentOwnership | None,
    plan: ArtifactPlan,
    snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
) -> None:
    if route.builtin or record is None:
        return
    artifact_identities = {
        (artifact.artifact.grant_id, artifact.artifact.relative_path): artifact
        for artifact in plan.artifacts
    }
    owned_identities = {
        (artifact.grant_id, artifact.artifact_path) for artifact in record.artifacts
    }
    if set(artifact_identities) != owned_identities:
        raise AppError("external owned artifact identity does not match the manifest")
    for artifact in plan.artifacts:
        current = snapshots.get(artifact.artifact)
        owned = next(
            item
            for item in record.artifacts
            if (item.grant_id, item.artifact_path)
            == (artifact.artifact.grant_id, artifact.artifact.relative_path)
        )
        if (
            not isinstance(current, bytes)
            or hashlib.sha256(current).hexdigest() != owned.written_sha256
        ):
            raise AppError("external owned artifact has drifted")


def _vscode_proof_path() -> Path:
    configured = os.environ.get("MODFIG_VSCODE_PROOF")
    if configured:
        return Path(configured).expanduser().absolute()
    return (Path.home() / ".modfig" / "vscode-runtime-proof.json").absolute()


def _configured_vscode_user_data_root() -> Path | None:
    configured = os.environ.get("MODFIG_VSCODE_USER_DATA_ROOT")
    if not configured:
        return None
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise AppError("MODFIG_VSCODE_USER_DATA_ROOT must be absolute")
    return root.absolute()


def _load_public_vscode_proof() -> RuntimeProof:
    user_data_root = _configured_vscode_user_data_root()
    if user_data_root is None:
        return vscode.load_vscode_runtime_proof(_vscode_proof_path())
    return vscode.load_vscode_runtime_proof(_vscode_proof_path(), user_data_root=user_data_root)


def _vscode_runtime_proof(
    declaration: PreflightDeclaration,
    destinations: Mapping[tuple[str, Component, ArtifactIdentity], Path],
    proof: RuntimeProof | None,
) -> RuntimeProof:
    if proof is None:
        raise AppError("VS Code runtime proof is unavailable")
    runtime = proof.provenance
    if not isinstance(runtime, vscode.VSCodeRuntime):
        raise AppError("VS Code runtime proof is unavailable")
    try:
        vscode.bind_vscode_runtime_paths(
            runtime,
            settings_path=destinations[("vscode", "core", vscode._VSCODE_ARTIFACT)],
            state_db_path=destinations[("vscode", "core", vscode._VSCODE_STATE_ARTIFACTS[0])],
            state_wal_path=destinations[("vscode", "core", vscode._VSCODE_STATE_ARTIFACTS[1])],
            state_shm_path=destinations[("vscode", "core", vscode._VSCODE_STATE_ARTIFACTS[2])],
        )
    except KeyError as exc:
        raise AppError("VS Code destination paths are incomplete") from exc
    return RuntimeProof(
        {"channel": runtime.channel, "platform": runtime.os_name, "profile": runtime.profile_mode},
        preflight_declaration_sha256(declaration),
        provenance=runtime,
    )


def _snapshot_vscode_bundle(
    paths: DatabasePaths,
) -> tuple[dict[Path, ArtifactSnapshot], dict[Path, FileVersion]]:
    before_versions = {
        path: inspect_private_file(path, "VS Code state database member")
        for path in paths.members()
    }
    raw = snapshot_members(paths)
    snapshots: dict[Path, ArtifactSnapshot] = {}
    versions: dict[Path, FileVersion] = {}
    for path, content in raw.items():
        version = inspect_private_file(path, "VS Code state database member")
        if content is None:
            snapshots[path] = AbsentDestination()
        else:
            if len(content) > 16 * 1024 * 1024:
                raise AppError("VS Code state database member exceeds 16 MiB")
            if hashlib.sha256(content).hexdigest() != version.sha256:
                raise AppError("VS Code state database member changed while snapshotting")
            snapshots[path] = content
        versions[path] = version
    if any(versions[path] != before_versions[path] for path in paths.members()):
        raise AppError("VS Code state database member changed while snapshotting")
    if any(
        inspect_private_file(path, "VS Code state database member") != versions[path]
        for path in paths.members()
    ):
        raise AppError("VS Code state database member changed while snapshotting")
    return snapshots, versions


def _snapshot(path: Path) -> tuple[ArtifactSnapshot, FileVersion]:
    version = inspect_private_file(path, "adapter artifact")
    if not version.exists:
        return AbsentDestination(), version
    content = read_private_bytes(path, "adapter artifact")
    if len(content) > 16 * 1024 * 1024:
        raise AppError("adapter artifact exceeds 16 MiB")
    if inspect_private_file(path, "adapter artifact") != version:
        raise AppError("adapter artifact changed while snapshotting")
    return content, version


def _manifest_expected(snapshot: OwnershipManifestSnapshot, path: Path) -> FileVersion:
    return (
        inspect_private_file(path, "manifest")
        if snapshot._version.parent_identity == (0, 0)
        else snapshot._version
    )


def _pending_backup_set_ids(journal_path: Path) -> tuple[str, ...]:
    if not journal_path.exists():
        return ()
    from .journal import load_journal

    return (load_journal(journal_path).backup_set,)


def _trusted_recovery_destination(item: TransactionArtifact) -> Path:
    routes = _merged_adapter_routes(include_chatgpt=item.logical_client == "chatgpt")
    if routes is None:
        routes = _builtin_adapter_routes()
    for route in routes:
        if (
            route.logical_client == item.logical_client
            and route.component == item.component
            and route.adapter_id == item.adapter_id
        ):
            identity = ArtifactIdentity(item.grant_id, item.artifact_path)
            return _grant_destination(route, identity, write=True)
    raise AppError("pending journal destination is not trusted")


def _recover_pending(
    journal_path: Path,
    backup_root: Path,
    *,
    trusted_manifest_path: Path | None = None,
) -> None:
    from .recovery import _recover_transaction

    _recover_transaction(
        journal_path,
        backup_root,
        trusted_manifest_path=trusted_manifest_path,
        trusted_destinations=(),
        trusted_destination_resolver=_trusted_recovery_destination,
    )


def _selected_apply_clients(
    config: str | None, target: str, manifest_path: Path
) -> tuple[str, ...]:
    registry = load_valid_registry(config)
    manifest = load_ownership_manifest_snapshot(manifest_path)
    return selected_clients(
        target, registry, ownership_manifest_owned_components(manifest.manifest)
    )


def _apply_transaction(
    config: str | None,
    target: str,
    yes: bool,
    proofs: Mapping[tuple[str, Component], RuntimeProof | None] | None = None,
    *,
    journal_path: Path | None = None,
    backup_root: Path | None = None,
) -> None:
    if proofs is None:
        proofs = {}
    validated = validate_logical_client(target)
    if not yes and validated not in {"factory", "all"}:
        raise AppError("apply requires --yes")
    manifest_path = resolve_manifest_path().absolute()
    journal_path = (journal_path or manifest_path.with_name("pending.json")).absolute()
    backup_root = (backup_root or manifest_path.with_name("backups")).absolute()
    if journal_path.exists():
        _recover_pending(
            journal_path,
            backup_root,
            trusted_manifest_path=manifest_path,
        )

    registry = load_valid_registry(config)
    manifest_preview = load_ownership_manifest_snapshot(manifest_path)
    owned = ownership_manifest_owned_components(manifest_preview.manifest)
    clients = selected_clients(validated, registry, owned)
    # ponytail: probe runs only for selected Factory openai models, before any
    # backup or client mutation, so a failing endpoint aborts the transaction.
    if "factory" in clients:
        factory.probe_factory_responses(registry, os.environ)
    routes = _merged_adapter_routes(include_chatgpt="chatgpt" in clients)
    selected = tuple(
        (client, component, route)
        for client in clients
        for component, route in _resolve_selected_routes(client, registry, owned, routes)
    )
    for client, component, route in selected:
        record = _component_record(manifest_preview.manifest, client, component)
        _validate_record_route(record, route)

    entry_points = (
        discover_adapter_entry_points()
        if any(not route.builtin for _, _, route in selected)
        else {}
    )
    prepared: list[tuple[str, Component, AdapterRoute, AdapterV1, PreflightDeclaration]] = []
    destinations: dict[tuple[str, Component, ArtifactIdentity], Path] = {}
    all_destinations: set[Path] = set()
    for client, component, route in selected:
        adapter = _adapter_for_route(route, entry_points)
        config_value = registry.client_component(client, component) or {}
        validation_context = AdapterValidationContext(
            client,
            component,
            partial(registry.resolve_model, logical_client=client),
        )
        adapter.validate(config_value, validation_context)
        record = _component_record(manifest_preview.manifest, client, component)
        plan_context = adapter_plan_context(client, component, registry)
        declaration = adapter.preflight(
            AdapterContext(
                client,
                component,
                plan_context.models,
                _chatgpt_record_ownership(record),
            )
            if route.adapter_id == "modfig.chatgpt"
            else AdapterContext(client, component)
        )
        for request in declaration.read_requests:
            _grant_destination(route, request.artifact, write=False)
        for write in declaration.prospective_writes:
            destination = _grant_destination(route, write.artifact, write=True)
            if destination in all_destinations:
                raise AppError(f"duplicate or overlapping destination: {destination}")
            all_destinations.add(destination)
            destinations[(client, component, write.artifact)] = destination
        prepared.append((client, component, route, adapter, declaration))
    _require_unique_destinations(tuple(destinations.values()))

    invocation_id = uuid.uuid4().hex
    with operation_locks(
        journal_path,
        iter((manifest_path, *(sorted(all_destinations, key=str)))),
        invocation_id,
    ) as locks:
        manifest_snapshot = load_ownership_manifest_snapshot(manifest_path)
        if manifest_snapshot.manifest != manifest_preview.manifest:
            raise AppError("manifest changed before planning")
        plans: list[
            tuple[
                str,
                Component,
                AdapterRoute,
                AdapterV1,
                RuntimeProof | None,
                ArtifactPlan,
                dict[ArtifactIdentity, ArtifactSnapshot],
                dict[Path, FileVersion],
            ]
        ] = []
        for client, component, route, adapter, declaration in prepared:
            record = _component_record(manifest_snapshot.manifest, client, component)
            _validate_record_route(record, route)
            if route.adapter_id == "modfig.oh_my_droid":
                current_declaration = adapter.preflight(AdapterContext(client, component))
                if current_declaration != declaration:
                    raise AppError("oh-my-droid preflight declaration changed before snapshotting")
            proof = (
                None if route.adapter_id == "modfig.factory" else proofs.get((client, component))
            )
            if route.adapter_id == "modfig.vscode" and proof is None:
                proof = _load_public_vscode_proof()
            if route.adapter_id == "modfig.vscode":
                proof = _vscode_runtime_proof(declaration, destinations, proof)
                runtime = proof.provenance
                if not isinstance(runtime, vscode.VSCodeRuntime):
                    raise AppError("VS Code runtime proof is unavailable")
                vscode.preflight(runtime)
            elif route.adapter_id == "modfig.chatgpt":
                if proof is None:
                    proof = _load_public_chatgpt_proof()
                proof = _chatgpt_runtime_proof(declaration, route, proof)
            elif proof is None and not route.builtin:
                raise AppError(f"runtime proof unavailable for {client} {component!r}")
            if proof is not None and proof.declaration_sha256 != preflight_declaration_sha256(
                declaration
            ):
                raise AppError("runtime proof does not match the preflight declaration")
            ownership = (
                _chatgpt_record_ownership(record)
                if route.adapter_id == "modfig.chatgpt"
                else ({} if record is None else record.ownership)
            )
            plan_context = adapter_plan_context(client, component, registry)
            snapshots: dict[ArtifactIdentity, ArtifactSnapshot] = {}
            versions: dict[Path, FileVersion] = {}
            identities = {request.artifact for request in declaration.read_requests} | {
                write.artifact for write in declaration.prospective_writes
            }
            state_bundle = (
                DatabasePaths(
                    destinations[(client, component, vscode._VSCODE_STATE_ARTIFACTS[0])],
                    destinations[(client, component, vscode._VSCODE_STATE_ARTIFACTS[1])],
                    destinations[(client, component, vscode._VSCODE_STATE_ARTIFACTS[2])],
                )
                if route.adapter_id == "modfig.vscode"
                else None
            )
            if state_bundle is not None:
                bundle_snapshots, bundle_versions = _snapshot_vscode_bundle(state_bundle)
                for identity, path in zip(
                    vscode._VSCODE_STATE_ARTIFACTS,
                    state_bundle.members(),
                    strict=True,
                ):
                    snapshots[identity] = bundle_snapshots[path]
                    versions[path] = bundle_versions[path]
                identities -= set(vscode._VSCODE_STATE_ARTIFACTS)
            for identity in identities:
                path = _grant_destination(
                    route,
                    identity,
                    write=identity in {write.artifact for write in declaration.prospective_writes},
                )
                if (
                    route.adapter_id == "modfig.oh_my_droid"
                    and identity.grant_id == oh_my_droid.PLUGIN_GRANT
                ):
                    snapshots[identity], versions[path] = oh_my_droid.snapshot_plugin_file(path)
                elif (
                    route.adapter_id == "modfig.oh_my_droid"
                    and identity.grant_id == oh_my_droid.DROID_GRANT
                ):
                    snapshots[identity], versions[path] = oh_my_droid.snapshot_droid_file(path)
                else:
                    snapshots[identity], versions[path] = _snapshot(path)
            if route.adapter_id == "modfig.factory":
                plan = factory.adapter.plan(plan_context, snapshots, ownership)
            else:
                plan = adapter.plan(plan_context, proof, snapshots, ownership)
            validate_plan_against_declarations(plan, declaration, plan_context)
            if not plan.artifacts:
                raise AppError("transaction requires at least one artifact per client component")
            _validate_external_owned_artifact(route, record, plan, snapshots)
            for artifact in plan.artifacts:
                if isinstance(artifact.planned, bytes) and len(artifact.planned) > 16 * 1024 * 1024:
                    raise AppError("planned output exceeds 16 MiB")
            plans.append((client, component, route, adapter, proof, plan, snapshots, versions))

        if not any(
            artifact.planned != snapshots[artifact.artifact]
            for _client, _component, _route, _adapter, _proof, plan, snapshots, _versions in plans
            for artifact in plan.artifacts
        ):
            return
        artifacts: list[TransactionArtifact] = []
        affected_model_ids: list[str] = []
        updated_manifest = manifest_snapshot.manifest
        for _client, _component, _route, _adapter, _proof, plan, _snapshots, _versions in plans:
            affected = plan.ownership.get("affectedModelIds", ())
            if isinstance(affected, (list, tuple)):
                affected_model_ids.extend(item for item in affected if isinstance(item, str))
        _acknowledge_factory_warning(tuple(dict.fromkeys(affected_model_ids)), yes)
        for client, component, route, _adapter, _proof, plan, _snapshots, versions in plans:
            for artifact in plan.artifacts:
                destination = destinations[(client, component, artifact.artifact)]
                before = versions[destination]
                after_sha = (
                    None
                    if isinstance(artifact.planned, AbsentDestination)
                    else hashlib.sha256(artifact.planned).hexdigest()
                )
                artifacts.append(
                    TransactionArtifact(
                        client,
                        component,
                        route.adapter_id,
                        artifact.artifact.grant_id,
                        artifact.artifact.relative_path,
                        destination,
                        before,
                        after_sha,
                    )
                )
        for client, component, route, _adapter, _proof, plan, _snapshots, _versions in plans:
            owned_artifacts = tuple(
                OwnedArtifact(
                    artifact.artifact.grant_id,
                    artifact.artifact.relative_path,
                    next(
                        item.before_version.sha256
                        for item in artifacts
                        if item.logical_client == client
                        and item.component == component
                        and item.grant_id == artifact.artifact.grant_id
                        and item.artifact_path == artifact.artifact.relative_path
                    ),
                    hashlib.sha256(artifact.planned).hexdigest()
                    if isinstance(artifact.planned, bytes)
                    else None,
                )
                for artifact in plan.artifacts
                if isinstance(artifact.planned, bytes)
            )
            replacement = (
                None
                if not owned_artifacts
                else ComponentOwnership(
                    component,
                    _adapter_provenance(route, entry_points),
                    owned_artifacts[0].grant_id,
                    owned_artifacts[0].artifact_path,
                    owned_artifacts[0].preimage_sha256,
                    owned_artifacts[0].written_sha256,
                    plan.ownership,
                    owned_artifacts,
                )
            )
            updated_manifest = _replace_record(updated_manifest, client, replacement, component)
        config_path = resolve_config_path(config).path
        updated_manifest = OwnershipManifest(
            hashlib.sha256(read_private_bytes(config_path, "registry")).hexdigest(),
            hashlib.sha256("\n".join(clients).encode()).hexdigest(),
            updated_manifest.clients,
        )
        after_manifest = ownership_manifest_bytes(updated_manifest)
        backup_request = BackupRequest(
            invocation_id,
            invocation_id,
            tuple(BackupArtifact(item.destination, item.before_version) for item in artifacts),
            frozenset(
                destinations[(client, component, artifact.artifact)]
                for client, component, route, _adapter, _proof, plan, _snapshots, _versions in plans
                if route.adapter_id == "modfig.oh_my_droid"
                for artifact in plan.artifacts
            ),
        )
        backup = create_backup_set(
            backup_root,
            backup_request,
            protected_set_ids=_pending_backup_set_ids(journal_path),
        )
        journal = InvocationJournal(
            invocation_id,
            manifest_path,
            manifest_snapshot.serialized,
            _manifest_expected(manifest_snapshot, manifest_path),
            after_manifest,
            hashlib.sha256(after_manifest).hexdigest(),
            tuple(artifacts),
            backup.path.name,
            backup.integrity,
        )
        journal_version = inspect_private_file(journal_path, "pending journal")
        try:
            journal_version = save_journal(
                journal_path,
                journal,
                journal_version,
                locks[journal_path],
            )
        except Exception:
            remove_backup_set(backup_root, backup)
            raise

        current_versions = {item.destination: item.before_version for item in artifacts}
        written_versions: dict[Path, FileVersion] = {}
        try:
            for client, component, route, adapter, proof, plan, _snapshots, _versions in plans:
                if route.adapter_id == "modfig.factory":
                    factory.adapter.recheck()
                else:
                    adapter.recheck(proof)
                written: list[ArtifactSnapshot] = []
                for artifact in plan.artifacts:
                    destination = destinations[(client, component, artifact.artifact)]
                    if isinstance(artifact.planned, AbsentDestination):
                        conditional_delete(
                            destination,
                            current_versions[destination],
                            "adapter artifact",
                            writer_exclusion=locks[destination],
                        )
                        written_versions[destination] = inspect_private_file(
                            destination, "adapter artifact"
                        )
                    else:
                        written_versions[destination] = conditional_write_bytes(
                            destination,
                            artifact.planned,
                            current_versions[destination],
                            "adapter artifact",
                            writer_exclusion=locks[destination],
                        )
                    if route.adapter_id == "modfig.oh_my_droid":
                        written_snapshot, _ = oh_my_droid.snapshot_droid_file(destination)
                    else:
                        written_snapshot, _ = _snapshot(destination)
                    if written_snapshot != artifact.planned:
                        raise AppError("post-write state does not match planned artifact")
                    written.append(written_snapshot)
                if route.adapter_id == "modfig.factory":
                    factory.adapter.verify(AdapterContext(client, component), tuple(written))
                elif route.adapter_id == "modfig.vscode":
                    assert proof is not None
                    vscode.adapter.verify(
                        AdapterContext(client, component), proof, tuple(written), plan.ownership
                    )
                else:
                    verify_context = (
                        AdapterContext(
                            client,
                            component,
                            adapter_plan_context(client, component, registry).models,
                            plan.ownership,
                        )
                        if route.adapter_id == "modfig.chatgpt"
                        else AdapterContext(client, component)
                    )
                    adapter.verify(verify_context, proof, tuple(written))
            conditional_write_bytes(
                manifest_path,
                after_manifest,
                _manifest_expected(manifest_snapshot, manifest_path),
                "manifest",
                writer_exclusion=locks[manifest_path],
            )
        except Exception:
            prestates = validate_backup_set(backup, backup_request)
            try:
                for item in artifacts:
                    expected = written_versions.get(item.destination)
                    if expected is None:
                        continue
                    content = prestates[item.destination]
                    if content is None:
                        conditional_delete(
                            item.destination,
                            expected,
                            "rollback destination",
                            writer_exclusion=locks[item.destination],
                        )
                    else:
                        conditional_write_bytes(
                            item.destination,
                            content,
                            expected,
                            "rollback destination",
                            writer_exclusion=locks[item.destination],
                        )
                if inspect_private_file(manifest_path, "manifest") != _manifest_expected(
                    manifest_snapshot, manifest_path
                ):
                    raise AppError("manifest changed during rollback")
                conditional_delete(
                    journal_path,
                    journal_version,
                    "pending journal",
                    writer_exclusion=locks[journal_path],
                )
            except Exception as rollback_error:
                raise AppError(
                    "transaction failed and rollback is incomplete; recovery required"
                ) from rollback_error
            remove_backup_set(backup_root, backup)
            raise
        conditional_delete(
            journal_path,
            journal_version,
            "pending journal",
            writer_exclusion=locks[journal_path],
        )
    remove_backup_set(backup_root, backup)


def apply(config: str | None, target: str, yes: bool) -> None:
    validated = validate_logical_client(target)
    if not yes and validated not in {"factory", "all"}:
        raise AppError("apply requires --yes")
    if validated in {"factory", "all"}:
        _apply_transaction(config, validated, yes, {})
        return
    _apply_transaction(config, validated, yes, {})


def run(command: str, config: str | None, target: str = "all", yes: bool = False) -> None:
    if command == "diff":
        diff(config, target)
        return
    if command == "apply":
        apply(config, target, yes)
        return
    raise AppError(f"unsupported command: {command}")
