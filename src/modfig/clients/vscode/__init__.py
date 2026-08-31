"""Stable Code adapter with a recorded, read-only runtime proof boundary.

The adapter reads only sanitized installed-runtime facts during proof capture,
keeps Code's own secret backend separate from transaction storage, and requires
stable Code quiescence before every mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import plistlib
import re
import stat
import subprocess
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from ...adapters import (
    AbsentDestination,
    AdapterContext,
    AdapterMetadata,
    AdapterOwnership,
    AdapterPlanContext,
    AdapterPlanError,
    AdapterValidationContext,
    ArtifactIdentity,
    ArtifactPlan,
    ArtifactSnapshot,
    PlannedArtifact,
    PreflightDeclaration,
    ProspectiveWrite,
    ResolvedModel,
    RuntimeProof,
    SnapshotRequest,
)
from ...errors import AppError
from ...platform import require_secure_io
from ...registry import Registry
from ...secrets import resolve_secret, secret_variable
from ...state import CollisionError, reconcile
from ...storage import atomic_write_json, read_private_bytes
from .db import (
    DatabasePaths,
    owned_row_ids,
    plan_secret_rows_bundle,
    read_owned_row_values,
)
from .secrets import (
    LinuxSecretServiceBackend,
    MacOSKeychainBackend,
    SecretContract,
    SecretKeyBackend,
    _key_bytes,
    decode_secret,
    encode_secret,
)

STATE_DB_MEMBERS: tuple[str, ...] = ("state.vscdb", "state.vscdb-wal", "state.vscdb-shm")
VSCODE_PROVIDER_PREFIX = "ModFig/"
VSCODE_SECRET_ROW_PREFIX = "secret://chat.lm.secret.lm-"
VSCODE_SECRET_INPUT_PREFIX = "${input:chat.lm.secret.lm-"
_VSCODE_SECRET_COMPONENT_RE = re.compile(r"[^a-z0-9_-]+")
_VSCODE_ARTIFACT = ArtifactIdentity("vscode-config", PurePosixPath("chatLanguageModels.json"))
_VSCODE_STATE_ARTIFACTS = tuple(
    ArtifactIdentity("vscode-state", PurePosixPath(member)) for member in STATE_DB_MEMBERS
)
_VSCODE_METADATA = AdapterMetadata("modfig.vscode", "vscode", "core")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class VSCodeAdapter:
    metadata: AdapterMetadata = _VSCODE_METADATA

    def describe(self) -> AdapterMetadata:
        return self.metadata

    def validate(self, config: Mapping[str, object], context: AdapterValidationContext) -> None:
        _validate_adapter_binding(context.logical_client, context.component)
        if config and not isinstance(config, Mapping):
            raise AdapterPlanError("VS Code core config must be a mapping")

    def preflight(self, context: AdapterContext) -> PreflightDeclaration:
        _validate_adapter_binding(context.logical_client, context.component)
        artifacts = (_VSCODE_ARTIFACT, *_VSCODE_STATE_ARTIFACTS)
        return PreflightDeclaration(
            {
                "adapterId": self.metadata.adapter_id,
                "logicalClient": self.metadata.logical_client,
                "component": "core",
                "runtimeProof": "stable-code-quiescence-and-secret-contract",
            },
            tuple(SnapshotRequest(item) for item in artifacts),
            tuple(ProspectiveWrite(item) for item in artifacts),
        )

    def plan(
        self,
        context: AdapterPlanContext,
        proof: RuntimeProof,
        snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
        ownership: AdapterOwnership,
    ) -> ArtifactPlan:
        _validate_adapter_binding(context.logical_client, context.component)
        runtime = _proof_runtime(proof)
        preflight(runtime)
        settings_source = snapshots.get(_VSCODE_ARTIFACT)
        if not isinstance(settings_source, bytes):
            raise AdapterPlanError("VS Code settings snapshot is absent")
        plan = _plan_settings_from_context(context, settings_source, ownership, runtime)
        database_plan: dict[ArtifactIdentity, ArtifactSnapshot] = {
            identity: snapshots[identity] for identity in _VSCODE_STATE_ARTIFACTS
        }
        prior_secret_row_ids = _owned_secret_row_ids(ownership)
        secret_ownership: dict[str, object] = {}
        if runtime.secret_backend is not None and not runtime.secret_values and context.models:
            if runtime.secret_format == "basic-text":
                raise AdapterPlanError(
                    "VS Code basic-text secret rows are not supported for transactional apply"
                )
            runtime = replace(
                runtime,
                secret_values=_resolve_vscode_secret_values(
                    model.api_key_reference for model in context.models
                ),
            )
        if runtime.secret_backend is not None and (context.models or prior_secret_row_ids):
            primary = database_plan[_VSCODE_STATE_ARTIFACTS[0]]
            if not isinstance(primary, bytes):
                raise AdapterPlanError("VS Code state database snapshot is missing or absent")
            current_code_rows = isinstance(_parse_vscode_settings(settings_source), list)
            secret_components = (
                _vscode_secret_components(model.provider_key for model in context.models)
                if current_code_rows
                else {}
            )
            rows = (
                plan_secret_rows_from_models(
                    context.models,
                    runtime,
                    runtime.secret_values,
                    row_prefix=(
                        VSCODE_SECRET_ROW_PREFIX if current_code_rows else "modfig:ModFig/"
                    ),
                )
                if context.models
                else {}
            )
            row_mutations: dict[str, bytes | str | None] = dict(rows)
            row_mutations.update(
                {row_id: None for row_id in prior_secret_row_ids if row_id not in rows}
            )
            paths = _database_paths(runtime)
            bundle_snapshot: dict[Path, bytes | None] = {}
            for path, identity in zip(paths.members(), _VSCODE_STATE_ARTIFACTS, strict=True):
                snapshot = database_plan[identity]
                bundle_snapshot[path] = (
                    None if isinstance(snapshot, AbsentDestination) else snapshot
                )
            bundle = plan_secret_rows_bundle(paths, row_mutations, snapshot=bundle_snapshot)
            for identity, planned in zip(
                _VSCODE_STATE_ARTIFACTS,
                (bundle[paths.database], bundle[paths.wal], bundle[paths.shm]),
                strict=True,
            ):
                database_plan[identity] = AbsentDestination() if planned is None else planned
            planned_primary = database_plan[_VSCODE_STATE_ARTIFACTS[0]]
            assert isinstance(planned_primary, bytes)
            fingerprints = read_owned_row_values(planned_primary, tuple(sorted(rows)))
            secret_variables = {
                (
                    (VSCODE_SECRET_ROW_PREFIX if current_code_rows else "modfig:ModFig/")
                    + (
                        secret_components[model.provider_key]
                        if current_code_rows
                        else model.provider_key
                    )
                ): secret_variable(model.api_key_reference)
                for model in context.models
            }
            secret_variables = dict(secret_variables)
            secret_variables = {
                row_id: variable for row_id, variable in secret_variables.items() if row_id in rows
            }
            secret_ownership = {
                "secretRowIds": tuple(sorted(rows)),
                "secretRowFingerprints": {
                    row_id: hashlib.sha256(fingerprints[row_id]).hexdigest()
                    for row_id in sorted(rows)
                },
                "secretRowVariables": secret_variables,
            }
        artifacts = [
            PlannedArtifact(
                _VSCODE_ARTIFACT,
                _serialize_vscode_settings(plan.settings),
                "features.core.models",
                {
                    "providerIds": sorted(plan.owned_provider_ids),
                    "modelIds": {key: sorted(value) for key, value in plan.owned_model_ids.items()},
                },
            )
        ]
        for identity in _VSCODE_STATE_ARTIFACTS:
            source = database_plan[identity]
            if not isinstance(source, (bytes, AbsentDestination)):
                raise AdapterPlanError("VS Code state database snapshot is missing")
            if identity == _VSCODE_STATE_ARTIFACTS[0] and isinstance(source, AbsentDestination):
                raise AdapterPlanError("VS Code state database snapshot is missing or absent")
            artifacts.append(PlannedArtifact(identity, source, "features.core.secret-rows", {}))
        return ArtifactPlan(
            tuple(artifacts),
            {
                "providerIds": sorted(plan.owned_provider_ids),
                "modelIds": {key: sorted(value) for key, value in plan.owned_model_ids.items()},
                **secret_ownership,
            },
        )

    def recheck(self, proof: RuntimeProof) -> None:
        runtime = _proof_runtime(proof)
        if runtime.runtime_recheck is None:
            raise AdapterPlanError("VS Code runtime recheck is unavailable")
        try:
            if runtime.runtime_recheck() is not True:
                raise AdapterPlanError("VS Code is running or runtime identity changed")
        except AdapterPlanError:
            raise
        except Exception as exc:
            raise AdapterPlanError("VS Code runtime recheck failed") from exc

    def verify(
        self,
        context: AdapterContext,
        proof: RuntimeProof,
        written: Sequence[ArtifactSnapshot],
        ownership: Mapping[str, object] | None = None,
    ) -> None:
        _validate_adapter_binding(context.logical_client, context.component)
        runtime = _proof_runtime(proof)
        preflight(runtime)
        if len(written) != 4 or not isinstance(written[0], bytes):
            raise AdapterPlanError("VS Code verification requires settings and database members")
        try:
            _parse_vscode_settings(written[0])
        except AppError as exc:
            raise AdapterPlanError(exc.message) from exc
        if any(not isinstance(item, (bytes, AbsentDestination)) for item in written[1:]):
            raise AdapterPlanError("VS Code database member verification failed")
        variables = None if ownership is None else ownership.get("secretRowVariables")
        if runtime.secret_backend is not None and not runtime.secret_values and variables:
            if not isinstance(variables, Mapping) or not all(
                isinstance(variable, str) for variable in variables.values()
            ):
                raise AdapterPlanError("VS Code secret row variables are invalid")
            if runtime.secret_format == "basic-text":
                raise AdapterPlanError(
                    "VS Code basic-text secret rows are not supported for transactional apply"
                )
            runtime = replace(
                runtime,
                secret_values=_resolve_vscode_secret_values(
                    f"env.{variable}" for variable in variables.values()
                ),
            )
        if runtime.secret_values and runtime.secret_backend is not None:
            primary = written[1]
            if not isinstance(primary, bytes):
                raise AdapterPlanError("VS Code state database verification failed")
            _validate_owned_secret_rows(primary, runtime, ownership)


def _validate_adapter_binding(logical_client: str, component: object) -> None:
    if logical_client != "vscode" or component != "core":
        raise AdapterPlanError("VS Code adapter binding must be vscode/core")


def _database_paths(runtime: VSCodeRuntime) -> DatabasePaths:
    return DatabasePaths(runtime.state_db_path, runtime.state_wal_path, runtime.state_shm_path)


def _owned_secret_row_ids(ownership: AdapterOwnership) -> tuple[str, ...]:
    row_ids = ownership.get("secretRowIds", ())
    if not isinstance(row_ids, (list, tuple)) or not all(
        isinstance(row_id, str)
        and (row_id.startswith(VSCODE_SECRET_ROW_PREFIX) or row_id.startswith("modfig:ModFig/"))
        for row_id in row_ids
    ):
        raise AdapterPlanError("VS Code secret row ownership is invalid")
    return tuple(row_ids)


def _resolve_vscode_secret_values(references: Iterable[str]) -> Mapping[str, bytes]:
    """Resolve only the selected model references for encrypted secret-row planning."""
    values: dict[str, bytes] = {}
    for reference in references:
        variable = secret_variable(reference)
        try:
            values[variable] = resolve_secret(reference).encode()
        except AppError as exc:
            raise AppError(f"VS Code secret for {variable!r} is not resolved") from exc
    return values


def _validate_owned_secret_rows(
    source: bytes,
    runtime: VSCodeRuntime,
    ownership: Mapping[str, object] | None,
) -> None:
    if runtime.secret_backend is None:
        raise AdapterPlanError("VS Code secret backend is unavailable")
    owned_ids = () if ownership is None else ownership.get("secretRowIds", ())
    if not isinstance(owned_ids, (list, tuple)):
        raise AdapterPlanError("VS Code secret row ownership is invalid")
    if not owned_ids:
        owned_ids = owned_row_ids(source)
    if not all(
        isinstance(row_id, str)
        and (row_id.startswith(VSCODE_SECRET_ROW_PREFIX) or row_id.startswith("modfig:ModFig/"))
        for row_id in owned_ids
    ):
        raise AdapterPlanError("VS Code secret row ownership is invalid")
    rows = read_owned_row_values(source, tuple(owned_ids))
    if set(rows) != set(owned_ids):
        raise AdapterPlanError("VS Code owned secret row is missing")
    fingerprints = {} if ownership is None else ownership.get("secretRowFingerprints", {})
    if not isinstance(fingerprints, Mapping):
        raise AdapterPlanError("VS Code secret row fingerprints are invalid")
    for row_id, value in rows.items():
        expected_fingerprint = fingerprints.get(row_id)
        if expected_fingerprint is not None and (
            not isinstance(expected_fingerprint, str)
            or hashlib.sha256(value).hexdigest() != expected_fingerprint
        ):
            raise AdapterPlanError("VS Code owned secret row fingerprint changed")
    contract = SecretContract(runtime.os_name, runtime.channel, runtime.secret_format)
    decoded = {
        row_id: decode_secret(value, contract, runtime.secret_backend)
        for row_id, value in rows.items()
    }
    variables = None if ownership is None else ownership.get("secretRowVariables")
    if variables is not None:
        if not isinstance(variables, Mapping) or set(variables) != set(owned_ids):
            raise AdapterPlanError("VS Code secret row variables are invalid")
        expected: dict[str, bytes] = {}
        for row_id, variable in variables.items():
            if not isinstance(variable, str) or not isinstance(
                runtime.secret_values.get(variable), bytes
            ):
                raise AdapterPlanError("VS Code secret row runtime value is unavailable")
            expected[row_id] = runtime.secret_values[variable]
        if decoded != expected:
            raise AdapterPlanError(
                "VS Code owned secret row plaintext does not match runtime secrets"
            )
        return
    expected_values = list(runtime.secret_values.values())
    if len(decoded) != len(expected_values) or sorted(decoded.values()) != sorted(expected_values):
        raise AdapterPlanError("VS Code owned secret row plaintext does not match runtime secrets")


def _proof_runtime(proof: RuntimeProof) -> VSCodeRuntime:
    runtime = proof.provenance
    if not isinstance(runtime, VSCodeRuntime):
        raise AdapterPlanError("VS Code runtime proof is unavailable")
    return runtime


def _plan_settings_from_context(
    context: AdapterPlanContext,
    source: bytes,
    ownership: AdapterOwnership,
    runtime: VSCodeRuntime,
) -> VSCodePlan:
    existing = _parse_vscode_settings(source)
    provider_ids = ownership.get("providerIds", ())
    model_ids = ownership.get("modelIds", {})
    if not isinstance(provider_ids, (list, tuple, set, frozenset)):
        provider_ids = ()
    if not isinstance(model_ids, Mapping):
        model_ids = {}
    return plan_vscode_models(
        context.models,
        existing,
        owned_provider_ids={item for item in provider_ids if isinstance(item, str)},
        owned_model_ids={
            str(key): set(value) if isinstance(value, (list, tuple, set, frozenset)) else set()
            for key, value in model_ids.items()
        },
        runtime=runtime,
    )


adapter = VSCodeAdapter()


class _StrictJsonError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    del value
    raise _StrictJsonError("non-standard JSON number")


@dataclass(frozen=True)
class VSCodeRuntimeFacts:
    """Sanitized installed-Code identity facts used for proof matching."""

    platform: str
    channel: str
    version: str
    build: str
    bundle_identity: str
    executable_sha256: str
    contract_identity: str
    secret_format: str


@dataclass(frozen=True)
class VSCodeRuntime:
    """Compatibility facts recorded by a sanitized VS Code proof probe."""

    supported_os: tuple[str, ...]
    supported_channels: tuple[str, ...]
    supported_profile_modes: tuple[str, ...]
    user_data_root: Path
    settings_path: Path
    state_db_path: Path
    state_wal_path: Path
    state_shm_path: Path
    safe_storage_supported: bool
    key_context: str
    process_quiescent: bool
    vendor_api_type_mapping: bool
    vendor_api_type_map: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    runtime_probe: Callable[[], VSCodeRuntime] | None = None
    runtime_recheck: Callable[[], bool] | None = None
    os_name: str = ""
    channel: str = ""
    profile_mode: str = ""
    secret_format: str = ""
    version: str = ""
    build: str = ""
    bundle_identity: str = ""
    executable_sha256: str = ""
    contract_identity: str = ""
    secret_backend: SecretKeyBackend | Callable[[], bytes] | None = field(
        default=None, repr=False, compare=False
    )
    secret_values: Mapping[str, bytes] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class VSCodePlan:
    settings: dict[str, Any] | list[dict[str, Any]]
    owned_provider_ids: frozenset[str]
    owned_model_ids: dict[str, frozenset[str]]


def _runtime_identity(runtime: VSCodeRuntime) -> tuple[object, ...]:
    return (
        runtime.supported_os,
        runtime.supported_channels,
        runtime.supported_profile_modes,
        runtime.user_data_root,
        runtime.settings_path,
        runtime.state_db_path,
        runtime.state_wal_path,
        runtime.state_shm_path,
        runtime.safe_storage_supported,
        runtime.key_context,
        runtime.process_quiescent,
        runtime.vendor_api_type_mapping,
        dict(runtime.vendor_api_type_map),
        runtime.os_name,
        runtime.channel,
        runtime.profile_mode,
        runtime.secret_format,
        runtime.version,
        runtime.build,
        runtime.bundle_identity,
        runtime.executable_sha256,
        runtime.contract_identity,
    )


@dataclass(frozen=True)
class _DeferredVSCodeSecretBackend:
    """Resolve Code's native secret backend only when row crypto is needed."""

    os_name: str

    def key_bytes(self) -> bytes:
        return _secret_backend(self.os_name).key_bytes()


