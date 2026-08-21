from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .components import Component, ExtensionComponent
from .errors import AppError
from .locking import OperationLock
from .manifest import (
    ComponentOwnership,
    OwnershipManifest,
    _component_name,
    _digest,
    _ownership_record_payload,
    parse_ownership_manifest_bytes,
)
from .storage import FileVersion, conditional_write_bytes, read_private_bytes

_LOGICAL_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_VERSION = 4


def _canonical(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


def _safe_component(value: str, label: str) -> None:
    if not value or value in {".", ".."} or Path(value).name != value:
        raise AppError(f"journal {label} must be a safe path component")


def _validate_version(version: FileVersion, label: str) -> None:
    if len(version.parent_identity) != 2 or any(value < 0 for value in version.parent_identity):
        raise AppError(f"journal {label} version parent identity is invalid")
    if version.exists:
        if (
            version.size is None
            or version.size < 0
            or version.sha256 is None
            or not re.fullmatch(r"[0-9a-f]{64}", version.sha256)
        ):
            raise AppError(f"journal {label} version is invalid")
    elif version.size is not None or version.sha256 is not None:
        raise AppError(f"journal absent {label} version is invalid")


def _component_payload(component: Component) -> dict[str, str]:
    return (
        {"kind": "core"} if component == "core" else {"kind": "extension", "name": component.name}
    )


def _parse_component(raw: Any) -> Component:
    if raw == {"kind": "core"}:
        return "core"
    if (
        isinstance(raw, dict)
        and set(raw) == {"kind", "name"}
        and raw.get("kind") == "extension"
        and isinstance(raw["name"], str)
    ):
        try:
            return ExtensionComponent(raw["name"])
        except ValueError as exc:
            raise AppError("invalid pending journal extension component") from exc
    raise AppError("invalid pending journal component")


def _validate_artifact_path(path: PurePosixPath) -> None:
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or any("\\" in part for part in path.parts)
    ):
        raise AppError("journal artifact path must be confined")


@dataclass(frozen=True)
class TransactionArtifact:
    logical_client: str
    component: Component
    adapter_id: str
    grant_id: str
    artifact_path: PurePosixPath
    destination: Path
    before_version: FileVersion
    after_sha256: str | None

    def __post_init__(self) -> None:
        if (
            not _LOGICAL_ID.fullmatch(self.logical_client)
            or not _STABLE_ID.fullmatch(self.adapter_id)
            or not _STABLE_ID.fullmatch(self.grant_id)
        ):
            raise AppError("journal artifact identity is invalid")
        _validate_artifact_path(self.artifact_path)
        if not self.destination.is_absolute():
            raise AppError("journal artifact destination must be absolute")
        _validate_version(self.before_version, "artifact prestate")
        if self.after_sha256 is not None:
            _digest(self.after_sha256, "artifact after digest")


@dataclass(frozen=True)
class InvocationJournal:
    invocation_id: str
    manifest_path: Path
    manifest_before_bytes: bytes | None
    manifest_before_version: FileVersion
    manifest_after_bytes: bytes
    manifest_after_sha256: str
    artifacts: tuple[TransactionArtifact, ...]
    backup_set: str
    backup_integrity: str

    def __post_init__(self) -> None:
        _safe_component(self.invocation_id, "invocation id")
        if not self.manifest_path.is_absolute():
            raise AppError("journal manifest path must be absolute")
        _validate_version(self.manifest_before_version, "manifest prestate")
        if self.manifest_before_bytes is None:
            if self.manifest_before_version.exists:
                raise AppError("journal absent manifest bytes do not match manifest version")
        elif (
            not self.manifest_before_version.exists
            or len(self.manifest_before_bytes) != self.manifest_before_version.size
            or hashlib.sha256(self.manifest_before_bytes).hexdigest()
            != self.manifest_before_version.sha256
        ):
            raise AppError("journal manifest before bytes do not match manifest version")
        _digest(self.manifest_after_sha256, "manifest after digest")
        if hashlib.sha256(self.manifest_after_bytes).hexdigest() != self.manifest_after_sha256:
            raise AppError("journal after manifest bytes do not match digest")
        after = parse_ownership_manifest_bytes(
            self.manifest_after_bytes, source="pending invocation journal"
        )
        before = (
            OwnershipManifest()
            if self.manifest_before_bytes is None
            else parse_ownership_manifest_bytes(
                self.manifest_before_bytes, source="pending invocation journal prestate"
            )
        )
        destinations = [str(item.destination) for item in self.artifacts]
        if len(set(destinations)) != len(destinations):
            raise AppError("journal artifact destinations must be unique")
        _validate_manifest_intent(before, after, self.artifacts)
        if self.artifacts:
            _safe_component(self.backup_set, "backup set")
            _digest(self.backup_integrity, "backup integrity")
        elif self.backup_set != "none" or self.backup_integrity != "none":
            raise AppError("journal without artifacts requires backup none")


