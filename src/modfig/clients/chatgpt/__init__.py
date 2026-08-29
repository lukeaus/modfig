from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import urlparse

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.toml_document import TOMLDocument

from ...adapter_routes import CODEX_MODEL_CATALOG_FILENAME, resolve_codex_config_path
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
from ...platform import CapabilityUnavailableError
from ...registry import Registry
from ...secrets import secret_variable
from ...storage import atomic_write_json, read_private_bytes
from ...toml_storage import ParsedToml, dump_toml, load_toml


class ChatGPTConfigError(AppError):
    """ChatGPT shared configuration cannot be safely inspected or projected."""


_CHATGPT_HOME_GRANT = "chatgpt-home"
_CHATGPT_LEGACY_PROFILE = "modfig.config.toml"
_CHATGPT_BASE_ARTIFACT = ArtifactIdentity(_CHATGPT_HOME_GRANT, PurePosixPath("config.toml"))
_CHATGPT_LEGACY_PROFILE_ARTIFACT = ArtifactIdentity(
    _CHATGPT_HOME_GRANT, PurePosixPath(_CHATGPT_LEGACY_PROFILE)
)
_CHATGPT_LEGACY_CATALOG_ARTIFACT = ArtifactIdentity(
    _CHATGPT_HOME_GRANT, PurePosixPath(CODEX_MODEL_CATALOG_FILENAME)
)
_CHATGPT_METADATA = AdapterMetadata("modfig.chatgpt", "chatgpt", "core")
_CHATGPT_PROOF_VERSION = 1
_CHATGPT_EXECUTABLES = ("codex", "codex-cli")
_CHATGPT_MANAGED_FIELDS = ["name", "base_url", "env_key", "wire_api", "models"]
_CHATGPT_CATALOG_BASE_INSTRUCTIONS = "You are Codex, a coding agent."
_CHATGPT_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHATGPT_VERSION_RE = re.compile(
    r"^(?:codex|codex-cli)(?:\s+v?)\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$"
)
_CHATGPT_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_CHATGPT_PROVIDER_PREFIX = "modfig-"
_CHATGPT_PROFILE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_CHATGPT_REASONING_LEVEL_DESCRIPTIONS = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
    "max": "Maximum reasoning depth for the hardest problems",
    "ultra": "Maximum reasoning with automatic task delegation",
}


def _profile_artifact(provider_key: str) -> ArtifactIdentity:
    return ArtifactIdentity(_CHATGPT_HOME_GRANT, PurePosixPath(f"{provider_key}.config.toml"))


def _catalog_artifact(provider_key: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        _CHATGPT_HOME_GRANT, PurePosixPath(f"modfig-{provider_key}-catalog.json")
    )


def _catalog_filename(provider_key: str) -> str:
    return f"modfig-{provider_key}-catalog.json"


@dataclass(frozen=True)
class ChatGPTRuntime:
    config_path: Path
    codex_home: Path
    executable: Path
    executable_sha256: str
    version: str
    process_quiescent: bool = True
    runtime_recheck: Any = None


def _proof_error() -> CapabilityUnavailableError:
    return CapabilityUnavailableError(
        "ChatGPT runtime proof is unavailable; run `modfig chatgpt proof capture` "
        "with a quiescent Codex CLI"
    )


def _codex_executable() -> Path:
    for name in _CHATGPT_EXECUTABLES:
        found = shutil.which(name)
        if found:
            return _canonical_executable(Path(found))
    raise _proof_error()


def _canonical_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _proof_error() from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise _proof_error()
    return resolved


def _executable_sha256(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise _proof_error() from exc


def _codex_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"], check=False, capture_output=True, text=True
        )
    except OSError as exc:
        raise _proof_error() from exc
    if result.returncode != 0:
        raise _proof_error()
    value = (result.stdout or result.stderr).strip().splitlines()
    if not value or not _CHATGPT_VERSION_RE.fullmatch(value[0]):
        raise _proof_error()
    return value[0]


def _codex_process_quiescent() -> bool:
    try:
        results = [
            subprocess.run(["pgrep", "-x", name], check=False, capture_output=True)
            for name in _CHATGPT_EXECUTABLES
        ]
    except OSError as exc:
        raise _proof_error() from exc
    if any(result.returncode == 0 for result in results):
        return False
    if all(result.returncode == 1 for result in results):
        return True
    raise _proof_error()


def _config_shape(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "format": "toml",
            "configExists": False,
            "modelProvidersTable": False,
            "providerTableCount": 0,
            "profilesTable": False,
            "catalogOverrides": False,
            "managedFields": _CHATGPT_MANAGED_FIELDS,
        }
    try:
        document = load_chatgpt_config(path).document
    except Exception as exc:
        raise _proof_error() from exc
    providers = document.get("model_providers")
    profiles = document.get("profiles")
    if providers is not None and not isinstance(providers, Mapping):
        raise _proof_error()
    if profiles is not None and not isinstance(profiles, Mapping):
        raise _proof_error()
    catalog_pointer = document.get("model_catalog_json")
    return {
        "format": "toml",
        "configExists": True,
        "modelProvidersTable": providers is not None,
        "providerTableCount": 0 if providers is None else len(providers),
        "profilesTable": profiles is not None,
        "catalogOverrides": "catalog" in document
        or (
            "model_catalog_json" in document
            and not (
                isinstance(catalog_pointer, str)
                and Path(catalog_pointer).name.startswith("modfig-")
                and Path(catalog_pointer).name.endswith("-catalog.json")
                and Path(catalog_pointer).parent == path.parent
            )
        ),
        "managedFields": _CHATGPT_MANAGED_FIELDS,
    }


def _runtime_facts(
    config_path: Path,
    executable: Path,
    process_probe: Callable[[], bool] | None = None,
) -> ChatGPTRuntime:
    if not config_path.is_absolute() or config_path.name != "config.toml":
        raise _proof_error()
    quiescent = _codex_process_quiescent() if process_probe is None else process_probe()
    if quiescent is not True:
        raise _proof_error()
    executable = _canonical_executable(executable)
    return ChatGPTRuntime(
        config_path.absolute(),
        config_path.parent.absolute(),
        executable,
        _executable_sha256(executable),
        _codex_version(executable),
        quiescent,
    )