def _secret_backend(os_name: str) -> SecretKeyBackend:
    if os_name == "macos":
        return MacOSKeychainBackend()
    if os_name == "linux":
        return LinuxSecretServiceBackend({"application": "Code Safe Storage"})
    raise AppError("VS Code secret contract platform is unsupported")


def _verify_secret_contract(
    os_name: str,
    secret_format: str,
    backend: SecretKeyBackend | Callable[[], bytes] | None = None,
) -> None:
    SecretContract(os_name, "stable", secret_format)
    key = _key_bytes(backend or _secret_backend(os_name))
    try:
        if not isinstance(key, bytes) or not key:
            raise AppError("VS Code safe-storage key is invalid")
    finally:
        del key


def contract_identity(os_name: str, secret_format: str) -> str:
    """Return the stable, non-secret identity of Code's secret-row contract."""
    SecretContract(os_name, "stable", secret_format)
    return f"vscode-stable-{os_name}-{secret_format}-v1"


def _proof_error() -> AppError:
    return AppError("VS Code runtime proof is unavailable")


def _strict_json(path: Path) -> Mapping[str, object]:
    try:
        source = read_private_bytes(path, "VS Code runtime proof")
        value = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, _StrictJsonError):
        raise _proof_error() from None
    if not isinstance(value, Mapping):
        raise _proof_error()
    return value


