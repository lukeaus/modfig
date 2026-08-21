from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .components import Component, ExtensionComponent
from .errors import AppError
from .locking import OperationLock
from .platform import PrivateParentMissingError, open_private_parent
from .storage import (
    ConcurrentModificationError,
    FileVersion,
    _read_private_at,
    conditional_write_bytes,
    inspect_private_file,
)

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: str, label: str) -> None:
    if not _HEX_SHA256.fullmatch(value):
        raise AppError(f"manifest {label} must be a SHA-256 value")


def _string(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise AppError(f"manifest {label} must be a string")
    return value


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-standard JSON number")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _parse_manifest_object(content: bytes, source: str) -> dict[str, Any]:
    try:
        raw = json.loads(
            content,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AppError(f"cannot read manifest {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AppError(f"manifest must be a JSON object: {source}")
    return raw


# --- Manifest v3: generic client/component ownership -------------------------
#
# v3 replaces the v2 target-specific ownership union with generic per-client,
# per-component ownership records. Each record carries adapter provenance, the
# grant ID it was written under, a canonical grant-relative artifact path, the
# preimage hash (None until a prior write exists), the written content hash, and
# opaque JSON-safe adapter ownership. FileVersion is intentionally NOT persisted
# here; transactional versioning belongs to the future journal/snapshot.


_LOGICAL_CLIENT_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_GRANT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ADAPTER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _component_name(component: Component) -> str:
    return "core" if component == "core" else component.name


@dataclass(frozen=True)
class AdapterProvenance:
    adapter_id: str
    distribution: str
    version: str | None = None

    def __post_init__(self) -> None:
        if not _ADAPTER_ID_RE.fullmatch(self.adapter_id):
            raise AppError("manifest adapter provenance id must be a stable identifier")
        if not self.distribution:
            raise AppError("manifest adapter provenance distribution must not be empty")
        if self.version is not None and not self.version:
            raise AppError("manifest adapter provenance version must not be empty")


def _validate_artifact_path(path: PurePosixPath) -> None:
    if path.is_absolute():
        raise AppError("manifest artifact path must be relative")
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise AppError("manifest artifact path must not escape its grant root")
    if any("\\" in part or part.startswith("/") for part in parts):
        raise AppError("manifest artifact path must be a clean relative POSIX path")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise AppError("manifest JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _validate_json_safe(value: Any, label: str) -> None:
    try:
        json.dumps(_json_value(value), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AppError(f"manifest {label} must be JSON-safe") from exc


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise AppError("manifest JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True)
class OwnedArtifact:
    grant_id: str
    artifact_path: PurePosixPath
    preimage_sha256: str | None
    written_sha256: str

    def __post_init__(self) -> None:
        if not _GRANT_ID_RE.fullmatch(self.grant_id):
            raise AppError("manifest component grant id must be a stable identifier")
        _validate_artifact_path(self.artifact_path)
        if self.preimage_sha256 is not None:
            _digest(self.preimage_sha256, "component preimage")
        _digest(self.written_sha256, "component written hash")


@dataclass(frozen=True)
class ComponentOwnership:
    component: Component
    adapter: AdapterProvenance
    grant_id: str
    artifact_path: PurePosixPath
    preimage_sha256: str | None
    written_sha256: str
    ownership: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    artifacts: tuple[OwnedArtifact, ...] = ()

    def __post_init__(self) -> None:
        entries = self.artifacts
        if not entries:
            entries = (
                OwnedArtifact(
                    self.grant_id,
                    self.artifact_path,
                    self.preimage_sha256,
                    self.written_sha256,
                ),
            )
        else:
            entries = tuple(entries)
            if not entries:
                raise AppError("manifest component artifacts must not be empty")
        identities = [(item.grant_id, item.artifact_path) for item in entries]
        if len(identities) != len(set(identities)):
            raise AppError("manifest component artifacts must be unique")
        object.__setattr__(self, "artifacts", entries)
        first = entries[0]
        object.__setattr__(self, "grant_id", first.grant_id)
        object.__setattr__(self, "artifact_path", first.artifact_path)
        object.__setattr__(self, "preimage_sha256", first.preimage_sha256)
        object.__setattr__(self, "written_sha256", first.written_sha256)
        _validate_json_safe(dict(self.ownership), "component ownership")
        object.__setattr__(self, "ownership", _freeze_json(self.ownership))


@dataclass(frozen=True)
class ClientOwnership:
    components: tuple[ComponentOwnership, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for record in self.components:
            name = _component_name(record.component)
            if name in seen:
                raise AppError("manifest client has duplicate component ownership")
            seen.add(name)


@dataclass(frozen=True)
class OwnershipManifest:
    registry_sha256: str = "0" * 64
    selected_targets_sha256: str = "0" * 64
    clients: dict[str, ClientOwnership] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _digest(self.registry_sha256, "registry digest")
        _digest(self.selected_targets_sha256, "selected target digest")
        for name in self.clients:
            if not _LOGICAL_CLIENT_RE.fullmatch(name):
                raise AppError("manifest client name must be a logical identifier")


@dataclass(frozen=True)
class OwnershipManifestSnapshot:
    manifest: OwnershipManifest
    serialized: bytes | None
    sha256: str | None
    _version: FileVersion = field(repr=False)


def _component_payload(component: Component) -> str:
    return _component_name(component)


def _ownership_record_payload(record: ComponentOwnership) -> dict[str, Any]:
    return {
        "component": _component_payload(record.component),
        "adapter": {
            "adapterId": record.adapter.adapter_id,
            "distribution": record.adapter.distribution,
            **({"version": record.adapter.version} if record.adapter.version is not None else {}),
        },
        **(
            {
                "artifacts": [
                    {
                        "grantId": artifact.grant_id,
                        "artifactPath": str(artifact.artifact_path),
                        **(
                            {"preimageSha256": artifact.preimage_sha256}
                            if artifact.preimage_sha256 is not None
                            else {}
                        ),
                        "writtenSha256": artifact.written_sha256,
                    }
                    for artifact in record.artifacts
                ]
            }
            if len(record.artifacts) > 1
            else {
                "grantId": record.grant_id,
                "artifactPath": str(record.artifact_path),
                **(
                    {"preimageSha256": record.preimage_sha256}
                    if record.preimage_sha256 is not None
                    else {}
                ),
                "writtenSha256": record.written_sha256,
            }
        ),
        "ownership": _json_value(record.ownership),
    }


def ownership_manifest_payload(manifest: OwnershipManifest) -> dict[str, Any]:
    return {
        "manifestVersion": 3,
        "registrySha256": manifest.registry_sha256,
        "selectedTargetsSha256": manifest.selected_targets_sha256,
        "clients": {
            name: {
                "components": [_ownership_record_payload(record) for record in client.components]
            }
            for name, client in sorted(manifest.clients.items())
        },
    }


def ownership_manifest_bytes(manifest: OwnershipManifest) -> bytes:
    return (
        json.dumps(ownership_manifest_payload(manifest), indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode()


def _parse_adapter(raw: Any) -> AdapterProvenance:
    if not isinstance(raw, dict):
        raise AppError("manifest adapter provenance must be an object")
    version = raw.get("version")
    if "version" in raw and (not isinstance(version, str) or not version):
        raise AppError("manifest adapter provenance version must be a non-empty string")
    return AdapterProvenance(
        adapter_id=_string(raw, "adapterId", "adapter id"),
        distribution=_string(raw, "distribution", "adapter distribution"),
        version=version,
    )


def _parse_component(raw: Any) -> Component:
    name = raw if isinstance(raw, str) else None
    if name is None and isinstance(raw, dict):
        name = raw.get("component")
    if not isinstance(name, str):
        raise AppError("manifest component must be a string")
    return "core" if name == "core" else ExtensionComponent(name)


def _parse_ownership_record(raw: Any) -> ComponentOwnership:
    if not isinstance(raw, dict):
        raise AppError("manifest component ownership must be an object")
    preimage = raw.get("preimageSha256")
    if preimage is not None and not isinstance(preimage, str):
        raise AppError("manifest component preimage must be a string")
    ownership_raw = raw.get("ownership", {})
    if not isinstance(ownership_raw, dict):
        raise AppError("manifest component ownership payload must be an object")
    raw_artifacts = raw.get("artifacts")
    if raw_artifacts is not None:
        if not isinstance(raw_artifacts, list):
            raise AppError("manifest component artifacts must be a list")
        artifacts: list[OwnedArtifact] = []
        for item in raw_artifacts:
            if not isinstance(item, dict):
                raise AppError("manifest component artifact must be an object")
            item_preimage = item.get("preimageSha256")
            if item_preimage is not None and not isinstance(item_preimage, str):
                raise AppError("manifest component preimage must be a string")
            artifacts.append(
                OwnedArtifact(
                    _string(item, "grantId", "component grant id"),
                    PurePosixPath(_string(item, "artifactPath", "component artifact path")),
                    item_preimage,
                    _string(item, "writtenSha256", "component written hash"),
                )
            )
        if not artifacts:
            raise AppError("manifest component artifacts must not be empty")
        return ComponentOwnership(
            component=_parse_component(raw),
            adapter=_parse_adapter(raw.get("adapter")),
            grant_id=artifacts[0].grant_id,
            artifact_path=artifacts[0].artifact_path,
            preimage_sha256=artifacts[0].preimage_sha256,
            written_sha256=artifacts[0].written_sha256,
            ownership=ownership_raw,
            artifacts=tuple(artifacts),
        )
    return ComponentOwnership(
        component=_parse_component(raw),
        adapter=_parse_adapter(raw.get("adapter")),
        grant_id=_string(raw, "grantId", "component grant id"),
        artifact_path=PurePosixPath(_string(raw, "artifactPath", "component artifact path")),
        preimage_sha256=preimage,
        written_sha256=_string(raw, "writtenSha256", "component written hash"),
        ownership=ownership_raw,
    )


def _parse_ownership_manifest(raw: dict[str, Any], source: str) -> OwnershipManifest:
    if raw.get("manifestVersion") != 3:
        raise AppError(f"manifest has unsupported version; current manifest v3 required: {source}")
    clients_raw = raw.get("clients")
    if not isinstance(clients_raw, dict) or not all(isinstance(name, str) for name in clients_raw):
        raise AppError("manifest clients must be an object")
    clients: dict[str, ClientOwnership] = {}
    for name, value in clients_raw.items():
        if not isinstance(value, dict) or not isinstance(value.get("components"), list):
            raise AppError("manifest client must be an object with components")
        clients[name] = ClientOwnership(
            components=tuple(_parse_ownership_record(item) for item in value["components"])
        )
    return OwnershipManifest(
        registry_sha256=_string(raw, "registrySha256", "registry digest"),
        selected_targets_sha256=_string(raw, "selectedTargetsSha256", "selected target digest"),
        clients=clients,
    )


def parse_ownership_manifest_bytes(
    content: bytes, *, source: str = "manifest"
) -> OwnershipManifest:
    return _parse_ownership_manifest(_parse_manifest_object(content, source), source)


def load_ownership_manifest_snapshot(path: Path) -> OwnershipManifestSnapshot:
    # Missing manifest, including a missing parent directory, is an empty v3
    # state. A non-private or symlinked parent still fails closed via open_private_parent.
    try:
        parent_descriptor = open_private_parent(path, "manifest")
    except PrivateParentMissingError:
        version = FileVersion((0, 0), None, None, None)
        return OwnershipManifestSnapshot(OwnershipManifest(), None, None, version)
    try:
        parent = os.fstat(parent_descriptor)
        result = _read_private_at(parent_descriptor, path, "manifest", missing_ok=True)
        if result is None:
            version = FileVersion((parent.st_dev, parent.st_ino), None, None, None)
            return OwnershipManifestSnapshot(OwnershipManifest(), None, None, version)
        status, content = result
        digest = hashlib.sha256(content).hexdigest()
        version = FileVersion(
            (parent.st_dev, parent.st_ino),
            (status.st_dev, status.st_ino),
            len(content),
            digest,
        )
        manifest = _parse_ownership_manifest(_parse_manifest_object(content, str(path)), str(path))
        return OwnershipManifestSnapshot(manifest, content, digest, version)
    finally:
        os.close(parent_descriptor)


def load_ownership_manifest(path: Path) -> OwnershipManifest:
    return load_ownership_manifest_snapshot(path).manifest


def save_ownership_manifest(
    path: Path,
    manifest: OwnershipManifest,
    snapshot: OwnershipManifestSnapshot,
    lock: OperationLock,
) -> FileVersion:
    expected = snapshot._version
    if expected.parent_identity == (0, 0):
        current = inspect_private_file(path, "manifest")
        if current.exists:
            raise ConcurrentModificationError(f"manifest changed before mutation: {path}")
        expected = current
    return conditional_write_bytes(
        path,
        ownership_manifest_bytes(manifest),
        expected,
        "manifest",
        writer_exclusion=lock,
    )


def ownership_manifest_owned_components(
    manifest: OwnershipManifest,
) -> Mapping[str, tuple[Component, ...]]:
    """Selected v3 component ownership keyed by logical client."""
    return MappingProxyType(
        {
            name: tuple(record.component for record in client.components)
            for name, client in manifest.clients.items()
        }
    )
