from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Protocol, TypeAlias, runtime_checkable

from .adapter_routes import AdapterRoute, AdapterRouteError
from .components import Component
from .errors import AppError
from .manifest import _freeze_json, _json_value
from .registry import ModelReference


class AdapterPlanError(AppError):
    """An adapter plan or declaration violates the host-validated contract."""


_FEATURE_KEY_RE = re.compile(r"^features\.[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


@dataclass(frozen=True)
class AdapterMetadata:
    adapter_id: str
    logical_client: str
    component: Component


@dataclass(frozen=True)
class ArtifactIdentity:
    grant_id: str
    relative_path: PurePosixPath

    def __post_init__(self) -> None:
        if not self.grant_id:
            raise AdapterPlanError("artifact grant id must not be empty")
        validate_artifact_relative_path(self.relative_path)


@dataclass(frozen=True)
class SnapshotRequest:
    artifact: ArtifactIdentity


@dataclass(frozen=True)
class AbsentDestination:
    """Sentinel marking that a destination has no prior or planned bytes.

    Distinct from ``None`` (which is not a valid plan/snapshot value) and from
    empty bytes (which is a real, present file).
    """


PlannedBytes: TypeAlias = bytes | AbsentDestination
ArtifactSnapshot: TypeAlias = bytes | AbsentDestination
AdapterOwnership: TypeAlias = Mapping[str, object]


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    validate_json_safe(value, "contract payload")
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def _freeze_config(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


@dataclass(frozen=True)
class PlannedArtifact:
    artifact: ArtifactIdentity
    planned: PlannedBytes
    feature_key: str
    reconciliation: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.planned is None:
            raise AdapterPlanError("planned artifact bytes must be bytes or AbsentDestination")
        if not isinstance(self.planned, (bytes, AbsentDestination)):
            raise AdapterPlanError("planned artifact bytes must be bytes or AbsentDestination")
        validate_feature_key(self.feature_key)
        validate_json_safe(self.reconciliation, "reconciliation")
        object.__setattr__(self, "reconciliation", _freeze_mapping(self.reconciliation))


@dataclass(frozen=True)
class ArtifactPlan:
    artifacts: tuple[PlannedArtifact, ...]
    ownership: Mapping[str, object]

    def __post_init__(self) -> None:
        validate_json_safe(self.ownership, "ownership")
        object.__setattr__(self, "ownership", _freeze_mapping(self.ownership))
        artifacts = tuple(self.artifacts)
        object.__setattr__(self, "artifacts", artifacts)
        seen: set[ArtifactIdentity] = set()
        for artifact in artifacts:
            if artifact.artifact in seen:
                raise AdapterPlanError(f"duplicate artifact identity in plan: {artifact.artifact}")
            seen.add(artifact.artifact)


@dataclass(frozen=True)
class ResolvedModel:
    """Client-filtered model facts safe for adapter planning."""

    provider_key: str
    base_url: str
    api_key_reference: str
    model: str
    display_name: str
    max_output_tokens: int
    effective_provider: str
    no_image_support: bool
    favourite: bool
    factory_id: str
    vscode_id: str | None = None
    vscode_reasoning_levels: tuple[str, ...] = ()
    vscode_default_reasoning_level: str | None = None
    max_input_tokens: int | None = None
    tool_calling: bool = True
    provider_name: str | None = None
    chatgpt_provider_id: str | None = None
    chatgpt_wire_api: str | None = None
    chatgpt_catalog_id: str | None = None
    chatgpt_reasoning_levels: tuple[str, ...] = ()
    chatgpt_default: bool = False
    context_window: int | None = None


@dataclass(frozen=True)
class AdapterValidationContext:
    logical_client: str
    component: Component
    resolve_model: Callable[[ModelReference], object]


@dataclass(frozen=True)
class ProspectiveWrite:
    artifact: ArtifactIdentity


@dataclass(frozen=True)
class PreflightDeclaration:
    proof_requirements: Mapping[str, object]
    read_requests: tuple[SnapshotRequest, ...]
    prospective_writes: tuple[ProspectiveWrite, ...]

    def __post_init__(self) -> None:
        validate_json_safe(self.proof_requirements, "proof requirements")
        object.__setattr__(self, "proof_requirements", _freeze_mapping(self.proof_requirements))
        read_requests = tuple(self.read_requests)
        prospective_writes = tuple(self.prospective_writes)
        object.__setattr__(self, "read_requests", read_requests)
        object.__setattr__(self, "prospective_writes", prospective_writes)
        seen: set[ArtifactIdentity] = set()
        for write in prospective_writes:
            if write.artifact in seen:
                raise AdapterPlanError(
                    f"duplicate prospective write in declaration: {write.artifact}"
                )
            seen.add(write.artifact)


@dataclass(frozen=True)
class RuntimeProof:
    facts: Mapping[str, object]
    declaration_sha256: str
    recheck: Callable[[], RuntimeProof] | None = field(default=None, repr=False, compare=False)
    provenance: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", _freeze_mapping(self.facts))


@dataclass(frozen=True)
class AdapterContext:
    logical_client: str
    component: Component
    models: tuple[ResolvedModel, ...] = ()
    ownership: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "ownership", _freeze_config(self.ownership))


@dataclass(frozen=True)
class AdapterPlanContext:
    """Immutable plan-time context for a single selected client/component.

    Carries the bound logical client/component and the exact selected component
    config mapping, frozen. Adapters receive only this slice — never the full
    Registry, sibling client config, or mutable handles.
    """

    logical_client: str
    component: Component
    selected_config: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    models: tuple[ResolvedModel, ...] = ()
    _resolve_model: Callable[[ModelReference], ResolvedModel] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_config", _freeze_config(self.selected_config))
        object.__setattr__(self, "models", tuple(self.models))

    def resolve_model(self, reference: ModelReference) -> ResolvedModel:
        if self._resolve_model is None:
            raise AdapterPlanError("model resolver is unavailable")
        model = self._resolve_model(reference)
        if model not in self.models:
            raise AdapterPlanError("resolved model is outside the client-filtered snapshot")
        return model


@runtime_checkable
class AdapterV1(Protocol):
    def describe(self) -> AdapterMetadata: ...

    def validate(self, config: Mapping[str, object], context: AdapterValidationContext) -> None: ...

    def preflight(self, context: AdapterContext) -> PreflightDeclaration: ...

    def plan(
        self,
        context: AdapterPlanContext,
        proof: RuntimeProof | None,
        snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
        ownership: AdapterOwnership,
    ) -> ArtifactPlan: ...

    def recheck(self, proof: RuntimeProof | None) -> None: ...

    def verify(
        self,
        context: AdapterContext,
        proof: RuntimeProof | None,
        written: Sequence[ArtifactSnapshot],
    ) -> None: ...


def preflight_declaration_sha256(declaration: PreflightDeclaration) -> str:
    payload = {
        "proofRequirements": _json_value(declaration.proof_requirements),
        "readRequests": [
            {
                "grantId": request.artifact.grant_id,
                "relativePath": str(request.artifact.relative_path),
            }
            for request in declaration.read_requests
        ],
        "prospectiveWrites": [
            {"grantId": write.artifact.grant_id, "relativePath": str(write.artifact.relative_path)}
            for write in declaration.prospective_writes
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_artifact_relative_path(path: PurePosixPath) -> None:
    if path.is_absolute():
        raise AdapterPlanError("artifact relative path must not be absolute")
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise AdapterPlanError("artifact relative path must not escape its grant root")
    if any("\\" in part or part.startswith("/") for part in parts):
        raise AdapterPlanError("artifact relative path must be a clean relative POSIX path")


def validate_feature_key(key: str) -> None:
    if not key or not _FEATURE_KEY_RE.fullmatch(key):
        raise AdapterPlanError(f"invalid feature key: {key!r}")


def validate_json_safe(value: object, label: str) -> None:
    try:
        json.dumps(_json_value(value), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AdapterPlanError(f"{label} must be JSON-safe") from exc


def validate_plan_against_declarations(
    plan: ArtifactPlan,
    declaration: PreflightDeclaration,
    context: AdapterPlanContext,
) -> None:
    declared = {write.artifact for write in declaration.prospective_writes}
    component_name = "core" if context.component == "core" else context.component.name
    for artifact in plan.artifacts:
        if artifact.artifact not in declared:
            raise AdapterPlanError(
                f"plan artifact not declared as prospective write: {artifact.artifact}"
            )
        if not artifact.feature_key.startswith(f"features.{component_name}."):
            raise AdapterPlanError(
                f"feature key does not belong to selected component {component_name!r}: "
                f"{artifact.feature_key!r}"
            )


def discover_adapter_entry_points() -> Mapping[str, importlib.metadata.EntryPoint]:
    discovered: dict[str, importlib.metadata.EntryPoint] = {}
    for entry_point in importlib.metadata.entry_points(group="modfig.adapters.v1"):
        if entry_point.name in discovered:
            raise AdapterRouteError(f"duplicate adapter entry-point name: {entry_point.name}")
        discovered[entry_point.name] = entry_point
    return discovered


def load_enabled_adapter(
    route: AdapterRoute,
    *,
    entry_points: Mapping[str, importlib.metadata.EntryPoint] | None = None,
) -> AdapterV1:
    if not route.enabled:
        raise AdapterRouteError(f"adapter route {route.adapter_id!r} is disabled")
    available = discover_adapter_entry_points() if entry_points is None else entry_points
    entry_point = available.get(route.adapter_id)
    if entry_point is None:
        raise AdapterRouteError(f"adapter entry point is not installed: {route.adapter_id}")
    distribution = entry_point.dist
    if distribution is None or distribution.name != route.distribution:
        actual = None if distribution is None else distribution.name
        raise AdapterRouteError(
            f"adapter distribution mismatch for {route.adapter_id}: expected "
            f"{route.distribution!r}, found {actual!r}"
        )
    owned = [
        candidate
        for candidate in distribution.entry_points
        if candidate.group == "modfig.adapters.v1" and candidate.name == route.adapter_id
    ]
    if len(owned) != 1 or owned[0].value != entry_point.value:
        raise AdapterRouteError(f"adapter distribution ownership mismatch for {route.adapter_id}")
    loaded = entry_point.load()
    if not isinstance(loaded, AdapterV1):
        raise AdapterRouteError(f"entry point {route.adapter_id!r} is not an AdapterV1")
    expected = AdapterMetadata(route.adapter_id, route.logical_client, route.component)
    if loaded.describe() != expected:
        raise AdapterRouteError(f"adapter metadata does not match route for {route.adapter_id}")
    return loaded