def _strict_record(
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object]]:
    if (
        set(record) != {"proofVersion", "binding", "contract", "capture"}
        or type(record.get("proofVersion")) is not int
        or record.get("proofVersion") != 1
    ):
        raise _proof_error()
    binding = record.get("binding")
    contract = record.get("contract")
    capture = record.get("capture")
    if not all(isinstance(value, Mapping) for value in (binding, contract, capture)):
        raise _proof_error()
    assert (
        isinstance(binding, Mapping)
        and isinstance(contract, Mapping)
        and isinstance(capture, Mapping)
    )
    if set(binding) != {
        "platform",
        "channel",
        "profileMode",
        "version",
        "build",
        "bundleIdentity",
        "executableSha256",
    }:
        raise _proof_error()
    if set(contract) != {"identity", "safeStorage", "keyContext", "secretFormat"}:
        raise _proof_error()
    if set(capture) != {"provenance", "capturedAt", "freshUntil"}:
        raise _proof_error()
    if binding.get("platform") not in {"macos", "linux"} or binding.get("channel") != "stable":
        raise _proof_error()
    if binding.get("profileMode") != "default":
        raise _proof_error()
    if binding.get("bundleIdentity") != "com.microsoft.VSCode":
        raise _proof_error()
    for value in binding.values():
        if not isinstance(value, str) or not value:
            raise _proof_error()
    if (
        not _VERSION_RE.fullmatch(str(binding["version"]))
        or not _VERSION_RE.fullmatch(str(binding["build"]))
        or not _SHA256_RE.fullmatch(str(binding["executableSha256"]))
    ):
        raise _proof_error()
    if contract.get("safeStorage") != "proven" or contract.get("keyContext") != "proven":
        raise _proof_error()
    secret_format = contract.get("secretFormat")
    if not isinstance(secret_format, str):
        raise _proof_error()
    try:
        expected_identity = contract_identity(str(binding["platform"]), secret_format)
    except AppError:
        raise _proof_error() from None
    if contract.get("identity") != expected_identity:
        raise _proof_error()
    if capture.get("provenance") != "read-only-installed-stable-code":
        raise _proof_error()
    captured_at = capture.get("capturedAt")
    fresh_until = capture.get("freshUntil")
    if (
        not isinstance(captured_at, str)
        or not isinstance(fresh_until, str)
        or not _RFC3339_RE.fullmatch(captured_at)
        or not _RFC3339_RE.fullmatch(fresh_until)
    ):
        raise _proof_error()
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        fresh = datetime.fromisoformat(fresh_until.replace("Z", "+00:00"))
    except ValueError:
        raise _proof_error() from None
    if captured.utcoffset() is None or fresh.utcoffset() is None or not captured < fresh:
        raise _proof_error()
    return binding, contract, capture


def _facts_from_binding(
    binding: Mapping[str, object], contract: Mapping[str, object]
) -> VSCodeRuntimeFacts:
    return VSCodeRuntimeFacts(
        str(binding["platform"]),
        str(binding["channel"]),
        str(binding["version"]),
        str(binding["build"]),
        str(binding["bundleIdentity"]),
        str(binding["executableSha256"]),
        str(contract["identity"]),
        str(contract["secretFormat"]),
    )


def _facts_equal(
    expected: VSCodeRuntimeFacts, actual: VSCodeRuntimeFacts | Mapping[str, object]
) -> bool:
    if isinstance(actual, Mapping):
        try:
            actual = VSCodeRuntimeFacts(
                str(actual["platform"]),
                str(actual["channel"]),
                str(actual["version"]),
                str(actual["build"]),
                str(actual["bundle_identity"]),
                str(actual["executable_sha256"]),
                str(actual["contract_identity"]),
                str(actual["secret_format"]),
            )
        except (KeyError, TypeError):
            return False
    return expected == actual