def _records(manifest: OwnershipManifest) -> dict[tuple[str, str], Any]:
    return {
        (client, _component_name(record.component)): record
        for client, value in manifest.clients.items()
        for record in value.components
    }


def _record_bytes(record: ComponentOwnership | None) -> bytes | None:
    return None if record is None else _canonical(_ownership_record_payload(record))


def _validate_manifest_intent(
    before: OwnershipManifest, after: OwnershipManifest, artifacts: tuple[TransactionArtifact, ...]
) -> None:
    expected: dict[tuple[str, str], list[TransactionArtifact]] = {}
    for item in artifacts:
        expected.setdefault((item.logical_client, _component_name(item.component)), []).append(item)
    old, new = _records(before), _records(after)
    for key in set(old) | set(new):
        if key not in expected and _record_bytes(old.get(key)) != _record_bytes(new.get(key)):
            raise AppError("journal untouched manifest intent changed without an artifact")
    for key, planned in expected.items():
        record = new.get(key)
        present = [item for item in planned if item.after_sha256 is not None]
        if not present:
            if record is not None:
                raise AppError("deleted journal artifact remains in after manifest intent")
            continue
        if record is None:
            raise AppError("journal artifact identity does not match after manifest intent")
        if any(item.adapter_id != record.adapter.adapter_id for item in present):
            raise AppError("journal artifact adapter does not match after manifest intent")
        owned = {(item.grant_id, item.artifact_path): item for item in record.artifacts}
        if set(owned) != {(item.grant_id, item.artifact_path) for item in present}:
            raise AppError("journal artifact bundle does not match after manifest intent")
        for item in present:
            artifact = owned[(item.grant_id, item.artifact_path)]
            if (
                item.before_version.sha256 != artifact.preimage_sha256
                or item.after_sha256 != artifact.written_sha256
            ):
                raise AppError("journal artifact identity does not match after manifest intent")


def _version_payload(value: FileVersion) -> dict[str, Any]:
    return {
        "parentIdentity": list(value.parent_identity),
        "leafIdentity": None if value.leaf_identity is None else list(value.leaf_identity),
        "size": value.size,
        "sha256": value.sha256,
    }


def journal_payload(journal: InvocationJournal) -> dict[str, Any]:
    return {
        "invocationId": journal.invocation_id,
        "manifestPath": str(journal.manifest_path),
        "manifestBeforeBytes": None
        if journal.manifest_before_bytes is None
        else base64.b64encode(journal.manifest_before_bytes).decode(),
        "manifestBeforeVersion": _version_payload(journal.manifest_before_version),
        "manifestAfterBytes": base64.b64encode(journal.manifest_after_bytes).decode(),
        "manifestAfterSha256": journal.manifest_after_sha256,
        "artifacts": [
            {
                "logicalClient": item.logical_client,
                "component": _component_payload(item.component),
                "adapterId": item.adapter_id,
                "grantId": item.grant_id,
                "artifactPath": str(item.artifact_path),
                "destination": str(item.destination),
                "beforeVersion": _version_payload(item.before_version),
                "afterSha256": item.after_sha256,
            }
            for item in journal.artifacts
        ],
        "backupSet": journal.backup_set,
        "backupIntegrity": journal.backup_integrity,
    }


def journal_bytes(journal: InvocationJournal) -> bytes:
    payload = journal_payload(journal)
    return (
        _canonical(
            {
                "journalVersion": _VERSION,
                "payload": payload,
                "integrity": hashlib.sha256(_canonical(payload)).hexdigest(),
            }
        )
        + b"\n"
    )