def capture_chatgpt_proof_record(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    executable: Path | None = None,
    process_probe: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    try:
        environment = os.environ if environ is None else environ
        config_path = resolve_chatgpt_config_path(environment, home)
        exe = _codex_executable() if executable is None else _canonical_executable(executable)
        runtime = _runtime_facts(config_path, exe, process_probe)
        captured = now or datetime.now(UTC)
        if captured.utcoffset() is None:
            raise _proof_error()
        record: dict[str, object] = {
            "proofVersion": _CHATGPT_PROOF_VERSION,
            "binding": {
                "platform": platform.system().lower(),
                "executable": str(runtime.executable),
                "executableSha256": runtime.executable_sha256,
                "version": runtime.version,
                "codexHome": str(runtime.codex_home),
                "configPath": str(runtime.config_path),
            },
            "config": _config_shape(config_path),
            "processCheck": {
                "detectorId": "pgrep-codex-cli",
                "quiescent": True,
            },
            "capture": {
                "capturedAt": _timestamp_text(captured),
                "freshUntil": _timestamp_text(captured + timedelta(days=1)),
            },
            "sanitization": {
                "containsTomlContents": False,
                "containsCredentialValues": False,
                "containsResponseBodies": False,
            },
        }
        _strict_record(record)
        return record
    except CapabilityUnavailableError:
        raise
    except Exception as exc:
        raise _proof_error() from exc


def write_chatgpt_proof_record(record: Mapping[str, object], path: Path) -> None:
    try:
        _strict_record(record)
        atomic_write_json(path, record)
    except CapabilityUnavailableError:
        raise
    except Exception as exc:
        raise _proof_error() from exc


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


def _strict_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(
            read_private_bytes(path, "ChatGPT runtime proof"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (AppError, UnicodeError, json.JSONDecodeError, _StrictJsonError):
        raise _proof_error() from None
    if not isinstance(value, Mapping):
        raise _proof_error()
    return value


def _record_string(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise _proof_error()
    return value


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _proof_error()
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _CHATGPT_TIMESTAMP_RE.fullmatch(value):
        raise _proof_error()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _proof_error() from None
    if parsed.utcoffset() is None:
        raise _proof_error()
    return parsed


def _strict_record(
    record: Mapping[str, object],
) -> tuple[
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
]:
    if set(record) != {
        "proofVersion",
        "binding",
        "config",
        "processCheck",
        "capture",
        "sanitization",
    } or (
        type(record.get("proofVersion")) is not int
        or record["proofVersion"] != _CHATGPT_PROOF_VERSION
    ):
        raise _proof_error()
    values = tuple(
        record[key] for key in ("binding", "config", "processCheck", "capture", "sanitization")
    )
    if not all(isinstance(value, Mapping) for value in values):
        raise _proof_error()
    binding, config, process, capture, sanitization = values
    assert isinstance(binding, Mapping)
    assert isinstance(config, Mapping)
    assert isinstance(process, Mapping)
    assert isinstance(capture, Mapping)
    assert isinstance(sanitization, Mapping)
    if set(binding) != {
        "platform",
        "executable",
        "executableSha256",
        "version",
        "codexHome",
        "configPath",
    }:
        raise _proof_error()
    if set(config) != {
        "format",
        "configExists",
        "modelProvidersTable",
        "providerTableCount",
        "profilesTable",
        "catalogOverrides",
        "managedFields",
    }:
        raise _proof_error()
    if set(process) != {"detectorId", "quiescent"}:
        raise _proof_error()
    if set(capture) != {"capturedAt", "freshUntil"}:
        raise _proof_error()
    if set(sanitization) != {
        "containsTomlContents",
        "containsCredentialValues",
        "containsResponseBodies",
    }:
        raise _proof_error()
    for key in ("platform", "executable", "version", "codexHome", "configPath"):
        _record_string(binding, key)
    executable = Path(str(binding["executable"]))
    codex_home = Path(str(binding["codexHome"]))
    config_path = Path(str(binding["configPath"]))
    if (
        not executable.is_absolute()
        or not codex_home.is_absolute()
        or not config_path.is_absolute()
    ):
        raise _proof_error()
    if config_path != codex_home / "config.toml":
        raise _proof_error()
    if not isinstance(binding["executableSha256"], str) or not _CHATGPT_SHA256_RE.fullmatch(
        binding["executableSha256"]
    ):
        raise _proof_error()
    if config["format"] != "toml" or type(config["configExists"]) is not bool:
        raise _proof_error()
    for key in ("modelProvidersTable", "profilesTable", "catalogOverrides"):
        if type(config[key]) is not bool:
            raise _proof_error()
    if type(config["providerTableCount"]) is not int or config["providerTableCount"] < 0:
        raise _proof_error()
    if config["managedFields"] != _CHATGPT_MANAGED_FIELDS:
        raise _proof_error()
    if config["configExists"] is False and any(
        config[key] not in (False, 0)
        for key in (
            "modelProvidersTable",
            "providerTableCount",
            "profilesTable",
            "catalogOverrides",
        )
    ):
        raise _proof_error()
    if process["detectorId"] != "pgrep-codex-cli" or process["quiescent"] is not True:
        raise _proof_error()
    captured = _timestamp(capture["capturedAt"])
    fresh_until = _timestamp(capture["freshUntil"])
    if captured >= fresh_until:
        raise _proof_error()
    if any(value is not False for value in sanitization.values()):
        raise _proof_error()
    return binding, config, process, capture, sanitization


def load_chatgpt_runtime_proof(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    executable: Path | None = None,
    process_probe: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> RuntimeProof:
    try:
        record = _strict_json(path)
        binding, _config, process, capture, _sanitization = _strict_record(record)
        config_path = resolve_chatgpt_config_path(environ, home).absolute()
        exe = _codex_executable() if executable is None else _canonical_executable(executable)
        runtime = _runtime_facts(config_path, exe, process_probe)
        if record.get("config") != _config_shape(config_path):
            raise _proof_error()
        if binding.get("executable") != str(exe):
            raise _proof_error()
        if (
            binding.get("executable") != str(runtime.executable)
            or binding.get("executableSha256") != runtime.executable_sha256
            or binding.get("version") != runtime.version
            or binding.get("codexHome") != str(runtime.codex_home)
            or binding.get("configPath") != str(runtime.config_path)
            or process.get("quiescent") is not True
        ):
            raise _proof_error()
        if binding.get("platform") != platform.system().lower():
            raise _proof_error()
        captured_at = _timestamp(capture["capturedAt"])
        fresh_until = _timestamp(capture["freshUntil"])
        current = now or datetime.now(UTC)
        if current.utcoffset() is None:
            raise _proof_error()
        if current < captured_at or current >= fresh_until:
            raise _proof_error()

        probe = _codex_process_quiescent if process_probe is None else process_probe

        def recheck() -> bool:
            if datetime.now(UTC) >= fresh_until:
                return False
            try:
                current_runtime = _runtime_facts(config_path, exe, probe)
            except CapabilityUnavailableError:
                return False
            return (
                current_runtime.config_path == runtime.config_path
                and current_runtime.executable == runtime.executable
                and current_runtime.executable_sha256 == runtime.executable_sha256
                and current_runtime.version == runtime.version
            )

        return RuntimeProof(
            {"version": runtime.version, "configPath": str(runtime.config_path)},
            "",
            provenance=replace(runtime, process_quiescent=True, runtime_recheck=recheck),
        )
    except CapabilityUnavailableError:
        raise
    except Exception as exc:
        raise _proof_error() from exc


@dataclass(frozen=True)
class ChatGPTAdapter:
    metadata: AdapterMetadata = _CHATGPT_METADATA

    def describe(self) -> AdapterMetadata:
        return self.metadata

    def validate(self, config: Mapping[str, object], context: AdapterValidationContext) -> None:
        del config
        _validate_adapter_binding(context.logical_client, context.component)

    def preflight(self, context: AdapterContext) -> PreflightDeclaration:
        _validate_adapter_binding(context.logical_client, context.component)
        grouped = _models_by_provider(context.models)
        paths = {
            _CHATGPT_BASE_ARTIFACT.relative_path,
            *(
                path
                for provider_key in grouped
                for path in (
                    _profile_artifact(provider_key).relative_path,
                    _catalog_artifact(provider_key).relative_path,
                )
            ),
        }
        owned_hashes = _artifact_hashes(context.ownership)
        paths.update(
            path for path in owned_hashes if _safe_managed_artifact_path(path) and path not in paths
        )
        if (
            _CHATGPT_LEGACY_PROFILE_ARTIFACT.relative_path in owned_hashes
            or _CHATGPT_LEGACY_CATALOG_ARTIFACT.relative_path in owned_hashes
            or "catalogSha256" in context.ownership
        ):
            paths.update(
                {
                    _CHATGPT_LEGACY_PROFILE_ARTIFACT.relative_path,
                    _CHATGPT_LEGACY_CATALOG_ARTIFACT.relative_path,
                }
            )
        identities = tuple(
            ArtifactIdentity(_CHATGPT_HOME_GRANT, path) for path in sorted(paths, key=str)
        )
        return PreflightDeclaration(
            {
                "adapterId": self.metadata.adapter_id,
                "logicalClient": self.metadata.logical_client,
                "component": "core",
                "runtimeProof": "codex-cli-catalog-v1",
            },
            tuple(SnapshotRequest(identity) for identity in identities),
            tuple(ProspectiveWrite(identity) for identity in identities),
        )

    def plan(
        self,
        context: AdapterPlanContext,
        proof: RuntimeProof,
        snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
        ownership: AdapterOwnership,
    ) -> ArtifactPlan:
        _validate_adapter_binding(context.logical_client, context.component)
        runtime = _chatgpt_runtime(proof)
        grouped = _models_by_provider(context.models)
        default_key = _default_provider_key(context.models)
        previous = _previous_provider_fingerprints(ownership)
        legacy_catalog_json = str((runtime.codex_home / CODEX_MODEL_CATALOG_FILENAME).absolute())
        legacy_catalog_hash = _legacy_owned_hashes(ownership).get(
            _CHATGPT_LEGACY_CATALOG_ARTIFACT.relative_path
        )
        # ponytail: pointers to any catalog we previously wrote (e.g. an old
        # default provider's) are owned; allow rebasing the pointer to the new
        # default provider's catalog.
        owned_catalog_jsons = tuple(
            str((runtime.codex_home / path).absolute())
            for path in _artifact_hashes(ownership)
            if path.name != "config.toml" and path.name.endswith("-catalog.json")
        )
        artifacts: list[PlannedArtifact] = []
        artifact_hashes: dict[str, str] = {}
        projected_by_key: dict[str, Mapping[str, object]] = {}
        for provider_key, models in grouped.items():
            projected = _project_resolved_models(models)
            if len(projected) != 1:
                raise AdapterPlanError("ChatGPT provider group must contain one provider")
            projected_by_key[provider_key] = projected[0]
            profile_identity = _profile_artifact(provider_key)
            catalog_identity = _catalog_artifact(provider_key)
            profile_source = snapshots.get(profile_identity, AbsentDestination())
            profile_document = _load_document(profile_source)
            catalog_pointer = str((runtime.codex_home / _catalog_filename(provider_key)).absolute())
            _reconcile_single_provider(
                profile_document.document,
                projected[0],
                previous,
                catalog_pointer,
                legacy_catalog_json if legacy_catalog_hash is not None else None,
                owned_catalog_jsons,
            )
            profile_planned = dump_toml(profile_document)
            catalog_planned = _project_chatgpt_catalog(models)
            _check_owned_or_planned_catalog(
                snapshots.get(catalog_identity, AbsentDestination()),
                catalog_planned,
                ownership,
                catalog_identity.relative_path,
            )
            artifacts.extend(
                (
                    PlannedArtifact(
                        profile_identity,
                        profile_planned,
                        "features.core.catalog",
                        {"providerKey": provider_key},
                    ),
                    PlannedArtifact(
                        catalog_identity,
                        catalog_planned,
                        "features.core.catalog",
                        {"providerKey": provider_key},
                    ),
                )
            )
            artifact_hashes[str(profile_identity.relative_path)] = hashlib.sha256(
                profile_planned
            ).hexdigest()
            artifact_hashes[str(catalog_identity.relative_path)] = hashlib.sha256(
                catalog_planned
            ).hexdigest()

        base_source = snapshots.get(_CHATGPT_BASE_ARTIFACT, AbsentDestination())
        base_document = _load_document(base_source)
        default_projected = projected_by_key.get(default_key)
        if default_projected is None:
            raise AdapterPlanError("ChatGPT default provider has no enabled models")
        default_catalog_pointer = str(
            (runtime.codex_home / _catalog_filename(default_key)).absolute()
        )
        _reconcile_single_provider(
            base_document.document,
            default_projected,
            previous,
            default_catalog_pointer,
            legacy_catalog_json if legacy_catalog_hash is not None else None,
            owned_catalog_jsons,
        )
        planned_base = dump_toml(base_document)
        artifacts.append(
            PlannedArtifact(
                _CHATGPT_BASE_ARTIFACT,
                planned_base,
                "features.core.catalog",
                {"defaultProviderKey": default_key},
            )
        )
        artifact_hashes[str(_CHATGPT_BASE_ARTIFACT.relative_path)] = hashlib.sha256(
            planned_base
        ).hexdigest()

        desired_paths = {PurePosixPath(path) for path in artifact_hashes}
        stale_owned_hashes = _artifact_hashes(ownership)
        for path, expected in _legacy_owned_hashes(ownership).items():
            stale_owned_hashes.setdefault(path, expected)
        for path, expected in stale_owned_hashes.items():
            if path in desired_paths or not _safe_managed_artifact_path(path):
                continue
            identity = ArtifactIdentity(_CHATGPT_HOME_GRANT, path)
            current = snapshots.get(identity, AbsentDestination())
            if isinstance(current, AbsentDestination):
                continue
            if not isinstance(current, bytes) or hashlib.sha256(current).hexdigest() != expected:
                raise AdapterPlanError(f"ChatGPT stale artifact {path!s} has drifted")
            artifacts.append(
                PlannedArtifact(identity, AbsentDestination(), "features.core.catalog", {})
            )

        fingerprints = {
            str(provider["id"]): _provider_fingerprint(provider)
            for provider in projected_by_key.values()
        }
        return ArtifactPlan(
            tuple(artifacts),
            {
                "providerIds": [str(provider["id"]) for provider in projected_by_key.values()],
                "providerFingerprints": fingerprints,
                "defaultProviderKey": default_key,
                "artifactHashes": artifact_hashes,
                "artifactOrder": list(artifact_hashes),
                "affectedModelIds": [
                    f"chatgpt:{model.chatgpt_catalog_id}"
                    for model in context.models
                    if model.chatgpt_catalog_id is not None
                ],
            },
        )

    def recheck(self, proof: RuntimeProof) -> None:
        runtime = _chatgpt_runtime(proof)
        if runtime.runtime_recheck is not None and runtime.runtime_recheck() is not True:
            raise AdapterPlanError("ChatGPT runtime changed after planning")

    def verify(
        self,
        context: AdapterContext,
        proof: RuntimeProof,
        written: Sequence[ArtifactSnapshot],
    ) -> None:
        _validate_adapter_binding(context.logical_client, context.component)
        runtime = _chatgpt_runtime(proof)
        expected_hashes = _artifact_hashes(context.ownership)
        if not expected_hashes:
            raise AdapterPlanError("ChatGPT verification requires artifact ownership")
        raw_order = context.ownership.get("artifactOrder", list(expected_hashes))
        if not isinstance(raw_order, (list, tuple)) or not all(
            isinstance(path, str) for path in raw_order
        ):
            raise AdapterPlanError("ChatGPT verification artifact order is invalid")
        artifact_order = tuple(PurePosixPath(path) for path in raw_order)
        if set(artifact_order) != set(expected_hashes) or len(artifact_order) != len(
            expected_hashes
        ):
            raise AdapterPlanError("ChatGPT verification artifact order diverges from ownership")
        present = [item for item in written if isinstance(item, bytes)]
        if len(present) != len(artifact_order):
            raise AdapterPlanError("ChatGPT verification artifact count diverges from ownership")
        by_path = dict(zip(artifact_order, present, strict=True))
        for path, source in by_path.items():
            if hashlib.sha256(source).hexdigest() != expected_hashes[path]:
                raise AdapterPlanError(f"ChatGPT verification artifact diverges at {path!s}")

        grouped = _models_by_provider(context.models)
        default_key = _default_provider_key(context.models)
        expected_profiles: dict[PurePosixPath, tuple[str, str, tuple[str, ...]]] = {}
        expected_catalogs: dict[PurePosixPath, tuple[str, ...]] = {}
        for provider_key, models in grouped.items():
            provider_ids = {model.chatgpt_provider_id for model in models}
            raw_catalog_ids = tuple(model.chatgpt_catalog_id for model in models)
            if (
                len(provider_ids) != 1
                or None in provider_ids
                or any(item is None for item in raw_catalog_ids)
            ):
                raise AdapterPlanError("ChatGPT verification model identities are invalid")
            catalog_ids = tuple(item for item in raw_catalog_ids if item is not None)
            provider_id = next(iter(provider_ids))
            assert provider_id is not None
            catalog_path = PurePosixPath(_catalog_filename(provider_key))
            expected_profiles[PurePosixPath(f"{provider_key}.config.toml")] = (
                provider_id,
                str(runtime.codex_home / catalog_path),
                tuple(catalog_ids),
            )
            expected_catalogs[catalog_path] = catalog_ids
        expected_profiles[PurePosixPath("config.toml")] = expected_profiles[
            PurePosixPath(f"{default_key}.config.toml")
        ]
        if set(expected_profiles) | set(expected_catalogs) != set(artifact_order):
            raise AdapterPlanError("ChatGPT verification artifact identities diverge")
        for path, (provider_id, catalog_pointer, model_ids) in expected_profiles.items():
            try:
                document = load_toml_bytes(by_path[path]).document
            except ChatGPTConfigError as exc:
                raise AdapterPlanError(f"ChatGPT verification TOML is invalid at {path!s}") from exc
            if document.get("model_provider") != provider_id:
                raise AdapterPlanError(f"ChatGPT verification provider diverges at {path!s}")
            if document.get("model_catalog_json") != catalog_pointer:
                raise AdapterPlanError(f"ChatGPT verification catalog pointer diverges at {path!s}")
            providers = document.get("model_providers")
            provider = providers.get(provider_id) if isinstance(providers, Mapping) else None
            if not isinstance(provider, Mapping) or provider.get("models") != list(model_ids):
                raise AdapterPlanError(f"ChatGPT verification models diverge at {path!s}")
        for path, model_ids in expected_catalogs.items():
            if _catalog_slugs(by_path[path]) != model_ids:
                raise AdapterPlanError(f"ChatGPT verification catalog diverges at {path!s}")


def _validate_adapter_binding(logical_client: str, component: object) -> None:
    if logical_client != "chatgpt" or component != "core":
        raise AdapterPlanError("ChatGPT adapter binding must be chatgpt/core")


adapter = ChatGPTAdapter()


def preflight() -> None:
    load_chatgpt_runtime_proof(_chatgpt_proof_path())


@dataclass(frozen=True)
class ChatGPTInspection:
    active_model: str | None
    active_provider: str | None
    foreign_provider_ids: tuple[str, ...]
    managed_provider_ids: tuple[str, ...]
    planned_provider_ids: tuple[str, ...]
    selected_profile: str | None
    profile_override: bool
    catalog_supported: bool
    requires_catalog_proof: bool
    changed: bool
    diff: str


def resolve_chatgpt_config_path(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    environment = os.environ if environ is None else environ
    trusted_home = Path.home() if home is None else home
    try:
        return resolve_codex_config_path(environment, trusted_home)
    except ValueError as exc:
        raise ChatGPTConfigError(str(exc)) from exc


def load_chatgpt_config(path: Path) -> ParsedToml:
    return load_toml(path)


def _safe_endpoint(value: str) -> bool:
    parsed = urlparse(value)
    if (
        not parsed.scheme
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return False
    try:
        # Accessing .port validates port syntax and range.
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "https" or (
        parsed.scheme == "http" and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    )


def project_chatgpt_providers(
    registry: Registry, environ: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], ...]:
    del environ
    return _project_resolved_models(_resolved_chatgpt_models(registry))


def _resolved_chatgpt_models(registry: Registry) -> tuple[ResolvedModel, ...]:
    return tuple(
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
            chatgpt_provider_id=provider.chatgpt_provider_id(),
            chatgpt_wire_api=provider.chatgpt_wire_api(),
            chatgpt_catalog_id=model.chatgpt_catalog_id(),
            chatgpt_reasoning_levels=model.chatgpt_reasoning_levels,
            chatgpt_default=provider.chatgpt_default(),
            provider_name=provider.name,
            context_window=model.context_window,
        )
        for provider, model in registry.emitted_models("chatgpt")
    )


def project_chatgpt_catalog(registry: Registry, environ: Mapping[str, str] | None = None) -> bytes:
    del environ
    return _project_chatgpt_catalog(_resolved_chatgpt_models(registry))


def _catalog_entry(
    catalog_id: str,
    label: str,
    description: str,
    context_window: int,
    image_support: bool,
    priority: int,
    reasoning_levels: Sequence[str],
) -> dict[str, Any]:
    # Field set verified against Codex 0.148.0 strict catalog parsing; unknown
    # fields or missing required fields cause the entire catalog to be discarded.
    return {
        "slug": catalog_id,
        "display_name": label,
        "description": description,
        "supported_reasoning_levels": [
            {
                "effort": effort,
                "description": _CHATGPT_REASONING_LEVEL_DESCRIPTIONS[effort],
            }
            for effort in reasoning_levels
        ],
        "shell_type": "default",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": _CHATGPT_CATALOG_BASE_INSTRUCTIONS,
        "supports_reasoning_summaries": False,
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": None,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "supports_parallel_tool_calls": False,
        "experimental_supported_tools": [],
        "context_window": context_window,
        "max_context_window": context_window,
        "input_modalities": ["text"] if not image_support else ["text", "image"],
    }


def _project_chatgpt_catalog(models: Sequence[ResolvedModel]) -> bytes:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for priority, model in enumerate(models):
        catalog_id = model.chatgpt_catalog_id
        if catalog_id is None:
            raise ChatGPTConfigError(f"ChatGPT model {model.model!r} has no catalog identity")
        if catalog_id in seen:
            raise ChatGPTConfigError(f"duplicate ChatGPT catalog id: {catalog_id!r}")
        seen.add(catalog_id)
        context_window = model.context_window
        if context_window is None or context_window <= 0:
            raise ChatGPTConfigError(f"ChatGPT model {model.model!r} has no usable context window")
        provider_label = model.provider_name or model.provider_key
        label = _catalog_display_name(model.display_name, provider_label)
        entries.append(
            _catalog_entry(
                catalog_id,
                label,
                f"{model.display_name} via {provider_label}",
                context_window,
                not model.no_image_support,
                priority,
                model.chatgpt_reasoning_levels,
            )
        )
    return (
        json.dumps({"models": entries}, indent=2, sort_keys=True, ensure_ascii=True).encode("utf-8")
        + b"\n"
    )


def _project_resolved_models(models: Sequence[ResolvedModel]) -> tuple[dict[str, Any], ...]:
    models_by_provider: dict[str, list[str]] = {}
    emitted_providers: list[ResolvedModel] = []
    for model in models:
        provider_id = model.chatgpt_provider_id
        if provider_id is None:
            raise ChatGPTConfigError(f"ChatGPT model {model.model!r} has no provider identity")
        catalog_id = model.chatgpt_catalog_id
        if catalog_id is None:
            raise ChatGPTConfigError(f"ChatGPT model {model.model!r} has no catalog identity")
        if provider_id not in models_by_provider:
            models_by_provider[provider_id] = []
            emitted_providers.append(model)
        models_by_provider[provider_id].append(catalog_id)

    projected = []
    for model in emitted_providers:
        provider_id = model.chatgpt_provider_id
        assert provider_id is not None
        variable = secret_variable(model.api_key_reference)
        if model.chatgpt_wire_api != "responses":
            raise ChatGPTConfigError(
                f"ChatGPT provider {provider_id!r} must use the responses wire API"
            )
        if not _safe_endpoint(model.base_url):
            raise ChatGPTConfigError(f"ChatGPT provider {provider_id!r} has an unsafe base URL")
        projected.append(
            {
                "id": provider_id,
                "name": model.provider_name or model.provider_key,
                "base_url": model.base_url,
                "env_key": variable,
                "wire_api": "responses",
                "models": models_by_provider[provider_id],
            }
        )
    return tuple(projected)


def _models_by_provider(
    models: Sequence[ResolvedModel],
) -> dict[str, tuple[ResolvedModel, ...]]:
    grouped: dict[str, list[ResolvedModel]] = {}
    for model in models:
        if not _CHATGPT_PROFILE_KEY_RE.fullmatch(model.provider_key):
            raise AdapterPlanError(
                f"ChatGPT provider key is not a safe profile name: {model.provider_key!r}"
            )
        grouped.setdefault(model.provider_key, []).append(model)
    return {key: tuple(value) for key, value in grouped.items()}


def _default_provider_key(models: Sequence[ResolvedModel]) -> str:
    grouped = _models_by_provider(models)
    defaults = {model.provider_key for model in models if model.chatgpt_default}
    if not defaults and len(grouped) == 1:
        return next(iter(grouped))
    if len(defaults) != 1:
        raise AdapterPlanError("exactly one ChatGPT provider must be marked default")
    return next(iter(defaults))


def _load_document(source: ArtifactSnapshot) -> ParsedToml:
    if isinstance(source, AbsentDestination):
        return load_toml_bytes(b"")
    if isinstance(source, bytes):
        return load_toml_bytes(source)
    raise AdapterPlanError("ChatGPT artifact snapshot is absent")


def _artifact_hashes(ownership: AdapterOwnership) -> dict[PurePosixPath, str]:
    raw = ownership.get("artifactHashes", {})
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AdapterPlanError("ChatGPT ownership artifactHashes must be an object")
    result: dict[PurePosixPath, str] = {}
    for raw_path, digest in raw.items():
        if not isinstance(raw_path, str) or not isinstance(digest, str):
            raise AdapterPlanError("ChatGPT ownership artifactHashes must contain strings")
        path = PurePosixPath(raw_path)
        if not _safe_managed_artifact_path(path) or not _SHA256_HEX_RE.fullmatch(digest):
            raise AdapterPlanError("ChatGPT ownership artifactHashes contains an invalid entry")
        result[path] = digest
    return result


def _safe_managed_artifact_path(path: PurePosixPath) -> bool:
    if path.is_absolute() or len(path.parts) != 1:
        return False
    name = path.name
    if name in {"config.toml", _CHATGPT_LEGACY_PROFILE, CODEX_MODEL_CATALOG_FILENAME}:
        return True
    if name.endswith(".config.toml"):
        return bool(_CHATGPT_PROFILE_KEY_RE.fullmatch(name.removesuffix(".config.toml")))
    if name.startswith("modfig-") and name.endswith("-catalog.json"):
        return bool(_CHATGPT_PROFILE_KEY_RE.fullmatch(name[7:-13]))
    return False


def _legacy_owned_hashes(ownership: AdapterOwnership) -> dict[PurePosixPath, str]:
    hashes = _artifact_hashes(ownership)
    if _CHATGPT_LEGACY_CATALOG_ARTIFACT.relative_path not in hashes:
        legacy_catalog = ownership.get("catalogSha256")
        if isinstance(legacy_catalog, str) and _SHA256_HEX_RE.fullmatch(legacy_catalog):
            hashes[_CHATGPT_LEGACY_CATALOG_ARTIFACT.relative_path] = legacy_catalog
    return {
        path: digest
        for path, digest in hashes.items()
        if path
        in {
            _CHATGPT_LEGACY_PROFILE_ARTIFACT.relative_path,
            _CHATGPT_LEGACY_CATALOG_ARTIFACT.relative_path,
        }
    }


def _check_owned_or_planned_catalog(
    current: ArtifactSnapshot,
    planned: bytes,
    ownership: AdapterOwnership,
    path: PurePosixPath,
) -> None:
    if isinstance(current, AbsentDestination):
        return
    if not isinstance(current, bytes):
        raise AdapterPlanError(f"ChatGPT catalog snapshot is invalid at {path!s}")
    current_sha = hashlib.sha256(current).hexdigest()
    planned_sha = hashlib.sha256(planned).hexdigest()
    if current_sha == planned_sha:
        return
    if _artifact_hashes(ownership).get(path) == current_sha:
        return
    raise AdapterPlanError(f"ChatGPT catalog drift at {path!s}; refusing overwrite")


def _reconcile_single_provider(
    document: TOMLDocument,
    projected: Mapping[str, object],
    previous: Mapping[str, str],
    managed_catalog_json: str,
    legacy_catalog_json: str | None = None,
    owned_catalog_jsons: Collection[str] = (),
) -> None:
    _ensure_managed_catalog_pointer(
        document, managed_catalog_json, legacy_catalog_json, owned_catalog_jsons
    )
    root = cast(MutableMapping[str, Any], document)
    profiles = root.get("profiles")
    if isinstance(profiles, Mapping):
        for profile_id, profile in profiles.items():
            if isinstance(profile, Mapping) and any(
                key in profile for key in ("model_providers", "catalog", "model_catalog_json")
            ):
                raise AdapterPlanError(
                    f"ChatGPT profile {profile_id!r} has a managed catalog override"
                )
    elif profiles is not None:
        raise AdapterPlanError("ChatGPT profiles must be a table")
    raw_providers = root.get("model_providers")
    if raw_providers is None:
        providers: MutableMapping[str, Any] = tomlkit.table()
        root["model_providers"] = providers
    elif isinstance(raw_providers, Mapping):
        providers = cast(MutableMapping[str, Any], raw_providers)
    else:
        raise AdapterPlanError("ChatGPT model_providers must be a table")

    provider_id = projected.get("id")
    if not isinstance(provider_id, str) or not provider_id:
        raise AdapterPlanError("ChatGPT projected provider ID is invalid")
    generated = {key: value for key, value in projected.items() if key != "id"}
    existing = providers.get(provider_id)
    if existing is not None:
        if not isinstance(existing, Mapping):
            raise AdapterPlanError(f"ChatGPT provider {provider_id!r} must be a table")
        existing_fingerprint = _provider_fingerprint(existing)
        generated_fingerprint = _provider_fingerprint(generated)
        if existing_fingerprint != generated_fingerprint and (
            previous.get(provider_id) != existing_fingerprint
        ):
            raise AdapterPlanError(
                f"ChatGPT provider collision or drift for {provider_id!r}; refusing overwrite"
            )
    table = tomlkit.table()
    for key, value in generated.items():
        table[key] = value
    providers[provider_id] = table

    for existing_id in tuple(providers):
        if not isinstance(existing_id, str) or not existing_id.startswith(_CHATGPT_PROVIDER_PREFIX):
            continue
        if existing_id == provider_id:
            continue
        existing_value = providers[existing_id]
        if existing_id not in previous:
            raise AdapterPlanError(
                f"unowned ChatGPT provider {existing_id!r} is reserved; refusing overwrite"
            )
        if (
            not isinstance(existing_value, Mapping)
            or _provider_fingerprint(existing_value) != previous[existing_id]
        ):
            raise AdapterPlanError(f"ChatGPT stale provider {existing_id!r} has drifted")
        del providers[existing_id]

    models = generated.get("models")
    if (
        not isinstance(models, Sequence)
        or not models
        or not all(isinstance(model, str) for model in models)
    ):
        raise AdapterPlanError("ChatGPT projected provider has no selectable models")
    selected_provider = _selected_provider(document)
    selected_model = _selected_model(document)
    if (
        selected_provider == provider_id
        and selected_model is not None
        and selected_model not in models
    ):
        raise AdapterPlanError(
            f"ChatGPT selected model {selected_model!r} is absent from provider "
            f"{provider_id!r}; refusing to change selection"
        )
    root["model_provider"] = provider_id
    root["model"] = selected_model if selected_provider == provider_id else models[0]


def load_toml_bytes(source: bytes) -> ParsedToml:
    try:
        text = source.decode("utf-8")
        return ParsedToml(source, tomlkit.parse(text))
    except (UnicodeError, TOMLKitError) as exc:
        raise ChatGPTConfigError("cannot parse ChatGPT config snapshot") from exc


def _chatgpt_runtime(proof: RuntimeProof | None) -> ChatGPTRuntime:
    if proof is None or not isinstance(proof.provenance, ChatGPTRuntime):
        raise AdapterPlanError("ChatGPT runtime proof is unavailable")
    return proof.provenance


def _chatgpt_proof_path() -> Path:
    configured = os.environ.get("MODFIG_CHATGPT_PROOF")
    if configured:
        return Path(configured).expanduser().absolute()
    return (Path.home() / ".modfig" / "chatgpt-runtime-proof.json").absolute()


def _provider_fingerprint(provider: Mapping[str, object]) -> str:
    value = {key: _primitive(provider[key]) for key in provider if key != "id"}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _previous_provider_fingerprints(ownership: AdapterOwnership) -> Mapping[str, str]:
    raw = ownership.get("providerFingerprints", {})
    if not isinstance(raw, Mapping):
        raise AdapterPlanError("ChatGPT ownership providerFingerprints must be an object")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
        raise AdapterPlanError("ChatGPT ownership providerFingerprints must contain strings")
    return raw


def _previous_catalog_sha256(ownership: AdapterOwnership) -> str | None:
    raw = ownership.get("catalogSha256")
    if raw is None:
        return None
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9a-f]{64}", raw):
        raise AdapterPlanError("ChatGPT ownership catalogSha256 must be a sha256 hex digest")
    return raw


def _catalog_display_name(display_name: str, provider_label: str) -> str:
    suffix = f"[{provider_label}]"
    return display_name if display_name.endswith(suffix) else f"{display_name} {suffix}"


def _ensure_managed_catalog_pointer(
    document: TOMLDocument,
    managed_catalog_json: str,
    legacy_catalog_json: str | None = None,
    owned_catalog_jsons: Collection[str] = (),
) -> None:
    root = cast(MutableMapping[str, Any], document)
    if "catalog" in root:
        raise AdapterPlanError("ChatGPT catalog override cannot be safely managed")
    existing_pointer = root.get("model_catalog_json")
    # ponytail: allow pointers to any previously owned provider catalog; only
    # the current default's and the legacy name were accepted before, which
    # blocked switching the default provider between owned profiles.
    if existing_pointer is not None and existing_pointer not in {
        managed_catalog_json,
        legacy_catalog_json,
        *owned_catalog_jsons,
    }:
        raise AdapterPlanError("unowned ChatGPT model catalog pointer; refusing overwrite")
    profiles = root.get("profiles")
    if isinstance(profiles, Mapping):
        for profile_id, profile in profiles.items():
            if isinstance(profile, Mapping) and any(
                key in profile for key in ("catalog", "model_catalog_json")
            ):
                raise AdapterPlanError(
                    f"ChatGPT profile {profile_id!r} has a managed catalog override"
                )
    elif profiles is not None:
        raise AdapterPlanError("ChatGPT profiles must be a table")
    if existing_pointer != managed_catalog_json:
        # tomlkit __setitem__ can nest after the last table; add() stays at root.
        if existing_pointer is None:
            document.add("model_catalog_json", tomlkit.item(managed_catalog_json))
        else:
            root["model_catalog_json"] = tomlkit.item(managed_catalog_json)


def _reconcile_provider_catalog(
    document: TOMLDocument,
    projected: Sequence[Mapping[str, object]],
    previous: Mapping[str, str],
    managed_catalog_json: str,
) -> None:
    _ensure_managed_catalog_pointer(document, managed_catalog_json)
    root = cast(MutableMapping[str, Any], document)
    profiles = root.get("profiles")
    if isinstance(profiles, Mapping):
        for profile_id, profile in profiles.items():
            if isinstance(profile, Mapping) and "model_providers" in profile:
                raise AdapterPlanError(
                    f"ChatGPT profile {profile_id!r} has a managed catalog override"
                )
    providers: MutableMapping[str, Any]
    raw_providers = root.get("model_providers")
    if raw_providers is None:
        providers = tomlkit.table()
        root["model_providers"] = providers
    elif not isinstance(raw_providers, Mapping):
        raise AdapterPlanError("ChatGPT model_providers must be a table")
    else:
        providers = cast(MutableMapping[str, Any], raw_providers)

    projected_ids = {
        str(provider["id"]) for provider in projected if isinstance(provider.get("id"), str)
    }
    selected_provider = _selected_provider(document)
    selected_model = _selected_model(document)
    if selected_model is not None:
        owner = _owner_provider_id(projected, selected_model)
        if owner is not None and owner != selected_provider:
            root["model_provider"] = owner
            selected_provider = owner
    if (selected_provider is None) != (selected_model is None):
        raise AdapterPlanError(
            "ChatGPT profile selection must define both model and model_provider"
        )
    if selected_provider is None and projected:
        first_provider = projected[0]
        first_models = first_provider.get("models")
        first_provider_id = first_provider.get("id")
        if (
            not isinstance(first_provider_id, str)
            or not isinstance(first_models, Sequence)
            or not first_models
            or not isinstance(first_models[0], str)
        ):
            raise AdapterPlanError("ChatGPT projected catalog has no selectable model")
        root["model_provider"] = first_provider_id
        root["model"] = first_models[0]
        selected_provider = first_provider_id
        selected_model = first_models[0]
    for existing_provider_id in providers:
        if (
            isinstance(existing_provider_id, str)
            and existing_provider_id.startswith(_CHATGPT_PROVIDER_PREFIX)
            and existing_provider_id not in projected_ids
            and existing_provider_id not in previous
        ):
            raise AdapterPlanError(
                f"unowned ChatGPT provider {existing_provider_id!r} is reserved; refusing overwrite"
            )

    for projected_provider in projected:
        projected_provider_id = projected_provider.get("id")
        if not isinstance(projected_provider_id, str) or not projected_provider_id:
            raise AdapterPlanError("ChatGPT projected provider ID is invalid")
        generated = {key: value for key, value in projected_provider.items() if key != "id"}
        existing = providers.get(projected_provider_id)
        if existing is None:
            table = tomlkit.table()
            for key, value in generated.items():
                table[key] = value
            providers[projected_provider_id] = table
        else:
            if not isinstance(existing, Mapping):
                raise AdapterPlanError(
                    f"ChatGPT provider {projected_provider_id!r} must be a table"
                )
            existing_fingerprint = _provider_fingerprint(existing)
            generated_fingerprint = _provider_fingerprint(generated)
            if existing_fingerprint != generated_fingerprint and (
                previous.get(projected_provider_id) != existing_fingerprint
            ):
                raise AdapterPlanError(
                    f"ChatGPT provider collision or drift for {projected_provider_id!r}; "
                    "refusing overwrite"
                )
            if existing_fingerprint != generated_fingerprint:
                table = tomlkit.table()
                for key, value in generated.items():
                    table[key] = value
                providers[projected_provider_id] = table
        generated_models = generated.get("models")
        if projected_provider_id == selected_provider and (
            selected_model is None
            or not isinstance(generated_models, Sequence)
            or selected_model not in generated_models
        ):
            raise AdapterPlanError(
                f"ChatGPT selected model {selected_model!r} is absent from provider "
                f"{projected_provider_id!r}; refusing to change selection"
            )

    for provider_id, fingerprint in previous.items():
        if provider_id in projected_ids:
            continue
        existing = providers.get(provider_id)
        if existing is None:
            continue
        if not isinstance(existing, Mapping) or _provider_fingerprint(existing) != fingerprint:
            raise AdapterPlanError(
                f"ChatGPT stale provider {provider_id!r} has drifted; refusing removal"
            )
        if _selected_provider(document) == provider_id:
            raise AdapterPlanError(
                f"ChatGPT stale provider {provider_id!r} is currently selected; refusing removal"
            )
        del providers[provider_id]


def _catalog_slugs(source: bytes) -> tuple[str, ...]:
    try:
        document = json.loads(
            source.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, _StrictJsonError):
        raise AdapterPlanError("written ChatGPT model catalog is not strict JSON") from None
    if not isinstance(document, Mapping) or set(document) != {"models"}:
        raise AdapterPlanError("written ChatGPT model catalog must be a single models object")
    models = document.get("models")
    if (
        not isinstance(models, list)
        or not models
        or not all(isinstance(entry, Mapping) for entry in models)
    ):
        raise AdapterPlanError("written ChatGPT model catalog has no model entries")
    slugs: list[str] = []
    for entry in models:
        slug = entry.get("slug")
        label = entry.get("display_name")
        if not isinstance(slug, str) or not slug or not isinstance(label, str) or not label:
            raise AdapterPlanError("written ChatGPT model catalog entry lacks slug or display_name")
        if slug in slugs:
            raise AdapterPlanError(f"written ChatGPT model catalog duplicates slug {slug!r}")
        slugs.append(slug)
    return tuple(slugs)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _primitive(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_primitive(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"__toml_scalar__": type(value).__name__, "value": str(value)}


def _selected_provider(document: Mapping[str, object]) -> str | None:
    profiles = document.get("profiles")
    profile_name = document.get("profile")
    if isinstance(profile_name, str) and isinstance(profiles, Mapping):
        profile = profiles.get(profile_name)
        if isinstance(profile, Mapping):
            profile_provider = profile.get("model_provider")
            if isinstance(profile_provider, str):
                return profile_provider
    root_provider = document.get("model_provider")
    return root_provider if isinstance(root_provider, str) else None


def _selected_model(document: Mapping[str, object]) -> str | None:
    model = document.get("model")
    return model if isinstance(model, str) else None


def _owner_provider_id(
    projected: Sequence[Mapping[str, object]], selected_model: str
) -> str | None:
    owner: str | None = None
    for provider in projected:
        provider_id = provider.get("id")
        models = provider.get("models")
        if (
            isinstance(provider_id, str)
            and isinstance(models, Sequence)
            and selected_model in models
        ):
            if owner is not None:
                raise AdapterPlanError(
                    f"ChatGPT selected model {selected_model!r} is owned by multiple providers"
                )
            owner = provider_id
    return owner


def _managed_models(document: Mapping[str, object]) -> set[str]:
    providers = document.get("model_providers")
    if not isinstance(providers, Mapping):
        raise AdapterPlanError("written ChatGPT config has no provider table")
    managed: set[str] = set()
    for provider_id, table in providers.items():
        if isinstance(provider_id, str) and provider_id.startswith(_CHATGPT_PROVIDER_PREFIX):
            if not isinstance(table, Mapping):
                raise AdapterPlanError("written ChatGPT provider table is invalid")
            models = table.get("models")
            if not isinstance(models, Sequence) or not all(
                isinstance(item, str) for item in models
            ):
                raise AdapterPlanError("written ChatGPT provider models are invalid")
            managed.update(models)
    return managed


def _table(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    if key not in document:
        return {}
    value = document[key]
    if not isinstance(value, Mapping):
        raise ChatGPTConfigError(f"ChatGPT {key!r} must be a TOML table")
    return value


def inspect_chatgpt(
    path: Path, registry: Registry, environ: Mapping[str, str] | None = None
) -> ChatGPTInspection:
    document = load_chatgpt_config(path).document
    raw_providers = _table(document, "model_providers")
    for provider_id, value in raw_providers.items():
        if not isinstance(value, Mapping):
            raise ChatGPTConfigError(f"ChatGPT provider {str(provider_id)!r} must be a TOML table")
    profiles = _table(document, "profiles")
    catalog_pointer = document.get("model_catalog_json")
    managed_catalog_pointer = (
        isinstance(catalog_pointer, str)
        and Path(catalog_pointer).parent == path.parent
        and Path(catalog_pointer).name.startswith("modfig-")
        and Path(catalog_pointer).name.endswith("-catalog.json")
    )
    if "catalog" in document or ("model_catalog_json" in document and not managed_catalog_pointer):
        raise ChatGPTConfigError("ChatGPT catalog override cannot be safely inspected")

    selected_profile: str | None = None
    selected_provider: str | None = None
    if "profile" in document:
        raw_profile = document.get("profile")
        if not isinstance(raw_profile, str) or not raw_profile:
            raise ChatGPTConfigError("ChatGPT 'profile' must be a non-empty string")
        if raw_profile not in profiles:
            raise ChatGPTConfigError(
                f"ChatGPT selected profile {raw_profile!r} has no matching table"
            )
        selected_profile = raw_profile
        selected = profiles[selected_profile]
        if not isinstance(selected, Mapping):
            raise ChatGPTConfigError(f"selected profile {selected_profile!r} must be a TOML table")
        if "model_provider" in selected:
            raw_selected_provider = selected["model_provider"]
            if not isinstance(raw_selected_provider, str) or not raw_selected_provider:
                raise ChatGPTConfigError(
                    f"selected profile {selected_profile!r} model_provider must be a "
                    "non-empty string"
                )
            selected_provider = raw_selected_provider
        if (selected_provider is not None and selected_provider.startswith("modfig-")) or any(
            key in selected for key in ("model_providers", "catalog", "model_catalog_json")
        ):
            raise ChatGPTConfigError(
                f"selected profile {selected_profile!r} has a managed ChatGPT override"
            )

    projected = project_chatgpt_providers(registry, environ)
    planned = {
        str(provider["id"]): {key: value for key, value in provider.items() if key != "id"}
        for provider in projected
    }
    provider_ids = tuple(str(key) for key in raw_providers)
    managed = tuple(
        provider_id
        for provider_id in provider_ids
        if provider_id in planned and _primitive(raw_providers[provider_id]) == planned[provider_id]
    )
    collisions = tuple(
        provider_id
        for provider_id in provider_ids
        if provider_id in planned and provider_id not in managed
    )
    if collisions:
        raise ChatGPTConfigError(
            f"ChatGPT provider collision for {collisions[0]!r}; existing values are unowned"
        )

    stale = tuple(
        provider_id
        for provider_id in provider_ids
        if provider_id.startswith("modfig-") and provider_id not in planned
    )
    if stale:
        raise ChatGPTConfigError(
            f"ChatGPT provider collision for {stale[0]!r}; existing values are unowned"
        )
    active_provider: str | None = None
    if selected_provider is not None:
        active_provider = selected_provider
    elif "model_provider" in document:
        raw_active = document.get("model_provider")
        if not isinstance(raw_active, str):
            raise ChatGPTConfigError("ChatGPT 'model_provider' must be a string")
        active_provider = raw_active
    if active_provider is not None and active_provider in stale:
        raise ChatGPTConfigError(
            f"selected ChatGPT provider {active_provider!r} is an unowned stale candidate"
        )

    foreign = tuple(provider_id for provider_id in provider_ids if provider_id not in managed)
    missing = tuple(provider_id for provider_id in planned if provider_id not in managed)
    if active_provider is not None and active_provider in missing:
        raise ChatGPTConfigError(
            f"ChatGPT active provider {active_provider!r} is missing from the config"
        )
    changes = missing + stale
    diff = "\n".join(
        [*(f"+ [model_providers.{provider_id}]" for provider_id in missing)]
        + [*(f"~ [model_providers.{provider_id}]" for provider_id in stale)]
    )
    return ChatGPTInspection(
        active_model=_string(document.get("model")),
        active_provider=active_provider,
        foreign_provider_ids=foreign,
        managed_provider_ids=managed,
        planned_provider_ids=tuple(planned),
        selected_profile=selected_profile,
        profile_override=False,
        catalog_supported=True,
        requires_catalog_proof=True,
        changed=bool(changes),
        diff=diff,
    )


def apply_chatgpt(path: Path, registry: Registry, environ: Mapping[str, str] | None = None) -> None:
    del path, registry, environ
    raise CapabilityUnavailableError(
        "direct ChatGPT mutation is unavailable; use `modfig apply --target chatgpt` "
        "for the transactional adapter"
    )