def _read_installed_file(root: Path, relative: PurePosixPath) -> bytes:
    require_secure_io()
    if not root.is_absolute() or root.is_symlink():
        raise _proof_error()
    descriptors: list[int] = []
    try:
        directory = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(directory)
        for component in root.parts[1:]:
            directory = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            descriptors.append(directory)
        parts = relative.parts
        for component in parts[:-1]:
            directory = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            descriptors.append(directory)
        file_descriptor = os.open(
            parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory
        )
        descriptors.append(file_descriptor)
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _proof_error()
        with os.fdopen(file_descriptor, "rb") as handle:
            descriptors.pop()
            content = handle.read()
        after = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(content) != before.st_size:
            raise _proof_error()
        return content
    except (OSError, IndexError):
        raise _proof_error() from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _installed_runtime_facts(
    *,
    os_name: str,
    installation_root: Path | None = None,
) -> VSCodeRuntimeFacts:
    root = installation_root or _default_installation_root(os_name)
    executable_relative = (
        PurePosixPath("Contents/MacOS/Code") if os_name == "macos" else PurePosixPath("code")
    )
    product_relative = (
        PurePosixPath("Contents/Resources/app/product.json")
        if os_name == "macos"
        else PurePosixPath("resources/app/product.json")
    )
    product = _read_installed_file(root, product_relative)
    executable = _read_installed_file(root, executable_relative)
    bundle_identity = "com.microsoft.VSCode"
    if os_name == "macos":
        try:
            info_bytes = _read_installed_file(root, PurePosixPath("Contents/Info.plist"))
            info = plistlib.loads(info_bytes)
        except (OSError, plistlib.InvalidFileException, ValueError):
            raise _proof_error() from None
        if not isinstance(info, Mapping) or info.get("CFBundleIdentifier") != bundle_identity:
            raise _proof_error()
    try:
        metadata = json.loads(product.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise _proof_error() from None
    if not isinstance(metadata, Mapping):
        raise _proof_error()
    version = metadata.get("version")
    build = metadata.get("commit") or metadata.get("build")
    if (
        not isinstance(version, str)
        or not isinstance(build, str)
        or not _VERSION_RE.fullmatch(version)
        or not _VERSION_RE.fullmatch(build)
    ):
        raise _proof_error()
    secret_format = "oscrypt-v10" if os_name == "macos" else "oscrypt-v11"
    return VSCodeRuntimeFacts(
        os_name,
        "stable",
        version,
        build,
        bundle_identity,
        "sha256:" + hashlib.sha256(executable).hexdigest(),
        contract_identity(os_name, secret_format),
        secret_format,
    )


def _record_runtime_contract(
    record: Mapping[str, object],
    *,
    home: Path,
    facts: VSCodeRuntimeFacts,
    user_data_root: Path | None = None,
) -> Mapping[str, object]:
    root = (
        user_data_root.absolute()
        if user_data_root is not None
        else discover_vscode_user_data_root(home, facts.platform)
    )
    return {
        "supportedOs": [facts.platform],
        "supportedChannels": ["stable"],
        "supportedProfileModes": ["default"],
        "userDataRoot": str(root),
        "settingsPath": str(root / "chatLanguageModels.json"),
        "stateDbPath": str(root / "globalStorage" / "state.vscdb"),
        "stateWalPath": str(root / "globalStorage" / "state.vscdb-wal"),
        "stateShmPath": str(root / "globalStorage" / "state.vscdb-shm"),
        "stateDatabaseMembers": list(STATE_DB_MEMBERS),
        "itemTable": {
            "access": "proof-recorded-rows-only",
            "unknownRows": "preserve-without-inspection",
        },
        "safeStorage": "proven",
        "keyContext": "proven",
        "processDetector": "proven",
        "processQuiescent": True,
        "vendorApiTypeMapping": False,
        "secretFormat": facts.secret_format,
    }


def load_vscode_runtime_proof(
    path: Path,
    *,
    home: Path | None = None,
    user_data_root: Path | None = None,
    os_name: str | None = None,
    process_probe: Callable[[], bool] | None = None,
    now: datetime | None = None,
    observe: Callable[[], VSCodeRuntimeFacts] | None = None,
) -> RuntimeProof:
    """Load a strict local record and match it to fresh read-only observations."""
    try:
        raw = _strict_json(path)
        binding, contract, capture = _strict_record(raw)
        current_time = datetime.now(UTC) if now is None else now
        if current_time.utcoffset() is None:
            raise _proof_error()
        captured_at = datetime.fromisoformat(str(capture["capturedAt"]).replace("Z", "+00:00"))
        fresh_until = datetime.fromisoformat(str(capture["freshUntil"]).replace("Z", "+00:00"))
        if (
            captured_at.utcoffset() is None
            or fresh_until.utcoffset() is None
            or current_time < captured_at
            or current_time >= fresh_until
        ):
            raise _proof_error()
        expected = _facts_from_binding(binding, contract)
        system = (platform.system() if os_name is None else os_name).lower()
        normalized_os = "macos" if system in {"darwin", "macos"} else system
        if normalized_os != expected.platform:
            raise _proof_error()
        probe = process_probe or (lambda: _default_process_probe(normalized_os))
        if probe() is not True:
            raise _proof_error()
        actual = (
            observe()
            if observe is not None
            else _installed_runtime_facts(os_name=expected.platform)
        )
        if not _facts_equal(expected, actual):
            raise _proof_error()
        runtime_record = _record_runtime_contract(
            raw,
            home=Path.home() if home is None else home,
            facts=expected,
            user_data_root=user_data_root,
        )
        runtime = acquire_vscode_runtime(
            runtime_record,
            os_name=expected.platform,
            channel=expected.channel,
            profile_mode="default",
            secret_backend=_DeferredVSCodeSecretBackend(expected.platform),
            process_probe=lambda: probe() is True,
        )
        actual = (
            observe()
            if observe is not None
            else _installed_runtime_facts(os_name=expected.platform)
        )
        if not _facts_equal(expected, actual):
            raise _proof_error()

        def recheck() -> bool:
            if datetime.now(UTC) >= fresh_until:
                return False
            if probe() is not True:
                return False
            current = (
                observe()
                if observe is not None
                else _installed_runtime_facts(os_name=expected.platform)
            )
            return _facts_equal(expected, current)

        runtime = replace(
            runtime,
            version=expected.version,
            build=expected.build,
            bundle_identity=expected.bundle_identity,
            executable_sha256=expected.executable_sha256,
            contract_identity=expected.contract_identity,
            runtime_probe=None,
            runtime_recheck=recheck,
        )
        return RuntimeProof(
            {
                "channel": expected.channel,
                "platform": expected.platform,
                "version": expected.version,
                "build": expected.build,
                "bundleIdentity": expected.bundle_identity,
                "executableSha256": expected.executable_sha256,
                "contractIdentity": expected.contract_identity,
                "secretFormat": expected.secret_format,
            },
            "",
            provenance=runtime,
        )
    except AppError:
        raise _proof_error() from None
    except Exception:
        raise _proof_error() from None


def capture_vscode_proof_record(
    *,
    home: Path | None = None,
    os_name: str | None = None,
    installation_root: Path | None = None,
    process_probe: Callable[[], bool] | None = None,
    secret_backend: SecretKeyBackend | Callable[[], bytes] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Capture stable Code metadata without launching or mutating Code."""
    del home
    try:
        system = (platform.system() if os_name is None else os_name).lower()
        normalized_os = "macos" if system in {"darwin", "macos"} else system
        if normalized_os not in {"macos", "linux"}:
            raise _proof_error()
        probe = process_probe or (lambda: _default_process_probe(normalized_os))
        if probe() is not True:
            raise _proof_error()
        root = installation_root or _default_installation_root(normalized_os)
        facts = _installed_runtime_facts(os_name=normalized_os, installation_root=root)
        _verify_secret_contract(normalized_os, facts.secret_format, secret_backend)
        captured = now or datetime.now(UTC)
        fresh = captured + timedelta(days=1)
        return {
            "proofVersion": 1,
            "binding": {
                "platform": facts.platform,
                "channel": facts.channel,
                "profileMode": "default",
                "version": facts.version,
                "build": facts.build,
                "bundleIdentity": facts.bundle_identity,
                "executableSha256": facts.executable_sha256,
            },
            "contract": {
                "identity": facts.contract_identity,
                "safeStorage": "proven",
                "keyContext": "proven",
                "secretFormat": facts.secret_format,
            },
            "capture": {
                "provenance": "read-only-installed-stable-code",
                "capturedAt": captured.isoformat().replace("+00:00", "Z"),
                "freshUntil": fresh.isoformat().replace("+00:00", "Z"),
            },
        }
    except AppError:
        raise _proof_error() from None
    except Exception:
        raise _proof_error() from None


def write_vscode_proof_record(
    record: Mapping[str, object],
    path: Path,
) -> None:
    """Validate and atomically publish a private owner-only proof record."""
    try:
        _strict_record(record)
        atomic_write_json(path, record)
    except AppError:
        raise _proof_error() from None
    except Exception:
        raise _proof_error() from None


def _default_installation_root(os_name: str) -> Path:
    if os_name == "macos":
        return Path("/Applications/Visual Studio Code.app")
    return Path("/usr/share/code")


def preflight(runtime: VSCodeRuntime | None = None) -> VSCodeRuntime:
    """Require a proven stable Code runtime and an initial quiescence check."""
    if runtime is None:
        raise AppError(
            "VS Code support is unavailable until the macOS and Linux "
            "proof-of-life contract (paths, safeStorage, process detection, "
            "version) is recorded",
            exit_code=1,
        )
    if not runtime.safe_storage_supported or not runtime.key_context:
        raise AppError("VS Code safeStorage/key context is unproven")
    if not runtime.process_quiescent:
        raise AppError("VS Code is not quiescent; close it before inspecting or changing settings")
    effective_os = runtime.os_name or (
        runtime.supported_os[0] if len(runtime.supported_os) == 1 else ""
    )
    if effective_os not in {"macos", "linux"}:
        raise AppError("VS Code platform is unsupported")
    effective_channel = runtime.channel or (
        runtime.supported_channels[0] if len(runtime.supported_channels) == 1 else ""
    )
    if effective_channel != "stable":
        raise AppError("VS Code channel is unsupported; stable Code is required")
    if runtime.settings_path.name != "chatLanguageModels.json":
        raise AppError("VS Code settings path is not proof-bound")
    members = (runtime.state_db_path, runtime.state_wal_path, runtime.state_shm_path)
    if tuple(path.parent for path in members) != (runtime.state_db_path.parent,) * 3:
        raise AppError("VS Code state database members must share a parent")
    if tuple(path.name for path in members) != STATE_DB_MEMBERS:
        raise AppError("VS Code state database member names are not proof-bound")
    if runtime.runtime_probe is not None:
        current = runtime.runtime_probe()
        if _runtime_identity(current) != _runtime_identity(runtime):
            raise AppError("VS Code runtime proof changed before inspection")
    return runtime


def _string_list(record: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = record.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise AppError(f"VS Code runtime {key} must be a non-empty list of strings")
    return tuple(value)


def _absolute_path(record: Mapping[str, object], key: str, label: str) -> Path:
    raw = record.get(key)
    if not isinstance(raw, str) or not raw:
        raise AppError(f"VS Code {label} is absent from the runtime proof")
    path = Path(raw)
    if not path.is_absolute():
        raise AppError(f"VS Code {label} must be absolute")
    return path


def resolve_vscode_runtime(
    record: Mapping[str, object],
    *,
    os_name: str,
    channel: str,
    profile_mode: str,
) -> VSCodeRuntime:
    """Resolve only explicitly proven VS Code compatibility facts."""

    def probe() -> VSCodeRuntime:
        return resolve_vscode_runtime(
            record, os_name=os_name, channel=channel, profile_mode=profile_mode
        )

    supported_os = _string_list(record, "supportedOs")
    if os_name not in {"macos", "linux"} or os_name not in supported_os:
        raise AppError(f"VS Code operating system {os_name!r} is not proof-supported")
    if any(value not in {"macos", "linux"} for value in supported_os):
        raise AppError("VS Code runtime record claims an unsupported operating system")
    supported_channels = _string_list(record, "supportedChannels")
    if channel not in supported_channels:
        raise AppError(f"VS Code channel {channel!r} is not proof-supported")
    supported_profile_modes = _string_list(record, "supportedProfileModes")
    if profile_mode not in supported_profile_modes:
        raise AppError(f"VS Code profile mode {profile_mode!r} is not proof-supported")

    user_data_root = _absolute_path(record, "userDataRoot", "user data root")
    settings_path = _absolute_path(record, "settingsPath", "settings path")
    state_db_path = _absolute_path(record, "stateDbPath", "state database path")
    state_wal_path = _absolute_path(record, "stateWalPath", "state WAL path")
    state_shm_path = _absolute_path(record, "stateShmPath", "state SHM path")

    members = record.get("stateDatabaseMembers")
    if not isinstance(members, list) or tuple(members) != STATE_DB_MEMBERS:
        raise AppError("VS Code state database members are not proof-supported")

    item_table = record.get("itemTable")
    if not isinstance(item_table, Mapping):
        raise AppError("VS Code itemTable contract is absent from the runtime proof")
    if item_table.get("access") != "proof-recorded-rows-only":
        raise AppError("VS Code itemTable access must be proof-recorded-rows-only")
    if item_table.get("unknownRows") != "preserve-without-inspection":
        raise AppError("VS Code itemTable unknown rows must preserve-without-inspection")

    if record.get("safeStorage") != "proven":
        raise AppError("VS Code safeStorage is unproven")
    if record.get("keyContext") != "proven":
        raise AppError("VS Code keyContext is unproven")
    if record.get("processDetector") != "proven":
        raise AppError("VS Code processDetector is unproven")
    if record.get("processQuiescent") is not True:
        raise AppError("VS Code is not quiescent; close it before inspecting or changing settings")

    vendor_mapping = record.get("vendorApiTypeMapping")
    if type(vendor_mapping) is not bool:
        raise AppError("VS Code vendor/apiType mapping flag is absent from the runtime proof")

    vendor_api_type_map: dict[str, tuple[str, str]] = {}
    if vendor_mapping:
        raw_map = record.get("vendorApiTypeMap")
        if not isinstance(raw_map, Mapping):
            raise AppError("VS Code vendor/apiType map is absent from the runtime proof")
        for provider_key, entry in raw_map.items():
            if not isinstance(entry, Mapping):
                raise AppError(
                    f"VS Code vendor/apiType entry for {provider_key!r} must be a mapping"
                )
            vendor = entry.get("vendor")
            api_type = entry.get("apiType")
            if not isinstance(vendor, str) or not isinstance(api_type, str):
                raise AppError(f"VS Code vendor/apiType entry for {provider_key!r} must be strings")
            vendor_api_type_map[str(provider_key)] = (vendor, api_type)

    return VSCodeRuntime(
        supported_os=supported_os,
        supported_channels=supported_channels,
        supported_profile_modes=supported_profile_modes,
        user_data_root=user_data_root,
        settings_path=settings_path,
        state_db_path=state_db_path,
        state_wal_path=state_wal_path,
        state_shm_path=state_shm_path,
        safe_storage_supported=True,
        key_context="proven",
        process_quiescent=True,
        vendor_api_type_mapping=vendor_mapping,
        vendor_api_type_map=vendor_api_type_map,
        os_name=os_name,
        channel=channel,
        profile_mode=profile_mode,
        secret_format=(
            str(record.get("secretFormat"))
            if isinstance(record.get("secretFormat"), str)
            else ("oscrypt-v10" if os_name == "macos" else "oscrypt-v11")
        ),
        runtime_probe=probe,
        runtime_recheck=lambda: probe().process_quiescent,
    )


def discover_vscode_user_data_root(home: Path | None = None, os_name: str | None = None) -> Path:
    """Return the stable Code default-profile root for supported platforms."""
    root = Path.home() if home is None else home
    system = (platform.system() if os_name is None else os_name).lower()
    if system in {"darwin", "macos"}:
        return root / "Library" / "Application Support" / "Code" / "User"
    if system == "linux":
        return root / ".config" / "Code" / "User"
    raise AppError("VS Code platform is unsupported")


def _default_process_probe(os_name: str | None = None) -> bool:
    """True only when stable Code has no matching process."""
    system = (platform.system() if os_name is None else os_name).lower()
    names = ("Electron", "Code") if system in {"darwin", "macos"} else ("code", "Code")
    try:
        results = [
            subprocess.run(["pgrep", "-x", name], check=False, capture_output=True, text=False)
            for name in names
        ]
    except OSError as exc:
        raise AppError("VS Code process quiescence check failed") from exc
    if any(result.returncode == 0 for result in results):
        return False
    if all(result.returncode == 1 for result in results):
        return True
    raise AppError("VS Code process quiescence check failed")


def discover_vscode_runtime(
    *,
    home: Path | None = None,
    os_name: str | None = None,
    process_probe: Callable[[], bool] | None = None,
    secret_backend: SecretKeyBackend | Callable[[], bytes] | None = None,
    secret_format: str | None = None,
) -> VSCodeRuntime:
    """Discover only stable default-profile Code and require quiescence first."""
    system = (platform.system() if os_name is None else os_name).lower()
    normalized_os = "macos" if system in {"darwin", "macos"} else system
    user_data_root = discover_vscode_user_data_root(home, normalized_os)
    format_value = secret_format or ("oscrypt-v10" if normalized_os == "macos" else "oscrypt-v11")
    backend = secret_backend
    if backend is None:
        backend = _secret_backend(normalized_os)
    probe = process_probe or (lambda: _default_process_probe(normalized_os))
    try:
        quiescent = probe()
    except AppError:
        raise
    except Exception as exc:
        raise AppError("VS Code process quiescence check failed") from exc
    if quiescent is not True:
        raise AppError("VS Code is not quiescent; close it before inspecting or changing settings")
    try:
        _key_bytes(backend)
    except AppError:
        raise
    except Exception as exc:
        raise AppError("VS Code safe-storage key lookup failed") from exc
    runtime = VSCodeRuntime(
        supported_os=(normalized_os,),
        supported_channels=("stable",),
        supported_profile_modes=("default",),
        user_data_root=user_data_root,
        settings_path=user_data_root / "chatLanguageModels.json",
        state_db_path=user_data_root / "globalStorage" / "state.vscdb",
        state_wal_path=user_data_root / "globalStorage" / "state.vscdb-wal",
        state_shm_path=user_data_root / "globalStorage" / "state.vscdb-shm",
        safe_storage_supported=True,
        key_context="proven",
        process_quiescent=True,
        vendor_api_type_mapping=False,
        os_name=normalized_os,
        channel="stable",
        profile_mode="default",
        secret_format=format_value,
        secret_backend=backend,
        runtime_recheck=probe,
    )
    return runtime


def acquire_vscode_runtime(
    record: Mapping[str, object],
    *,
    os_name: str,
    channel: str,
    profile_mode: str,
    secret_backend: SecretKeyBackend | Callable[[], bytes] | None = None,
    process_probe: Callable[[], bool] | None = None,
) -> VSCodeRuntime:
    """Resolve a recorded proof and require the injected process check to be quiescent."""
    runtime = resolve_vscode_runtime(
        record, os_name=os_name, channel=channel, profile_mode=profile_mode
    )
    if process_probe is not None:
        try:
            if process_probe() is not True:
                raise AppError(
                    "VS Code is not quiescent; close it before inspecting or changing settings"
                )
        except AppError:
            raise
        except Exception as exc:
            raise AppError("VS Code process quiescence check failed") from exc
    return replace(
        runtime,
        secret_backend=secret_backend,
        runtime_recheck=process_probe or runtime.runtime_recheck,
    )


def bind_vscode_runtime_paths(
    runtime: VSCodeRuntime,
    *,
    settings_path: Path,
    state_db_path: Path,
    state_wal_path: Path,
    state_shm_path: Path,
) -> VSCodeRuntime:
    """Allow only the settings and SQLite paths bound by the runtime proof."""
    if not isinstance(runtime, VSCodeRuntime):
        raise AppError("VS Code runtime proof is unavailable")
    if (
        settings_path,
        state_db_path,
        state_wal_path,
        state_shm_path,
    ) != (
        runtime.settings_path,
        runtime.state_db_path,
        runtime.state_wal_path,
        runtime.state_shm_path,
    ):
        raise AppError("VS Code destination paths do not match the runtime proof")
    return runtime


def _validate_json_strings(value: object) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AppError("VS Code settings contain a non-finite JSON number")
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise AppError("VS Code settings contain invalid Unicode strings")
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _validate_json_strings(key)
            _validate_json_strings(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _validate_json_strings(nested)


def _validate_settings(settings: object) -> dict[str, Any] | list[dict[str, Any]]:
    if isinstance(settings, list):
        if not settings:
            raise AppError("VS Code settings provider list must not be empty")
        _validate_json_strings(settings)
        for index, provider in enumerate(settings):
            if not isinstance(provider, dict):
                raise AppError(f"VS Code settings[{index}] must be an object")
            name = provider.get("name")
            if not isinstance(name, str) or not name:
                raise AppError(f"VS Code settings[{index}].name must be a non-empty string")
            raw_models = provider.get("models")
            if raw_models is None:
                continue
            if not isinstance(raw_models, list):
                raise AppError(f"VS Code settings[{index}].models must be a list")
            for m_index, model in enumerate(raw_models):
                if not isinstance(model, dict):
                    raise AppError(f"VS Code settings[{index}].models[{m_index}] must be an object")
                model_id = model.get("id")
                if not isinstance(model_id, str) or not model_id:
                    raise AppError(
                        f"VS Code settings[{index}].models[{m_index}].id must be a non-empty string"
                    )
        return settings
    if not isinstance(settings, dict):
        raise AppError("VS Code settings must be a JSON object or provider list")
    _validate_json_strings(settings)
    raw_providers = settings.get("providers")
    if raw_providers is None:
        if "providers" in settings:
            raise AppError("VS Code settings providers must be a list")
        return settings
    if not isinstance(raw_providers, list):
        raise AppError("VS Code settings providers must be a list")
    for index, provider in enumerate(raw_providers):
        if not isinstance(provider, dict):
            raise AppError(f"VS Code settings providers[{index}] must be an object")
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise AppError(f"VS Code settings providers[{index}].id must be a non-empty string")
        raw_models = provider.get("models")
        if raw_models is None:
            if "models" in provider:
                raise AppError(f"VS Code settings providers[{index}].models must be a list")
            continue
        if not isinstance(raw_models, list):
            raise AppError(f"VS Code settings providers[{index}].models must be a list")
        for m_index, model in enumerate(raw_models):
            if not isinstance(model, dict):
                raise AppError(
                    f"VS Code settings providers[{index}].models[{m_index}] must be an object"
                )
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id:
                raise AppError(
                    f"VS Code settings providers[{index}].models[{m_index}].id "
                    "must be a non-empty string"
                )
    return settings


def _validate_provider_api_key_references(
    settings: dict[str, Any] | list[dict[str, Any]],
) -> None:
    providers: object = settings if isinstance(settings, list) else settings.get("providers", [])
    if not isinstance(providers, list):
        return
    for provider in providers:
        if not isinstance(provider, Mapping) or "apiKey" not in provider:
            continue
        api_key = provider["apiKey"]
        if not isinstance(api_key, str) or not api_key:
            raise AppError("VS Code provider apiKey must be a non-empty string")
        if api_key.startswith("env."):
            try:
                secret_variable(api_key)
            except AppError as exc:
                raise AppError(
                    "VS Code provider apiKey must use an env.VAR_NAME reference"
                ) from exc
        elif not (
            api_key.startswith(VSCODE_SECRET_INPUT_PREFIX)
            and api_key.endswith("}")
            and re.fullmatch(r"\$\{input:chat\.lm\.secret\.lm-[a-z0-9][a-z0-9_-]*\}", api_key)
        ):
            raise AppError("VS Code provider apiKey must be an env or Code input reference")


def _serialize_vscode_settings(
    settings: dict[str, Any] | list[dict[str, Any]],
) -> bytes:
    validated = _validate_settings(settings)
    _validate_provider_api_key_references(validated)
    try:
        return (json.dumps(validated, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (TypeError, UnicodeError, ValueError):
        raise AppError("VS Code settings cannot be serialized as UTF-8 JSON") from None


def _parse_vscode_settings(source: bytes) -> dict[str, Any] | list[dict[str, Any]]:
    try:
        settings = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, _StrictJsonError):
        raise AppError("VS Code settings contain malformed JSON") from None
    except json.JSONDecodeError as exc:
        raise AppError(f"VS Code settings contain malformed JSON at byte {exc.pos}") from None
    return _validate_settings(settings)


def project_vscode_providers(
    registry: Registry, runtime: VSCodeRuntime
) -> tuple[dict[str, Any], ...]:
    """Project the legacy object-form fixture used by offline adapter tests."""
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
            vscode_extra_args=model.vscode_extra_args(),
            vscode_extra_headers=model.vscode_extra_headers(),
            base_url_override=model.base_url_override,
        )
        for provider, model in registry.emitted_models("vscode")
    )
    return _project_legacy_vscode_model_snapshots(models, runtime)


def project_vscode_model_snapshots(
    models: Sequence[ResolvedModel], runtime: VSCodeRuntime
) -> tuple[dict[str, Any], ...]:
    """Project the current stable Code language-model list without secrets."""
    preflight(runtime)
    secret_components = _vscode_secret_components(model.provider_key for model in models)
    provider_entries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for model in models:
        if model.provider_key not in provider_entries:
            provider_name = model.provider_name or model.provider_key
            entry: dict[str, Any] = {
                "name": provider_name,
                "vendor": "customendpoint",
                "apiKey": f"{VSCODE_SECRET_INPUT_PREFIX}{secret_components[model.provider_key]}}}",
                "apiType": "chat-completions",
                "models": [],
                "settings": {},
            }
            if runtime.vendor_api_type_mapping:
                mapping = runtime.vendor_api_type_map.get(model.provider_key)
                if mapping is None:
                    raise AppError(
                        f"VS Code vendor/apiType mapping for {model.provider_key!r} "
                        "is not proof-recorded"
                    )
                entry["vendor"] = mapping[0]
                entry["apiType"] = mapping[1]
            provider_entries[model.provider_key] = entry
            order.append(model.provider_key)
        model_id = model.vscode_id or model.model
        projected_model: dict[str, Any] = {
            "id": model_id,
            "name": model.display_name,
            "url": model.resolved_base_url().rstrip("/") + "/chat/completions",
            "toolCalling": model.tool_calling,
            "vision": not model.no_image_support,
            "maxInputTokens": model.max_input_tokens or 0,
            "maxOutputTokens": model.max_output_tokens,
        }
        if model.vscode_reasoning_levels:
            projected_model["supportsReasoningEffort"] = list(model.vscode_reasoning_levels)
            if model.vscode_default_reasoning_level is not None:
                projected_model["defaultReasoningEffort"] = model.vscode_default_reasoning_level
                provider_entries[model.provider_key]["settings"][model_id] = {
                    "reasoningEffort": model.vscode_default_reasoning_level
                }
            else:
                provider_entries[model.provider_key]["settings"][model_id] = {}
        # ponytail: VS Code's chatLanguageModels contract renders request
        # passthroughs as modelOptions (body) and requestHeaders (headers).
        if model.vscode_extra_args is not None:
            projected_model["modelOptions"] = model.vscode_extra_args
        if model.vscode_extra_headers is not None:
            projected_model["requestHeaders"] = model.vscode_extra_headers
        provider_entries[model.provider_key]["models"].append(projected_model)
    return tuple(provider_entries[key] for key in order)


def _merge_provider(
    existing: Mapping[str, Any],
    generated: Mapping[str, Any],
    owned_model_ids: Collection[str],
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(existing)
    for key, value in generated.items():
        if key == "models":
            continue
        merged[key] = value
    existing_models = existing.get("models", [])
    generated_models = generated.get("models", [])
    merged_models = reconcile(
        existing_models,
        generated_models,
        set(owned_model_ids),
        lambda m: m["id"],
    )
    merged["models"] = list(merged_models)
    return merged


def _vscode_secret_components(provider_keys: Iterable[str]) -> Mapping[str, str]:
    projected: dict[str, str] = {}
    owners: dict[str, str] = {}
    for provider_key in dict.fromkeys(provider_keys):
        component = _VSCODE_SECRET_COMPONENT_RE.sub("-", provider_key.lower()).strip("-")
        if not component:
            raise AppError(f"VS Code provider key {provider_key!r} has no safe secret identifier")
        owner = owners.get(component)
        if owner is not None and owner != provider_key:
            raise AppError(
                f"VS Code provider keys {owner!r} and {provider_key!r} "
                f"share secret identifier {component!r}"
            )
        owners[component] = provider_key
        projected[provider_key] = component
    return projected


def _validate_ownership(
    owned_provider_ids: Collection[str], owned_model_ids: Mapping[str, Collection[str]]
) -> None:
    for provider_id in owned_provider_ids:
        if not isinstance(provider_id, str) or not provider_id:
            raise AppError("VS Code ownership provider IDs must be non-empty strings")
    for provider_id, model_ids in owned_model_ids.items():
        if provider_id not in owned_provider_ids:
            raise AppError("VS Code model ownership must belong to an owned ModFig provider")
        if not isinstance(model_ids, Collection) or isinstance(model_ids, str):
            raise AppError("VS Code model ownership IDs must be a collection of strings")
        if not all(isinstance(model_id, str) and model_id for model_id in model_ids):
            raise AppError("VS Code model ownership IDs must be non-empty strings")


def plan_vscode(
    registry: Registry,
    settings: Mapping[str, Any],
    *,
    owned_provider_ids: set[str] | frozenset[str],
    owned_model_ids: Mapping[str, Collection[str]],
    runtime: VSCodeRuntime,
) -> VSCodePlan:
    """Merge ModFig-owned VS Code records while preserving every foreign setting."""
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
            vscode_extra_args=model.vscode_extra_args(),
            vscode_extra_headers=model.vscode_extra_headers(),
            base_url_override=model.base_url_override,
        )
        for provider, model in registry.emitted_models("vscode")
    )
    return plan_vscode_models(
        models,
        settings,
        owned_provider_ids=owned_provider_ids,
        owned_model_ids=owned_model_ids,
        runtime=runtime,
    )


def plan_vscode_models(
    models: Sequence[ResolvedModel],
    settings: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    owned_provider_ids: set[str] | frozenset[str],
    owned_model_ids: Mapping[str, Collection[str]],
    runtime: VSCodeRuntime,
) -> VSCodePlan:
    """Merge bounded host model snapshots while preserving foreign settings."""
    _validate_ownership(owned_provider_ids, owned_model_ids)
    validated_settings = _validate_settings(
        list(settings) if not isinstance(settings, Mapping) else dict(settings)
    )
    if isinstance(validated_settings, dict):
        _validate_legacy_ownership(owned_provider_ids, owned_model_ids)
    preflight(runtime)
    if isinstance(validated_settings, list):
        return _plan_current_vscode_models(
            models,
            validated_settings,
            owned_provider_ids=owned_provider_ids,
            owned_model_ids=owned_model_ids,
            runtime=runtime,
        )
    generated = _project_legacy_vscode_model_snapshots(models, runtime)
    generated_by_id: dict[str, dict[str, Any]] = {p["id"]: p for p in generated}

    existing_providers = validated_settings.get("providers", [])
    if not isinstance(existing_providers, list):
        existing_providers = []

    def owned_for(provider_id: str) -> Collection[str]:
        return owned_model_ids.get(provider_id, ())

    seen_providers: set[str] = set()
    for provider in existing_providers:
        if not isinstance(provider, Mapping):
            raise AppError("VS Code settings providers must be objects")
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise AppError("VS Code settings providers must have a non-empty string id")
        if provider_id in seen_providers:
            raise AppError(f"VS Code settings contain duplicate provider id {provider_id!r}")
        seen_providers.add(provider_id)
        raw_models = provider.get("models", [])
        if raw_models is None:
            continue
        if not isinstance(raw_models, list):
            raise AppError(f"VS Code provider {provider_id!r} models must be a list")
        seen_models: set[str] = set()
        for model in raw_models:
            if not isinstance(model, Mapping):
                raise AppError(f"VS Code provider {provider_id!r} models must be objects")
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id:
                raise AppError(
                    f"VS Code provider {provider_id!r} models must have a non-empty string id"
                )
            if model_id in seen_models:
                raise AppError(f"VS Code settings contain duplicate model id {model_id!r}")
            seen_models.add(model_id)

    merged: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for existing in existing_providers:
        provider_id = existing["id"]
        if provider_id in owned_provider_ids:
            if provider_id not in generated_by_id:
                existing_models = existing.get("models", [])
                if not isinstance(existing_models, list):
                    raise AppError(f"VS Code provider {provider_id!r} models must be a list")
                foreign_models = [
                    model for model in existing_models if model["id"] not in owned_for(provider_id)
                ]
                foreign_fields = set(existing) - {
                    "id",
                    "baseUrl",
                    "apiKey",
                    "vendor",
                    "apiType",
                    "models",
                }
                if foreign_models or foreign_fields:
                    retained = dict(existing)
                    retained["models"] = foreign_models
                    merged.append(retained)
                continue
            merged.append(
                _merge_provider(existing, generated_by_id[provider_id], owned_for(provider_id))
            )
            emitted.add(provider_id)
        else:
            generated_provider = generated_by_id.get(provider_id)
            if generated_provider is not None:
                existing_models = existing.get("models", [])
                if not isinstance(existing_models, list):
                    raise AppError(f"VS Code provider {provider_id!r} models must be a list")
                for key, value in generated_provider.items():
                    if key != "models" and existing.get(key) != value:
                        raise CollisionError(provider_id)
                generated_by_model_id = {
                    model["id"]: model for model in generated_provider["models"]
                }
                for model in existing_models:
                    generated_model = generated_by_model_id.get(model["id"])
                    if generated_model is not None and model != generated_model:
                        raise CollisionError((provider_id, model["id"]))
                adopted_models = set(owned_for(provider_id)) | set(generated_by_model_id)
                merged.append(_merge_provider(existing, generated_provider, adopted_models))
                emitted.add(provider_id)
            else:
                merged.append(dict(existing))
                emitted.add(provider_id)

    for gen in generated:
        if gen["id"] not in emitted:
            merged.append(dict(gen))
            emitted.add(gen["id"])

    planned = dict(validated_settings)
    planned["providers"] = merged
    owned_model_ids_generated = {
        provider["id"]: frozenset(model["id"] for model in provider["models"])
        for provider in generated
    }
    return VSCodePlan(
        settings=planned,
        owned_provider_ids=frozenset(generated_by_id.keys()),
        owned_model_ids=owned_model_ids_generated,
    )


def _plan_current_vscode_models(
    models: Sequence[ResolvedModel],
    settings: list[dict[str, Any]],
    *,
    owned_provider_ids: Collection[str],
    owned_model_ids: Mapping[str, Collection[str]],
    runtime: VSCodeRuntime,
) -> VSCodePlan:
    generated = project_vscode_model_snapshots(models, runtime)
    provider_keys: list[str] = []
    for model in models:
        if model.provider_key not in provider_keys:
            provider_keys.append(model.provider_key)
    generated_by_key = dict(zip(provider_keys, generated, strict=True))
    generated_by_name: dict[str, Mapping[str, Any]] = {}
    provider_key_by_name = {
        str(provider["name"]): key for key, provider in generated_by_key.items()
    }
    for provider in generated:
        name = str(provider["name"])
        if name in generated_by_name:
            raise AppError(f"duplicate generated VS Code provider name {name!r}")
        generated_by_name[name] = provider
    owned = set(owned_provider_ids)
    seen_names: set[str] = set()
    for index, provider in enumerate(settings):
        candidate_name = provider.get("name")
        if not isinstance(candidate_name, str) or not candidate_name:
            raise AppError(f"VS Code settings[{index}].name must be a non-empty string")
        if candidate_name in seen_names:
            raise AppError(f"duplicate VS Code provider name {candidate_name!r}")
        seen_names.add(candidate_name)
        models_value = provider.get("models", [])
        if not isinstance(models_value, list):
            raise AppError(f"VS Code provider {candidate_name!r} models must be a list")
        model_names: set[str] = set()
        for model in models_value:
            if not isinstance(model, Mapping) or not isinstance(model.get("id"), str):
                raise AppError(
                    f"VS Code provider {candidate_name!r} model IDs must be non-empty strings"
                )
            model_id = model["id"]
            if not model_id:
                raise AppError(
                    f"VS Code provider {candidate_name!r} model IDs must be non-empty strings"
                )
            if model_id in model_names:
                raise AppError(f"duplicate VS Code model ID {model_id!r}")
            model_names.add(model_id)

    known_provider_fields = {"name", "vendor", "apiKey", "apiType", "models"}
    merged: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for existing in settings:
        existing_name = str(existing["name"])
        generated_provider = generated_by_name.get(existing_name)
        provider_key = provider_key_by_name.get(existing_name)
        prior_ids: Collection[str] = owned_model_ids.get(existing_name, ())
        if provider_key is not None and not prior_ids:
            prior_ids = owned_model_ids.get(f"{VSCODE_PROVIDER_PREFIX}{provider_key}", ())
        if generated_provider is not None:
            if existing_name not in owned:
                for key, value in generated_provider.items():
                    if key != "models" and existing.get(key) != value:
                        raise CollisionError(existing_name)
                generated_by_model_id = {
                    model["id"]: model for model in generated_provider["models"]
                }
                for model in models_value:
                    generated_model = generated_by_model_id.get(model["id"])
                    if generated_model is not None and model != generated_model:
                        raise CollisionError((existing_name, model["id"]))
            managed_ids = set(prior_ids) | {
                str(model["id"]) for model in generated_provider["models"]
            }
            merged.append(_merge_provider(existing, generated_provider, managed_ids))
            emitted.add(existing_name)
            continue
        if existing_name not in owned:
            merged.append(dict(existing))
            continue
        existing_models = existing.get("models", [])
        if not isinstance(existing_models, list):
            raise AppError(f"VS Code provider {existing_name!r} models must be a list")
        foreign_models = [
            model for model in existing_models if model.get("id") not in set(prior_ids)
        ]
        foreign_fields = set(existing) - known_provider_fields
        if foreign_models or foreign_fields:
            retained = dict(existing)
            retained["models"] = foreign_models
            merged.append(retained)
        emitted.add(existing_name)

    for provider in generated:
        name = str(provider["name"])
        if name not in emitted:
            merged.append(dict(provider))

    owned_model_ids_generated = {
        str(provider["name"]): frozenset(str(model["id"]) for model in provider["models"])
        for provider in generated
    }
    return VSCodePlan(
        settings=merged,
        owned_provider_ids=frozenset(owned_model_ids_generated),
        owned_model_ids=owned_model_ids_generated,
    )


def _project_legacy_vscode_model_snapshots(
    models: Sequence[ResolvedModel], runtime: VSCodeRuntime
) -> tuple[dict[str, Any], ...]:
    preflight(runtime)
    provider_entries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for model in models:
        if model.provider_key not in provider_entries:
            variable = secret_variable(model.api_key_reference)
            entry: dict[str, Any] = {
                "id": f"{VSCODE_PROVIDER_PREFIX}{model.provider_key}",
                "baseUrl": model.resolved_base_url(),
                "apiKey": f"env.{variable}",
                "models": [],
            }
            if runtime.vendor_api_type_mapping:
                mapping = runtime.vendor_api_type_map.get(model.provider_key)
                if mapping is None:
                    raise AppError(
                        f"VS Code vendor/apiType mapping for {model.provider_key!r} "
                        "is not proof-recorded"
                    )
                entry["vendor"] = mapping[0]
                entry["apiType"] = mapping[1]
            provider_entries[model.provider_key] = entry
            order.append(model.provider_key)
        provider_entries[model.provider_key]["models"].append(
            {
                "id": model.vscode_id or model.model,
                "model": model.model,
                "displayName": model.display_name,
                "maxOutputTokens": model.max_output_tokens,
            }
        )
    return tuple(provider_entries[key] for key in order)


def _validate_legacy_ownership(
    owned_provider_ids: Collection[str], owned_model_ids: Mapping[str, Collection[str]]
) -> None:
    for provider_id in owned_provider_ids:
        if (
            not isinstance(provider_id, str)
            or not provider_id.startswith(VSCODE_PROVIDER_PREFIX)
            or provider_id == VSCODE_PROVIDER_PREFIX
        ):
            raise AppError("VS Code ownership provider IDs must use the ModFig/ namespace")
    for provider_id, model_ids in owned_model_ids.items():
        if provider_id not in owned_provider_ids:
            raise AppError("VS Code model ownership must belong to an owned ModFig provider")
        if not isinstance(model_ids, Collection) or isinstance(model_ids, str):
            raise AppError("VS Code model ownership IDs must be a collection of strings")
        if not all(isinstance(model_id, str) and model_id for model_id in model_ids):
            raise AppError("VS Code model ownership IDs must be non-empty strings")


def plan_secret_rows_from_models(
    models: Sequence[ResolvedModel],
    runtime: VSCodeRuntime,
    secrets: Mapping[str, bytes],
    *,
    row_prefix: str = "modfig:ModFig/",
) -> Mapping[str, bytes | str]:
    preflight(runtime)
    if runtime.secret_format == "basic-text":
        raise AppError("VS Code basic-text secret rows are not supported for transactional apply")
    if runtime.secret_backend is None:
        raise AppError("VS Code secret backend is unavailable")
    contract = SecretContract(runtime.os_name, runtime.channel, runtime.secret_format)
    rows: dict[str, bytes | str] = {}
    current_code_rows = row_prefix == VSCODE_SECRET_ROW_PREFIX
    secret_components = (
        _vscode_secret_components(model.provider_key for model in models)
        if current_code_rows
        else {}
    )
    seen_providers: set[str] = set()
    for model in models:
        if current_code_rows and model.provider_key in seen_providers:
            continue
        seen_providers.add(model.provider_key)
        variable = secret_variable(model.api_key_reference)
        plaintext = secrets.get(variable)
        if plaintext is None:
            raise AppError(f"VS Code secret for {variable!r} is not resolved")
        if not isinstance(plaintext, bytes):
            raise AppError("VS Code secret plaintext must be bytes")
        row_id = (
            f"{row_prefix}{secret_components[model.provider_key]}"
            if current_code_rows
            else f"{row_prefix}{model.provider_key}:{model.vscode_id or model.model}"
        )
        rows[row_id] = encode_secret(plaintext, contract, runtime.secret_backend)
    return rows