def save_journal(
    path: Path, journal: InvocationJournal, expected: FileVersion, lock: OperationLock
) -> FileVersion:
    return conditional_write_bytes(
        path, journal_bytes(journal), expected, "pending journal", writer_exclusion=lock
    )


def _reject_constant(value: str) -> None:
    del value
    raise ValueError("non-standard JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise AppError("invalid pending journal identity")
    return value


def _version(raw: Any) -> FileVersion:
    if not isinstance(raw, dict) or set(raw) != {
        "parentIdentity",
        "leafIdentity",
        "size",
        "sha256",
    }:
        raise AppError("invalid pending journal file version")

    def identity(value: Any, label: str) -> tuple[int, int]:
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in value)
        ):
            raise AppError(f"invalid pending journal {label}")
        return value[0], value[1]

    leaf = None if raw["leafIdentity"] is None else identity(raw["leafIdentity"], "leaf identity")
    if raw["size"] is not None and (
        not isinstance(raw["size"], int) or isinstance(raw["size"], bool) or raw["size"] < 0
    ):
        raise AppError("invalid pending journal file size")
    return FileVersion(
        identity(raw["parentIdentity"], "parent identity"), leaf, raw["size"], raw["sha256"]
    )


def _decode(raw: Any, label: str, nullable: bool = False) -> bytes | None:
    if nullable and raw is None:
        return None
    if not isinstance(raw, str):
        raise AppError(f"invalid pending journal {label}")
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise AppError(f"invalid pending journal {label}") from exc


def _parse_payload(raw: Any) -> InvocationJournal:
    required = {
        "invocationId",
        "manifestPath",
        "manifestBeforeBytes",
        "manifestBeforeVersion",
        "manifestAfterBytes",
        "manifestAfterSha256",
        "artifacts",
        "backupSet",
        "backupIntegrity",
    }
    if not isinstance(raw, dict) or set(raw) != required or not isinstance(raw["artifacts"], list):
        raise AppError("invalid pending journal payload")
    artifacts = []
    for item in raw["artifacts"]:
        required_artifact = {
            "logicalClient",
            "component",
            "adapterId",
            "grantId",
            "artifactPath",
            "destination",
            "beforeVersion",
            "afterSha256",
        }
        if not isinstance(item, dict) or set(item) != required_artifact:
            raise AppError("invalid pending journal artifact")
        artifacts.append(
            TransactionArtifact(
                _string(item, "logicalClient"),
                _parse_component(item["component"]),
                _string(item, "adapterId"),
                _string(item, "grantId"),
                PurePosixPath(_string(item, "artifactPath")),
                Path(_string(item, "destination")),
                _version(item["beforeVersion"]),
                item["afterSha256"]
                if item["afterSha256"] is None or isinstance(item["afterSha256"], str)
                else (_ for _ in ()).throw(AppError("invalid pending journal optional digest")),
            )
        )
    before = _decode(raw["manifestBeforeBytes"], "manifest before bytes", True)
    after = _decode(raw["manifestAfterBytes"], "manifest after bytes")
    assert after is not None
    return InvocationJournal(
        _string(raw, "invocationId"),
        Path(_string(raw, "manifestPath")),
        before,
        _version(raw["manifestBeforeVersion"]),
        after,
        _string(raw, "manifestAfterSha256"),
        tuple(artifacts),
        _string(raw, "backupSet"),
        _string(raw, "backupIntegrity"),
    )


def validate_journal_bytes(content: bytes) -> InvocationJournal:
    try:
        raw = json.loads(content, parse_constant=_reject_constant, parse_float=_finite_float)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AppError(f"cannot decode pending journal: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or set(raw) != {"journalVersion", "payload", "integrity"}
        or raw.get("journalVersion") != _VERSION
    ):
        raise AppError("pending journal version is unsupported or envelope is invalid")
    payload = raw["payload"]
    journal = _parse_payload(payload)
    if (
        not isinstance(raw["integrity"], str)
        or hashlib.sha256(_canonical(payload)).hexdigest() != raw["integrity"]
    ):
        raise AppError("pending journal structural digest is corrupt")
    return journal


def load_journal(path: Path) -> InvocationJournal:
    return validate_journal_bytes(read_private_bytes(path, "pending journal"))
